"""Record the frozen candidate and configuration before the first paid call: `config.json`, with no secret.

    cd use-cases/salon-graph && uv run python ../../docs/qualification/final-qualification/freeze.py ENV

Reads the checkout (revision, runtime status, hashes), the salon's manifest, the bot's own source for its
speech and turn-taking settings (it does not import the bot, which would build agents), the speech profile,
which environment names could override them, and Coval's agent, metric and personas. `ENV` is the `.env`
file the bot reads: only its names are recorded, never a value.
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import dotenv_values

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
SALON = ROOT / 'use-cases/salon-graph'
OVERRIDES = (
    'SLNG_BASE_URL', 'SLNG_STT_MODEL', 'SLNG_STT_LANGUAGE', 'SLNG_TTS_MODEL', 'SLNG_TTS_VOICE', 'SLNG_TTS_LANGUAGE',
    'VAD_STOP_SECS', 'MAX_CALLS', 'DIARY_CLOCK', 'CALLER_ID', 'CARRIER_NUMBER', 'DELIVERY', 'TWILIO_MARKS',
)  # fmt: skip
PACKAGES = ('pydantic', 'pydantic-ai-slim', 'pydantic-graph', 'pipecat-ai', 'langfuse', 'uvicorn', 'aiortc')


def git(*args: str) -> str:
    return subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    env = Path(sys.argv[1])
    bot = (SALON / 'bot.py').read_text()
    manifest = yaml.safe_load((SALON / 'salon/agent.yaml').read_text())
    lock = tomllib.loads((ROOT / 'uv.lock').read_text())
    versions = {p['name']: p['version'] for p in lock['package'] if p['name'] in PACKAGES}
    prompts = {
        str(p.relative_to(SALON)): sha(p) for p in sorted((SALON / 'salon/prompts').rglob('*')) if p.is_file()
    }
    runtime = ['core/src', 'use-cases/salon-graph', 'uv.lock', 'pyproject.toml']
    cases = json.loads((HERE / 'cases.v1.json').read_text())
    profile = dotenv_values(HERE / 'speech-profile.env')
    config = {
        'frozen_at': datetime.now(UTC).isoformat(),
        'revision': {
            'head': git('rev-parse', 'HEAD'),
            'branch': git('rev-parse', '--abbrev-ref', 'HEAD'),
            'pr36_head': 'ff13f1444c68de2de25093c8ddb1d8f778f7367c',
            'runtime_diff_from_pr36_head': git(
                'diff', '--stat', 'ff13f1444c68de2de25093c8ddb1d8f778f7367c', 'HEAD', '--', *runtime
            )
            or 'none',
            'runtime_status': git('status', '--porcelain', '--', *runtime) or 'clean',
            'trees': {
                path: git('rev-parse', f'HEAD:{path}') for path in ('core/src', 'use-cases/salon-graph')
            },
        },
        'dependencies': {
            'uv.lock_sha256': sha(ROOT / 'uv.lock'),
            'python': platform.python_version(),
            **versions,
        },
        'application': {
            'manifest': 'use-cases/salon-graph/salon/agent.yaml',
            'manifest_sha256': sha(SALON / 'salon/agent.yaml'),
            'app_version': manifest['app_version'],
            'prompts_sha256': prompts,
            'llm': manifest['model'],
            'model_settings': manifest['agents']['robin']['spec']['model_settings'],
            'warm_up': 'each runner worker warms its model clients once, at start (server.py, core.graph.model_clients '
            'warm=True): one single-token request per distinct client, not traced as a turn',
        },
        'speech': {
            'profile_file': 'docs/qualification/final-qualification/speech-profile.env',
            'profile': profile,
            'source_defaults': {
                'SLNG_BASE_URL': re.search(r"getenv\('SLNG_BASE_URL', '([^']+)'\)", bot)[1],
                'SLNG_STT_MODEL': re.search(r"getenv\('SLNG_STT_MODEL', '([^']+)'\)", bot)[1],
                'SLNG_TTS_MODEL': re.search(r"getenv\('SLNG_TTS_MODEL', '([^']+)'\)", bot)[1],
                'SLNG_TTS_VOICE': re.search(r"getenv\('SLNG_TTS_VOICE', '([^']+)'\)", bot)[1],
            },
            'tts_aggregation': 'TOKEN' if 'TextAggregationMode.TOKEN' in bot else 'unknown',
        },
        'turn_taking': {
            'vad': 'Silero',
            'vad_stop_seconds': float(re.search(r"getenv\('VAD_STOP_SECS'\) or ([0-9.]+)", bot)[1]),
            'turn_stop': 'LocalSmartTurnAnalyzerV3',
            'smart_turn_stop_seconds': float(re.search(r'SmartTurnParams\(stop_secs=([0-9.]+)\)', bot)[1]),
            'user_idle_seconds': float(re.search(r'IDLE_SECONDS = ([0-9.]+)', bot)[1]),
        },
        'overrides_present': {
            'process_env': sorted(n for n in OVERRIDES if n in os.environ),
            'dot_env_file': str(env),
            'dot_env': sorted(n for n in OVERRIDES if n in dotenv_values(env)),
            'dot_env_names': sorted(dotenv_values(env)),
        },
        'runner': {
            'transport': 'real Twilio Media Streams (Coval dials +447427919928), marks echoed',
            'delivery': 'marks: only a returned Twilio mark delivers a read-back',
            'server': 'use-cases/salon-graph/server.py under uvicorn, started by coval_sim run (a lone run: one campaign)',
            'workers': 2,
            'max_calls': int(
                re.search(
                    r"MAX_CALLS = int\(os.getenv\('MAX_CALLS'\) or (\d+)\)", (SALON / 'server.py').read_text()
                )[1]
            ),
            'concurrency': 1,
            'call_seconds': cases['call_seconds'],
            'drain_seconds': cases['call_seconds'] + 20,
            'host': f'{platform.machine()} {platform.mac_ver()[0]}',
        },
        'coval': {
            'agent': cases['agent'],
            'number': cases['number'],
            'sets': {name: c['coval_test_set'] for name, c in cases['cohorts'].items()},
            'personas': {name: c['persona'] for name, c in cases['cohorts'].items()},
            'cases_sha256': sha(HERE / 'cases.v1.json'),
            'iterations': cases['iterations'],
            'mutations': 'none',
        },
    }
    (HERE / 'config.json').write_text(json.dumps(config, indent=1) + '\n')
    print(json.dumps(config, indent=1))


if __name__ == '__main__':
    main()
