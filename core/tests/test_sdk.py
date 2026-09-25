"""The SDK features core builds on, each checked where it stands in for code core would otherwise own.

* Pydantic whole-candidate validation (`model_validate(..., extra='forbid')`)
  is the one state update: it replaces assignment field by field, and refuses
  unknown keys in nested models that set no `extra` of their own.
* A discriminated union in the state (`Unverified | Verified`) is the shape
  a salon-style behaviour validator enforces by hand; the model path cannot
  write it at all.
* `TemplateStr` compiled against `FlowCall[S]` gives prompts typed access to
  the application's state, nullable and union fields included, refuses an
  unknown field before any model connects, and cannot reach the session's
  services, which are private.
* `Agent.from_spec` with `deps_type=FlowCall[S]` builds a step's agent from
  an `AgentSpec`, and the existing graph driver (`core.flow`, a
  pydantic-graph `GraphBuilder`) runs it: session 3's composition.
* The collect tool's schema is the state's own: an enum and a pattern reach
  the model as the runtime will enforce them.

Hooks and `new_messages()` retention are checked where they are used:
`one_action_per_reply` and budgets in salon-graph's `test_budget`, retained
input and results after cancellation in `tests.test_history`.
"""

import asyncio
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, TypeAdapter, ValidationError
from pydantic_ai import Agent, TemplateStr
from pydantic_ai.models.function import AgentInfo, FunctionModel

import core
from core.graph import Node
from core.steps import FlowCall, FlowState
from core.turn import take_turn
from tests.test_public import Script, opened, turn


class Unverified(BaseModel):
    kind: Literal['unverified'] = 'unverified'


class Verified(BaseModel):
    kind: Literal['verified'] = 'verified'
    patient: str


class Visit(BaseModel):
    reason: str


class Patient(BaseModel):
    """A state with the shapes prompts must handle: a union, an optional model, a secret."""

    identity: Annotated[Unverified | Verified, Field(discriminator='kind')] = Unverified()
    visit: Visit | None = None
    pin: SecretStr = SecretStr('')


def call_with(state: Patient) -> FlowCall:
    return FlowCall[Patient](state=FlowState[Patient](agent='desk', facts=state))


def whole_candidates_refuse_nested_unknown_keys():
    state = TypeAdapter(Patient)
    try:
        state.validate_python({'visit': {'reason': 'a check-up', 'room': 4}}, extra='forbid')
    except ValidationError as error:
        assert error.errors()[0]['type'] == 'extra_forbidden', error
    else:
        raise AssertionError('a nested unknown key was dropped instead of refused')
    assert Visit.model_config.get('extra') is None, 'the refusal comes from the commit, not the model'


def the_model_cannot_write_a_verified_identity():
    """A well-formed `Verified` from a model is still the model's word: only the trusted path writes it."""
    call = call_with(Patient())
    try:
        call.collect(identity={'kind': 'verified', 'patient': 'P-1234'})
    except ValueError as error:
        assert 'not collected from the caller' in str(error)
    else:
        raise AssertionError('a model wrote a verified identity')
    try:
        call.update(identity={'kind': 'verified'})
    except ValidationError:
        pass
    else:
        raise AssertionError('a verified identity with no patient was accepted')
    call.update(identity={'kind': 'verified', 'patient': 'P-1234'})
    assert call.state.facts.identity == Verified(patient='P-1234')


def prompts_see_the_state_and_nothing_else():
    text = (
        '{{#if state.facts.visit}}Visit: {{state.facts.visit.reason}}.{{/if}} '
        'Identity: {{state.facts.identity.kind}}. PIN: {{state.facts.pin}}.'
    )
    prompt = TemplateStr(text, deps_type=FlowCall[Patient])
    said = prompt.render(call_with(Patient(visit=Visit(reason='a check-up'), pin=SecretStr('4321'))))
    assert 'Visit: a check-up.' in said and 'Identity: unverified.' in said, said
    assert '4321' not in said, f'a secret reached a prompt: {said}'
    assert 'Visit' not in prompt.render(call_with(Patient())), 'a missing optional model renders nothing'
    for wrong in ('{{state.facts.nickname}}', '{{services.book}}', '{{_services}}'):
        try:
            TemplateStr(wrong, deps_type=FlowCall[Patient])
        except Exception:
            continue
        raise AssertionError(f'{wrong} compiled against the call')


async def an_agent_spec_runs_on_the_existing_graph():
    """An `AgentSpec` with a typed template becomes a step's agent, and `take_turn` runs it through the graph."""
    seen: list[str] = []

    async def answer(messages, info: AgentInfo):
        seen.append(info.instructions or '')
        yield 'We open at nine.'

    agent = Agent.from_spec(
        {'name': 'desk', 'instructions': 'You help patient {{state.facts.identity.kind}} callers.'},
        deps_type=FlowCall[Patient],
        model=FunctionModel(stream_function=answer),
        output_type=[str],
    )
    call = call_with(Patient())
    said, finished = await take_turn(call, {'desk': Node(agent)}, 'When do you open?')
    assert said == 'We open at nine.' and finished.status == 'answered', (said, finished)
    assert len(seen) == 1 and seen[0].startswith('You help patient unverified callers.\n\nToday is'), seen
    try:
        Agent.from_spec(
            {'instructions': '{{state.facts.nickname}}'},
            deps_type=FlowCall[Patient],
            model=FunctionModel(stream_function=answer),
        )
    except Exception:
        return
    raise AssertionError('a spec naming no field of the state was accepted')


async def the_collect_schema_is_the_states_own():
    schemas = []
    script = Script()
    original = script.agent

    async def spy(messages, info: AgentInfo):
        schemas.extend(t.parameters_json_schema for t in info.output_tools if t.name == 'collect')
        async for part in original(messages, info):
            yield part

    script.model = FunctionModel(script.reading, stream_function=spy)
    flow, call, _ = opened(script)
    await turn(flow, call, script, 'P-1234.', patient='P-1234')
    await core.text_turn(flow, call, 'Ten.')
    assert schemas, 'the collect tool was never offered'
    text = str(schemas)
    assert "'enum': ['0900', '1000']" in text, text
    assert "'pattern': '^P-\\\\d{4}$'" in text, text
    assert 'reference' not in text, 'a field no collect step names was offered to the model'


async def main() -> None:
    whole_candidates_refuse_nested_unknown_keys()
    the_model_cannot_write_a_verified_identity()
    prompts_see_the_state_and_nothing_else()
    await an_agent_spec_runs_on_the_existing_graph()
    await the_collect_schema_is_the_states_own()
    print(
        'sdk ok: whole candidates, owned fields, typed prompts, AgentSpec on the graph, state-derived schemas'
    )


if __name__ == '__main__':
    asyncio.run(main())
