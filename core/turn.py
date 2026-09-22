"""One caller turn, with no idea what a caller is.

Everything about *running* a turn lives here: which agent speaks, where the
caller goes next, what the turn is allowed to spend, what happens when it is
interrupted. Nothing about how any of it reaches a person: no Pipecat frame,
no STT and no TTS is imported in this file, and that is the property a second
runtime adapter depends on.

What comes out is one ordered async stream of events. A runtime translates
them: `RunStarted` and `RunEnded` bracket one model run, `TextDelta` is speech
to stream, `FixedSpeech` is a line this code wrote rather than generated, and
`TurnFinished` says how the turn ended, explicitly, never implied by the
absence of a complaint.

Two things deliberately stay callbacks rather than events: `call._say` and
`call._end`, which a *tool* invokes in the middle of its own execution. A line
spoken by a tool has to reach TTS before the tool returns, and routing it
through this generator would mean a queue and a select loop to buy back the
ordering that awaiting the callback already has.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from loguru import logger
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolResultEvent,
    ModelRetry,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    RunCancelled,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    UserError,
)
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import InstructionPart, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.usage import UsageLimitExceeded

from core.graph import Node, moved_by, route
from core.history import paired, project
from core.policies import TurnBudget, pick
from core.state import CallContext, Checkpoint, Handoff, Question, turn_generation

STUMBLE = 'Sorry, I had a problem there. Could you say that again?'
"""Said when a run raised: an error the caller can hear beats silence on an open line."""
EXHAUSTED = "Sorry, I'm going round in circles there. Could you say that again?"
"""Said when a turn runs out of budget: no further model request, no business work, no second allowance."""
UNBACKED = "Sorry, let me correct myself: I haven't actually made that change. Let me check where we are."
"""Said when the model told the caller it changed something the business has no record of changing.

It cannot be unsaid, the words are already at TTS, so the next thing the
caller hears is the correction.
"""
WITHHELD = 'Sorry -- nothing has been changed yet. Let me check where we are.'
"""Said in place of a clause that claimed a change nothing made, on a step that can write.

Unlike `UNBACKED`, these words were caught before they reached TTS, so nothing
has to be taken back.
"""
_CLAUSE = re.compile(r'[.!?,;:]')
_CLAUSE_MAX = 60
"""Where one clause ends, and the cap that stops a model that never punctuates from holding the whole answer."""


@dataclass(frozen=True)
class RunStarted:
    """One model run is about to begin. A runtime opens its response here."""

    step: str


@dataclass(frozen=True)
class TextDelta:
    """Speech, as it arrives. Ordered, and never a tool's arguments."""

    text: str
    step: str


@dataclass(frozen=True)
class RunEnded:
    """That model run is over, however it went."""

    step: str


@dataclass(frozen=True)
class FixedSpeech:
    """A line this code wrote, not one a model generated.

    `reason` is what it is covering, so a runtime can decide whether it is
    interruptible and a trace can tell a correction from a confirmation.
    """

    text: str
    reason: Literal['error', 'exhausted', 'unbacked', 'withheld']


@dataclass(frozen=True)
class TurnFinished:
    """How the turn ended, said out loud rather than inferred.

    An empty event stream is not success and an absent error is not success.
    `status` is always one of the four, and `detail` carries the reason a turn
    that did not answer did not answer.
    """

    status: Literal['answered', 'interrupted', 'exhausted', 'failed']
    agent: str
    handoffs: tuple[str, ...]
    requests: int
    detail: str = ''


TurnEvent = RunStarted | TextDelta | RunEnded | FixedSpeech | TurnFinished


@dataclass
class Barge:
    """The caller talking over us, signalled from outside the turn.

    The runtime owns the signal, because only the runtime can hear it. The
    turn owns what it does: cancel the run in flight and stop before starting
    another. One per turn, so a stale signal cannot fence the next one.

    `_events` is annotated `object` on purpose, so this file imports no
    runtime's event type.
    """

    hit: bool = False
    _events: object = field(default=None, repr=False)

    def attach(self, events) -> None:
        self._events = events

    def detach(self) -> None:
        self._events = None

    def fire(self) -> None:
        self.hit = True
        if self._events is not None:
            self._events.cancel()  # ty: ignore[unresolved-attribute]


@dataclass(frozen=True)
class _Exhausted:
    """Internal: the hop loop ran out. Not part of the public stream."""

    why: str


@dataclass(frozen=True)
class _Failed:
    """Internal: a run raised something that is not a budget or a barge-in."""

    why: str


@dataclass
class _Outcome:
    """Internal: what one run produced, handed back to the hop loop."""

    output: object
    succeeded: set[str]


async def run_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget,
    barge: Barge | None = None,
) -> AsyncIterator[TurnEvent]:
    """Run agents until one answers the caller, and say what happened.

    The whole turn spends one budget: one tally of provider requests, one
    tool-call allowance, a fixed number of step runs and a deadline over all of
    them together, threaded through every run so four agents in one turn
    cannot each start again with a full allowance.

    The turn's generation is read back before any move is committed, so a turn
    superseded while it was unwinding cannot still move the caller; it is also
    set on the context so a tool task that outlives the turn reports it to
    `commit`.

    An exhausted turn is told so in one fixed line, with no further model
    request: there used to be one more run here, outside the budget that had
    just been declared spent, so the budget bounded nothing. The unbacked-claim
    check runs whatever else happened, because an exhausted or failed turn is
    exactly when a model is most likely to have invented an outcome; not after
    a barge-in, because then what the caller heard is not what was generated.
    """
    barge = barge if barge is not None else Barge()
    generation = call.begin_turn()
    turn_generation.set((call.id, generation))
    usage, limits = budget.account()
    handoffs: list[str] = []
    call._covered = False
    call.turn_started(prompt or '')

    spent = ''
    failure = ''
    try:
        async with asyncio.timeout(budget.seconds):
            async for event in _hops(
                call, agents, prompt, budget, usage, limits, handoffs, barge, generation
            ):
                if isinstance(event, _Exhausted):
                    spent = event.why
                elif isinstance(event, _Failed):
                    failure = event.why
                else:
                    yield event  # ty: ignore[invalid-yield]
    except TimeoutError:
        spent = f'the {budget.seconds:g}s turn deadline'
    except UsageLimitExceeded as over:
        spent = str(over)

    if failure:
        call.note_said('fixed', STUMBLE, origin='error')
        yield FixedSpeech(STUMBLE, reason='error')
    elif spent and not barge.hit:
        logger.warning('turn exhausted: {}', spent)
        call.note_said('fixed', EXHAUSTED, origin='error')
        yield FixedSpeech(EXHAUSTED, reason='exhausted')

    if not barge.hit and (claim := call.unbacked_claim()):
        logger.error('unbacked claim, nothing was written this turn: {!r}', claim)
        call.note_said('fixed', UNBACKED, origin='error')
        yield FixedSpeech(UNBACKED, reason='unbacked')

    status: Literal['answered', 'interrupted', 'exhausted', 'failed'] = 'answered'
    if failure:
        status = 'failed'
    elif barge.hit:
        status = 'interrupted'
    elif spent:
        status = 'exhausted'
    yield TurnFinished(
        status=status,
        agent=call.state.agent,
        handoffs=tuple(handoffs),
        requests=usage.requests,
        detail=failure or spent,
    )


async def _hops(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget,
    usage,
    limits,
    handoffs: list[str],
    barge: Barge,
    generation: int,
) -> AsyncIterator[object]:
    """Run agents until one answers, yielding what each one says.

    Before a step's agent runs, its `advance` may name a known next action:
    one that needs no model because the caller's answer to a tracked question
    already decides it. It spends the shared tool budget, retains the caller's
    input and is bracketed by the same run events, and a refusal from it is
    context for the model path that follows, never permission to retry a write.

    A `Question` output is a question the application worded, said by the
    application and ending the turn like any other answer; what it records is
    the one sentence the caller's next reply will be read as an answer to.

    A run that did not hand over may still have moved the caller by a borrowed
    tool that *succeeded*; the move is committed after the run, so the step
    that spoke is the step whose prompt wrote the words. Every move and every
    handoff is fenced on the turn's generation and the barge signal: a
    superseded or interrupted turn does not get to choose who takes the caller
    next, though a tool's own effect stands and is reconciled by its operation
    token.

    A prerequisite step reporting `completed` or `unserved` restores the
    owner's checkpoint and tells the receiving agent, through an instruction
    part, what just happened. An ordinary handoff commits the step and the
    pending slot together, because a caller moved into `verify` with no record
    of where they were going has lost their errand, and clears the prompt: the
    caller's turn is projected into the receiving agent's instructions as
    `{{heard}}`, so it appears once whichever steps the turn passes through.
    """
    for _ in range(budget.step_runs):
        node = agents[call.state.agent]
        output = None
        succeeded: set[str] = set()

        action = node.advance(call) if node.advance else None
        if action is not None:
            if barge.hit or not call.is_current(generation):
                return
            if usage.tool_calls >= budget.tool_calls:
                yield _Exhausted(f'the {budget.tool_calls}-tool budget')
                return
            call.retain_input()
            prompt = None
            call.attempt_started()
            call._response = f'{call.turn}:{call.attempt}:application'
            usage.tool_calls += 1
            yield RunStarted(call.state.agent)
            if barge.hit or not call.is_current(generation):
                yield RunEnded(node.agent.name or call.state.agent)
                return
            try:
                output = await action.run()
            except ModelRetry as error:
                call.retain_action(action.name, {'refused': str(error)})
                call._return_status = f'Application action {action.name} refused: {error}'
            except Exception as error:
                logger.exception('application action failed: {}', action.name)
                yield _Failed(f'{type(error).__name__}: {error}')
                return
            yield RunEnded(node.agent.name or call.state.agent)
        if action is None or output is None:
            async for event in _speak(call, node, prompt, budget, usage, limits, barge):
                if isinstance(event, _Outcome):
                    output, succeeded = event.output, event.succeeded
                elif isinstance(event, _Failed):
                    yield event
                    return
                else:
                    yield event

        call._return_status = ''
        if isinstance(output, Question) and not barge.hit:
            await call.speak_now(output.put(call), origin='ask')
        if not isinstance(output, Handoff):
            landed = moved_by(call.state, node, agents, succeeded)
            if landed != call.state.agent:
                if not call.is_current(generation) or barge.hit:
                    logger.info('fenced a move to {} from turn {}', landed, generation)
                    return
                logger.info('moved {} -> {} (via {})', call.state.agent, landed, ', '.join(sorted(succeeded)))
                handoffs.append(landed)
                call.commit(agent=landed)
            return
        if not call.is_current(generation) or barge.hit:
            logger.info('fenced a handoff from turn {}', generation)
            return
        if output.status != 'handoff':
            going_to, waiting = output.to, None
            checkpoint = call._checkpoint
            if checkpoint is not None:
                call._hidden_speech.update(s.id for s in call.all_spoken if s.id not in checkpoint.speech_ids)
                call.messages = checkpoint.messages
                call._return_status = (
                    f'Application return: {call.state.agent} {output.status}. '
                    f'Original caller request: {checkpoint.heard}'
                )
                if output.status == 'completed':
                    going_to = checkpoint.owner
                else:
                    call._return_status += ' The earlier request was abandoned; do not resume it.'
                call._checkpoint = None
                prompt = (
                    call.heard if output.status == 'unserved' and call.heard != checkpoint.heard else None
                )
        else:
            going_to, waiting = route(call.state, agents, output.to)
            if waiting is not None and going_to != waiting and call._checkpoint is None:
                call._checkpoint = Checkpoint(
                    waiting,
                    deepcopy(call.messages),
                    frozenset(s.id for s in call.all_spoken),
                    call.heard,
                )
            call._return_status = (
                f'Application handoff: {call.state.agent} -> {going_to}. '
                f'You are now {going_to}; handle the caller request here. '
                'The previous handoff finished routing, not the caller request.'
            )
        logger.info('handoff {} -> {} ({})', call.state.agent, going_to, output.because or '-')
        handoffs.append(going_to)
        call.commit(agent=going_to, pending=waiting)
        if output.status == 'handoff':
            prompt = None
        if barge.hit:
            return
        if lines := agents[going_to].enter:
            await call.say(pick(f'enter:{output.to}', lines), origin='entry')
    yield _Exhausted(f'the {budget.step_runs}-run hop budget')


def _speech(event) -> str:
    """The text of an event that is speech, and '' for everything that is not.

    A `Handoff` arrives as a tool call part, so it never matches here and
    never reaches the caller's ears.
    """
    match event:
        case PartStartEvent(part=TextPart(content=text)):
            return text
        case PartDeltaEvent(delta=TextPartDelta(content_delta=text)):
            return text
    return ''


async def _speak(
    call: CallContext,
    node: Node,
    prompt: str | None,
    budget: TurnBudget,
    usage,
    limits,
    barge: Barge,
) -> AsyncIterator[object]:
    """Run one step's agent, streaming whatever it says.

    `Node.outputs` builds the run's `output_type` from the live state; the
    per-request narrowing that keeps it honest across several requests inside
    one run is a capability on the agent itself.

    On a step that can write, speech is released a clause at a time so each
    clause can be read before the caller hears it, and a clause that claims a
    change nothing has made is not said at all. The gate is off once the step
    has written something, because the confirmation is then the business's
    own words, and off entirely on a step that cannot write.

    A `reset` step is asked the caller's own question outright and sent no
    conversation: what it needs is projected into its instructions, and what
    it does not need it cannot leak. An ordinary step gets the caller's turn on
    the first hop and `None` afterwards, because by then it is in the history.
    Every request's messages are projected through `history.project` before
    they go out, and any application status is appended as an instruction.

    The run spends the turn's own tally, not one of its own, and `retries` is
    semantic repair only: a provider's HTTP retries are a different layer, and
    folding them together would let a flaky network buy extra attempts at
    getting a tool call right.

    Which tools *succeeded* is how a free edge is taken: the result, never the
    call, because the call event fires before the tool runs.

    Messages are retained across cancellation from the SDK's own
    `new_messages()` boundary after teardown, never by slicing at the original
    history length: SDK normalisation can merge old requests, and a cancelled
    run still heard the caller and may have completed a tool.
    """
    agent: Agent = node.agent
    output_type = node.outputs(call.state) if node.outputs is not None else None
    step = call.state.agent
    succeeded: set[str] = set()
    output = None
    spoke = False
    gate: str | None = 'clause' if node.guarded and not call.wrote_this_turn() else None
    held = ''

    call.attempt_started()
    caller_turn = call.turn
    run_id = f'{caller_turn}:{call.attempt}'
    accepted_prompt = call.heard if node.history == 'reset' else prompt
    requested = 0
    streamed = 0
    excluded = frozenset(s.id for s in call.all_spoken) if node.history == 'reset' else frozenset()
    initial = [] if node.history == 'reset' else paired(project(call, call.messages))

    async def prepare(ctx, request):
        nonlocal requested
        requested += 1
        call._response = f'{run_id}:{requested}'
        messages = project(call, request.messages, excluded=excluded)
        parameters = request.model_request_parameters
        if call._return_status:
            parameters = replace(
                parameters,
                instruction_parts=[
                    *(parameters.instruction_parts or []),
                    InstructionPart(content=call._return_status),
                ],
            )
        return replace(request, messages=messages, model_request_parameters=parameters)

    async def tag(ctx, *, request_context, response):
        response.metadata = (response.metadata or {}) | {'voice_response': f'{run_id}:{requested}'}
        return response

    retained_run = False
    events = None

    def retain(new_messages):
        nonlocal retained_run
        retained_run = True
        retained = deepcopy(new_messages)
        for message in retained:
            if isinstance(message, ModelRequest) and any(
                isinstance(p, UserPromptPart) for p in message.parts
            ):
                message.metadata = (message.metadata or {}) | {'caller_turn': caller_turn}
                if accepted_prompt is not None and any(
                    isinstance(p, UserPromptPart) and p.content == accepted_prompt for p in message.parts
                ):
                    call._retained_turns.add(caller_turn)
            if isinstance(message, ModelResponse) and not (message.metadata or {}).get('voice_response'):
                message.metadata = (message.metadata or {}) | {'voice_response': f'{run_id}:{requested}'}
        call._protocol.extend(deepcopy(retained))
        call.messages.extend(retained)

    yield RunStarted(step)
    try:
        async with agent.run_stream_events(
            accepted_prompt,
            deps=call,
            message_history=initial,
            capabilities=[Hooks(before_model_request=prepare, after_model_request=tag)],
            output_type=output_type,
            usage=usage,
            usage_limits=limits,
            retries=budget.repair_retries,
        ) as events:
            barge.attach(events)
            async for event in events:
                if isinstance(event, PartStartEvent):
                    if event.index == 0:
                        streamed += 1
                    call._response = f'{run_id}:{streamed}'
                    match event.part:
                        case TextPart():
                            call.note_first_output('text')
                        case ToolCallPart():
                            call.note_first_output('tool')
                if chunk := _speech(event):
                    if not node.model_speech:
                        continue
                    if gate is not None:
                        held += chunk
                        if not (_CLAUSE.search(held) or len(held) >= _CLAUSE_MAX):
                            continue
                        text, held = held, ''
                        if call.claims_completion(text):
                            logger.error('withheld an unbacked claim: {!r}', text.strip())
                            gate = 'blocked'
                            break
                    else:
                        text = chunk
                    call.note_model_text(text)
                    if not spoke:
                        spoke = True
                        call._spoke = True
                        call._covered = False
                    yield TextDelta(text, step)
                elif isinstance(event, FunctionToolResultEvent):
                    if not isinstance(event.part, RetryPromptPart):
                        succeeded.add(event.part.tool_name)
                elif isinstance(event, AgentRunResultEvent):
                    retain(event.result.new_messages())
                    output = event.result.output
    except UsageLimitExceeded, TimeoutError:
        raise
    except RunCancelled as cancelled:
        retain(cancelled.new_messages())
    except Exception as error:
        logger.exception('agent run failed')
        yield RunEnded(step)
        yield _Failed(f'{type(error).__name__}: {error}')
        return
    finally:
        if events is not None and not retained_run:
            try:
                new_messages = events.new_messages()
            except UserError:
                pass
            else:
                retain(new_messages)
        barge.detach()
        if held and gate != 'blocked':
            call.note_model_text(held)
            call._spoke = True
            yield TextDelta(held, step)
        call.close_model_text()

    if gate == 'blocked':
        note_spoken(call, _delivered(call), interrupted=True)
        call.note_said('fixed', WITHHELD, origin='error')
        yield FixedSpeech(WITHHELD, reason='withheld')
    yield RunEnded(step)
    yield _Outcome(output, succeeded)


def _delivered(call: CallContext) -> str:
    """What the caller actually heard of the model's current segment."""
    segments = [s for s in call.spoken if s.kind == 'model']
    return segments[-1].text if segments else ''


def note_spoken(
    call: CallContext, content: str, interrupted: bool, *, segment_ids: tuple[str, ...] = ()
) -> None:
    """Reconcile identified speech with what the runtime says was delivered.

    Barge-in stops speech mid-sentence, but the run recorded every token the
    model produced. The delivered prefix comes from the runtime, which is the
    only thing that knows it. Never trims the last protocol response by
    position.
    """
    if not interrupted and not segment_ids:
        return
    call.note_heard(content, segment_ids=segment_ids)


async def take_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget | None = None,
    barge: Barge | None = None,
) -> tuple[str, TurnFinished]:
    """Everything one turn said, and how it ended. For tests.

    The same engine the Pipecat processor drives, with the events collected
    instead of translated into frames, which is what makes "the same scenario,
    both ways" a comparison rather than two different programs.
    """
    said: list[str] = []
    done: TurnFinished | None = None
    started = time.perf_counter()
    async for event in run_turn(call, agents, prompt, budget or TurnBudget(), barge):
        if isinstance(event, TextDelta | FixedSpeech):
            said.append(event.text)
        elif isinstance(event, TurnFinished):
            done = event
    assert done is not None, 'run_turn always finishes with a TurnFinished'
    logger.debug('turn {} in {:.2f}s', done.status, time.perf_counter() - started)
    return ''.join(said), done
