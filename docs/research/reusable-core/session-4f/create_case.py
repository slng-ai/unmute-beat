"""Create session 4f's one new Coval test set and case (call 2) from coval-fixtures.v1.json. Free: places no call.

    python3 create_case.py /path/to/.env

Reads COVAL_API_KEY from the given env file and never prints it. Writes the new ids back into the fixture file,
and refuses to run if the case already has an id, so it cannot make a second set by accident.
"""

import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = HERE / 'coval-fixtures.v1.json'


def key(env: Path) -> str:
    for line in env.read_text().splitlines():
        if line.startswith('COVAL_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit('no COVAL_API_KEY in the env file')


def post(path: str, body: dict, api_key: str) -> dict:
    request = urllib.request.Request(
        f'https://api.coval.dev/v1{path}',
        data=json.dumps(body).encode(),
        headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> None:
    api_key = key(Path(sys.argv[1]))
    fixtures = json.loads(FIXTURES.read_text())
    [case] = [c for c in fixtures['calls'] if c.get('new')]
    if case['coval_test_set'] or case['coval_test_case']:
        raise SystemExit(f'already created: {case["coval_test_set"]} {case["coval_test_case"]}')
    made = post(
        '/test-sets',
        {
            'display_name': 'reusable-core session 4f readiness v1: question, no, repeat, move',
            'description': 'One targeted call: a question during a read-back, an explicit no, a request to repeat a '
            'receipt, and a move of the booking just made.',
        },
        api_key,
    )
    test_set = made.get('test_set', made)
    set_id = test_set.get('id') or test_set.get('test_set_id')
    added = post(
        '/test-cases',
        {
            'test_set_id': set_id,
            'input_str': case['input'],
            'expected_behaviors': case['expected_behaviors'],
            'description': case['key'],
        },
        api_key,
    )
    test_case = added.get('test_case', added)
    case['coval_test_set'] = set_id
    case['coval_test_case'] = test_case.get('id') or test_case.get('test_case_id')
    FIXTURES.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False) + '\n')
    print('created', case['coval_test_set'], case['coval_test_case'])


if __name__ == '__main__':
    main()
