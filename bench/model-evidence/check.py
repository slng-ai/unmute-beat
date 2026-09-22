"""Offline check that failed processes stay in the paired cohort.

Run: python -m bench.model-evidence.check
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

run = importlib.import_module('bench.model-evidence.run')
seen = []


def child(command, **kwargs):
    model = command[command.index('--model') + 1]
    seen.append(model)
    if len(seen) == 1:
        raise subprocess.TimeoutExpired(command, 90)
    destination = Path(command[command.index('--output') + 1])
    destination.write_text(json.dumps({'rows': [{'speech': 'retained'}], 'errors': []}))
    return subprocess.CompletedProcess(command, 7 if len(seen) == 2 else 0)


with TemporaryDirectory() as temporary:
    output = Path(temporary) / 'cohort'
    with (
        patch.object(sys, 'argv', ['run', '--output', str(output), '--pairs', '2', '--scenario', 'hours']),
        patch.object(run.subprocess, 'run', child),
    ):
        run.main()
    calls = [json.loads(path.read_text()) for path in sorted(output.glob('*.json'))]
    assert len(calls) == 4
    assert seen == [*run.MODELS, *reversed(run.MODELS)]
    assert sum(bool(call['errors']) for call in calls) == 2
    assert sorted(call['process_exit'] for call in calls) == [0, 0, 7, 124]
    assert next(call for call in calls if call['process_exit'] == 7)['rows'] == [{'speech': 'retained'}]
print('timeout, failed-process evidence and paired order ok')
