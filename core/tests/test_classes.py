"""The reusable base classes in `core`, proven on a made-up library desk rather than the salon.

If `core` only worked for the salon it would not be reusable, so everything
here is built from `core` alone: a two-field state, one proposal kind, one
operation using the default authorisation, its yes bound by
`CallContext.approve` as a step's agent binds it. The last check proves no
subclass loaded here overrides a `@final` method.
"""

import asyncio
import contextlib
import inspect

from pydantic import BaseModel, ValidationError
from pydantic_ai import ModelRetry

from core.operations import Operation, OutcomeUnknownError
from core.proposals import BaseProposal, Proposal
from core.state import CallContext, VoiceState


class Loans(VoiceState):
    agent: str = 'desk'
    member: str = 'M-1'
    renewals: int = 0


class LibraryCall(CallContext):
    state: Loans = Loans()

    def identity(self) -> str:
        return self.state.member


class Renewal(BaseModel):
    book: str


class Renewed(BaseModel):
    book: str
    due: str


class RenewLoan(Operation[LibraryCall, Renewal, Renewed, str]):
    """Uses the default `_authorise`: a pending `renew_loan` question for this book, and a yes bound to it."""

    name = 'renew_loan'
    args_type = Renewal
    receipt_type = Renewed

    def __init__(self, backend):
        self.backend = backend

    def proposal(self, args):
        return BaseProposal(about='renew_loan', value=args.book, spoken=f'Renew {args.book}?')

    async def _agreed(self, call, question, *, usage, usage_limits):
        return call.approved()

    async def _execute(self, call, args, grant):
        return self.backend(args.book)

    async def _record(self, call, args, receipt):
        call.commit(renewals=call.state.renewals + 1)
        return f'{receipt.book} is due {receipt.due}'


def asked(call: LibraryCall, proposal: Proposal, spoken: str | None = None, *, received: bool = True) -> None:
    """Put the question to the caller; `received` says the words reached them in full."""
    call.ask(proposal)
    call.note_said('fixed', spoken or proposal.spoken, origin='ask')
    if received:
        call.acknowledge(call.spoken[-1].id)


def the_ledger():
    call = LibraryCall()
    call.turn_started('hello')
    asked(call, BaseProposal(about='renew_loan', value='Dune', spoken='Renew Dune?', owner='M-1'))
    assert call.pending_question() is None, 'not answerable on the turn it was asked'
    call.turn_started('yes')
    assert call.pending('renew_loan', value='Dune') is not None
    assert call.pending('renew_loan', value='Emma') is None and call.pending('other') is None
    call.commit(member='M-2')
    assert call.pending_question() is None and call.last_proposal() is None, 'asked of somebody else'
    call.commit(member='M-1')
    call.turn_started('what?')
    assert call.pending_question() is None and call._asked is None, 'aged out after one turn'

    call.turn_started('hello')
    asked(call, BaseProposal(about='renew_loan', value='Dune', spoken='Renew Dune?'), received=False)
    call.note_heard('Renew')
    call.turn_started('yes')
    last = call.last_proposal()
    assert call.pending_question() is None and last is not None and last[1] is False, 'spoken over'
    call.answered()
    assert call._asked is None


async def the_default_operation():
    calls = []

    def backend(book):
        calls.append(book)
        return {'book': book, 'due': 'Friday'}

    renew = RenewLoan(backend)
    call = LibraryCall()
    call.turn_started('renew Dune')
    try:
        await renew.run(call, Renewal(book='Dune'))
    except ModelRetry:
        pass
    else:
        raise AssertionError('ran with no readback')

    asked(call, renew.proposal(Renewal(book='Dune')))
    call.turn_started('no')
    try:
        await renew.run(call, Renewal(book='Dune'))
    except ModelRetry:
        pass
    else:
        raise AssertionError('ran without agreement')
    assert not calls and call.state.renewals == 0

    call.turn_started('renew Dune')
    asked(call, renew.proposal(Renewal(book='Dune')))
    call.turn_started('Yes.')
    assert not call.approve('renew_loan', value='Emma'), 'a yes binds only to the question asked'
    assert call.approve('renew_loan', value='Dune')
    try:
        await renew.run(call, Renewal(book='Emma'))
    except ModelRetry:
        pass
    else:
        raise AssertionError('agreement to one book renewed another')
    assert await renew.run(call, Renewal(book='Dune')) == 'Dune is due Friday'
    assert calls == ['Dune'] and call.state.renewals == 1
    try:
        await renew.run(call, Renewal(book='Dune'))
    except ModelRetry:
        pass
    else:
        raise AssertionError('one answer authorised two actions')

    sent: list[str] = []
    broken = RenewLoan(lambda book: sent.append(book) or {'book': book})
    call = LibraryCall()
    call.turn_started('renew Dune')
    asked(call, broken.proposal(Renewal(book='Dune')))
    call.turn_started('yes')
    call.approve('renew_loan', value='Dune')
    try:
        await broken.run(call, Renewal(book='Dune'))
    except OutcomeUnknownError as error:
        assert isinstance(error.__cause__, ValidationError), error.__cause__
    else:
        raise AssertionError('an unreadable receipt reached _record')
    assert call.state.renewals == 0
    assert [i.status for i in call.intents] == ['unknown'], (
        'an unreadable answer after dispatch is not a refusal'
    )
    call.turn_started('renew Dune')
    asked(call, broken.proposal(Renewal(book='Dune')))
    call.turn_started('yes')
    call.approve('renew_loan', value='Dune')
    with contextlib.suppress(OutcomeUnknownError):
        await broken.run(call, Renewal(book='Dune'))
    assert sent == ['Dune'], 'an intent whose outcome is unknown was sent again'


def subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from subclasses(sub)


def nothing_overrides_a_final_method():
    """Every subclass loaded so far keeps the base's final methods. salon-graph's `test_flow` runs this with the salon loaded."""
    for base in (Operation,):
        final = {name for name, member in vars(base).items() if getattr(member, '__final__', False)}
        assert final, base
        found = list(subclasses(base))
        assert found, base
        for sub in found:
            overridden = final & set(vars(sub))
            assert not overridden, f'{sub.__name__} overrides {overridden}'
            assert not inspect.isabstract(sub) or sub.__module__.startswith('core.'), sub


async def main():
    the_ledger()
    await the_default_operation()
    nothing_overrides_a_final_method()
    print('classes ok: ledger, default operation, a yes bound to its question, final methods held')


if __name__ == '__main__':
    asyncio.run(main())
