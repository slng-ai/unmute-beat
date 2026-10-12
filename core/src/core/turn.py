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
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from loguru import logger
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    ModelRetry,
    PartDeltaEvent,
    PartStartEvent,
    RunCancelled,
    TextPart,
    TextPartDelta,
    ToolCallEvent,
    ToolCallPart,
    ToolCallPartDelta,
    ToolResultEvent,
    UserError,
)
from pydantic_ai.capabilities.hooks import Hooks
from pydantic_ai.messages import (
    InstructionPart,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    UserPromptPart,
)
from pydantic_ai.usage import UsageLimitExceeded

from core import events as ev
from core.flow import Answered, Flow, Moved
from core.graph import LocalAction, Node, Restated, route
from core.history import History, paired, project
from core.locale import ENGLISH
from core.policies import TurnBudget, pick
from core.state import CallContext, Checkpoint, Handoff, Question, Spoken, turn_generation

STUMBLE, EXHAUSTED = ENGLISH.stumble, ENGLISH.exhausted
"""The English fixed lines, by their old names. What a call hears is its own locale's (`CallContext.locale`)."""


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
    reason: Literal['error', 'exhausted']


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


HOLD_CANCEL_SECONDS = 1.0
"""How long a cancelled holding-line timer may take to finish: a line mid-`say` completes, nothing more."""


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
        self._holding: asyncio.Task[None] | None = None
        """The holding-line timer, armed only while a tool or an application action is running."""
        self._channel: asyncio.Queue[tuple[object, asyncio.Future[None] | None]] = asyncio.Queue()

    def _stale(self) -> bool:
        """Whether this turn may no longer decide where the caller is."""
        return self.barge.hit or not self.call.is_current(self.generation)

    async def _hold(self) -> None:
        """Say one holding line (`Locale.holding`), only while nothing has been said in this turn.

        Said on the `TurnBudget.hold_after` timer, armed only while a tool or
        an application action is running: work the caller is waiting on that
        has lasted. Never because a request is the turn's second: a count of
        requests is not time waited, and a fast tool followed by a fast
        answer put "One moment" in front of an answer that was about to play
        (session 4e; it had been said on every second request since
        2026-09-25). A model slow to answer is the provider's stall, handled
        on its own (`TurnBudget.first_part`), not a line to fill.

        Not when a tool starts: a tool that speaks its own line would get the
        holding line right in front of it.

        Once per caller turn, and never after the answer started: the check is
        on the turn's own speech ledger, which records model text before it
        is passed on, and on every piece of the caller turn. A turn continued
        over pieces gets a new `Turn` and an empty ledger per piece, and said
        the line again on each (4 caller turns of 20 in Coval run StET3kNN).
        A line never played, blanked when the caller went on, does not count.
        Origin `tool_filler`, so a caller who cuts in over it is still in the
        same turn (`CallContext.continues_turn`). Never once the call is
        ending: a goodbye is the last thing the caller hears.
        """
        call = self.call
        piece = f':{call.turn}:'
        held = any(s.origin == 'tool_filler' and s.text and piece in s.id for s in call.all_spoken)
        if self._stale() or call.ended or held or any(s.text for s in call.spoken) or not call.locale.holding:
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

    async def _disarm_hold(self) -> None:
        """The work finished, or the turn did: a holding line not yet said is never said.

        Cancelled and awaited, so no timer from this turn is still pending when
        the next one starts, the call ends, or the task running the turn is
        torn down. The wait is bounded: a holding line already being said
        finishes its one `say`, nothing longer.
        """
        task, self._holding = self._holding, None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=HOLD_CANCEL_SECONDS)

    async def events(self, prompt: str | None) -> AsyncIterator[TurnEvent]:
        """Run the turn, yielding what a runtime needs to voice it, and finish with how it ended.

        An exhausted turn is told so in one fixed line, with no further model
        request: there used to be one more run here, outside the budget that
        had just been declared spent, so the budget bounded nothing.

        No word list checks model prose here. What was done is said by code,
        from the receipt; a model's own words claiming a change nothing made
        are an evaluation failure, not something a pattern could catch.
        """
        call, barge = self.call, self.barge
        self.generation = call.begin_turn()
        turn_generation.set((call.id, self.generation))
        call._covered = False
        call.turn_started(prompt or '')
        if call.continuing and call._turn_usage is not None and call._turn_usage[0] == call.turn:
            self.usage = call._turn_usage[1]
            _, self.limits = self.budget.account(len(call._pieces))
        call._turn_usage = (call.turn, self.usage)
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
            await self._disarm_hold()

        if failure:
            call.note_said('fixed', call.locale.stumble, origin='error')
            yield FixedSpeech(call.locale.stumble, reason='error')
        elif spent and not barge.hit:
            logger.warning('turn exhausted: {}', spent)
            call.note_said('fixed', call.locale.exhausted, origin='error')
            yield FixedSpeech(call.locale.exhausted, reason='exhausted')

        call.turn_ended(interrupted=barge.hit)
        status: Literal['answered', 'interrupted', 'exhausted', 'failed'] = 'answered'
        if failure:
            status = 'failed'
        elif barge.hit:
            status = 'interrupted'
        elif spent:
            status = 'exhausted'
        if call.events is not None:
            call.emit(
                ev.TurnFinished,
                status=status,
                step=call.state.agent,
                handoffs=list(self.handoffs),
                requests=self.usage.requests,
                detail=failure or spent,
            )
            call.turn_snapshot()
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

        Every handoff is fenced on the turn's generation and the barge
        signal: a superseded or interrupted turn does not get to choose who
        takes the caller next, though a tool's own effect stands and is
        reconciled by its intent.

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

        if self._stale():
            return
        retained_before = call.input_retained
        action = None
        if self._hops_run == 1 and (settle := call.settlement()) is not None:
            action = LocalAction('reconcile', settle)
        elif self._hops_run == 1 and (told := call.unheard_receipt()):
            action = LocalAction('receipt', lambda: _say_again(call, told), answers=False)
        if self._stale():
            return
        if not retained_before and call.input_retained:
            prompt = None
        if action is None:
            action = node.opening(call)
        if action is not None:
            if self._stale():
                return
            if (
                self.limits.tool_calls_limit is not None
                and self.usage.tool_calls >= self.limits.tool_calls_limit
            ):
                yield _Exhausted(f'the {self.limits.tool_calls_limit}-tool budget')
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
            answers_before = call._answers
            self._arm_hold()
            try:
                output = await action.run()
                if not action.answers:
                    call._return_status = (
                        f'{call._return_status} Application action {action.name} done: the caller spoke over '
                        f'what was done, and it was said again: "{output}". Read what else they said.'
                    ).lstrip()
                    output = None
                elif _silent(call, output, answers_before):
                    call._return_status = (
                        f'{call._return_status} Application action {action.name} done.'.lstrip()
                    )
                    output = None
            except ModelRetry as error:
                call.retain_action(action.name, {'refused': str(error)})
                call._return_status = (
                    f'{call._return_status} Application action {action.name} refused: {error}'.lstrip()
                )
            except Exception as error:
                logger.exception('application action failed: {}', action.name)
                yield _Failed(f'{type(error).__name__}: {error}')
                return
            finally:
                await self._disarm_hold()
            yield RunEnded(node.agent.name or call.state.agent)
            if output is None and self._stale():
                return
        if action is None or output is None:
            failed: _Failed | None = None
            run = self._speak(node, prompt)
            while True:
                stalled = False
                async for event in run:
                    if failed is not None:
                        continue
                    if isinstance(event, _Outcome):
                        output = event.output
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
        while isinstance(output, Restated):
            if barge.hit:
                return
            about = output.about
            try:
                output = await output.run(self.usage, self.limits)
            except ModelRetry as error:
                call.retain_action(about, {'refused': str(error)})
                call._return_status = f'Application action {about} refused: {error}'
                output = None
                async for event in self._speak(node, None):
                    if isinstance(event, _Outcome):
                        output = event.output
                    elif isinstance(event, _Failed):
                        yield event
                        return
                    elif not isinstance(event, _Stalled):
                        yield event
                call._return_status, call._entered = '', ''
        if isinstance(output, Question) and not barge.hit:
            await call.speak_now(output.put(call), origin='ask')
        if not isinstance(output, Handoff):
            return
        if self._stale():
            logger.info('fenced a handoff from turn {}', self.generation)
            return
        if output.status == 'returned':
            going_to = call.state.consulting or call.state.agent
            logger.info('consultation {} -> {} done', call.state.agent, going_to)
            self.handoffs.append(going_to)
            specialist = call.state.agent
            call._via = 'handback'
            call.commit(agent=going_to, consulting=None)
            call._return_status, call._entered = '', ''
            if not output.ends_turn and not barge.hit:
                call._detour_question = None
                call._entered = 'returned'
                call._return_status = (
                    f'Application consultation return: {specialist} answered the caller and handed back to '
                    f'you, {going_to}. It noted: "{output.because}". That is its reading; the caller\'s own '
                    'words are above. Carry on with what they asked of you.'
                )
                yield _Continue(None)
                return
            if not barge.hit and (words := call.reissue_waiting()):
                await call.speak_now(words, origin='ask')
            return
        consulting = call.state.consulting
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
        elif output.to in node.consults:
            going_to, waiting, consulting = output.to, call.state.pending, call.state.agent
            call._arrived[going_to] = call.turn
            call._detour_question = call._proposals.asked
            call._entered = 'handoff'
            call._return_status = (
                f'Application consultation: {call.state.agent} asked {going_to}. You are now {going_to}: '
                'answer the caller, ask them for anything you need, and call done once you have answered.'
            )
        else:
            going_to, waiting = route(call.state, nodes, output.to)
            consulting = None
            call._arrived[going_to] = call.turn
            if waiting is not None and going_to != waiting and call._checkpoint is None:
                call._checkpoint = Checkpoint(waiting, call.heard)
                call._arrived[waiting] = call.turn
            call._entered = 'handoff'
            call._return_status = (
                f'Application handoff: {call.state.agent} -> {going_to}. '
                f'You are now {going_to}; handle the caller request here. '
                'The previous handoff finished routing, not the caller request.'
            )
        logger.info('handoff {} -> {} ({})', call.state.agent, going_to, output.because or '-')
        self.handoffs.append(going_to)
        call._via = (
            'consult' if consulting == call.state.agent else 'exit' if output.status == 'handoff' else ''
        )
        call.commit(agent=going_to, pending=waiting, consulting=consulting)
        if output.status == 'handoff':
            prompt = None
        if barge.hit:
            return
        if output.ends_turn and nodes[going_to].opening(call) is None:
            call._return_status, call._entered = '', ''
            if output.reply:
                await call.speak_now(output.reply, origin='switch')
            return
        yield _Continue(prompt)

    async def _speak(self, node: Node, prompt: str | None) -> AsyncIterator[object]:
        """Run one step's agent, streaming whatever it says.

        The run's output set is the agent's own, never passed again: Pydantic
        AI rebuilds a run's output schema whenever one is passed, which cost
        about 5 ms of every hand-over turn in the N12 measurement (session 2).
        The per-request narrowing of `hand_over` is a capability on the agent.

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

        A conversation step speaks only through its outputs, never provider
        text (`Node.model_speech`); an action or end step's agent may answer in
        words, which go out as they stream.

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
        step = call.state.agent
        output = None

        call.attempt_started()
        caller_turn = call.turn
        run_id = f'{caller_turn}:{call.attempt}'
        accepted_prompt = call.heard if node.history == 'none' else prompt
        requested = 0
        streamed = 0
        excluded = frozenset(s.id for s in call.all_spoken) if node.history == 'none' else frozenset()
        initial = [] if node.history == 'none' else paired(project(call, call.messages))
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

        asked_at = 0.0
        first_at: float | None = None

        async def prepare(ctx, request):
            nonlocal requested, stall, asked_at, first_at
            requested += 1
            asked_at, first_at = time.perf_counter(), None
            call.emit(
                ev.ModelRequest,
                step=step,
                request=self.usage.requests + 1,
                budget=self.limits.request_limit or 0,
            )
            call._response = f'{run_id}:{requested}'
            disarm()
            stall = asyncio.get_running_loop().call_later(self.budget.first_part, give_up)
            messages = project(call, request.messages, excluded=excluded)
            if isinstance(node.history, History):
                messages = node.history.select(
                    call,
                    messages,
                    step=step,
                    loaded=node.capabilities,
                    owned=node.scope.operations if node.scope is not None else (),
                )
            parameters = request.model_request_parameters
            notes = [
                note
                for note in (
                    call.today.instructions() if call._today_shown else '',
                    '' if node.scope is not None and node.history == 'none' else call.spoken_over,
                    call._return_status,
                )
                if note
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
            if call.events is not None:
                now = time.perf_counter()
                call.emit(
                    ev.ModelEnded,
                    step=step,
                    model=response.model_name or '',
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    ms=round((now - asked_at) * 1000, 1),
                    first_ms=None if first_at is None else round((first_at - asked_at) * 1000, 1),
                )
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
            overlap = next(
                (
                    k
                    for k in range(min(len(new_messages), len(initial)), 0, -1)
                    if all(a.parts == b.parts for a, b in zip(new_messages[:k], initial[-k:], strict=True))
                ),
                0,
            )
            retained = deepcopy(new_messages[overlap:])
            served = {'for': call.state.consulting} if call.state.consulting else {}
            for message in retained:
                message.metadata = (message.metadata or {}) | {'step': step, 'run': run_id} | served
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
            if call.events is not None:
                _decisions(call, step, retained)

        yield RunStarted(step)
        try:
            async with agent.run_stream_events(
                accepted_prompt,
                deps=call,
                message_history=initial,
                capabilities=[Hooks(before_model_request=prepare, after_model_request=tag)],
                usage=self.usage,
                usage_limits=self.limits,
                retries={'tools': self.budget.repair_retries, 'output': self.budget.output_retries},
            ) as events:
                barge.attach(events)
                async for event in events:
                    if call.events is not None:
                        if first_at is None and isinstance(event, PartStartEvent):
                            first_at = time.perf_counter()
                        _stream(call, step, event)
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
                    if chunk := _speech(event):
                        if node.model_speech:
                            call.note_model_text(chunk)
                            call._spoke, call._covered = True, False
                            yield TextDelta(chunk, step)
                    elif isinstance(event, ToolCallEvent):
                        self._arm_hold()
                    elif isinstance(event, ToolResultEvent):
                        await self._disarm_hold()
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
            await self._disarm_hold()
            if events is not None and not retained_run:
                try:
                    new_messages = events.new_messages()
                except UserError:
                    pass
                else:
                    retain(new_messages)
            barge.detach()
            call.close_model_text()

        yield RunEnded(step)
        yield _Stalled() if stalled and not barge.hit else _Outcome(output)


REMINDERS = 2
"""Reminders before the line is closed. With Pipecat's 8 s idle timer, 24 s of silence ends a call."""


async def remind(call: CallContext) -> None:
    """The caller has said nothing for a while after the last answer played: prompt them, by code.

    "Are you still there?", then the question they were asked, word for word
    and put again: a yes after the reminder answers a copy they heard in full.
    After `REMINDERS` unanswered, a goodbye and the line closes. No model
    request: nothing about silence needs reading. The count starts again
    whenever the caller speaks (`CallContext.turn_started`).
    """
    if call.ended or not call.locale.still_there:
        return
    call._reminders += 1
    if call._reminders > REMINDERS:
        call.end_requested('abandoned')
        if call.locale.idle_goodbye:
            await call.speak_now(call.locale.idle_goodbye, origin='goodbye')
        await call.end_call()
        return
    await call.speak_now(call.locale.still_there, origin='clarify')
    if (asked := call.open_question()) is not None and asked.spoken:
        call.ask(asked)
        await call.speak_now(asked.spoken, origin='ask')


def run_turn(
    call: CallContext,
    agents: dict[str, Node[Any]],
    prompt: str | None,
    budget: TurnBudget,
    barge: Barge | None = None,
) -> AsyncIterator[TurnEvent]:
    """Run agents until one answers the caller, and say what happened. The function form of `Turn`."""
    return Turn(call, agents, budget, barge).events(prompt)


def _stream(call: CallContext, step: str, event: object) -> None:
    """A streamed piece, for the listener: speech, or a tool call's arguments as the model writes them."""
    match event:
        case (
            PartStartEvent(part=TextPart(content=text))
            | PartDeltaEvent(delta=TextPartDelta(content_delta=text))
        ):
            if text:
                call.emit(ev.ModelDelta, step=step, kind='text', text=text)
        case PartStartEvent(part=ToolCallPart(tool_name=name, args=args)):
            if args:
                call.emit(
                    ev.ModelDelta,
                    step=step,
                    kind='args',
                    name=name,
                    text=args if isinstance(args, str) else json.dumps(args),
                )
            else:
                call.emit(ev.ModelDelta, step=step, kind='args', name=name, text='')
        case PartDeltaEvent(delta=ToolCallPartDelta(args_delta=delta, tool_name_delta=name)):
            if delta:
                call.emit(
                    ev.ModelDelta,
                    step=step,
                    kind='args',
                    name=name or '',
                    text=delta if isinstance(delta, str) else json.dumps(delta),
                )


def _decisions(call: CallContext, step: str, messages: list) -> None:
    """Every tool or output the model called in these messages, and every one core sent back, for the listener."""
    for message in messages:
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                args = part.args_as_dict() if part.args else {}
                call.emit(ev.ModelDecision, step=step, name=part.tool_name, args=args)
            elif isinstance(part, RetryPromptPart) and part.tool_name:
                said = (
                    part.content if isinstance(part.content, str) else json.dumps(part.content, default=str)
                )
                call.emit(ev.ModelDecision, step=step, name=part.tool_name, refused=said)


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
    """Say an unheard receipt again, word for word: no model, no write. At most `RECEIPT_REPEATS` times.

    Hearing what was done comes first, and needs no reading of the reply.
    Then the step's own agent reads what the caller said over it, in the same
    turn (`LocalAction.answers`), with the receipt before their words in its
    history: a new request is read back, and a plain agreement gets nothing
    more (`CallContext.receipt_said_again`). Session 4g, trace 28723e6b:
    "move it to Saturday the same time", said over the booking's receipt,
    was dropped here, and the caller had to say it again.

    What a reading may do with it is bounded by the operation, not by
    silence. Coval run BVqdGEunV3avnHrHVwq4oE (session 3) had "Yes, tomorrow
    at nine AM. That's great." over a cut receipt moved to 10 by a front desk
    that could write on a reading. Now a change is only a read-back: nothing
    is carried out before a heard yes to it, and the spent yes that booked
    cannot be spent again (`ProposalLedger.rewind`).
    """
    call.said_again()
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
        ModelResponse(
            parts=[TextPart(content=line)],
            metadata={'speech_ids': [call.spoken[-1].id], 'step': call.state.agent},
        )
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
