# Architecture guide

Start here even if you have never built an agent or read a graph. The guide follows one real salon call: the caller
books a haircut, then moves it. It shows what the caller hears, then the configuration and code behind it. You can
read chapters 01 and 02 without knowing Python or YAML.

The central idea: **each node does one job. Its agent reads the caller and chooses one typed decision. Application
code does the work and says every line it already knows. Core remembers the call and checks the rules.** Moving
between nodes does not start a new conversation.

## Reading order

| Chapter | The question it answers |
|---|---|
| [01 — Stack and boundaries](01-stack-and-boundaries.md) | What are an agent, a tool and a node? What does core add to Pipecat? |
| [02 — A conversation from start to finish](02-a-conversation-from-start-to-finish.md) | In one real call, what does the model choose, what does code say, and how many requests does it cost? |
| [03 — State, memory, records and services](03-typed-state-and-history.md) | Where does each kind of data live, who may write it, and what does each node's model see? |
| [04 — Nodes and agent.yaml](04-typed-flows-and-node-agents.md) | How do we design the nodes, write the manifest, and what does core build from it? |
| [05 — Operations, approval and recovery](05-tools-approval-and-recovery.md) | How does a change run safely, and what happens when we do not know whether it finished? |
| [06 — Running on LiveKit](06-livekit.md) | How does the same brain plug into LiveKit Agents, what does LiveKit tell it about the caller, and what is still missing? |

Read in order for a gradual introduction. Each chapter ends with a runnable offline check and links to the code.
Examples marked as excerpts need the other application files around them.

To go straight to the design rules, read [one job per node](04-typed-flows-and-node-agents.md#1-one-job-per-node).
For the four kinds of data, read [chapter 03, section 1](03-typed-state-and-history.md#1-the-four-kinds-of-data).

## Terms used throughout

| Term | Meaning here |
|---|---|
| Application / use case | A conversation definition and the business services behind it. The reference is the salon in `use-cases/salon`. |
| Manifest | The application's `agent.yaml`, loaded as a `FlowSpec`. Version 2 keys the steps by id. |
| Node / step | One job the caller wants done, such as moving a booking. It can span several turns. A step is its declaration in the manifest. |
| Flow | The nodes and allowed moves, built into a runnable application (`StepFlow`). |
| Call | One `FlowCall`, with its own facts, position, history, speech records and pending work. |
| Caller turn | One reply cycle. Pieces the caller says before an answer has played out join the same turn. It is not the same as a VAD segment. |
| Model request | One provider request. The target is one per caller turn. |
| Decision | The one typed output a node's agent returns per request: `reply`, `record`, an operation, `hand_over`, `goodbye`, or `done` for a consulted node. The caller's update rides on it. |
| Tool | An ordinary function the agent may call and get a result from in the same turn, such as `options` or a pure read. |
| Offer | The ids a caller may pick for a choice field, with the application's words for them. It stands only for the state it was made for. |
| Operation | A declared application action run through core's validation, approval and outcome tracking. It may be a read or a write. |
| Proposal | The exact action and arguments being read back for approval. |
| Intent | A tracked attempt at an operation, with a stable identifier and an outcome. |
| Receipt | The backend's answer, checked against the application's result model. |
| Playback evidence | A report that a speech segment finished playing. It supports a delivery decision. It does not prove a person understood. |

## Implementation status

Both applications on core, the salon and Faro, use manifest version 2, the only version core loads. Every
conversation node is structured: its agent answers with one typed output, and code says every receipt.

The salon's results on Coval, over the real phone line, are in the [root README](../../README.md#live-results). A
bare weekday name can still be read as the wrong week. The fix to the calendar text is planned, not made. Receipt
recovery has the routing limits described in chapter 05.

[Start with the stack and boundaries →](01-stack-and-boundaries.md)
