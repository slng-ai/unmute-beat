"""Importing core and checking a flow touches nothing outside it: no `.env`, no Pipecat, no Langfuse, no client.

Each check runs in a fresh interpreter with the model and tracing keys
removed, so what it sees is what an application importing `core` gets.
"""

import os
import subprocess
import sys

KEYS = (
    'GOOGLE_API_KEY',
    'OPENAI_API_KEY',
    'LANGFUSE_PUBLIC_KEY',
    'LANGFUSE_SECRET_KEY',
    'LANGFUSE_BASE_URL',
    'OTEL_EXPORTER_OTLP_ENDPOINT',
)


def fresh(program: str, **extra: str) -> str:
    """Run `program` in a new interpreter with no keys, and `extra` set; its last printed line."""
    env = {name: value for name, value in os.environ.items() if name not in KEYS} | extra
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


def a_local_collector_gets_the_spans_without_langfuse():
    """`OTEL_EXPORTER_OTLP_ENDPOINT` alone builds the provider and batches every span to that collector."""
    program = (
        'from core.observability import setup\n'
        'from opentelemetry import trace\n'
        'setup(session="s", trace_name="t")\n'
        'provider = trace.get_tracer_provider()\n'
        'processors = getattr(getattr(provider, "_active_span_processor", None), "_span_processors", ())\n'
        'exporters = [type(getattr(p, "span_exporter", None)).__name__ for p in processors]\n'
        'print(type(provider).__name__, exporters)\n'
    )
    collected = fresh(program, OTEL_EXPORTER_OTLP_ENDPOINT='http://127.0.0.1:9')
    assert collected.startswith('TracerProvider') and 'OTLPSpanExporter' in collected, collected
    nowhere = fresh(program)
    assert nowhere.startswith('ProxyTracerProvider'), 'no key and no collector: nothing is built'


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
    a_local_collector_gets_the_spans_without_langfuse()
    print(
        'package ok: importing and checking loads no env, no voice, no tracing, no provider; a local collector gets spans'
    )


if __name__ == '__main__':
    main()
