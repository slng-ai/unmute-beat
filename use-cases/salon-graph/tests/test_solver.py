"""The state solver, offline: what the application does with each decision, and what it must never do.

The solver model is scripted, so these check the application side of the
experiment: that a correction or a switch costs one request, that every
existing guard still decides, and that a decision cannot reach past them. The
live comparison of what a real model decides is `evals/compare.py`.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.output import OutputObjectDefinition

from core.decisions import Accept, Clarify, EndCall, NoDecision
from core.state import Handoff
from core.turn import Barge, take_turn
from evals.cases import CASES, Case
from evals.compare import projected_requests, resolves, rules_report, table
from evals.solver import solution_for, solver_input, solver_meaning
from salon import agents, classification
from salon.classification import MODEL, SOLVER_CLASSIFIER
from salon.models.consent import CorrectPhone, SalonSolution, SalonSwitch, SolverInput
from salon.models.state import Unresolved, open_call
from salon.tools.asking import Ask
from tests.test_replay import CLOCK, PHONE, pending, verified

WRITES = {'save_booking', 'record_complaint'}


def answering(solution: SalonSolution, seen: list | None = None):
    """A solver model that always returns this solution, recording what it was sent."""

    def answer(messages, info):
        data = SolverInput.model_validate_json(messages[-1].parts[0].content)
        if seen is not None:
            seen.append(data)
        return ModelResponse(parts=[TextPart(solution.model_dump_json())])

    return FunctionModel(answer)


@contextmanager
def solver(solution: SalonSolution, seen: list | None = None):
    with (
        patch.object(classification, 'INTERPRETER', 'solver'),
        patch.object(SOLVER_CLASSIFIER, 'model', answering(solution, seen)),
    ):
        yield


def refusing(messages, info):
    raise AssertionError('this step should not have run a model')


async def _ignore():
    return None


def voiced(call):
    """Give the call a line to the caller, so fixed speech is recorded as it would be live."""

    async def say(text):
        return None

    call._say = say
    call._end = _ignore
    return call


def schema_reaches_gemini_as_anyof():
    """Gemini rejects `oneOf`; the discriminated union has to arrive as `anyOf`, with the tag required."""
    assert 'oneOf' in str(SalonSolution.model_json_schema())
    schema = SOLVER_CLASSIFIER.json_schema()
    params = MODEL.customize_request_parameters(
        ModelRequestParameters(
            output_mode='native',
            output_object=OutputObjectDefinition(json_schema=schema, name='SalonSolution'),
        )
    )
    assert params.output_object is not None
    sent = params.output_object.json_schema
    assert 'oneOf' not in str(sent) and 'discriminator' not in str(sent), sent
    options = sent['$defs']['SalonDecision']['anyOf']
    assert len(options) == 10
    for option in options:
        definition = sent['$defs'][option['$ref'].rsplit('/', 1)[-1]]
        assert definition['required'][0] == 'kind', definition
    try:
        SalonSolution.model_validate_json('{"decision": {}}')
    except ValueError:
        pass
    else:
        raise AssertionError('a decision with no kind was read as one')


def mappings_round_trip():
    for case in CASES:
        assert solver_meaning(case, solution_for(case)) == case.expect, case.id


def expected(case: Case) -> str | None:
    """The action a correct decision leads to, before its executor has had its say."""
    meaning = case.expect
    if case.stale:
        return None
    if meaning == 'correct_phone':
        return 'ask_number'
    if meaning.startswith('switch_'):
        return 'hand_over'
    if not case.delivered:
        return None
    if meaning == 'end':
        return 'hang_up'
    if meaning == 'accept':
        return {'phone': 'confirm_customer', 'booking': 'save_booking', 'complaint': 'record_complaint'}.get(
            case.about
        )
    if meaning in ('move', 'add') and case.about == 'move_or_add':
        return 'save_booking'
    return None


async def every_case_reaches_its_executor():
    for case in CASES:
        call = await pending(case)
        call._end = _ignore
        with solver(solution_for(case)):
            action = await agents.advance(call)
            name = None if action is None else action.name
            assert name == expected(case), (case.id, name, expected(case))
            output = None
            if action is not None:
                try:
                    output = await action.run()
                except ModelRetry:
                    assert name == 'hand_over', (case.id, 'only a switch the graph does not allow is refused')
        assert call.state.writes == (1 if name in WRITES else 0), case.id
        if case.about == 'phone':
            assert bool(call.state.customer_verified) == (name == 'confirm_customer'), case.id
        if name == 'ask_number':
            assert isinstance(output, Ask) and output.value == case.digits, case.id
        if name == 'hand_over' and output is not None:
            assert (
                isinstance(output, Handoff)
                and output.ends_turn
                and output.to == case.expect.removeprefix('switch_')
            ), case.id


async def unheard_proposals_cannot_be_accepted():
    for case in CASES:
        if case.delivered and not case.stale:
            continue
        for decision in (Accept(), EndCall()):
            call = await pending(case)
            call._end = _ignore
            with solver(SalonSolution(decision=decision)):
                action = await agents.advance(call)
            assert action is None and not call.state.writes and not call.ended, (case.id, decision)
            assert case.about != 'phone' or not call.state.customer_verified, case.id


def case(case_id: str) -> Case:
    return next(c for c in CASES if c.id == case_id)


async def one_request_turns():
    """The table in the plan: each of these used to cost two or three requests."""
    with solver(solution_for(case('phone-partial-correction'))):
        call = voiced(await pending(case('phone-partial-correction'), answered=False))
        with agents.AGENTS['verify'].agent.override(model=FunctionModel(refusing)):
            _, finished = await take_turn(call, agents.AGENTS, case('phone-partial-correction').utterance)
        assert finished.status == 'answered' and finished.requests == 1, finished
        assert not call.state.customer_verified and call.state.agent == 'verify'
        question = call.pending_question()
        assert question is None and call._asked is not None and call._asked.value == '+34600111234'
        assert '234' in call.spoken[-1].text and call.spoken[-1].origin == 'ask'

    for which in ('booking-switch-complaint-details', 'booking-interrupted-switch'):
        call = voiced(await pending(case(which), answered=False))
        with (
            solver(solution_for(case(which))),
            agents.AGENTS['complaints'].agent.override(model=FunctionModel(refusing)),
        ):
            _, finished = await take_turn(call, agents.AGENTS, case(which).utterance)
        assert finished.status == 'answered' and finished.requests == 1, (which, finished)
        assert call.state.agent == 'complaints' and not call.state.writes, which
        assert call.spoken[-1].origin == 'switch' and 'What happened' in call.spoken[-1].text, which
        assert call.pending_question() is None and call._asked is None, which

    call = await pending(case('booking-yes'), answered=False)
    with solver(solution_for(case('booking-yes'))):
        _, finished = await take_turn(call, agents.AGENTS, 'Yes, please.')
    assert finished.requests == 1 and call.state.writes == 1

    call = open_call(PHONE)

    async def answer(messages, info):
        yield 'We open at nine.'

    seen = []
    with (
        solver(SalonSolution(decision=NoDecision()), seen),
        agents.AGENTS['concierge'].agent.override(model=FunctionModel(stream_function=answer)),
    ):
        _, finished = await take_turn(call, agents.AGENTS, 'When do you open?')
    assert finished.requests == 1 and not seen, 'an ordinary question has no solver in front of it'


async def a_switch_still_meets_prerequisites():
    """An unidentified caller switching to complaints is read their number back first; the reply is not said."""
    call = voiced(open_call(PHONE))
    Ask(about='closing', spoken='Anything else?').put(call)
    call.note_said('fixed', 'Anything else?', origin='ask')
    switch = SalonSolution(decision=SalonSwitch(target='complaints'), reply='What happened?')
    with (
        solver(switch),
        agents.AGENTS['verify'].agent.override(model=FunctionModel(refusing)),
        agents.AGENTS['complaints'].agent.override(model=FunctionModel(refusing)),
    ):
        _, finished = await take_turn(call, agents.AGENTS, 'I want to complain.')
    assert finished.requests == 1 and call.state.agent == 'verify' and call.state.pending == 'complaints'
    assert all('What happened' not in s.text for s in call.spoken)
    assert call._asked is not None and call._asked.about == 'phone', 'the verify opening read the number back'


async def must_never():
    with solver(SalonSolution(decision=CorrectPhone(digits='+34600111234'))):
        call = await pending(case('phone-yes'))
        action = await agents.advance(call)
        assert action is not None and action.name == 'ask_number'
        await action.run()
        assert not call.state.customer_verified and not call.state.phone_confirmed

    with solver(SalonSolution(decision=CorrectPhone(digits='six hundred'))):
        call = await pending(case('phone-yes'))
        assert await agents.advance(call) is None, (
            'digits that are not a number go to the agent, not a readback'
        )

    with solver(SalonSolution(decision=Clarify(question='Which digits were wrong?'))):
        call = await pending(case('booking-maybe'))
        assert await agents.advance(call) is None, 'only a readback gets a solver-worded question'
        call = await pending(case('phone-ambiguous-correction'))
        action = await agents.advance(call)
        assert action is not None and action.name == 'clarify'
        await action.run()
        assert call._asked is None, 'a clarified proposal cannot be accepted by the next yes'

    call, books = verified('booking')
    slot = books.find_slots(PHONE, 'Friday', 'haircut', CLOCK, offered_to=call.id)['slots'][0]
    held = books.save_booking(PHONE, 'book', slot['slot_id'], '', False, CLOCK, offered_to=call.id)
    call.ask(Ask(about='booking', value='x', action='book', spoken='Shall I book Saturday?'))
    call.note_said('fixed', 'Shall I book Saturday?', origin='ask')
    call.turn_started('Forget it, I want to complain.')
    with solver(SalonSolution(decision=SalonSwitch(target='complaints'), reply='What happened?')):
        action = await agents.advance(call)
        assert action is not None
        moved = await action.run()
    assert isinstance(moved, Handoff) and moved.to == 'complaints'
    assert [b['booking_id'] for b in books.bookings_for(PHONE, CLOCK.date())] == [held['booking_id']]

    call = await pending(case('booking-switch-complaint'))
    call.commit(unresolved=Unresolved(token='t', action='book'))
    with solver(solution_for(case('booking-switch-complaint'))):
        action = await agents.advance(call)
        assert action is not None
        try:
            await action.run()
        except ModelRetry:
            pass
        else:
            raise AssertionError('a switch went ahead while a write was unresolved')
    assert call.state.agent == 'booking'

    entered = asyncio.Event()

    async def slow(messages, info):
        entered.set()
        await asyncio.Event().wait()

    call = await pending(case('booking-switch-complaint'), answered=False)
    barge = Barge()
    with (
        patch.object(classification, 'INTERPRETER', 'solver'),
        patch.object(SOLVER_CLASSIFIER, 'model', FunctionModel(slow)),
    ):
        task = asyncio.create_task(
            take_turn(call, agents.AGENTS, case('booking-switch-complaint').utterance, barge=barge)
        )
        await entered.wait()
        barge.fire()
        _, finished = await asyncio.wait_for(task, 1)
    assert finished.status == 'interrupted' and call.state.agent == 'booking' and not call.state.writes

    def broken(messages, info):
        return ModelResponse(parts=[TextPart('broken')])

    call = await pending(case('booking-yes'))
    with (
        patch.object(classification, 'INTERPRETER', 'solver'),
        patch.object(SOLVER_CLASSIFIER, 'model', FunctionModel(broken)),
    ):
        assert await agents.advance(call) is None
    assert call.requests[0].fallback and not call.state.writes


def projected_requests_follow_the_plan():
    """With every reading right, the solver's turns cost what the plan's table says, and never more."""
    for which, classifier, solver_ in (
        ('booking-yes', 1, 1),
        ('phone-full-correction', 2, 1),
        ('booking-switch-complaint', 3, 1),
        ('booking-interrupted-switch', 2, 1),
    ):
        c = case(which)
        assert projected_requests(c, c.legacy_meaning, 'classifier') == classifier, which
        assert projected_requests(c, c.expect, 'solver') == solver_, which
    dearer = []
    for c in CASES:
        more = projected_requests(c, c.expect, 'solver') - projected_requests(
            c, c.legacy_meaning, 'classifier'
        )
        if more > 0:
            dearer.append(c.id)
            assert more == 1 and not c.delivered and not resolves(c, c.expect, solver=True), c.id
    assert sorted(dearer) == [
        'booking-interrupted-yes',
        'complaint-interrupted-yes',
        'phone-interrupted-yes',
    ], 'the known cost: a spoken-over proposal is read even when nothing can come of it'
    total = sum(projected_requests(c, c.expect, 'solver') for c in CASES)
    assert total < sum(projected_requests(c, c.legacy_meaning, 'classifier') for c in CASES)
    floor = rules_report()
    assert floor.false_accepts == 0 and floor.requests <= len(CASES)
    assert 'rules' in table([floor])


async def main():
    schema_reaches_gemini_as_anyof()
    projected_requests_follow_the_plan()
    mappings_round_trip()
    solver_input(CASES[0])
    await every_case_reaches_its_executor()
    await unheard_proposals_cannot_be_accepted()
    await one_request_turns()
    await a_switch_still_meets_prerequisites()
    await must_never()
    print(f'solver ok: {len(CASES)} cases, one-request turns, guards unchanged')


if __name__ == '__main__':
    asyncio.run(main())
