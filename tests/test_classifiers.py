"""Offline contracts for generic classification and the salon's decision boundary."""

import asyncio
from contextlib import suppress
from datetime import datetime
from typing import Literal
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimitExceeded, UsageLimits

from core.classifiers import Classifier
from core.interpreters import RuleInterpreter
from core.policies import TurnBudget
from core.turn import Barge, take_turn
from salon import agents, diary
from salon.classification import CLASSIFIER, classify_reply
from salon.models.consent import Asked, ReplyClassification
from salon.models.state import SalonCall, open_call
from salon.tools.asking import Ask
from tests.classifier_model import classifier_model


class Input(BaseModel):
    text: str = Field(min_length=1)


class Output(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: Literal['one', 'two']


class Nested(BaseModel):
    answer: Output
    count: int = Field(gt=0)


async def generic_contracts():
    seen = []

    def respond(messages, info):
        seen.append(messages)
        assert len(messages) == 1 and len(messages[0].parts) == 1
        assert not info.function_tools and not info.output_tools
        assert info.model_request_parameters.output_mode == 'native'
        assert info.model_request_parameters.output_object.json_schema == Nested.model_json_schema()
        assert info.instructions == 'Classify the input using the class descriptions in the output schema.'
        return ModelResponse(
            parts=[TextPart('{"answer":{"label":"one"},"count":2}')],
            usage=RequestUsage(input_tokens=12, output_tokens=8),
        )

    classifier = Classifier(FunctionModel(respond), Input, Nested)
    usage = RunUsage()
    result = await classifier.classify(
        Input(text='first'), usage=usage, usage_limits=UsageLimits(request_limit=1)
    )
    assert result.answer.label == 'one' and result.count == 2
    assert usage.requests == 1 and usage.input_tokens == 12 and usage.output_tokens == 8
    try:
        await classifier.classify(
            Input(text='second'), usage=usage, usage_limits=UsageLimits(request_limit=1)
        )
    except UsageLimitExceeded:
        pass
    else:
        raise AssertionError('classifier exceeded its shared request budget')
    assert len(seen) == 1
    try:
        await classifier.classify(Input.model_construct(text=''))
    except ValidationError:
        pass
    else:
        raise AssertionError('invalid input reached the model')
    assert len(seen) == 1

    def other(messages, info):
        assert Nested.model_validate_json(messages[0].parts[0].content).count == 2
        return ModelResponse(parts=[TextPart('{"text":"different input and output models"}')])

    second = Classifier(FunctionModel(other), Nested, Input)
    assert (await second.classify(result)).text == 'different input and output models'


async def invalid_answers_and_cancellation():
    for parts, finish in (
        ([], 'stop'),
        ([TextPart('not JSON')], 'stop'),
        ([TextPart('{"label":"unknown"}')], 'stop'),
        ([TextPart('{"label":"one","extra":true}')], 'stop'),
        ([TextPart('{"label":"one"}'), TextPart('{"label":"two"}')], 'stop'),
        ([ToolCallPart('write', '{}')], 'tool_call'),
        ([TextPart('{"label":"one"}')], 'length'),
    ):

        def invalid(messages, info, parts=parts, finish=finish):
            return ModelResponse(parts=parts, finish_reason=finish, usage=RequestUsage(input_tokens=3))

        usage = RunUsage()
        try:
            await Classifier(FunctionModel(invalid), Input, Output).classify(Input(text='input'), usage=usage)
        except UnexpectedModelBehavior, ValidationError:
            pass
        else:
            raise AssertionError('invalid model output was accepted')
        assert usage.requests == 1 and usage.input_tokens == 3

    def failed(messages, info):
        raise ModelHTTPError(503, 'offline')

    usage = RunUsage()
    try:
        await Classifier(FunctionModel(failed), Input, Output).classify(Input(text='input'), usage=usage)
    except ModelHTTPError:
        pass
    else:
        raise AssertionError('provider error was hidden')
    assert usage.requests == 1

    entered = asyncio.Event()

    async def slow(messages, info):
        entered.set()
        await asyncio.Event().wait()

    usage = RunUsage()
    task = asyncio.create_task(
        Classifier(FunctionModel(slow), Input, Output).classify(Input(text='input'), usage=usage)
    )
    await entered.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert task.cancelled() and usage.requests == 1


PHONE = '+34600111222'
CLOCK = datetime(2026, 9, 21, 10, tzinfo=diary.TZ)


def pending_booking():
    books = diary.Diary()
    books.set_clock(CLOCK)
    call = open_call(PHONE, diary=books)
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    slot = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
    question = Asked(
        about='booking', value=slot['slot_id'], action='book', owner=PHONE, spoken='Shall I book Friday?'
    )
    call.ask(question)
    call.note_said('fixed', question.spoken, origin='ask')
    return call, slot


async def decisions_and_fallback():
    with classifier_model():
        call, _ = pending_booking()
        _, finished = await take_turn(call, agents.AGENTS, 'Yes.')
        assert finished.requests == 1 and call.state.writes == 1
        assert call._asked.about == 'closing'
        assert call.requests[0].duration is not None and not call.requests[0].fallback
        assert call.requests[0].outcome == 'action' and '"confirmation":"accept"' in call.requests[0].labels
        assert sum(isinstance(p, UserPromptPart) for m in call._protocol for p in m.parts) == 1
        assert 'appointment_choice' not in str(call.messages)
        _, finished = await take_turn(call, agents.AGENTS, 'No, thanks.')
        assert finished.requests == 1 and call.ended == 'caller'

        call, _ = pending_booking()
        seen = []

        async def clarify(messages, info):
            seen.append(messages)
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"retry"}')}

        with agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=clarify)):
            _, finished = await take_turn(call, agents.AGENTS, 'Yes, if it is free.')
        assert len(seen) == 1 and finished.requests == 2 and not call.state.writes
        assert not call.requests[0].fallback and call.requests[0].outcome == 'none', (
            'a conditional answer was understood; the agent carrying on is not a failure'
        )
        assert sum(isinstance(p, UserPromptPart) for m in seen[0] for p in m.parts) == 1

        call = open_call(PHONE)

        async def answer(messages, info):
            yield 'We open at nine.'

        with agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=answer)):
            _, finished = await take_turn(call, agents.AGENTS, 'When do you open?')
        assert finished.requests == 1 and all(r.kind != 'classifier' for r in call.requests)


def pending_readback():
    call = open_call(PHONE)
    call.commit(agent='verify')
    Ask(about='phone', value=PHONE, spoken='Is that right?').put(call)
    call.note_said('fixed', 'Is that right?', origin='ask')
    return call


async def outcome_is_not_fallback():
    """The complaint trace: an accepted readback, then a step with no opening, is an action and not a fallback."""

    async def answer(messages, info):
        yield 'How can I help?'

    concierge = agents.AGENTS['concierge'].agent
    with classifier_model(), concierge.override(model=FunctionModel(stream_function=answer)):
        call = pending_readback()
        _, finished = await take_turn(call, agents.AGENTS, 'Yes.')
        assert call.state.customer_verified and call.state.agent == 'concierge' and finished.requests == 2
        classified = call.requests[0]
        assert classified.kind == 'classifier' and not classified.fallback and classified.outcome == 'action'

        call = pending_readback()

        def elsewhere(phone):
            return {'customer_phone': '+34600999888', 'customer_status': 'existing'}

        async def guidance(messages, info):
            yield {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"correction"}')}

        verify = agents.AGENTS['verify'].agent
        with (
            patch.object(call.diary, 'find_or_create_customer', elsewhere),
            verify.override(model=FunctionModel(stream_function=guidance)),
        ):
            await take_turn(call, agents.AGENTS, 'Yes.')
        assert not call.state.customer_verified
        assert not call.requests[0].fallback and call.requests[0].outcome == 'refused'

    call = pending_readback()

    def broken(messages, info):
        return ModelResponse(parts=[TextPart('broken')])

    async def guidance(messages, info):
        yield {0: DeltaToolCall(name='verification_guidance', json_args='{"topic":"correction"}')}

    with (
        patch.object(CLASSIFIER, 'model', FunctionModel(broken)),
        agents.AGENTS['verify'].agent.override(model=FunctionModel(stream_function=guidance)),
    ):
        await take_turn(call, agents.AGENTS, 'Yes.')
    assert call.requests[0].fallback and call.requests[0].outcome == 'none'


async def binding_failures_and_late_results():
    with classifier_model():
        call, _ = pending_booking()
        call.note_heard('Shall I')
        call.turn_started('Yes.')
        assert call.pending_question() is None
        assert (await classify_reply(call)).confirmation != 'accept'

        call, _ = pending_booking()
        call.turn_started('Yes.')
        usage = RunUsage()
        assert (await classify_reply(call, usage=usage)).confirmation == 'accept'
        assert (await classify_reply(call, usage=usage)).confirmation == 'accept' and usage.requests == 1
        call.commit(customer_phone='+34600999888')
        assert call.classification.confirmation != 'accept'
        await classify_reply(call, usage=usage)
        assert usage.requests == 1

    call, _ = pending_booking()
    call.turn_started('Yes.')

    def invalid(messages, info):
        return ModelResponse(parts=[TextPart('broken')])

    with patch.object(CLASSIFIER, 'model', FunctionModel(invalid)):
        usage = RunUsage()
        for _ in range(2):
            assert (await classify_reply(call, usage=usage)).confirmation == 'unclear'
        assert usage.requests == 1

    entered, release = asyncio.Event(), asyncio.Event()

    async def late(messages, info):
        entered.set()
        await release.wait()
        return ModelResponse(parts=[TextPart(ReplyClassification(confirmation='accept').model_dump_json())])

    call, _ = pending_booking()
    call.turn_started('Yes.')
    with patch.object(CLASSIFIER, 'model', FunctionModel(late)):
        task = asyncio.create_task(classify_reply(call))
        await entered.wait()
        call.begin_turn()
        call.turn_started('No.')
        release.set()
        assert (await task).confirmation == 'unclear'
    assert call.classification.confirmation == 'unclear' and not call.state.writes


async def limits_and_barge():
    call, _ = pending_booking()
    with classifier_model():
        _, finished = await take_turn(call, agents.AGENTS, 'Yes.', TurnBudget(model_requests=0))
    assert finished.status == 'exhausted' and finished.requests == 0 and not call.state.writes

    entered = asyncio.Event()

    async def slow(messages, info):
        entered.set()
        await asyncio.Event().wait()

    call, _ = pending_booking()
    barge = Barge()
    with patch.object(CLASSIFIER, 'model', FunctionModel(slow)):
        task = asyncio.create_task(take_turn(call, agents.AGENTS, 'Yes.', barge=barge))
        await entered.wait()
        barge.fire()
        _, finished = await asyncio.wait_for(task, 1)
    assert finished.status == 'interrupted' and not call.state.writes
    assert call.turn in call._retained_turns


async def rule_interpreters_spend_nothing():
    """A code-only interpreter keeps the once-per-turn contract and costs no request or budget."""
    decided = []

    class Words(RuleInterpreter[SalonCall, Input, Output]):
        def _input(self, call):
            return Input(text=call.heard)

        def _rule(self, value):
            decided.append(value.text)
            return Output(label='one' if 'one' in value.text else 'two')

    words = Words('words', Output(label='two'))
    call = open_call(PHONE)
    call.turn_started('one please')
    usage = RunUsage()
    limits = UsageLimits(request_limit=0)
    assert (await words.interpret(call, usage=usage, usage_limits=limits)).label == 'one'
    assert (await words.interpret(call)).label == 'one' and decided == ['one please']
    assert not call.requests and usage.requests == 0
    call.turn_started('two')
    assert words.current(call).label == 'two' and decided == ['one please']
    assert (await words.interpret(call)).label == 'two' and decided == ['one please', 'two']


def classes_live_in_schema():
    schema = ReplyClassification.model_json_schema()
    for definition in schema['$defs'].values():
        for choice in definition['anyOf']:
            assert choice['const'] and choice['description']
    assert len(CLASSIFIER.instructions.splitlines()) == 1
    assert 'booking' not in CLASSIFIER.instructions


async def main():
    classes_live_in_schema()
    await generic_contracts()
    await invalid_answers_and_cancellation()
    await decisions_and_fallback()
    await outcome_is_not_fallback()
    await binding_failures_and_late_results()
    await limits_and_barge()
    await rule_interpreters_spend_nothing()
    print('classifiers ok: schemas, validation, accounting, fallback, binding, cancellation')


if __name__ == '__main__':
    asyncio.run(main())
