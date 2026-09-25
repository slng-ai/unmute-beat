"""Delivery: only a read-back the caller got in full can be answered, and only its latest copy counts.

`Spoken.delivered` is `delivered` only on evidence: the runtime's playback
stop, or a text reader's acknowledgement. No record, a queued copy, a copy
estimated to have played, and a copy cut short are all not heard. Copies
belong to a question by its serial, never by matching words. The first two
checks were session 1's known defects (`tests.defects`).
"""

from core.proposals import BaseProposal
from core.state import CallContext, VoiceState


class Desk(VoiceState):
    agent: str = 'desk'


class Call(CallContext):
    state: Desk = Desk()


READBACK = BaseProposal(about='book', value='slot-1', spoken='Tuesday at ten. Shall I book it?')


def asked(call: Call, proposal: BaseProposal = READBACK) -> str:
    """Put the read-back on the call and hand its words to speech: its copy's id."""
    call.ask(proposal)
    call.note_said('fixed', proposal.spoken, origin='ask')
    return call.spoken[-1].id


def a_read_back_never_recorded_is_not_heard():
    call = Call()
    call.turn_started('I want a haircut')
    call.ask(READBACK)
    call.turn_started('yes')
    assert call.pending_question() is None
    assert call.last_proposal() == (call._asked, False), 'still there to be read, never to be agreed to'


def a_read_back_queued_and_never_played_is_not_heard():
    call = Call()
    call.note_said('fixed', 'Hello.', origin='entry')
    call.note_reached()
    call.note_drained()
    call.turn_started('I want a haircut')
    asked(call)
    call.turn_started('yes')
    assert call.pending_question() is None, 'the runtime reports playback, and never started this'


def text_with_no_acknowledgement_is_unknown():
    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    assert call.spoken[-1].delivered == 'unknown'
    call.turn_started('yes')
    assert call.pending_question() is None
    assert [s.delivered for s in call.all_spoken if s.id == copy] == ['unknown']


def a_partly_read_read_back_is_not_heard():
    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    assert call.acknowledge(copy, heard='Tuesday at ten.')
    assert call.spoken[-1].delivered == 'interrupted'
    call.turn_started('yes')
    assert call.pending_question() is None


def the_full_repeat_after_a_cut_copy_is_heard_once():
    call = Call()
    call.turn_started('I want a haircut')
    first = asked(call)
    call.acknowledge(first, heard='Tuesday')
    second = asked(call)
    call.acknowledge(second)
    call.turn_started('yes')
    assert call.pending('book', value='slot-1') is not None, 'the copy heard last is the one answered'
    assert call.answering()[1] == (second,), 'asked again: a new question, whose only copy is the full one'
    call.answered()
    assert call.pending_question() is None, 'one answer authorises one action'


def the_same_words_for_another_question_prove_nothing():
    """Two questions in the same words: the first one's delivered copy is not the second one's."""
    call = Call()
    call.turn_started('hello')
    first = BaseProposal(about='phone', value='+34600111222', spoken='Is that right?')
    call.acknowledge(asked(call, first))
    call.ask(BaseProposal(about='closing', spoken='Is that right?'))
    call.turn_started('yes')
    assert call.pending_question() is None, 'matched by words, the old copy would have answered it'


def a_late_stop_cannot_play_a_copy_queued_after_the_cut():
    """The audio stop for what the caller cut off arrives after a new read-back went out: that copy did not play."""
    call = Call()
    call.turn_started('I want a haircut')
    call.note_said('fixed', 'Let me check.', origin='tool_filler')
    call.note_reached()
    call.note_barge()
    asked(call)
    call.note_drained()
    assert call.spoken[-1].delivered == 'unknown', call.spoken[-1]
    call.turn_started('yes')
    assert call.pending_question() is None


def time_passing_is_not_delivery():
    """A queued copy old enough to have played counts for turn-taking, never for approval."""
    call = Call()
    call.note_said('fixed', 'Hello.', origin='entry')
    call.note_reached()
    call.note_drained()
    call.turn_started('I want a haircut')
    asked(call)
    call.spoken[-1].at -= 60
    call.note_barge()
    assert call.spoken[-1].playback == 'played' and call.spoken[-1].heard_by == 'estimate'
    assert call.spoken[-1].delivered == 'unknown'


def a_played_read_back_is_heard():
    """The positive case, by the runtime's own signals: it started, and it ran out before the caller spoke."""
    call = Call()
    call.turn_started('I want a haircut')
    asked(call)
    call.note_reached()
    call._burst -= 60
    call.note_drained()
    assert call.spoken[-1].delivered == 'delivered' and call.spoken[-1].heard_by == 'signal'
    call.turn_started('yes')
    assert call.pending('book', value='slot-1') is not None


def main() -> None:
    a_read_back_never_recorded_is_not_heard()
    a_read_back_queued_and_never_played_is_not_heard()
    text_with_no_acknowledgement_is_unknown()
    a_partly_read_read_back_is_not_heard()
    the_full_repeat_after_a_cut_copy_is_heard_once()
    the_same_words_for_another_question_prove_nothing()
    a_late_stop_cannot_play_a_copy_queued_after_the_cut()
    time_passing_is_not_delivery()
    a_played_read_back_is_heard()
    print(
        'delivery ok: no record, queued, unacknowledged, partial, estimated and late copies never authorise'
    )


if __name__ == '__main__':
    main()
