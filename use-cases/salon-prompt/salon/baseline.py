"""The salon as one prompt on Pipecat's own Gemini service: the baseline the harness is measured against.

`bot.py` puts this where salon-graph's `bot.py` has `PydanticAgentProcessor`.
Everything else in the pipeline stays the same: STT, VAD, smart-turn, TTS,
the transport and the diary. What changes is who owns the conversation.
Here it is the usual Pipecat agent: one system prompt holding every rule, the
whole history sent on every request, and tools the model calls when it
decides to.

The prompt is the harness's own words, moved: the shared voice and diary
rules, the salon's documents, and one section per step
(`prompts/baseline.md`) with the rules the harness enforces in code written
as instructions instead. The tools reach the same `Diary` with the same
arguments, so a difference in a Coval run is the conversation layer and
nothing else.

The one guard the tools keep is the one any Pipecat bot has: no phone number,
no booking. Consent, read-backs and the "one question at a time" rules are
the model's to follow.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google import genai
from google.genai.types import HttpOptions
from opentelemetry import trace
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.llm_service import FunctionCallParams, FunctionCallResultProperties
from pydantic import TypeAdapter, ValidationError

from salon.diary import Diary, RefusedError
from salon.models.shapes import SubmittedPhone, unsupported_region

PROMPTS = Path(__file__).resolve().parent / 'prompts'
GREETING = "Hi, you've reached Sage and Stone. Robin speaking, what can I do for you?"
"""The graph build's opening line (`greeting:` in salon-graph's `flow.yaml`), word for word."""
MODEL = 'gemini-3.5-flash-lite'
"""The harness's model (`salon.classification.MODEL`), thinking pinned the same way."""

_tracer = trace.get_tracer('voice.baseline')


def written(action: str, args: dict[str, Any], result: Any) -> None:
    """One `write` span per diary write, as the harness leaves (`core.operations`), so concierge-tools' `ledger.py` counts both.

    It has no question in it: here the model decides the caller agreed, so
    whether it did is for a person or a judge to read from the transcript.
    """
    failed = isinstance(result, dict) and 'error' in result
    audit = {'action': action, 'args': args, 'status': 'failed' if failed else 'ok', 'consent': 'model'}
    with _tracer.start_as_current_span('write') as span:
        span.set_attribute('write.audit', json.dumps(audit, default=str))


PHONE = TypeAdapter(SubmittedPhone)


ASK_BOOKING = 'use `ask_booking`'
"""The one tool the shared diary rules name that the baseline does not have: it reads back and asks instead."""


def system_prompt() -> str:
    """Every rule the salon has, in one prompt: voice, tasks, diary rules, documents and complaint answers.

    The shared voice rules stop before their "Actions" section, which tells the
    model the runtime speaks read-backs and receipts. Here nothing does:
    `baseline.md` says what to do instead.
    """
    read = lambda name: (PROMPTS / name).read_text().strip()  # noqa: E731
    guidance = json.loads(read('knowledge/complaint-guidance.json'))
    voice = read('_voice.md').split('## Actions')[0].strip()
    diary = read('_diary.md')
    if ASK_BOOKING not in diary:
        raise RuntimeError('the diary rules changed: check the baseline still says the same')
    return '\n\n'.join(
        [
            voice,
            read('baseline.md'),
            diary.replace(ASK_BOOKING, 'read it back and wait for a yes'),
            '## Salon information',
            read('knowledge/services.md'),
            read('knowledge/refunds.md'),
            '## Complaint guidance',
            '\n'.join(f'- {topic}: {line}' for topic, line in guidance.items()),
        ]
    )


class VertexExpress(GoogleLLMService):
    """Pipecat's Gemini service on the Vertex AI Express-Mode key the harness uses.

    ponytail: Pipecat builds a Gemini API client from the key; this key is
    Vertex's, so the client is built for Vertex instead. Nothing else changes.
    """

    def create_client(self):
        self._client = genai.Client(vertexai=True, api_key=self._api_key, http_options=self._http_options)


@dataclass
class Desk:
    """What one call's tools share: the diary, the call's id for its offers, and who the caller is."""

    diary: Diary
    call_id: str
    phone: str = field(default='')

    def tools(self) -> list:
        """The five tools, bound to this call. Pipecat reads each one's schema from its signature and docstring."""

        async def confirm_customer(params: FunctionCallParams, phone: str):
            """Look up the caller, once they have said yes to their number read back.

            Args:
                phone: The caller's number in full international form: +34 then nine digits.
            """
            try:
                number = PHONE.validate_python(phone)
            except ValidationError as error:
                why = 'only Spanish numbers can be looked up' if unsupported_region(error) else str(error)
                return await params.result_callback({'error': why})
            self.phone = number
            await params.result_callback(self.diary.find_or_create_customer(number))

        async def read_diary(params: FunctionCallParams, day: str, service: str, prefer: str = ''):
            """The caller's bookings and the free times on a day.

            Args:
                day: The caller's own day words, like "Friday" or "tomorrow".
                service: haircut, hair-color, blowout, or any.
                prefer: The caller's own time words, like "morning" or "after 3", if any.
            """
            if not self.phone:
                return await params.result_callback({'error': 'Identify the caller first.'})
            now = self.diary.now()
            result = self.diary.find_slots(
                self.phone, day, service, now, offered_to=self.call_id, prefer=prefer
            )
            await params.result_callback(result)

        async def save_booking(
            params: FunctionCallParams,
            action: str,
            slot_id: str = '',
            booking_id: str = '',
            additional: bool = False,
        ):
            """Book, move or cancel an appointment.

            Args:
                action: book, move or cancel.
                slot_id: The slot id from `read_diary`, to book or move to.
                booking_id: The booking to move or cancel.
                additional: True only when the caller asked for a second appointment.
            """
            if not self.phone:
                return await params.result_callback({'error': 'Identify the caller first.'})
            try:
                result = self.diary.save_booking(
                    self.phone,
                    action,
                    slot_id,
                    booking_id,
                    additional,
                    self.diary.now(),
                    offered_to=self.call_id,
                )
            except RefusedError as error:
                result = {'error': str(error)}
            written('save_booking', {'action': action, 'slot_id': slot_id, 'booking_id': booking_id}, result)
            await params.result_callback(result)

        async def record_complaint(params: FunctionCallParams, summary: str, requested_resolution: str):
            """Record the caller's complaint, once they have said yes to it read back.

            Args:
                summary: What went wrong, in the caller's words.
                requested_resolution: What the caller wants the salon to do.
            """
            if not self.phone:
                return await params.result_callback({'error': 'Identify the caller first.'})
            result = self.diary.record_complaint(self.phone, summary, requested_resolution)
            written(
                'record_complaint', {'summary': summary, 'requested_resolution': requested_resolution}, result
            )
            await params.result_callback(result)

        async def end_call(params: FunctionCallParams, goodbye: str):
            """End the call once the caller has said they are finished.

            Args:
                goodbye: One short goodbye. It is the last thing they hear.
            """
            await params.llm.push_frame(TTSSpeakFrame(goodbye))
            await params.llm.push_frame(EndWorkerFrame())
            await params.result_callback(None, properties=FunctionCallResultProperties(run_llm=False))

        return [confirm_customer, read_diary, save_booking, record_complaint, end_call]


def node(diary: Diary, call_id: str) -> tuple[LLMContext, GoogleLLMService]:
    """The context and the LLM service that stand where the harness does."""
    llm = VertexExpress(
        api_key=os.environ['GOOGLE_API_KEY'],
        settings=GoogleLLMService.Settings(
            model=MODEL,
            system_instruction=system_prompt(),
            thinking=GoogleLLMService.ThinkingConfig(thinking_level='minimal'),
        ),
        http_options=HttpOptions(headers={'X-Vertex-AI-LLM-Shared-Request-Type': 'priority'}),
    )
    return LLMContext(tools=Desk(diary, call_id).tools()), llm


__all__ = ['GREETING', 'node']
