"""Compare two models on the frozen POC using the existing five-scenario runner.

Run: python -m bench.model-evidence.run --output bench/raw/model-matched --pairs 20
Only the benchmark process overrides agent models; no production source changes.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from bench import native_compare

MODELS = ('gemini-3.1-flash-lite', 'gemini-3.5-flash-lite')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument('--model', choices=MODELS)
    parser.add_argument('--scenario', choices=native_compare.SCENARIOS)
    args = parser.parse_args()
    assert not os.getenv('BENCH_PINNED'), 'Use the normal priority configuration in both arms'
    if args.model:
        assert args.scenario and not args.output.exists()
        from salon.agents import AGENTS, SETTINGS
        from utils.arm import settings

        model_name = f'google-cloud:{args.model}'
        provenance = settings() | {
            'model': model_name,
            'model_pinned': False,
            'fixture': 'fresh',
            'salon_clock': native_compare.CLOCK.isoformat(),
            'override': 'Agent.override(model=...), identical default model settings',
        }
        assert all(node.agent.model_settings == SETTINGS for node in AGENTS.values())
        sys.argv = ['bench.native_compare', 'poc', args.scenario, '--output', str(args.output)]
        try:
            with ExitStack() as stack:
                for node in AGENTS.values():
                    stack.enter_context(node.agent.override(model=model_name))
                asyncio.run(native_compare.main())
        finally:
            if args.output.exists():
                result = json.loads(args.output.read_text())
                result['settings'] = provenance
                args.output.write_text(json.dumps(result, indent=2) + '\n')
        return

    assert args.pairs > 0 and not args.output.exists(), 'Use a new destination for each cohort'
    args.output.mkdir(parents=True)
    for repeat in range(args.pairs):
        for scenario in (args.scenario,) if args.scenario else native_compare.SCENARIOS:
            for model in MODELS if repeat % 2 == 0 else reversed(MODELS):
                destination = args.output / f'{scenario}-{repeat:02d}-{model}.json'
                command = [
                    sys.executable,
                    '-B',
                    '-m',
                    'bench.model-evidence.run',
                    '--model',
                    model,
                    '--scenario',
                    scenario,
                    '--output',
                    str(destination),
                ]
                with destination.with_suffix('.log').open('w') as log:
                    try:
                        completed = subprocess.run(
                            command, stdout=log, stderr=subprocess.STDOUT, timeout=90, check=False
                        )
                        exit_code = completed.returncode
                    except subprocess.TimeoutExpired:
                        exit_code = 124
                result = (
                    json.loads(destination.read_text())
                    if destination.exists()
                    else {
                        'arm': 'poc',
                        'scenario': scenario,
                        'rows': [],
                        'errors': ['No output artifact'],
                    }
                )
                if exit_code:
                    result['errors'].append(f'Process exit {exit_code}')
                result.update(model=model, repeat=repeat, process_exit=exit_code)
                destination.write_text(json.dumps(result, indent=2) + '\n')
                print(
                    json.dumps(
                        {
                            'pair': repeat,
                            'scenario': scenario,
                            'model': model,
                            'exit': exit_code,
                            'errors': result['errors'],
                        }
                    ),
                    flush=True,
                )


if __name__ == '__main__':
    main()
