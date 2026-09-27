"""Stage B5: the real runtime's own spans, judged by the real evaluator command, offline.

One salon booking is driven through the real processor, a Pipecat TTS base,
`FastAPIWebsocketTransport` wired by `core.processor.playout` (so Twilio marks
go out and come back through `TwilioMarks`), over the fake phone of
`tests.test_marks`. Every span the runtime emits is captured with the
OpenTelemetry SDK's in-memory exporter and written in `load_report --saved`
form. Then the real `load_report.py` command judges it, in concierge-tools'
own folder, with an evidence file that holds what the runtime cannot say
about itself: what the test case asked for, a review of what the caller's
words meant and what the agent claimed, and the measured ending. Those are
written by this test from its own script, not read back from the trace.

* E1: with complete evidence, every dimension passes: exit 0;
* E2: the same export with `proposal_args` removed: authorization is unknown,
  exit 3; with the read-back's mark moved after the answer began: delivery
  fails, exit 1; with the playback record removed: delivery is unknown, exit
  3; with the marks bound to another proposal: unknown, exit 3.

The runtime's own consent check is not the judge here: the review is.
"""

import asyncio
import contextlib
import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pipecat.frames.frames import LLMContextFrame, VADUserStartedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from core.processor import PydanticAgentProcessor, playout
from tests.script import PHONE, opened, slot
from tests.test_marks import Phone, Voice

TOOLS = Path(__file__).parents[2] / 'concierge-tools'
CASE, SIMULATION = 'offline-booking', 'offline-e2e'
FRIDAY = '2026-10-02'

exported = InMemorySpanExporter()
_provider = trace.get_tracer_provider()
if not isinstance(_provider, TracerProvider):
    _provider = TracerProvider()
    trace.set_tracer_provider(_provider)
_provider.add_span_processor(SimpleSpanProcessor(exported))


class Line:
    """One salon call on the real adapter and transport, turn by turn, with the caller's onsets as a VAD reports them."""

    def __init__(self) -> None:
        self.script, self.flow, self.call, self.diary = opened(
            verified_phone=PHONE, customer_status='existing'
        )
        self.call.commit(agent='booking')
        self.node = PydanticAgentProcessor(self.call, self.flow.nodes)
        self.phone = Phone()
        transport = FastAPIWebsocketTransport(
            websocket=self.phone,  # ty: ignore[invalid-argument-type]
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                serializer=TwilioFrameSerializer(
                    stream_sid='MZ1', params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
                ),
            ),
        )
        after = playout(transport, self.call)
        assert after == [], 'a Twilio stream is wired in its serializer, with nothing after the output'
        pipeline = Pipeline([transport.input(), self.node, Voice(), transport.output()])
        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
            cancel_on_idle_timeout=False,
        )
        self.runner = WorkerRunner(handle_sigint=False)
        self.context = LLMContext()
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> Line:
        started = asyncio.Event()

        @self.worker.event_handler('on_pipeline_started')
        async def _started(worker, frame) -> None:
            started.set()

        await self.runner.add_workers(self.worker)
        self._tasks = [asyncio.create_task(self.runner.run()), asyncio.create_task(self.phone.play())]
        await asyncio.wait_for(started.wait(), 5)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.worker.cancel()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5)

    async def until(self, done, limit: float = 5.0) -> None:
        deadline = time.perf_counter() + limit
        while not done():
            if time.perf_counter() > deadline:
                raise TimeoutError('the line never got there')
            await asyncio.sleep(0.005)

    async def turn(self, words: str, *moves: Any) -> None:
        """The last answer plays out, the caller starts speaking after it, then their words arrive."""
        await self.until(lambda: all(s.delivered == 'delivered' for s in self.call.spoken if s.mark))
        await asyncio.sleep(0.25)
        self.script.moves = list(moves)
        await self.node.queue_frame(VADUserStartedSpeakingFrame(start_secs=0.2))
        self.context.add_message({'role': 'user', 'content': words})
        await self.node.queue_frame(LLMContextFrame(self.context))
        await self.until(lambda: self.call.heard.endswith(words) and self.node._idle.is_set())


def rows(spans: list) -> list[dict[str, Any]]:
    """The emitted spans as observations in a `load_report --saved` export: one Coval-shaped row."""
    observations = [
        {
            'id': format(s.context.span_id, '016x'),
            'parent': format(s.parent.span_id, '016x') if s.parent else None,
            'type': 'SPAN',
            'name': s.name,
            'start': _iso(s.start_time),
            'end': _iso(s.end_time),
            'attrs': dict(s.attributes or {}),
        }
        for s in spans
    ]
    first, last = min(s.start_time for s in spans), max(s.end_time for s in spans)
    observations.append(
        {
            'id': 'c0',
            'parent': None,
            'type': 'SPAN',
            'name': 'conversation',
            'start': _iso(first),
            'end': _iso(last),
            'attrs': {},
        }
    )
    simulation = {
        'simulation_id': SIMULATION,
        'status': 'COMPLETED',
        'call_sid': 'CAoffline',
        'test_case_id': CASE,
    }
    return [{'run': 'offline', 'simulation': simulation, 'observations': observations}]


def _iso(nanos: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(nanos / 1e9, UTC).isoformat()


def evidence(write_key: str) -> dict[str, Any]:
    """What the runtime cannot say about itself, written from this test's own script."""
    appointment = {'service': 'haircut', 'date': FRIDAY, 'time': '14:00', 'phone': PHONE}
    return {
        'cases': {CASE: {'bookings': 1, 'service': 'haircut'}},
        'reviews': {
            SIMULATION: {
                'reviewer': 'agent',
                'source': 'tests.test_audit_export: the scripted caller of this offline call',
                'final': appointment,
                'writes': {
                    write_key: {'accepted': True, 'note': '"Yes, please." after the read-back played'}
                },
                'claims': [{'action': 'booked', 'appointment': appointment}],
            }
        },
        'facts': {
            SIMULATION: {
                'ended_by': 'agent',
                'observed': True,
                'source': 'tests.test_audit_export: the pipeline ended on the goodbye',
            }
        },
    }


def judge(export: list[dict[str, Any]], proof: dict[str, Any], folder: Path, name: str) -> int:
    """The real `load_report.py` command, in concierge-tools' folder, on a saved export and an evidence file."""
    (folder / f'{name}.json').write_text(json.dumps(export))
    (folder / f'{name}-evidence.json').write_text(json.dumps(proof))
    done = subprocess.run(
        [
            sys.executable,
            'load_report.py',
            '--saved',
            str(folder / f'{name}.json'),
            '--evidence',
            str(folder / f'{name}-evidence.json'),
            '--out',
            str(folder / name),
        ],
        cwd=TOOLS,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return done.returncode


def write_audit(export: list[dict[str, Any]]) -> dict[str, Any]:
    [write] = [
        o for o in export[0]['observations'] if o['name'] == 'write' and 'booked' in o['attrs']['write.audit']
    ]
    return write


def edited(export: list[dict[str, Any]], change) -> list[dict[str, Any]]:
    """A copy of the export with the booking's write audit changed by `change(audit)`."""
    out = copy.deepcopy(export)
    write = write_audit(out)
    audit = json.loads(write['attrs']['write.audit'])
    change(audit)
    write['attrs']['write.audit'] = json.dumps(audit)
    return out


async def a_booking_on_the_wire() -> list[dict[str, Any]]:
    exported.clear()
    async with Line() as line:
        friday = slot(FRIDAY, '14:00')
        await line.turn(
            'A haircut on Friday at two, please.',
            ('collect', {'service': 'haircut', 'booking_day': 'Friday', 'booking_time': '14:00'}),
            ('call', 'options'),
            ('say', 'I have two in the afternoon. Shall I take it?'),
        )
        await line.turn('That one.', ('collect', {'slot': friday}), ('call', 'book_appointment'))
        await line.turn('Yes, please.', ('call', 'book_appointment'))
        assert len(line.diary.bookings_for(PHONE, line.diary.now().date())) == 1, (
            'the heard yes did not book once'
        )
        [standing] = line.call.effective()
        await line.turn("That's all, thanks.", ('call', 'goodbye', {'recap': [standing.intent[:8]]}))
    return rows(list(exported.get_finished_spans()))


def main() -> None:
    export = asyncio.run(a_booking_on_the_wire())
    audit = json.loads(write_audit(export)['attrs']['write.audit'])
    assert audit['intent'] and audit['operation'] == audit['intent'] and audit['sequence'] >= 1, audit
    assert audit['proposal_args'] == audit['args'] and audit['needs_approval'] is True, audit
    assert audit['transport'] == 'twilio' and audit['simulated_playout'] is False, audit
    assert audit['playback'] and audit['playback']['marks'], audit
    assert all(m['proposal'] == audit['proposal'] for m in audit['playback']['marks']), audit
    key = f'book_appointment@{audit["turn"]}'
    proof = evidence(key)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        assert judge(export, proof, folder, 'complete') == 0, (folder / 'complete' / 'report.md').read_text()
        cases = {
            'no-proposal-args': (lambda a: a.pop('proposal_args'), 3),
            'late-mark': (lambda a: a['playback'].update(answered_at='2000-01-01T00:00:00+00:00'), 1),
            'no-playback': (lambda a: a.pop('playback'), 3),
            'other-proposal': (
                lambda a: a['playback'].update(
                    marks=[{**m, 'proposal': m['proposal'] + 7} for m in a['playback']['marks']]
                ),
                3,
            ),
        }
        for name, (change, expected) in cases.items():
            status = judge(edited(export, change), proof, folder, name)
            assert status == expected, (name, status, (folder / name / 'report.md').read_text())
    print('audit export ok: E1 complete evidence passes; E2 missing or corrupted binding and delivery do not')


if __name__ == '__main__':
    main()
