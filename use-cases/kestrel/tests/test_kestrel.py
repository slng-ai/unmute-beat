"""Kestrel's promises, on the real flow with a scripted model and a fresh fake claims desk.

K1 the greeting asks for the policy number, and the moves match the design.
K2 a whole call: the policy read back and found, every question asked by
code in order, the safety line once, the one car picked, the claim read back
and filed once, one request per turn. K3 a day in the future is refused with
the locale's words. K4 two cars: code asks which. K5 a correction after the
read-back gets a fresh one, and the claim files with it. K6 an unheard
read-back files nothing. K7 a no files nothing. K8 a filing whose answer was
lost is found again, never filed twice. K9 a policy that does not match is
refused and nothing is revealed. K10 every prompt names only its own outputs.
"""

import asyncio

from core.flow import Flow
from kestrel.offers import SAFETY
from tests.script import FACTS, PRIYA, SPEC, found, opened, told, turn

READ_BACK = (
    'Shall I file your claim for the blue Toyota Corolla? On Monday the 28th of September in the evening, '
    'at the A40 near Oxford: a van reversed into my front bumper. Nobody was hurt, no police report, '
    'another driver was involved, and the car can be driven. Is that all right?'
)
FILED = (
    'Your claim is filed. Your claim number is K C, one zero zero, two three one. '
    'A claims handler will call you within one working day. Is there anything else I can help with?'
)


async def the_greeting_asks_for_the_policy_number():
    """K1."""
    _, flow, _, _, said = opened()
    assert (
        said
        == 'Thanks for calling Kestrel Insurance claims, this is Sam. Can I take your policy number, please?'
    )
    assert Flow.of(flow.nodes).moves == {'verify': frozenset({'incident'}), 'incident': frozenset({'verify'})}


async def a_whole_call_files_one_claim():
    """K2."""
    script, flow, call, claims, _ = opened()
    said = await turn(
        flow,
        call,
        script,
        "It's KP482913, and I was born on the 5th of June 1979.",
        ('call', 'record', {'update': {'policy_number': 'KP482913', 'birth_date': '1979-06-05'}}),
    )
    assert said == [
        'Is your policy number K P, four eight two, nine one three, with a date of birth of the 5th of June, 1979?'
    ], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'lookup_policy'))
    assert said == [
        'Thank you, Daniel, I have your policy.',
        'I can take your claim now. On which day did it happen?',
    ], said
    steps = [
        (
            'Monday evening.',
            {'incident_day': '2026-09-28', 'incident_time': 'evening'},
            'Where did it happen?',
        ),
        ('On the A40 near Oxford.', {'place': 'the A40 near Oxford'}, 'In your own words, what happened?'),
        (
            'A van reversed into my front bumper.',
            {'what_happened': 'a van reversed into my front bumper'},
            'Was anyone hurt?',
        ),
        (
            'Yes, my passenger hurt her neck.',
            {'injuries': True},
            f'{SAFETY} Was it reported to the police? If so, do you have a reference?',
        ),
        ('No police.', {'police_report': 'none'}, 'Was another vehicle or driver involved?'),
        ('Yes, the van.', {'other_party': True}, 'Can your car still be driven?'),
    ]
    for words, update, asked in steps:
        said = await turn(flow, call, script, words, ('call', 'record', {'update': update}))
        assert said == [asked], (words, said)
    said = await turn(
        flow, call, script, 'Yes, it drives.', ('call', 'record', {'update': {'vehicle_drivable': True}})
    )
    assert said == [READ_BACK.replace('Nobody was hurt', 'Someone was hurt')], said
    assert call.state.facts.vehicle == 'AB12CDE', 'the one car is picked by code'
    said = await turn(flow, call, script, 'Yes.', ('call', 'file_claim'))
    assert said == [FILED], said
    assert len(claims.filed) == 1 and len(script.requests) == 10, (len(claims.filed), len(script.requests))


async def a_future_day_is_refused():
    """K3."""
    script, flow, call, _, _ = opened()
    await found(flow, call, script)
    refusal = "That day hasn't happened yet. On which day did it happen?"
    said = await turn(
        flow,
        call,
        script,
        'Next Monday.',
        ('call', 'record', {'update': {'incident_day': '2026-10-05'}}),
        ('call', 'reply', {'say': refusal}),
    )
    assert said == [refusal], said
    assert call.state.facts.incident_day is None, call.state.facts.incident_day
    assert refusal in str(script.requests[-1].messages[-1].parts[0].content), (
        "the locale's words go with the retry"
    )


async def two_cars_are_asked_about():
    """K4."""
    script, flow, call, _, _ = opened()
    said = await told(flow, call, script, PRIYA)
    assert said == ['Which car was it: the white Kia Niro or the black Ford Fiesta?'], said
    said = await turn(flow, call, script, 'The Kia.', ('call', 'record', {'update': {'vehicle': 'LM70XYZ'}}))
    assert said == [READ_BACK.replace('the blue Toyota Corolla', 'the white Kia Niro')], said


async def a_correction_gets_a_fresh_read_back():
    """K5."""
    script, flow, call, claims, _ = opened()
    assert await told(flow, call, script) == [READ_BACK]
    said = await turn(
        flow,
        call,
        script,
        "Actually no, it can't be driven.",
        ('call', 'record', {'update': {'vehicle_drivable': False}}),
    )
    assert said == [READ_BACK.replace('the car can be driven', 'the car cannot be driven')], said
    await turn(flow, call, script, 'Yes.', ('call', 'file_claim'))
    [claim] = claims.filed.values()
    assert claim['vehicle_drivable'] is False, claim


async def an_unheard_read_back_files_nothing():
    """K6."""
    script, flow, call, claims, _ = opened()
    await told(flow, call, script, heard=False)
    await turn(flow, call, script, 'Yes.', ('call', 'file_claim'), ('call', 'reply', {'say': 'Sorry?'}))
    assert claims.filed == {}


async def a_no_files_nothing():
    """K7."""
    script, flow, call, claims, _ = opened()
    await told(flow, call, script)
    await turn(
        flow, call, script, 'No.', ('call', 'reply', {'say': 'What should I change?', 'decline': True})
    )
    assert claims.filed == {}


async def a_lost_answer_is_found_not_filed_twice():
    """K8."""
    script, flow, call, claims, _ = opened()
    await told(flow, call, script)
    claims.lose_next = True
    said = await turn(flow, call, script, 'Yes.', ('call', 'file_claim'))
    assert said == [FILED], said
    assert len(claims.filed) == 1


async def a_policy_that_does_not_match_is_refused():
    """K9."""
    script, flow, call, _, _ = opened()
    wrong = {'policy_number': 'KP482913', 'birth_date': '1980-06-05'}
    await turn(flow, call, script, 'KP482913, born 5 June 1980.', ('call', 'record', {'update': wrong}))
    await turn(
        flow,
        call,
        script,
        'Yes.',
        ('call', 'lookup_policy'),
        ('call', 'reply', {'say': "I can't find that one."}),
    )
    assert call.state.facts.policy is None and call.state.agent == 'verify'
    assert 'Daniel' not in script.requests[-1].sent and 'Corolla' not in script.requests[-1].sent


async def prompts_name_only_their_own_outputs():
    """K10."""
    every = set(SPEC.operations)
    for step in SPEC.steps:
        prompt = str(SPEC.prompt(step.id))
        for name in every - set(step.operations):
            assert f'`{name}`' not in prompt, (step.id, name)
        assert '`hand_over`' not in prompt, step.id
    assert set(FACTS) <= set(SPEC.steps[1].fields)


async def main() -> None:
    for case in (
        the_greeting_asks_for_the_policy_number,
        a_whole_call_files_one_claim,
        a_future_day_is_refused,
        two_cars_are_asked_about,
        a_correction_gets_a_fresh_read_back,
        an_unheard_read_back_files_nothing,
        a_no_files_nothing,
        a_lost_answer_is_found_not_filed_twice,
        a_policy_that_does_not_match_is_refused,
        prompts_name_only_their_own_outputs,
    ):
        await case()
        print(f'ok  {case.__name__}')


if __name__ == '__main__':
    asyncio.run(main())
