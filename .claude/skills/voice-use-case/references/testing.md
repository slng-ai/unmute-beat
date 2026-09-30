# Offline tests and checks

The tests drive the real compiled flow with a scripted model. The script says which decision the model makes
each turn. The test then checks what the caller heard, where the call is, and the backend's end state.

Scripted models prove the runtime's rules: read-backs, consent, offers, routing, one write per yes. They never
prove that a live model reads callers right. Say so when you hand over.

```text
tests/
  __main__.py     runs every module with model requests switched off
  script.py       the scripted model, and helpers to open a call and take a turn
  test_flow.py    the cases, as async functions, run from `if __name__ == '__main__'`
```

No pytest and no fixtures. Each module is an assert script. It prints `ok` or raises on the first failure.

## tests/__main__.py

Copy the salon's ([`tests/__main__.py`](../../../../use-cases/salone-concierge-graph-new/tests/__main__.py)) and
change `MODULES`. It sets `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` before any module is imported, so a
test that forgot the scripted model raises instead of billing.

## tests/script.py

Copy the salon's ([`tests/script.py`](../../../../use-cases/salone-concierge-graph-new/tests/script.py)). Change
the imports, the backend, the clock and the seed data, and the docstrings that name the salon or the diary. The
parts that matter:

```python
@dataclass
class Script:
    moves: list[tuple[Any, ...]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    call: core.FlowCall | None = None

    def __post_init__(self) -> None:
        self.model = FunctionModel(self.reading, stream_function=self.agent)

    async def agent(self, messages, info):
        """Each request takes the next move: ('call', tool, args) or ('calls', [(tool, args), ...])."""
        ...

async def turn(flow, call, script, words, *moves, heard=True) -> list[str]:
    """One caller turn. Every segment reaches the caller unless `heard` is False."""
    script.moves = list(moves)
    reply = await core.text_turn(flow, call, words)
    if heard:
        call.acknowledge(*(segment.id for segment in reply.segments))
    return [segment.text for segment in reply.segments]
```

1. `SPEC.compile(model=script.model)` builds the real flow with the scripted model in every node.
2. `core.text_turn` runs one caller turn and returns what was said.
3. `call.acknowledge(...)` is the text stand-in for playback evidence. Without it, a read-back was never heard, and
   a yes to it approves nothing. `heard=False` tests exactly that.
4. `call.commit(agent='<node>')` puts a call straight into a node, for a test that starts mid-call.
5. Pin the clock (`clock=`) so dates in the test never move.

A move is the decision the model makes. On a structured node:

```python
('call', 'reply', {'say': 'We open at nine.', 'update': {'item': 'mug'}})
('call', 'record', {'update': {'delivery_day': '2026-10-02'}})
('call', 'place_order', {'update': {'item': 'mug'}})     # first call: code reads it back
('call', 'place_order')                                    # on a heard yes: it runs
('call', 'hand_over', {'to': 'returns', 'because': 'a return'})
('call', 'reply', {'say': 'What would you like instead?', 'decline': True})
('call', 'done', {'answer': 'Cotton, machine wash at 30.'})
```

## The cases to write

Write one async function per case, named as a sentence. Cover these for every operation that changes something:

1. **The flow compiles and its moves match the design.** `Flow.of(flow.nodes).moves` equals the diagram the user
   approved.
2. **A heard yes runs it once.** Read back, acknowledge, yes: the backend holds one change, and the caller heard
   the receipt's `say` words.
3. **An unheard read-back approves nothing.** Same turns with `heard=False`: the backend is unchanged.
4. **A second yes changes nothing more.** The backend still holds one change.
5. **A no changes nothing.** `reply` with `decline`: the backend is unchanged.
6. **A pick that was not offered is refused.** An invented id in the update does not reach the backend.

For level 3, also:

7. **Each node says its own first line.** Hand over, and check the words are the choice function's `say`, with
   one request for the turn (the hand-over's), not two.
8. **A prerequisite is interposed and returns.** Hand over to a node that `requires` a fact. The call goes to
   `satisfied_by`, then comes back once the receipt sets the fact.
9. **A whole call's requests per turn.** Count `script.requests` per caller turn. The target is one.

For level 2, also check each exit and consultation: the call lands in the right node, and a consulted node's
`done` returns the caller.

Check the backend's end state, not only the words. A test that only compares strings passes on a call that booked
twice.

## Prompts name only their outputs

A node's prompt must not name an output it lacks, such as an operation it does not have or `collect`, which a
structured node never has. After a scripted call, check each request: every operation named in its node's prompt
is in `request.tools`. The salon's `a_call_verifies_reads_and_books_once` shows the idea.

## Golden requests (optional)

Pinning each node's first request in a golden file shows every prompt or schema change in a diff. The salon does
it in `requests_are_pinned`, blessed with `BLESS_PAYLOADS=1`. Add it once the prompts settle, not while the design
is moving.

## Commands

```bash
uv run ruff format . && uv run ruff check --fix .                             # repo root, every member
uv run --directory use-cases/<folder> ty check .
uv run --directory use-cases/<folder> python -m tests                         # offline, no key needed
uv run --directory use-cases/<folder> python ../../.claude/skills/voice-use-case/scripts/check_flow.py <pkg>/agent.yaml
```

All four must be clean. Paste the real output.

1. `--fix` sorts imports. Ruff treats `core` and your package as one first-party group, so the salon's order is
   wrong for a package whose name sorts before `core`. Let `--fix` decide.
2. One module on its own needs a placeholder key: `GOOGLE_API_KEY=x uv run python -m tests.test_flow`. Only
   `python -m tests` sets it for you.

## Live checks

Offline tests do not measure accuracy or latency. Before any claim about either, the user runs live calls:

- in the browser: `cd use-cases/<folder> && uv run bot.py`, then http://localhost:7860;
- on Coval, with `coval_sim` (see `CLAUDE.md`). Live runs are billable. Ask before launching one.

Judge live calls by the backend's end state and the traces, not only by transcripts.
