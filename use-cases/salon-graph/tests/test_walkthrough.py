"""The booking walkthrough of session 4, through the real public entry point, one boundary at a time.

    caller asks for Friday at 2 -> concierge hands to booking -> core interposes verify
    -> verify reads the number back by code, looks it up on a heard yes -> core returns to booking
    -> booking looks up times (tool result comes back to booking) -> offers, waits at booking
    -> bare "yes" to two times: booking asks which -> "the later one": read back
    -> "yes": booked once, receipt spoken by code -> "actually, I want to complain": handoff

Each step asserts which node is active, what each provider request was sent
(the requested time, the offer, the pending read-back, the receipt), and
what the diary holds. It also writes the booking node's rendered context at
the offer, the pending read-back and after the receipt to
`docs/research/reusable-core/session-4e/booking-context.md`, so the
design can be reviewed from what the agent actually sees. Session 4's and 4c's copies
stay as they were measured. A recap reference is an intent id, random per run,
so the snapshot writes it as `<ref>`.

Covers D1-D6, P4, P13, P14, P15 and P17 (scripted). Scripted models prove the
runtime's handling; whether a real model reads "the later one" right is the
live paired check's business.
"""

import asyncio
import re
from pathlib import Path

from tests.script import CLOCK, PHONE, Script, opened, slot, text_of, turn

TODAY = CLOCK.date()
FRIDAY = '2026-10-02'
SNAPSHOTS = (
    Path(__file__).parents[3] / 'docs' / 'research' / 'reusable-core' / 'session-4e' / 'booking-context.md'
)
REF = re.compile(r'\b[0-9a-f]{8}(?=: ")')
"""A recap reference in the rendered context: an intent id's first characters, different every run."""


def snapshot(script: Script, moment: str) -> str:
    """The latest booking request's instructions and tools, as a section of the snapshot file."""
    request = next(r for r in reversed(script.requests) if r.step == 'booking')
    instructions = REF.sub('<ref>', request.instructions)
    return (
        f'## {moment}\n\nActive node: `{request.step}`. Tools and outputs offered: '
        f'{", ".join(f"`{t}`" for t in request.tools)}.\n\n```text\n{instructions}\n```\n'
    )


async def the_booking_walkthrough():
    script, flow, call, diary = opened()
    sections = []

    said = await turn(
        flow,
        call,
        script,
        'Hi, can I book a haircut on Friday at two in the afternoon?',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
        ('call', 'hand_over', {'to': 'booking', 'because': 'a haircut on Friday at 2 PM'}),
    )
    assert script.steps == ['concierge', 'concierge'], script.steps
    assert call.state.agent == 'verify' and call.state.pending == 'booking', call.state
    assert said == ['Is your number +34 600 11 12 22?'], said
    facts = call.state.facts
    assert (facts.service, facts.booking_day, facts.booking_time) == ('haircut', '2026-10-02', '14:00')
    assert not facts.verified_phone, 'a number from the carrier is not an identity'

    said = await turn(
        flow,
        call,
        script,
        "Yes, that's my number.",
        ('call', 'lookup_customer'),
        ('call', 'options'),
        ('say', 'I have Friday at 2:00 or 2:30 PM. Which time works for you?'),
    )
    assert script.steps[2:] == ['verify', 'booking', 'booking'], (
        'P13: no concierge, router or reader',
        script.steps,
    )
    assert call.state.agent == 'booking' and call.state.pending is None and facts is not call.state.facts
    assert call.state.facts.verified_phone == PHONE and call.state.facts.booking_time == '14:00', 'P4: kept'
    assert said == ['Thanks, I have you.', 'I have Friday at 2:00 or 2:30 PM. Which time works for you?']
    entered = script.requests[3]
    assert 'Your role: booking' in entered.instructions and 'Your role: verify' not in entered.instructions, (
        'P15: booking enters with its own instructions, never the verification ones'
    )
    assert 'two in the afternoon' in text_of(entered.messages), 'P15: the original request is in the history'
    assert '"booking_time":"14:00"' in entered.instructions, (
        'P15: the shared state carries the requested time'
    )
    result = script.requests[4]
    assert 'Friday the 2nd at 2:00 PM' in text_of(result.messages), (
        'P13: the tool result came back to booking'
    )
    assert list(call._offered['slot'])[:2] == [slot(FRIDAY, '14:00'), slot(FRIDAY, '13:30')], call._offered
    sections.append(snapshot(script, 'At the offer: times on offer, nothing pending, waiting at booking'))
    assert diary.bookings_for(PHONE, TODAY) == []

    said = await turn(flow, call, script, 'Yes.', ('say', 'Sure. Which one, 2:00 or 2:30?'))
    assert call.state.agent == 'booking' and not call.state.facts.slot, (
        'P17: a bare yes to two times picks none'
    )
    assert diary.bookings_for(PHONE, TODAY) == [] and not call.evidence[1:], 'P17: and books none'

    later = slot(FRIDAY, '14:30')
    said = await turn(
        flow,
        call,
        script,
        'The later one, please.',
        ('collect', {'slot': later}),
        ('call', 'book_appointment'),
    )
    assert said == ['Shall I book a haircut, Friday the 2nd at 2:30 PM?'], said
    assert call.state.agent == 'booking' and diary.bookings_for(PHONE, TODAY) == [], 'D4: the pick is no yes'
    sections.append(snapshot(script, 'At the pending read-back: the pick recorded, its read-back asked'))

    said = await turn(flow, call, script, 'Yes, that works.', ('call', 'book_appointment'))
    assert said == ["You're all set for Friday the 2nd at 2:30 PM. Anything else I can help with?"], said
    [booked] = diary.bookings_for(PHONE, TODAY)
    assert (booked['date'], booked['time']) == (FRIDAY, '14:30'), booked
    [_lookup, write] = call.evidence
    assert write.operation == 'book_appointment' and write.proposal and write.segments, (
        'D5: bound to its read-back'
    )
    assert call.state.agent == 'booking' and call.state.facts.booked == booked['booking_id'], (
        'D6: booking stays'
    )

    said = await turn(flow, call, script, 'What do I owe for that?', ('say', 'A haircut is 28 euros.'))
    sections.append(snapshot(script, 'After the receipt: the booking on record, offers spent'))
    assert call.state.agent == 'booking' and 'book_appointment' in script.requests[-1].instructions

    await turn(
        flow,
        call,
        script,
        'Actually, I want to complain about my last visit.',
        ('call', 'hand_over', {'to': 'complaints', 'because': 'a complaint about a past visit'}),
        ('say', "I'm sorry something went wrong. What happened?"),
    )
    assert call.state.agent == 'complaints', 'P14: the booking agent chose the declared complaints exit'
    complaint = script.requests[-1]
    assert 'book_appointment' in complaint.instructions and booked['booking_id'] in complaint.instructions, (
        'D6: the complaint step can still see the real booking'
    )
    assert len(diary.bookings_for(PHONE, TODAY)) == 1

    SNAPSHOTS.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS.write_text(
        '# Booking context at three moments\n\n'
        'Generated by `use-cases/salon-graph/tests/test_walkthrough.py` from the provider requests of a scripted '
        'call through the real flow. Each block is exactly what the booking agent was sent as instructions: '
        'the shared voice, the booking prompt, and the application state core renders before every request.\n\n'
        + '\n'.join(sections)
    )


async def a_changed_day_offers_again_and_books_nothing_old():
    """D5: "actually Saturday" at the read-back: the old read-back cannot be spent, Saturday is looked up in booking."""
    script, flow, call, diary = opened(verified_phone=PHONE, customer_status='existing')
    call.commit(agent='booking')
    await turn(
        flow,
        call,
        script,
        'A haircut on Friday at two.',
        ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
        ('call', 'options'),
        ('say', 'Two is free on Friday. Shall I take it?'),
    )
    await turn(
        flow,
        call,
        script,
        'Two, yes.',
        ('collect', {'slot': slot(FRIDAY, '14:00')}),
        ('call', 'book_appointment'),
    )
    assert call.state.facts.slot == slot(FRIDAY, '14:00')
    said = await turn(
        flow,
        call,
        script,
        'Actually, make it Saturday.',
        ('calls', [('collect', {'values': {'booking_day': 'Saturday'}}), ('book_appointment', {})]),
        ('call', 'options'),
        ('say', 'On Saturday I have 2:00 PM. Does that work?'),
    )
    assert diary.bookings_for(PHONE, TODAY) == [], 'nothing was written under the Friday approval'
    assert call.state.facts.booking_day == '2026-10-03' and slot(FRIDAY, '14:00') not in call._offered.get(
        'slot', {}
    )
    assert any('2026-10-03' in s for s in call._offered.get('slot', {})), call._offered
    assert call.state.agent == 'booking' and said[-1].startswith('On Saturday'), said


async def a_spoken_answer_stays_and_an_invented_exit_fails():
    """P14: speech alone leaves booking active; an exit booking does not declare is refused when it runs."""
    script, flow, call, _ = opened(verified_phone=PHONE, customer_status='existing')
    call.commit(agent='booking')
    await turn(flow, call, script, 'Do you do beards?', ('say', 'We do haircuts, colour and blowouts.'))
    assert call.state.agent == 'booking'
    await turn(
        flow,
        call,
        script,
        'Put me through to billing.',
        ('call', 'hand_over', {'to': 'verify', 'because': 'invented'}),
        ('say', "I can't transfer you there."),
    )
    assert call.state.agent == 'booking', 'an exit booking does not declare moved the caller'
    assert 'options' in script.requests[0].tools and 'hand_over' in script.requests[0].tools


async def main() -> None:
    await the_booking_walkthrough()
    await a_changed_day_offers_again_and_books_nothing_old()
    await a_spoken_answer_stays_and_an_invented_exit_fails()
    print(
        'walkthrough ok: detour kept the time, same-node tool result, waiting, bare yes, read-back, one booking, handoff'
    )


if __name__ == '__main__':
    asyncio.run(main())
