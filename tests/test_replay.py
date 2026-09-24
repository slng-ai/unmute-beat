"""Every reviewed reply case, replayed offline through the application side.

The model is scripted to give each case's labelled answer, so this checks
what the application does with a *correct* interpretation: which action runs,
what gets written, and that an interrupted or stale question can never be
accepted even when the interpretation says yes. Whether a model gives the
labelled answer is `evals/consent.py`'s question, and costs money.
"""

import asyncio
from datetime import datetime

from pydantic_ai import RunContext

from evals.cases import CASES, Case, Meaning, by_group
from evals.consent import classifier_meaning, label_of, reading_for
from salon import agents, diary
from salon.models.consent import Asked
from salon.models.state import SalonCall, open_call
from salon.tools.asking import Ask
from salon.tools.complaints import ask_complaint
from tests.classifier_model import scripted

PHONE = '+34600111222'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)
WRITES = {'save_booking', 'record_complaint'}


def context(call):
    return RunContext(deps=call, model=None, usage=None)  # ty: ignore[invalid-argument-type]


def verified(agent: str) -> tuple[SalonCall, diary.Diary]:
    books = diary.Diary()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books)
    call.commit(agent=agent, customer_verified='existing', verified_phone=PHONE)
    return call, books


async def pending(case: Case, answered: bool = True) -> SalonCall:
    """A call whose last turn put this case's question to the caller, and, if `answered`, this turn's reply."""
    if case.about == 'phone':
        call = open_call(PHONE)
        call.commit(agent='verify')
        Ask(about='phone', value=PHONE, spoken=case.spoken).put(call)
    elif case.about == 'booking':
        call, books = verified('booking')
        slot = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
        call.ask(
            Asked(about='booking', value=slot['slot_id'], action='book', owner=PHONE, spoken=case.spoken)
        )
    elif case.about == 'move_or_add':
        call, books = verified('booking')
        first = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
        held = books.save_booking(PHONE, 'book', first['slot_id'], '', False, CLOCK, offered_to=call.id)
        second = books.find_slots(PHONE, 'Saturday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
        call.ask(
            Asked(
                about='move_or_add',
                value=second['slot_id'],
                action='move',
                booking_id=held['booking_id'],
                owner=PHONE,
                spoken=case.spoken,
            )
        )
    elif case.about == 'complaint':
        call, _ = verified('complaints')
        call.turn_started('My haircut was uneven. I want a refund.')
        question = await ask_complaint(context(call), 'My haircut was uneven', 'I want a refund')
        question.model_copy(update={'spoken': case.spoken}).put(call)
    else:
        call, _ = verified('concierge')
        call.ask(Asked(about='closing', spoken=case.spoken, owner=PHONE))
    call.note_said('fixed', case.spoken, origin='ask')
    if not case.delivered:
        call.note_heard(case.spoken[:12])
    if case.stale:
        call.turn_started('One moment.')
    if answered:
        call.turn_started(case.utterance)
    return call


def expected_action(case: Case, meaning: Meaning) -> str | None:
    """The application action a correct interpretation should lead to, or None for conversation."""
    if not case.delivered or case.stale:
        return None
    if meaning == 'end':
        return 'hang_up'
    if meaning == 'accept':
        return {'phone': 'confirm_customer', 'booking': 'save_booking', 'complaint': 'record_complaint'}.get(
            case.about
        )
    if meaning in ('move', 'add') and case.about == 'move_or_add':
        return 'save_booking'
    return None


async def replay(case: Case, meaning: Meaning) -> tuple[str | None, SalonCall]:
    call = await pending(case)
    call._end = _ignore
    with scripted(lambda name, data: label_of(meaning, name)):
        action = await agents.advance(call)
        if action is not None:
            await action.run()
    return (None if action is None else action.name), call


async def _ignore():
    return None


def cases_are_well_formed():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids)), 'duplicate case ids'
    assert len(CASES) >= 50, len(CASES)
    assert set(by_group()) == {
        'clear',
        'conditional',
        'question',
        'correction',
        'switch',
        'interrupted',
        'stale',
        'mixed',
        'closing',
        'choice',
    }
    for case in CASES:
        if case.expect == 'correct_phone':
            assert case.digits.startswith('+34'), case.id
        label = label_of(case.legacy_meaning, reading_for(case).name) or ''
        assert classifier_meaning(case, label) == case.legacy_meaning, case.id


async def correct_interpretations_do_the_right_thing():
    for case in CASES:
        name, call = await replay(case, case.legacy_meaning)
        want = expected_action(case, case.legacy_meaning)
        assert name == want, (case.id, name, want)
        written = call.state.writes
        assert written == (1 if want in WRITES else 0), (case.id, written)
        assert bool(call.state.customer_verified) == (want == 'confirm_customer' or case.about != 'phone'), (
            case.id
        )
        assert (call.ended == 'caller') == (want == 'hang_up'), (case.id, call.ended)


async def unheard_questions_cannot_be_accepted():
    for case in CASES:
        if case.delivered and not case.stale:
            continue
        for meaning in ('accept', 'move', 'add'):
            name, call = await replay(case, meaning)
            assert name is None and not call.state.writes, (case.id, meaning, name)
            assert case.about != 'phone' or not call.state.customer_verified, case.id


async def main():
    cases_are_well_formed()
    await correct_interpretations_do_the_right_thing()
    await unheard_questions_cannot_be_accepted()
    print(f'replay ok: {len(CASES)} reviewed cases, application side only')


if __name__ == '__main__':
    asyncio.run(main())
