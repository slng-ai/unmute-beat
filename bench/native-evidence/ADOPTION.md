# What the typed POC must improve

Latest scope: preserve the POC and the selected native implementation. Compare
the closest equivalent business scenarios; do not redesign either tool surface
to manufacture exact parity. Differences are evidence to explain, not changes
to hide. Nothing in this update changes the production POC.

The selected repository remains clean at
`2ddc7b5c9f15e307a470ff9013200bea1a3000e9`. The existing 200-call comparison uses
the previously documented knowledge/configuration/business-service overlay,
**not an untouched build of that repository**. Its native Workers/Flows and
model-authored consent remain intact. No further guarantees are being ported
from the POC into native.

## Benefits and their evidence

| Dimension | Selected native example / earlier comparison | Current typed POC | What this establishes |
|---|---|---|---|
| Argument validation | Generated flow handlers consume argument dictionaries; business handlers perform their own checks. The compiler already uses Pydantic TypeAdapters for declared state and finish results. | Generated Pydantic validators check tool inputs; typed service models check responses before state changes. | A consistent validation boundary is a POC benefit. “Native has no typing or validation” would be false. |
| Approval authority | Booking receives model-authored `confirmed` / `additional`; phone readback and complaint approval depend partly on prompt compliance. | Approval comes from accepted caller speech and the application’s tracked question. Stored complaint wording is consumed after approval. | Stronger application enforcement, demonstrated by `test_consent` and `test_salon`; a model-generated boolean cannot authorize a write. |
| Readback and receipts | The model voices readbacks and narrates returned results. | Verification and complaints use structured output functions with tracked readbacks and deterministic receipts. | Removes the reproduced untracked-question and stalled-complaint paths. Booking still permits model speech and has only a partial claim detector. |
| Write recovery | Native already guards terminal flow tools against duplicate execution while repairing saved results. | Operation tokens, response/request reconciliation and an unresolved-write barrier cover booking and complaint writes. | Broader recovery coverage is tested in `test_salon`. Do not claim native has no duplicate protection or that all native failure paths have been compared. |
| Interrupted history | Native owns its own context/flow lifecycle. | Public SDK message boundaries retain accepted fragments and completed receipts; separate projections represent delivered speech. | POC regressions pass in `test_history` / `test_recovery`. Superiority over native under identical microphone interruptions is not established. |
| Latency | The retained architecture comparison has mixed scenario results. | Recent voice responsiveness is good, but the declared aggregate no-regression gate did not pass. | No universal speed advantage or completed migration case yet. |

These are properties of these implementations. Pipecat can support custom
validation and consent logic; the question is how directly and reliably the
chosen structures express them. Pydantic by itself does not prove consent,
prevent a wrong but well-typed result, or guarantee natural conversation.

## Comparison boundary

Use the same caller intentions and check the actual booking/complaint outcome,
approval timing, identity, returned receipt and continuity. Keep the existing
five scenarios and retain failures. Match model/configuration, knowledge and
fixtures where the documented overlay already does so. State remaining
differences in schemas, required confirmations, history and output handling.

The [tool audit](TOOL_CONTRACT_AUDIT.md) maps each operation. It remains true:
the tools are functionally related, **not identical model-facing contracts**.
The current user instruction does not authorize changing the POC to eliminate
that difference. Consequently, report end-to-end implementation behavior, not
isolated framework overhead or the independent causal effect of Pydantic.

Adoption requires reliable completion on the fresh microphone matrix, retained
receipt/input behavior under interruption, and acceptable latency on matched
audio. The current evidence does not close those gates. The existing consent
matcher also has a bounded vocabulary: the repaired “Go for it” case needs a
fresh microphone retest.

## Excluded shared-tool experiment

An optional adapter temporarily ran native Pipecat Workers/Flows over all the
POC's tool definitions, Pydantic validators, consent handlers and prompts. Its
final qualification passed **15/15 POC and 15/15 native calls**, three repetitions
of five scenarios. Sixteen prepared schema/configuration states matched, and
an offline native handoff/write regression passed. The handler-registration
negative control failed as expected. Two earlier diagnostic cohorts each
passed 15/15 POC and 12/15 native; all their failures are retained.

That experiment imported the very POC guarantees we want to evaluate and
replaced the generated native graph with four flow nodes in one worker. It is
therefore **excluded from the native-reference comparison**. Its adapter and
checks were removed from active source, and `bench/native_compare.py` restored
byte-for-byte to the measured reference runner. It was never deployed to the
voice server and added no dependency.

`parity-source.tar.gz`, `parity-snapshot.json`, `parity-qualification.json.gz`,
and `parity-*.txt` retain the experiment for inspection. The source archive is
the experiment, not the current runtime. Schema equality in that experiment
does not establish equality between the unchanged POC and native example.
