"""The salon's business acceptance matrix (A-L), on the real flow, asserting the diary and the receipts.

Every case drives the public entry point with scripted node agents and a
fixed diary clock (Monday 28 September 2026). The scripted choices include
hostile ones, so a guard is shown to hold at execution, not because a model
obeyed its prompt. Letters follow session 4's matrix; P numbers its
node-agent acceptance list. What each old module asserted, and where it
lives now, is in `docs/research/reusable-core/session-4.md`.
"""

import asyncio

from core.locale import SPANISH
from salon.diary import Diary
from tests.script import CLOCK, PHONE, opened, slot, turn

TODAY = CLOCK.date()
FRIDAY, SATURDAY = '2026-10-02', '2026-10-03'
TUE = '2026-09-29'


def booked(diary: Diary) -> list[tuple[str, str, str]]:
    return [(b['service'], b['date'], b['time']) for b in diary.bookings_for(PHONE, TODAY)]


async def at_booking(**facts):
    """A verified caller already in the booking step."""
    script, flow, call, diary = opened(verified_phone=PHONE, customer_status='existing', **facts)
    call.commit(agent='booking')
    return script, flow, call, diary


async def read_back(flow, call, script, day='Friday', at='14:00', words='Friday at two.', heard=True):
    """Look up times for a haircut on `day` and read back the one at `at`."""
    date = {'Friday': FRIDAY, 'Tuesday': TUE, 'Saturday': SATURDAY}[day]
    await turn(
        flow,
        call,
        script,
        words,
        ('collect', {'service': 'haircut', 'booking_day': day, 'booking_time': at}),
        ('call', 'options'),
        ('say', 'Which time works for you?'),
    )
    return await turn(
        flow,
        call,
        script,
        'That one.',
        ('collect', {'slot': slot(date, at)}),
        ('call', 'book_appointment'),
        heard=heard,
    )


def seed(diary: Diary, day: str = FRIDAY, at: str = '09:00') -> str:
    """A booking the caller already holds, made through the diary's own offer and write."""
    diary.find_slots(PHONE, day, 'haircut', CLOCK, offered_to='seed', prefer=at)
    return diary.save_booking(PHONE, 'book', slot(day, at), '', False, CLOCK, offered_to='seed')['booking_id']


async def a_booking_before_identity_verifies_first():
    """A + P2: verification interposes with no routing request, returns to booking by code, nothing is booked yet."""
    script, flow, call, diary = opened()
    await turn(
        flow,
        call,
        script,
        'A haircut Friday at two, please.',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut'}),
    )
    assert call.state.agent == 'verify' and script.steps == ['concierge', 'concierge'], script.steps
    await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'), ('say', 'Which time suits you?'))
    assert script.steps[2:] == ['verify', 'booking'], ('the return asked no router', script.steps)
    assert call.state.agent == 'booking' and booked(diary) == []


async def a_pick_is_read_back_and_only_a_heard_yes_books():
    """B, C, I: the pick asks the exact read-back; an unheard one earns nothing; a heard yes books once."""
    script, flow, call, diary = await at_booking()
    said = await read_back(flow, call, script, heard=False)
    assert said[-1] == 'Shall I book a haircut, Friday the 2nd at 2:00 PM?' and booked(diary) == []
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [], 'a yes to a read-back nobody heard booked'
    said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], (said, booked(diary))
    assert said == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"]


async def no_a_condition_or_a_change_writes_nothing():
    """D: no, a conditional yes, and a yes to a question that is not the read-back write nothing; a change is read back fresh."""
    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    await turn(flow, call, script, 'No, not that one.', ('say', 'No problem. Which time would suit you?'))
    await turn(flow, call, script, 'Book it then.', ('call', 'book_appointment'))
    assert booked(diary) == [], 'a no, then a stale read-back, booked'

    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    said = await turn(
        flow, call, script, 'Yes, if it is under 30 euros?', ('say', 'It is 28 euros. Shall I go ahead?')
    )
    assert booked(diary) == [] and said == ['It is 28 euros. Shall I go ahead?']
    said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [] and said[-1].startswith('Shall I book'), 'the price answer was not a read-back'

    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    said = await turn(
        flow,
        call,
        script,
        'Yes, nine, actually make it half past two.',
        ('calls', [('book_appointment', {}), ('collect', {'values': {'slot': slot(FRIDAY, '14:30')}})]),
        ('call', 'book_appointment'),
    )
    assert booked(diary) == [], 'P5: booked the old time before the change'
    assert said[-1] == 'Shall I book a haircut, Friday the 2nd at 2:30 PM?', said


async def a_move_moves_the_callers_own_booking_once():
    """E: the booking they hold is moved, once, on a heard yes to the move's read-back; no second one is made."""
    script, flow, call, diary = await at_booking()
    held = seed(diary)
    said = await turn(
        flow,
        call,
        script,
        'Can you move my Friday haircut to two?',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
        ('call', 'options'),
        ('say', 'Two is free. Shall I move it?'),
    )
    said = await turn(
        flow,
        call,
        script,
        'Yes, move it to two.',
        ('collect', {'appointment': held, 'slot': slot(FRIDAY, '14:00')}),
        ('call', 'move_appointment'),
    )
    assert (
        said[-1] == 'Shall I move your haircut from Friday the 2nd at 9:00 AM to Friday the 2nd at 2:00 PM?'
    ), said
    said = await turn(flow, call, script, 'Yes please.', ('call', 'move_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], booked(diary)
    assert said == ["That's moved to Friday the 2nd at 2:00 PM. Anything else I can help with?"], said


async def a_booking_beside_one_held_is_read_back_as_a_second():
    """E: `book` for a caller who holds one is read back as a second appointment; only that yes makes two."""
    script, flow, call, diary = await at_booking()
    seed(diary)
    said = await read_back(flow, call, script)
    assert said[-1] == (
        'Shall I book you a second appointment, a haircut Friday the 2nd at 2:00 PM, as well as your haircut '
        'Friday the 2nd at 9:00 AM?'
    ), said
    await turn(flow, call, script, 'No, I meant move it.', ('say', 'Understood.'))
    assert len(booked(diary)) == 1, 'an unrelated second appointment was made'


async def a_cancellation_is_a_real_write_and_the_slot_books_again():
    """F: take-back runs the guarded cancel with its own receipt; the same slot can then be booked again."""
    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    [held] = diary.bookings_for(PHONE, TODAY)
    said = await turn(
        flow,
        call,
        script,
        'Actually, cancel that.',
        ('call', 'options'),
        ('collect', {'appointment': held['booking_id']}),
        ('call', 'cancel_appointment'),
    )
    assert said[-1] == 'Shall I cancel your haircut Friday the 2nd at 2:00 PM?' and booked(diary), said
    said = await turn(flow, call, script, 'Yes, cancel it.', ('call', 'cancel_appointment'))
    assert booked(diary) == [] and said == ["That's cancelled for you. Anything else I can help with?"], said
    assert [e.operation for e in call.evidence] == ['book_appointment', 'cancel_appointment']
    await read_back(flow, call, script, words='Friday at two again, then.')
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], (
        'the second booking of one slot vanished into the first token'
    )


async def a_complaint_is_filed_in_the_callers_words_once():
    """G: quoted words only; asking to complain is not a complaint; a changed complaint needs its own read-back."""
    script, flow, call, diary = opened()
    await turn(
        flow,
        call,
        script,
        'I want to complain.',
        ('call', 'hand_over', {'to': 'complaints', 'because': 'complaint'}),
    )
    assert call.state.agent == 'verify' and call.state.pending == 'complaints'
    await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'), ('say', 'What happened?'))
    assert call.state.agent == 'complaints'
    await turn(
        flow,
        call,
        script,
        'I would like to make a complaint please.',
        ('collect', {'complaint': 'I would like to make a complaint please'}),
        ('say', 'What happened?'),
    )
    assert call.state.facts.complaint == '', 'asking to complain was recorded as the complaint'
    await turn(
        flow,
        call,
        script,
        'The colour came out patchy and uneven. I want a partial refund.',
        ('collect', {'complaint': 'The stylist ruined my hair', 'wanted_outcome': 'a partial refund'}),
        ('say', 'Could you tell me in your words?'),
    )
    assert call.state.facts.complaint == '', "a paraphrase was recorded as the caller's words"
    said = await turn(
        flow,
        call,
        script,
        'Like I said.',
        (
            'collect',
            {'complaint': 'The colour came out patchy and uneven', 'wanted_outcome': 'a partial refund'},
        ),
        ('call', 'record_complaint'),
    )
    assert said[-1].startswith(
        'Shall I record your complaint in your words: The colour came out patchy and uneven.'
    )
    await turn(
        flow,
        call,
        script,
        'Yes, and it was uneven at the back.',
        (
            'calls',
            [('collect', {'values': {'complaint': 'it was uneven at the back'}}), ('record_complaint', {})],
        ),
        ('call', 'record_complaint'),
    )
    assert diary.complaints() == [], 'a changed complaint was filed on the old approval'
    said = await turn(flow, call, script, 'Yes.', ('call', 'record_complaint'))
    [filed] = diary.complaints()
    assert (
        filed['summary'] == 'it was uneven at the back'
        and filed['requested_resolution'] == 'a partial refund'
    )
    assert said == [
        "That's on record, reference CX0001. That one goes to the manager, who will call you back. "
        'Your requested resolution is pending approval. Anything else I can help with?'
    ], said
    await turn(flow, call, script, 'Yes.', ('call', 'record_complaint'))
    assert len(diary.complaints()) == 1, 'the complaint was written twice'


async def an_unknown_write_is_settled_before_any_model():
    """H + P8: the write lands and its answer is lost; the next turn asks the diary by the same token, no model, no second write."""
    script, flow, call, diary = await at_booking()
    real = diary.save_booking
    calls = {'saves': 0}

    def lose_first(*args, **kwargs):
        calls['saves'] += 1
        result = real(*args, **kwargs)
        if calls['saves'] == 1:
            raise TimeoutError('the diary did not answer')
        return result

    asked = {'n': 0}
    real_outcome = diary.outcome_of

    def forgetful(token):
        asked['n'] += 1
        return None if asked['n'] == 1 else real_outcome(token)

    diary.save_booking, diary.outcome_of = lose_first, forgetful
    await read_back(flow, call, script)
    said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    assert said == [
        "I couldn't confirm whether that went through, so I won't try it again until it's been checked."
    ]
    assert len(booked(diary)) == 1 and not call.evidence
    requests = len(script.requests)
    said = await turn(flow, call, script, 'Can you check whether it was booked?')
    assert len(script.requests) == requests, 'P8: a model ran before the unknown write was settled'
    assert said == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"], said
    assert (
        calls['saves'] == 1
        and len(booked(diary)) == 1
        and [e.operation for e in call.evidence] == ['book_appointment']
    )


async def a_result_about_another_slot_is_not_this_booking():
    """N1: the diary answers in legal values about another time, or with a cancellation: unknown, never a success."""
    for wrong in ({'time': '15:00', 'spoken': 'Friday the 2nd at 3:00 PM'}, {'status': 'cancelled'}):
        script, flow, call, diary = await at_booking()
        await read_back(flow, call, script)
        real = diary.save_booking

        def about_something_else(*args, _real=real, _wrong=wrong, **kwargs):
            return _real(*args, **kwargs) | _wrong

        diary.save_booking = about_something_else
        said = await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
        assert said == [call.locale.unsettled], (wrong, said)
        assert not call.evidence, (wrong, 'an answer to another question was filed as this booking')


async def a_receipt_cut_before_it_played_is_said_again_not_moved():
    """J + P7: session 3's stress call, offline. The receipt for 9:00 never played; the caller said
    "Yes, tomorrow at nine AM. That's great." The booking stays, is said again with no model, and nothing moves.
    """
    script, flow, call, diary = await at_booking()
    await turn(
        flow,
        call,
        script,
        'A haircut tomorrow at nine.',
        ('collect', {'service': 'haircut', 'booking_day': 'tomorrow', 'booking_time': '09:00'}),
        ('call', 'options'),
        ('say', 'Nine is free.'),
    )
    await turn(
        flow,
        call,
        script,
        'Nine, please.',
        ('collect', {'slot': slot(TUE, '09:00')}),
        ('call', 'book_appointment'),
    )
    script.moves = [('call', 'book_appointment')]
    import core

    reply = await core.text_turn(flow, call, 'Yes.')
    receipt = next(s for s in reply.segments if s.text.startswith("You're all set"))
    call.acknowledge(receipt.id, heard='')
    requests = len(script.requests)
    said = await turn(
        flow,
        call,
        script,
        "Yes, tomorrow at nine AM. That's great.",
        ('call', 'options'),
        ('collect', {'slot': slot(TUE, '10:00')}),
        ('call', 'book_appointment'),
    )
    assert len(script.requests) == requests and said == [receipt.generated], ('said again, no model', said)
    assert booked(diary) == [('haircut', TUE, '09:00')], booked(diary)
    assert [e.operation for e in call.evidence] == ['book_appointment']


async def an_identity_is_the_lookup_of_the_number_agreed_to():
    """Identity: digits are not identity; a lookup about somebody else verifies nobody; a correction drops identity."""
    script, flow, call, diary = opened(phone='')
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        'It is plus 34 600 111 222.',
        ('collect', {'customer_phone': PHONE}),
        ('call', 'lookup_customer'),
    )
    assert call.state.facts.customer_phone == PHONE and call.state.facts.verified_phone == ''
    real = diary.find_or_create_customer
    diary.find_or_create_customer = lambda phone: real('+34600999888')
    await turn(flow, call, script, 'Yes.', ('call', 'lookup_customer'), ('say', 'Sorry, let me check again.'))
    assert call.state.facts.verified_phone == '', 'a lookup about somebody else verified the caller'
    diary.find_or_create_customer = real

    script, flow, call, diary = opened(verified_phone=PHONE, customer_status='existing')
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        'Wait, my number is +34 600 111 333.',
        ('collect', {'customer_phone': '+34600111333'}),
        ('say', 'Let me read it back.'),
    )
    facts = call.state.facts
    assert (facts.verified_phone, facts.customer_status) == ('', ''), (
        'a corrected number kept the old identity'
    )

    script, flow, call, _ = opened(phone='')
    call.commit(agent='verify')
    await turn(
        flow,
        call,
        script,
        '600 111 222.',
        ('collect', {'customer_phone': '600111222'}),
        ('say', 'Which country?'),
    )
    assert call.state.facts.customer_phone == '', 'a number with no country code was completed for the caller'


async def a_bare_weekday_beside_another_week_is_asked_about():
    """P6: "Saturday" while Friday next week is on the table could be the 3rd or the 10th: ask, offer nothing, then the date decides."""
    script, flow, call, _ = await at_booking()
    await turn(
        flow,
        call,
        script,
        'A haircut on Friday next week.',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday next week'}),
        ('call', 'options'),
        ('say', 'On Friday the 9th I have 9:00 AM. Which time works for you?'),
    )
    assert any('2026-10-09' in s for s in call._offered['slot'])
    await turn(
        flow,
        call,
        script,
        'Saturday instead.',
        ('collect', {'booking_day': 'Saturday'}),
        ('call', 'options'),
        ('say', 'Do you mean Saturday the 3rd or Saturday the 10th?'),
    )
    result = next(
        p for m in reversed(call.messages) for p in m.parts if getattr(p, 'tool_name', '') == 'options'
    )
    assert 'Saturday the 3rd' in str(result.content) and 'Saturday the 10th' in str(result.content), (
        result.content
    )
    assert not call._offered.get('slot'), ('an ambiguous day offered times', call._offered)
    await turn(
        flow,
        call,
        script,
        'The 10th.',
        ('collect', {'booking_day': '2026-10-10'}),
        ('call', 'options'),
        ('say', 'On Saturday the 10th I have 9:00 AM.'),
    )
    assert all('2026-10-10' in s for s in call._offered['slot']), call._offered


async def model_speech_never_claims_a_completed_action():
    """Claims: on a writing step, only a receipt says something is done, in any supported language."""
    script, flow, call, diary = await at_booking()
    said = await turn(
        flow, call, script, 'Book Friday at two.', ('say', "I've booked you for Friday at two. See you then.")
    )
    assert booked(diary) == [] and "I've booked" not in ' '.join(said), said
    assert said[-1] == 'Sorry -- nothing has been changed yet. Let me check where we are.', said
    said = await turn(
        flow, call, script, 'Did it save?', ('say', 'Nothing was saved yet. Which time would you like?')
    )
    assert said == ['Nothing was saved yet. Which time would you like?'], (
        'a truthful "nothing was saved" was withheld'
    )
    await read_back(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'book_appointment'))
    call.commit(agent='complaints')
    said = await turn(flow, call, script, 'And my complaint?', ('say', 'Your complaint is saved as well.'))
    assert 'Your complaint is saved' not in ' '.join(said) and diary.complaints() == [], said
    assert call.claims_completion("I've booked you for Saturday at nine"), (
        'a claim about other arguments passed'
    )
    call._locale = SPANISH
    assert call.claims_completion('Te he reservado el sábado a las nueve.') and call.claims_completion(
        'Ya está registrada.'
    )
    assert not call.claims_completion('No se ha guardado nada.')

    script, flow, call, diary = opened(verified_phone=PHONE, customer_status='existing')
    said = await turn(
        flow,
        call,
        script,
        'A haircut on Friday morning, please.',
        ('say', "I've got you down for a haircut this Friday morning. Which time suits you?"),
    )
    assert call.state.agent == 'concierge' and "I've got you down" not in ' '.join(said), (
        'the concierge said a booking was made before anything was (Coval call b7BSSwtX)'
    )


async def a_goodbye_never_claims_a_booking_nothing_made():
    """Coval gate 1, call cYMMixAc: the caller said yes to the read-back; the model said goodbye instead.

    Its words were "Great, your haircut is booked for Friday the 2nd at
    9:00 AM. See you then!" and nothing was booked: a goodbye's words were
    spoken with no claim check. Now such a goodbye is refused back to the
    model, which then carries the booking out; after a real booking, the same
    recap is said.
    """
    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    false_goodbye = (
        'call',
        'goodbye',
        {'words': 'Great, your haircut is booked for Friday at two. See you then!'},
    )
    said = await turn(
        flow, call, script, 'Yes please, that is perfect.', false_goodbye, ('call', 'book_appointment')
    )
    assert booked(diary) == [('haircut', FRIDAY, '14:00')], booked(diary)
    assert not call.ended and 'See you then' not in ' '.join(said), said
    recap = ('call', 'goodbye', {'words': "You're booked for Friday at two. Goodbye!"})
    said = await turn(flow, call, script, "That's all, thanks.", recap)
    assert call.ended and said[-1] == "You're booked for Friday at two. Goodbye!", said


async def one_action_per_reply_and_one_budget_per_turn():
    """L: a reply with two actions runs only the first; a turn that never ends is stopped with no business work."""
    script, flow, call, diary = await at_booking()
    await read_back(flow, call, script)
    await turn(
        flow,
        call,
        script,
        'Yes.',
        (
            'calls',
            [('book_appointment', {}), ('hand_over', {'to': 'complaints', 'because': 'second action'})],
        ),
    )
    assert booked(diary) == [('haircut', FRIDAY, '14:00')] and call.state.agent == 'booking', call.state.agent

    script, flow, call, diary = await at_booking()
    loop = [('call', 'options')] * 20
    said = await turn(flow, call, script, 'Hm.', *loop)
    assert (
        said[-1] in ("Sorry, I'm going round in circles there. Could you say that again?",)
        or len(script.requests) <= 8
    )
    assert booked(diary) == []


async def main() -> None:
    await a_booking_before_identity_verifies_first()
    await a_pick_is_read_back_and_only_a_heard_yes_books()
    await no_a_condition_or_a_change_writes_nothing()
    await a_move_moves_the_callers_own_booking_once()
    await a_booking_beside_one_held_is_read_back_as_a_second()
    await a_cancellation_is_a_real_write_and_the_slot_books_again()
    await a_complaint_is_filed_in_the_callers_words_once()
    await an_unknown_write_is_settled_before_any_model()
    await a_result_about_another_slot_is_not_this_booking()
    await a_receipt_cut_before_it_played_is_said_again_not_moved()
    await an_identity_is_the_lookup_of_the_number_agreed_to()
    await a_bare_weekday_beside_another_week_is_asked_about()
    await model_speech_never_claims_a_completed_action()
    await a_goodbye_never_claims_a_booking_nothing_made()
    await one_action_per_reply_and_one_budget_per_turn()
    print('business ok: A-L on the diary, identity, dates, claims')


if __name__ == '__main__':
    asyncio.run(main())
