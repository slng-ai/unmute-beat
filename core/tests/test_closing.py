"""Run: uv run python -m tests.test_closing

Closing lines (`core.closing`) on nested data, the way a receipt or a state holds it:

* `{customer.name}`, `{customer[name]}` and `{items[0].spoken}` read inside nested models and lists;
* a field the data does not have is said as nothing, in any format, never as an error;
* a case's `when` names a nested field by its path, and a missing one fits nothing;
* one line, a list of cases and nothing all read as cases (`Closing.of`), and a line's fields are its
  whole paths;
* an end step says a line filled from a nested state field, on the clinic;
* a nested state goes back whole: an edit inside it carries on, and one its type refuses names its
  whole path (`('contact', 'phone')`) and opens nothing.

Asserts, no pytest.
"""

import asyncio
from pathlib import Path

from pydantic import ValidationError

from core.closing import Case, Closing, FieldPath, Template
from core.flowspec import load as load_text
from tests.clinic_flow.clinic import Backend, ClinicState, Contact
from tests.script import Call, Script, turn

HERE = Path(__file__).parent / 'clinic_flow'
RECEIPT = {
    'status': 'existing',
    'customer': {'name': 'Ana', 'tier': {'level': 2}},
    'items': [{'spoken': 'Tuesday at ten'}],
    'price': 12.5,
}


def nested_fields_are_read_by_path():
    assert Template('Hi {customer.name}, level {customer.tier.level}.').fill(RECEIPT) == 'Hi Ana, level 2.'
    assert Template('Hi {customer[name]}: {items[0].spoken}.').fill(RECEIPT) == 'Hi Ana: Tuesday at ten.'
    assert Template('{{literal}} {price:.2f}').fill(RECEIPT) == '{literal} 12.50'
    assert FieldPath('items[0].spoken').parts == ('items', '0', 'spoken')
    assert FieldPath('customer.name').head == 'customer'


def a_missing_field_is_said_as_nothing():
    for line in ('{customer.email}', '{nowhere.at.all}', '{items[3].spoken}', '{missing:.2f}', '{status.x}'):
        assert Template(line).fill(RECEIPT) == '', line
    assert Template('Booked {when}.').fill({}) == 'Booked .'


def a_case_names_a_nested_field():
    closing = Closing.model_validate(
        [
            {'when': {'customer.tier.level': [2, 3]}, 'line': 'Welcome back, {customer.name}.'},
            {'when': {'customer.email': 'x'}, 'line': 'Never.'},
            {'line': 'Thanks.'},
        ]
    )
    assert closing.said(RECEIPT) == 'Welcome back, Ana.'
    assert closing.said({'customer': {'tier': {'level': 1}}}) == 'Thanks.'
    assert not Case(when={'customer.email': None}, line='x').fits(RECEIPT, refused=False), (
        'a field the receipt does not have is not a null one'
    )
    assert closing.refused({'customer': {'tier': {'level': 1}}}) == '', (
        'no case fits this refusal, and the line for any success is not for one: nothing is said'
    )
    assert closing.fields() == ('customer.name',)


def every_declaration_reads_as_cases():
    assert Closing.of('Booked {when}.').said({'when': 'today'}) == 'Booked today.'
    assert not Closing.of('') and not Closing.of(None)
    assert Closing.declared('one line') == 'one line', 'one line is kept as it was declared'
    assert isinstance(Closing.declared([{'line': 'x'}]), Closing)
    assert Template('{a.b} and {c[0]} and {a.b}').fields() == ('a.b', 'c[0]')
    try:
        Template('{unclosed').fields()
    except ValueError:
        pass
    else:
        raise AssertionError('an unclosed brace was read')


async def an_end_step_reads_a_nested_state_field():
    text = (HERE / 'classes.yaml').read_text().replace('actions.py:Book\n', 'actions.py:RoutedBook\n')
    text += '  done: {kind: end, outcome: completed, say: "Booked. We will text {contact.phone}."}\n'
    script = Script()
    flow = load_text(text, root=HERE).compile(model=script.model)
    call = script.call = flow.open(ClinicState(contact=Contact(phone='+34600111222')), services=Backend())
    fill = (Call('options', update={'patient': 'P-1234'}), Call('book', update={'slot': '1000'}))
    await turn(flow, call, script, 'P-1234, ten.', *fill)
    said = await turn(flow, call, script, 'Yes.', Call('book'))
    assert 'Booked. We will text +34600111222.' in said, said


async def a_nested_state_goes_back_whole():
    script = Script()
    flow = load_text((HERE / 'classes.yaml').read_text(), root=HERE).compile(model=script.model)
    call = script.call = flow.open(ClinicState(contact=Contact(phone='+34600111222')), services=Backend())
    await turn(flow, call, script, 'P-1234, ten.', Call('options', update={'patient': 'P-1234'}))
    saved = call.snapshot()
    assert saved.facts['contact'] == {'phone': '+34600111222', 'texts': False}, saved.facts
    moved = saved.facts | {'contact': {'phone': '+34611222333', 'texts': True}}
    resumed = flow.open(ClinicState(), services=Backend(), resume=saved, facts=moved)
    assert resumed.state.facts.contact == Contact(phone='+34611222333', texts=True)
    try:
        flow.open(
            ClinicState(), services=Backend(), resume=saved, facts=saved.facts | {'contact': {'phone': 'x'}}
        )
    except ValidationError as error:
        assert error.errors()[0]['loc'] == ('contact', 'phone'), error.errors()
    else:
        raise AssertionError('a nested edit the state refuses opened a call')


if __name__ == '__main__':
    nested_fields_are_read_by_path()
    a_missing_field_is_said_as_nothing()
    a_case_names_a_nested_field()
    every_declaration_reads_as_cases()
    asyncio.run(an_end_step_reads_a_nested_state_field())
    asyncio.run(a_nested_state_goes_back_whole())
    print('closing ok: nested fields by path, missing ones said as nothing, cases on nested fields')
