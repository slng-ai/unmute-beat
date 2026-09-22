"""Trace-derived recovery checks: python -m bench.continuations --repeats 3.

Real Gemini responses, no audio. Phone prefixes use a blocked FunctionModel to
make cancellation deterministic; the final readback and verification use Gemini.
"""

import argparse
import asyncio
import hashlib
import json
import re
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from pydantic_ai import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from bench.scenarios import CLOCK, PHONE, Counter
from salon import diary
from salon.agents import AGENTS
from salon.state import Appointment, open_call
from utils.arm import outcome, settings

SCENARIOS = (
    'split_phone',
    'complaint_live',
    'complaint_booked',
    'complaint_booked_dated',
    'complaint_no_remedy',
    'complaint_policy',
    'complaint_correction',
    'complaint_abandon',
)


async def scenario(name, write):
    """Drive only declared caller replies; inspect typed readbacks and business facts."""
    diary.reset()
    diary.set_clock(CLOCK)
    call = open_call('')
    call.commit(agent='concierge' if name == 'complaint_live' else 'verify')
    processor = Counter(call, AGENTS)

    async def turn(words, *, cancelled=False):
        started = time.perf_counter()
        try:
            await processor._turn(words)
        finally:
            record(words, scripted=cancelled, seconds=time.perf_counter() - started)
        if not cancelled:
            assert processor._finished and processor._finished.status == 'answered', processor._finished
        if name == 'complaint_live' and call.state.agent == 'verify':
            assert not call.complaint_approved, 'verification must precede complaint handling'
            assert call.state.pending == 'complaints', 'verification lost the complaint destination'

    def record(words, *, scripted=False, seconds):
        write(
            {
                'said': words,
                'scripted_cancellation': scripted,
                'status': processor._finished.status if processor._finished else 'missing',
                'requests': processor.requests(),
                'seconds': seconds,
                'handoffs': list(processor._finished.handoffs) if processor._finished else [],
                'state': call.state.model_dump(mode='json'),
                'speech': [{'text': s.text, 'origin': s.origin} for s in call.spoken],
            }
        )

    if name == 'split_phone':
        call.messages = [ModelRequest(parts=[UserPromptPart(s)]) for s in ('Ready.', 'One moment.')]
        for fragment in ('+34.', '600 111.'):
            ready = asyncio.Event()

            async def blocked(messages, info, ready=ready):
                ready.set()
                await asyncio.Event().wait()
                yield 'Unreachable.'

            with AGENTS['verify'].agent.override(model=FunctionModel(stream_function=blocked)):
                task = asyncio.create_task(turn(fragment, cancelled=True))
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                finally:
                    processor.interrupt()
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
        await turn('222.')
        question = call._asked
        assert question and question.about == 'phone' and question.value == PHONE, question
        for fragment in ('+34.', '600 111.', '222.'):
            assert (
                sum(
                    isinstance(p, UserPromptPart) and p.content == fragment
                    for m in call._protocol
                    for p in m.parts
                )
                == 1
            ), f'fragment missing or duplicated: {fragment}'
        await turn('Right.')
        assert call.state.verified_phone == PHONE and call.state.customer_verified
        assert call.state.writes == 0
    elif name in ('complaint_policy', 'complaint_correction', 'complaint_abandon'):
        call.commit(
            agent='complaints', customer_phone=PHONE, customer_verified='existing', verified_phone=PHONE
        )
        if name == 'complaint_policy':
            await turn('What are your refund options?')
            spoken = ' '.join(s.text for s in call.spoken).lower()
            assert 'twenty percent' in spoken and 'approval' in spoken
            assert not call.state.writes and not call._asked
        await turn('My haircut was uneven yesterday. I want a full refund.')
        assert call._asked and call._asked.about == 'complaint'
        if name == 'complaint_abandon':
            await turn('Never mind the complaint. Just tell me your opening hours.')
            assert call.state.agent == 'concierge' and not call.complaint_approved
            assert not call.state.writes and not outcome(PHONE)['complaints']
        else:
            if name == 'complaint_correction':
                await turn('No, I want a half refund instead.')
                assert call._asked and 'half refund' in call._asked.requested_resolution.lower()
                assert not call.complaint_approved and not call.state.writes
            approved = call._asked
            await turn('Yes, that is right.')
            actual = outcome(PHONE)
            assert len(actual['complaints']) == 1 and call.state.writes == 1
            complaint = actual['complaints'][0]
            assert complaint['summary'] == approved.summary
            assert complaint['requested_resolution'] == approved.requested_resolution
            assert sum(complaint['reference'] in s.generated for s in call.all_spoken) == 1
            assert not actual['bookings']
            await turn('No thanks, goodbye.')
            assert processor.ended, 'complaint goodbye did not end the call'
    elif name.startswith('complaint_booked') or name == 'complaint_no_remedy':
        call.commit(
            agent='concierge', customer_phone=PHONE, customer_verified='existing', verified_phone=PHONE
        )
        slots = diary.find_slots(PHONE, 'Saturday', 'haircut', diary.now(), offered_to=call.id)['slots']
        slot = next(s for s in slots if s['time'] == '10:00')
        saved = diary.save_booking(PHONE, 'book', slot['slot_id'], '', False, diary.now(), offered_to=call.id)
        call.commit(
            appointment=Appointment(
                **{k: saved[k] for k in Appointment.model_fields if k != 'action'}, action='book'
            )
        )
        before = outcome(PHONE)['bookings']
        call.messages = [
            ModelResponse(parts=[TextPart('Done, Saturday at 10:00 AM. Anything else?')]),
            ModelRequest(parts=[UserPromptPart('Um, actually, my last booking—uh, my last haircut—')]),
        ]
        words = (
            'Was. Completely uneven, and I would like. To understand if the booking that we just. '
            'Put into the calendar— Can be free.'
        )
        if name.endswith('_dated'):
            words += ' The uneven haircut was one week ago.'
        if name == 'complaint_no_remedy':
            words = 'Haircut. Completely uneven.'
        await turn(words)
        if name == 'complaint_no_remedy':
            speech = ' '.join(s.text for s in call.spoken).lower()
            assert not re.search(r'\b(redo|no charge|free|saturday)\b', speech), speech
            assert not call._asked or not call._asked.requested_resolution
            await turn('Can the booking we already have be free?')
        assert call.state.agent == 'complaints' and processor._finished.handoffs == (
            () if name == 'complaint_no_remedy' else ('complaints',)
        ), 'complaint routing loop'
        approved = call._asked
        assert approved and approved.about == 'complaint', 'apology/policy did not advance to readback'
        assert not call.state.writes and outcome(PHONE)['bookings'] == before
        await turn('Yes, that is right.')
        actual = outcome(PHONE)
        assert call.state.writes == 1 and len(actual['complaints']) == 1, actual
        complaint = actual['complaints'][0]
        assert complaint['summary'] == approved.summary
        assert complaint['requested_resolution'] == approved.requested_resolution
        assert 'free' in complaint['requested_resolution'].lower(), complaint
        assert actual['bookings'] == before, 'complaint changed the upcoming booking'
        assert sum(complaint['reference'] in s.generated for s in call.all_spoken) == 1
    else:
        await turn(
            'Hey, I wanted to re-raise a complaint regarding my latest appointment that I had one week ago. '
            'It was completely uneven.'
        )
        assert call.state.agent == 'verify' and not call.state.customer_verified
        assert not call._asked or call._asked.about == 'phone'
        await turn(f'My phone number is {PHONE}.')
        assert call._asked and call._asked.about == 'phone'
        await turn('Yes, that is right.')
        assert call.state.agent == 'complaints' and call.state.customer_verified
        await turn('It was a haircut. I would like a free redo with a different stylist, please.')
        assert not call.state.writes
        approved = None
        for _ in range(5):
            if call.state.writes:
                break
            question = call._asked
            if question and question.turn == call.turn:
                assert question.about in ('complaint', 'phone'), question
                if question.about == 'complaint':
                    approved = question
                words = 'Yes, that is right.'
            elif call.state.agent == 'verify':
                words = f'My phone number is {PHONE}.'
            elif any('?' in s.text for s in call.spoken):
                words = 'Yes, please.'
            else:
                raise AssertionError('complaint stalled without the required structured readback')
            await turn(words)
        actual = outcome(PHONE)
        assert call.state.writes == 1 and len(actual['complaints']) == 1, actual
        complaint = actual['complaints'][0]
        assert approved and complaint['summary'] == approved.summary
        assert complaint['requested_resolution'] == approved.requested_resolution
        assert 'different stylist' in complaint['requested_resolution'].lower(), complaint
        assert sum(complaint['reference'] in s.generated for s in call.all_spoken) == 1
        assert not actual['bookings'] and not call.complaint_approved
    write({'result': 'ok', 'outcome': outcome(PHONE)})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--label', default='continuations')
    parser.add_argument('--scenario', action='append', choices=SCENARIOS)
    args = parser.parse_args()
    path = Path('bench/raw') / f'{args.label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    with path.open('w') as file:

        def write(row):
            file.write(json.dumps(row) + '\n')
            file.flush()

        write(
            {
                'suite': 'continuations',
                'audio': False,
                'settings': settings() | {'fixture': 'fresh', 'salon_clock': CLOCK.isoformat()},
                'suite_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )
        for repeat in range(args.repeats):
            for name in args.scenario or SCENARIOS:

                def row(value, name=name, repeat=repeat):
                    write({'scenario': name, 'repeat': repeat, **value})

                try:
                    await scenario(name, row)
                except Exception as error:
                    failures += 1
                    row({'result': f'{type(error).__name__}: {error}', 'outcome': outcome(PHONE)})
                    print(f'{repeat + 1} {name}: {type(error).__name__}: {error}', flush=True)
                else:
                    print(f'{repeat + 1} {name}: ok', flush=True)
    print(f'{failures} failures; evidence: {path}')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
