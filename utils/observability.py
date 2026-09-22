"""Langfuse over OpenTelemetry. Call `setup()` once, before any agent is built.

Pydantic AI emits OTel spans and Langfuse collects them. Two things have to be
true for a call to arrive as one trace: Langfuse must export the non-GenAI
spans everything hangs from, and Pipecat's stt and tts spans must be said again
in the attribute keys Langfuse renders. Credentials come from
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL`.
"""

import os
import sys

from dotenv import load_dotenv
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from pydantic_ai import Agent

load_dotenv()

_SIDE = {'stt': 'output', 'tts': 'input'}
"""STT turns audio into text and TTS the reverse, so the one string each span carries belongs on the opposite side."""


def _langfuse_words(span) -> dict | None:
    """Pipecat's attributes, said again in the keys Langfuse reads.

    The two describe a span in different vocabularies: Pipecat writes the GenAI
    conventions plus its own `transcript`, `text` and `metrics.*`, and Langfuse
    renders `langfuse.observation.*` and nothing else. Untranslated, every stt
    and tts observation arrives with an empty input, output and model, while
    the agents' spans, which Pydantic AI already writes in Langfuse's words,
    look fine. Everything Pipecat measured goes into the metadata rather than
    a chosen few.
    """
    scope = span.instrumentation_scope
    if scope is None or not scope.name.startswith('pipecat'):
        return None
    attributes = span.attributes or {}
    words = {f'langfuse.observation.metadata.{key}': value for key, value in attributes.items()}
    if side := _SIDE.get(attributes.get('gen_ai.operation.name')):
        words['langfuse.observation.type'] = 'generation'
        words['langfuse.observation.model.name'] = attributes.get('gen_ai.request.model', '')
        words[f'langfuse.observation.{side}'] = attributes.get('transcript') or attributes.get('text') or ''
    return words


class _Speech(SpanProcessor):
    """Translate Pipecat's spans on their way out.

    Registered before Langfuse's own processor, so a span is already rewritten
    by the time Langfuse queues it for export.

    ponytail: replaces the attribute map rather than writing into it, because
    OpenTelemetry seals a span's attributes when it ends and a processor only
    sees ended spans. The upgrade path is Pipecat emitting these keys itself,
    at which point this class goes.
    """

    def on_end(self, span) -> None:
        if words := _langfuse_words(span):
            span._attributes = dict(span.attributes or {}) | words


def setup() -> None:
    """Instrument every agent and point the process's tracer at Langfuse.

    Langfuse drops spans silently when unconfigured, so the absence of a key
    is said once on stderr rather than pretended away. Its default filter keeps
    only GenAI spans, which would throw away the conversation span everything
    else hangs from, so everything is exported.

    ponytail: an environment check, not `auth_check()`, which costs a network
    round trip per run. Export everything rather than an allow-list, because
    this process only makes spans we asked for; narrow it if a chatty library
    gets instrumented. No atexit flush of our own: Langfuse registers one.
    """
    Agent.instrument_all()
    if not os.getenv('LANGFUSE_PUBLIC_KEY'):
        print('warning: LANGFUSE_PUBLIC_KEY unset, traces are going nowhere', file=sys.stderr)
        return

    attributes = {'service.name': 'slng-voice-harness'}
    if environment := os.getenv('LANGFUSE_TRACING_ENVIRONMENT'):
        attributes['langfuse.environment'] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(_Speech())
    trace.set_tracer_provider(provider)
    Langfuse(should_export_span=lambda _span: True)
