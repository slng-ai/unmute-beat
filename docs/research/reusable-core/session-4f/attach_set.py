"""Attach one test set to a Coval agent, keeping every set it already has. Free: places no call.

    python attach_set.py /path/to/.env AGENT_ID TEST_SET_ID

Reads COVAL_API_KEY from the env file and never prints it.
"""

import json
import sys
import urllib.request
from pathlib import Path

from create_case import key


def call(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        f'https://api.coval.dev/v1{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> None:
    api_key = key(Path(sys.argv[1]))
    agent_id, set_id = sys.argv[2], sys.argv[3]
    agent = call('GET', f'/agents/{agent_id}', api_key)
    agent = agent.get('agent', agent)
    sets = list(agent.get('test_set_ids') or [])
    print('before', sets)
    if set_id in sets:
        return
    updated = call('PATCH', f'/agents/{agent_id}', api_key, {'test_set_ids': [*sets, set_id]})
    print('after', (updated.get('agent', updated)).get('test_set_ids'))


if __name__ == '__main__':
    main()
