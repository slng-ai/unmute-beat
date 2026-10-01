"""Wasla's promises, on the real flow with a scripted model and a fresh fake ledger.

W1 the greeting names the holder from the ledger. W2 a whole call: the date of
birth read back and checked, the account opened by code, a promise read back
and recorded once, one request per turn. W3 identify never sees the account.
W4 an unheard read-back verifies nothing. W5 three wrong dates lock the
account. W6 not a good time moves the holder to a callback, noted once. W7 a
promise past the deadline is refused with the locale's words. W8 a no records
nothing. W9 a wrong number is read back on arrival and taken off the list. W10
someone else is asked for a callback and hears nothing about the account. W11
a dispute is noted in the caller's words. W12 every prompt names only its own
step's operations.
"""

import asyncio

from core.flow import Flow
from tests.script import SPEC, opened, turn, verified

NOTICE = (
    'Thank you. This call is recorded for training and quality. '
    "I'm calling about four hundred thirty-four dirhams and fifty fils for your purchase from Falcon Electronics, "
    'which is now overdue. Could you make the payment today?'
)
GOOD_TIME = 'Is now a good time to talk about your Wasla account?'
CALL_BACK = 'When would be a good day and time to call back?'


async def the_greeting_names_the_holder():
    """W1, and the moves the design agreed."""
    _, flow, _, _, said = opened()
    assert said == 'Hello, this is Lina calling from Wasla. Am I speaking with Layla Haddad?', said
    assert Flow.of(flow.nodes).moves == {
        'identify': frozenset({'account', 'callback', 'concern', 'wrong_number'}),
        'account': frozenset({'callback', 'concern', 'identify'}),
        'callback': frozenset(),
        'wrong_number': frozenset(),
        'concern': frozenset({'account', 'identify'}),
    }, Flow.of(flow.nodes).moves


async def a_whole_call_records_one_promise():
    """W2."""
    script, flow, call, ledger, _ = opened()
    said = await turn(
        flow,
        call,
        script,
        'Yes, speaking.',
        ('call', 'reply', {'say': 'Thanks, Layla. What is your date of birth?'}),
    )
    said = await turn(
        flow,
        call,
        script,
        'Fourteenth of March, ninety.',
        ('call', 'record', {'update': {'birth_date': '1990-03-14'}}),
    )
    assert said == ['Is your date of birth the 14th of March, 1990?'], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'verify_holder'))
    assert said == ['Thank you, that matches.', GOOD_TIME], said
    assert call.state.facts.verified is not None and call.state.agent == 'account'
    said = await turn(
        flow, call, script, 'Sure, go ahead.', ('call', 'record', {'update': {'good_time': True}})
    )
    assert said == [NOTICE], said
    said = await turn(
        flow,
        call,
        script,
        'I can pay on the ninth.',
        ('call', 'record', {'update': {'promise_day': '2026-10-09'}}),
    )
    assert said == [
        'Shall I record your promise to pay four hundred thirty-four dirhams and fifty fils on Friday the 9th of October?'
    ], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'record_promise'))
    assert said == [
        'Thank you. Your promise to pay on Friday the 9th of October is recorded. '
        'You can pay in the Wasla app or on our website. Is there anything else I can help with?'
    ], said
    await turn(
        flow,
        call,
        script,
        'Yes, yes.',
        ('call', 'record_promise'),
        ('call', 'reply', {'say': 'Anything else?'}),
    )
    assert ledger.codes('WA-1001') == ['PTP'], ledger.records
    assert len(script.requests) == 7, [r.step for r in script.requests]


async def identify_never_sees_the_account():
    """W3: before the check, no request carries the amount, the merchant or the dates."""
    script, flow, call, _, _ = opened()
    await verified(flow, call, script)
    for request in (r for r in script.requests if r.step == 'identify'):
        for secret in ('Falcon', 'four hundred', '434.5', 'August', 'September'):
            assert secret not in request.sent, (secret, request.step)
        assert 'Layla Haddad' in request.instructions, 'the pre-set name is rendered in the prompt'
    assert all(r.step == 'identify' for r in script.requests), 'account opened with no request of its own'


async def an_unheard_read_back_verifies_nothing():
    """W4."""
    script, flow, call, _, _ = opened()
    await turn(flow, call, script, 'Yes.', ('call', 'reply', {'say': 'What is your date of birth?'}))
    await turn(
        flow,
        call,
        script,
        '14 March 1990.',
        ('call', 'record', {'update': {'birth_date': '1990-03-14'}}),
        heard=False,
    )
    await turn(flow, call, script, 'Yes.', ('call', 'verify_holder'), ('call', 'reply', {'say': 'Sorry?'}))
    assert call.state.facts.verified is None and call.state.agent == 'identify'


async def three_wrong_dates_lock_the_account():
    """W5."""
    script, flow, call, ledger, _ = opened()
    await turn(flow, call, script, 'Yes.', ('call', 'reply', {'say': 'What is your date of birth?'}))
    for wrong in ('1990-03-15', '1990-04-14', '1991-03-14'):
        await turn(flow, call, script, 'It is ...', ('call', 'record', {'update': {'birth_date': wrong}}))
        await turn(
            flow,
            call,
            script,
            'Yes.',
            ('call', 'verify_holder'),
            ('call', 'reply', {'say': "That doesn't match."}),
        )
    assert call.state.facts.verified is None and ledger.wrong_tries['WA-1001'] == 3
    assert 'locked' in script.requests[-1].sent, 'the model is told the account is locked'
    assert ledger.verify('WA-1001', ledger.account('WA-1001').birth_date)['status'] == 'locked'


async def not_a_good_time_notes_a_callback():
    """W6."""
    script, flow, call, ledger, _ = opened()
    await verified(flow, call, script)
    said = await turn(
        flow, call, script, "Not now, I'm driving.", ('call', 'record', {'update': {'good_time': False}})
    )
    assert said == ['No problem at all.', CALL_BACK], said
    assert call.state.agent == 'callback'
    update = {'callback_day': '2026-10-02', 'callback_time': '17:00'}
    said = await turn(
        flow, call, script, 'Tomorrow at five.', ('call', 'schedule_callback', {'update': update})
    )
    assert said == ['Shall I note that we call back on Friday the 2nd of October at 5 PM?'], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'schedule_callback'))
    assert said == [
        "That's noted: we'll call back on Friday the 2nd of October at 5 PM. Thank you for your time."
    ]
    assert ledger.codes('WA-1001') == ['CB']


async def a_day_past_the_deadline_is_refused():
    """W7."""
    script, flow, call, ledger, _ = opened()
    await verified(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'good_time': True}}))
    refusal = 'I can only note a day up to Sunday the 11th of October. Which day by then works for you?'
    said = await turn(
        flow,
        call,
        script,
        'The twentieth.',
        ('call', 'record', {'update': {'promise_day': '2026-10-20'}}),
        ('call', 'reply', {'say': refusal}),
    )
    assert said == [refusal], said
    assert refusal in script.requests[-1].sent, "the locale's words are sent with the retry"
    assert call.state.facts.promise_day is None and ledger.codes('WA-1001') == []


async def a_no_records_nothing():
    """W8."""
    script, flow, call, ledger, _ = opened()
    await verified(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'good_time': True}}))
    await turn(flow, call, script, 'Friday.', ('call', 'record', {'update': {'promise_day': '2026-10-02'}}))
    await turn(
        flow,
        call,
        script,
        'No, wait.',
        ('call', 'reply', {'say': 'Of course. Which day works?', 'decline': True}),
    )
    assert ledger.codes('WA-1001') == []


async def a_wrong_number_is_taken_off_the_list():
    """W9."""
    script, flow, call, ledger, _ = opened()
    said = await turn(
        flow,
        call,
        script,
        "There's no Layla here.",
        ('call', 'hand_over', {'to': 'wrong_number', 'because': 'wrong number'}),
    )
    assert said == [
        "I'm sorry to have bothered you. Shall I take this number off our list, so we don't call it again?"
    ], said
    said = await turn(flow, call, script, 'Yes please.', ('call', 'mark_wrong_number'))
    assert said == ["Done. You won't get any more calls from us on this number. Sorry for the trouble."], said
    assert ledger.codes('WA-1001') == ['WN']


async def someone_else_hears_nothing_about_the_account():
    """W10."""
    script, flow, call, _, _ = opened()
    said = await turn(
        flow,
        call,
        script,
        "This is her husband, she's out.",
        ('call', 'hand_over', {'to': 'callback', 'because': 'a third party answered'}),
    )
    assert said == [CALL_BACK], said
    await turn(
        flow,
        call,
        script,
        'What is this about?',
        ('call', 'reply', {'say': 'I can only discuss it with Layla.'}),
    )
    for request in script.requests:
        for secret in ('Falcon', 'four hundred', '434.5'):
            assert secret not in request.sent, (secret, request.step)


async def a_dispute_is_noted_in_their_words():
    """W11."""
    script, flow, call, ledger, _ = opened()
    await verified(flow, call, script)
    await turn(flow, call, script, 'Yes.', ('call', 'record', {'update': {'good_time': True}}))
    words = 'I never bought anything from Falcon Electronics'
    await turn(
        flow, call, script, f'{words}.', ('call', 'hand_over', {'to': 'concern', 'because': 'a dispute'})
    )
    update = {'concern_kind': 'dispute', 'concern_note': words}
    said = await turn(flow, call, script, f'{words}, really.', ('call', 'record_concern', {'update': update}))
    assert said == [f'Shall I record that you dispute this purchase: {words}?'], said
    said = await turn(flow, call, script, 'Yes.', ('call', 'record_concern'))
    assert said[0].startswith("I've recorded your dispute."), said
    assert ledger.codes('WA-1001') == ['DISPUTE']


async def prompts_name_only_their_own_operations():
    """W12."""
    every = set(SPEC.operations)
    for step in SPEC.steps:
        prompt = str(SPEC.prompt(step.id))
        own = set(step.operations)
        for name in every - own:
            assert f'`{name}`' not in prompt, (step.id, name)
        if not step.exits:
            assert '`hand_over`' not in prompt and 'hand them to' not in prompt, step.id


async def main() -> None:
    for case in (
        the_greeting_names_the_holder,
        a_whole_call_records_one_promise,
        identify_never_sees_the_account,
        an_unheard_read_back_verifies_nothing,
        three_wrong_dates_lock_the_account,
        not_a_good_time_notes_a_callback,
        a_day_past_the_deadline_is_refused,
        a_no_records_nothing,
        a_wrong_number_is_taken_off_the_list,
        someone_else_hears_nothing_about_the_account,
        a_dispute_is_noted_in_their_words,
        prompts_name_only_their_own_operations,
    ):
        await case()
        print(f'ok  {case.__name__}')


if __name__ == '__main__':
    asyncio.run(main())
