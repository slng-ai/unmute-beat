# Session 4b, stage B: runtime repairs (working report)

This is a working report. It is updated at every checkpoint. The sections marked "open" are not finished.

## Checkpoint

| Item | Value |
|---|---|
| Parent | PR #20, branch `feat/session-4b-evaluator`, handoff `c07891afdaa4987d4d3cc91e1f054fc02fb805ea` |
| This branch | `feat/session-4b-runtime` (renamed from `worktree-session-4b-runtime`, which Claude Code created) |
| PR #20 state at start | open, head `c07891a`, base `feat/session-4-node-agents-cc` (checked 26 September 2026) |
| Tested source SHA | open |
| Stage | step 0 done: setup, baseline, defects registered |

## Effective configuration

Installed from `uv sync --locked --all-packages`. The lockfile did not change.

| Package | Version |
|---|---|
| pydantic | 2.13.5 |
| pydantic-ai-slim | 2.46.0 |
| pydantic-graph | 2.46.0 |
| pipecat-ai | 1.11.0 |
| langfuse | 4.15.4 |
| Python | 3.14.5 |

Speech profile: `docs/research/reusable-core/session-3/speech-profile.v1.env`, unchanged.

## Baseline at the parent

Every member ran `ty check` and `python -m tests`, and all of them passed. One check failed: `ruff check` found one import-order error (I001) in `docs/research/reusable-core/session-3/package-scenario.py`. That error is inherited from the parent.

`python -m tests.defects` at the parent (`session-4b-runtime/defects-before.txt`):

```
KNOWN DEFECT (session 4b stage B1): a write cancelled after its commit is sent again
KNOWN DEFECT (session 4b stage B1): an invalid reconciliation answer clears the retry guard
KNOWN DEFECT (session 4b stage B2): a lookup receipt backs a goodbye that claims a booking
```

## Budgets

These live in `session-4b-runtime/budget.json`. Every billable action updates that file.

| Pool | Allowed | Spent |
|---|---:|---:|
| Text model requests | 100 | 0 |
| Text tokens | 250,000 | 0 |
| Coval repair calls | 20 | 0 |

## Decisions

Open.

## Gates

Open.
