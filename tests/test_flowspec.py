"""Flow files: the examples written as JSON behave like the code, and every broken file is refused before it runs.

Each publish check has one broken file, and each broken file must be refused
for its own reason, with every problem listed at once, so the person editing
it can fix them together.
"""

import asyncio
import json
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.flowspec import FlowSpecError, Registry, load
from examples.access import flow as access
from examples.access.backend import DIRECTORY
from examples.help import flow as help_line
from tests.test_examples import TO_CONFIRM, Line, extracts, heard, reads, turns

ACCESS = json.loads((Path(access.__file__).parent / 'flow.json').read_text())


def scripted(flow):
    DIRECTORY.requests.clear()
    DIRECTORY.lose_next_answer = False
    flow.interpreter.classifier.model = FunctionModel(reads)
    return flow


async def the_file_and_the_code_agree():
    outcomes = []
    for flow in (
        access.build(FunctionModel(stream_function=extracts)),
        access.from_file(FunctionModel(stream_function=extracts)),
    ):
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
        said.append((line.said, line.ended, flow.nodes['answer'].agent._instructions))
    assert said[0] == said[1] and said[0][1], said


def broken(change) -> dict:
    spec = json.loads(json.dumps(ACCESS))
    change(spec['steps'])
    return spec


def refused(spec: dict, *expected: str) -> None:
    try:
        load(json.dumps(spec)).compile(access.REGISTRY, model=FunctionModel(stream_function=extracts))
    except FlowSpecError as error:
        for words in expected:
            assert any(words in problem for problem in error.problems), (words, error.problems)
    else:
        raise AssertionError(f'accepted a broken flow; expected {expected}')


def unreadable(spec: dict) -> None:
    try:
        load(json.dumps(spec))
    except ValidationError:
        pass
    else:
        raise AssertionError('read a file with an unknown kind or field')


def step(steps, step_id):
    return next(s for s in steps if s['id'] == step_id)


def every_publish_check():
    unreadable(broken(lambda steps: steps.append({'id': 'x', 'kind': 'teleport'})))
    unreadable(broken(lambda steps: step(steps, 'team').update({'colour': 'blue'})))
    unreadable({**ACCESS, 'version': 2})

    refused(
        broken(lambda steps: step(steps, 'system').update({'id': 'team'})), "'team' is used more than once"
    )
    refused(
        broken(lambda steps: step(steps, 'confirm').update({'on_reject': 'nowhere'})),
        "'nowhere', which is not a step",
    )
    refused(
        broken(lambda steps: step(steps, 'submit').update({'operation': 'launch'})),
        "operation 'launch', which is not registered",
    )
    refused(broken(lambda steps: step(steps, 'team').update({'model': 'teams'})), "field model 'teams'")
    refused(broken(lambda steps: step(steps, 'eligibility').update({'accepts': 'vibes'})), "check 'vibes'")
    refused(
        broken(lambda steps: step(steps, 'level').update({'after': ['colour']})),
        "'colour', which no earlier step collects",
    )
    refused(broken(lambda steps: steps.remove(step(steps, 'confirm'))), 'no confirm step for')
    refused(
        broken(lambda steps: step(steps, 'end').update({'then': 'submit'})),
        'reached without its confirmation',
    )
    both = broken(
        lambda steps: (
            step(steps, 'confirm').update({'on_reject': 'nowhere'}),
            step(steps, 'team').update({'model': 'teams'}),
        )
    )
    refused(both, 'nowhere', 'teams')

    try:
        load(json.dumps(help_line.SPEC.model_dump())).compile(
            Registry(), model=FunctionModel(stream_function=extracts)
        )
    except FlowSpecError as error:
        assert error.problems == ["answer names knowledge 'library', which is not registered"], error.problems
    else:
        raise AssertionError('compiled against a registry missing its knowledge')


async def main():
    await the_file_and_the_code_agree()
    every_publish_check()
    print('flowspec ok: files behave like code, and every broken file is refused')


if __name__ == '__main__':
    asyncio.run(main())
