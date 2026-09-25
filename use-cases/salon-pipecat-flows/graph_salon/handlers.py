"""The Python behind `flow.yaml`: one Pipecat direct function per tool the config names.

`Flow(config, handlers=graph_salon.handlers)` looks each tool up here by name. A
tool returns `({..., 'next': <node>}, TRANSITION_IN_YAML)`, and the branch
table in `flow.yaml` turns `next` into a node. So code decides which move to
take, and the config decides which moves exist. A `next` of `stay` matches no
case, and the call stays where it is with the model to answer.

Every tool is wrapped by `@tool(ArgsModel)`, which is where the guarantees
run, in one order for every tool:

1. read the caller's turn from the context (`SalonCall.hear`),
2. count the call against the turn budget,
3. validate the arguments with the tool's Pydantic model,
4. refuse a second action in the same reply (`SalonCall.claim`),
5. run the tool, and turn a refusal into a result the model can use,
6. publish `say`, `question` and `facts` to `flow_manager.state` for the next node.

A listening node speaks `say` through the `say` action here, not Pipecat's
`tts_say` (see `say` for why). Any other exception becomes the fixed fault line on the node's `_listen`
twin, never an error the model is asked to explain (Langfuse trace
56e1b598). The docstring of each tool is what the model reads: rationale
lives here and on the helpers.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, get_args

from loguru import logger
from pipecat.flows import NO_RESPONSE, TRANSITION_IN_YAML, FlowManager
from pipecat.frames.frames import TTSSpeakFrame
from pydantic import BaseModel, TypeAdapter, ValidationError

from graph_salon import speech
from graph_salon.call import RefusalError, SalonCall
from graph_salon.models import (
    Answer,
    AskBooking,
    AskComplaint,
    AskNumber,
    BookingChoice,
    Choice,
    ComplaintChoice,
    EndCall,
    HandOver,
    NoArgs,
    NodeName,
    Origin,
    Outcome,
    PhoneChoice,
    ReadDiary,
    Step,
)
from graph_salon.writes import UNCLEAR, WRITES
from salon.models.receipts import ComplaintRecord, CustomerLookup, Held, Saved, Slots

ARGS: dict[str, type[BaseModel]] = {}
"""Each tool's argument model, by tool name: the schema the model is sent (`graph_salon.flow.typed`) and the validator."""

_HELD = TypeAdapter(list[Held])
_STEPS: dict[str, Step] = {name: name for name in get_args(Step)}
_LISTEN: dict[str, NodeName] = {
    'concierge': 'concierge_listen',
    'verify': 'verify_listen',
    'booking': 'booking_listen',
    'complaints': 'complaints_listen',
}


class Run(Protocol):
    """A tool's body: the call, its validated arguments, and the node it was called on."""

    __name__: str

    def __call__(self, call: SalonCall, args: Any, here: str, /) -> Awaitable[Outcome]: ...


def step(node: str) -> Step:
    """The step a node belongs to: `booking_listen` is `booking`. `confirm` belongs to none.

    Raises:
        ValueError: If the node belongs to no step.
    """
    if (base := _STEPS.get(node.removesuffix('_listen'))) is None:
        raise ValueError(f'{node} is not a step')
    return base


def listening(node: str) -> NodeName:
    """Where a fixed line is spoken from: the node's `_listen` twin, or `confirm` itself.

    It never raises: it is what the fault path calls, and a fault that
    faulted would reach the model after all.
    """
    if node == 'confirm':
        return 'confirm'
    return _LISTEN.get(node.removesuffix('_listen'), 'concierge_listen')


def _problems(error: ValidationError) -> str:
    return '; '.join(f'{".".join(map(str, e["loc"]))}: {e["msg"]}' for e in error.errors())


async def _dispatch(
    call: SalonCall, here: str, args_type: type[BaseModel], run: Run, raw: dict, action: bool
) -> Outcome:
    """The budget, then the arguments, then the one-action rule, then the tool. Nothing refused has changed the state."""
    if call.over_budget():
        return Outcome(result={'error': 'Too many steps this turn.'}, next=listening(here), say=speech.BUDGET)
    try:
        args = args_type.model_validate(raw)
    except ValidationError as error:
        return Outcome(result={'error': _problems(error)})
    try:
        if action:
            call.claim(here)
        outcome = await run(call, args, here)
    except RefusalError as error:
        return Outcome(result={'error': str(error)})
    if action:
        call.acted(here)
    return outcome


def tool(args_type: type[BaseModel], *, action: bool = True) -> Callable[[Run], Callable[..., Awaitable]]:
    """Make `run` a Pipecat direct function whose arguments are `args_type`.

    The function's own signature is `(flow_manager, **args)`: Pipecat reads a
    direct function's schema from its type hints and cannot express a
    `Literal` (`direct_function.py`, its own TODO), so the schema comes from
    `args_type` instead (`graph_salon.flow.typed`), and so does the validation.
    The name and docstring are copied by hand, not with `functools.wraps`:
    its `__wrapped__` would make Pipecat read `run`'s signature, whose first
    parameter is not `flow_manager`.

    Args:
        args_type: The tool's arguments.
        action: Whether the tool moves the caller or the business. One per reply; a read is not one.
    """

    def wrap(run: Run) -> Callable[..., Awaitable]:
        async def handler(flow_manager: FlowManager, **raw: Any) -> tuple[dict[str, Any], object]:
            call: SalonCall = flow_manager.state['call']
            here = flow_manager.current_node or ''
            try:
                call.hear(flow_manager.get_current_context())
                outcome = await _dispatch(call, here, args_type, run, raw, action)
            except Exception:
                logger.exception(f'graph salon: {run.__name__} failed')
                outcome = Outcome(result={'error': 'internal fault'}, next=listening(here), say=speech.FAULT)
            if outcome.next == 'wait':
                return outcome.result, NO_RESPONSE
            flow_manager.state.update(say=outcome.say, facts=call.facts(), question=_question(call))
            return {**outcome.result, 'next': outcome.next}, TRANSITION_IN_YAML

        handler.__name__ = handler.__qualname__ = run.__name__
        handler.__doc__ = run.__doc__
        ARGS[run.__name__] = args_type
        return handler

    return wrap


async def say(action: dict, flow_manager: FlowManager) -> None:
    """The `say` action on every listening node: speak `state['say']`, and wait for nothing.

    Pipecat's own `tts_say` queues the speech and then an
    `ActionFinishedFrame`, and holds the node change until that frame
    reaches the end of the pipeline (`pipecat/flows/actions.py`), with no
    timeout. The frame is an ordinary control frame, so a caller who cuts in
    drops it, and the node change never completes: in Langfuse trace
    2b75413c the caller cut into the read-back, the call stayed on `booking`
    without `answer`, and the model went on to invent a read-back and a
    receipt. A custom action is not waited for, so this queues the speech
    and returns.

    ponytail: back to `tts_say` once Pipecat keeps `ActionFinishedFrame`
    through an interruption, or bounds that wait.
    """
    if text := flow_manager.state.get('say', ''):
        await flow_manager.worker.queue_frame(TTSSpeakFrame(text))


def _question(call: SalonCall) -> str:
    proposal = call.state.proposal
    return proposal.question if proposal else ''


def ask(call: SalonCall, choice: Choice, question: str, origin: Origin) -> Outcome:
    """Put a question on the table and go to `confirm`, which speaks it and waits for the caller."""
    call.propose(choice, question, origin)
    return Outcome(result={'asked': question}, next='confirm', say=question)


def entering(call: SalonCall, destination: Step) -> Outcome:
    """Go where the caller asked, through verification first when the destination needs an identity.

    The destination is kept as `resume`, so the lookup's yes goes straight
    back to it with no model routing. A carrier number is read back at once,
    by code, with no model request. With none, code asks for the number,
    and the model first runs when the caller gives it.
    """
    if destination in ('booking', 'complaints') and not call.verified:
        call.commit(resume=destination)
        if caller := call.state.caller_id:
            call.commit(caller_id='')
            return ask(call, PhoneChoice(phone=caller), speech.number_question(caller), 'verify')
        return Outcome(next='verify_listen', say=speech.ASK_NUMBER)
    if destination == 'verify':
        return Outcome(next='verify_listen', say=speech.ASK_NUMBER)
    return Outcome(next=destination)


@tool(HandOver)
async def hand_over(call: SalonCall, args: HandOver, here: str) -> Outcome:
    """Send the caller to the step that handles their request."""
    if args.to == step(here):
        raise RefusalError(f'Nothing was done: this is already {args.to}.')
    if args.to == 'verify':
        call.commit(phone='', resume=step(here) if step(here) in ('booking', 'complaints') else None)
    return entering(call, args.to)


@tool(EndCall)
async def end_call(call: SalonCall, args: EndCall, here: str) -> Outcome:
    """End the call once the caller has said they are finished."""
    return Outcome(next='end', say=args.goodbye)


@tool(AskNumber)
async def ask_number(call: SalonCall, args: AskNumber, here: str) -> Outcome:
    """Read a number back to the caller and wait for their answer."""
    return ask(call, PhoneChoice(phone=args.phone), speech.number_question(args.phone), 'verify')


@tool(NoArgs)
async def stop_verifying(call: SalonCall, args: NoArgs, here: str) -> Outcome:
    """Stop asking for a number: the caller declined or moved on."""
    call.commit(resume=None)
    return Outcome(next='concierge')


def _read(call: SalonCall, args: ReadDiary) -> dict[str, Any]:
    """The diary's answer for the verified caller, validated into `Slots` before a field is read.

    Raises:
        RefusalError: If nobody is identified, or the diary's answer does not validate.
    """
    state, diary = call.state, call.diary
    if not state.phone:
        raise RefusalError('Nothing was read: the caller is not identified.')
    raw = diary.find_slots(
        state.phone, args.day, args.service, diary.now(), offered_to=call.id, prefer=args.prefer
    )
    try:
        return Slots.model_validate(raw).model_dump(mode='json')
    except ValidationError as error:
        raise RefusalError(
            'The diary could not be read just now. Tell the caller and offer to try again.'
        ) from error


@tool(ReadDiary, action=False)
async def read_diary(call: SalonCall, args: ReadDiary, here: str) -> Outcome:
    """The caller's bookings and the free times on a day."""
    if step(here) != 'concierge':
        return Outcome(result=_read(call, args))
    if not call.verified:
        call.commit(pending_read=args)
        return entering(call, 'booking')
    return Outcome(result=_read(call, args), next='booking')


@tool(AskBooking)
async def ask_booking(call: SalonCall, args: AskBooking, here: str) -> Outcome:
    """Read back a booking, a move or a cancellation and wait for a yes."""
    phone, diary = call.state.phone, call.diary
    if not phone:
        raise RefusalError('Nothing was asked: the caller is not identified.')
    held = _HELD.validate_python(diary.bookings_for(phone, diary.now().date()))
    mine = next((booking for booking in held if booking.booking_id == args.booking_id), None)
    if args.action in ('move', 'cancel') and mine is None:
        they_hold = [h.model_dump(mode='json') for h in held] or 'nothing'
        raise RefusalError(
            f'Nothing was asked: the caller holds no booking {args.booking_id!r}. They hold: {they_hold}.'
        )
    if args.action == 'cancel':
        assert mine is not None
        question = speech.booking_question('cancel', mine.service, mine.spoken)
    else:
        offered = diary.offer_of(call.id, args.slot_id, phone)
        if not offered:
            raise RefusalError(
                'Nothing was asked: that slot is not one this caller is being offered. '
                'Read the diary again and offer a time it returned.'
            )
        if args.action == 'book' and held and not args.additional:
            raise RefusalError(
                f'Nothing was asked: the caller already holds {", ".join(h.spoken for h in held)}. '
                'Ask whether to move it or add a second one.'
            )
        question = speech.booking_question(args.action, offered['service'], offered['spoken'])
    return ask(call, BookingChoice(**args.model_dump()), question, 'booking')


@tool(AskComplaint)
async def ask_complaint(call: SalonCall, args: AskComplaint, here: str) -> Outcome:
    """Read the complaint back and wait for a yes."""
    if not call.verified:
        raise RefusalError('Nothing was asked: the caller is not identified.')
    choice = ComplaintChoice(summary=args.summary, requested_resolution=args.requested_resolution)
    return ask(call, choice, speech.complaint_question(args.summary, args.requested_resolution), 'complaints')


@tool(Answer)
async def answer(call: SalonCall, args: Answer, here: str) -> Outcome:
    """Record the caller's answer to the question."""
    proposal = call.state.proposal
    if proposal is None:
        return Outcome(result={'error': 'No question is waiting.'}, next='wait')
    if args.reply != 'yes':
        call.drop_proposal()
        return Outcome(result={'reply': args.reply}, next=proposal.origin)
    try:
        receipt = WRITES[proposal.choice.kind].run(call)
    except RefusalError as error:
        return Outcome(result={'error': str(error)}, next=proposal.origin)
    return landed(call, receipt, proposal.choice)


def landed(call: SalonCall, receipt: BaseModel, choice: Choice) -> Outcome:
    """Where a write that landed leads. A lookup resumes the waiting request; a write's receipt is spoken by code.

    A diary read the concierge was asked for before anybody was identified
    is run here, by code, with the caller's own words it was asked with:
    the model is not run again to ask for what it already asked for.
    """
    result = receipt.model_dump(mode='json')
    match receipt, choice:
        case CustomerLookup(), _:
            resume, pending = call.state.resume or 'concierge', call.state.pending_read
            call.commit(resume=None, pending_read=None)
            if pending is not None:
                try:
                    result['diary'] = _read(call, pending)
                except RefusalError as error:
                    result['diary'] = {'error': str(error)}
            return Outcome(result=result, next=resume)
        case Saved(), _:
            return Outcome(result=result, next='booking_listen', say=speech.saved(receipt))
        case ComplaintRecord(), ComplaintChoice():
            said = speech.acknowledgement(receipt.reference, choice.requested_resolution)
            return Outcome(result=result, next='complaints_listen', say=said)
    return Outcome(result={'error': UNCLEAR}, next='concierge_listen', say=speech.FAULT)
