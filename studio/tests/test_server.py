"""Run: uv run python -m tests.test_server

The API the web page reads, on the desk fixture, with Docker left alone (`services=False`).
"""

import time
import urllib.error
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from studio.api.app import Studio
from studio.boot import Boot
from studio.runs.store import MemoryStore
from studio.settings import Settings

SETTINGS = replace(Settings.from_env(), root=Path(__file__).parent / 'fixtures', agents='*/agent.yaml')


def unreachable(url: str) -> dict:
    raise urllib.error.URLError('connection refused')


def the_api_serves_agents_and_says_what_is_missing():
    app = Studio(SETTINGS, Boot(SETTINGS, services=False), fetch=unreachable, store=MemoryStore()).app()
    with TestClient(app) as client:
        assert client.get('/api/health').json() == {'engine': 'ok', 'store': 'memory'}

        listed = client.get('/api/agents').json()
        assert [agent['id'] for agent in listed] == ['desk'] and 'graph' not in listed[0], listed

        desk = client.get('/api/agents/desk').json()
        assert desk['graph']['entry'] == 'front' and len(desk['graph']['nodes']) == 5, desk

        missing = client.get('/api/agents/nobody')
        assert missing.status_code == 404 and 'nobody' in missing.json()['detail']

        assert client.get('/api/runs').json() == [], 'no run yet'
        nowhere = client.post('/api/runs', json={'agent': 'desk', 'channel': 'text'})
        assert nowhere.status_code == 503 and 'no runner is linked' in nowhere.json()['detail'], (
            nowhere.json()
        )
        assert client.get('/api/runners').json() == []
        assert client.get('/voice-sdk/playout.js').status_code == 200, (
            "core's voice page is served for the studio"
        )

        limits = client.get('/api/metrics/limits').json()
        assert limits['voice_to_voice'] == {'slow': 1000, 'bad': 1500}, limits
        silent = client.get('/api/metrics')
        assert silent.status_code == 503 and 'Agents page' in silent.json()['detail'], (
            'no Jaeger is said, never shown as no slow turns'
        )

        schema = client.get('/api/events/schema').json()
        assert 'discriminator' in schema, schema.keys()

        deadline = time.monotonic() + 5
        while not (boot := client.get('/api/boot').json())['ready'] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert boot['agents'] == ['desk'] and not boot['failed'], boot


if __name__ == '__main__':
    the_api_serves_agents_and_says_what_is_missing()
    print('server ok: health, agents, graph, 404, no runner said, the voice page, event schema, boot')
