"""Langfuse over OpenTelemetry. Call `setup()` once, before any agent is built.

Pydantic AI emits OTel spans and Langfuse collects them. Three things have to be
true for a call to arrive as one trace: Langfuse must export the non-GenAI
spans everything hangs from, Pipecat's stt and tts spans must be said again in
the attribute keys Langfuse renders, and the session and trace name must be on
every span rather than the root alone. Credentials come from
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL`.
"""

import json
import os
import sys

from dotenv import load_dotenv
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from pydantic_ai import Agent

load_dotenv()

_SPEECH = {'stt': ('output', 'user'), 'tts': ('input', 'assistant')}
"""STT turns audio into text and TTS the reverse, so the one string each span carries belongs on the opposite side, and to the opposite speaker in the transcript."""

_CALL: dict[str, str] = {}
"""The call every span in this process belongs to, in Langfuse's own keys minus the prefix.

Langfuse v4 queries observations, not traces, so anything needed to filter or
aggregate has to be on every span rather than only the root. Left on the root
alone the cost-bearing generations fell outside the call's session: Pydantic AI
writes a `gen_ai.conversation.id` of its own, Langfuse read that as the session,
and the whole agency's spend landed under an id nothing else used. `langfuse.*`
wins over the generic conventions, which is what pulls them back.

ponytail: one call per process. `trace.set_tracer_provider` already refuses a
second registration, so the assumption is older than this dict; key it by
`span.context.trace_id` if a runner ever serves two calls at once.
"""


def _langfuse_words(span) -> dict:
    """Pipecat's attributes, said again in the keys Langfuse reads.

    The two describe a span in different vocabularies: Pipecat writes the GenAI
    conventions plus its own `transcript`, `text` and `metrics.*`, and Langfuse
    renders `langfuse.observation.*` and nothing else. Untranslated, every stt
    and tts observation arrives with an empty input, output and model, while
    the agents' spans, which Pydantic AI already writes in Langfuse's words,
    look fine. Everything Pipecat measured goes into the metadata rather than
    a chosen few.
    """
    attributes = span.attributes or {}
    words = {f'langfuse.observation.metadata.{key}': value for key, value in attributes.items()}
    if speech := _SPEECH.get(attributes.get('gen_ai.operation.name')):
        side, _role = speech
        words['langfuse.observation.type'] = 'generation'
        words['langfuse.observation.model.name'] = attributes.get('gen_ai.request.model', '')
        words[f'langfuse.observation.{side}'] = attributes.get('transcript') or attributes.get('text') or ''
    return words


def _spoken(lines: list[dict[str, str]], role: str) -> str:
    """Everything one side of the conversation said, in order."""
    return ' '.join(line['content'] for line in lines if line['role'] == role)


class _Speech(SpanProcessor):
    """Translate Pipecat's spans on their way out, and stamp the call on every span.

    Registered before Langfuse's own processor, so a span is already rewritten
    by the time Langfuse queues it for export.

    ponytail: replaces the attribute map rather than writing into it, because
    OpenTelemetry seals a span's attributes when it ends and a processor only
    sees ended spans. The upgrade path is Pipecat emitting these keys itself,
    at which point this class goes.
    """

    def __init__(self) -> None:
        self._call: list[dict[str, str]] = []
        self._turn: list[dict[str, str]] = []

    def on_end(self, span) -> None:
        words = {f'langfuse.{key}': value for key, value in _CALL.items()}
        scope = span.instrumentation_scope
        if scope is not None and scope.name.startswith('pipecat'):
            words |= _langfuse_words(span) | self._transcript(span)
        if words:
            span._attributes = dict(span.attributes or {}) | words

    def _transcript(self, span) -> dict:
        """The conversation, read back off the spans already carrying it.

        Pipecat's `conversation` and `turn` spans have no input or output of
        their own: the LLM service that would fill them is the one this harness
        replaced, so Langfuse rendered the root of every call empty, and v4 asks
        for the whole request and response exactly there.

        The words are already going past. `stt` carries what the caller said and
        `tts` what the bot said; both end before the turn they belong to, and
        every turn before the conversation, because `TurnTraceObserver` closes
        the open turn first. So collect them in order and spend them on the way
        up. Whatever a last, unclosed turn left behind goes on the conversation
        too: a call that ends mid-turn still has to read as the call it was.
        """
        attributes = span.attributes or {}
        if speech := _SPEECH.get(attributes.get('gen_ai.operation.name')):
            _side, role = speech
            if said := attributes.get('transcript') or attributes.get('text'):
                self._turn.append({'role': role, 'content': said})
            return {}
        if span.name == 'turn':
            lines, self._turn = self._turn, []
            self._call += lines
            words = {}
            if caller := _spoken(lines, 'user'):
                words['langfuse.observation.input'] = caller
            if bot := _spoken(lines, 'assistant'):
                words['langfuse.observation.output'] = bot
            return words
        if span.name == 'conversation':
            lines = self._call + self._turn
            if not lines:
                return {}
            words = {'langfuse.observation.input': json.dumps(lines)}
            if bot := [line['content'] for line in lines if line['role'] == 'assistant']:
                words['langfuse.observation.output'] = bot[-1]
            return words
        return {}


def setup(session: str = '', trace_name: str = '') -> None:
    """Instrument every agent and point the process's tracer at Langfuse.

    `session` groups the call's observations and `trace_name` is what its trace
    is listed by; both are stamped on every span rather than the root alone, for
    the reason `_CALL` gives.

    Langfuse drops spans silently when unconfigured, so the absence of a key
    is said once on stderr rather than pretended away. Its default filter keeps
    only GenAI spans, which would throw away the conversation span everything
    else hangs from, so everything is exported. The environment goes on the
    resource under OpenTelemetry's own key, which is what Langfuse reads off a
    resource it did not build.

    ponytail: an environment check, not `auth_check()`, which costs a network
    round trip per run. Export everything rather than an allow-list, because
    this process only makes spans we asked for; narrow it if a chatty library
    gets instrumented. No atexit flush of our own: Langfuse registers one.
    """
    Agent.instrument_all()
    if session:
        _CALL['session.id'] = session
    if trace_name:
        _CALL['trace.name'] = trace_name
    if not os.getenv('LANGFUSE_PUBLIC_KEY'):
        print('warning: LANGFUSE_PUBLIC_KEY unset, traces are going nowhere', file=sys.stderr)
        return

    attributes = {'service.name': 'slng-voice-harness'}
    if environment := os.getenv('LANGFUSE_TRACING_ENVIRONMENT'):
        attributes['deployment.environment'] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(_Speech())
    trace.set_tracer_provider(provider)
    Langfuse(should_export_span=lambda _span: True)
