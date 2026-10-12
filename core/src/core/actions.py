"""Operations written as classes: the class is the argument model, its methods the business action.

    class LookupCustomer(core.Lookup[Salon]):
        phone: core.Phone
        say = 'Thanks, I have you.'

        def run(self, salon: Salon, intent: core.Intent) -> Customer: ...
        def readback(self, salon: Salon) -> str: ...

    # agent.yaml
    operations:
      lookup_customer: operations.py:LookupCustomer

One line in the manifest instead of a function and a dozen keys. What the
keys said is read from the class and its receipt:

- **Arguments** bind to the state field of the same name. A different or
  nested source is a Pydantic alias: `Field(validation_alias='verified_phone')`
  or `AliasPath('customer', 'phone')`. Validation is by name only
  (`validate_by_alias=False`), so the alias is where core looks and never a
  second name to accept: a missing `customer` is a missing argument, not a
  fallback to a state field that happens to be called `phone`.
- **Outcome** is the return type. A discriminated union of `core.Receipt`
  models (`Annotated[Saved | Declined, Field(discriminator='status')]`):
  members that subclass `core.Rejection` are an authoritative no, the rest
  a success. A lone receipt is always a success.
- **Resource and version** are `core.Subject` and `core.Version` markers on
  the receipt's fields.
- **Assignment**: the one state field typed as the success receipt gets the
  whole receipt. Two such fields are ambiguous and refused at load; write
  `assign` out then.

What cannot be inferred safely stays written out, as class variables:
`matches` (the receipt fields that must equal the same-named argument: who
the answer is about), `idempotent`, `route`, `say`, `recap`.

`readback` and `reconcile` are methods; a class that does not override
`reconcile` has none, and an unknown outcome is then never retried. `Write`
always needs a heard read-back and a yes. `Lookup` needs one only when it
defines `readback`.

The class is adapted into the ordinary operation engine
(`core.steps.FunctionOperation`), so the dispatch order, intents, receipts,
evidence and reconciliation are the same as for a declared function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from core.state import Intent
from core.values import Receipt

__all__ = ['Action', 'Lookup', 'Write']


class Action[S](BaseModel):
    """One business action's arguments, and the action itself. Subclass `Lookup` or `Write`, not this."""

    model_config = ConfigDict(extra='forbid', validate_by_name=True, validate_by_alias=False, frozen=True)

    changes: ClassVar[bool] = True
    """Whether it changes a business resource."""
    approval: ClassVar[Literal['readback', 'none'] | None] = None
    """`readback`: only after a heard read-back and a yes. None: `Write` always, `Lookup` when it has a readback."""
    say: ClassVar[str | Sequence[Mapping[str, Any]]] = ''
    """The receipt's speech, formatted from its fields: `You're all set for {spoken}.`

    Or lines by result, as `core.closing` reads them: `[{'when': {'verified': True}, 'line': ...}, ...]`.
    """
    recap: ClassVar[str | Sequence[Mapping[str, Any]]] = ''
    """What a goodbye may say about it later, formatted from the receipt. Only a change has one."""
    route: ClassVar[str] = ''
    """The receipt field naming the step a success moves the caller to."""
    idempotent: ClassVar[bool] = False
    """The backend dedups by the intent id, so an intent whose outcome is unknown may be sent again, as itself."""
    matches: ClassVar[tuple[str, ...]] = ()
    """Receipt fields that must equal the argument of the same name: an answer about someone else counts for nothing."""
    assign: ClassVar[Mapping[str, str] | None] = None
    """State field to receipt field (`'*'`: the whole receipt), set on success. None: inferred from the types."""
    restated_confirms: ClassVar[bool] = False
    """A lookup whose value, said again by the caller, is a yes to its read-back (the same phone number)."""

    def run(self, services: S, intent: Intent, /) -> Receipt:
        """Do it once, and return what the service answered. Raise `core.OutcomeUnknownError` when that is unknown."""
        raise NotImplementedError

    def readback(self, services: S, /) -> str:
        """The question the caller says yes to, in the application's words, asking from its first word."""
        raise NotImplementedError

    def reconcile(self, services: S, intent: Intent, /) -> Receipt | None:
        """What an intent whose outcome is unknown did, found by the intent: the receipt, or None when unknown yet."""
        raise NotImplementedError

    @classmethod
    def defines(cls, method: str) -> bool:
        """Whether this class, or a class between it and `Action`, gives the method its own body."""
        return getattr(cls, method) is not getattr(Action, method)

    @classmethod
    def needs_approval(cls) -> bool:
        """Whether a heard read-back and a yes must come first."""
        return cls.approval == 'readback' if cls.approval is not None else cls.defines('readback')

    @classmethod
    def spoken(cls) -> dict[str, Any]:
        """What the loader reads about speech and approval, as the declared form's keys: `approval`, `say`, `recap`."""
        return {
            'approval': 'readback' if cls.needs_approval() else 'none',
            'say': cls.say,
            'recap': cls.recap,
            'changes': cls.changes,
            'route': cls.route,
        }


class Lookup[S](Action[S]):
    """A read: it changes nothing, so any answer to it is final. A read-back only if it defines `readback`."""

    changes: ClassVar[bool] = False


class Write[S](Action[S]):
    """A change to a business resource: done only on a heard read-back and a yes to it."""

    changes: ClassVar[bool] = True
    approval: ClassVar[Literal['readback', 'none'] | None] = 'readback'
