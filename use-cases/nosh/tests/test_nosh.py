"""Nosh's promises, on the real flow with a scripted model and fake place tools.

N1 the greeting asks where, in code's words. N2 a whole call: the location read
back, the places and the details all said by code, one request per turn. N3 a
new area takes away the location, the places and the details. N4 an invented
place id never reaches the tools. N5 a location Google cannot place is asked
again in code's words. N6 an area with no craving asks what they fancy before
any search; an exact address with none searches nearby. N7 details Google no
longer has go back to the model. N8 a second place replaces the details, and a
search is asked once. N9 goodbye ends the call. N10 the prompt names only the
outputs the step has.
"""

import asyncio

from core.flow import Flow
from tests.script import SPEC, opened, turn

ASK_WHERE = 'Whereabouts are you looking? An address or an area is fine.'
TAPAS = {'area': 'Gothic Quarter, Barcelona', 'at_address': False, 'craving': 'tapas'}
LISTED = (
    'I found Bar Cañete, a tapas restaurant with a 4.5 out of five from lots of reviews; '
    'El Xampanyet, a bar with a 4.4 out of five from 800 reviews; '
    'and Bodega La Palma, a spanish restaurant with a 4 out of five from lots of reviews. '
    'Do any of those sound good?'
)
CANETE = (
    'Bar Cañete is on Carrer de la Unió, 17, Ciutat Vella, Barcelona. '
    "It's a 4.5 out of five, and mid-range. Today they're open 1:00 PM to 12:00 AM. "
    "I've got their number and a website too, if you'd like them. "
    'Want to hear about another one, or is this the one?'
)


async def the_greeting_asks_where():
    """N1."""
    _, flow, _, tools, said = opened()
    assert said == f"Hey, it's Nosh! You wanted a hand finding somewhere good. {ASK_WHERE}", said
    assert Flow.of(flow.nodes).moves == {'finding': frozenset()}, 'one step, no moves'
    assert tools.sent == []


async def a_whole_call_is_said_by_code():
    """N2: every line after the caller's words is code's, one request per caller turn."""
    script, flow, call, tools, _ = opened()
    said = await turn(
        flow, call, script, 'Tapas in the Gothic Quarter, Barcelona.', ('call', 'record', {'update': TAPAS})
    )
    assert said == ["So that's Barri Gòtic, Barcelona, Spain, right?"], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    assert said == [LISTED], said
    said = await turn(flow, call, script, 'The first one.', ('call', 'record', {'update': {'place': 'p1'}}))
    assert said == [CANETE], said
    assert call.state.facts.details is not None and call.state.facts.details.place == 'p1'
    assert call.state.facts.details.price == 'mid-range'
    assert len(script.requests) == 3, [r.step for r in script.requests]
    assert [r for r, _ in tools.sent] == ['resolve-location', 'search-text', 'place-details'], tools.sent
    assert tools.routes('search-text')[0]['latitude'] == 41.3833, 'the search is biased to the agreed point'


async def a_new_area_takes_everything_back():
    """N3: the chain resets: area -> spot -> place -> details."""
    script, flow, call, _, _ = opened()
    await turn(flow, call, script, 'Tapas in the Gothic Quarter.', ('call', 'record', {'update': TAPAS}))
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    await turn(flow, call, script, 'The first.', ('call', 'record', {'update': {'place': 'p1'}}))
    said = await turn(
        flow, call, script, 'Actually, Gràcia.', ('call', 'record', {'update': {'area': 'Gràcia, Barcelona'}})
    )
    assert said == ["So that's Gràcia, Barcelona, Spain, right?"], said
    facts = call.state.facts
    assert (facts.spot, facts.place, facts.details) == (None, None, None), facts


async def an_invented_place_is_refused():
    """N4: a place id nobody offered never reaches the tools."""
    script, flow, call, tools, _ = opened()
    await turn(flow, call, script, 'Tapas in the Gothic Quarter.', ('call', 'record', {'update': TAPAS}))
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    await turn(
        flow,
        call,
        script,
        'The fifth one.',
        ('call', 'record', {'update': {'place': 'p9'}}),
        ('call', 'reply', {'say': 'I only have those three. Which one sounds good?'}),
    )
    assert call.state.facts.place is None and call.state.facts.details is None
    assert tools.routes('place-details') == []


async def a_location_google_cannot_place_is_asked_again():
    """N5."""
    script, flow, call, _, _ = opened()
    said = await turn(
        flow, call, script, 'Nowhere town.', ('call', 'record', {'update': {'area': 'Nowhere'}})
    )
    assert said == [
        "Hmm, I couldn't place that. Could you give me a nearby cross street, a neighbourhood or the city?"
    ], said
    assert call.state.facts.spot is None


async def no_craving_asks_first_but_an_address_searches_nearby():
    """N6."""
    script, flow, call, tools, _ = opened()
    area = {'area': 'Gothic Quarter, Barcelona', 'at_address': False}
    await turn(flow, call, script, 'The Gothic Quarter.', ('call', 'record', {'update': area}))
    said = await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    assert said == ['What are you in the mood for? A cuisine, a bar, or no preference?'], said
    assert tools.routes('search-text') == [] and tools.routes('search-nearby') == []

    script, flow, call, tools, _ = opened()
    street = {'area': 'Passeig de Sant Joan 55, Barcelona', 'at_address': True}
    await turn(flow, call, script, "I'm at Passeig de Sant Joan 55.", ('call', 'record', {'update': street}))
    said = await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    assert said == [
        'I found Bar Snack 55, a bar with a 4 out of five from 600 reviews. Do any of those sound good?'
    ]
    assert len(tools.routes('search-nearby')) == 1 and tools.routes('search-text') == []


async def details_google_lost_go_back_to_the_model():
    """N7: a 404 from place-details is a no: nothing is said for it, and the model words it."""
    script, flow, call, _, _ = opened()
    await turn(flow, call, script, 'Tapas in the Gothic Quarter.', ('call', 'record', {'update': TAPAS}))
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    said = await turn(
        flow,
        call,
        script,
        'The second one.',
        ('call', 'record', {'update': {'place': 'p2'}}),
        (
            'call',
            'reply',
            {'say': "Sorry, I can't pull up El Xampanyet's details just now. Want another one?"},
        ),
    )
    assert said == ["Sorry, I can't pull up El Xampanyet's details just now. Want another one?"], said
    assert call.state.facts.details is None


async def a_second_place_replaces_the_details():
    """N8."""
    script, flow, call, tools, _ = opened()
    await turn(flow, call, script, 'Tapas in the Gothic Quarter.', ('call', 'record', {'update': TAPAS}))
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'spot': 's1'}}))
    await turn(flow, call, script, 'The first.', ('call', 'record', {'update': {'place': 'p1'}}))
    said = await turn(flow, call, script, 'And the third?', ('call', 'record', {'update': {'place': 'p3'}}))
    assert said == [
        'Bodega La Palma is on Carrer de la Palma de Sant Just, 7, Barcelona. '
        "It's a 4 out of five. Want to hear about another one, or is this the one?"
    ], said
    assert call.state.facts.details is not None and call.state.facts.details.place == 'p3'
    assert len(tools.routes('search-text')) == 1, 'the same search is asked once per call'
    assert len(tools.routes('place-details')) == 2


async def goodbye_ends_the_call():
    """N9."""
    script, flow, call, _, _ = opened()
    said = await turn(flow, call, script, "No, that's it, thanks!", ('call', 'goodbye', {'recap': []}))
    assert said == ['Enjoy your meal! Bye for now.'], said
    assert call.ended


async def the_prompt_names_only_its_outputs():
    """N10."""
    script, flow, call, _, _ = opened()
    await turn(flow, call, script, 'Tapas in the Gothic Quarter.', ('call', 'record', {'update': TAPAS}))
    outputs = set(script.requests[0].tools)
    assert {'record', 'reply', 'goodbye'} <= outputs, outputs
    prompt = str(SPEC.prompt('finding'))
    for name in ('hand_over', 'collect', 'place_details', 'done'):
        assert f'`{name}`' not in prompt, name


async def main() -> None:
    for case in (
        the_greeting_asks_where,
        a_whole_call_is_said_by_code,
        a_new_area_takes_everything_back,
        an_invented_place_is_refused,
        a_location_google_cannot_place_is_asked_again,
        no_craving_asks_first_but_an_address_searches_nearby,
        details_google_lost_go_back_to_the_model,
        a_second_place_replaces_the_details,
        goodbye_ends_the_call,
        the_prompt_names_only_its_outputs,
    ):
        await case()
        print(f'ok  {case.__name__}')


if __name__ == '__main__':
    asyncio.run(main())
