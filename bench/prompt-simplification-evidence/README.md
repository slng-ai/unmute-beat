# Short prompt evidence — 2026-09-21

**Historical:** the user subsequently requested a goal-based complaint prompt,
then verification before both complaints and bookings. See the
[current flow evidence](../verification-first-evidence/README.md). The final
goal-only intermediate runs (`complaint-goal…`, 19:23:05–06) scored 22/24 recovery
and 6/12 continuation before the routing change. The table below records the
preceding prompt experiments and is not the current build's acceptance result.

**Final build: 13/13 offline modules; Ruff, formatting and ty clean. Real-model
recovery 22/24; continuation 10/12. Shorter prompts are implemented; improved
reliability and POC acceptance are not established.**

Final model runs are `prompt-roles-recovery-20260921T191752Z.jsonl` and
`prompt-roles-20260921T191753Z.jsonl`. Their source and prompt hashes match
`snapshot.json`. Each scenario was repeated three times with Gemini 3.5 Flash
Lite, priority service, MINIMAL thinking, fresh fixtures and a fixed salon clock.
Provider region remains unpinned. These are simulated text conversations, not
microphone acceptance.

## Final results

| Suite / cases | Result | Remaining failure |
|---|---:|---|
| Original recovery | 22/24 | One same-time move did not move; one complaint-verification conversation exhausted the hop budget. |
| Split phone | 3/3 | None in this sample; initial cancellations use a scripted model. |
| Complaint through verification | 3/3 | None in this continuation sample. |
| Upcoming booking, past visit date omitted | 2/3 | One ordinary redo question instead of a question about the missing date or a structured readback. |
| Upcoming booking, past visit date supplied | 2/3 | One ordinary confirmation instead of the structured readback. |

The failed recovery complaint was not recorded. The move remained on its prior
day. Write/approval guards do not guarantee conversational progress. The ordinary
redo wording can also imply that a future booking is free before that change is
approved; replay success alone does not prove every policy statement correct.

`offline.txt`, `ruff.txt`, `format.txt` and `types.txt` retain the final offline
results. `offline-initial.txt` retains two stale wording assertions: the approval
sentence changed, and a generic voice example contained a word the history test
used as its sentinel. The assertions now check the new state wording and the
actual previous caller sentence respectively; consent and history checks remain.

## All model runs, including failed experiments

| Label / UTC start | Continuation | Recovery | Change under test |
|---|---:|---:|---|
| `prompt-simple`, 19:10:58–59 | 10/12 nominal | 24/24 | Short shared and role prompts. Older, weaker missing-date assertion. |
| `prompt-simple-final`, 19:13:15–16 | 11/12 | 21/24 | Short handoff description and a concrete example outcome in the readback argument description. This was an unsuccessful experiment, despite the filename. |
| `prompt-outcome`, 19:14:56 | — | 3/3 focused | Removed the example outcome; restored the previous handoff description. Only `complaint_verify`. |
| `prompt-short`, 19:15:44–46 | 9/12 | 24/24 | Full suites after that correction. |
| `prompt-roles`, 19:17:52–53 | **10/12** | **22/24** | Final explicit role headings. |

`results.json` retains exact filenames, failures and all source, prompt and suite
hashes. The concrete free-redo example was repeatedly copied into a complaint
before the caller requested it; the final tool description requires caller-owned
wording and allows an unspecified outcome. Multiple instructions changed in that
experiment, so it is not a controlled measurement of a single phrase's effect.

Prompt iterations show variability, not monotonic improvement. The final result
supersedes earlier passing samples. No claim of a lower failure rate follows from
these small runs, especially across the strengthened missing-date check.

## Size and reproduction

`before/` preserves all six prior templates. `before-snapshot.json` points to the
preceding runtime repair; its noted lint-equivalent source difference predates this
prompt work. `prompt-sizes.json` measures rendered instructions, including policy
documents but excluding tool schemas and history. The shared voice file is 482 →
173 words; representative rendered role prompts shrink by 45–73%.

From `/Users/nicoferdi/.codex/worktrees/517a/slng_voice_harness`:

```bash
uv run python -m tests
uv run ruff check .
uv run ruff format --check .
uv run ty check .
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.recovery --repeats 3 --label prompt-recheck
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.continuations --repeats 3 --label continuation-recheck
```

The retained runs used the main checkout's existing Python virtual environment
through `uv run --no-project --python` to avoid adding dependencies. Model calls
use the existing `.env`; offline tests do not need it.

To load these prompts in a fresh microphone session, stop the old bot and restart:

```bash
SALON_FIXTURE=fresh uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python bot.py --port 7861
```

Open `http://localhost:7861/client/`. Book an appointment, then describe an uneven
past haircut and ask whether the upcoming visit can be free. Expect a useful
question or structured complaint readback after the apology, then one receipt
after approval. The booking must stay unchanged. Also try the same request with
the past visit date supplied in the first sentence. These fresh audio checks
have not been run by the assistant.
