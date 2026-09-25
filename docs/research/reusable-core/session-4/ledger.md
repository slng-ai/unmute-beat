# Session 4 ledger

## Starting point

| Item | Value |
|---|---|
| Parent | PR #18, `feat/session-3-authoring`, `074a6c290ec1273334190e72dc32279b4e289d6a` (open, checked 2026-09-25T17:45Z) |
| Grandparent | PR #17, `feat/session-2-public-boundary`, `47f41923064f57fa01847785a46d14c1999a1fd5` (open) |
| Branch | `feat/session-4-node-agents-cc`, PR target `feat/session-3-authoring` |
| Speech profile | `session-3/speech-profile.v1.env`, sha256 `a168116910c50ec399dd0ccb654ac103d87b8a8387faa78701b57da0e6f28444` (unchanged) |
| `uv.lock` | sha256 `ff362a07b142fca7c305b4f7607475ae9d6979e65264fce0809694e42a063f49` |
| Native reference pin | `550dd67c4bae37901d93e6af069c2fd556a59b9a022a3a2677e83272f06496af` (`matrix/scenarios.v1.json`) |
| SDK | pydantic-ai-slim 2.46.0, pydantic-graph 2.46.0, pipecat-ai 1.11.0 |

## Baseline checks on the parent

All green except one inherited lint problem: `docs/research/reusable-core/session-3/package-scenario.py` had an unsorted import block and one unformatted line. Fixed with `ruff check --fix` and `ruff format`, no behaviour change.

- `uv run ruff check .`: 1 error (the file above), then clean.
- `uv run ruff format --check .`: 1 file, then clean.
- `core`: `ty check` clean, `python -m tests` 10 modules ok.
- `salon-graph`: `ty check` clean, `python -m tests` 18 modules ok (9.4 s wall).
- `faro-language-learning`: `ty check` clean, `python -m tests` 1 module ok.

## Coval allowance

Session 4 has its own cap of 20 calls. Used so far: **1** (see the table below).

Inherited run reconciled by ID on 2026-09-25T17:45Z, no relaunch:

| Run | Simulation | State at Coval | Counted in |
|---|---|---|---|
| `DDxDkFaSDhpvNc4Yax7vE6` (session-3 bulk stress) | `E2n7Wg6ZYpzwJgcpvukHnt`, `EAZZjLcMCxSoGHjfe7X4kV`, `L2T4QnHbJcCoUDoKJGScY9` | COMPLETED, audio present | session 3 |
| same | `UAeZiGa7wzMijRpQmENSLq` | still IN PROGRESS, no audio, 2 h 24 min after launch; run status IN PROGRESS | session 3, **inconclusive** |

The stuck simulation stays inconclusive. The local bot for that run stopped long ago, so it cannot still be serving the call.

| # | Job | Run | Simulations | Result |
|---|---|---|---|---|
| 1 | complex stress (K3Xwqi7g, VSruLZYYyz3w7fkdUaheDR, 1x1, concurrency 1), revision 044057e | `GYZdoiie3dVF9VuguVRyQi` | 1: `fymMyWTiyz6eqfK3RwRTRX` (call CA09f5c0958d564d9782dc10947e8e421c) | **Failed, critical.** The number read-back was queued behind the new holding line, counted as heard when that line's audio ended, and cut by the caller about 2 s in; their restated digits then authorised the lookup (write audit: question asked turn 3, segment `5a0c789242ed:3:8`, heard "that's two seven three four five four"). Also: the runner exited on a Coval HTTP 429 during a status poll and took the bot down 68 s into the call (at booking). No booking was made. |

Call 1 stopped further launches. Repairs on the same branch before any other call: core counts a segment that joined playing audio as heard only once its own words had time to play (`CallContext.note_drained`, regression `tests.test_continuation:a_read_back_queued_behind_a_holding_line_is_not_heard_when_it_ends`, which fails with the rule disabled); `coval_sim` retries a rate-limited read instead of ending the job. Offline suites and N1-N13 rerun clean.

Reserve call 2, logged before launch: the same frozen complex case on the repaired revision. Question: with the read-back delivery repaired, does the fragmented number, the day change at the booking read-back and any interruption end with one Saturday booking and every write on a heard question? Expanded count: 1.
