"""The two example flows, offline, with scripted models: `core` running businesses that are not the salon.

The access request is walked turn by turn through the real engine. The
checks are the plan's: two answers in one sentence fill two steps; agreeing
before the readback does nothing; changing an answer forgets what rested on
it; the receipt says submitted, not approved; an answer that went missing is
pending, not failed. The help line answers, stays, and ends on goodbye with
no interpreter request at all.
"""

import asyncio
import json
from datetime import date

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from core.flow import Flow
from core.steps import ConfirmInput, ConfirmReading, FlowCall
from core.turn import take_turn
from examples.access import flow as access
from examples.access.backend import DIRECTORY
from examples.help import flow as help_line

SAID = {
    "It's E-100.": {'employee_id': 'E-100'},
    'E-999.': {'employee_id': 'E-999'},
    'Engineering, and I need GitHub.': {'team': 'engineering', 'system': 'github'},
    'Engineering.': {'team': 'engineering'},
    'GitHub.': {'system': 'github'},
    'Write access.': {'level': 'write'},
    'Admin.': {'level': 'admin'},
    'Read only.': {'level': 'read'},
    'To review pull requests.': {'reason': 'to review pull requests'},
    'From the first of October.': {'start': '2026-10-01'},
    'Until the fifteenth of October.': {'end': '2026-10-15'},
    'Until the end of December.': {'end': '2026-12-20'},
}
"""What a correct model extracts from each reply. Anything else, it answers in words and extracts nothing."""
READ = {'Yes.': 'accept', 'No, forget it.': 'reject', 'Actually, change the level.': 'revise'}


def heard(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    return str(part.content)
    return ''


async def extracts(messages, info):
    values = SAID.get(heard(messages))
    if values is None:
        yield 'Sorry, could you say that again?'
    else:
        yield {0: DeltaToolCall(name='collect', json_args=json.dumps({'values': values}))}


def reads(messages, info):
    data = ConfirmInput.model_validate_json(messages[-1].parts[0].content)
    kind = READ.get(data.utterance, 'none')
    return ModelResponse(
        parts=[TextPart(ConfirmReading.model_validate({'decision': {'kind': kind}}).model_dump_json())]
    )


def scripted_access():
    DIRECTORY.requests.clear()
    DIRECTORY.lose_next_answer = False
    flow = access.build(FunctionModel(stream_function=extracts))
    flow.interpreter.classifier.model = FunctionModel(reads)
    return flow


class Line:
    """A call with its line to the caller recorded."""

    def __init__(self, call: FlowCall) -> None:
        self.call = call
        self.said: list[str] = []
        self.ended = False
        call._say = self.say
        call._end = self.end

    async def say(self, text):
        self.said.append(text)

    async def end(self):
        self.ended = True


async def turns(flow, line: Line, *replies: str) -> list:
    finished = []
    for reply in replies:
        _, done = await take_turn(line.call, flow.nodes, reply)
        assert done.status == 'answered', (reply, done)
        finished.append(done)
    return finished


TO_CONFIRM = (
    "It's E-100.",
    'Engineering, and I need GitHub.',
    'Write access.',
    'To review pull requests.',
    'From the first of October.',
    'Until the fifteenth of October.',
)


async def the_request_end_to_end():
    flow = scripted_access()
    line = Line(flow.open_call())
    done = await turns(flow, line, *TO_CONFIRM)
    assert all(turn.requests == 1 for turn in done), [t.requests for t in done]
    assert 'system' not in done[1].handoffs, 'the system was given with the team, so its step was skipped'
    assert line.call.state.agent == 'confirm' and line.said[-1].startswith(
        'To confirm: write access to github'
    )
    facts = line.call.state.facts
    assert facts['identify']['match'] and facts['eligibility']['eligible'] and not DIRECTORY.requests

    (final,) = await turns(flow, line, 'Yes.')
    assert final.requests == 1 and line.call.state.outcome == 'completed'
    assert len(DIRECTORY.requests) == 1 and DIRECTORY.requests[0]['start'] == '2026-10-01'
    assert 'AR-0001 is submitted' in line.said[-1] and 'still needs approval' in line.said[-1]
    assert line.call.state.facts['submit']['status'] == 'submitted'


async def agreement_needs_the_readback():
    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, "It's E-100.", 'Engineering, and I need GitHub.', 'Yes.')
    assert line.call.state.agent == 'level' and not DIRECTORY.requests, (
        'a yes before any readback did nothing'
    )

    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM)
    line.call.note_heard('To confirm: write')
    await turns(flow, line, 'Yes.')
    assert not DIRECTORY.requests and line.call.state.outcome == '', (
        'a readback spoken over cannot be agreed to'
    )
    assert line.said[-1].startswith('To confirm'), 'it is read back again instead'
    await turns(flow, line, 'Yes.')
    assert len(DIRECTORY.requests) == 1 and line.call.state.outcome == 'completed'


async def a_changed_answer_forgets_what_rested_on_it():
    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM)
    flow.record(line.call, {'system': 'aws'})
    facts = line.call.state.facts
    assert 'level' not in facts and 'eligibility' not in facts and facts['reason'], facts

    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM, 'Actually, change the level.')
    assert line.call.state.agent == 'level' and 'level' not in line.call.state.facts
    assert 'eligibility' not in line.call.state.facts and line.said[-1] == 'What access level do you need?'
    await turns(flow, line, 'Read only.')
    assert line.call.state.agent == 'confirm' and 'read access' in line.said[-1], (
        'checked again, then read back'
    )


async def endings_that_are_not_success():
    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, 'E-999.')
    assert line.call.state.outcome == 'failed' and "can't find" in line.said[-1]

    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM[:2], 'Admin.', *TO_CONFIRM[3:5], 'Until the end of December.')
    assert line.call.state.outcome == 'failed' and 'policy' in line.said[-1] and not DIRECTORY.requests

    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM, 'No, forget it.')
    assert line.call.state.outcome == 'cancelled' and not DIRECTORY.requests

    flow = scripted_access()
    line = Line(flow.open_call())
    await turns(flow, line, *TO_CONFIRM)
    DIRECTORY.lose_next_answer = True
    await turns(flow, line, 'Yes.')
    assert line.call.state.outcome == 'pending' and "couldn't confirm" in line.said[-1]
    assert len(DIRECTORY.requests) == 1, (
        'it may well have gone through, which is why it is pending, not failed'
    )


async def the_help_line():
    async def answers(messages, info):
        if 'bye' in heard(messages).lower():
            yield {0: DeltaToolCall(name='goodbye', json_args='{"words": "Bye for now."}')}
        else:
            yield 'We open at ten on Saturdays.'

    flow = help_line.build(FunctionModel(stream_function=answers))
    line = Line(flow.open_call())
    done = await turns(flow, line, 'When do you open on Saturday?', 'And do you print?')
    assert all(turn.requests == 1 for turn in done) and line.call.state.agent == 'answer'
    assert not any(r.kind == 'classifier' for r in line.call.requests)
    await turns(flow, line, 'Thanks, bye.')
    assert line.ended and line.said[-1] == 'Bye for now.'


def flows_are_graphs_too():
    moves = Flow.of(access.AGENTS).moves
    assert moves['confirm'] == {'submit', 'cancelled', 'level', 'submitted'}
    assert 'employee' not in moves['team'], 'a flow only moves forward, or where a step says'
    assert Flow.of(help_line.AGENTS).moves == {'answer': frozenset()}
    diagram = Flow.of(access.AGENTS).render()
    assert 'after_start' in diagram and 'after_turn_start' in diagram, (
        'a step named start keeps its own label'
    )
    assert date(2026, 10, 1) >= DIRECTORY.today


async def main():
    await the_request_end_to_end()
    await agreement_needs_the_readback()
    await a_changed_answer_forgets_what_rested_on_it()
    await endings_that_are_not_success()
    await the_help_line()
    flows_are_graphs_too()
    print('examples ok: a ten-step request and a help line, on core alone')


if __name__ == '__main__':
    asyncio.run(main())
