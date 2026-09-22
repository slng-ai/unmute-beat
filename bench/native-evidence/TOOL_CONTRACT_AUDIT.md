# Tool contract audit

21 September 2026. No production tools or prompts changed during this audit.
The previous booking-tool experiment remains reverted. The “Go for it” repair
is retained.

The current comparison does **not** have one-to-one model-visible tools. Each
arm exposes 12 distinct names across the inspected states; only `save_booking`
and `record_complaint` share a name, and both have different argument contracts.
Sharing the underlying diary does not make the model-facing interfaces equal.

| Operation | Current POC | Current native reference | Material difference |
|---|---|---|---|
| Availability | `read_diary(when, service, at='')` | `find_slots(date, service, prefer)` | Different names, argument names and required/default fields. POC also checks caller date provenance and falls back to the accepted utterance for a time preference. |
| Booking write | `save_booking(action, slot_id, booking_id)` | `save_booking(action, slot_id, booking_id, confirmed, additional)` | Native requires two model-authored booleans. POC obtains authority from caller speech and the pending question. |
| Customer lookup | `confirm_customer(phone)` | `find_or_create_customer(phone)` | Different name and phone schema. POC checks a tracked readback and caller agreement before lookup; native relies on the prompt for the readback procedure. |
| Complaint write | `record_complaint()` | `record_complaint(requested_resolution, summary)` | POC consumes stored approved wording. Native asks the model to supply wording again. |
| Questions/guidance | `ask_number`, `verification_guidance`, `ask_booking`, `ask_complaint`, `complaint_guidance` | Spoken directly by the model | These POC tools have no native counterpart. They also change whether another model request is needed. |
| Routing/termination | `hand_over`, `stop_verifying`, `hang_up` | `book`, `verify_customer`, `to_complaints`, `to_concierge`, `finish_*`, `end_call` | Different graph interfaces and finish/return behavior. |

Return contracts also differ. POC `confirm_customer` returns a handoff after
validated lookup, and `save_booking` supplies receipt speech as its output.
Native lookup/save handlers return dictionaries consumed by the generated flow.
POC complaint output is `{recorded, reference}` and speaks a fixed receipt;
the native adapter returns `{complaint_id, status, complaint}` for subsequent
model narration. These are implementation facts from the handlers, not results
of a differential handler test.

## Evidence and scope

Run from the repository root with the existing POC interpreter:

```sh
uv run --no-project \
  --python /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.venv/bin/python \
  python -B bench/native-evidence/check_tool_contracts.py
```

The script reuses the existing offline native configuration probe. It supplies
dummy credentials, prevents Google client construction, disables Pydantic AI
network requests, and stops each POC request before any tool handler executes.
It captures seven POC states, including complaint recording before and after
approval, and reads native direct-tool schemas and the generated flow declarations.

[`tool-contracts.json`](tool-contracts.json) retains full schemas, tool
descriptions, text-output availability and source hashes. The POC captures
prepared first-request definitions; native flow schemas are declarations, not
an observation of every runtime request. This is enough to disprove equality,
not enough to prove future execution or provider-wire parity.

The prior 200-call cohort remains useful evidence about those two frozen
implementations. It cannot isolate runtime overhead with equal tools. The latest
user instruction preserves both the POC and native example, comparing them as
closely as their existing interfaces allow. Do not add POC readback, validation
or routing capabilities to native merely to obtain schema equality. Match business
scenarios and report the interface differences as part of the implementation
comparison. See the [adoption assessment](ADOPTION.md).

Before measurement, inspect actual provider-bound tool declarations and exercise
matching handler inputs, refusals and receipts. Equal names alone are insufficient.
The original acceptance conditions—correct outcomes, interruption/history behavior,
matched latency and fresh audio—remain open wherever the current report says so.
