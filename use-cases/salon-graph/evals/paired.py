"""Session 4's paired semantic check: one caller reply, the classifier-first control and the node-agent candidate.

    cd use-cases/salon-graph
    uv run --env-file ../../.env python -m evals.paired candidate --requests 16 --out /tmp/paired
    cd <control checkout>/use-cases/salon-graph
    uv run --env-file <repo>/.env python <repo>/use-cases/salon-graph/evals/paired.py control --requests 16 --out /tmp/paired

The same file runs in both arms, each in its own checkout and process, so
importing `salon` can only ever load that arm's salon. For every case in
`docs/research/reusable-core/session-4/cases.json`, in order, it seeds the
same state (a heard read-back, an offer on the table, a receipt that was cut),
puts the same prior dialogue in the history, and sends one caller reply
through the arm's real turn engine and its real model. It records what the
business ended up with (bookings, identity, what was kept), what the caller
was asked next, whether the call ended, and the provider requests and tokens
the turn spent. Nothing a model says is graded; the expectations are the
case file's.

One attempt per case. A case starts only while the arm has more requests left
than one turn can spend (8); when it has not, the rest are reported as not run.

Seeding writes the arm's own records directly (its question ledger, its
offers); where an arm needs a turn to get there (a booking whose receipt is
then cut), that turn runs on a scripted model and costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path.cwd()))

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from core.observability import setup
from core.policies import TurnBudget
from core.turn import FixedSpeech, TextDelta, TurnFinished, receive, run_turn
from evals.calls import attributes, is_request, spans
from salon.diary import TZ, Diary

CLOCK = datetime(2026, 9, 28, 8, 30, tzinfo=TZ)
PHONE = '+34600111222'
TURN_REQUESTS = 4
"""What the measured reply turn may spend, on both arms alike: a case starts only while the arm has at least this left.

A turn's own budget is 8 requests; 4 keeps the 32-request allowance a hard cap and still fits every reply either
arm needs in the offline runs. A reply that needs more ends `exhausted` and is reported as an incomplete measurement.
"""
CASES = (
    Path(__file__).resolve().parents[3] / 'docs' / 'research' / 'reusable-core' / 'session-4' / 'cases.json'
)


def history(*lines: tuple[str, str]) -> list:
    """Prior dialogue as model messages: ('caller', words) or ('agent', words)."""
    return [
        ModelRequest(parts=[UserPromptPart(text)])
        if who == 'caller'
        else ModelResponse(parts=[TextPart(text)])
        for who, text in lines
    ]


def offered_times(diary: Diary, key: str, day: str) -> list[dict]:
    return diary.find_slots(PHONE, day, 'haircut', diary.now(), offered_to=key)['slots']


def offer_words(slots: list[dict]) -> str:
    day = slots[0]['spoken'].rsplit(' at ', 1)[0]
    times = ', '.join(s['time'] for s in slots[:3])
    return f'For a haircut on {day}, I have {times}. Which time works for you?'


class Candidate:
    """The node-agent salon: `salon.app`, agent nodes compiled from `agent.yaml`."""

    name = 'candidate'

    def __init__(self) -> None:
        from core.steps import Prompted, _args
        from salon.app import FLOW, open_call

        self.flow, self.open_call, self.prompted, self.args = FLOW, open_call, Prompted, _args
        self.nodes = FLOW.nodes

    def open(self, verified: bool, **facts: Any):
        self.diary = Diary(clock=CLOCK)
        extra = {'verified_phone': PHONE, 'customer_status': 'existing'} if verified else {}
        self.call = self.open_call(self.flow, PHONE, self.diary, **extra, **facts)
        return self.call

    @property
    def key(self) -> str:
        return self.call.services.call

    async def ask(self, about: str, words: str, value: str = '') -> None:
        await self.call.speak_now(
            self.prompted(about=about, spoken=words, value=value).put(self.call), origin='ask'
        )

    def operation(self, step: str, name: str):
        return next(op for op in getattr(self.flow.by_id[step], 'operations', ()) if op.name == name)

    async def booking_readback(self, day: str, at: str) -> None:
        call = self.open(True)
        call.commit(agent='booking')
        slots = offered_times(self.diary, self.key, day)
        call._offered = {'slot': {s['slot_id']: s['spoken'] for s in slots}}
        call.collect(service='haircut', booking_day=day, booking_time=at)
        call._offered = {'slot': {s['slot_id']: s['spoken'] for s in slots}}
        call.collect(slot=next(s['slot_id'] for s in slots if s['time'] == at))
        book = self.operation('booking', 'book_appointment')
        proposal = book.proposal_for(call, self.args(book, call))
        call.messages = history(
            ('caller', f'A haircut on {day}, please.'),
            ('agent', offer_words(slots)),
            ('caller', f'{at}, please.'),
        )
        call.turn_started(f'{at}, please.')
        await self.ask(proposal.about, proposal.spoken, proposal.value)

    async def phone_readback(self, day: str) -> None:
        call = self.open(False, service='haircut', booking_day=day)
        call.commit(agent='verify', pending='booking')
        lookup = self.operation('verify', 'lookup_customer')
        proposal = lookup.proposal_for(call, self.args(lookup, call))
        call.messages = history(('caller', f'I would like a haircut on {day}.'))
        call.turn_started(f'I would like a haircut on {day}.')
        await self.ask(proposal.about, proposal.spoken, proposal.value)

    async def offer(self, day: str, service: str = 'haircut') -> None:
        call = self.open(True)
        call.commit(agent='booking')
        slots = offered_times(self.diary, self.key, day)[:3]
        call.collect(service=service, booking_day=day)
        call._offered = {'slot': {s['slot_id']: s['spoken'] for s in slots}}
        call.messages = history(('caller', f'A haircut on {day}, please.'))
        call.turn_started(f'A haircut on {day}, please.')
        await self.ask('offer', offer_words(slots))

    async def booked(self, day: str, at: str) -> None:
        await self.booking_readback(day, at)
        self.heard()

        async def yes(messages, info: AgentInfo):
            yield {0: DeltaToolCall(name='book_appointment', json_args='{}')}

        with self.nodes['booking'].agent.override(model=FunctionModel(stream_function=yes)):
            await drive(self.call, self.nodes, 'Yes.')

    def facts(self) -> dict:
        return self.call.state.facts.model_dump(mode='json') | {'agent': self.call.state.agent}

    def verified(self) -> bool:
        return self.call.state.facts.verified_phone == PHONE

    def heard(self) -> None:
        self.call.acknowledge(*(s.id for s in self.call.spoken if s.text))
        self.call.turn_ended(interrupted=False)


class Control:
    """The classifier-first salon at the parent commit: `salon.agents`, the readings and `SalonCall`."""

    name = 'control'

    def __init__(self) -> None:
        from salon.agents import AGENTS  # ty: ignore[unresolved-import]
        from salon.tools.booking import booking_question, offer  # ty: ignore[unresolved-import]
        from salon.tools.identity import readback_number  # ty: ignore[unresolved-import]

        from salon.models.state import BookingRequest, open_call  # ty: ignore[unresolved-import]

        self.nodes, self.open_call, self.request = AGENTS, open_call, BookingRequest
        self.booking_question, self.offer_times, self.readback_number = (
            booking_question,
            offer,
            readback_number,
        )

    def open(self, verified: bool, **_: Any):
        self.diary = Diary(clock=CLOCK)
        self.call = self.open_call(PHONE, diary=self.diary)
        if verified:
            self.call.commit(customer_verified='existing', verified_phone=PHONE, phone_confirmed=True)
        return self.call

    @property
    def key(self) -> str:
        return self.call.id

    async def booking_readback(self, day: str, at: str) -> None:
        call = self.open(True)
        call.commit(agent='booking')
        slots = offered_times(self.diary, self.key, day)
        call.messages = history(
            ('caller', f'A haircut on {day}, please.'),
            ('agent', offer_words(slots)),
            ('caller', f'{at}, please.'),
        )
        call.turn_started(f'{at}, please.')
        question = self.booking_question(
            call, 'book', next(s['slot_id'] for s in slots if s['time'] == at), ''
        )
        await call.speak_now(question.put(call), origin='ask')

    async def phone_readback(self, day: str) -> None:
        call = self.open(False)
        call.commit(agent='verify', pending='booking', wanted=self.request(service='haircut', when=day))
        call.messages = history(('caller', f'I would like a haircut on {day}.'))
        call.turn_started(f'I would like a haircut on {day}.')
        question = await self.readback_number(call, PHONE)
        await call.speak_now(question.put(call), origin='ask')

    async def offer(self, day: str, service: str = 'haircut') -> None:
        call = self.open(True)
        call.commit(agent='booking')
        call.messages = history(('caller', f'A haircut on {day}, please.'))
        call.turn_started(f'A haircut on {day}, please.')
        await self.offer_times(call, day, service, '')

    async def booked(self, day: str, at: str) -> None:
        from tests.classifier_model import default_script, scripted  # ty: ignore[unresolved-import]

        await self.booking_readback(day, at)
        self.heard()
        with scripted(lambda name, data: 'accept' if name == 'confirmation' else default_script(name, data)):
            await drive(self.call, self.nodes, 'Yes.')

    def facts(self) -> dict:
        return self.call.state.model_dump(mode='json')

    def verified(self) -> bool:
        return self.call.state.verified_phone == PHONE

    def heard(self) -> None:
        self.call.acknowledge(*(s.id for s in self.call.spoken if s.text))
        self.call.turn_ended(interrupted=False)


async def drive(call, nodes, words: str, budget: TurnBudget | None = None) -> tuple[str, TurnFinished | None]:
    """One caller turn through the arm's real engine; everything said, and how it ended."""
    parts, done = [], None
    async for event in run_turn(call, nodes, words, budget or TurnBudget()):
        if isinstance(event, TextDelta | FixedSpeech):
            parts.append(event.text)
        elif isinstance(event, TurnFinished):
            done = event
    return ''.join(parts), done


async def seed(arm, setup: str) -> None:
    if setup == 'booking_readback':
        await arm.booking_readback('Tuesday', '09:00')
    elif setup == 'phone_readback':
        await arm.phone_readback('Friday')
    elif setup == 'three_slot_offer':
        await arm.offer('Tuesday')
    elif setup == 'offer_friday_next_week':
        await arm.offer('Friday next week')
    elif setup in ('receipt_cut', 'receipt_heard'):
        await arm.booked('Tuesday', '09:00')
        receipt = [s for s in arm.call.spoken if s.text]
        if setup == 'receipt_cut':
            arm.call.acknowledge(*(s.id for s in receipt), heard='')
            arm.call.turn_ended(interrupted=True)
            return
    else:
        raise ValueError(setup)
    arm.heard()


def judge(case: dict, seen: dict) -> list[str]:
    """What this reply got wrong against the case's expected business outcome. Empty means it met it."""
    expect, wrong = case['expect'], []
    said, facts = seen['said'].lower(), json.dumps(seen['facts']).lower()
    asked = (seen['asked'] or '').lower()
    if len(seen['bookings']) != expect['bookings']:
        wrong.append(f'bookings {len(seen["bookings"])}, expected {expect["bookings"]}')
    if 'kept_time' in expect and any(b['time'] != expect['kept_time'] for b in seen['bookings']):
        wrong.append(f'the booking moved: {seen["bookings"]}')
    if expect.get('verified') and not seen['verified']:
        wrong.append('the caller was not verified')
    if 'keeps' in expect and expect['keeps'] not in facts + said:
        wrong.append(f'lost "{expect["keeps"]}"')
    if 'moves_on_to' in expect and expect['moves_on_to'].lower() not in said + asked + facts:
        wrong.append(f'did not move on to {expect["moves_on_to"]}')
    if expect.get('no_single_readback') and 'shall i book' in said:
        wrong.append('read one time back on a reply that picked none')
    if 'reads_back' in expect and not (
        'shall i book' in said and expect['reads_back'].replace(':00', '') in said
    ):
        wrong.append(f'did not read back {expect["reads_back"]}')
    if 'says' in expect and expect['says'] not in said:
        wrong.append(f'did not say {expect["says"]}')
    if expect.get('ended') and not seen['ended']:
        wrong.append('the call did not end')
    if expect.get('names_a_date') and not any(d in said for d in ('3rd', '10th', 'october')):
        wrong.append('named no date')
    return wrong


async def run(arm_name: str, requests_left: int, out: Path, dry: bool = False) -> int:
    cases = json.loads(CASES.read_text())['cases']
    collector = spans()
    arm = Candidate() if arm_name == 'candidate' else Control()
    if dry:
        dry_run(arm)
    results = []
    for case in cases:
        if requests_left < TURN_REQUESTS:
            results.append({'id': case['id'], 'holdout': case['holdout'], 'result': 'not run: allowance'})
            continue
        session = f'paired-{arm_name}-{case["id"]}-{uuid.uuid4().hex[:6]}'
        setup(session=session, trace_name=f'salon paired {arm_name}')
        await seed(arm, case['setup'])
        collector.take()
        before = time.time()
        said, done = await drive(
            arm.call, arm.nodes, case['utterance'], TurnBudget(model_requests=TURN_REQUESTS)
        )
        receive(arm.call)
        ended = collector.take()
        requests = [s for s in ended if is_request(s)]
        seen = {
            'said': said,
            'asked': getattr(arm.call._asked, 'spoken', None),
            'bookings': arm.diary.bookings_for(PHONE, CLOCK.date()),
            'verified': arm.verified(),
            'ended': bool(arm.call.ended),
            'facts': arm.facts(),
        }
        wrong = judge(case, seen)
        usage = {
            'input': sum(int(attributes(s).get('gen_ai.usage.input_tokens', 0)) for s in requests),
            'output': sum(int(attributes(s).get('gen_ai.usage.output_tokens', 0)) for s in requests),
            'cache': sum(
                int(attributes(s).get('gen_ai.usage.cache_read_input_tokens', 0) or 0) for s in requests
            ),
            'reasoning': sum(
                int(attributes(s).get('gen_ai.usage.details.reasoning_tokens', 0) or 0) for s in requests
            ),
        }
        requests_left -= len(requests)
        results.append(
            {
                'id': case['id'],
                'holdout': case['holdout'],
                'utterance': case['utterance'],
                'result': 'incomplete: turn allowance'
                if done and done.status == 'exhausted'
                else 'met'
                if not wrong
                else 'missed',
                'wrong': wrong,
                'said': said,
                'status': done.status if done else '',
                'requests': len(requests),
                'kinds': [str(attributes(s).get('gen_ai.request.model', '')) for s in requests],
                'tokens': usage,
                'seconds': round(time.time() - before, 2),
                'session': session,
                **{k: v for k, v in seen.items() if k != 'said'},
            }
        )
        print(
            f'{arm_name} {case["id"]}: {results[-1]["result"]} {wrong} requests {len(requests)} tokens {usage}',
            flush=True,
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / f'{arm_name}.json').write_text(json.dumps(results, indent=2, default=str))
    print(f'{arm_name}: {requests_left} requests left -> {out / (arm_name + ".json")}', flush=True)
    return 0


def dry_run(arm) -> None:
    """`--dry`: every agent (and, in the control, every reading) scripted, to check the seeding costs nothing live."""
    from contextlib import ExitStack

    import pydantic_ai.models

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    async def ok(messages, info: AgentInfo):
        yield 'Okay.'

    stack = ExitStack()
    for node in arm.nodes.values():
        stack.enter_context(node.agent.override(model=FunctionModel(stream_function=ok)))
    if arm.name == 'control':
        from tests.classifier_model import classifier_model  # ty: ignore[unresolved-import]

        stack.enter_context(classifier_model())
    arm.dry = stack


def main() -> int:
    parser = argparse.ArgumentParser(prog='evals.paired')
    parser.add_argument('arm', choices=('candidate', 'control'))
    parser.add_argument(
        '--requests', type=int, default=16, help="this arm's share of the 32-request allowance"
    )
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument(
        '--dry', action='store_true', help='scripted models only: check the seeding, spend nothing'
    )
    args = parser.parse_args()
    return asyncio.run(run(args.arm, args.requests, args.out, args.dry))


if __name__ == '__main__':
    sys.exit(main())
