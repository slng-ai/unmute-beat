"""Create the two qualification test sets and their 20 cases in Coval, and attach both to the agent. Free: no call.

    cd use-cases/concierge-tools && uv run python ../../docs/qualification/final-qualification/make_sets.py ENV

Reads COVAL_API_KEY from the env file and never prints it. Writes each new ID into `cases.v1.json` as soon as
Coval returns it, and refuses a case or set that already has one, so running it again never makes a second
copy. The agent keeps every set it already has.
"""

import json
import sys
import urllib.request
from pathlib import Path

CASES = Path(__file__).with_name('cases.v1.json')
API = 'https://api.coval.dev/v1'


def key(env: Path) -> str:
    for line in env.read_text().splitlines():
        if line.startswith('COVAL_API_KEY='):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit('no COVAL_API_KEY in the env file')


def call(method: str, path: str, body: dict | None, api_key: str) -> dict:
    request = urllib.request.Request(
        f'{API}{path}',
        data=json.dumps(body).encode() if body is not None else None,
        headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def save(doc: dict) -> None:
    CASES.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n')


def main() -> None:
    api_key = key(Path(sys.argv[1]))
    doc = json.loads(CASES.read_text())
    for name, cohort in doc['cohorts'].items():
        if not cohort['coval_test_set']:
            made = call(
                'POST',
                '/test-sets',
                {
                    'display_name': f'reusable-core final qualification {name} v1',
                    'description': f'Ten {name} callers, one task each, on their own synthetic numbers and a '
                    'cohort diary seeded before the run (docs/qualification/final-qualification/cases.v1.json).',
                },
                api_key,
            )
            made = made.get('test_set', made)
            cohort['coval_test_set'] = made.get('id') or made.get('test_set_id')
            save(doc)
            print('set', name, cohort['coval_test_set'])
        for case in cohort['cases']:
            if case['coval_test_case']:
                continue
            added = call(
                'POST',
                '/test-cases',
                {
                    'test_set_id': cohort['coval_test_set'],
                    'input_str': case['input'],
                    'expected_behaviors': [case['trigger']],
                    'description': case['key'],
                },
                api_key,
            )
            added = added.get('test_case', added)
            case['coval_test_case'] = added.get('id') or added.get('test_case_id')
            save(doc)
            print('case', case['key'], case['coval_test_case'])
    agent = call('GET', f'/agents/{doc["agent"]}', None, api_key)
    agent = agent.get('agent', agent)
    sets = list(agent.get('test_set_ids') or [])
    wanted = [c['coval_test_set'] for c in doc['cohorts'].values() if c['coval_test_set'] not in sets]
    if wanted:
        updated = call('PATCH', f'/agents/{doc["agent"]}', {'test_set_ids': [*sets, *wanted]}, api_key)
        print('agent sets', (updated.get('agent', updated)).get('test_set_ids'))


if __name__ == '__main__':
    main()
