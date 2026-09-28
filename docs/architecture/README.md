# Architecture guide

The harness runs the conversation inside a voice application. The application defines what the agent can do; core keeps track of what the caller said, what the agent knows and which actions are allowed. This guide starts with that division of work and gradually explains how it is implemented.

## Reading order

| Chapter | The question it answers |
|---|---|
| [01 — Stack and boundaries](01-stack-and-boundaries.md) | What does this package add, and what belongs to the application or media framework? |
| [02 — A conversation from start to finish](02-a-conversation-from-start-to-finish.md) | What happens between opening a call, receiving words, producing a reply and accepting the next turn? |
| [03 — Typed state and history](03-typed-state-and-history.md) | Where do facts live, who can change them, and what does the next agent request remember? |
| [04 — Typed flows and node agents](04-typed-flows-and-node-agents.md) | How do YAML, prompts and functions become executable nodes and legal routes? |
| [05 — Tools, approval and recovery](05-tools-approval-and-recovery.md) | How does a requested action become an authorized effect, and what happens when its answer is lost? |

Start with 01–02 for the mental model. Read 03–05 when authoring or reviewing an application. Transport implementation details and a dedicated performance/concurrency guide are deferred; this series covers their boundaries only.

## Terms used throughout

| Term | Meaning here |
|---|---|
| Application / use case | A conversation definition and the domain services behind it. |
| Manifest | The application's YAML definition, commonly `agent.yaml`, loaded as a `FlowSpec`. |
| Step | An author's declared unit of work, such as `conversation` or `action`. |
| Node | The compiled runtime unit: an agent and/or code, with entry behavior and allowed exits. |
| Flow | The compiled application (`StepFlow`) and its graph of permitted routes. |
| Call | One `FlowCall`, with its own facts, position, history, speech records and pending work. |
| Caller turn | A reply cycle recognized by core; interrupted pieces may continue the same turn. It is not identical to a VAD segment. |
| Model request | One provider request. A caller turn may need none, one or several. |
| Tool | A model-visible function or output, such as `collect`, an operation or `hand_over`. |
| Operation | A declared application action executed through core's validation, approval and outcome tracking. It may be a read or a write. |
| Proposal | The exact action and arguments currently being read back for approval. |
| Intent | A tracked attempt at an operation, with a stable identifier and an outcome. |
| Receipt | A typed backend result. Its declared fields determine whether it establishes success, refusal or an unknown outcome. |
| Playback evidence | An acknowledgement associated with a speech segment. It supports a delivery decision, not proof of a person's comprehension. |

[Start with the stack and boundaries →](01-stack-and-boundaries.md)
