# Complaint-stall runtime repair evidence

Historical runtime-repair runs before the subsequent shared prompt simplification.
See the [combined report](../complaint-stall-20260921.md) and
[current prompt evidence](../prompt-simplification-evidence/README.md) for the final build.

`trace-excerpt.json` retains the relevant observations and protocol from
`c0b18b8715dc95019c929312074e8ea4`, including the discarded readback and normal
apology-only model stop. `snapshot.json` identifies source, prompts and dependencies;
the worktree was uncommitted on `c6e8cfd`.

| Check | Evidence |
|---|---|
| Paired handoff/readback, both orders; invalid readback still refused | `output-conflict.txt` |
| Old handoff code fails the new check | `negative-output-conflict.txt` |
| Acknowledgement continues within the same turn | `apology-followup.txt` |
| Plain `str` output fails the continuation check | `negative-apology.txt` |
| All 13 offline modules | `offline.txt` |
| Ruff, formatting, ty | `ruff.txt`, `format.txt`, `types.txt` |

Real Gemini 3.5 Flash Lite, priority tier, MINIMAL thinking, fresh in-memory
fixtures, fixed salon clock. No audio. Each case has three repetitions.

| Run | Nominal result |
|---|---:|
| `complaint-progress-20260921T185105Z.jsonl` | 7/12 |
| `complaint-progress-v2-20260921T185333Z.jsonl` | 12/12 |
| `complaint-progress-v3-20260921T185621Z.jsonl` | 9/12 |
| `complaint-progress-final-20260921T190107Z.jsonl` | 10/12 |
| `complaint-stall-recovery-20260921T185423Z.jsonl` | 23/24 |
| `complaint-stall-recovery-v3-20260921T185617Z.jsonl` | 21/24 |
| `complaint-stall-recovery-final-20260921T190112Z.jsonl` | 24/24 |

The final runtime-repair continuation failures were ordinary/policy replies in
the dated upcoming-booking complaint instead of structured readbacks. One reply
also promised a free upcoming booking. The later prompt work strengthened the
missing-date question assertion; these historical scores used the earlier check.
Intermediate success is not final acceptance, and a replay pass does not score
every spoken policy assertion.

Reproduction uses the current worktree, not these historical prompt revisions:

```bash
uv run python -m tests
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.recovery --repeats 3
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.continuations --repeats 3
```
