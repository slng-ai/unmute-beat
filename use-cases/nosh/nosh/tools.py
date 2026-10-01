"""The place tools Nosh calls: four read-only routes of `tools-agent-infra`, hosted on Render.

    tools = Tools()                                  # TOOLS_URL and TOOLS_API_KEY from the environment
    tools.post('resolve-location', {'query': 'Gothic Quarter, Barcelona'})

Every route is a `POST` with a bearer token. `resolve-location` and
`place-details` answer 404 when Google has nothing, which is `NotFoundError`
here. Any other failure is raised as it is: the caller of `post` decides
what it means. The service's `send-sms` and `calendar-invite` routes are
never called.

ponytail: stdlib `urllib`, one request at a time, no retry. Render's free
tier sleeps, so the first request after a quiet spell can take tens of
seconds; `bot.py` wakes it when a call starts.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

DEFAULT_URL = 'https://tools-agent-infra.onrender.com'


class NotFoundError(Exception):
    """Google has nothing for that location or place id."""


@dataclass
class Tools:
    """A client for the place tools. The model never sees it."""

    url: str = field(default_factory=lambda: os.getenv('TOOLS_URL') or DEFAULT_URL)
    key: str = field(default_factory=lambda: os.getenv('TOOLS_API_KEY', ''))
    timeout: float = 20.0

    def post(self, route: str, body: dict[str, Any]) -> Any:
        """One route's JSON answer. A 404 is `NotFoundError`; anything else that fails raises."""
        request = Request(
            f'{self.url.rstrip("/")}/tools/{route}',
            data=json.dumps(body).encode(),
            headers={'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404:
                raise NotFoundError(f'{route}: nothing found') from error
            raise
