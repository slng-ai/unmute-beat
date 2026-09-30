"""Run: uv run python -m tests.test_values

The value types core ships (`core.values`), the manifest clock
(`current_datetime:`), the caller ID and the locale's words for a refused
value. Asserts, no pytest.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from pydantic_ai.messages import RetryPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

import core
from core.flowspec import load as load_text
from tests.clinic_flow import actions
from tests.clinic_flow.clinic import Backend, ClinicState

HERE = Path(__file__).parent / 'clinic_flow'


class Booking(BaseModel):
    phone: core.Phone | None = None
    day: Annotated[date, core.NotPast] | None = None
    complaint: core.Quoted | None = None


def error_type(values: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    try:
        Booking.model_validate(values, context=context)
    except ValidationError as error:
        return error.errors()[0]['type']
    return ''


def a_phone_says_which_way_it_is_wrong():
    assert Booking(phone='+34 671 27 34 54').phone == '+34671273454'
    assert Booking(phone='+34671273454').phone.spoken == '+34 671 27 34 54'
    assert error_type({'phone': '671273454'}) == 'phone_no_country_code', 'no country is ever guessed'
    assert error_type({'phone': '+3467127345444'}) == 'phone_too_long'
    assert error_type({'phone': '+34671'}) == 'phone_too_short'
    assert error_type({'phone': '+999123456789'}) == 'phone_unknown_country'
    schema = Booking.model_json_schema()['properties']['phone']['anyOf'][0]
    assert schema == {'type': 'string', 'description': core.Phone.DESCRIPTION}, (
        'no pattern reaches a provider'
    )
    assert core.Phone.from_carrier('+34600111222') == '+34600111222'
    assert core.Phone.from_carrier('8656696') is None and core.Phone.from_carrier('') is None, 'withheld'
    assert core.Phone.from_carrier('+8656696123') == '+8656696123', 'a number that only begins like a word'


def dates_and_quotes_read_the_call():
    today = date(2026, 9, 29)
    context = {'today': today, 'said': ['the dye burnt my scalp'], 'before': Booking()}
    assert error_type({'day': '2026-09-01'}, context) == 'date_past'
    assert Booking.model_validate({'day': '2026-12-24'}, context=context).day == date(2026, 12, 24), (
        'no horizon'
    )
    assert Booking(day='2026-01-01').day == date(2026, 1, 1), 'no call, no today: nothing refused'
    assert error_type({'complaint': 'my haircut was uneven'}, context) == 'not_quoted'
    assert Booking.model_validate({'complaint': 'The dye burnt my scalp!'}, context=context).complaint
    held = {**context, 'before': Booking(complaint='kept from before'), 'said': []}
    assert Booking.model_validate({'complaint': 'kept from before'}, context=held).complaint, (
        'unchanged passes'
    )


def a_receipt_is_strict():
    class Saved(core.Receipt):
        status: str

    try:
        Saved.model_validate({'status': 'booked', 'extra': 1})
    except ValidationError:
        pass
    else:
        raise AssertionError('a receipt accepted a field nobody declared')


def the_manifest_sets_the_calls_clock():
    flow = core.load(HERE / 'classes.yaml').compile(model='test')
    noon = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    reads = [noon, datetime(2026, 9, 30, 10, 0, tzinfo=UTC)]
    call = flow.open(ClinicState(), services=Backend(), clock=lambda: reads.pop(0))
    assert call.today.at.tzinfo == ZoneInfo('Europe/Madrid') and call.today.at.hour == 12, call.today.at
    assert call.today.ahead == 7 and call.today.instructions().count('\n- ') == 7
    assert call.validation_context()['today'] == date(2026, 9, 29), 'read once at open'
    assert call.today.date == date(2026, 9, 29), 'the second reading never happened'
    text = (HERE / 'classes.yaml').read_text()
    hidden = load_text(text.replace('{timezone:', '{enabled: false, timezone:'), root=HERE).compile(
        model='test'
    )
    assert not hidden.open(ClinicState(), services=Backend())._today_shown
    try:
        load_text(text.replace('Europe/Madrid', 'Europe/Atlantis'), root=HERE)
    except ValidationError as error:
        assert 'is not a timezone' in str(error), error
    else:
        raise AssertionError('an unknown timezone loaded')


def a_caller_id_fills_a_phone_identity_only():
    text = (
        'version: 1\nid: caller\nstate: actions.py:CallerState\nidentity: phone\n'
        'agents: {a: {prompt: prompts/desk.md}}\nsteps: [{id: desk, kind: conversation, agent: a}]\n'
    )
    caller = load_text(text, root=HERE).compile(model='test')
    assert caller.open(actions.CallerState(), caller_id='+34600111222').state.facts.phone == '+34600111222'
    assert caller.open(actions.CallerState(), caller_id='anonymous').state.facts.phone is None, 'withheld'
    flow = core.load(HERE / 'classes.yaml').compile(model='test')
    try:
        flow.open(ClinicState(), services=Backend(), caller_id='+34600111222')
    except ValueError as error:
        assert 'not a core.Phone' in str(error), error
    else:
        raise AssertionError('a caller id filled a patient number')


class Script:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values
        self.retries: list[str] = []
        self.model = FunctionModel(stream_function=self.agent)

    async def agent(self, messages: Any, info: AgentInfo):
        self.retries += [
            str(part.content)
            for message in messages
            for part in getattr(message, 'parts', ())
            if isinstance(part, RetryPromptPart)
        ]
        if self.values:
            values, self.values = self.values, {}
            yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': values}))}
            return
        yield 'Could you give me your patient number?'


async def a_refusal_carries_the_locales_words():
    text = (
        (HERE / 'classes.yaml')
        .read_text()
        .replace(
            'agents:',
            'locale: {base: english, refusals: {string_pattern_mismatch: "Patient numbers look like P-1234."}}\nagents:',
            1,
        )
    )
    script = Script({'patient': 'P-12'})
    flow = load_text(text, root=HERE).compile(model=script.model)
    call = flow.open(ClinicState(), services=Backend())
    await core.text_turn(flow, call, 'It is P 12.')
    assert any('Patient numbers look like P-1234.' in retry for retry in script.retries), script.retries
    assert call.refusal_words() == 'Patient numbers look like P-1234.', 'kept for a rendered wait to say'
    assert call.state.facts.patient is None


if __name__ == '__main__':
    a_phone_says_which_way_it_is_wrong()
    dates_and_quotes_read_the_call()
    a_receipt_is_strict()
    the_manifest_sets_the_calls_clock()
    a_caller_id_fills_a_phone_identity_only()
    asyncio.run(a_refusal_carries_the_locales_words())
    print('values ok: phone, dates, quotes, receipts, the manifest clock, caller id, refusal words')
