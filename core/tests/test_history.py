"""C3 request snapshots, canonical speech, resets and prerequisite returns. Offline."""

import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace

from pipecat.frames.frames import LLMFullResponseStartFrame
from pydantic_ai import Agent, RunContext, TemplateStr, Tool, ToolOutput
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.graph import Node
from core.history import paired, project
from core.processor import PydanticAgentProcessor
from core.state import CallContext, Handoff, VoiceState
from core.turn import Barge, note_spoken, take_turn


class State(VoiceState):
    agent: str = 'owner'
    saved: str = 'before'
    private_phone: str = '+34600111222'
    verified: bool | None = None


class Call(CallContext):
    state: State


class Adapter(PydanticAgentProcessor):
    async def push_frame(self, frame, direction=None):
        """Like the assistant aggregator, start before the first speech segment."""
        if isinstance(frame, LLMFullResponseStartFrame):
            assert not self._active_playback
            self.playback_started('old-audio', run=frame.metadata['voice_run'])


def text(messages):
    return '\n'.join(
        p.content for m in messages for p in m.parts if isinstance(p, (TextPart, UserPromptPart))
    )


async def snapshots(adapter=False):
    """Every request re-renders named values; fixed speech and tool pairs appear once."""
    seen = []

    async def save(ctx: RunContext[Call]) -> str:
        ctx.deps.commit(saved='after')
        await ctx.deps.speak_now('Saved receipt.', origin='confirmation')
        return 'receipt-id-1'

    async def stream(messages, info):
        seen.append((deepcopy(messages), deepcopy(info)))
        if len(seen) == 1:
            yield 'Before saving. '
            yield {1: DeltaToolCall(name='save', json_args='{}', tool_call_id='save-1')}
        else:
            yield 'Anything else?'

    agent = Agent(
        FunctionModel(stream_function=stream),
        deps_type=Call,
        tools=[save],
        instructions=TemplateStr('Saved={{state.saved}}', deps_type=Call),
        model_settings={'temperature': 0.0},
    )
    call = Call(state=State())
    nodes = {'owner': Node(agent)}
    probe = Adapter(call, nodes)
    if adapter:
        await probe._turn('Please save it.')
    else:
        await take_turn(call, nodes, 'Please save it.')
    assert len(seen) == 2
    for index, (messages, info) in enumerate(seen):
        assert ('Saved=before' if index == 0 else 'Saved=after') in info.instructions
        assert call.state.private_phone not in info.instructions
        assert [t.name for t in info.function_tools] == ['save']
        assert info.function_tools[0].parameters_json_schema['type'] == 'object'
        assert info.allow_text_output and not info.output_tools
        assert info.model_settings['temperature'] == 0.0
        assert sum(isinstance(p, UserPromptPart) for m in messages for p in m.parts) == 1
    view = text(seen[1][0])
    assert view.count('Saved receipt.') == view.count('Before saving.') == 1, view
    assert view.index('Before saving.') < view.index('Saved receipt.'), view
    assert [m for m in seen[1][0] if m.parts] == paired(seen[1][0])
    assert isinstance(seen[1][0][-1], ModelRequest) and seen[1][0][-1].parts, (
        'Google rejects an empty trailing request'
    )
    protocol = deepcopy(call._protocol)
    ids = tuple(s.id for s in call.spoken)
    if not adapter:
        probe._playback_runs[(call.turn, call._attempt)] = list(ids)
        probe.playback_started('old-audio', run=(call.turn, call._attempt))
    assert probe._playback_turns['old-audio'] == list(ids)
    await take_turn(call, nodes, 'Next question.')
    newest = call.spoken[0].text
    probe.note_spoken('Before saving. Saved', True, timestamp='old-audio')
    assert call.spoken[0].text == newest
    view = text(project(call, call.messages))
    assert 'Saved receipt.' not in view and 'receipt-id-1' in str(call.messages)
    assert 'Saved receipt.' in str(protocol) or any(s.generated == 'Saved receipt.' for s in call.all_spoken)
    assert call._protocol[: len(protocol)] == protocol
    assert call.all_spoken[1].text == 'Saved'
    assert call.all_spoken[1].delivery == 'runtime-estimate'
    note_spoken(call, 'Before saving. Saved receipt. Anything else?', True, segment_ids=ids)
    assert call.all_spoken[1].text == 'Saved', 'a duplicate callback expanded heard speech'
    once = project(call, call.messages)
    assert project(call, once) == once


async def resets():
    """Reset is a request policy, never deletion of the conversation or active tool loop."""
    seen = []

    async def touch() -> str:
        return 'loop-result'

    async def stream(messages, info):
        seen.append(deepcopy(messages))
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield {0: DeltaToolCall(name='touch', json_args='{}', tool_call_id=f'touch-{len(seen)}')}
        else:
            yield f'Answer {len(seen)}.'

    agent = Agent[Call, str](
        FunctionModel(stream_function=stream), deps_type=Call, tools=[Tool(touch, takes_ctx=False)]
    )
    call = Call(state=State())
    nodes = {'owner': Node(agent, history='reset')}
    await take_turn(call, nodes, 'First reset input.')
    await take_turn(call, nodes, 'Second reset input.')
    assert 'First reset input.' not in text(seen[2])
    assert 'Answer 2.' not in text(seen[2])
    assert 'loop-result' in str(seen[3]) and [m for m in seen[3] if m.parts] == paired(seen[3])
    await take_turn(call, {'owner': replace(nodes['owner'], history='messages')}, 'Normal input.')
    assert text(seen[4]).count('First reset input.') == 1
    assert text(seen[4]).count('Second reset input.') == 1
    assert 'Answer 2.' in text(seen[4]) and 'Answer 4.' in text(seen[4])
    assert len(call.transcript()) == 6


async def returns(unserved=False):
    """A prerequisite hands back into one shared conversation, with an application status.

    Session 4: the step that waited is no longer given back its history from
    before the detour. It sees what everyone said since, the prerequisite's
    question and the caller's answer included, so a correction made during
    the detour is not lost (T3.v2 lost the requested time this way). The
    original request is in it once, and the status says how the detour ended.
    A given-up detour's last words go on as the caller's prompt.
    """
    seen = []

    async def delegate() -> Handoff:
        return Handoff(to='owner')

    async def finish(ctx: RunContext[Call]) -> Handoff:
        if not unserved:
            ctx.deps.commit(verified=True)
        return Handoff(to='owner', status='unserved' if unserved else 'completed')

    async def owner(messages, info):
        seen.append((deepcopy(messages), info.instructions))
        if len(seen) == 1:
            yield {0: DeltaToolCall(name='delegate', json_args='{}')}
        else:
            yield 'Owner answer.'

    async def child(messages, info):
        if 'child-secret' not in text(messages) and 'Forget' not in text(messages):
            yield 'Confirm child-secret?'
        else:
            yield {0: DeltaToolCall(name='finish', json_args='{}')}

    nodes = {
        'owner': Node(
            Agent[Call, object](
                FunctionModel(stream_function=owner),
                deps_type=Call,
                output_type=[str, ToolOutput(delegate, name='delegate')],
            ),
            requires=('verified',),
            satisfied_by='child',
        ),
        'child': Node(
            Agent[Call, object](
                FunctionModel(stream_function=child),
                deps_type=Call,
                output_type=[str, ToolOutput(finish, name='finish')],
            ),
        ),
    }
    call = Call(state=State())
    await take_turn(call, nodes, 'Original errand.')
    assert call._checkpoint is not None and call.state.agent == 'child', (call.state, call.messages)
    await take_turn(call, nodes, 'Forget the errand.' if unserved else 'Yes child-secret, and make it ten.')
    messages, instructions = seen[1]
    assert text(messages).count('Original errand.') == 1
    assert 'Confirm child-secret?' in text(messages), 'the detour is part of the one conversation'
    if not unserved:
        assert 'make it ten' in text(messages), 'a correction made during the detour reached the owner'
    assert ('unserved' if unserved else 'completed') in instructions
    assert ('Forget the errand.' in text(messages)) == unserved
    assert call._checkpoint is None and call.state.pending is None
    await take_turn(call, nodes, 'Unrelated question.')
    assert not call._return_status and call._checkpoint is None


async def cancellation_keeps_receipts():
    """A native task cancellation retains the completed write's protocol pair."""
    waiting = asyncio.Event()
    seen = []
    writes = []

    async def save() -> str:
        writes.append('durable-receipt')
        return 'durable-receipt'

    async def stream(messages, info):
        seen.append(deepcopy(messages))
        if len(seen) == 1:
            yield {0: DeltaToolCall(name='save', json_args='{}', tool_call_id='write-1')}
        elif len(seen) == 2:
            waiting.set()
            await asyncio.Event().wait()
        else:
            yield 'The earlier write completed.'

    call = Call(state=State())
    call.messages = [ModelRequest(parts=[UserPromptPart(words)]) for words in ('Earlier A', 'Earlier B')]
    agent = Agent(FunctionModel(stream_function=stream), tools=[Tool(save, takes_ctx=False)])
    nodes = {'owner': Node(agent)}
    task = asyncio.create_task(take_turn(call, nodes, 'Save this.'))
    await asyncio.wait_for(waiting.wait(), 2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert 'durable-receipt' in str(call._protocol)
    await take_turn(call, nodes, 'Did it save?')
    request = seen[-1]
    assert 'durable-receipt' in str(request) and 'write-1' in str(request)
    assert [m for m in request if m.parts] == paired(request)
    assert text(request).count('Save this.') == text(request).count('Did it save?') == 1
    assert writes == ['durable-receipt']
    assert sum(isinstance(p, ToolReturnPart) for m in call._protocol for p in m.parts) == 1


async def cancelled_fragments(native=True, streaming=False, fragments=('+34.', '680 830.', '464.')):
    """History merging cannot erase input when either cancellation path restarts the model."""
    ready = asyncio.Event()
    seen = []

    async def stream(messages, info):
        seen.append(deepcopy(messages))
        if len(seen) < len(fragments):
            if streaming:
                yield 'Let me check. '
            ready.set()
            await asyncio.Event().wait()
        yield 'I have the complete number.'

    call = Call(state=State())
    call.messages = [
        ModelRequest(parts=[UserPromptPart(words)], metadata={'caller_turn': turn})
        for turn, words in ((-2, 'Earlier A'), (-1, 'Earlier B'))
    ]
    nodes = {'owner': Node(Agent(FunctionModel(stream_function=stream)))}
    adapter = Adapter(call, nodes)
    for fragment in fragments[:-1]:
        ready.clear()
        barge = Barge()
        task = asyncio.create_task(
            adapter._turn(fragment) if native else take_turn(call, nodes, fragment, barge=barge)
        )
        await asyncio.wait_for(ready.wait(), 2)
        if native:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            assert task.cancelled(), 'external cancellation must propagate'
        else:
            barge.fire()
            _, finished = await asyncio.wait_for(task, 2)
            assert finished.status == 'interrupted'
        assert fragment in text(call._protocol), (fragment, call._protocol)
        assert call.input_retained
        assert not adapter._carried
    await adapter._turn(fragments[-1])
    actual = [p.content for m in seen[-1] for p in m.parts if isinstance(p, UserPromptPart)]
    assert actual == ['Earlier A', 'Earlier B', *fragments], actual


async def cancellation_before_sdk_start():
    """A Pipecat response frame can be cancelled before an SDK run exists."""
    ready = asyncio.Event()
    seen = []

    class BeforeRun(Adapter):
        async def push_frame(self, frame, direction=None):
            if isinstance(frame, LLMFullResponseStartFrame) and not ready.is_set():
                ready.set()
                await asyncio.Event().wait()

    async def stream(messages, info):
        seen.append(deepcopy(messages))
        yield 'Complete.'

    call = Call(state=State())
    adapter = BeforeRun(call, {'owner': Node(Agent(FunctionModel(stream_function=stream)))})
    task = asyncio.create_task(adapter._turn('+34.'))
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert not seen and not call.input_retained
    assert adapter._carried == '+34.'
    await adapter._turn('680 830 464.')
    assert text(seen[-1]) == '+34. 680 830 464.'


def interrupted_protocol():
    """Two submitted turns cannot be identified from an aggregate callback."""
    call = Call(state=State())
    call.turn_started('Hello')
    call.note_model_text('Partial answer unheard suffix.')
    segment = call.spoken[0]
    note_spoken(call, 'Partial answer', True, segment_ids=(segment.id,))
    messages = [
        ModelResponse(parts=[ToolCallPart('save', {}, 'complete')]),
        ModelRequest(parts=[ToolReturnPart('save', 'business-receipt', 'complete')]),
        ModelResponse(parts=[ToolCallPart('save', {}, 'orphan')]),
    ]
    view = paired(project(call, messages))
    assert 'orphan' not in str(view) and 'business-receipt' in str(view)
    assert 'unheard suffix' not in text(view) and 'Partial answer' in text(view)
    assert 'orphan' in str(messages)
    call.turn_started('Newer turn')
    call.note_model_text('New answer.')
    adapter = Adapter(call, {})
    current = call._open
    note_spoken(call, 'Partial', True, segment_ids=(segment.id,))
    assert call._open is current, 'late playback closed the newer streaming segment'
    assert not call.note_heard('wrong', segment_ids=('unknown',))
    segment.delivery = 'submitted'
    adapter.playback_started('ambiguous')
    adapter.note_spoken('', True, timestamp='ambiguous')
    assert segment.text == 'Partial' and call.spoken[0].text == 'New answer.'


async def main():
    await snapshots()
    await snapshots(adapter=True)
    await resets()
    await returns()
    await returns(unserved=True)
    await cancellation_keeps_receipts()
    for native in (True, False):
        for streaming in (False, True):
            await cancelled_fragments(native, streaming)
            await cancelled_fragments(native, streaming, ('+34 680 830.', '464.'))
    await cancelled_fragments(fragments=('yes', 'yes', 'Please continue.'))
    await cancellation_before_sdk_start()
    interrupted_protocol()
    print('ok')


if __name__ == '__main__':
    asyncio.run(main())
