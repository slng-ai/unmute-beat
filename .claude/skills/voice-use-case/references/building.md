# Writing the files

The salon is the working version of every file here:
[`use-cases/salon/salon/`](../../../../use-cases/salon/salon/). Open the
matching salon file before you write each one, and copy its shape.

```text
use-cases/<folder>/
  pyproject.toml     the member: copy the salon's, rename it
  bot.py             copy the salon's, change only what names the salon
  <pkg>/
    __init__.py      empty
    state.py         one Pydantic model: every fact the call keeps
    receipts.py      what the backend may answer, as core.Receipt models
    operations.py    the services dataclass, and one class per operation
    offers.py        choice functions (only if there are choices)
    capabilities.py  deferred knowledge (only if needed)
    prompts/         _voice.md, one .md per node, shared includes
    agent.yaml       the manifest
    app.py           SPEC, FLOW, open_call
  tests/             see testing.md
```

## pyproject.toml

Copy the salon's. Change `name` and `description`. Keep the same dependencies and the `[tool.ty]` overrides for
`tests/**`. Then run `uv sync --all-packages` from the repo root. Adding any other dependency needs the user's
yes first.

## state.py

```python
class ShopState(BaseModel):
    """The business facts of one call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    customer_phone: core.Phone | None = None
    customer: Customer | None = None
    """Set only by the lookup's receipt. Ordering requires it."""
    item: str | None = Field(
        None, description='The id of the item the caller picked, from the items on offer.'
    )
    delivery_day: Annotated[date, core.NotPast] | None = Field(
        None, description='The day they want it, as YYYY-MM-DD, worked out from the calendar you are shown.'
    )
```

1. **Plain Pydantic, with defaults.** No `core` import beyond the value types. No `CallContext` subclass.
2. **Types carry the rules.** Use `Literal[...]` for a fixed list, `core.Phone`, `Annotated[date, core.NotPast]`,
   `core.Quoted` for the caller's own words. A validator raising `PydanticCustomError` gives a stable error type
   that `locale.refusals` can word for the caller.
3. **`Field(description=...)` is prompt text.** The model reads it with the type. Say the format and where the
   value comes from ("from the items on offer", "from the calendar").
4. **A receipt can be a field.** A field typed as an operation's success receipt is set whole by that receipt,
   never by the model. Do not list it in any node's `fields`. Only one state field may have that receipt's type.
5. **Use a `date` or `time`, not text.** Core shows every node today and a labelled calendar
   (`current_datetime:`), so the model can pick a real date. No day-grammar regex.
6. **No word lists and no regex over the caller's words.** If you need to know something about what they said,
   make it a typed field the node records (the salon's `wants_refund: bool`).

## receipts.py

```python
class Placed(core.Receipt):
    status: Literal['placed']
    order_id: Annotated[str, core.Subject('order')]
    phone: core.Phone
    spoken: str
    revision: Annotated[int, core.Version()] = 1


class OutOfStock(core.Rejection):
    status: Literal['out_of_stock']
    phone: core.Phone
```

1. `core.Receipt` is strict and forbids unknown fields. Every backend answer is parsed into one before a field is
   read. A return annotation is not validation.
2. A change's success receipt marks its resource id with `core.Subject('<kind>')` and its version with
   `core.Version()`.
3. A `core.Rejection` subclass is an authoritative no. Nothing was done.
4. Give the receipt the fields its `say` and `recap` words need, such as `spoken`.
5. **Do not name a field after its type.** `date: date | None = None` hides the `date` type inside the class body.
   The package then fails to import, and the compile check shows it as dozens of confusing secondary errors. Use
   an alias, as the salon does: `Day = Annotated[date, ...]`, then `date: Day`.

## operations.py

```python
@dataclass
class Shop:
    """One call's services: the backend client. The model never sees it."""

    backend: Backend
    call: str


Outcome = Annotated[Placed | OutOfStock, Field(discriminator='status')]


class PlaceOrder(core.Write[Shop]):
    """Place the order for the item picked. Run only on the caller's yes to the read-back."""

    phone: core.Phone = Field(validation_alias=AliasPath('customer', 'customer_phone'))
    item: Annotated[str, StringConstraints(min_length=1)]
    matches = ('phone',)
    idempotent = True
    say = 'Your order {order_id} is placed. Anything else?'
    recap = 'Your order {order_id} is placed.'

    def run(self, shop: Shop, intent: core.Intent) -> Outcome:
        try:
            raw = shop.backend.place(self.phone, self.item, token=intent.id)
        except Exception as error:
            raise core.OutcomeUnknownError(f'the backend did not answer: {error}') from error
        return TypeAdapter(Outcome).validate_python(raw)

    def readback(self, shop: Shop) -> str:
        return f'Shall I order {shop.backend.name_of(self.item)}?'

    def reconcile(self, shop: Shop, intent: core.Intent) -> Outcome:
        return TypeAdapter(Outcome).validate_python(shop.backend.outcome_of(intent.id))
```

| Part | Meaning |
|---|---|
| `core.Write` | changes something. Always read back, runs only on a heard yes. |
| `core.Lookup` | reads. Read back only if it defines `readback`. Set `restated_confirms = True` when the same value said again is a yes. |
| fields | the arguments. Bound from the state field of the same name. Use `validation_alias` or `AliasPath` for another or a nested field. A missing source is a missing argument. |
| return type | the outcome. A discriminated union of receipts. `Rejection` members are a no. |
| `run(services, intent)` | does the work. Use `intent.id` as the backend's idempotency token. |
| `readback(services)` | the question the caller answers. Start it as a question ("Shall I..."), so it cannot sound like a confirmation. |
| `reconcile(services, intent)` | finds a lost answer by the intent's id. Without it, an unknown outcome is never retried. |
| `matches` | receipt fields that must equal the argument of the same name, so an answer about someone else counts for nothing. |
| `idempotent` | the backend deduplicates by the intent id, so an unknown intent may be sent again as itself. |
| `say`, `recap` | the receipt's words, formatted from its fields. `recap` is what a goodbye says. |
| `route` | the receipt field that names the next node, which must be a `Literal` of step ids. |

When the backend does not answer, raise `core.OutcomeUnknownError`. Do not guess a result. A plain `run` runs in a
worker thread, so a blocking client is fine.

`core.Intent` is what core issued for this action. `intent.id` is its unique id. `intent.args` is the arguments
as a JSON string, not a dict. `intent.subject` is the caller's identity value. The services may define
`owed(identity) -> list[core.Intent]`, to settle intents that an earlier, finished call left unanswered. The
salon's `Salon.owed` shows how. Leave it out for a stub backend that lives in memory.

An application with no operations, such as an information line, needs no `operations.py`, `receipts.py`,
services or `identity:`. Its `open_call` is `flow.open(State())`.

## offers.py

```python
class Offer(BaseModel):
    options: dict[str, str] = {}
    note: str = ''
    say: str = ''
    pick: str = ''


def items_in_stock(shop: Shop, state: ShopState) -> Offer:
    """What the caller may pick, and the words for it."""
    items = shop.backend.in_stock()
    if not items:
        return Offer(say='Nothing is in stock today. Can I help with anything else?')
    if len(items) == 1:
        return Offer(options=items, pick=next(iter(items)))
    return Offer(options=items, say=f'I have {", ".join(items.values())}. Which one would you like?')
```

| Field | Meaning |
|---|---|
| `options` | ids the caller may pick, with the words for each. Only an offered id is accepted. |
| `say` | what code says: the offer, or a question for a fact still missing ("Which day would you like?"). |
| `note` | what the model is told and the caller is not. |
| `pick` | the only option. Code records it with no question. |

Each application defines its own offer model with these field names. A choice function asks for what it still
needs by returning `say` with no `options`. That is how a level 3 node asks for a missing day with no model
request.

## capabilities.py

```python
refunds = Capability(
    id='refunds',
    description='The refund policy. Load it only when the caller asks about a refund or money back.',
    instructions=(Path(__file__).parent / 'prompts' / 'knowledge' / 'refunds.md').read_text(),
    defer_loading=True,
)
```

The model sees one catalog line. It loads the text only when asked, and it stays loaded for the call.

## prompts/

1. `_voice.md` is the persona, shared by every node through `agents: {name: {prompt: prompts/_voice.md}}`. Tone,
   length, how numbers are said. Everything is spoken by TTS: plain text, no lists, no markup.
2. One prompt per node, headed `## Your role: <node>`. Say the job, then which decision to use when:
   - `reply` to answer, with facts in `update`;
   - `record` when code speaks next (an offer, or a read-back via `reads_back`);
   - the operation with the pick in `update`; the same call again on a clear yes;
   - `reply` with `decline` on a clear no;
   - `hand_over` to each exit, by name, with when;
   - `done` in a consulted node.
3. Name only the outputs this node has. A node with no operation has no operation output. A node with no exits
   has no `hand_over`. The salon's tests check this.
4. Never tell the model to read an action back or confirm it is done. Code does both. Never list the facts or
   the offers in the prompt. Code adds them before every request.
5. "A yes with a change, a condition or a question is not a yes." Put this in every node that runs an operation.

Prompts need a live check before anyone claims they work. Do not edit the salon's prompts: they are the versions
the live runs measured.

## agent.yaml

Start from the level's skeleton in [levels.md](levels.md). Then:

1. Every step's `fields` must also be in its `context.facts`.
1. A `hand_over` update can carry only fields the node handing over may change. To pass a day from `front` to
   `booking`, list it in `front`'s `fields` too.
1. An operation under `carries_out:` must not also be under `operations:`. The loader refuses it ("pick one").
   `carries_out` runs after a `record` in that node. It does not run when the trigger field arrives in a
   hand-over's update. The node's agent then records it, which costs one request.
2. Every name must exist: files, classes, functions, fields, exits, operations. The loader checks all of them.
3. `identity:` names the state field for the caller (the phone). `caller_id=` in `open_call` fills it.
4. Set `app_version:` to today's date with a suffix, such as `'2026.09.30-v1'`. Traces record it.
5. Copy `model:` and the persona's `spec: model_settings` from the salon, unless the user names another model.

Run `scripts/check_flow.py` after every change to the manifest.

## app.py

```python
SPEC = core.load(Path(__file__).with_name('agent.yaml'))
GREETING = SPEC.greeting
FLOW = SPEC.compile()


def open_call(
    flow: core.StepFlow, from_number: str = '', backend: Backend | None = None, **facts
) -> core.FlowCall:
    shop = Shop(backend if backend is not None else Backend(), uuid4().hex[:12])
    return flow.open(ShopState(**facts), services=shop, caller_id=from_number)
```

`compile()` runs once per process. `open()` makes one isolated call per caller. The salon's `open_call` also takes
`key`, `settle` and `clock`. Add them when the bot or tests need them.

## bot.py

Copy the salon's `bot.py`. Then make one pass over it:

1. Change the imports from `salon.app` and `salon.diary` to your package.
2. Change `NAME` and the module docstring.
3. Replace the diary block (`DIARY_PATH`, `DIARY`, the clock pin) with your backend, or remove it.
4. Change the `open_call(...)` line in `run_bot` to match your `open_call`. Drop `settle=` and `key=` if your
   `open_call` does not take them.
5. Search the file for "salon" and "diary" until nothing is left.

Keep everything else: the playback wiring (`core.processor.playout`), the refusal of a line that reports no
playback, the silence reminder and the call ceiling. Without playback evidence no read-back can be approved, so
nothing can ever be booked.
