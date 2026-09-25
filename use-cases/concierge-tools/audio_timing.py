"""Who spoke when in a two-channel call recording, and the audio timings the Verification contract names.

    uv run python audio_timing.py call.wav            # a Coval recording: one speaker per channel

Speech is found by loudness alone: 20 ms frames above a threshold, gaps
shorter than `GAP` joined. The channel that speaks first is taken to be the
agent, because the agent greets. From the two speakers' intervals it reports:

- response latency: for each caller stretch, the agent's first start after
  the caller began (and before the caller's next stretch) minus the caller's
  end; negative when the agent began before the caller had stopped. A caller
  stretch split by a pause longer than `GAP` counts as two;
- overlaps: every stretch where both were speaking;
- barge-in stop latency: for each caller start inside agent speech, the agent's
  end minus the caller's start.

ponytail: an energy threshold, not voice detection. Breathing, line noise or a
quiet speaker move the edges by a frame or two (20-40 ms), and a threshold
that suits one recording may not suit another; `--threshold` is the knob.
Endpoint and transcript times are not in the audio: they come from the trace.
"""

import argparse
import sys
import wave
from array import array
from pathlib import Path

FRAME = 0.02
GAP = 0.3
"""Silence shorter than this inside one speaker's talk is a pause, not an end."""
THRESHOLD = 500
"""RMS on 16-bit samples above which a frame counts as speech."""


def intervals(samples: list[int], rate: int, threshold: float = THRESHOLD) -> list[tuple[float, float]]:
    """The stretches of one channel loud enough to be speech, in seconds."""
    size = int(rate * FRAME)
    found: list[tuple[float, float]] = []
    for index in range(0, len(samples) - size + 1, size):
        frame = samples[index : index + size]
        rms = (sum(s * s for s in frame) / size) ** 0.5
        if rms < threshold:
            continue
        start, end = index / rate, (index + size) / rate
        if found and start - found[-1][1] <= GAP:
            found[-1] = (found[-1][0], end)
        else:
            found.append((start, end))
    return found


def channels(path: Path) -> tuple[int, list[list[int]]]:
    """The sample rate and each channel's 16-bit samples."""
    with wave.open(str(path)) as audio:
        assert audio.getsampwidth() == 2, 'expects 16-bit PCM'
        count, rate = audio.getnchannels(), audio.getframerate()
        data = array('h', audio.readframes(audio.getnframes()))
    return rate, [list(data[c::count]) for c in range(count)]


def timings(agent: list[tuple[float, float]], caller: list[tuple[float, float]]) -> dict[str, list]:
    """The contract's audio measures from the two speakers' intervals. Raw values, nothing clamped."""
    response = []
    for index, (c_start, c_end) in enumerate(caller):
        following = caller[index + 1][0] if index + 1 < len(caller) else float('inf')
        replies = [a0 for a0, _ in agent if c_start < a0 < following]
        if replies:
            response.append(round(replies[0] - c_end, 3))
    overlaps = [
        (round(max(a0, c0), 3), round(min(a1, c1), 3))
        for a0, a1 in agent
        for c0, c1 in caller
        if a0 < c1 and c0 < a1
    ]
    stops = [round(a1 - c0, 3) for a0, a1 in agent for c0, _ in caller if a0 < c0 < a1]
    return {'response_latency': response, 'overlaps': overlaps, 'barge_in_stop_latency': stops}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='audio_timing.py', description='Audio timings of a two-channel call.'
    )
    parser.add_argument('recording', type=Path)
    parser.add_argument('--threshold', type=float, default=THRESHOLD)
    args = parser.parse_args(argv)
    rate, (first, second, *_) = channels(args.recording)
    one, two = intervals(first, rate, args.threshold), intervals(second, rate, args.threshold)
    agent, caller = (one, two) if (one or [(9e9, 0)])[0][0] <= (two or [(9e9, 0)])[0][0] else (two, one)
    print(f'agent speech  {len(agent)} stretches  {[(round(a, 2), round(b, 2)) for a, b in agent]}')
    print(f'caller speech {len(caller)} stretches  {[(round(a, 2), round(b, 2)) for a, b in caller]}')
    for name, values in timings(agent, caller).items():
        print(f'{name:22s} n={len(values)}  {values}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
