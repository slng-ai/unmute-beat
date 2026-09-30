"""Run: uv run python -m tests.test_actions

Operations written as classes (`core.Lookup`, `core.Write`), on the clinic
(`clinic_flow/classes.yaml`, `clinic_flow/actions.py`): one manifest line per
operation, and the same guarantees as the long form.

* a heard yes books once, an unheard read-back books nothing, and the model
  calling `book` again is not a yes;
* a `core.Rejection` member is a terminal no: nothing assigned, nothing claimed;
* an unknown outcome is settled by the class's `reconcile`, never sent twice;
* an argument from inside a nested state field (`AliasPath`) is missing when
  that field is, and never taken from a same-named field;
* what cannot be inferred is refused at load, before any model.

Asserts, no pytest.
"""

import asyncio
from pathlib import Path

from pydantic import ValidationError, create_model

import core
from core.flowspec import Registry
from core.flowspec import load as load_text
from core.steps import _args
from tests.clinic_flow import actions
from tests.clinic_flow.broken_actions import Yes
from tests.clinic_flow.clinic import Backend, ClinicState, Contact
from tests.script import Call, Reply, Script, turn

HERE = Path(__file__).parent / 'clinic_flow'


def opened(script: Script, backend: Backend | None = None) -> tuple[core.StepFlow, core.FlowCall]:
    flow = core.load(HERE / 'classes.yaml').compile(model=script.model)
    script.call = flow.open(ClinicState(), services=backend if backend is not None else Backend())
    return flow, script.call


FILL = (Call('options', update={'patient': 'P-1234'}), Call('book', update={'slot': '1000'}))
"""The patient recorded as the times are offered, then the pick read back: two requests."""


async def a_heard_yes_books_once():
    for heard in (False, True):
        script = Script()
        flow, call = opened(script)
        said = await turn(flow, call, script, 'P-1234, Tuesday at ten.', *FILL, heard=heard)
        assert said[-1] == 'Shall I book Tuesday at 10:00 AM for P-1234?', said
        said = await turn(flow, call, script, 'Yes.', Call('book'))
        if not heard:
            assert call.services.bookings == {}, 'a yes to a read-back nobody heard booked'
            continue
        assert call.services.bookings == {'CL001': ('P-1234', '1000')}, call.services.bookings
        assert said[0] == 'Booked for Tuesday at 10:00 AM, reference CL001.', said
        assert call.state.facts.reference == 'CL001', 'the explicit assign set the reference'
        [filed] = call.evidence
        assert filed.kind == 'booking' and filed.resource == 'CL001' and filed.version == 1, filed


async def a_rejection_is_a_terminal_no():
    script = Script()
    backend = Backend()
    backend.full = True
    flow, call = opened(script, backend)
    await turn(flow, call, script, 'P-1234, ten.', *FILL)
    said = await turn(flow, call, script, 'Yes.', Call('book'), Reply('That time has gone, sorry.'))
    assert 'Booked for' not in ' '.join(said) and call.state.facts.reference == '', said
    [intent] = call.intents
    assert intent.status == 'rejected', intent
    [filed] = call.evidence
    assert not filed.succeeded and filed.receipt['status'] == 'full', filed


async def an_unknown_outcome_is_reconciled_by_the_class():
    script = Script()
    backend = Backend()
    backend.failing = True
    flow, call = opened(script, backend)
    await turn(flow, call, script, 'P-1234, ten.', *FILL)
    said = await turn(flow, call, script, 'Yes.', Call('book'), Reply('Let me see.'))
    assert "couldn't confirm" in ' '.join(said) and backend.calls == 1, said
    said = await turn(flow, call, script, 'Did it work?', Reply('One moment.'))
    assert backend.calls == 1, 'reconciled by asking, never booked a second time'
    assert call.state.facts.reference == 'CL001' and 'Booked for' in ' '.join(said), said


def a_nested_argument_comes_from_its_own_path():
    spec = core.load(HERE / 'classes.yaml')
    flow = spec.compile(model='test')
    remind = spec._resolver(Registry()).operation('remind')
    assert remind.binds == {'phone': 'contact'} and remind.paths == {'phone': ('contact', 'phone')}
    call = flow.open(ClinicState(), services=Backend())
    try:
        _args(remind, call)
    except ValidationError:
        pass
    else:
        raise AssertionError('no contact yet, and the argument was found anyway')
    call.update(contact=Contact(phone='+34600111222'))
    assert _args(remind, call).phone == '+34600111222'
    assert actions.Remind.model_validate({'phone': '+1'}).phone == '+1', 'validated by name'
    try:
        actions.Remind.model_validate({'contact': {'phone': '+1'}})
    except ValidationError:
        pass
    else:
        raise AssertionError('the alias was accepted as a second name')


def what_cannot_be_inferred_is_refused_at_load():
    base = (HERE / 'classes.yaml').read_text()

    def problems(extra: str) -> str:
        text = base.replace(
            '  remind: actions.py:Remind\n', f'  remind: actions.py:Remind\n  broken: {extra}\n'
        )
        return ' | '.join(load_text(text, root=HERE).problems())

    assert 'is not a core.Lookup or core.Write' in problems('clinic.py:book'), problems('clinic.py:book')
    assert 'needs a discriminator' in problems('broken_actions.py:Undiscriminated')
    assert 'which the state does not have' in problems('broken_actions.py:NoSource')
    assert 'needs a readback method' in problems('broken_actions.py:Unread')
    assert 'is not a core.Receipt' in problems('broken_actions.py:PlainReceipt')
    assert 'needs a core.Version field' in problems('broken_actions.py:Unversioned')
    resolve = load_text(base, root=HERE)._resolver(Registry())
    held = create_model('Held', first=(Yes | None, None), second=(Yes | None, None)).model_fields
    assert (
        resolve._inferred_assign('x', [Yes], held) == {}
        and 'could each hold its receipt' in resolve.problems[-1]
    )
    assert resolve._inferred_assign('x', [Yes], {'first': held['first']}) == {'first': '*'}


if __name__ == '__main__':
    asyncio.run(a_heard_yes_books_once())
    asyncio.run(a_rejection_is_a_terminal_no())
    asyncio.run(an_unknown_outcome_is_reconciled_by_the_class())
    a_nested_argument_comes_from_its_own_path()
    what_cannot_be_inferred_is_refused_at_load()
    print(
        'actions ok: one line per operation, the same guarantees, refused at load when it cannot be inferred'
    )
