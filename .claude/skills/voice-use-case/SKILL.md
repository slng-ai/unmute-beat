---
name: voice-use-case
description: Design and build a voice application (a "use case") on this repo's slng-voice-core package, the node-type architecture where every node, fact, operation and exit is typed in agent.yaml. It interviews the user about what their calls must do, picks the right level (one node, hand-off, or typed nodes), agrees a node graph with them, then writes the state model, operations, choice functions, prompts, manifest, bot and offline tests, and checks them. Use it whenever someone wants a new voice agent or use case in this repo, wants to turn a prompt-only bot into core nodes, wants to add, split or re-wire nodes in an agent.yaml, or asks how to structure a conversation on core, even if they never say "skill" or "node".
---

# Build a voice use case on core

An application on core is one Python package plus one manifest. Core runs the call. The application declares
what the call knows, what it may change, and how its nodes connect. This skill gets that design right with the
user first, then builds it.

```text
interview -> pick a level -> agree the node graph -> build -> check -> hand over
```

1. **Interview.** Ask what the calls are for, what they change in a backend, and what facts they need.
2. **Pick a level.** Recommend one of three levels, with a reason from their answers.
3. **Agree the graph.** Show a node table and a diagram. Get a yes before writing code.
4. **Build.** Write the files in the order below, compiling after each step.
5. **Check.** ruff, ty, the offline tests, and the compile check. Paste the real output.
6. **Hand over.** Say what was built, what is stubbed, and what needs a live check.

Read these before you build. They are short and they are the ground truth for this repo:

- [`README.md`](../../../README.md): the authoring contract, with a shop example.
- [`docs/architecture/04-typed-flows-and-node-agents.md`](../../../docs/architecture/04-typed-flows-and-node-agents.md): nodes, offers, prerequisites.
- The reference build: [`use-cases/salon/`](../../../use-cases/salon/).
  Copy its patterns. Do not import from it.

Then read the reference file for the part you are on:

| File | Read it when |
|---|---|
| [references/levels.md](references/levels.md) | picking a level, and for the manifest skeleton of each level |
| [references/building.md](references/building.md) | writing state, receipts, operations, offers, prompts, `app.py`, `bot.py` |
| [references/testing.md](references/testing.md) | writing the offline tests and running the checks |

## 1. Interview the user

The user knows their business. You know core. The interview turns their business into typed pieces. Ask in
rounds with `AskUserQuestion`, two to four questions per round, and offer concrete options drawn from what they
already said. If the user already gave an answer in their request, do not ask it again. Write it down and move
on. If they cannot answer yet, pick a sensible default, say so, and mark it as an open question in the design.

**Round 1: the job.**

- What is the agent for, in one sentence? Who calls it?
- Does a call **change something** in a backend (book, order, cancel, record), or only answer questions?
- Is there a backend to call today, or should you write a stub (a SQLite file or an in-memory class)?
- Which language or languages? Core's own lines exist in English and Spanish only.

**Round 2: the actions.** For each thing a call can change or look up:

- What are its arguments? Which ones does the caller say, and which come from a lookup?
- What does the backend answer on success, and on a refusal? These become receipt models.
- How should it be read back to the caller before it runs? Write the sentence with them.
- If the answer is lost, can the backend be asked again by an id? Does it deduplicate by that id?

**Round 3: the facts.**

- What facts does the call collect? For each: its type and its rules (a date not in the past, a phone number,
  one of a fixed list, the caller's own words).
- Which facts are **picked from a list** the backend offers (a time slot, one of their bookings)? These become
  choice functions. A pick must be one that was offered, so the model cannot invent an id.
- Must the caller be identified first? By what: caller ID read back, a number, a reference?

**Round 4: the shape.**

- Walk through two or three real calls with them, from greeting to goodbye. The happy path, a change of mind,
  and a refusal.
- Are there separate jobs the caller might switch between mid-call (book, then move it)?
- Are there side questions a specialist should answer before returning the caller (hair advice, a policy)?
- Is there reference text, such as prices or a policy, that most calls never need?

Stop when you can fill the design table in step 3. More questions after that cost the user time for nothing.

## 2. Pick a level

Read [references/levels.md](references/levels.md). In short:

| Level | Shape | Choose it when |
|---|---|---|
| 1. One node | one conversation node with its own fields and operations | the call is open-ended: questions, maybe one simple action, no fixed order of facts |
| 2. Hand-off | a front node that routes, and specialist nodes with `exits` and `consults` | a few separate jobs, each simple, and the main risk is the wrong job |
| 3. Typed nodes | one job per node, code-said offers, prerequisites, read-backs | the call changes business data, collects facts in an order, or picks from backend offers |

**Recommend level 3 whenever a call changes business data.** It is where accuracy comes from: each request
carries one job's prompt, facts and outputs, and code says every line it already knows. Levels 1 and 2 are fine
for information lines, or as a first cut the user will split later. Say which level you recommend and why, in
one or two sentences tied to their answers. Let the user overrule you.

The add-ons in `levels.md` (consultation, code node, `carries_out`, receipt routes, languages, deferred knowledge)
combine with any level. Suggest one only when an answer from the interview calls for it.

Every level uses the version 2 contract only: `version: 2`, structured nodes, and class operations of one line
each. `levels.md` lists the rules and the older manifests in the repo not to copy from. The compile check refuses
anything else.

## 3. Agree the node graph

Before any code, show the user the design. Use this template:

```markdown
## <App name>: design

Level: <1, 2 or 3>, because <reason from their answers>.

| Node | Its one job | Fields it may change | Operation | First line (said by code, or "model") |
|---|---|---|---|---|

Operations:
| Operation | Kind (Lookup/Write) | Arguments (state field) | Read-back sentence | Success receipt | Refusal |
|---|---|---|---|---|---|

Choices: <field>: offered by <function>, rests on <fields>.
Prerequisites: <node> requires <fact>, satisfied by <node>.

(mermaid flowchart of the nodes: solid arrows for exits, dotted for prerequisites and consultations)

Open questions: <defaults you picked that they should check>
```

Ask the user to confirm or change it. Change the design, not the code, until they say yes. This is the cheapest
point to fix a wrong node split.

## 4. Build

Work in `use-cases/<folder>/`, with the package inside it (for example `use-cases/shop-orders/shop/`). The root
workspace already includes every folder under `use-cases/`. Follow
[references/building.md](references/building.md). Build in this order, because each file imports the one before
it:

1. `pyproject.toml` for the member, copied from the salon's and renamed. Then `uv sync --all-packages` at the root.
2. `<pkg>/state.py` and `<pkg>/receipts.py`.
3. `<pkg>/operations.py`: the services dataclass and one class per operation.
4. `<pkg>/offers.py`: the choice functions, if there are choices.
5. `<pkg>/prompts/`: `_voice.md`, then one prompt per node.
6. `<pkg>/agent.yaml`, then `<pkg>/app.py`.
7. Run the compile check. Fix every message it prints before you go on:

   ```bash
   uv run --directory use-cases/<folder> python ../../.claude/skills/voice-use-case/scripts/check_flow.py <pkg>/agent.yaml
   ```

   It prints each node's legal moves. Compare them with the diagram the user approved.
8. `bot.py`, copied from the salon and changed only where it names the salon.
9. `tests/`: see [references/testing.md](references/testing.md).

Keep to the repo's conventions (from `CLAUDE.md`): every comment is a docstring, single quotes, 110 columns, no
pytest, no new dependency without asking. A use case imports `core` and nothing from another use case.

## 5. Check

Run these in the new member, and paste the real output:

```bash
uv run ruff format . && uv run ruff check --fix .            # from the repo root
uv run --directory use-cases/<folder> ty check .
uv run --directory use-cases/<folder> python -m tests
```

Do not call the work done while any of them fails. Do not round a failure up to a pass.

## 6. Hand over

Tell the user, briefly:

1. What was built: the nodes, the operations, and the level.
2. What is a stub, such as a fake backend, and what the real one must answer.
3. What the offline tests prove: the runtime's rules on the real flow with a scripted model. They do not prove a
   live model reads callers right. Prompts need a live run (text or Coval) before anyone claims accuracy.
4. How to try it: `cd use-cases/<folder> && uv run bot.py`, then open http://localhost:7860.
