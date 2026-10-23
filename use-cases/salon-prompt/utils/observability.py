"""Langfuse over OpenTelemetry. Call `setup()` once, at the start of each call.

A copy of `core.observability` without the Pydantic AI hook: this build has no
Pydantic AI, so the parts about agent runs find nothing to read. Pipecat emits
OTel spans and Langfuse collects them. Four things have to be
true for a call to read as one call: Langfuse must export the non-GenAI spans
everything hangs from, Pipecat's stt and tts spans must be said again in the
attribute keys Langfuse renders, the session and trace name must be on every
span rather than the root alone, and what each agent run and model request
produced must be readable at a glance rather than a dump of parts. Credentials
come from `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL`.
"""

import json
import os
import sys
from contextvars import ContextVar

from dotenv import load_dotenv
from langfuse import Langfuse
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

load_dotenv()

_SPEECH = {'stt': ('output', 'user'), 'tts': ('input', 'assistant')}
"""STT turns audio into text and TTS the reverse, so the one string each span carries belongs on the opposite side, and to the opposite speaker in the transcript."""

_CALL: ContextVar[dict[str, str] | None] = ContextVar('langfuse_call', default=None)
"""The call the code running now belongs to, in Langfuse's own keys minus the prefix.

Langfuse v4 queries observations, not traces, so anything needed to filter or
aggregate has to be on every span rather than only the root. Left on the root
alone the cost-bearing generations fell outside the call's session: Pydantic AI
writes a `gen_ai.conversation.id` of its own, Langfuse read that as the session,
and the whole agency's spend landed under an id nothing else used. `langfuse.*`
wins over the generic conventions, which is what pulls them back.

A context variable, not a module dict, because one runner serves many calls at
once: `setup` runs at the top of each call's own task, every task the call
starts inherits it, and `_Speech` binds it to the span's trace when the span
starts. A process-wide dict gave every span the session of whichever call
started last.
"""
_PROVIDER: list[TracerProvider] = []
"""The one tracer provider, built by the first `setup`. OpenTelemetry refuses a second registration."""


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


def _readable(final_result: str) -> str:
    """What an agent run produced, as one sentence.

    Pydantic AI records a run's `final_result` as the output's JSON, which for
    this agency is a `Handoff` or an `Ask` far more often than a sentence. A
    question is best read as the words the caller heard; a handoff as where the
    caller went. Anything else is left exactly as Pydantic AI wrote it.
    """
    try:
        result = json.loads(final_result)
    except ValueError:
        return final_result
    if not isinstance(result, dict):
        return final_result
    if spoken := result.get('spoken'):
        return str(spoken)
    if to := result.get('to'):
        if result.get('status', 'handoff') != 'handoff':
            return f'finished; back to {to}'
        because = result.get('because') or ''
        return f'handed over to {to}: {because}' if because else f'handed over to {to}'
    return final_result


def _line(role: str, parts: list) -> dict:
    """One message in the shape Langfuse renders as chat: text and tool returns as `content`, calls as OpenAI-style `tool_calls`.

    Langfuse marks a tool "called" only when it finds it in a `tool_calls`
    array; a call written into the text reads fine but leaves every tool
    listed as not called.
    """
    content: list[str] = []
    calls: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        kind = part.get('type')
        if kind == 'text' and part.get('content'):
            content.append(str(part['content']))
        elif kind == 'tool_call':
            arguments = part.get('arguments', part.get('args', {}))
            calls.append(
                {
                    'id': part.get('id', ''),
                    'type': 'function',
                    'function': {
                        'name': part.get('name', 'tool'),
                        'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments),
                    },
                }
            )
        elif kind == 'tool_call_response':
            result = part.get('result', part.get('content', ''))
            rendered = result if isinstance(result, str) else json.dumps(result)
            content.append(f'{part.get("name", "tool")} -> {rendered}')
    line: dict = {'role': role, 'content': ' '.join(content)}
    if calls:
        line['tool_calls'] = calls
    return line


def _messages(messages_json: str) -> list[dict]:
    """Pydantic AI's OTel message array, parsed, or nothing."""
    try:
        messages = json.loads(messages_json)
    except ValueError:
        return []
    return (
        [message for message in messages if isinstance(message, dict)] if isinstance(messages, list) else []
    )


def _said(messages_json: str) -> str:
    """What one model request answered: its text alone when that is all there was, otherwise the message with its tool calls."""
    lines = [
        _line(message.get('role', 'assistant'), message.get('parts', []))
        for message in _messages(messages_json)
    ]
    lines = [line for line in lines if line['content'] or 'tool_calls' in line]
    if not lines:
        return ''
    if len(lines) == 1 and 'tool_calls' not in lines[0]:
        return lines[0]['content']
    return json.dumps(lines[0] if len(lines) == 1 else lines)


def _prompt(system_json: str, messages_json: str) -> str:
    """What a model was given, as the chat transcript Langfuse renders: the system prompt first, then every message."""
    lines = [_line('system', _messages(system_json))]
    lines += [
        _line(message.get('role', ''), message.get('parts', [])) for message in _messages(messages_json)
    ]
    return json.dumps([line for line in lines if line['content'] or 'tool_calls' in line])


def _agent_words(span) -> dict:
    """Pydantic AI's run and request spans, with an input and an output somebody can read.

    Pydantic AI writes the prompt as `gen_ai.system_instructions` plus
    `gen_ai.input.messages` on a request and `pydantic_ai.all_messages` on a
    run, and Langfuse renders none of it: the request's input arrives as its
    tool definitions alone and the run's as null. `langfuse.observation.input`
    always wins, so both are said again as a transcript with the system prompt
    first. The run's input stops at the message that started it, since what
    followed is under its own request spans. The run span keeps its type
    explicit and gets `_readable` of its result; the request span gets `_said`
    of its output messages. The originals stay in the observation's metadata,
    so nothing is lost by saying it plainly.

    ponytail: the run's prompt is the history it was given, `all_messages` up
    to `new_message_index`, plus the user message at that index when this run
    had one of its own; one OTel message per Pydantic AI message. If a format
    version ever splits a message by role, cut at the first assistant message
    after the index instead.
    """
    attributes = span.attributes or {}
    operation = attributes.get('gen_ai.operation.name')
    system = str(attributes.get('gen_ai.system_instructions') or '[]')
    if operation == 'invoke_agent':
        words = {'langfuse.observation.type': 'agent'}
        if (result := attributes.get('final_result')) is not None:
            words['langfuse.observation.output'] = _readable(str(result))
        if history := _messages(str(attributes.get('pydantic_ai.all_messages') or '')):
            index = int(attributes.get('pydantic_ai.new_message_index', 0))
            asked = (
                history[index : index + 1] if history[index:] and history[index].get('role') == 'user' else []
            )
            words['langfuse.observation.input'] = _prompt(system, json.dumps(history[:index] + asked))
        return words
    if operation == 'chat':
        words = {}
        if said := _said(str(attributes.get('gen_ai.output.messages') or '')):
            words['langfuse.observation.output'] = said
        if given := attributes.get('gen_ai.input.messages'):
            words['langfuse.observation.input'] = _prompt(system, str(given))
        return words
    return {}


def _spoken(lines: list[dict[str, str]], role: str) -> str:
    """Everything one side of the conversation said, in order."""
    return ' '.join(line['content'] for line in lines if line['role'] == role)


class _Speech(SpanProcessor):
    """Translate Pipecat's and Pydantic AI's spans on their way out, and stamp the call on every span.

    Registered before Langfuse's own processor, so a span is already rewritten
    by the time Langfuse queues it for export.

    ponytail: replaces the attribute map rather than writing into it, because
    OpenTelemetry seals a span's attributes when it ends and a processor only
    sees ended spans. The upgrade path is Pipecat emitting these keys itself,
    at which point this class goes.
    """

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}
        """Each open trace's call, bound when its first span started in that call's context."""
        self._lines: dict[int, list[dict[str, str]]] = {}
        """Each open trace's conversation so far, from the turns that have closed."""
        self._turns: dict[int, list[dict[str, str]]] = {}
        """Each open trace's current turn, from its stt and tts spans."""

    def on_start(self, span, parent_context=None) -> None:
        if (call := _CALL.get()) is not None:
            self._calls.setdefault(span.context.trace_id, call)

    def on_end(self, span) -> None:
        trace_id = span.context.trace_id
        call = self._calls.get(trace_id) or _CALL.get() or {}
        words = {f'langfuse.{key}': value for key, value in call.items()}
        scope = span.instrumentation_scope
        if scope is not None and scope.name.startswith('pipecat'):
            words |= _langfuse_words(span) | self._transcript(span, trace_id)
        elif scope is not None and scope.name.startswith('pydantic-ai'):
            words |= _agent_words(span)
        if words:
            span._attributes = dict(span.attributes or {}) | words
        if span.name == 'conversation' or span.parent is None:
            self._calls.pop(trace_id, None)
            self._lines.pop(trace_id, None)
            self._turns.pop(trace_id, None)

    def _transcript(self, span, trace_id: int) -> dict:
        """The conversation, read back off the spans already carrying it.

        Pipecat's `conversation` and `turn` spans have no input or output of
        their own: the LLM service that would fill them is the one core
        replaced, so Langfuse rendered the root of every call empty, and v4 asks
        for the whole request and response exactly there.

        The words are already going past. `stt` carries what the caller said and
        `tts` what the bot said; both end before the turn they belong to, and
        every turn before the conversation, because `TurnTraceObserver` closes
        the open turn first. So collect them in order and spend them on the way
        up. Whatever a last, unclosed turn left behind goes on the conversation
        too: a call that ends mid-turn still has to read as the call it was.

        Collected per trace, because calls running side by side share this
        processor and their spans end interleaved.
        """
        attributes = span.attributes or {}
        if speech := _SPEECH.get(attributes.get('gen_ai.operation.name')):
            _side, role = speech
            if said := attributes.get('transcript') or attributes.get('text'):
                self._turns.setdefault(trace_id, []).append({'role': role, 'content': said})
            return {}
        if span.name == 'turn':
            lines = self._turns.pop(trace_id, [])
            self._lines.setdefault(trace_id, []).extend(lines)
            words = {}
            if caller := _spoken(lines, 'user'):
                words['langfuse.observation.input'] = caller
            if bot := _spoken(lines, 'assistant'):
                words['langfuse.observation.output'] = bot
            return words
        if span.name == 'conversation':
            lines = self._lines.get(trace_id, []) + self._turns.get(trace_id, [])
            if not lines:
                return {}
            words = {'langfuse.observation.input': json.dumps(lines)}
            if bot := [line['content'] for line in lines if line['role'] == 'assistant']:
                words['langfuse.observation.output'] = bot[-1]
            return words
        return {}


def setup(session: str = '', trace_name: str = '') -> None:
    """Name the call this task is running, and on first use point the process's tracer at Langfuse.

    `session` groups the call's observations and `trace_name` is what its trace
    is listed by; both are stamped on every span rather than the root alone, for
    the reason `_CALL` gives. Call it at the top of each call's own task, before
    anything that call traces starts: the provider is built once, the call's
    names are set every time.

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
    call = {}
    if session:
        call['session.id'] = session
    if trace_name:
        call['trace.name'] = trace_name
    _CALL.set(call)
    if _PROVIDER:
        return
    if not os.getenv('LANGFUSE_PUBLIC_KEY'):
        print('warning: LANGFUSE_PUBLIC_KEY unset, traces are going nowhere', file=sys.stderr)
        return

    attributes = {'service.name': 'slng-voice-core'}
    if environment := os.getenv('LANGFUSE_TRACING_ENVIRONMENT'):
        attributes['deployment.environment'] = environment
    provider = TracerProvider(resource=Resource.create(attributes))
    provider.add_span_processor(_Speech())
    trace.set_tracer_provider(provider)
    _PROVIDER.append(provider)
    Langfuse(should_export_span=lambda _span: True)
