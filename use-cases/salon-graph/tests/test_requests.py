"""Requests per semantic turn on booking, before and after session 4f, on the real flow with the scripted provider.

The session 4e prompt had booking call `collect`, then `options`, then `wait`
for every offer; the live traces showed the request after `options` called
only `wait` in 13 of 15 cases (`docs/research/reusable-core/session-4f/booking_requests.py`),
and `wait` looks the offer up itself. The session 4f prompt records and waits
in one reply, calls `options` only for an id it cannot see, turns a no down
with `decline`, and says a receipt again with `say_again`.

Each case runs the same caller turns twice on a fresh call and diary: once
with the replies the old prompt asked for, once with the new. Where the words
should be the same, they must be, and so must the diary; where the new rule
changes them (a no, a requested repeat), the case says what changed. The table
printed at the end is the report's scripted request count.

* K1 an ordinary offer; K2 an ambiguous "yes" to two times; K3 a time taken
  by another caller between the offer and the yes; K4 the caller's own slot
  (V11); K5 book, move and cancel one booking; K6 a question instead of a yes;
  K7 a no to the read-back; K8 a cut receipt, then a request to hear it again.

Scripted models prove what the runtime allows and what it costs, never what a
real model will choose: that is the live check's job.
"""

import asyncio

from tests.script import slot, turn
from tests.test_business import FRIDAY, TUE, at_booking, seed
from tests.test_moves import OTHER, snapshot, take

OFFER_FRIDAY = {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}


def calls(*pairs) -> tuple:
    """One model response carrying these tool calls, in order."""
    return ('calls', [(name, {'values': args} if name == 'collect' else args) for name, args in pairs])


async def run(case, variant: str) -> tuple[list[list[str]], list[int], list[tuple]]:
    """Play a case's turns in one variant: what the caller heard, requests per turn, and the diary after."""
    script, flow, call, diary = await at_booking()
    said, requests = [], []
    for step in case(diary):
        if callable(step):
            step(call, diary)
            continue
        words, old, new = step
        before = len(script.requests)
        heard = not words.endswith('~')
        reply = await turn(
            flow, call, script, words.rstrip('~'), *(old if variant == 'old' else new), heard=heard
        )
        if not heard:
            call.acknowledge(call.all_spoken[-1].id, heard='')
        said.append(reply)
        requests.append(len(script.requests) - before)
    return said, requests, snapshot(diary.path)


def k1(diary):
    yield (
        'A haircut on Friday at two.',
        [('collect', OFFER_FRIDAY), ('call', 'options'), ('call', 'wait')],
        [calls(('collect', OFFER_FRIDAY), ('wait', {}))],
    )


def k2(diary):
    yield from k1(diary)
    yield 'Yes.', [('call', 'options'), ('call', 'wait')], [('call', 'wait')]


def k3(diary):
    yield from k1(diary)
    pick = [('collect', {'slot': slot(FRIDAY, '14:00')}), ('call', 'book_appointment')]
    yield 'Two, please.', pick, pick
    yield lambda call, diary: take(diary.path, OTHER, FRIDAY, '14:00')
    yield (
        'Yes.',
        [('call', 'book_appointment'), ('call', 'wait')],
        [('call', 'book_appointment'), ('call', 'wait')],
    )


def k4(diary):
    seed(diary, TUE, '09:00')
    asked = {'service': 'haircut', 'booking_day': 'Tuesday', 'booking_time': '09:00'}
    yield (
        'Tuesday at nine, yes.',
        [('collect', asked), ('call', 'options'), ('call', 'wait')],
        [calls(('collect', asked), ('wait', {}))],
    )


def booked_friday(diary):
    yield from k1(diary)
    pick = [('collect', {'slot': slot(FRIDAY, '14:00')}), ('call', 'book_appointment')]
    yield 'Two, please.', pick, pick
    yield 'Yes.', [('call', 'book_appointment')], [('call', 'book_appointment')]


def k5(diary):
    yield from booked_friday(diary)
    [held] = [b[0] for b in snapshot(diary.path)]
    moved = {'appointment': held, 'booking_day': 'Tuesday', 'booking_time': '14:00'}
    yield (
        'Move it to Tuesday at two.',
        [('call', 'options'), calls(('collect', moved), ('options', {})), ('call', 'wait')],
        [calls(('collect', moved), ('options', {})), ('call', 'wait')],
    )
    move = [('collect', {'slot': slot(TUE, '14:00')}), ('call', 'move_appointment')]
    yield 'Two is fine.', move, move
    yield 'Yes.', [('call', 'move_appointment')], [('call', 'move_appointment')]
    yield (
        'Actually, cancel it.',
        [('call', 'options'), ('collect', {'appointment': held}), ('call', 'cancel_appointment')],
        [('call', 'cancel_appointment')],
    )
    yield 'Yes.', [('call', 'cancel_appointment')], [('call', 'cancel_appointment')]


def read_back_two(diary):
    yield from k1(diary)
    pick = [('collect', {'slot': slot(FRIDAY, '14:00')}), ('call', 'book_appointment')]
    yield 'Two, please.', pick, pick


def k6(diary):
    yield from read_back_two(diary)
    yield 'Is that with Anna?', [('call', 'options'), ('call', 'wait')], [('call', 'wait')]


def k7(diary):
    yield from read_back_two(diary)
    yield 'No.', [('call', 'options'), ('call', 'wait')], [calls(('decline', {}), ('wait', {}))]


def k8(diary):
    yield from read_back_two(diary)
    yield 'Yes.~', [('call', 'book_appointment')], [('call', 'book_appointment')]
    yield 'Sorry?', [('call', 'wait')], [('call', 'wait')]
    yield 'Could you repeat that?', [('call', 'wait')], [('call', 'say_again')]


CASES = {
    'K1 ordinary offer': (k1, 'same'),
    'K2 ambiguous yes': (k2, 'same'),
    'K3 time taken before the yes': (k3, 'same'),
    'K4 own slot (V11)': (k4, 'same'),
    'K5 book, move, cancel': (k5, 'same'),
    'K6 question instead of a yes': (k6, 'same'),
    'K7 a no': (k7, 'changed'),
    'K8 cut receipt, then asked again': (k8, 'changed'),
}


async def main() -> None:
    rows = []
    for name, (case, words) in CASES.items():
        old_said, old_requests, old_diary = await run(case, 'old')
        new_said, new_requests, new_diary = await run(case, 'new')
        assert old_diary == new_diary, (name, 'the diary differs', old_diary, new_diary)
        assert sum(new_requests) <= sum(old_requests), (name, old_requests, new_requests)
        if words == 'same':
            assert old_said == new_said, (name, old_said, new_said)
        rows.append((name, old_requests, new_requests))
    k7_said = (await run(k7, 'new'))[0][-1]
    assert k7_said[0].startswith("All right, I haven't changed anything."), k7_said
    k8_old, k8_new = (await run(k8, 'old'))[0][-1], (await run(k8, 'new'))[0][-1]
    assert k8_old == [
        'You already have a haircut Friday the 2nd at 2:00 PM. Would you like a different time?'
    ], (
        'before say_again, a request to hear the receipt got an offer instead',
        k8_old,
    )
    assert k8_new == ["You're all set for Friday the 2nd at 2:00 PM. Anything else I can help with?"], k8_new
    for name, old, new in rows:
        print(f'{name:36} old {sum(old):2} {old}  new {sum(new):2} {new}')
    print('requests ok: K1-K8, the same words and diary with fewer booking requests')


if __name__ == '__main__':
    asyncio.run(main())
