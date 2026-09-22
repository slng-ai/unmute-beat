"""Matched transcript qualification on real POC/native runtimes; never an audio benchmark.

Run each arm in its own process, using the same interpreter, environment and
scenario. Native text frames replace TTS; Workers, bus, Flows and Google service
run unchanged. Completed text-only provider responses delimit native turns.
"""

import argparse
import asyncio
import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from salon import diary
from salon.state import Appointment, open_call

ROOT = Path(__file__).resolve().parents[1]
PHONE = '+34600111222'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)
SCENARIOS = {
    'hours': ('What are your opening hours on weekdays and Saturdays? Are you open on Sunday?',),
    'complaint': ('My haircut was uneven yesterday. I want a full refund.', 'Yes, that is right.'),
    'complaint_verify': (
        'My haircut was uneven yesterday. I want a full refund.',
        'Yes, that is my number.',
        'Yes, that is right.',
    ),
    'booking': (
        'I would like a haircut this Friday afternoon.',
        'Yes, that is my number.',
        'The three o clock one please.',
    ),
    'move': ('Move my haircut to Saturday at the same time.',),
}


def fixture(name):
    diary.reset()
    diary.set_clock(CLOCK)
    diary.find_or_create_customer(PHONE)
    if name == 'move':
        slots = diary.find_slots(PHONE, 'Friday', 'haircut', CLOCK, prefer='15:00')['slots']
        selected = next(slot for slot in slots if slot['time'] == '15:00')
        return diary.save_booking(PHONE, 'book', selected['slot_id'], '', False, CLOCK)
    return None


def business():
    return {'bookings': diary.bookings_for(PHONE, CLOCK.date()), 'complaints': diary.complaints()}


def score(name, rows):
    """Same business/consent checks for both arms; retain speech for manual review."""
    final = rows[-1]['business']
    errors = []
    speech = ' '.join(row['speech'] for row in rows).lower()
    if any(row['status'] != 'answered' for row in rows):
        errors.append('an unanswered turn')
    if name in ('complaint_verify', 'booking'):
        digits = re.sub(r'\D', '', rows[0]['speech'])
        if PHONE[1:] not in digits:
            errors.append('complete phone readback missing before verification')
    if name == 'hours':
        if final['bookings'] or final['complaints'] or any(row['tools'] for row in rows):
            errors.append('public question triggered an action')
        if 'sunday' not in speech or not ('closed' in speech or 'shut' in speech):
            errors.append('missing Sunday closure')
    elif name.startswith('complaint'):
        if len(final['complaints']) != 1 or final['bookings']:
            errors.append('expected one complaint and no booking')
        if any(row['business']['complaints'] for row in rows[:-1]):
            errors.append('complaint written before readback approval')
        if final['complaints']:
            saved = final['complaints'][0]
            if (
                'uneven' not in saved['summary'].lower()
                or 'full refund' not in saved['requested_resolution'].lower()
            ):
                errors.append('complaint facts or requested remedy changed')
            if saved['reference'] not in rows[-1]['speech']:
                errors.append('complaint receipt not spoken')
    else:
        expected_day = '2026-09-26' if name == 'move' else '2026-09-25'
        bookings = final['bookings']
        if len(bookings) != 1 or bookings[0]['date'] != expected_day or bookings[0]['time'] != '15:00':
            errors.append('wrong booking count, date or time')
        if name == 'booking' and any(row['business']['bookings'] for row in rows[:-1]):
            errors.append('booking written before slot selection')
    return errors


async def poc(name, saved, rows):
    from bench.scenarios import Counter
    from salon.agents import AGENTS

    call = open_call(PHONE)
    if name in ('complaint', 'move'):
        call.commit(customer_verified='existing', verified_phone=PHONE)
    if saved:
        call.commit(
            appointment=Appointment(
                **{k: saved[k] for k in Appointment.model_fields if k != 'action'}, action='book'
            )
        )
    processor = Counter(call, AGENTS)
    for said in SCENARIOS[name]:
        start = time.perf_counter()
        await processor._turn(said)
        rows.append(
            {
                'said': said,
                'seconds': time.perf_counter() - start,
                'requests': processor.requests(),
                'ttft': [r.ttft for r in call.requests if r.ttft is not None],
                'first_speech_seconds': min((s.at - start for s in call.spoken), default=None),
                'speech': ' '.join(s.generated for s in call.spoken),
                'status': processor._finished.status,
                'tools': list(processor._finished.handoffs),
                'business': business(),
            }
        )
    return rows


async def native(name, saved, build, rows):
    from pipecat.bus import BusBridgeProcessor
    from pipecat.frames.frames import (
        LLMAssistantPushAggregationFrame,
        LLMRunFrame,
        LLMTextFrame,
        TextFrame,
        TTSSpeakFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.workers.llm import LLMWorkerActivationArgs
    from pipecat.workers.runner import WorkerRunner

    sys.path.insert(0, str(build))
    spec = importlib.util.spec_from_file_location('native_salon_bot', build / 'bot.py')
    assert spec and spec.loader
    bot = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bot
    spec.loader.exec_module(bot)
    spoken = []
    spoken_at = []

    class TextSink(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, (LLMTextFrame, TTSSpeakFrame)):
                spoken.append(frame.text)
                spoken_at.append(time.perf_counter())
            if isinstance(frame, TTSSpeakFrame):
                await self.push_frame(TextFrame(frame.text), direction)
                await self.push_frame(LLMAssistantPushAggregationFrame(), direction)
            else:
                await self.push_frame(frame, direction)

    bot.build_concierge_tts = TextSink
    bot.build_complaint_specialist_tts = TextSink
    context = LLMContext(messages=[{'role': 'user', 'content': SCENARIOS[name][0]}])
    state = bot.build_state()
    state.customer_phone = PHONE
    state.booking_date = CLOCK.date().isoformat()
    state.booking_weekday = 'Monday'
    state.salon_local_time = '10:00'
    if name in ('complaint', 'move'):
        state._unconfirmed.discard('customer_phone')
        state.customer_verified = 'existing'
    if saved:
        state.appointment = {k: saved[k] for k in ('booking_id', 'service', 'date', 'time', 'spoken')}
        state.appointment['action'] = 'book'
    workers = [
        bot.ConciergeAgent(state=state, context=context),
        bot.ComplaintSpecialistAgent(state=state, context=context),
    ]
    requests = []
    done = asyncio.Event()
    for worker in workers:
        llm = worker.llm
        original_stream = llm._stream_response
        original_process = llm._process_context
        local = {}

        async def measured_stream(ctx, original=original_stream, local=local):
            row = {
                'ttft': None,
                'tools': [],
                'text': '',
                'prompt': str(ctx.get_messages()),
                'started': time.perf_counter(),
            }
            local['row'] = row
            requests.append(row)
            if len(requests) > 40:
                raise RuntimeError('Native qualification exceeded 40 requests')
            async for chunk in original(ctx):
                for candidate in chunk.candidates or []:
                    for part in candidate.content.parts if candidate.content else []:
                        if not part.thought and (part.text or part.function_call):
                            if row['ttft'] is None:
                                row['ttft'] = time.perf_counter() - row['started']
                            if part.text:
                                row['text'] += part.text
                            if part.function_call:
                                row['tools'].append(
                                    {'name': part.function_call.name, 'args': part.function_call.args}
                                )
                if chunk.usage_metadata:
                    row['usage'] = chunk.usage_metadata.model_dump(mode='json', exclude_none=True)
                yield chunk

        async def measured_process(ctx, original=original_process, local=local):
            await original(ctx)
            if (row := local.get('row')) and not row['tools']:
                done.set()

        llm._stream_response = measured_stream
        llm._process_context = measured_process

    runner = WorkerRunner()
    user, assistant = LLMContextAggregatorPair(context)
    main = PipelineWorker(
        Pipeline([user, BusBridgeProcessor(bus=runner.bus, worker_name='main'), assistant]), name='main'
    )
    ready, started = asyncio.Event(), asyncio.Event()
    start = 0.0

    @runner.event_handler('on_ready')
    async def on_ready(runner):
        ready.set()

    @main.event_handler('on_pipeline_started')
    async def on_started(worker, frame):
        nonlocal start
        await ready.wait()
        await runner.add_workers(*workers)
        start = time.perf_counter()
        await main.activate_worker('concierge', args=LLMWorkerActivationArgs(messages=[], run_llm=True))
        started.set()

    await runner.add_workers(main)
    running = asyncio.create_task(runner.run())
    before, before_spoken = 0, 0
    try:
        await asyncio.wait_for(started.wait(), 10)
        for index, said in enumerate(SCENARIOS[name]):
            if index:
                done.clear()
                before, before_spoken = len(requests), len(spoken)
                context.add_message({'role': 'user', 'content': said})
                start = time.perf_counter()
                await next(worker for worker in workers if worker.active).queue_frame(LLMRunFrame())
            else:
                before, before_spoken = 0, 0
            await asyncio.wait_for(done.wait(), 30)
            elapsed = time.perf_counter() - start
            for worker in workers:
                if worker.active:
                    await asyncio.wait_for(worker.flush_pipeline(), 5)
            await main.flush_pipeline()
            recent = requests[before:]
            rows.append(
                {
                    'said': said,
                    'seconds': elapsed,
                    'requests': len(recent),
                    'ttft': [r['ttft'] for r in recent if r['ttft'] is not None],
                    'first_speech_seconds': spoken_at[before_spoken] - start
                    if len(spoken_at) > before_spoken
                    else None,
                    'speech': ''.join(spoken[before_spoken:]),
                    'status': 'answered',
                    'tools': [t for r in recent for t in r['tools']],
                    'business': business(),
                    'protocol': recent,
                }
            )
    finally:
        if len(rows) < len(SCENARIOS[name]):
            recent = requests[before:]
            rows.append(
                {
                    'said': SCENARIOS[name][len(rows)],
                    'seconds': time.perf_counter() - start,
                    'status': 'failed',
                    'speech': ''.join(spoken[before_spoken:]),
                    'requests': len(recent),
                    'tools': [t for r in recent for t in r['tools']],
                    'protocol': recent,
                    'business': business(),
                }
            )
        await runner.cancel(reason='Qualification complete')
        await running
    return rows


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm', choices=('poc', 'native', 'paired'))
    parser.add_argument('scenario', choices=(*SCENARIOS, 'all'))
    parser.add_argument(
        '--build',
        type=Path,
        default=ROOT / 'bench/raw/native-reference/examples/salon-concierge/build/pipecat',
    )
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pairs', type=int, default=20)
    parser.add_argument(
        '--native-dependencies',
        type=Path,
        help='Existing native venv site-packages; appended after the POC runtime, never installed or modified',
    )
    args = parser.parse_args()
    if args.arm == 'paired':
        assert args.pairs > 0
        args.output.mkdir(parents=True, exist_ok=True)
        names = SCENARIOS if args.scenario == 'all' else (args.scenario,)
        for repeat in range(args.pairs):
            for name in names:
                arms = ('native', 'poc') if repeat % 2 else ('poc', 'native')
                for arm in arms:
                    destination = args.output / f'{name}-{repeat:02d}-{arm}.json'
                    assert not destination.exists(), f'Never overwrite a measured run: {destination}'
                    command = [
                        sys.executable,
                        '-B',
                        '-m',
                        'bench.native_compare',
                        arm,
                        name,
                        '--build',
                        str(args.build),
                        '--output',
                        str(destination),
                    ]
                    if args.native_dependencies:
                        command += ['--native-dependencies', str(args.native_dependencies)]
                    try:
                        with destination.with_suffix('.log').open('w') as log:
                            completed = subprocess.run(
                                command, stdout=log, stderr=subprocess.STDOUT, timeout=90, check=False
                            )
                        exit_code = completed.returncode
                    except subprocess.TimeoutExpired:
                        exit_code = 124
                        destination.write_text(
                            json.dumps(
                                {
                                    'arm': arm,
                                    'scenario': name,
                                    'rows': [],
                                    'errors': ['Process exceeded 90-second limit'],
                                }
                            )
                            + '\n'
                        )
                    data = (
                        json.loads(destination.read_text())
                        if destination.exists()
                        else {'errors': ['No output artifact']}
                    )
                    print(
                        json.dumps(
                            {
                                'pair': repeat,
                                'scenario': name,
                                'arm': arm,
                                'exit': exit_code,
                                'errors': data.get('errors'),
                            }
                        ),
                        flush=True,
                    )
        return
    assert args.scenario != 'all', 'all requires paired mode'
    if args.native_dependencies:
        sys.path.append(str(args.native_dependencies))
    logger.remove()
    logger.add(sys.stderr, level='WARNING')
    saved = fixture(args.scenario)
    result = {
        'arm': args.arm,
        'scenario': args.scenario,
        'clock': CLOCK.isoformat(),
        'mode': 'transcript',
        'rows': [],
    }
    try:
        result['rows'] = await (
            poc(args.scenario, saved, result['rows'])
            if args.arm == 'poc'
            else native(args.scenario, saved, args.build, result['rows'])
        )
        result['errors'] = score(args.scenario, result['rows'])
    except Exception as exc:
        result['errors'] = [f'{type(exc).__name__}: {exc}']
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, default=str) + '\n')
    print(
        json.dumps(
            {
                'arm': args.arm,
                'scenario': args.scenario,
                'errors': result['errors'],
                'requests': sum(r['requests'] for r in result['rows']),
            }
        )
    )


if __name__ == '__main__':
    asyncio.run(main())
