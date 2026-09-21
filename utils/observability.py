"""Langfuse wiring. Call once, before the agent is built."""

import os
import sys

from dotenv import load_dotenv
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from pydantic_ai import Agent

load_dotenv()

# STT turns audio into text and TTS does the reverse, so the one string each span
# carries belongs on the opposite side of the observation.
_SIDE = {'stt': 'output', 'tts': 'input'}


def _langfuse_words(span) -> dict | None:
    """Pipecat's attributes, said again in the keys Langfuse reads.

    The two describe a span in different vocabularies: Pipecat writes the GenAI
    conventions plus its own ``transcript`` / ``text`` / ``metrics.*``, and
    Langfuse renders ``langfuse.observation.*`` and nothing else. Untranslated,
    every stt and tts observation arrives with an empty input, output and model,
    while the agents' spans -- which pydantic-ai already writes in Langfuse's
    words -- look fine. That is exactly how the gap shows up.
    """
    scope = span.instrumentation_scope
    if scope is None or not scope.name.startswith('pipecat'):
        return None
    attributes = span.attributes or {}
    # Everything Pipecat measured rather than a chosen few: ttfb, the voice, the
    # language, whether the transcript was final, the turn number.
    words = {f'langfuse.observation.metadata.{key}': value for key, value in attributes.items()}
    if side := _SIDE.get(attributes.get('gen_ai.operation.name')):
        words['langfuse.observation.type'] = 'generation'
        words['langfuse.observation.model.name'] = attributes.get('gen_ai.request.model', '')
        words[f'langfuse.observation.{side}'] = attributes.get('transcript') or attributes.get('text') or ''
    return words


class _Speech(SpanProcessor):
    """Translate Pipecat's spans on their way out.

    Registered before Langfuse's own processor, so a span is already rewritten by
    the time Langfuse queues it for export.
    """

    def on_end(self, span) -> None:
        # ponytail: replaces the attribute map rather than writing into it --
        # OpenTelemetry seals a span's attributes when it ends, and a processor
        # only ever sees ended spans. The upgrade path is Pipecat emitting these
        # keys itself, at which point this class goes.
        if words := _langfuse_words(span):
            span._attributes = dict(span.attributes or {}) | words


def setup() -> None:
    # Pydantic AI emits OTel spans; Langfuse collects them. Credentials come from
    # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL in the environment.
    Agent.instrument_all()
    # Langfuse drops spans silently when unconfigured, so say so rather than pretending.
    # ponytail: an env check, not auth_check() -- that costs a network round trip per run.
    if not os.getenv('LANGFUSE_PUBLIC_KEY'):
        print('warning: LANGFUSE_PUBLIC_KEY unset, traces are going nowhere', file=sys.stderr)
        return

    attributes = {'service.name': 'slng-voice-harness'}
    if environment := os.getenv('LANGFUSE_TRACING_ENVIRONMENT'):
        # Langfuse reads this off the resource. It builds one itself when it owns
        # the provider, and here it does not.
        attributes['langfuse.environment'] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(_Speech())
    trace.set_tracer_provider(provider)

    # Langfuse attaches its exporter to the provider registered above, so Pipecat's
    # `trace.get_tracer("pipecat")` and pydantic-ai both feed the one trace. Its
    # default filter keeps only gen_ai spans, which would throw away the
    # conversation span everything else hangs from.
    # ponytail: export everything rather than an allow-list -- this process only
    # makes spans we asked for. Narrow it if a chatty library gets instrumented.
    Langfuse(should_export_span=lambda _span: True)
    # ponytail: no atexit flush here -- Langfuse already registers atexit shutdown,
    # which flushes. Adding our own just ran the whole flush cycle twice.
