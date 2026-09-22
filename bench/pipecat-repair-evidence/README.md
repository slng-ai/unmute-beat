# Pipecat repair evidence, 2026-09-21

See [the complete status report](../poc-status-20260921.md). JSONL files retain all
benchmark attempts, including failures and build/configuration headers. The final
source digest is `1e1da902e1e35c44931dc2e07c0049940078fdd0956f1b81f0cf001cac827849`;
the final prompt digest is `810860122c99fc28a530f1fda95fb9d6a2c5e6277af43ff27a098e0c0bd9f723`.

Commands ran from `/Users/nicoferdi/.codex/worktrees/517a/slng_voice_harness`.
The existing main-checkout environment supplied the installed runtime, without a
dependency change:

```bash
UV_CACHE_DIR=/tmp/voice-diagnosis-uv PYTHONDONTWRITEBYTECODE=1 PYDANTIC_AI_NO_BANNER=1 \
uv run --no-project --python /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/python \
python -B -m tests > bench/pipecat-repair-evidence/offline.txt 2>&1

/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/ruff check .
/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/ruff format --check .
/Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/ty check .
```

Live commands used the same `uv run --no-project --python ...` prefix and
`--env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env` before
`python -B`. No credentials are included in this directory.

| Invocation | Retained log | Result |
|---|---|---|
| `-m bench.recovery --repeats 3 --label pipecat-repair` | `recovery.log` | 24/24, initial prompt |
| `-m bench.continuations --repeats 3 --label pipecat-continuations` | `continuations.log` | 3/6 |
| `-m bench.continuations --repeats 3 --label pipecat-continuations-v2` | `continuations-v2.log` | 4/6 |
| `-m bench.continuations --repeats 3 --label pipecat-continuations-v3` | `continuations-v3.log` | 6/6, final prompt |
| `-m bench.recovery --repeats 3 --label pipecat-repair-final` | `recovery-final.log` | 23/24, final prompt |
| `-m bench.recovery --repeats 3 --scenario move_same_time --label pipecat-move-followup` | `move-followup.log` | 2/3, final prompt |

Negative controls ran before the production fixes with real model requests disabled:
`tests.test_history.cancelled_fragments()`,
`tests.test_recovery.cancelled_queue_keeps_all_accepted_input(fragmented=True)` and
`tests.test_recovery.complaint_cannot_skip_readback()`. Their retained tracebacks show
the expected failures. `offline-initial.txt` is the first implementation's failed
template-schema check; `offline.txt` is the final passing suite. Nothing failed was
reclassified as a pass.

`trace-metrics-source.json.gz` contains metric-bearing observations for the 14
unique supplied traces, with original IDs, parentage, fetch completeness and source
hashes. It omits API headers and transcript/tool payloads; payload character/byte
counts and hashes were measured before removal. `live-outcomes.json` contains the
four voice calls' actual business outcomes/configuration without phone fields.
The full originals remain in Langfuse and the local ignored call records.

```bash
PYTHONPATH=. python bench/pipecat-repair-evidence/measure.py
```

This regenerates `metrics.json` using the existing tested `bench.turns` parser.
Missing cache fields, TTFT, turn parentage and client playout remain missing.
`snapshot.json` hashes the final source, prompts, test/benchmark scripts and lock;
its own hash is intentionally excluded.
