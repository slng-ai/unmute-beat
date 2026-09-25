"""Run: uv run python -m tests.test_audio_timing

The audio timings on a recording made here: two channels of tone and silence
at known times, so every edge is known to the frame.

Asserts, no pytest, no fixtures, same as the rest.
"""

import math
import tempfile
import wave
from array import array
from pathlib import Path

from audio_timing import channels, intervals, timings

RATE = 8000


def recording(agent: list[tuple[float, float]], caller: list[tuple[float, float]], seconds: float) -> Path:
    """A stereo 16-bit file: a loud tone where each speaker talks, silence elsewhere. Agent on the left."""
    frames = array('h')
    for n in range(int(seconds * RATE)):
        t = n / RATE
        for spans in (agent, caller):
            loud = any(a <= t < b for a, b in spans)
            frames.append(int(8000 * math.sin(2 * math.pi * 440 * t)) if loud else 0)
    path = Path(tempfile.mkdtemp()) / 'call.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(frames.tobytes())
    return path


def near(found: list[tuple[float, float]], expected: list[tuple[float, float]]) -> bool:
    return len(found) == len(expected) and all(
        abs(a - x) <= 0.02 and abs(b - y) <= 0.02 for (a, b), (x, y) in zip(found, expected, strict=True)
    )


def test_timings() -> None:
    """A reply, a pause inside one turn, an agent who starts too early and a caller who cuts in."""
    agent = [(0.0, 1.0), (3.0, 5.0), (6.9, 8.0), (9.0, 11.0)]
    caller = [(1.5, 2.0), (2.2, 2.5), (5.5, 7.0), (10.0, 10.6)]
    rate, (left, right) = channels(recording(agent, caller, 12.0))
    found_agent, found_caller = intervals(left, rate), intervals(right, rate)
    assert near(found_agent, agent), found_agent
    assert near(found_caller, [(1.5, 2.5), (5.5, 7.0), (10.0, 10.6)]), (
        'a 0.2 s pause is one turn',
        found_caller,
    )
    measured = timings(found_agent, found_caller)
    assert [round(x, 1) for x in measured['response_latency']] == [0.5, -0.1], measured
    assert [(round(a, 1), round(b, 1)) for a, b in measured['overlaps']] == [(6.9, 7.0), (10.0, 10.6)], (
        measured
    )
    assert [round(x, 1) for x in measured['barge_in_stop_latency']] == [1.0], measured


if __name__ == '__main__':
    test_timings()
    print('audio timing ok')
