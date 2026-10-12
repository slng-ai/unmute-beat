"""Run: uv run python -m tests.test_actions

Operations written as classes (`core.Lookup`, `core.Write`), on the clinic
(`clinic_flow/classes.yaml`, `clinic_flow/actions.py`): one manifest line per
operation, and the same guarantees as the long form.

* a heard yes books once, an unheard read-back books nothing, and the model
  calling `book` again is not a yes;
* a `core.Rejection` member is a terminal no: nothing assigned, nothing claimed;
* an unknown outcome is settled by the class's `reconcile`, never sent twice;
* a union return still moves the caller by its receipt's `route`, and its
  `core.Rejection` member moves no one;
* an argument from inside a nested state field (`AliasPath`) is missing when
  that field is, and never taken from a same-named field;
* what cannot be inferred is refused at load, before any model: a success
  member without the route field among them;
* closing lines by result (`core.closing`): the case the receipt fits is said,
  a refusal is said by code only when a case names it, and lines that could
  never be reached are refused at load.

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


async def a_union_receipt_routes_on_success_only():
    text = (HERE / 'classes.yaml').read_text().replace('actions.py:Book\n', 'actions.py:RoutedBook\n')
    text += '  done: {kind: end, outcome: completed, say: Goodbye.}\n'
    for full in (False, True):
        script = Script()
        backend = Backend()
        backend.full = full
        flow = load_text(text, root=HERE).compile(model=script.model)
        call = script.call = flow.open(ClinicState(), services=backend)
        await turn(flow, call, script, 'P-1234, ten.', *FILL)
        no = (Reply('That time has gone, sorry.'),) if full else ()
        said = await turn(flow, call, script, 'Yes.', Call('book'), *no)
        assert call.state.agent == ('desk' if full else 'done'), (full, call.state.agent, said)


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


async def the_manifest_can_set_a_class_s_closing_lines():
    """`{class: ..., say: ...}`: the closing line is config, the rest is still the class's; a recap on a read is refused."""
    base = (HERE / 'classes.yaml').read_text()
    text = base.replace(
        '  book: actions.py:Book\n',
        '  book: {class: actions.py:Book, say: "All set for {when}, ref {reference}.", recap: "Booked {when}."}\n',
    )
    script = Script()
    flow = load_text(text, root=HERE).compile(model=script.model)
    call = script.call = flow.open(ClinicState(), services=Backend())
    await turn(flow, call, script, 'P-1234, Tuesday at ten.', *FILL)
    said = await turn(flow, call, script, 'Yes.', Call('book'))
    assert said[0] == 'All set for Tuesday at 10:00 AM, ref CL001.', said
    assert flow.recaps(call) and next(iter(flow.recaps(call).values())) == 'Booked Tuesday at 10:00 AM.'
    refused = base.replace(
        '  remind: actions.py:Remind\n', '  remind: {class: actions.py:Remind, recap: "Noted."}\n'
    )
    assert 'nothing of it to recap' in ' | '.join(load_text(refused, root=HERE).problems())


CASES = """  book:
    class: actions.py:Book
    say:
      - when: {slot: '0900'}
        line: Early one, {when}.
      - when: {status: full}
        line: Sorry, {when} has just gone.
      - line: Booked {when}.
"""


async def closing_lines_follow_the_result():
    """Each result has its own line: a matching case, the default, and a refusal said by code, not the agent."""
    base = (HERE / 'classes.yaml').read_text()
    text = base.replace('  book: actions.py:Book\n', CASES)
    for slot, full, expected in (
        ('0900', False, 'Early one, Tuesday at 9:00 AM.'),
        ('1000', False, 'Booked Tuesday at 10:00 AM.'),
        ('1000', True, 'Sorry, Tuesday at 10:00 AM has just gone.'),
    ):
        script = Script()
        backend = Backend()
        backend.full = full
        flow = load_text(text, root=HERE).compile(model=script.model)
        call = script.call = flow.open(ClinicState(), services=backend)
        fill = (FILL[0], Call('book', update={'slot': slot}))
        await turn(flow, call, script, 'P-1234, Tuesday.', *fill)
        then = (Reply('Shall I look for another time?'),) if full else ()
        said = await turn(flow, call, script, 'Yes.', Call('book'), *then)
        assert said[0] == expected, (slot, full, said)
        if full:
            assert said[1:] == ['Shall I look for another time?'], said
            assert 'The caller has just been told' in script.requests[-1].told, 'the agent knows it was said'
            assert call.intents[0].status == 'rejected' and call.state.facts.reference == ''
    silent = base.replace(
        '  book: actions.py:Book\n',
        CASES.replace('      - when: {status: full}\n        line: Sorry, {when} has just gone.\n', ''),
    )
    script = Script()
    backend = Backend()
    backend.full = True
    flow = load_text(silent, root=HERE).compile(model=script.model)
    call = script.call = flow.open(ClinicState(), services=backend)
    await turn(flow, call, script, 'P-1234, ten.', *FILL)
    said = await turn(flow, call, script, 'Yes.', Call('book'), Reply('That time has gone, sorry.'))
    assert said == ['That time has gone, sorry.'], 'no case names the refusal: the agent says it, as before'
    hidden = base.replace(
        '  book: actions.py:Book\n',
        CASES.replace('      - line: Booked {when}.\n', '').replace(
            '    say:\n', '    say:\n      - line: Booked {when}.\n'
        ),
    )
    try:
        load_text(hidden, root=HERE)
    except core.FlowSpecError as error:
        assert 'put it last' in str(error), error
    else:
        raise AssertionError('a default line hiding the cases after it was loaded')


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
    assert "routes by 'next', which Yes does not have" in problems('broken_actions.py:HalfRouted')
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
    asyncio.run(a_union_receipt_routes_on_success_only())
    asyncio.run(the_manifest_can_set_a_class_s_closing_lines())
    asyncio.run(closing_lines_follow_the_result())
    a_nested_argument_comes_from_its_own_path()
    what_cannot_be_inferred_is_refused_at_load()
    print(
        'actions ok: one line per operation, the same guarantees, refused at load when it cannot be inferred'
    )
