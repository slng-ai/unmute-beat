# Architecture guide

Start here even if you have never built an agent or read a graph. The guide follows one caller who wants to change a record, explains what they hear, and then shows the code and configuration behind it. You can read chapters 01–02 without knowing Python or YAML.

The central idea is simple: **the agent interprets and chooses, application functions do the work, and core remembers the call and checks the execution rules.** A node can ask several questions. Moving between nodes does not start a new conversation.

## Reading order

| Chapter | The question it answers |
|---|---|
| [01 — Stack and boundaries](01-stack-and-boundaries.md) | What are an agent, a tool and a node? What does the harness add? |
| [02 — A conversation from start to finish](02-a-conversation-from-start-to-finish.md) | Who asks for missing information, what happens after a lookup, and who writes the spoken reply? |
| [03 — Typed state and history](03-typed-state-and-history.md) | How does the assistant remember answers and handle corrections? First Python examples. |
| [04 — Typed flows and node agents](04-typed-flows-and-node-agents.md) | How do we configure those jobs, questions and routes? The YAML and graph implementation. |
| [05 — Tools, approval and recovery](05-tools-approval-and-recovery.md) | How do functions run safely, and what happens when we do not know whether a change finished? |

Read in order for a gradual introduction. Source links and runnable offline checks are at the end of each chapter; you can skip them on a first reading. Examples labelled as excerpts need the other application files described around them. Transport internals and a dedicated performance/concurrency guide are deferred.

For the two common questions, go directly to [asking naturally for missing information](02-a-conversation-from-start-to-finish.md#3-decide-who-writes-the-questions) or [answering after a lookup](02-a-conversation-from-start-to-finish.md#4-answer-after-the-lookup).

## Terms used throughout

| Term | Meaning here |
|---|---|
| Application / use case | A conversation definition and the domain services behind it. |
| Manifest | The application's YAML definition, commonly `agent.yaml`, loaded as a `FlowSpec`. |
| Step | An author's declared unit of work, such as `conversation` or `action`. |
| Node | One job in the conversation, implemented by an agent and/or code. It can span several turns. |
| Flow | The jobs and allowed routes, built into a runnable application (`StepFlow`). |
| Call | One `FlowCall`, with its own facts, position, history, speech records and pending work. |
| Caller turn | A reply cycle recognized by core; interrupted pieces may continue the same turn. It is not identical to a VAD segment. |
| Model request | One provider request. A caller turn may need none, one or several. |
| Tool | A model-visible function or output, such as `collect`, an operation or `hand_over`. |
| Operation | A declared application action executed through core's validation, approval and outcome tracking. It may be a read or a write. |
| Proposal | The exact action and arguments currently being read back for approval. |
| Intent | A tracked attempt at an operation, with a stable identifier and an outcome. |
| Receipt | The backend's answer, checked against the application's result model and rules. |
| Playback evidence | An acknowledgement associated with a speech segment. It supports a delivery decision, not proof of a person's comprehension. |

## Implementation status

This guide describes the implementation on `main` after [PR #32](https://github.com/slng-ai/slng-voice-harness/pull/32). Both salon and Faro use the public manifest path; the old classifier modules, `kind: agent` and application-defined `call:` classes have been removed. The [cleanup report](../research/reusable-core/core-cleanup.md#5-public-compatibility-changes) lists the replacements for those APIs.

The new Faro build has offline verification only. Its live grading accuracy is still unmeasured, and receipt recovery has routing limits described in chapter 05. [The documentation review](../research/reusable-core/pr32-architecture-review.md) records what was checked at this revision.

[Start with the stack and boundaries →](01-stack-and-boundaries.md)
