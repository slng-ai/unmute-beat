"""Flow folders: each step's prompt and code named in the YAML, and every broken flow refused before it runs.

Each rule has one broken flow, and each broken flow must be refused for its
own reason, with every problem listed at once, so the person editing it can
fix them together. The examples' folders are the working flows; broken ones
are the same YAML with one thing changed, loaded against the same folder.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import yaml
from pydantic import ValidationError
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.flowspec import FlowSpecError, Registry, load, load_file
from examples.access import flow as access
from examples.access.backend import DIRECTORY
from examples.help import flow as help_line
from tests.test_examples import TO_CONFIRM, Line, extracts, heard, reads, turns

ACCESS_DIR = Path(access.__file__).parent
HELP_DIR = Path(help_line.__file__).parent
ACCESS = yaml.safe_load((ACCESS_DIR / 'flow.yaml').read_text())
MODEL = FunctionModel(stream_function=extracts)


def scripted(flow):
    DIRECTORY.requests.clear()
    DIRECTORY.lose_next_answer = False
    flow.interpreter.classifier.model = FunctionModel(reads)
    return flow


def text_of(agent) -> str:
    """An agent's instructions as plain text, whether written as text or compiled from a prompt file."""
    return ''.join(
        str(getattr(part.instruction, '_source', part.instruction)) for part in agent._instructions
    )


async def the_folder_and_the_code_agree():
    outcomes = []
    for flow in (access.build(MODEL), access.from_file(MODEL)):
        scripted(flow)
        line = Line(flow.open_call())
        done = await turns(flow, line, *TO_CONFIRM, 'Actually, change the level.', 'Read only.', 'Yes.')
        outcomes.append(
            (
                line.said,
                line.call.state.outcome,
                [d.requests for d in done],
                [d.handoffs for d in done],
                DIRECTORY.requests[:],
            )
        )
    assert outcomes[0] == outcomes[1], outcomes
    assert outcomes[0][1] == 'completed' and outcomes[0][4][0]['level'] == 'read'

    folder = access.from_file(MODEL)
    team = text_of(folder.nodes['team'].agent)
    assert team.startswith('# Sam at the access desk') and '## Your step: the team' in team, (
        'voice, then prompt'
    )
    assert text_of(folder.nodes['employee'].agent) != team, 'each step has its own prompt'

    async def answers(messages, info):
        if 'bye' in heard(messages).lower():
            yield {0: DeltaToolCall(name='goodbye', json_args='{"words": "Bye."}')}
        else:
            yield 'Printing is ten cents a page.'

    said = []
    for flow in (
        help_line.build(FunctionModel(stream_function=answers)),
        help_line.from_file(FunctionModel(stream_function=answers)),
    ):
        line = Line(flow.open_call())
        await turns(flow, line, 'Can I print?', 'Bye.')
        said.append((line.said, line.ended, text_of(flow.nodes['answer'].agent)))
    assert said[0] == said[1] and said[0][1] and 'Printing is 10 cents' in said[0][2], said


def broken(change, spec: dict = ACCESS) -> dict:
    spec = json.loads(json.dumps(spec))
    change(spec['steps'])
    return spec


def refused(
    spec: dict, *expected: str, root: Path = ACCESS_DIR, registry: Registry | None = None
) -> list[str]:
    try:
        load(json.dumps(spec), root=root).compile(registry, model=MODEL)
    except FlowSpecError as error:
        for words in expected:
            assert any(words in problem for problem in error.problems), (words, error.problems)
        return error.problems
    raise AssertionError(f'accepted a broken flow; expected {expected}')


def unreadable(spec: dict) -> None:
    try:
        load(json.dumps(spec), root=ACCESS_DIR)
    except ValidationError:
        pass
    else:
        raise AssertionError('read a flow with an unknown kind, an unknown field or a missing one')


def step(steps, step_id):
    return next(s for s in steps if s['id'] == step_id)


def every_rule():
    unreadable(broken(lambda steps: steps.append({'id': 'x', 'kind': 'teleport'})))
    unreadable(broken(lambda steps: step(steps, 'team').update({'colour': 'blue'})))
    unreadable({**ACCESS, 'version': 2})
    unreadable(broken(lambda steps: step(steps, 'submit').update({'prompt': 'prompts/team.md'})))
    unreadable(broken(lambda steps: step(steps, 'team').pop('prompt')))

    refused(
        broken(lambda steps: step(steps, 'system').update({'id': 'team'})), "'team' is used more than once"
    )
    refused(
        broken(lambda steps: step(steps, 'confirm').update({'on_reject': 'nowhere'})),
        "'nowhere', which is not a step",
    )
    refused(
        broken(lambda steps: step(steps, 'submit').update({'code': 'steps/launch.py'})),
        "'steps/launch.py' does not exist",
    )
    refused(
        broken(lambda steps: step(steps, 'submit').update({'code': '../help/flow.py'})),
        'outside the flow folder',
    )
    refused(
        broken(lambda steps: step(steps, 'submit').update({'code': 'steps/fields.py'})), 'has no OPERATION'
    )
    refused(broken(lambda steps: step(steps, 'submit').update({'code': 'voice.md'})), 'must be a Python file')
    refused(
        broken(lambda steps: step(steps, 'submit').update({'operation': 'launch'})),
        'exactly one of code or operation',
    )
    refused(
        broken(lambda steps: step(steps, 'team').update({'model': 'steps/fields.py:Teams'})), "has no 'Teams'"
    )
    refused(
        broken(lambda steps: step(steps, 'team').update({'model': 'steps/fields.py'})),
        'must name what to take',
    )
    refused(
        broken(lambda steps: step(steps, 'team').update({'prompt': 'prompts/teams.md'})),
        "'prompts/teams.md' does not exist",
    )
    refused(
        broken(lambda steps: step(steps, 'system').update({'prompt': 'prompts/team.md'})),
        "prompt 'prompts/team.md' is already the prompt of team",
    )
    refused(
        broken(lambda steps: step(steps, 'level').update({'after': ['colour']})),
        "'colour', which no earlier step collects",
    )
    refused(broken(lambda steps: steps.remove(step(steps, 'confirm'))), 'no confirm step for')
    refused(
        broken(lambda steps: step(steps, 'end').update({'then': 'submit'})),
        'reached without its confirmation',
    )
    refused({**ACCESS, 'voice': 'nowhere.md'}, "'nowhere.md' does not exist")
    everything = refused(
        broken(
            lambda steps: (
                step(steps, 'confirm').update({'on_reject': 'nowhere'}),
                step(steps, 'level').update({'prompt': 'prompts/levels.md'}),
            )
        ),
        'nowhere',
        'levels.md',
    )
    assert len(everything) == 2, everything

    help_spec = yaml.safe_load((HELP_DIR / 'flow.yaml').read_text())
    by_name = {**help_spec, 'steps': [{**help_spec['steps'][0], 'knowledge': 'library'}]}
    assert refused(by_name, root=HELP_DIR) == ["answer: knowledge 'library' is not registered"]
    load(json.dumps(by_name), root=HELP_DIR).compile(
        Registry(knowledge={'library': 'Open daily.'}), model=MODEL
    )

    try:
        load((ACCESS_DIR / 'flow.yaml').read_text()).compile(model=MODEL)
    except FlowSpecError as error:
        assert any('has no folder; use load_file' in problem for problem in error.problems), error.problems
    else:
        raise AssertionError('a flow read from text resolved files without a folder')

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / 'ask.md').write_text('The caller likes {{state.colour}}.')
        (root / 'fine.md').write_text('The caller said {{heard}}.')
        spec = {
            'version': 1,
            'id': 'tiny',
            'steps': [{'id': 'ask', 'kind': 'conversation', 'prompt': 'ask.md'}],
        }
        refused(spec, "prompt 'ask.md' does not fit the call", root=root)
        spec['steps'][0]['prompt'] = 'fine.md'
        load(json.dumps(spec), root=root).compile(model=MODEL)
        (root / 'act.py').write_text('OPERATION = None')
        spec['steps'].append({'id': 'act', 'kind': 'action', 'code': 'act.py'})
        refused(spec, 'not a Python package', root=root)


def yaml_and_json_are_the_same_flow():
    """JSON is YAML, so either loads; plain YAML's surprises are refused with the field named, not guessed."""
    path = ACCESS_DIR / 'flow.yaml'
    assert (
        load(json.dumps(ACCESS), root=ACCESS_DIR).model_dump()
        == load_file(path).model_dump()
        == access.SPEC.model_dump()
    )
    surprise = path.read_text().replace('on_reject: cancelled', 'on_reject: no')
    try:
        load(surprise, root=ACCESS_DIR)
    except ValidationError as error:
        assert any(e['loc'][-1] == 'on_reject' for e in error.errors()), error
    else:
        raise AssertionError('an unquoted no was read as a step id')
    for text in ('', '- just\n- a list\n'):
        try:
            load(text)
        except FlowSpecError:
            pass
        else:
            raise AssertionError(f'read {text!r} as a flow')


async def main():
    await the_folder_and_the_code_agree()
    every_rule()
    yaml_and_json_are_the_same_flow()
    print('flowspec ok: folders behave like code, and every broken flow is refused')


if __name__ == '__main__':
    asyncio.run(main())
