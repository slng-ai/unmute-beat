"""Run: uv run --env-file .env python -m tests.test_voice

Asserts, no pytest, no fixtures. Drives the processor directly with a scripted
model: no transport, no audio, no network.
"""

import asyncio
import json
from contextlib import ExitStack

from pydantic import TypeAdapter, ValidationError
from pydantic_ai import ModelResponse, TemplateStr, TextPart
from pydantic_ai.messages import ModelRequest, ToolCallPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)

from voice.agents import AGENTS
from voice.harness import TurnBudget
from voice.processor import EXHAUSTED, PydanticAgentProcessor
from voice.state import Call, CallState, Phone

# --- typed state -------------------------------------------------------------

phone = TypeAdapter(Phone)
assert phone.validate_python('+34600111222') == '+34600111222'
assert phone.validate_python('') == '', 'empty is "not yet", not "wrong"'
# no country code, leading zero, spaces, and one digit past E.164's 15.
for bad in ('600111222', '+0600111222', '+34 600 111 222', '+3460011122233445'):
    try:
        phone.validate_python(bad)
    except ValidationError:
        pass
    else:
        raise AssertionError(f'{bad!r} is not E.164 and must be refused')

# --- prompts -----------------------------------------------------------------

# Every prompt compiled at import of voice.agents; a placeholder naming no field
# must fail there rather than render as nothing mid-call.
try:
    TemplateStr('{{state.caller_phon}}', deps_type=Call)
except Exception as error:
    assert 'caller_phon' in str(error)
else:
    raise AssertionError('an unknown placeholder must be refused when the prompt compiles')

# --- a scripted model --------------------------------------------------------


def says(text, chunk=8):
    """Stream `text` in small pieces, the way a real model arrives."""

    async def stream(messages, info: AgentInfo):
        for start in range(0, len(text), chunk):
            yield text[start : start + chunk]

    return stream


def hands_to(name, because='test', instead='Sorry, what was that?'):
    """Call the Handoff output tool, which is how a handoff reaches the processor.

    With no output tool to call it speaks `instead`. That is not a convenience:
    the run the processor forces once the hop budget is spent narrows the output
    to `str`, so speech is the only thing a real model could produce there.
    """

    async def stream(messages, info: AgentInfo):
        if not info.output_tools:
            async for chunk in says(instead)(messages, info):
                yield chunk
            return
        yield {
            0: DeltaToolCall(
                name=info.output_tools[0].name,
                json_args=json.dumps({'to': name, 'because': because}),
            )
        }

    return stream


def calls_then_says(tool_name, args, text, chunk=8):
    """Call the tool on the first request, answer on the second."""

    async def stream(messages, info: AgentInfo):
        already_called = any(
            isinstance(part, ToolCallPart) and part.tool_name == tool_name
            for message in messages
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        if already_called:
            for start in range(0, len(text), chunk):
                yield text[start : start + chunk]
        else:
            yield {0: DeltaToolCall(name=tool_name, json_args=json.dumps(args))}

    return stream


class Probe(PydanticAgentProcessor):
    """The processor with its downstream replaced by a list."""

    def __init__(self, call, **kwargs):
        super().__init__(call, AGENTS, **kwargs)
        self.frames = []

    async def push_frame(self, frame, direction=None):
        self.frames.append(frame)

    def spoken(self):
        return ''.join(f.text for f in self.frames if isinstance(f, LLMTextFrame))

    def fixed_lines(self):
        return [f.text for f in self.frames if isinstance(f, TTSSpeakFrame)]


def scripted(**by_agent):
    """Override each named agent's model for the duration of a `with` block."""
    stack = ExitStack()
    for name, fn in by_agent.items():
        stack.enter_context(AGENTS[name].agent.override(model=FunctionModel(stream_function=fn)))
    return stack


# --- one plain turn ----------------------------------------------------------


async def plain_turn():
    call = Call()
    probe = Probe(call)
    with scripted(concierge=says('We are open until six.')):
        await probe._turn('when do you close?')

    kinds = [type(f).__name__ for f in probe.frames]
    assert kinds[0] == 'LLMFullResponseStartFrame', kinds
    assert kinds[-1] == 'LLMFullResponseEndFrame', kinds
    assert probe.spoken() == 'We are open until six.', probe.spoken()
    assert call.state.agent == 'concierge'
    # The caller's turn and the answer both landed in the agents' own history.
    assert isinstance(call.messages[0], ModelRequest)
    assert isinstance(call.messages[-1], ModelResponse)


# --- a handoff ---------------------------------------------------------------


async def handoff():
    call = Call()
    probe = Probe(call)
    with scripted(concierge=hands_to('intake'), intake=says('Is that plus three four, six zero zero, one one one, two two two?')):
        await probe._turn('my number is plus three four six zero zero one one one two two two')

    assert call.state.agent == 'intake', call.state.agent
    assert probe.spoken() == 'Is that plus three four, six zero zero, one one one, two two two?', probe.spoken()
    # The hop is covered by a line we speak, not one the model wrote.
    assert probe.fixed_lines(), 'the receiving agent has enter lines; one must be spoken'
    assert probe.fixed_lines()[0] in AGENTS['intake'].enter
    # Two runs, so two response envelopes.
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in probe.frames) == 2
    assert sum(isinstance(f, LLMFullResponseEndFrame) for f in probe.frames) == 2


# --- a handoff that never settles -------------------------------------------


async def one_line_per_gap():
    """A handoff line and a tool's filler cover the same silence. One of them speaks."""
    call = Call()
    probe = Probe(call)
    with scripted(
        concierge=hands_to('intake'),
        intake=calls_then_says('confirm_phone', {'phone': '+34600111222'}, "That's saved."),
    ):
        await probe._turn('my number is plus three four six zero zero one one one two two two')

    assert probe.fixed_lines() == [probe.fixed_lines()[0]], probe.fixed_lines()
    assert probe.fixed_lines()[0] in AGENTS['intake'].enter, probe.fixed_lines()
    assert call.state.caller_phone == '+34600111222'


async def ping_pong():
    call = Call()
    probe = Probe(call, budget=TurnBudget(step_runs=3))
    # Every run hands the caller on, so the budget is certain to run out. A live
    # call did exactly this and the caller heard nothing at all and rang off.
    with scripted(concierge=hands_to('intake'), intake=hands_to('concierge')):
        await probe._turn('hello?')
    # Bounded rather than hung, and never silent: exactly the runs the budget
    # paid for, and then a fixed line spoken by the processor.
    #
    # This used to expect a fourth run -- one more model request, made *after*
    # the budget was declared spent and outside it. That bounded nothing: the
    # extra run could itself hand off, call a tool, or take as long as it
    # liked, and the number the budget named was never the number of requests
    # a turn could make. The contract changed deliberately here.
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in probe.frames) == 3
    assert EXHAUSTED in probe.fixed_lines(), probe.fixed_lines()
    assert not probe.spoken(), 'the fallback is fixed speech, not another model run'


async def no_self_handoff():
    """An agent handing over to itself is refused, and it answers instead."""
    call = Call()
    probe = Probe(call)
    with scripted(
        concierge=lambda m, i: hands_to('concierge')(m, i)
        if len(m) < 3
        else says('We are open until six.')(m, i)
    ):
        await probe._turn('when do you close?')
    assert call.state.agent == 'concierge', call.state.agent
    assert probe.spoken() == 'We are open until six.', probe.spoken()
    # One run: the refusal came back to the model inside it, not as a hop.
    assert sum(isinstance(f, LLMFullResponseStartFrame) for f in probe.frames) == 1


# --- a tool, and the line that covers it ------------------------------------


async def tool_turn():
    call = Call(state=CallState(agent='intake'))
    probe = Probe(call)
    with scripted(intake=calls_then_says('confirm_phone', {'phone': '+34600111222'}, "That's saved.")):
        await probe._turn("yes, that's right")

    assert call.state.caller_phone == '+34600111222', call.state
    assert probe.spoken() == "That's saved.", probe.spoken()
    # The filler was spoken before the tool ran, so the caller hears something
    # during the round trip rather than silence.
    fillers = probe.fixed_lines()
    assert fillers, 'confirm_phone has filler lines; one must be spoken'
    said_at = probe.frames.index(next(f for f in probe.frames if isinstance(f, TTSSpeakFrame)))
    first_text = probe.frames.index(next(f for f in probe.frames if isinstance(f, LLMTextFrame)))
    assert said_at < first_text, 'the filler must be queued before the answer'


# --- what the caller actually heard -----------------------------------------


async def truncation():
    call = Call()
    probe = Probe(call)
    with scripted(concierge=says('Friday at nine, Saturday at ten, or Sunday at noon.')):
        await probe._turn('when are you free?')

    probe.note_spoken('Friday at nine, Sat', interrupted=True)
    text = [p.content for m in call.messages if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, TextPart)]
    assert text == ['Friday at nine, Sat'], text

    # Nothing heard at all: the response goes, rather than staying as an empty
    # message no provider will take back.
    before = len(call.messages)
    probe.note_spoken('', interrupted=True)
    assert len(call.messages) == before - 1

    # Not interrupted: history is left exactly as the run recorded it.
    probe.note_spoken('anything', interrupted=False)
    assert len(call.messages) == before - 1


# --- barge-in ----------------------------------------------------------------


async def barge_in():
    line = 'This is a long answer that the caller is about to talk straight over.'

    async def slowly(messages, info):
        for word in line.split():
            yield word + ' '
            await asyncio.sleep(0.02)

    call = Call()
    probe = Probe(call)
    with scripted(concierge=slowly):
        turn = asyncio.create_task(probe._turn('tell me everything'))
        await asyncio.sleep(0.08)
        probe.interrupt()
        await turn

    assert probe.spoken(), 'some of it was said before the caller cut in'
    assert probe.spoken() != line + ' ', 'the rest must never have been said'
    # The envelope still closes, or the assistant aggregator never ends the turn.
    assert isinstance(probe.frames[-1], LLMFullResponseEndFrame), type(probe.frames[-1])
    # What did complete is kept: the caller's turn is in the agents' history.
    assert any(isinstance(m, ModelRequest) for m in call.messages), call.messages


async def main():
    await plain_turn()
    await handoff()
    await one_line_per_gap()
    await ping_pong()
    await no_self_handoff()
    await tool_turn()
    await truncation()
    await barge_in()
    print('ok')


asyncio.run(main())
