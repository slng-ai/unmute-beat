# Complaint stall and prompt simplification — 2026-09-21

The reported silence was a completed model response with no next action, not
missing TTS audio. The repair handles conflicting output tools and acknowledgement-only
complaint replies. The follow-up simplifies all four salon prompts and their shared
voice/diary instructions. Fresh microphone acceptance is still required.

**Latest user-directed flow:** general questions need no identification; both
booking and complaint requests verify first, then resume the requested step.
Already verified callers go directly to it. This supersedes the earlier complaint
readback-before-verification rule. The [current evidence](verification-first-evidence/README.md)
records **13/13 offline modules, 23/24 recovery cases and 6/12 continuation cases**.
Verification order is enforced; complaint conversation quality is still open.

## What the trace shows

[Call c0b18b…](https://cloud.langfuse.com/project/cmq0t92kl024oad0e43tdq77q/traces?traceId=c0b18b8715dc95019c929312074e8ea4)
contains this sequence after the caller asks whether the next booking can be free
because the previous haircut was uneven:

| UTC | Event |
|---|---|
| 18:41:27.430 | The initial caller fragment enters protocol history; Pipecat interrupts the attempt. |
| 18:41:34.463 | The restarted attempt receives the remaining sentence. The earlier fragment is retained. |
| 18:41:34.468 | Concierge hands over to complaints. |
| 18:41:35.311 | Complaints emits both `hand_over(concierge)` and `ask_complaint`. The first output succeeds; the SDK skips the readback. |
| 18:41:36.198 | Concierge hands back to complaints. |
| 18:41:36.831 | Complaints generates only “That's not okay, I'm sorry to hear that.” with a normal stop. |
| 18:41:37.337 | TTS starts that apology. The agent attempt finishes as `answered`, after four model requests. |

The retained tool receipt explicitly says `Output tool not used - a final result
was already processed.` Pydantic AI's first successful output ended that run;
allowing plain text then made the apology a valid terminal answer in the last run.
The run did not time out or exhaust its budget. The previous fragment-retention
repair held on this call.

The caller's later “Okay” restarted progress. The call eventually recorded one
complaint, CX0001, with three total writes: a booking, its move, and the complaint.
The upcoming booking remained Saturday at 10:00. Eventual completion does not make
the stalled exchange acceptable.

The focused [trace and protocol extract](complaint-stall-evidence/trace-excerpt.json)
records the source/prompt hashes, all 177 observations' retrieval completeness,
and the relevant protocol receipts. Contact fields are omitted from the extract.

## Runtime repair

- A handoff paired with the current step's readback is refused before it takes
  ownership. The SDK can accept the valid readback already present in that response,
  with no additional model request. Invalid readbacks still require repair.
- Complaint text uses Pydantic AI `TextOutput` validation. A narrow English
  acknowledgement-only check requests the missing continuation within the existing
  turn budget. Text continues to stream; the rejected acknowledgement may already
  have been heard, so the retry asks the model to continue without repeating it.
- Existing approval, identity, one-write and exact-wording checks remain in code.
  Endpointing, streaming settings, dependencies and Pipecat ownership are unchanged.

The text check is deliberately limited: it does not prove that every arbitrary
answer advances the complaint. A policy-only answer or a redundant question can
still pass it. A caught acknowledgement costs another model request; it does not
add a fixed delay to other turns.

The [Pydantic AI output documentation](https://pydantic.dev/docs/ai/core-concepts/output/)
describes output tools and text validation. No LiveKit runtime or Pydantic harness
package was added.

## Simpler prompts

Each request now receives a short shared identity/voice guide, its role's task
and relevant salon facts, current call state, then the latest accepted speech.
That speech is explicitly a continuation of conversation history when fragmented.
Static guidance precedes call-specific fields; cache reuse is not assumed or claimed.

Removed repeated personality rules, transfer explanations, punctuation micromanagement,
and duplicate descriptions of code-owned confirmation and validation. The shared
voice file fell from **482 to 173 words**. Readbacks and receipts retain their
application-owned wording; the model has freedom over ordinary questions and answers.
The complaint prompt was further reduced, at the user's request, to handling the
complaint from salon policy and guiding the caller toward resolution. Identification
is already complete when it starts. The complaint readback tool states its trigger directly. Its arguments continue
to distinguish a caller-requested outcome from an unspecified one.

Rendered instruction sizes for an identified caller with an upcoming booking:

| Agent | Before, words | After, words | Reduction |
|---|---:|---:|---:|
| Concierge | 2,103 | 805 | 62% |
| Booking | 1,718 | 473 | 72% |
| Verification | 943 | 311 | 67% |
| Complaints | 1,490 | 717 | 52% |

These are whitespace-separated words, including relevant policy documents but
excluding tool schemas and conversation history, not provider token counts.
[Current size data](verification-first-evidence/prompt-sizes.json) identifies that
call state; [previous prompts](prompt-simplification-evidence/before/)
are retained for comparison.

The user's pasted agent/voice guides informed the separation of role, workflow,
tools and call facts. The [LiveKit voice article](https://livekit.com/blog/prompting-voice-agents-to-sound-more-realistic)
informed the use of concrete spoken examples. Its repeated filler, SSML and emotion-tag
recipes were not adopted: the requested direction was less instruction, and those
features have not been validated on this call's TTS configuration. XML message or
reasoning tags from the pasted examples are also inappropriate for this direct
text-to-speech output.

## Verification and remaining acceptance

The final build passes all **13 offline modules**, Ruff, formatting and ty.
The last real-model sample passes **23/24 recovery cases** and **6/12 continuation
cases**. Phone continuation and the initial complaint-through-verification scenario
pass 3/3 each. One recovery complaint failed completion, and all six upcoming-booking
complaints missed the structured readback; some also assumed free-redo eligibility
or promised a free booking. No claim that shorter prompts improved reliability
is justified. The exact paired-output and apology-only defects have executable
negative controls; broader complaint progress remains open.

See the [current flow evidence](verification-first-evidence/README.md), the
[prompt experiments](prompt-simplification-evidence/README.md), and the earlier
[runtime repair evidence](complaint-stall-evidence/README.md) for negative controls.
The offline checks cover both orders of paired output tools, invalid readbacks,
acknowledgement continuation, cancellation, history and consent.

The original recovery suite uses fixed caller turns. The continuation suite also
checks structured approval, unchanged bookings and a single complaint receipt.
Its missing-date assertion was tightened: a mention of “fourteen days” in policy
does not count as asking when the haircut occurred. Older nominal pass counts
used the weaker check and must not be treated as a strict matched quality comparison.

Real-model replay is text only. It establishes neither microphone endpointing nor
client audibility. Shorter instructions alone do not establish a latency improvement;
the earlier measured voice medians of 1.149 versus 1.154 seconds remain historical
pre-repair samples. Client playout and a matched native Pipecat comparison remain open.
