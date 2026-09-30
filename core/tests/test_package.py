"""Importing core and checking a flow touches nothing outside it: no `.env`, no Pipecat, no Langfuse, no client.

Each check runs in a fresh interpreter with the model and tracing keys
removed, so what it sees is what an application importing `core` gets.
"""

import os
import subprocess
import sys

KEYS = ('GOOGLE_API_KEY', 'OPENAI_API_KEY', 'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')


def fresh(program: str) -> str:
    """Run `program` in a new interpreter with no keys; its last printed line."""
    env = {name: value for name, value in os.environ.items() if name not in KEYS}
    run = subprocess.run([sys.executable, '-c', program], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr[-2000:]
    return run.stdout.strip().splitlines()[-1]


def importing_and_checking_loads_nothing_else():
    loaded = fresh(
        'import dotenv, sys\n'
        'reads = []\n'
        'dotenv.load_dotenv = lambda *a, **k: reads.append(1)\n'
        'import core\n'
        "assert core.load('tests/clinic_flow/agent.yaml').problems() == []\n"
        'from opentelemetry import trace\n'
        "outside = ('pipecat', 'langfuse', 'google.genai', 'openai', 'opentelemetry.sdk')\n"
        'found = sorted(m for m in sys.modules if m.split(".")[0] in outside or m.startswith(outside))\n'
        'print(reads, found, type(trace.get_tracer_provider()).__name__)\n'
    )
    assert loaded == '[] [] ProxyTracerProvider', loaded


def a_missing_extra_says_which_to_install():
    for module, extra, blocked in (
        ('core.processor', 'voice', 'pipecat'),
        ('core.observability', 'tracing', 'langfuse'),
    ):
        said = fresh(
            'import sys\n'
            f'sys.modules[{blocked!r}] = None\n'
            'try:\n'
            f'    import {module}\n'
            'except ImportError as error:\n'
            '    print(error)\n'
        )
        assert f'slng-voice-core[{extra}]' in said, said


def main() -> None:
    importing_and_checking_loads_nothing_else()
    a_missing_extra_says_which_to_install()
    print('package ok: importing and checking loads no env, no voice, no tracing, no provider')


if __name__ == '__main__':
    main()
