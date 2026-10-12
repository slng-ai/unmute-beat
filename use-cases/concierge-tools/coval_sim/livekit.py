"""What a Coval LiveKit job needs on this side: a token endpoint, the worker's readiness, the room binding.

Coval reaches a LiveKit agent by asking a token endpoint for a room token, joining that room on the
LiveKit server, and waiting for the agent to join it. The endpoint here (`TokenServer`) is a small
stdlib HTTP server behind the job's cloudflared tunnel. Each token it mints dispatches the job's own
worker by name (`roomConfig.agents`), whatever the request asked for, so a request can only ever bring
our worker into a room. Every request it answers is recorded with the `X-Coval-Simulation-Id` header
Coval fills in: that record is the only evidence binding a simulation to its room, and the room name is
the call's Langfuse session (`bind_rooms`, for `load_report.py --call SIMULATION=ROOM`).

The token is the JWT LiveKit's own `AccessToken.to_jwt` writes (livekit-api, `access_token.py`):
HS256, `iss` the API key, `sub` the identity, `name`, `nbf`, `exp`, the `video` grant in camelCase and
`roomConfig` as protobuf JSON. Minted with `hmac` and `hashlib`, so this tool imports no LiveKit package.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TOKEN_TTL_S = 3600
"""How long a minted token may be used to join: an hour, far longer than any call."""
MAX_BODY = 16_384
"""The largest token request read. Coval's is a few hundred bytes."""
SIMULATION_HEADER = 'X-Coval-Simulation-Id'
"""The header the Coval agent is told to send (`connection`), filled with each simulation's ID by Coval."""
REGISTERED = re.compile(r'(?<![A-Za-z])registered worker\b')
"""LiveKit's log line once a worker has registered with the server (`livekit/agents/worker.py`,
`AgentServer._handle_register`: `logger.info("registered worker", extra={"agent_name": ...})`).
In `start` mode the line is JSON, with the extras on the same line; in `dev` it is coloured text, extras
after the message. Either way the agent name is on the line."""
NAME = r'^[A-Za-z0-9_.:@-]{1,128}$'
"""What a room or participant name may be. Coval sends a UUID and `simulated_user`."""


def connection(host: str, server_url: str, agent_name: str, key: str) -> dict[str, Any]:
    """The Coval agent's LiveKit connection: our token endpoint, the LiveKit server, the worker's name.

    `generate_token_headers` is a JSON string, as Coval takes it. It carries the job's own key, which
    the endpoint requires (`TokenServer`), and the simulation ID, which Coval fills in per call.
    """
    return {
        'generate_token_endpoint': f'https://{host}/token',
        'livekit_url': server_url,
        'livekit_agent_name': agent_name,
        'generate_token_headers': json.dumps(
            {
                SIMULATION_HEADER: '{{simulation_id}}',
                'Authorization': f'Bearer {key}',
            }
        ),
    }


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, separators=(',', ':')).encode()


def mint(
    api_key: str,
    api_secret: str,
    *,
    room: str,
    identity: str,
    agent_name: str,
    ttl_s: int = TOKEN_TTL_S,
    now: float | None = None,
) -> str:
    """A LiveKit access token to join `room` as `identity`, which dispatches `agent_name` into the room.

    The claims are the ones `AccessToken.to_jwt` writes for the same grants, key for key. The room
    configuration's dispatch takes effect when this join creates the room, which a fresh room name does.
    """
    issued = int(time.time() if now is None else now)
    claims = {
        'name': identity,
        'video': {
            'roomJoin': True,
            'room': room,
            'canPublish': True,
            'canSubscribe': True,
            'canPublishData': True,
        },
        'roomConfig': {'agents': [{'agentName': agent_name}]},
        'sub': identity,
        'iss': api_key,
        'nbf': issued,
        'exp': issued + ttl_s,
    }
    signing = f'{_b64(_json({"alg": "HS256", "typ": "JWT"}))}.{_b64(_json(claims))}'
    signature = hmac.new(api_secret.encode(), signing.encode(), hashlib.sha256).digest()
    return f'{signing}.{_b64(signature)}'


class TokenRequest(BaseModel):
    """What Coval posts to the token endpoint. Its `room_config` is read by nobody: the token names our worker."""

    model_config = ConfigDict(extra='ignore')

    room_name: str = Field(pattern=NAME)
    participant_name: str = Field('simulated_user', pattern=NAME)


class TokenServer:
    """The job's token endpoint: `POST /token` on 127.0.0.1, one thread per request, until the job ends.

    It answers `{token, serverUrl, room_name}` and records each answer, one JSON line in `records`:
    when, the room, the identity, the worker dispatched, and the simulation ID from the
    `X-Coval-Simulation-Id` header (None when Coval sent none, or sent the placeholder unfilled).
    Anything else is refused with a 4xx and recorded nowhere: a request without the job's key (401), a
    path other than `/token` (404), a method other than POST (405), no `Content-Length` (411), a body
    that is not one JSON object with a valid `room_name` (400), or one too large (413).

    Args:
        api_key: The LiveKit API key, the token's issuer.
        api_secret: The LiveKit API secret the token is signed with.
        server_url: The LiveKit server Coval joins (`LIVEKIT_URL`), sent back as `serverUrl`.
        agent_name: The job's worker, dispatched into every room a token is minted for.
        records: The JSONL file each answered request is appended to.
        key: The bearer key a request must carry; the Coval agent's headers hold it (`connection`).
        port: The local port; 0 picks a free one, read back from `port`.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        server_url: str,
        agent_name: str,
        records: Path,
        *,
        key: str,
        port: int = 0,
    ) -> None:
        if not (api_key and api_secret and server_url and agent_name and key):
            raise ValueError(
                'a token server needs the API key and secret, the server URL, an agent name and a key'
            )
        self.api_key, self.api_secret, self.server_url = api_key, api_secret, server_url
        self.agent_name, self.records, self.key = agent_name, records, key
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """One request, answered by the server that owns it."""

            def do_POST(self) -> None:
                owner._answer(self)

            def do_GET(self) -> None:
                owner._send(self, 405, {'error': 'POST a JSON body to /token'})

            def log_message(self, format: str, *args: Any) -> None:
                """Silent: every answered request is in `records`, and the job's output stays readable."""

        self._http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self._http.daemon_threads = True
        self.port: int = self._http.server_address[1]
        self._thread = threading.Thread(target=self._http.serve_forever, name='token server', daemon=True)

    def __enter__(self) -> Self:
        self.records.parent.mkdir(parents=True, exist_ok=True)
        self._thread.start()
        return self

    def __exit__(
        self, kind: type[BaseException] | None, error: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._http.shutdown()
        self._http.server_close()

    @staticmethod
    def _send(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
        data = _json(body)
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        if status == 405:
            handler.send_header('Allow', 'POST')
        handler.end_headers()
        handler.wfile.write(data)

    def _answer(self, handler: BaseHTTPRequestHandler) -> None:
        """Check one request, mint its token, record it, answer it. The record is written before the answer.

        The body is read before anything is refused: a socket closed with a
        request still unread is reset, and the client may lose the refusal.
        """
        try:
            length = int(handler.headers.get('Content-Length', ''))
        except ValueError:
            return self._send(handler, 411, {'error': 'Content-Length is required'})
        if length > MAX_BODY:
            return self._send(handler, 413, {'error': f'a token request is at most {MAX_BODY} bytes'})
        body = handler.rfile.read(length) if length > 0 else b''
        if handler.path.split('?', 1)[0] != '/token':
            return self._send(handler, 404, {'error': 'the token endpoint is /token'})
        if not hmac.compare_digest(
            handler.headers.get('Authorization', '').encode(), f'Bearer {self.key}'.encode()
        ):
            return self._send(
                handler, 401, {'error': "missing or wrong key: the Coval agent's headers carry it"}
            )
        try:
            request = TokenRequest.model_validate_json(body)
        except ValidationError as error:
            fields = ', '.join('.'.join(str(p) for p in e['loc']) or 'body' for e in error.errors())
            return self._send(handler, 400, {'error': f'not a token request: {fields}'})
        simulation = handler.headers.get(SIMULATION_HEADER, '').strip()
        token = mint(
            self.api_key,
            self.api_secret,
            room=request.room_name,
            identity=request.participant_name,
            agent_name=self.agent_name,
        )
        self._record(
            {
                'at': datetime.now(UTC).isoformat(),
                'room': request.room_name,
                'identity': request.participant_name,
                'agent_name': self.agent_name,
                'simulation_id': simulation if simulation and '{{' not in simulation else None,
            }
        )
        return self._send(
            handler, 200, {'token': token, 'serverUrl': self.server_url, 'room_name': request.room_name}
        )

    def _record(self, line: dict[str, Any]) -> None:
        with self._lock, self.records.open('a') as out:
            out.write(json.dumps(line) + '\n')
            out.flush()


def token_requests(records: Path) -> list[dict[str, Any]]:
    """Every request a token server answered, in order. A missing file is none; a torn last line is skipped."""
    if not records.exists():
        return []
    lines = []
    for text in records.read_text().splitlines():
        try:
            lines.append(json.loads(text))
        except ValueError:
            continue
    return lines


def bind_rooms(simulation_ids: Iterable[str], requests: list[dict[str, Any]]) -> dict[str, str]:
    """Which room each simulation joined, by the ID Coval sent with its token request, never by order.

    A simulation binds to a room when every request carrying its ID named that one room, and no other
    simulation's ID named it too. Anything else is 'unknown': no request carried its ID (Coval sent
    none), it asked for two rooms, or a room was asked for by two simulations.
    """
    rooms: dict[str, set[str]] = {}
    for request in requests:
        if request.get('simulation_id'):
            rooms.setdefault(str(request['simulation_id']), set()).add(str(request['room']))
    bound = {
        sim: next(iter(rooms[sim])) if len(rooms.get(sim, ())) == 1 else 'unknown' for sim in simulation_ids
    }
    claimed = [room for sim, asked in rooms.items() for room in asked]
    return {
        sim: room if room == 'unknown' or claimed.count(room) == 1 else 'unknown'
        for sim, room in bound.items()
    }


def registered(output: str, agent_name: str) -> bool:
    """Whether a worker's log says it registered with the server under `agent_name`."""
    return any(REGISTERED.search(line) and agent_name in line for line in output.splitlines())
