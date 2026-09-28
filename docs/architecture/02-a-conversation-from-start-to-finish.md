# 02 — A conversation from start to finish

A conversation is one call with shared facts and history. Different nodes can handle different parts of it without starting a new session. This chapter follows the existing support-desk fixture through a successful request, then explains what changes when speech is interrupted.

The fixture speaks Spanish. The caller says the printer on ticket `T-101` now works and supplies account `ACC-1234`. The desired effect is to mark that ticket resolved. These identifiers and the resolution rule belong to the fixture, not core.

## 1. Build the definition, then open a call

The application loads its manifest with `core.load(...)`, compiles it with a model, and calls `flow.open(state, services=...)` for each session. The compiled node definitions can be shared; the opened call has its own application facts, flow position, history, proposals, speech records and intents.

For this fixture, the definitions are in [`agent.yaml`](../../core/tests/ticket_flow/agent.yaml), the facts are a `DeskState`, and the services are a `Desk` instance. Opening the call starts it at `faq`, as declared by the manifest. Application startup wires the call to its transport and greeting; opening a `FlowCall` itself does not start a microphone or phone connection.

## 2. Accept the caller's words

In a voice call, Pipecat's user aggregator supplies recognized input through an `LLMContextFrame`. `PydanticAgentProcessor` takes unseen user messages and runs core's turn engine. It also carries forward input that a cancelled run never retained.

The text entry point is `core.text_turn(flow, call, words)`. It runs the same engine and returns identified speech segments and a turn result. Returning those segments does not acknowledge their delivery.

Core records the caller's input and begins a generation of work. A generation identifies the active attempt so that older work cannot later move the conversation to an obsolete destination. A turn has a shared budget across node runs and model requests; handing over does not reset that allowance.

## 3. Let the active node interpret the request

The `faq` agent sees the caller's words and the destinations available from `faq`. In the scripted walkthrough, it selects `hand_over(to='desk', ...)` because the caller wants an existing ticket resolved. The graph checks the route, and `desk` becomes active.

The `desk` agent records the account, ticket and resolution with `collect`. These are declared writable fields. The ticket's backend revision is not one of them: it must come from a lookup result.

The agent hands over to `look_up`. This is an `action` step: code runs the function without asking a model to decide what to do. It calls `look_up_ticket` with arguments taken from the call's facts, validates the result, records the revision and status, and speaks the application's lookup line. Its `returns: true` sends execution back to `desk` by code.

The support-desk test drives that path within one caller turn. A different reply might cause the agent to ask for a missing field and remain on `desk` for another turn instead.

## 4. Ask for approval without performing the change

Back on `desk`, the agent selects `resolve_ticket`. Core binds its arguments from current facts and renders the application's read-back:

> ¿Marco el ticket T-101 como resuelto: la impresora ya imprime?

The pending proposal identifies this operation and its exact arguments. An intent exists, but the write has not been dispatched. The ticket is still open at revision 1.

The read-back is a fixed speech segment. In a voice call, the adapter sends it to TTS and the transport. The segment's delivery is established separately. This distinction prevents a fast model response from being mistaken for speech the caller had a chance to hear.

## 5. Read the next reply in the same conversation

```mermaid
sequenceDiagram
    participant Caller
    participant Media as Media adapter
    participant Core as Core and active node
    participant Backend as Application backend
    Caller->>Media: Request and identifiers
    Media->>Core: Accepted caller input
    Core->>Backend: Deterministic lookup
    Backend-->>Core: Typed lookup result
    Core-->>Media: Fixed read-back, no write
    Media-->>Caller: Synthesized read-back
    Media->>Core: Segment playback evidence
    Caller->>Media: Yes
    Media->>Core: Next reply and speech-onset timing
    Core->>Core: Interpret reply; check proposal and delivery
    Core->>Backend: Dispatch the approved intent
    Backend-->>Core: Typed result
    Core-->>Media: Receipt wording
    Media-->>Caller: Spoken result
```

The caller says “Sí.” The active node's agent interprets that reply and selects the operation again. Core checks that the reply answers the current question, with the same arguments and identity, after its read-back played. The operation then executes and returns a **receipt**: a validated backend result. This receipt sets revision 2 and status `resolved`.

The resulting line is rendered from that receipt:

> Ticket T-101 resuelto. ¿Algo más?

There is no second model request merely to paraphrase this result. The call can remain on `desk` for another request; completing an operation does not automatically end the call.

| Point in the walkthrough | Application facts | Backend effect |
|---|---|---|
| After collection | Account, ticket and caller's resolution are known; revision is not yet established | None |
| After lookup | Revision 1 and status `open` came from the backend | Read only |
| After read-back | Same facts; an exact proposal is pending | No resolution sent |
| After the approved result | Revision 2 and status `resolved` came from the receipt | One resolution committed |

## 6. When the caller interrupts

Pipecat signals the interruption. The processor notifies core before the interrupted task unwinds, and core retains accepted input and completed tool results. Cancelling generation does not mean a dispatched backend effect was cancelled; [chapter 05](05-tools-approval-and-recovery.md) follows that case.

If the caller starts speaking again before any non-filler answer has finished playing, core can treat the new words as another piece of the same caller turn. It joins the input, preserves the question the turn was answering where appropriate, and prevents the older task from changing the current route. A caller who speaks after an answer has finished is answering a new turn instead. With missing playback signals, the continuation decision is conservative; it is not a replacement endpoint detector.

A read-back whose delivery is unconfirmed cannot authorize a change. It may be repeated within bounded limits. An unheard result of an already completed operation may also be repeated, using the receipt rather than sending the operation again. The agent then reads any new request spoken over that receipt instead of discarding it.

Playback evidence says what the adapter can establish about a segment. It does not prove comprehension or physical speaker audibility. The detailed transport implementations are outside this chapter.

## Follow the example offline

From the repository root after `uv sync --all-packages`, this runs the existing successful walkthrough with provider requests disabled and a temporary database:

```bash
uv run --directory core python - <<'PY'
import asyncio
import tempfile
from pathlib import Path

from pydantic_ai import models

models.ALLOW_MODEL_REQUESTS = False

from tests.test_tickets import a_heard_yes_resolves_once

with tempfile.TemporaryDirectory() as folder:
    asyncio.run(a_heard_yes_resolves_once(Path(folder)))

print('Read-back first; one resolution after the acknowledged yes.')
PY
```

This fixture uses a scripted model and explicit text acknowledgements. It asserts the pending read-back, unchanged backend before approval, and one committed resolution afterward. It proves that execution sequence, not live Spanish understanding or audio delivery.

## Check this against the code

- [`StepFlow.open`](../../core/src/core/steps.py) creates the call; [`PydanticAgentProcessor`](../../core/src/core/processor.py) accepts media events and converts engine output to frames.
- [`Turn` and `text_turn`](../../core/src/core/turn.py) run the shared engine. [`CallContext.turn_started`, `continues_turn` and `commit`](../../core/src/core/state.py) manage joined input and generation checks.
- [`TO_READ_BACK`](../../core/tests/ticket_flow/worker.py) records the scripted node/tool sequence; [`a_heard_yes_resolves_once`](../../core/tests/test_tickets.py) checks the facts and effect.
- [Delivery tests](../../core/tests/test_delivery.py) and [Pipecat/text equivalence tests](../../use-cases/salon-graph/tests/test_core.py) cover the boundaries the text walkthrough does not exercise by itself.

[← Stack and boundaries](01-stack-and-boundaries.md) · [Reading guide](README.md) · [Next: typed state and history →](03-typed-state-and-history.md)
