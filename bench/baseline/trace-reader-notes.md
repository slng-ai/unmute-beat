# The compiler's trace reader — same corrections, not applied here

Phase 01 lists three instrumentation targets. Two are in this repository and
are fixed. The third,
`~/.codex/worktrees/a809/unmute_cli/scripts/read_langfuse_trace.py`, is in the
Go compiler's worktree — a different repository, with its own commit
(`71304830c04614dacd0f6336e568b1162dfd1ed7`) and its own test story. **No file
outside this repository was changed.** This is the note that keeps the work
from being lost.

That script does a different job: it prints one call back as a transcript,
tool list, usage table and latency table, grouped by span *name*. It does not
compute per-turn rates, so it does not have the `turn_of` bug at all. What it
does share is the measurement vocabulary, and three things in it will disagree
with `bench/turns.py` as it now stands.

## 1. `print_latency` uses a different p50

```python
p50 = values[len(values) // 2]
```

That is the upper median for even `n`: at `n=6` it takes index 3 where nearest
rank takes index 2. `bench/turns.py` now uses `ceil(p*n) - 1` and prints the
convention beside every figure. Two numbers in one write-up computed two ways
cannot be compared. Either adopt `ceil(p*n) - 1` there too, or state the
convention in its output — but not neither.

## 2. `print_usage` counts STT and TTS as generations

```python
if span.get('type') != 'GENERATION' or not details:
    continue
```

It does bucket by span name and does label the TTS row `characters`, so a
reader can tell them apart — but the guard itself treats all three as the same
kind of thing. `bench/turns.py` now excludes `stt` and `tts` by name *and*
structurally (a speech span has no `agents` ancestor). If that script ever
grows a "requests" figure, it needs the same exclusion first.

## 3. The docstring's latency advice is right and should stay

```
`agent_turn` is the number a caller feels. Do not add `user_turn` to it:
the endpointing wait is already inside the turn, so summing the two counts
the same silence twice.
```

This is the same distinction `bench/turns.py` now makes between
`turn_first_word` (from the finished transcript, so endpointing and STT are
already spent) and the Pipecat `turn` span (which spans them, and is reported
as a labelled proxy). Worth keeping aligned in wording as well as in substance
when Phase 03 compares the two configurations.

## 4. `turn.user_bot_latency_seconds` contains the endpointing wait

Measured on trace `5dc6e0eeb2dcd9f86faef13ee7490346`. The gap between the
final STT observation ending and the `agents` span opening is **0.00s on
seven of nine caller turns and 1.4-1.5s on the other two**. Both of those two
are trailing utterances -- a sentence that drifts off rather than lands -- and
1.5s is exactly `SMART_TURN_STOP_SECS`. `LocalSmartTurnAnalyzerV3` judged them
INCOMPLETE and the fallback timer ran out. The same pattern, at the same
value, appears in the earlier trace `a0dd15b94dc9cb9a4c14795f324e2229`.

So a reader that attributes `user_bot_latency_seconds` to the agency is wrong
by up to the fallback on those turns, and right on the others -- which is the
worst shape for a bug, because the average looks plausible. The agency's own
time is the `agents` span; the difference is turn-taking and belongs to
whoever is tuning the analyser.

Concretely: on that call the slowest turn (4.23s user-to-bot) was 1.48s of
endpointing, 0.84s of handoff, 0.84s of a knowledge lookup, 0.67s to the first
token and ~0.4s of TTS. Only the middle two were ever the agency's to fix.

## Not a defect

`check_v4` in that script is a genuinely good idea this repository does not
have an equivalent of: it reads observations back and asserts the v4 contract
(one session, one trace, one root, every observation carrying a session id)
against a call that really happened. Worth borrowing in the other direction
when Phase 03 starts collecting comparative data.
