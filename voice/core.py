"""One caller turn, with no idea what a caller is.

Contract C0's core half. Everything about *running* a turn lives here --
which agent speaks, where the caller goes next, what the turn is allowed to
spend, what happens when it is interrupted -- and nothing about how any of it
reaches a person. No Pipecat frame, no LiveKit session, no STT and no TTS is
imported in this file, and that is the property the second adapter depends
on.

What comes out is one ordered async stream of events. A runtime translates
them: `RunStarted` and `RunEnded` bracket one model run, `TextDelta` is
speech to stream, `FixedSpeech` is a line this code wrote rather than
generated, and `TurnFinished` says how the turn ended -- answered,
interrupted, exhausted or failed, explicitly, never implied by the absence of
a complaint.

Two things deliberately stay callbacks rather than events, because C0 allows
it and the plumbing is much simpler: `call._say` and `call._end`, which a
*tool* invokes in the middle of its own execution. A filler line has to reach
TTS before the tool it is covering returns, and routing it through this
generator would mean a queue and a select loop to buy back the ordering that
awaiting the callback already has. The awaited outcome is the same either
way.
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

from voice.harness import (
    CallContext,
    Checkpoint,
    Handoff,
    Node,
    Question,
    TurnBudget,
    _turn_generation,  # the turn a late commit would be from
)
from voice.harness import moved_by as _moved_by
from voice.harness import pick as _pick
from voice.harness import route as _route
from voice.history import paired, project

STUMBLE = 'Sorry, I had a problem there. Could you say that again?'
# Said when a turn runs out of budget: no further model request, no business
# work, and no second chance at an allowance that is already spent.
EXHAUSTED = "Sorry, I'm going round in circles there. Could you say that again?"
# Said when the model has told the caller it changed something the business
# has no record of changing. It cannot be unsaid -- the words are already at
# TTS -- so the next thing the caller hears is the correction.
UNBACKED = "Sorry, let me correct myself: I haven't actually made that change. Let me check where we are."
# Said in place of a sentence that claimed a change the business has no record
# of, on a step that can write. Unlike `UNBACKED`, these words were caught
# before they reached TTS, so nothing has to be taken back.
WITHHELD = 'Sorry -- nothing has been changed yet. Let me check where we are.'
# Where one clause ends. On a guarded step, speech is held to here so it can
# be read before it is said; the cap is what stops a model that never
# punctuates from holding the whole answer.
_CLAUSE = re.compile(r'[.!?,;:]')
_CLAUSE_MAX = 60


# --- what a turn emits -------------------------------------------------------


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
    interruptible and a trace can tell a filler from a confirmation.
    """

    text: str
    reason: Literal['error', 'exhausted', 'unbacked', 'withheld']


@dataclass(frozen=True)
class TurnFinished:
    """How the turn ended, said out loud rather than inferred.

    An empty event stream is not success and an absent error is not success.
    `status` is always one of the four, and `detail` carries the reason a
    turn that did not answer did not answer.
    """

    status: Literal['answered', 'interrupted', 'exhausted', 'failed']
    agent: str
    handoffs: tuple[str, ...]
    requests: int
    detail: str = ''


TurnEvent = RunStarted | TextDelta | RunEnded | FixedSpeech | TurnFinished


# --- barge-in ----------------------------------------------------------------


@dataclass
class Barge:
    """The caller talking over us, signalled from outside the turn.

    The runtime owns the signal, because only the runtime can hear it. The
    core owns what it does: cancel the run in flight and stop before starting
    another. One per turn, so a stale signal cannot fence the next one.
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
            # `_events` is annotated `object` on purpose, so this file imports
            # no runtime's event type.
            self._events.cancel()  # ty: ignore[unresolved-attribute]


# --- one turn ----------------------------------------------------------------


async def run_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget,
    barge: Barge | None = None,
) -> AsyncIterator[TurnEvent]:
    """Run agents until one answers the caller, and say what happened.

    The whole turn spends one budget: one tally of provider requests, one
    tool-call allowance, a fixed number of step runs and a deadline over all
    of them together. Threaded through every run, so four agents in one turn
    cannot each start again with a full allowance.
    """
    barge = barge if barge is not None else Barge()
    # This turn's identity. Read back before any move is committed, so a turn
    # that was superseded while it was unwinding cannot still move the caller.
    generation = call.begin_turn()
    # Everything this turn runs -- including a tool task that outlives it --
    # carries this number, so `commit` can tell a late write from a current
    # one without every tool having to hold it.
    _turn_generation.set((call.id, generation))
    usage, limits = budget.account()
    handoffs: list[str] = []
    # A new silence to cover, so one fixed line is allowed again.
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
                    # The two sentinels are filtered out above, which ty tracks
                    # as a negated intersection rather than as the public union.
                    yield event  # ty: ignore[invalid-yield]
    except TimeoutError:
        spent = f'the {budget.seconds:g}s turn deadline'
    except UsageLimitExceeded as over:
        spent = str(over)

    if failure:
        # An error the caller can hear beats silence on an open line.
        call.note_said('fixed', STUMBLE, origin='error')
        yield FixedSpeech(STUMBLE, reason='error')
    elif spent and not barge.hit:
        # The turn ran out. One fixed line: no further provider request, no
        # business work, and nothing that hands the exhausted budget back its
        # money. There used to be one more model run here, outside the budget
        # that had just been declared spent -- so the budget bounded nothing,
        # and the run it bought still held every ordinary tool.
        logger.warning('turn exhausted: {}', spent)
        call.note_said('fixed', EXHAUSTED, origin='error')
        yield FixedSpeech(EXHAUSTED, reason='exhausted')

    # Checked whatever else happened in the turn, because the sentence is
    # already on its way to the caller and an exhausted or failed turn is
    # exactly when a model is most likely to have invented an outcome. Not
    # after a barge-in: the caller talked over it, so what they heard is not
    # what was generated and `note_heard` has already cut it down.
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


@dataclass(frozen=True)
class _Exhausted:
    """Internal: the hop loop ran out. Not part of the public stream."""

    why: str


@dataclass(frozen=True)
class _Failed:
    """Internal: a run raised something that is not a budget or a barge-in."""

    why: str


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
    """Run agents until one answers, yielding what each one says."""
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
                # A refusal is context for the ordinary model path, never permission to retry a write.
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
            # A question the application worded, said by the application. It
            # ends the turn like any other answer, and what it records is the
            # one sentence the caller's next reply will be read as an answer
            # to. Said through `speak_now` for the same reason a confirmation
            # is: the words are already decided, and a round trip spent asking
            # a model to repeat them is a round trip of silence.
            await call.speak_now(output.put(call), origin='ask')
        if not isinstance(output, Handoff):
            # The free edge. A borrowed tool *succeeded*, so the caller is
            # already in the step that owns it, and the request that moved
            # them is the one that just answered them. Set after the run, not
            # during, so the step that spoke is the step whose prompt wrote
            # the words.
            landed = _moved_by(call.state, node, agents, succeeded)
            if landed != call.state.agent:
                if not call.is_current(generation) or barge.hit:
                    # Either a newer caller turn has been accepted since this
                    # one started, or the caller talked over this one. Both
                    # mean the same thing: this result is about a question
                    # they have moved on from. The tool's own effect stands
                    # and is reconciled by its operation token; where the
                    # caller *is* is no longer this turn's to say.
                    #
                    # The barge half was missing, and a turn could come back
                    # `status='interrupted'` having moved the caller from
                    # `concierge` into `booking` on a diary read nobody heard
                    # the answer to.
                    logger.info('fenced a move to {} from turn {}', landed, generation)
                    return
                logger.info('moved {} -> {} (via {})', call.state.agent, landed, ', '.join(sorted(succeeded)))
                handoffs.append(landed)
                call.commit(agent=landed)
            return
        # Where they were sent, then where the graph actually puts them:
        # `booking` before anyone is identified means `verify` first, with
        # `booking` recorded to resume into.
        if not call.is_current(generation) or barge.hit:
            # The same fence on an explicit handoff. A superseded or
            # interrupted turn does not get to choose who takes the caller
            # next: the agent they are with hears what they actually said and
            # can hand them over again if it is still the right answer.
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
            going_to, waiting = _route(call.state, agents, output.to)
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
        # One commit: the step and what is waiting behind it are the same
        # decision, and a caller moved into `verify` with no record of where
        # they were going has lost their errand.
        call.commit(agent=going_to, pending=waiting)
        # The caller's turn is asked once and appears once. It used to be
        # re-sent to every agent that took over, because the *last* message a
        # receiving run reads is otherwise somebody else's spent tool call --
        # `hand_over` answered with "Final result processed." and nothing to
        # reply to. Measured on the call of 2026-09-20: Gemini returned a
        # candidate with no parts at all, pydantic-ai sent back "Please return
        # text or include your response in a tool call", and the answer came
        # on the retry, 0.64s of the 3.94s that turn cost.
        #
        # What replaces it is a projection rather than a repetition: every
        # prompt renders `{{heard}}`, so the agent taking over reads the
        # caller's exact words in its own instructions. One `UserPromptPart`
        # per turn, whichever steps the turn passes through, which is what
        # C3 asks for -- and the receiving run still has something to answer.
        if output.status == 'handoff':
            prompt = None
        if barge.hit:
            # Barged in during the hop. Starting the next agent now would
            # answer a question the caller has moved on from.
            return
        if lines := agents[going_to].enter:
            # Cover the hop: a whole model request stands between the caller
            # finishing and the next agent's first word. Through the call, so
            # a tool firing next does not cover it twice.
            await call.say(_pick(f'enter:{output.to}', lines), origin='entry')
    yield _Exhausted(f'the {budget.step_runs}-run hop budget')


@dataclass
class _Outcome:
    """Internal: what one run produced, handed back to the hop loop."""

    output: object
    succeeded: set[str]


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
    one run is a capability on the agent itself, not something here.
    """
    agent: Agent = node.agent
    output_type = node.outputs(call.state) if node.outputs is not None else None
    step = call.state.agent
    succeeded: set[str] = set()
    output = None
    spoke = False
    # On a step that can write, speech is released a clause at a time so each
    # clause can be read before the caller hears it. `None` once the step has
    # written something -- the confirmation is then the diary's own words and
    # there is nothing left to claim falsely -- or on a step that cannot write
    # at all, where every sentence streams token by token as before.
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
            # A `reset` step is asked the caller's own question outright,
            # because it is being sent no conversation to find it in -- and a
            # run with neither has nothing to answer at all. On an ordinary
            # step this is the caller's turn on the first hop and `None`
            # afterwards, because by then it is in the history.
            accepted_prompt,
            deps=call,
            # C3's two history modes. A `reset` step is sent none of the
            # conversation: what it needs is projected into its instructions,
            # and what it does not need it cannot leak.
            message_history=initial,
            capabilities=[Hooks(before_model_request=prepare, after_model_request=tag)],
            output_type=output_type,
            # The turn's own tally, not this run's: a turn that hands the
            # caller between four agents spends one allowance between them.
            usage=usage,
            usage_limits=limits,
            # Semantic repair only. A provider's own HTTP retries are a
            # different layer with a different counter, and folding them
            # together would let a flaky network buy extra attempts at getting
            # a tool call right.
            retries=budget.repair_retries,
        ) as events:
            barge.attach(events)
            async for event in events:
                if isinstance(event, PartStartEvent):
                    if event.index == 0:
                        streamed += 1
                    call._response = f'{run_id}:{streamed}'
                    # The first thing this provider request produced, and
                    # what kind of thing it was. A tool call closes the
                    # request's clock exactly as speech does -- the model
                    # answered, the caller just cannot hear this one -- and a
                    # time to first token that does not say which it measured
                    # cannot be compared with another architecture's.
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
                            # Mid-clause on a step that can write. Nothing
                            # goes out until the clause can be read.
                            continue
                        text, held = held, ''
                        if call.claims_completion(text):
                            # It says the business changed and nothing has.
                            # These words have not left this process, so
                            # unlike the after-the-fact correction they can
                            # simply not be said.
                            logger.error('withheld an unbacked claim: {!r}', text.strip())
                            gate = 'blocked'
                            break
                    else:
                        text = chunk
                    # Into the ledger as it streams, so a fixed line spoken
                    # by a tool later in this run lands *after* what was said
                    # before it and *before* what is said after.
                    call.note_model_text(text)
                    if not spoke:
                        spoke = True
                        # This turn has model speech of its own, so there is
                        # something in the history for `note_spoken` to cut
                        # down if the caller talks over it.
                        call._spoke = True
                        # The model is talking, so the gap is closed: a tool
                        # called later in this turn may speak its own line.
                        call._covered = False
                    yield TextDelta(text, step)
                elif isinstance(event, FunctionToolResultEvent):
                    # Which tools *succeeded* is how a free edge is taken. The
                    # result, never the call: a `read_diary` that refused an
                    # unidentified caller used to move them into `booking`
                    # anyway, because the call event fires before the tool
                    # runs and whatever it then does.
                    if not isinstance(event.part, RetryPromptPart):
                        succeeded.add(event.part.tool_name)
                elif isinstance(event, AgentRunResultEvent):
                    retain(event.result.new_messages())
                    output = event.result.output
    except UsageLimitExceeded, TimeoutError:
        # The turn's budget, not this run's problem. Straight up to
        # `run_turn`, which owns the allowance -- swallowed here it would
        # become one more agent run apologising, which is the unbudgeted extra
        # request this replaced.
        raise
    except RunCancelled as cancelled:
        # Barge-in. Keep what completed: a tool that ran and returned is a
        # thing that happened, and the next turn has to know about it.
        retain(cancelled.new_messages())
    except Exception as error:
        logger.exception('agent run failed')
        yield RunEnded(step)
        yield _Failed(f'{type(error).__name__}: {error}')
        return
    finally:
        if events is not None and not retained_run:
            # SDK normalization can merge old requests, changing their count.
            # Read its new-message boundary after teardown, including native
            # task cancellation; slicing by len(initial) loses accepted input.
            try:
                new_messages = events.new_messages()
            except UserError:
                # Cancellation before the lazy SDK run starts: the adapter
                # carries this input because no request has retained it.
                pass
            else:
                retain(new_messages)
        barge.detach()
        # Whatever was still mid-clause when the run ended goes out: the model
        # stopped, so there is nothing more coming to complete it.
        if held and gate != 'blocked':
            call.note_model_text(held)
            call._spoke = True
            yield TextDelta(held, step)
        call.close_model_text()

    if gate == 'blocked':
        # The model's history has the whole sentence; the caller heard the
        # part that went out before it. Same reconciliation a barge-in gets,
        # for the same reason: the next request must not be told the caller
        # heard something they did not.
        note_spoken(call, _delivered(call), interrupted=True)
        call.note_said('fixed', WITHHELD, origin='error')
        yield FixedSpeech(WITHHELD, reason='withheld')
    yield RunEnded(step)
    yield _Outcome(output, succeeded)


def _delivered(call: CallContext) -> str:
    """What the caller actually heard of the model's current segment."""
    segments = [s for s in call.spoken if s.kind == 'model']
    return segments[-1].text if segments else ''


# --- delivered-history reconciliation ----------------------------------------


def note_spoken(
    call: CallContext, content: str, interrupted: bool, *, segment_ids: tuple[str, ...] = ()
) -> None:
    """Reconcile identified speech; never trim the last protocol response by position."""
    if not interrupted and not segment_ids:
        return
    call.note_heard(content, segment_ids=segment_ids)


# --- a runtime-free way to run one turn --------------------------------------


async def take_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget | None = None,
    barge: Barge | None = None,
) -> tuple[str, TurnFinished]:
    """Everything one turn said, and how it ended. For tests and benchmarks.

    The same core the Pipecat processor drives, with the events collected
    instead of translated into frames -- which is what makes "the same
    scenario, both ways" a comparison rather than two different programs.
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
