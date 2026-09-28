# PR #32: implementation and architecture documentation review

Reviewed on 28 September 2026 against merged `main`, commit
`e0edb305c2c0dc2ca07faf9510c0e3045d42d24d`. This review updates documentation;
it changes no runtime code, application prompts, dependencies or evaluation thresholds.

## What the implementation establishes

- The six legacy modules (`classifiers`, `readings`, `conversation`,
  `interpreters`, `desk`, `decisions`) are absent. The manifest no longer
  accepts `kind: agent` or `call:`. Current applications use the public path.
- Faro has eight conversation steps and one action step, `help`. Its agent
  judges an answer; application functions save the attempt and apply learning
  rules. Core supplies no grading or booking policy.
- The new flow rules are exercised on a support desk as well as Faro:
  per-call locales, receipt destinations, operations triggered by collection,
  greeting read-backs and validation context containing accepted caller words.
- Approval and playback remain separate from model interpretation. The Faro
  launcher refuses a browser client without the required playback capability.

Sources: [manifest compiler](../../../core/src/core/flowspec.py),
[compiled steps](../../../core/src/core/steps.py),
[Faro manifest](../../../use-cases/faro-language-learning/faro/agent.yaml),
[Faro functions](../../../use-cases/faro-language-learning/faro/functions.py),
[flow-rule tests](../../../core/tests/test_flow_rules.py),
[Faro browser tests](../../../use-cases/faro-language-learning/tests/test_bot.py).

## Limits the guide now states explicitly

| Boundary | Verified behavior and implication |
|---|---|
| Receipt routing | `ConversationStep._approved` follows `route:` after immediate success. `ActionStep` instead follows its own control fields. The compiler accepting an operation's `route:` does not make an action step follow it. |
| Recovery | `FlowCall.settlement` reconciles the effect and its presentation but does not apply the receipt destination. A recovered call can remain at an earlier node. Faro's cleanup report already records the resulting placement-resume problem. |
| Collection-triggered operations | `carries_out` needs a successful collection-only response, a newly recorded trigger field and valid complete arguments. Other arguments can come from existing state. It selects the first eligible rule, not all matching operations. |
| Rendered speech | Free-form model output does not go straight to speech, but application receipt fields can contain model-written feedback. Rendering does not prove a grade or its explanation correct. |
| Locales | `language`/`locales` select core's fixed lines. They do not translate arbitrary application strings or change speech providers. |

Two additional provider-disabled probes on the routed support desk confirmed
the first two limits: an action with `then: resolve` followed that route even
when its receipt named `reopen`; a lost lookup response reconciled successfully
while the call stayed at `desk` rather than moving to `reopen`.

These are documentation corrections, not fixes for those runtime limits.
Receipt-driven continuation after recovery needs its own implementation and
regression before claiming complete recovery across node boundaries.

## Verification rerun for this review

From the isolated checkout of the merged revision, using its own locked uv
environment and provider requests disabled by the suite runners:

| Check | Result |
|---|---|
| `uv run --locked --directory core python -m tests` | 17 modules pass |
| `uv run --locked --directory use-cases/salon-graph python -m tests` | 21 modules pass |
| `uv run --locked --directory use-cases/faro-language-learning python -m tests` | 2 modules pass, including B1–B3 browser wiring |
| `uv run --locked --directory use-cases/concierge-tools python -m tests` | 5 modules pass |
| `uv run --locked ruff check .` | Pass |
| `uv run --locked ruff format --check .` | 316 files formatted |
| `ty check .` in core and Faro | Pass |
| Existing `session-4d-correctness/package-proof.sh` | Pass: wheel installed outside the checkout; clinic and support-desk scenarios |
| Cleanup's installed-wheel `package-check.py` | Pass: client assets, absence of retired modules and receipt routing |
| Executable examples in architecture chapters 02–05 | Pass with provider requests disabled |
| Local documentation links and heading anchors | Pass |

The [cleanup report](core-cleanup.md#verification) contains the original N1–N13,
N12 and headless Chrome results. This review did not rerun the timing benchmark
or the separate C1–C8 Chrome script. Its browser evidence comes from the core,
salon and Faro regression suites. The recorded N12 absolute ceiling passes;
the relative rule still fails. No new speed claim is made.

No live model requests, Coval calls, paid rescoring or trace writes were made.
Faro's new grading/onboarding accuracy remains unmeasured. Its limited
reconciliation coverage, prose coach and omitted spoken session recap remain
as described in [the migration report](core-cleanup.md#what-is-not-verified).
LiveKit and cross-browser qualification remain separate work.
