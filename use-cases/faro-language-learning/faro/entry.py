"""Faro, as every launcher needs it (`core.Entry`): its flow, its learners' store, its languages, its goodbye.

Faro keeps Pipecat's own turn taking and asks no silent learner if they are still there; it closes
the learner's session once the line drops (`faro.close_call`).
"""

import core
import faro

ENTRY = core.Entry(
    id=faro.SPEC.id or 'faro',
    name='faro call',
    flow=lambda: faro.FLOW,
    open=lambda flow, opening: faro.open_call(flow, opening.caller_id),
    greeting=faro.greeting,
    close=faro.close_call,
    stt_language=faro.STT_LANGUAGE,
    tts_language=faro.TTS_LANGUAGE,
    turn_taking='pipecat',
    reminders=False,
)
