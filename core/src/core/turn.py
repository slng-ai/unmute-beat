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
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

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
    ToolCallEvent,
    ToolCallPart,
    ToolResultEvent,
    UserError,
)
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import InstructionPart, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.usage import UsageLimitExceeded

from core.flow import Answered, Flow, Moved
from core.graph import LocalAction, Node, moved_by, route
from core.history import paired, project
from core.locale import ENGLISH
from core.policies import TurnBudget, pick
from core.state import _FILLERS, CallContext, Checkpoint, Handoff, Question, Spoken, turn_generation

STUMBLE, EXHAUSTED, UNBACKED, WITHHELD = (
    ENGLISH.stumble,
    ENGLISH.exhausted,
    ENGLISH.unbacked,
    ENGLISH.withheld,
)
"""The English fixed lines, by their old names. What a call hears is its own locale's (`CallContext.locale`)."""
_CLAUSE = re.compile(r'[.!?,;:]')
_CLAUSE_MAX = 60
"""Where one clause ends, and the cap that stops a model that never punctuates from holding the whole answer."""
EARLY_AFTER = 0.05
"""Seconds `advance` may take before the step's run is started beside it (`_Early`).

A decision made by code is back well inside this; one waiting on a model
reading takes 0.7 s or more, and that is the wait the early run overlaps.
"""


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


@dataclass
class _Hold:
    """Internal: what keeps an early run quiet until the readings have decided nothing."""

    opened: asyncio.Event = field(default_factory=asyncio.Event)
    dropped: bool = False
    """The readings decided something: the run is cancelled, and nothing it produced is kept or said."""


_DONE = object()


class _Early:
    """Internal: the step's run, started while `advance` waits on its readings.

    A reading takes 0.7 s or more, and a turn it does not decide used to pay
    for it and then for the step's first request, one after the other. Here
    the request goes out at the same time. The run stops at its first event
    and waits (`_Hold`): the SDK's event stream has no buffer, so the run can
    get no further, and no tool or output function runs, no speech is
    recorded and nothing reaches the caller until the readings are done. If
    they decide an action, the run is dropped; if they decide nothing, it goes
    on from where it stopped, its first request already answered.

    ponytail: a dropped run's request is still billed for its input. That is
    the price of the overlap, paid only on the turns a reading decides.
    """

    def __init__(self, run: Callable[[_Hold], AsyncIterator[object]]) -> None:
        self.hold = _Hold()
        self._queue: asyncio.Queue[object] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for event in run(self.hold):
                    await self._queue.put(event)
            finally:
                self._queue.put_nowait(_DONE)

        self._task = asyncio.ensure_future(pump())

    async def drop(self) -> None:
        """Cancel the run and keep nothing of it."""
        self.hold.dropped = True
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

    async def events(self) -> AsyncIterator[object]:
        """Let the run go on, and pass its events out as they come. Its errors are raised here."""
        self.hold.opened.set()
        try:
            while (event := await self._queue.get()) is not _DONE:
                yield event
            await self._task
        finally:
            if not self._task.done():
                await self.drop()


@dataclass(frozen=True)
class _Stalled:
    """Internal: a step request sent nothing within `TurnBudget.first_part`, so its run was cancelled."""


@dataclass(frozen=True)
class _Exhausted:
    """Internal: the hop loop ran out. Not part of the public stream."""

    why: str


@dataclass(frozen=True)
class _Failed:
    """Internal: a run raised something that is not a budget or a barge-in."""

    why: str


@dataclass(frozen=True)
class _Continue:
    """Internal: this step handed the caller on; run the step they are in now, with this prompt."""

    prompt: str | None


@dataclass
class _Outcome:
    """Internal: what one run produced, handed back to the hop loop."""

    output: object
    succeeded: set[str]


class Turn:
    """One accepted caller turn: agents run until one answers, within one budget.

    The whole turn spends one budget: one tally of provider requests, one
    tool-call allowance, a fixed number of step runs and a deadline over all of
    them together, threaded through every run so four agents in one turn
    cannot each start again with a full allowance.

    The turn's generation is read back before any move is committed, so a turn
    superseded while it was unwinding cannot still move the caller; it is also
    set on the context so a tool task that outlives the turn reports it to
    `commit`. `run_turn` is the function form of this class.
    """

    def __init__(
        self,
        call: CallContext,
        nodes: dict[str, Node[Any]],
        budget: TurnBudget,
        barge: Barge | None = None,
        flow: Flow | None = None,
    ) -> None:
        self.call = call
        self.nodes = nodes
        self.budget = budget
        self.barge = barge if barge is not None else Barge()
        self.usage, self.limits = budget.account()
        self.handoffs: list[str] = []
        self.generation = 0
        self.flow = flow if flow is not None else Flow.of(nodes)
        """The steps table as a Pydantic graph: it walks the hops and refuses a move the table does not allow."""
        self._hops_run = 0
        self._stalls = 0
        """Step requests this turn that sent nothing in time (`TurnBudget.first_part`). The first is tried again."""
        self._step_requests = 0
        """Step requests sent this turn, over every run: the second one is when a holding line goes out."""
        self._holding: asyncio.Task[None] | None = None
        """The holding-line timer, armed only while a tool or an application action is running."""
        self._channel: asyncio.Queue[tuple[object, asyncio.Future[None] | None]] = asyncio.Queue()

    def _stale(self) -> bool:
        """Whether this turn may no longer decide where the caller is."""
        return self.barge.hit or not self.call.is_current(self.generation)

    async def _hold(self) -> None:
        """Say one holding line (`Locale.holding`), only while nothing has been said in this turn.

        Said when a second step request of the turn goes out (`_speak`), and
        on the `TurnBudget.hold_after` timer for any other silence that lasts.
        A second request means a whole model round trip is still ahead: a
        tool's result going back to the model, a retry, or the step a
        hand-over just entered: 0.7 s or more before the answer over seven
        calls of 2026-09-25, and most of the turns that waited long.

        Not when the readings finish, and not when a tool starts. The step's
        first request goes out with the readings (`_Early`), so its answer is
        usually ready when they finish; and a tool that speaks its own line
        would get the holding line right in front of it.

        Once per turn, and never after the answer started: the check is on
        the turn's own speech ledger, which records model text before it is
        passed on. Origin `tool_filler`, so a caller who cuts in over it is
        still in the same turn (`CallContext.continues_turn`). Never once the
        call is ending: a goodbye is the last thing the caller hears.
        """
        call = self.call
        if self._stale() or call.ended or any(s.text for s in call.spoken) or not call.locale.holding:
            return
        await call.say(pick('holding', call.locale.holding), origin='tool_filler')

    async def _hold_after(self) -> None:
        """The holding line for work still running after `TurnBudget.hold_after` seconds."""
        await asyncio.sleep(self.budget.hold_after)
        await self._hold()

    def _arm_hold(self) -> None:
        """Work has started that the caller will wait for: a tool, or an application action.

        The timer is tied to that work, never to the turn: a model that is
        merely slow to say goodbye is not work the caller is waiting on, and a
        turn-long timer put "One moment" in front of a goodbye (session 3).
        """
        if self._holding is None or self._holding.done():
            self._holding = asyncio.ensure_future(self._hold_after())

    def _disarm_hold(self) -> None:
        """The work finished, or the turn did: a holding line not yet said is never said."""
        if self._holding is not None:
            self._holding.cancel()
            self._holding = None

    async def events(self, prompt: str | None) -> AsyncIterator[TurnEvent]:
        """Run the turn, yielding what a runtime needs to voice it, and finish with how it ended.

        An exhausted turn is told so in one fixed line, with no further model
        request: there used to be one more run here, outside the budget that
        had just been declared spent, so the budget bounded nothing. The
        unbacked-claim check runs whatever else happened, because an exhausted
        or failed turn is exactly when a model is most likely to have invented
        an outcome; not after a barge-in, because then what the caller heard is
        not what was generated.
        """
        call, barge = self.call, self.barge
        self.generation = call.begin_turn()
        turn_generation.set((call.id, self.generation))
        call._covered = False
        call.turn_started(prompt or '')
        if prompt is not None:
            prompt = call.fresh or None

        spent = ''
        failure = ''
        try:
            async with asyncio.timeout(self.budget.seconds):
                async for event in self._hops(prompt):
                    if isinstance(event, _Exhausted):
                        spent = event.why
                    elif isinstance(event, _Failed):
                        failure = event.why
                    else:
                        yield event  # ty: ignore[invalid-yield]
        except TimeoutError:
            spent = f'the {self.budget.seconds:g}s turn deadline'
        except UsageLimitExceeded as over:
            spent = str(over)
        finally:
            self._disarm_hold()

        if failure:
            call.note_said('fixed', call.locale.stumble, origin='error')
            yield FixedSpeech(call.locale.stumble, reason='error')
        elif spent and not barge.hit:
            logger.warning('turn exhausted: {}', spent)
            call.note_said('fixed', call.locale.exhausted, origin='error')
            yield FixedSpeech(call.locale.exhausted, reason='exhausted')

        if not barge.hit and (claim := call.unbacked_claim()):
            logger.error('unbacked claim, nothing was written this turn: {!r}', claim)
            call.note_said('fixed', call.locale.unbacked, origin='error')
            yield FixedSpeech(call.locale.unbacked, reason='unbacked')

        call.turn_ended(interrupted=barge.hit)
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
            handoffs=tuple(self.handoffs),
            requests=self.usage.requests,
            detail=failure or spent,
        )

    async def _hops(self, prompt: str | None) -> AsyncIterator[object]:
        """Run agents until one answers, yielding what each one says.

        Before a step's agent runs, its `advance` may name a known next action:
        one that needs no model because the caller's answer to a tracked
        question already decides it. If it declines, a step just entered may
        still open with a known action of its own (`Node.openings`). Either
        way the action spends the shared tool budget, retains
        the caller's input and is bracketed by the same run events, and a
        refusal from it is context for the model path that follows, never
        permission to retry a write.

        A `Question` output is a question the application worded, said by the
        application and ending the turn like any other answer; what it records
        is the one sentence the caller's next reply will be read as an answer
        to.

        A run that did not hand over may still have moved the caller by a
        borrowed tool that *succeeded*; the move is committed after the run,
        so the step that spoke is the step whose prompt wrote the words. Every
        move and every handoff is fenced on the turn's generation and the
        barge signal: a superseded or interrupted turn does not get to choose
        who takes the caller next, though a tool's own effect stands and is
        reconciled by its operation token.

        A prerequisite step reporting `completed` or `unserved` restores the
        owner's checkpoint and tells the receiving agent, through an
        instruction part, what just happened. An ordinary handoff commits the
        step and the pending slot together, because a caller moved into
        `verify` with no record of where they were going has lost their
        errand, and clears the prompt: the caller's turn is projected into the
        receiving agent's instructions as `{{heard}}`, so it appears once
        whichever steps the turn passes through.

        The hops are walked by the flow's Pydantic graph (`core.flow`): each
        graph step runs one `_hop`, and the decision after it sends the caller
        on only along a move the steps table allows. The hop budget is counted
        in `hop`.
        """
        async for event in self._through_graph(prompt):
            yield event

    async def _through_graph(self, prompt: str | None) -> AsyncIterator[object]:
        """Walk the hops through the flow's graph, passing each event out in lockstep.

        The graph runs in its own task, so what a hop yields crosses a channel.
        Each event waits for its acknowledgement, which comes only when this
        generator is asked for the next one, so nothing a hop does after an
        event (a fixed line straight to TTS, a commit) can overtake it. When
        this generator is cancelled or closed, the graph task is cancelled and
        awaited, so a hop's own clean-up still runs before the turn is over.
        """
        task = asyncio.ensure_future(self.flow.run(self, self.call.state.agent, prompt))
        task.add_done_callback(lambda _: self._channel.put_nowait((None, None)))
        try:
            while True:
                event, ack = await self._channel.get()
                if ack is None:
                    break
                yield event
                ack.set_result(None)
            await task
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def hop(self, step: str, prompt: str | None) -> Moved | Answered:
        """Run one hop for the graph (`core.flow.Hopper`), passing its events out, and say where the caller is."""
        if step != self.call.state.agent:
            raise RuntimeError(f'the graph ran {step} but the caller is in {self.call.state.agent}')
        self._hops_run += 1
        if self._hops_run > self.budget.step_runs:
            await self._emit(_Exhausted(f'the {self.budget.step_runs}-run hop budget'))
            return Answered()
        carried: _Continue | None = None
        async for event in self._hop(prompt):
            if isinstance(event, _Continue):
                carried = event
            else:
                await self._emit(event)
        return Answered() if carried is None else Moved(self.call.state.agent, carried.prompt)

    async def _emit(self, event: object) -> None:
        """Hand one event to `_through_graph` and wait until it has been passed on."""
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._channel.put_nowait((event, ack))
        await ack

    async def _hop(self, prompt: str | None) -> AsyncIterator[object]:
        """One step of the turn: the step the caller is in answers, or moves them on.

        Ends with `_Continue` when the caller was handed on and the next step
        should run, carrying the prompt it gets; ends without it when the turn
        is answered, interrupted or failed.
        """
        call, nodes, barge = self.call, self.nodes, self.barge
        node = nodes[call.state.agent]
        output = None
        succeeded: set[str] = set()

        if self._stale():
            return
        retained_before = call.input_retained
        requests_before = len(call.requests)
        action = None
        early: _Early | None = None
        retained_early = retained_before
        if self._hops_run == 1 and (settle := call.settlement()) is not None:
            action = LocalAction('reconcile', settle)
        elif self._hops_run == 1 and (told := call.unheard_receipt()):
            action = LocalAction('receipt', lambda: _say_again(call, told))
        if node.advance and action is None:
            state_before, held_before = call.state, dict(call._held)
            task = asyncio.ensure_future(node.advance(call, self.usage, self.limits))
            barge.attach(task)
            try:
                done, _ = await asyncio.wait({task}, timeout=EARLY_AFTER)
                if not done:
                    retained_early = call.input_retained
                    sent = None if retained_early else prompt
                    early = _Early(lambda hold: self._speak(node, sent, hold))
                action = await task
            except asyncio.CancelledError:
                if early is not None:
                    await early.drop()
                if not barge.hit:
                    raise
                return
            except UsageLimitExceeded, TimeoutError:
                if early is not None:
                    await early.drop()
                raise
            except Exception as error:
                logger.exception('advance failed on {}', call.state.agent)
                if early is not None:
                    await early.drop()
                yield _Failed(f'{type(error).__name__}: {error}')
                return
            finally:
                barge.detach()
            if early is not None and (
                action is not None
                or self._stale()
                or node.opening(call) is not None
                or call.state is not state_before
                or call._held != held_before
                or call.input_retained != retained_early
            ):
                await early.drop()
                early = None
        if self._stale():
            return
        if not retained_before and call.input_retained:
            prompt = None
        classified = next((r for r in call.requests[requests_before:] if r.kind == 'classifier'), None)
        if classified is not None and action is None:
            classified.outcome = 'none'
        if action is None:
            action = node.opening(call)
        if action is not None:
            if self._stale():
                return
            if self.usage.tool_calls >= self.budget.tool_calls:
                yield _Exhausted(f'the {self.budget.tool_calls}-tool budget')
                return
            call.retain_input()
            prompt = None
            call.attempt_started()
            call._response = f'{call.turn}:{call.attempt}:application'
            self.usage.tool_calls += 1
            yield RunStarted(call.state.agent)
            if self._stale():
                yield RunEnded(node.agent.name or call.state.agent)
                return
            decided = classified if classified is not None and not classified.outcome else None
            answers_before = call._answers
            self._arm_hold()
            try:
                output = await action.run()
                if decided is not None:
                    decided.outcome = 'action'
                if _silent(call, output, answers_before):
                    call._return_status = (
                        f'{call._return_status} Application action {action.name} done.'.lstrip()
                    )
                    output = None
            except ModelRetry as error:
                if decided is not None:
                    decided.outcome = 'refused'
                call.retain_action(action.name, {'refused': str(error)})
                call._return_status = (
                    f'{call._return_status} Application action {action.name} refused: {error}'.lstrip()
                )
            except Exception as error:
                logger.exception('application action failed: {}', action.name)
                yield _Failed(f'{type(error).__name__}: {error}')
                return
            finally:
                self._disarm_hold()
            yield RunEnded(node.agent.name or call.state.agent)
        if action is None or output is None:
            failed: _Failed | None = None
            run = early.events() if early is not None else self._speak(node, prompt)
            while True:
                stalled = False
                async for event in run:
                    if failed is not None:
                        continue
                    if isinstance(event, _Outcome):
                        output, succeeded = event.output, event.succeeded
                    elif isinstance(event, _Stalled):
                        stalled = True
                    elif isinstance(event, _Failed):
                        failed = event
                    else:
                        yield event
                if failed is not None:
                    yield failed
                    return
                if not stalled:
                    break
                if self._stale():
                    return
                if self._stalls:
                    yield _Exhausted('a second step request that sent nothing')
                    return
                self._stalls += 1
                run = self._speak(node, None if call.input_retained else prompt)

        call._return_status, call._entered = '', ''
        if isinstance(output, Question) and not barge.hit:
            await call.speak_now(output.put(call), origin='ask')
        if not isinstance(output, Handoff):
            landed = moved_by(call.state, node, nodes, succeeded)
            if landed != call.state.agent:
                if self._stale():
                    logger.info('fenced a move to {} from turn {}', landed, self.generation)
                    return
                logger.info('moved {} -> {} (via {})', call.state.agent, landed, ', '.join(sorted(succeeded)))
                self.handoffs.append(landed)
                call.commit(agent=landed)
            return
        if self._stale():
            logger.info('fenced a handoff from turn {}', self.generation)
            return
        if output.status != 'handoff':
            going_to, waiting = output.to, None
            checkpoint = call._checkpoint
            if checkpoint is not None:
                call._entered = output.status
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
            going_to, waiting = route(call.state, nodes, output.to)
            if waiting is not None and going_to != waiting and call._checkpoint is None:
                call._checkpoint = Checkpoint(
                    waiting,
                    deepcopy(call.messages),
                    frozenset(s.id for s in call.all_spoken),
                    call.heard,
                    call.turn,
                )
            call._entered = 'handoff'
            call._return_status = (
                f'Application handoff: {call.state.agent} -> {going_to}. '
                f'You are now {going_to}; handle the caller request here. '
                'The previous handoff finished routing, not the caller request.'
            )
        logger.info('handoff {} -> {} ({})', call.state.agent, going_to, output.because or '-')
        self.handoffs.append(going_to)
        call.commit(agent=going_to, pending=waiting)
        if output.status == 'handoff':
            prompt = None
        if barge.hit:
            return
        if (
            output.ends_turn
            and output.reply
            and nodes[going_to].opening(call) is None
            and not call.claims_completion(output.reply)
        ):
            call._return_status, call._entered = '', ''
            await call.speak_now(output.reply, origin='switch')
            return
        if lines := nodes[going_to].enter:
            await call.say(pick(f'enter:{output.to}', lines), origin='entry')
        yield _Continue(prompt)

    async def _speak(
        self, node: Node, prompt: str | None, hold: _Hold | None = None
    ) -> AsyncIterator[object]:
        """Run one step's agent, streaming whatever it says.

        With `hold`, this is an early run (`_Early`): it stops at its first
        event until the readings have decided nothing, and a dropped one keeps
        nothing. The barge signal is attached only once it goes on, because
        until then the signal belongs to `advance`.

        `Node.outputs` builds the run's `output_type` from the live state; the
        per-request narrowing that keeps it honest across several requests
        inside one run is a capability on the agent itself. An output set that
        is the agent's own is not passed again: Pydantic AI rebuilds a run's
        output schema whenever one is passed, which cost about 5 ms of every
        hand-over turn in the N12 measurement (session 2).

        On a step that can write, speech is released a clause at a time so
        each clause can be read before the caller hears it, and a clause that
        claims a change nothing has made is not said at all. The gate is off
        once the step has written something, because the confirmation is then
        the business's own words, and off entirely on a step that cannot write.

        A `reset` step is asked the caller's own question outright and sent no
        conversation: what it needs is projected into its instructions, and
        what it does not need it cannot leak. An ordinary step gets the
        caller's turn on the first hop and `None` afterwards, because by then
        it is in the history. Every request's messages are projected through
        `history.project` before they go out, and any application status is
        appended as an instruction.

        The run spends the turn's own tally, not one of its own, and `retries`
        is semantic repair only: a provider's HTTP retries are a different
        layer, and folding them together would let a flaky network buy extra
        attempts at getting a tool call right.

        Which tools *succeeded* is how a free edge is taken: the result, never
        the call, because the call event fires before the tool runs.

        Messages are retained across cancellation from the SDK's own
        `new_messages()` boundary after teardown, never by slicing at the
        original history length: SDK normalisation can merge old requests, and
        a cancelled run still heard the caller and may have completed a tool.

        A run with no prompt whose history does not end on a request is sent
        the caller's words again. The SDK would otherwise send an empty
        request, which `project` drops, and the provider refuses a history
        that ends on its own reply. It happened after a stalled request was
        retried (`TurnBudget.first_part`): 3 turns in 24 replayed calls of
        2026-09-25 ended in the stumble line. A repeat of the turn's own words
        is dropped from later requests (`core.history.project`).
        """
        call, barge = self.call, self.barge
        agent: Agent = node.agent
        output_type = node.outputs(call.state) if node.outputs is not None else None
        if output_type is agent.output_type:
            output_type = None
        step = call.state.agent
        succeeded: set[str] = set()
        output = None
        spoke = False
        gate: str | None = 'clause' if node.guarded else None
        held = ''

        call.attempt_started()
        caller_turn = call.turn
        run_id = f'{caller_turn}:{call.attempt}'
        accepted_prompt = call.heard if node.history == 'reset' else prompt
        requested = 0
        streamed = 0
        excluded = frozenset(s.id for s in call.all_spoken) if node.history == 'reset' else frozenset()
        initial = [] if node.history == 'reset' else paired(project(call, call.messages))
        if accepted_prompt is None and call.heard and not (initial and isinstance(initial[-1], ModelRequest)):
            accepted_prompt = call.heard

        stall: asyncio.TimerHandle | None = None
        stalled = False

        def give_up() -> None:
            nonlocal stalled
            stalled = True
            logger.warning(
                'step request {}:{} sent nothing in {:g}s', run_id, requested, self.budget.first_part
            )
            if events is not None:
                events.cancel()

        def disarm() -> None:
            nonlocal stall
            if stall is not None:
                stall.cancel()
                stall = None

        async def prepare(ctx, request):
            nonlocal requested, stall
            requested += 1
            call._response = f'{run_id}:{requested}'
            if self._step_requests:
                await self._hold()
            self._step_requests += 1
            disarm()
            stall = asyncio.get_running_loop().call_later(self.budget.first_part, give_up)
            messages = project(call, request.messages, excluded=excluded)
            parameters = request.model_request_parameters
            notes = [
                note for note in (call.today.instructions(), call.spoken_over, call._return_status) if note
            ]
            if notes:
                parameters = replace(
                    parameters,
                    instruction_parts=[
                        *(parameters.instruction_parts or []),
                        *(InstructionPart(content=note) for note in notes),
                    ],
                )
            return replace(request, messages=messages, model_request_parameters=parameters)

        async def tag(ctx, *, request_context, response):
            disarm()
            response.metadata = (response.metadata or {}) | {'voice_response': f'{run_id}:{requested}'}
            return response

        retained_run = False
        events = None

        def retain(new_messages):
            """Keep what this run added, once.

            A run that failed can report the tail of the history it was given
            as new; those messages are already on the call, so the overlap with
            `initial` is dropped rather than appended a second time. Compared
            by parts, because the SDK rewrites the instructions on the last
            request it continues from.
            """
            nonlocal retained_run
            retained_run = True
            if hold is not None and hold.dropped:
                return
            overlap = next(
                (
                    k
                    for k in range(min(len(new_messages), len(initial)), 0, -1)
                    if all(a.parts == b.parts for a, b in zip(new_messages[:k], initial[-k:], strict=True))
                ),
                0,
            )
            retained = deepcopy(new_messages[overlap:])
            for message in retained:
                if isinstance(message, ModelRequest) and any(
                    isinstance(p, UserPromptPart) for p in message.parts
                ):
                    message.metadata = (message.metadata or {}) | {'caller_turn': caller_turn}
                    if (
                        accepted_prompt is not None
                        and caller_turn == call.turn
                        and any(
                            isinstance(p, UserPromptPart) and p.content == accepted_prompt
                            for p in message.parts
                        )
                    ):
                        call.mark_retained()
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
                usage=self.usage,
                usage_limits=self.limits,
                retries={'tools': self.budget.repair_retries, 'output': self.budget.output_retries},
            ) as events:
                if hold is None:
                    barge.attach(events)
                async for event in events:
                    if isinstance(event, PartStartEvent):
                        disarm()
                        if event.index == 0:
                            streamed += 1
                        call._response = f'{run_id}:{streamed}'
                        match event.part:
                            case TextPart():
                                call.note_first_output('text')
                            case ToolCallPart():
                                call.note_first_output('tool')
                    if hold is not None and not hold.opened.is_set():
                        await hold.opened.wait()
                        barge.attach(events)
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
                    elif isinstance(event, ToolCallEvent):
                        self._arm_hold()
                    elif isinstance(event, ToolResultEvent):
                        self._disarm_hold()
                        if isinstance(event, FunctionToolResultEvent) and not isinstance(
                            event.part, RetryPromptPart
                        ):
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
            disarm()
            self._disarm_hold()
            if events is not None and not retained_run:
                try:
                    new_messages = events.new_messages()
                except UserError:
                    pass
                else:
                    retain(new_messages)
            barge.detach()
            if held and gate != 'blocked' and not (hold is not None and hold.dropped):
                call.note_model_text(held)
                call._spoke = True
                yield TextDelta(held, step)
            call.close_model_text()

        if gate == 'blocked':
            note_spoken(call, _delivered(call), interrupted=True)
            call.note_said('fixed', call.locale.withheld, origin='error')
            yield FixedSpeech(call.locale.withheld, reason='withheld')
        yield RunEnded(step)
        yield _Stalled() if stalled and not barge.hit else _Outcome(output, succeeded)


def run_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget,
    barge: Barge | None = None,
) -> AsyncIterator[TurnEvent]:
    """Run agents until one answers the caller, and say what happened. The function form of `Turn`."""
    return Turn(call, agents, budget, barge).events(prompt)


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


async def _say_again(call: CallContext, words: str) -> str:
    """Say an unheard receipt again, word for word: no model, no write.

    Whatever the caller said over it is kept for the next turn's model.
    Coval run BVqdGEunV3avnHrHVwq4oE: the receipt for Saturday at 9 was cut
    before any audio played, the caller went on "Yes, tomorrow at nine AM.
    That's great.", and the front desk read it as a new request and moved
    their own booking to 10. Hearing what was done comes first; a reading of
    the reply is not needed to decide that.
    """
    call.note_receipt(words)
    await call.speak_now(words, origin='confirmation')
    return words


def _silent(call: CallContext, output: object, answers_before: int) -> bool:
    """Whether an application action left the caller with nothing to hear: no question, no hand-over, no answer.

    The step's model answers then, in the same turn, told what the action did.
    A yes to a read-back is often not the whole reply ("Yes, that is me. And
    can we move my cleaning to Thursday?"), and an action that only records
    the yes would end the turn in silence and drop the rest. A covering line
    (`CallContext.say`) answers nothing, so only `speak_now` counts.
    """
    if output is None or isinstance(output, Question | Handoff):
        return False
    return call._answers == answers_before


def _covering(call: CallContext, segment: Spoken) -> bool:
    """A covering line said in this turn, such as a check's "Thanks, I have you".

    The rest of a check is hidden from the step it hands back to, whose
    history is the one from before the check. This line is not: the caller
    has just heard it, and the step answering next must not say it again.
    """
    return segment.turn == call.turn and segment.origin in _FILLERS


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
    call.note_heard(content, segment_ids=segment_ids, interrupted=interrupted)


def greet(call: CallContext, line: str | Question) -> str:
    """Record the opening line as said, before the caller's first turn, and return its words.

    A `Question` is put on the call first: a caller ID read back in the
    greeting is then the question the first reply answers, checked like any
    other (`CallContext.pending`). The line goes into the history as the
    agent's own response, so the agents know who spoke first.
    """
    if not isinstance(line, str):
        line = line.put(call)
    call.note_said('fixed', line, origin='entry')
    call.messages.append(
        ModelResponse(parts=[TextPart(content=line)], metadata={'speech_ids': [call.spoken[-1].id]})
    )
    return line


@dataclass(frozen=True)
class TextReply:
    """One caller turn over text: the words, the segments that carried them, and how the turn ended."""

    text: str
    segments: tuple[Spoken, ...]
    """This turn's speech, each with the id `CallContext.acknowledge` takes once the reader has it."""
    finished: TurnFinished


async def text_turn(
    flow: Any, call: CallContext, words: str, budget: TurnBudget | None = None, barge: Barge | None = None
) -> TextReply:
    """Run one caller turn of a compiled flow over text. Nothing is acknowledged here.

    `flow` is a `StepFlow`, or its `nodes`. The reply's segments stay
    unknown until the text runtime says they reached the reader
    (`call.acknowledge(*ids)`): a turn returning is not the words arriving.
    """
    text, finished = await take_turn(call, getattr(flow, 'nodes', flow), words, budget, barge)
    return TextReply(text, tuple(s for s in call.spoken if s.text), finished)


class Asks[C](Protocol):
    """A question a use case words for its own call type: `put` records it on that call and returns its words."""

    def put(self, call: C, /) -> str: ...


def read_out[C: CallContext](
    call: C, line: str | Asks[C], origin: str = 'ask', kind: Literal['fixed', 'model'] = 'fixed'
) -> str:
    """Say a line to a text reader who gets all of it: put it on the call if it is a question, record it, acknowledge it.

    The text runtime's whole delivery of one line, for a reader that
    receives words the moment they are written. `kind='model'` is the step's
    model wording a question the code asked without words. Returns the words.
    """
    if not isinstance(line, str):
        line = line.put(call)
    call.note_said(kind, line, origin=origin)
    call.acknowledge(call.spoken[-1].id)
    return line


async def take_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget | None = None,
    barge: Barge | None = None,
    *,
    received: bool = False,
) -> tuple[str, TurnFinished]:
    """Everything one turn said, and how it ended. For tests.

    The same engine the Pipecat processor drives, with the events collected
    instead of translated into frames, which is what makes "the same scenario,
    both ways" a comparison rather than two different programs.

    `received` says a text consumer got every word the turn said, and
    acknowledges each segment once the turn is over (`receive`). Without it
    the turn's speech stays unknown, as speech handed to a runtime that never
    reports playback is: a read-back in it cannot be agreed to.
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
    if received:
        receive(call)
    return ''.join(said), done


def receive(call: CallContext) -> tuple[str, ...]:
    """A text consumer got everything this turn has said so far: acknowledge each segment. Returns their ids.

    The text runtime's playback signal (`CallContext.acknowledge`). Call it
    when the words have actually reached the reader, never merely because a
    turn returned.
    """
    ids = tuple(s.id for s in call.spoken if s.text)
    if ids:
        call.acknowledge(*ids)
    return ids
