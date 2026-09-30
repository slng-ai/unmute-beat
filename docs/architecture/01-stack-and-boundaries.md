# 01 — Stack and boundaries

Suppose someone calls a hair salon and says, “I need to move my appointment.” A voice application needs to understand the request, check who is calling, find the booking, offer new times, make the change only once the caller agrees, and say what happened.

The **harness** is the reusable code that manages that conversation. The package is called `slng-voice-core`, and Python applications import it as `core`.

## Start with four words

| Word | Plain meaning | Example |
|---|---|---|
| Model / LLM | The language model that interprets words and generates a response | Understands that “move it to Tuesday” refers to the booking under discussion |
| Agent | A model with instructions and a set of tools it may use | A receptionist allowed to move the caller's booking |
| Tool | A function the agent can request, or a typed decision it returns | `move_appointment` with the picked time asks code to read the move back |
| State | The named values remembered for this call | The caller's number, the booking being moved and the new day |

A **tool call** is the model asking for something to run. It is not the model reaching into a database itself. Application code performs the work and returns the result.

## How is this different from an agent with tools?

It is still an agent with tools. You could give an ordinary agent a prompt saying “find the booking, ask for approval, then move it.” You would also need code that remembers which change is awaiting approval, handles corrections and decides what to do if a tool's answer is lost.

The harness provides those recurring rules so each application does not have to write them again:

| Part | Its job |
|---|---|
| Agent | Interpret the caller and choose a permitted next action |
| Application functions | Look up information or make changes in the application's database or API |
| Harness | Keep the call's state, check the action's requirements, track its outcome and continue the conversation |

For example, the agent can interpret “yes” as agreement. Core checks which question that reply answers and whether its read-back finished playing. The application's function moves the booking only after its declared requirements pass. A wrong interpretation of “yes” is still possible; the harness does not make language understanding perfect.

A well-built ordinary agent can implement these rules too. The purpose of this package is to reuse their implementation across applications. It is not a claim that adding a graph automatically makes an agent faster or more accurate.

## A node is a job, not a sentence

A **node** is one job the caller wants done, such as moving a booking. It has its own agent, instructions, visible facts and tools. A code-only node can perform a known action without asking a model to choose it.

The salon's `moving` node names the booking, asks for the new day, offers times, reads the move back and handles a correction. Those can be several conversational turns in the same node.

Split by the caller's job. Booking, moving and cancelling are three nodes, not one: each has one operation, a small prompt, and a first line code already knows. [Chapter 04](04-typed-flows-and-node-agents.md#1-one-job-per-node) gives the design rules.

A **graph** is a map of these jobs and the allowed moves between them. An **edge** is one allowed move. Core uses the map while the conversation runs. Node names have no special meaning to core: the same machinery can run a salon, a support desk or a language tutor.

You do not need a node for every tool. A lookup can be an operation inside the current node. [Chapter 02](02-a-conversation-from-start-to-finish.md) follows both the words and the work in a real salon call.

## The media framework still owns audio

A conversation needs more than a language model. It also needs to hear the caller and play the reply:

| Term | What it does |
|---|---|
| STT — speech to text | Turns the caller's audio into words |
| VAD — voice activity detection | Detects when someone is speaking |
| Endpointing | Decides when a pause should count as the end of the caller's speech |
| TTS — text to speech | Turns the reply's words into audio |
| Transport | Carries audio, such as a WebRTC browser connection or a Twilio phone call |

Pipecat is the media framework used by the implemented integration. It connects those components. The harness fits where the application would otherwise put its LLM service and conversation logic; it does not replace Pipecat's audio system.

```mermaid
flowchart LR
    Caller["Caller audio"] --> Hear["Pipecat: hear words and detect turns"]
    Hear --> Core["Harness: agent, state and flow"]
    Core <--> Backend["Application functions and database"]
    Core --> Speak["Pipecat: turn reply into audio"]
    Speak --> Out["Caller hears the reply"]
    Speak -.-> Evidence["Playback reports"]
    Evidence -.-> Core
```

The application connects Pipecat to core through an **adapter**, `PydanticAgentProcessor`. An adapter translates between the two components' events. The same core engine can also be driven with text, which is useful for tests without microphones or phone calls.

Sending audio and playing it are different events. The integration uses Twilio marks or browser playback reports to decide whether a particular read-back finished before the caller replied. These reports describe what the transport or page observed, not whether a person understood the words.

This division helps explain faults. A pause being treated as finished speech is an endpointing decision. Forgetting accepted words or repeating a write whose outcome is unknown is a conversation-runtime issue. One incident can involve both.

## What an application supplies

An **application**, also called a use case, defines the actual job: what a support case or a lesson is, which changes are allowed and how to store them. Its **backend** is the database or external service that performs that work.

| Application supplies | Harness does with it |
|---|---|
| Named facts and their valid types | Remembers a separate, checked state for each call |
| Prompts — written instructions for each agent | Runs the right agent for the current node |
| Tools and allowed next nodes | Restricts what that agent can request |
| Functions that read or change the backend | Checks their inputs and returned results |
| Choice functions for what may be offered | Keeps each offer tied to the facts it was made for, and says its words |
| Questions, approval rules and result wording | Tracks the exact question and speaks the configured result |
| Storage and a way to look up uncertain results | Recovers an operation when the backend supports it |

The author connects these pieces in a configuration file, `agent.yaml`. That file is called the **manifest**. Manifest version 2 keys the nodes by id, and each node's agent answers with one typed decision (`speech: structured`). Chapter 04 explains how to read it; you do not need to learn YAML to follow the conversation in chapter 02.

The reference application is the salon in [`use-cases/salon`](../../use-cases/salon/). Faro, a learning application, and the support desk in core's tests use the same public path. Core contains no booking, ticket or grading policy. The active node's own agent reads each reply.

## Where Pydantic fits

The implementation uses three related libraries for different jobs:

| Library | Its role here |
|---|---|
| Pydantic | Checks the shape and declared rules of data: a revision must be a number, for example |
| Pydantic AI | Runs the agents and their model requests and tools |
| Pydantic Graph | Runs the map of permitted moves between nodes |

“Typed” means that values and connections have declared forms that code can check. It does not mean that every model judgement is true. A grade can be one of the allowed values and still be the wrong grade.

Core turns its own manifest format into these library objects. Applications do not write a second, separate graph to keep in sync.

## Portability today

Text execution and the Pipecat adapter are implemented. **LiveKit integration is planned, not implemented.** A LiveKit adapter would need to translate its own interruption and playback events into the same core rules; compatibility is not automatic.

The public `core` import does not load credential files, create model clients, or import Pipecat or Langfuse. The application chooses its model when it builds the flow. Voice and tracing are optional integrations.

## Check this against the code

| Read | What to verify |
|---|---|
| [Public exports](../../core/src/core/__init__.py), [package dependencies](../../core/pyproject.toml) | The package boundary and optional integrations. |
| [Pipecat processor](../../core/src/core/processor.py), [turn engine](../../core/src/core/turn.py) | Frames are adapted outside the conversation engine. |
| [Public API tests](../../core/tests/test_public.py), [structured tests](../../core/tests/test_structured.py) | Public loading, and one typed decision per model response. |
| [Salon manifest](../../use-cases/salon/salon/agent.yaml), [salon tests](../../use-cases/salon/tests/test_business.py) | The reference application: one job per node, every request pinned. |
| [Reuse tests](../../core/tests/test_tickets.py), [flow-rule tests](../../core/tests/test_flow_rules.py) | Another domain and renamed fields; receipt routing, automatic operations and per-call language. |
| [Faro manifest](../../use-cases/faro-language-learning/faro/agent.yaml), [Faro tests](../../use-cases/faro-language-learning/tests/test_faro.py) | A learning application on the same public path, with its own grading and storage rules. |

[Reading guide](README.md) · [Next: a conversation from start to finish →](02-a-conversation-from-start-to-finish.md)
