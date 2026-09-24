"""A second use case on core alone: a clinic's booking line, built from the same classes as the salon.

Nothing here comes from `salon`. What the clinic writes is its information:
who is talking, its options, its read-backs and what a yes does, its backend.
The acts, their readings, the order they are tried in, consent and the
write audit are core's (`core.conversation`, `core.operations`).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage, UsageLimits

from core.conversation import Closing, Conversation, HeardValue, Offer, ReadBack, TakeBack
from core.desk import Desk
from core.graph import LocalAction, Node
from core.operations import Operation
from core.proposals import BaseProposal
from core.readings import ChoiceInput, Option
from core.state import CallContext, Handoff, Held, VoiceState
from core.steps import Prompted

TIMES = {'0900': 'Tuesday at 9:00 AM', '1000': 'Tuesday at 10:00 AM'}
"""The clinic's free times, by id."""
_PATIENT = re.compile(r'\bP-\d{4}\b')


class ClinicState(VoiceState):
    agent: str = 'desk'
    patient: str = ''
    booked: str = ''


class ClinicCall(CallContext):
    state: ClinicState = Field(default_factory=ClinicState)
    _offered: tuple[Option, ...] = PrivateAttr(default=())
    _book: dict[str, str] = PrivateAttr(default_factory=dict)
    """The clinic's appointments, by slot: its whole backend."""

    def offer(self) -> str:
        """Put the free times on the table, as a question the next reply can pick from."""
        self._offered = tuple(Option(id=slot, said=said) for slot, said in TIMES.items())
        return Prompted(about='offer', spoken='I have Tuesday at 9 or at 10. Which works for you?').put(self)

    def table(self) -> tuple[Option, ...]:
        return self._offered

    @property
    def conversation(self) -> Conversation[ClinicCall]:
        return CLINIC


class Slot(BaseModel):
    slot: str


class Receipt(BaseModel):
    slot: str
    status: str


def _agreed_to(call: ClinicCall) -> bool:
    return CLINIC.label(call, 'confirmation') == 'accept'


class Book(Operation[ClinicCall, Slot, Receipt, str]):
    """Book a slot, once the caller said yes to its read-back. Core's `_authorise` checks the question."""

    name = 'book'
    args_type = Slot
    receipt_type = Receipt
    proposal_about = 'booking'

    def proposal(self, args: Slot) -> BaseProposal:
        return BaseProposal(about='booking', value=args.slot)

    async def _agreed(self, call: ClinicCall, question: Any, *, usage: Any, usage_limits: Any) -> bool:
        return _agreed_to(call)

    async def _execute(self, call: ClinicCall, args: Slot, grant: Any) -> dict[str, str]:
        call._book[args.slot] = call.state.patient or 'walk-in'
        return {'slot': args.slot, 'status': 'booked'}

    async def _record(self, call: ClinicCall, args: Slot, receipt: Receipt) -> str:
        call.commit(booked=receipt.slot)
        told = f'Booked for {TIMES[receipt.slot]}. Anything else?'
        call.hold('undo', Held(key=receipt.slot, told=told))
        await call.speak_now(Prompted(about='closing', spoken=told).put(call), origin='confirmation')
        return told


class Cancel(Book):
    """Cancel a slot booked in this call, once the caller said yes to that."""

    name = 'cancel'
    proposal_about = 'cancel'

    def proposal(self, args: Slot) -> BaseProposal:
        return BaseProposal(about='cancel', value=args.slot)

    async def _execute(self, call: ClinicCall, args: Slot, grant: Any) -> dict[str, str]:
        call._book.pop(args.slot, None)
        return {'slot': args.slot, 'status': 'cancelled'}

    async def _record(self, call: ClinicCall, args: Slot, receipt: Receipt) -> str:
        call.commit(booked='')
        call.release('undo')
        told = 'That is cancelled. Anything else?'
        await call.speak_now(Prompted(about='closing', spoken=told).put(call), origin='confirmation')
        return told


BOOK, CANCEL = Book(), Cancel()


class Patient(BaseModel):
    patient: str


class Identify(Operation[ClinicCall, Patient, Patient, str]):
    name = 'identify'
    args_type = Patient
    receipt_type = Patient
    proposal_about = 'patient'

    def proposal(self, args: Patient) -> BaseProposal:
        return BaseProposal(about='patient', value=args.patient)

    async def _agreed(self, call: ClinicCall, question: Any, *, usage: Any, usage_limits: Any) -> bool:
        return _agreed_to(call)

    async def _execute(self, call: ClinicCall, args: Patient, grant: Any) -> dict[str, str]:
        return {'patient': args.patient}

    async def _record(self, call: ClinicCall, args: Patient, receipt: Patient) -> str:
        call.commit(patient=receipt.patient)
        return receipt.patient


IDENTIFY = Identify()


async def _say(call: ClinicCall, prompted: Prompted) -> Prompted:
    return prompted


def _read_back_time(call: ClinicCall, slot: str) -> LocalAction:
    words = f'Shall I book you in for {TIMES[slot]}?'
    return LocalAction('ask_booking', lambda: _say(call, Prompted(about='booking', spoken=words, value=slot)))


def _offer_undo(call: ClinicCall, held: Held) -> LocalAction:
    words = f'Shall I cancel {TIMES[held.key]}?'
    return LocalAction(
        'ask_cancel', lambda: _say(call, Prompted(about='cancel', spoken=words, value=held.key))
    )


async def _read_back_patient(call: ClinicCall, patient: str) -> Prompted:
    return Prompted(about='patient', spoken=f'Is your patient number {patient}?', value=patient)


DESK = Desk[ClinicCall](
    call_type=ClinicCall, handoff=Handoff, actions=frozenset({'book', 'cancel', 'identify', 'hang_up'})
)
"""Core's shared actions, given the clinic's call and actions: its goodbye is the salon's code."""


def _patient_number(words: str) -> str:
    found = _PATIENT.findall(words)
    return found[-1] if found else ''


def conversation(model: Model | str) -> Conversation[ClinicCall]:
    """The clinic, as information given to core's acts."""
    return Conversation[ClinicCall](
        context='A patient is on the phone with the front desk of Riverside Clinic.',
        model=model,
        read_back_context='The front desk read something back and is waiting for a yes.',
        read_backs=(
            ReadBack(about='booking', action='book', run=lambda call, q: BOOK.run(call, Slot(slot=q.value))),
            ReadBack(
                about='cancel', action='cancel', run=lambda call, q: CANCEL.run(call, Slot(slot=q.value))
            ),
            ReadBack(
                about='patient',
                action='identify',
                run=lambda call, q: IDENTIFY.run(call, Patient(patient=q.value)),
            ),
        ),
        offers=(
            Offer[ClinicCall, ChoiceInput](
                name='chosen_time',
                about='offer',
                options=ClinicCall.table,
                input_type=ChoiceInput,
                context='The patient was offered appointment times.',
                on_pick=_read_back_time,
            ),
        ),
        values=(
            HeardValue(
                about='patient',
                action='ask_patient',
                parse=_patient_number,
                read_back=_read_back_patient,
                when=lambda call: not call.state.patient,
            ),
        ),
        take_backs=(
            TakeBack(
                name='undo_booking',
                kind='undo',
                context='Earlier in this call the clinic booked an appointment for the patient.',
                on_withdraw=_offer_undo,
            ),
        ),
        closing=Closing(goodbye=lambda call: DESK.end_conversation(call, 'Goodbye.')),
    )


CLINIC = conversation('test')
"""Built on a placeholder model name; tests swap every reading's model for a scripted one."""


def desk(agent_model: Model) -> dict[str, Node[Any]]:
    """The clinic's one step: its readings decide first, and its agent answers only what they leave."""

    async def advance(call: ClinicCall, usage: RunUsage, limits: UsageLimits) -> LocalAction | None:
        await CLINIC.read(call, usage=usage, usage_limits=limits, before_model=True)
        return CLINIC.decide(call)

    agent = Agent[ClinicCall, str](agent_model, name='desk', deps_type=ClinicCall, output_type=str)
    return {'desk': Node(agent, advance=advance)}
