"""Talk to the agents with no audio, no browser and no transport.

The fastest loop for working on prompts: it drives the real agents through the
real processor and prints every frame they would have spoken, so you see the
handoffs, the fixed lines and the state after each turn.

    uv run --env-file .env python talk.py
    uv run --env-file .env python talk.py "book a haircut for Monday" "yes that's right"
    HARNESS=voice uv run --env-file .env python talk.py "my number is +34600111222"

`[fixed]` is a line this code speaks; `[model]` is the agent's own streamed
speech. Ctrl-D or an empty line ends an interactive session.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from dotenv import load_dotenv
from pipecat.frames.frames import LLMTextFrame, TTSSpeakFrame

from voice.processor import PydanticAgentProcessor

load_dotenv(override=True)
os.environ.setdefault('PYDANTIC_AI_NO_BANNER', '1')


class Transcript(PydanticAgentProcessor):
    """The processor with its downstream replaced by the terminal."""

    def __init__(self, call, agents):
        super().__init__(call, agents)
        self._lines: list[list[str]] = []
        self.ended = False
        call._end = self._hang_up

    async def _hang_up(self):
        self.ended = True

    async def push_frame(self, frame, direction=None):
        if isinstance(frame, TTSSpeakFrame):
            self._lines.append(['fixed', frame.text])
        elif isinstance(frame, LLMTextFrame):
            # Re-join the stream so the transcript reads as speech, not tokens.
            if self._lines and self._lines[-1][0] == 'model':
                self._lines[-1][1] += frame.text
            else:
                self._lines.append(['model', frame.text])

    def flush(self):
        for kind, text in self._lines:
            print(f'  [{kind:5}] {text}')
        self._lines.clear()


def agency():
    if os.getenv('HARNESS', 'salon') == 'voice':
        from voice.agents import AGENTS, GREETING
        from voice.state import Call

        return Call(), AGENTS, GREETING

    from salon.agents import AGENTS, GREETING
    from salon.state import open_call

    return open_call(os.getenv('CALLER_ID', '+34600111222')), AGENTS, GREETING


def turns():
    """The caller's turns: the command line, or typed in one at a time."""
    if len(sys.argv) > 1:
        yield from sys.argv[1:]
        return
    while True:
        try:
            said = input('\nCALLER: ').strip()
        except EOFError:
            return
        if not said:
            return
        yield said


async def main():
    call, agents, greeting = agency()
    transcript = Transcript(call, agents)
    print(f'  [fixed] {greeting}')
    for said in turns():
        if len(sys.argv) > 1:
            print(f'\nCALLER: {said}')
        started = time.monotonic()
        await transcript._turn(said)
        transcript.flush()
        summary = ' '.join(
            f'{name}={value!r}'
            for name, value in call.state.model_dump().items()
            if value
        )
        print(f'  -> {summary}  ({time.monotonic() - started:.1f}s)')
        if transcript.ended:
            print('  -> the agent ended the call')
            return


if __name__ == '__main__':
    asyncio.run(main())
