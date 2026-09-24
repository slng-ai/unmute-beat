"""Offline contracts for generic classification and the salon's decision boundary."""

import asyncio
import difflib
import json
import os
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimitExceeded, UsageLimits

from core.classifiers import TASK, Classifier, ClassSet, Exchange, output_model
from core.interpreters import RuleInterpreter
from core.policies import TurnBudget
from core.readings import Reading
from core.state import Checkpoint
from core.turn import Barge, take_turn
from salon import agents, diary
from salon.classification import classify_reply
from salon.models.consent import Asked
from salon.models.receipts import Slots
from salon.models.state import SalonCall, open_call
from salon.tools.asking import Ask
from tests.classifier_model import ALL, classifier_model, classifier_with, scripted


class Input(Exchange):
    question: str = Field(default='', description='What the person was asked.')
    answer: str = Field(min_length=1, description='What the person said.')


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
        assert (
            info.instructions
            == f'{TASK}\n\nThe input:\n- `question`: What the person was asked.\n- `answer`: What the person said.'
        )
        return ModelResponse(
            parts=[TextPart('{"answer":{"label":"one"},"count":2}')],
            usage=RequestUsage(input_tokens=12, output_tokens=8),
        )

    classifier = Classifier(FunctionModel(respond), Input, Nested)
    usage = RunUsage()
    result = await classifier.classify(
        Input(answer='first'), usage=usage, usage_limits=UsageLimits(request_limit=1)
    )
    assert result.answer.label == 'one' and result.count == 2
    assert usage.requests == 1 and usage.input_tokens == 12 and usage.output_tokens == 8
    try:
        await classifier.classify(
            Input(answer='second'), usage=usage, usage_limits=UsageLimits(request_limit=1)
        )
    except UsageLimitExceeded:
        pass
    else:
        raise AssertionError('classifier exceeded its shared request budget')
    assert len(seen) == 1
    try:
        await classifier.classify(Input.model_construct(answer=''))
    except ValidationError:
        pass
    else:
        raise AssertionError('invalid input reached the model')
    assert len(seen) == 1

    await classes_from_data()


async def classes_from_data():
    """A classifier whose classes come from configuration: the author writes only context, classes and descriptions.

    The request carries the question and the answer from state, the author's
    context before the fixed task, and every class with its description in
    the schema. Nothing about any business is in the classifier itself.
    """
    reading = output_model(
        'Reading',
        {
            'intent': ClassSet(
                'What the person wants.',
                {'book': 'Wants an appointment.', 'unclear': 'Anything else, or not clear.'},
                default='unclear',
            )
        },
    )

    def respond(messages, info):
        schema = info.model_request_parameters.output_object.json_schema
        intent = schema['properties']['intent']
        assert intent['description'] == 'What the person wants.', intent
        assert [(m['const'], m['description']) for m in intent['anyOf']] == [
            ('book', 'Wants an appointment.'),
            ('unclear', 'Anything else, or not clear.'),
        ], intent
        assert info.instructions.startswith('A line into a shop.\n\n' + TASK), info.instructions
        assert '- `question`: What the person was asked.' in info.instructions
        sent = Input.model_validate_json(messages[0].parts[0].content)
        assert (sent.question, sent.answer) == ('How can I help?', 'Book me in.'), sent
        return ModelResponse(parts=[TextPart('{"intent":"book"}')])

    classifier = Classifier(FunctionModel(respond), Input, reading, context='A line into a shop.')
    result = await classifier.classify(Input(question='How can I help?', answer='Book me in.'))
    assert result.model_dump() == {'intent': 'book'}
    assert reading().model_dump() == {'intent': 'unclear'}, 'the default is the fallback'
    try:
        ClassSet('x', {'yes': 'Agrees.'}, default='no').annotation()
    except ValueError:
        pass
    else:
        raise AssertionError('a default outside the classes was accepted')


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
            await Classifier(FunctionModel(invalid), Input, Output).classify(
                Input(answer='input'), usage=usage
            )
        except UnexpectedModelBehavior, ValidationError:
            pass
        else:
            raise AssertionError('invalid model output was accepted')
        assert usage.requests == 1 and usage.input_tokens == 3

    def failed(messages, info):
        raise ModelHTTPError(503, 'offline')

    usage = RunUsage()
    try:
        await Classifier(FunctionModel(failed), Input, Output).classify(Input(answer='input'), usage=usage)
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
        Classifier(FunctionModel(slow), Input, Output).classify(Input(answer='input'), usage=usage)
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
        assert finished.requests == 2 and call.state.writes == 1, 'confirm and closing, side by side'
        assert call._asked.about == 'closing'
        assert call.requests[0].duration is not None and not call.requests[0].fallback
        assert call.requests[0].outcome == 'action' and '"confirmation":"accept"' in call.requests[0].labels
        assert sum(isinstance(p, UserPromptPart) for m in call._protocol for p in m.parts) == 1
        assert 'appointment_choice' not in str(call.messages)
        _, finished = await take_turn(call, agents.AGENTS, 'No, thanks.')
        assert call.ended == 'caller' and {r.kind for r in call.requests} == {'classifier'}, call.requests

        call, _ = pending_booking()
        seen = []

        async def clarify(messages, info):
            seen.append(messages)
            yield {0: DeltaToolCall(name='booking_guidance', json_args='{"topic":"retry"}')}

        with agents.AGENTS['booking'].agent.override(model=FunctionModel(stream_function=clarify)):
            _, finished = await take_turn(call, agents.AGENTS, 'Yes, if it is free.')
        assert len(seen) == 1 and finished.requests == 3 and not call.state.writes
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


async def a_yes_and_a_goodbye_are_both_done():
    """Coval run 4bLgLR4t, case 09: "Yes, that's perfect. Thank you. I do not need anything else. Goodbye."

    Only the confirm reading ran, so the booking was made and the caller was
    asked "Anything else?". The closing reading now runs beside it: the
    booking is made, its receipt asks nothing more, and the call ends.
    """
    words = "Yes, that's perfect. Thank you. I do not need anything else. Goodbye."

    def script(name, data):
        return {'confirmation': 'accept', 'conversation': 'end'}.get(name)

    async def line(text):
        """The call's line to the caller."""

    with scripted(script):
        call, _ = pending_booking()
        call._say = line
        _, finished = await take_turn(call, agents.AGENTS, words)
    assert call.state.writes == 1 and call.ended == 'caller', (call.state.writes, call.ended)
    lines = [s.text for s in call.spoken]
    assert lines[-1] == 'Thanks for calling. Goodbye.', lines
    assert not any('?' in text for text in lines), f'a question was asked of a caller who is leaving: {lines}'
    assert finished.requests == 2 and {r.kind for r in call.requests} == {'classifier'}, call.requests


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
        assert call.state.customer_verified and call.state.agent == 'concierge' and finished.requests == 3
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
        classifier_with(FunctionModel(broken)),
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
        assert (await classify_reply(call, usage=usage)).confirmation == 'accept' and usage.requests == 2, (
            'confirm and closing, once each'
        )
        call.commit(customer_phone='+34600999888')
        assert call.classification.confirmation != 'accept'
        await classify_reply(call, usage=usage)
        assert usage.requests == 2

    call, _ = pending_booking()
    call.turn_started('Yes.')

    def invalid(messages, info):
        return ModelResponse(parts=[TextPart('broken')])

    with classifier_with(FunctionModel(invalid)):
        usage = RunUsage()
        for _ in range(2):
            assert (await classify_reply(call, usage=usage)).confirmation == 'unclear'
        assert usage.requests == 2, 'each reading failed once and was not asked again'

    entered, release = asyncio.Event(), asyncio.Event()

    async def late(messages, info):
        entered.set()
        await release.wait()
        return ModelResponse(parts=[TextPart('{"confirmation": "accept"}')])

    call, _ = pending_booking()
    call.turn_started('Yes.')
    with classifier_with(FunctionModel(late)):
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
    with classifier_with(FunctionModel(slow)):
        task = asyncio.create_task(take_turn(call, agents.AGENTS, 'Yes.', barge=barge))
        await entered.wait()
        barge.fire()
        _, finished = await asyncio.wait_for(task, 1)
    assert finished.status == 'interrupted' and not call.state.writes
    assert call.input_retained


async def rule_interpreters_spend_nothing():
    """A code-only interpreter keeps the once-per-turn contract and costs no request or budget."""
    decided = []

    class Words(RuleInterpreter[SalonCall, Input, Output]):
        def _input(self, call):
            return Input(answer=call.heard)

        def _rule(self, value):
            decided.append(value.answer)
            return Output(label='one' if 'one' in value.answer else 'two')

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
    """Every reading's classes are in its own output schema, with descriptions; its instructions are the task."""
    for reading in ALL:
        classifier = reading.classifier
        for definition in classifier.output_type.model_json_schema()['properties'].values():
            for choice in definition.get('anyOf', []):
                assert choice['const'] and choice['description'], (reading.name, choice)
        assert classifier.instructions.startswith(classifier.context) and TASK in classifier.instructions
        assert '- `question`:' in classifier.instructions and '- `answer`:' in classifier.instructions
        for label in ('accept', 'conditional', 'withdraw', 'booking'):
            assert label not in classifier.instructions, (
                f'{reading.name}, {label!r}: classes belong in the schema'
            )


def every_read_back_is_one_request():
    """A flow's `ConfirmStep` sends what the salon's `confirmation` reading sends, so one dataset measures both."""
    from core.steps import confirm_interpreter
    from salon.classification import CONFIRMATION, MODEL

    step = confirm_interpreter(MODEL).classifier
    assert step.input_type is CONFIRMATION.classifier.input_type
    assert step.json_schema() == CONFIRMATION.classifier.json_schema()


def acts_keep_their_order():
    """The salon's acts are tried in core's slots, and within a slot in the order the salon gave them.

    A yes to the number read-back comes before the number heard in the same
    words, so "yes, that's +34 ..." is the yes; a pick comes after any take-back.
    """
    from salon.classification import SALON

    def named(rules) -> list[str]:
        return [
            f'{type(r.__self__).__name__}:{getattr(r.__self__, "name", "") or r.__self__.about}'
            for r in rules
        ]

    acts = SALON.acts
    assert named(acts.answer) == [
        'FixedChoice:appointment_choice',
        'ReadBack:phone',
        'ReadBack:booking',
        'ReadBack:complaint',
        'HeardValue:phone',
    ], named(acts.answer)
    assert named(acts.take_back) == ['TakeBack:last_change']
    assert named(acts.pick) == ['Offer:chosen_offer']
    assert named(acts.goodbye) == ['Closing:conversation']


GOLDEN = Path(__file__).parent / 'golden' / 'readings.json'
"""Every classifier request this repo can send, pinned. `BLESS_READINGS=1` rewrites it."""


def _request(classifier: Classifier) -> dict[str, Any]:
    """What one classifier sends besides the caller's words: model, settings, instructions and both schemas."""
    return {
        'model': getattr(classifier.model, 'model_name', classifier.model),
        'settings': dict(classifier.model_settings or {}),
        'instructions': classifier.instructions,
        'output_name': classifier.output_type.__name__,
        'output_schema': classifier.json_schema(),
        'input_schema': classifier.input_type.model_json_schema(),
    }


def _next_turn(call: SalonCall, heard: str, said: str = '') -> None:
    """The agent says `said` and the caller answers `heard`: a new turn, as a runtime with no playback signal counts it."""
    if said:
        call.note_said('fixed', said, origin='ask')
    call.turn_started(heard)


def _diary() -> diary.Diary:
    books = diary.Diary()
    books.set_clock(CLOCK)
    return books


def _states() -> dict[str, SalonCall]:
    """Salon calls standing where each reading can apply, on the pinned clock, so what each one sends is fixed."""
    states = {}

    call = open_call(PHONE, diary=(books := _diary()))
    _next_turn(call, 'I want a haircut on Friday.')
    call._checkpoint = Checkpoint(
        owner='booking', messages=[], speech_ids=frozenset(), heard=call.heard, turn=call.turn
    )
    call.commit(agent='verify')
    _next_turn(
        call,
        'Yes, and make it Saturday instead.',
        Ask(about='phone', value=PHONE, spoken='Is it +34 600 111 222?').put(call),
    )
    states['phone_read_back_in_a_detour'] = call

    call = open_call(PHONE, diary=(books := _diary()))
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    _next_turn(call, 'A haircut on Friday, please.')
    held = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
    books.save_booking(PHONE, 'book', held['slot_id'], '', False, CLOCK, offered_to=call.id)
    slots = Slots.model_validate(
        books.find_slots(PHONE, 'Saturday', 'haircut', CLOCK, offered_to=call.id)
    ).slots[:2]
    call.commit(offered=slots)
    offer = 'For a haircut on Saturday, I have 9:00 AM and 9:30 AM. Which time works for you?'
    call.ask(Asked(about='offer', spoken=offer, value=','.join(s.slot_id for s in slots), owner=PHONE))
    _next_turn(call, 'Nine, same as the other one.', offer)
    states['offer_while_holding'] = call

    call = open_call(PHONE, diary=(books := _diary()))
    call.commit(agent='booking', customer_verified='existing', verified_phone=PHONE)
    _next_turn(call, 'Can I get a haircut Saturday at ten?')
    slot = books.find_slots(PHONE, 'Saturday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
    ask = 'Would you like to move the appointment you already have, or add another one?'
    call.ask(Asked(about='move_or_add', spoken=ask, value=slot['slot_id'], action='move', owner=PHONE))
    _next_turn(call, 'Move it, please.', ask)
    states['move_or_add'] = call

    call = open_call(PHONE, diary=(books := _diary()))
    call.commit(agent='concierge', customer_verified='existing', verified_phone=PHONE)
    _next_turn(call, 'Yes, book it.')
    slot = books.find_slots(PHONE, 'Monday', 'blowout', CLOCK, offered_to=call.id)['slots'][0]
    booked = books.save_booking(PHONE, 'book', slot['slot_id'], '', True, CLOCK, offered_to=call.id)
    books.withdraw_offers(call.id)
    told = f"You're booked for {booked['spoken']}. Anything else?"
    call.offer_undo(booked['booking_id'], told)
    call.ask(Asked(about='closing', spoken=told, owner=PHONE))
    _next_turn(call, 'How much is that? If it is over 30, leave it.', told)
    states['closing_after_a_booking'] = call

    call = open_call(PHONE, diary=(books := _diary()))
    call.commit(agent='complaints')
    _next_turn(call, 'My colour came out patchy.')
    call.approve_complaint(
        Asked(about='complaint', spoken='A patchy colour. Shall I record that?', turn=call.turn)
    )
    call.commit(agent='verify')
    _next_turn(call, 'Yes, that is it.')
    _next_turn(
        call, 'Yes, go ahead.', Ask(about='phone', value=PHONE, spoken='Is it +34 600 111 222?').put(call)
    )
    states['complaint_kept_through_verify'] = call

    call = open_call(PHONE, diary=(books := _diary()))
    _next_turn(call, 'When do you open?')
    states['nothing_pending'] = call
    return states


def _sent(reading, call: SalonCall) -> str | None:
    value = reading.snapshot(call) if isinstance(reading, Reading) else reading._input(call)
    return None if value is None else value.model_dump_json()


def _named_pick() -> Reading:
    """The pick reading with its `named` field on (`Offer.named`): the one prompt change waiting to be measured."""
    from core.readings import choose
    from salon.classification import CHOSEN_OFFER, MODEL, SETTINGS
    from salon.models.consent import OfferInput

    context = CHOSEN_OFFER.classifier.context
    return choose(
        'chosen_offer',
        OfferInput,
        CHOSEN_OFFER.snapshot,
        context=context,
        model=MODEL,
        settings=SETTINGS,
        named=True,
    )


def pinned() -> dict[str, Any]:
    """Every classifier request, and the exact input each reading sends in each fixed call state."""
    from core.steps import confirm_interpreter
    from salon.classification import MODEL, SOLVER, SOLVER_CLASSIFIER

    readings = (*ALL, SOLVER)
    requests = {reading.name: _request(reading.classifier) for reading in ALL}
    requests |= {
        'solver': _request(SOLVER_CLASSIFIER),
        'confirm_step': _request(confirm_interpreter(MODEL).classifier),
        'chosen_offer+named': _request(_named_pick().classifier),
    }
    sent = {state: {r.name: _sent(r, call) for r in readings} for state, call in _states().items()}
    return {'requests': requests, 'sent': sent}


AGENT_GOLDEN = Path(__file__).parent / 'golden' / 'agents.json'
"""Each salon step's first model request, pinned: its instructions, its tools and its output tools."""


def _tools(defs) -> list[dict[str, Any]]:
    return [
        {
            'name': d.name,
            'description': d.description,
            'parameters': d.parameters_json_schema,
            'sequential': d.sequential,
        }
        for d in defs
    ]


async def _agent_request(step: str, heard: str) -> dict[str, Any]:
    """What a step's agent is sent on a turn no reading decides, from a call standing in that step."""
    from tests.classifier_model import classifier_model

    call = open_call(PHONE, diary=_diary())
    if step in ('booking', 'complaints'):
        call.commit(agent=step, customer_verified='existing', verified_phone=PHONE)
    else:
        call.commit(agent=step)
    seen: dict[str, Any] = {}

    async def capture(messages, info):
        if not seen:
            seen.update(
                instructions=info.instructions,
                allow_text=info.allow_text_output,
                tools=_tools(info.function_tools),
                outputs=_tools(info.output_tools),
            )
        yield {0: DeltaToolCall(name='no_such_tool', json_args='{}')}

    with classifier_model(), agents.AGENTS[step].agent.override(model=FunctionModel(stream_function=capture)):
        await take_turn(call, agents.AGENTS, heard)
    assert seen, f'{step} sent no model request'
    return seen


async def agent_requests_are_pinned():
    """No step's prompt or tool schema changes without a diff. Tool docstrings are prompt text."""
    heard = {
        'concierge': 'Do you do blowouts?',
        'verify': 'My number is on file.',
        'booking': 'A haircut on Friday, please.',
        'complaints': 'My colour came out patchy last week.',
    }
    pinned = {step: await _agent_request(step, words) for step, words in heard.items()}
    _compare(AGENT_GOLDEN, pinned)


def _compare(path: Path, value: Any) -> None:
    """The file is compared as text, because key order is part of what is sent. `BLESS_READINGS=1` rewrites it."""
    current = json.dumps(value, indent=1, sort_keys=False, default=str) + '\n'
    if os.environ.get('BLESS_READINGS') == '1':
        path.parent.mkdir(exist_ok=True)
        path.write_text(current)
        print(f'{path.name} pinned again: a prompt change, which needs a live check')
        return
    expected = path.read_text()
    if current != expected:
        diff = difflib.unified_diff(
            expected.splitlines(), current.splitlines(), 'pinned', 'now', lineterm='', n=2
        )
        raise AssertionError(f'a model request changed ({path.name}):\n' + '\n'.join(list(diff)[:80]))


def requests_are_pinned():
    """No classifier request changes without a diff: schema, instructions, settings and the values sent.

    Moving code between modules must not change a byte a model is sent. A
    change that means to is a prompt change, and needs its live check
    before it ships.
    """
    _compare(GOLDEN, pinned())


async def main():
    classes_live_in_schema()
    every_read_back_is_one_request()
    requests_are_pinned()
    await agent_requests_are_pinned()
    acts_keep_their_order()
    await generic_contracts()
    await invalid_answers_and_cancellation()
    await decisions_and_fallback()
    await a_yes_and_a_goodbye_are_both_done()
    await outcome_is_not_fallback()
    await binding_failures_and_late_results()
    await limits_and_barge()
    await rule_interpreters_spend_nothing()
    print('classifiers ok: schemas, validation, accounting, fallback, binding, cancellation')


if __name__ == '__main__':
    asyncio.run(main())
