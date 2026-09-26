"""Delivery: only a read-back the caller got in full can be answered, and only its latest copy counts.

`Spoken.delivered` is `delivered` only on evidence: the segment's own
transport mark, or a text reader's acknowledgement (simulated delivery). No
record, a queued copy, a copy estimated or signalled to have played, a copy
cut short or cleared, and a copy the caller spoke over before its mark came
back are all not heard. Copies
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


def the_stop_signal_alone_is_not_delivery():
    """Stage B3: the runtime's aggregate start and stop decide turn-taking only; they never authorise."""
    call = Call()
    call.turn_started('I want a haircut')
    asked(call)
    call.note_reached()
    call._burst -= 60
    call.note_drained()
    assert call.spoken[-1].playback == 'played' and call.spoken[-1].delivered == 'unknown'
    call.turn_started('yes')
    assert call.pending_question() is None, 'an aggregate stop signal authorised a yes'


def marked(call: Call, onset_after: float | None = 0.5) -> str:
    """A read-back whose mark was sent and came back; the caller then spoke `onset_after` seconds later."""
    copy = asked(call)
    name = call.mark_for(copy)
    call.note_mark_sent(name, 100.0)
    call.spoken[-1].at = 99.0
    assert call.note_mark_returned(name, 101.0)
    if onset_after is not None:
        call.note_onset(101.0 + onset_after)
    return name


def a_marked_read_back_answered_after_it_is_heard():
    """The positive case: its own mark came back, and the caller began after it."""
    call = Call()
    call.turn_started('I want a haircut')
    marked(call)
    assert call.spoken[-1].delivered == 'delivered' and call.spoken[-1].heard_by == 'mark'
    call.turn_started('yes')
    assert call.pending('book', value='slot-1') is not None


def an_answer_that_began_before_the_mark_is_not_to_it():
    """Onset before, at and after the mark, with the adapter's detection lead: only a clear after counts."""
    for after, lead, heard in ((-0.2, 0.0, False), (0.0, 0.0, True), (0.1, 0.2, False), (0.3, 0.2, True)):
        call = Call()
        call._answer_lead = lead
        call.turn_started('I want a haircut')
        marked(call, onset_after=after)
        call.turn_started('yes')
        assert (call.pending_question() is not None) is heard, (after, lead)
    call = Call()
    call.turn_started('I want a haircut')
    marked(call, onset_after=None)
    call.turn_started('yes')
    assert call.pending_question() is None, 'with marks and no onset, when the reply began is unknown'


def a_mark_with_no_audio_before_it_proves_nothing():
    """A TTS that produced no audio, and said nothing about it: its mark returns at once, and delivers nothing."""
    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    name = call.mark_for(copy)
    call.spoken[-1].at = 99.0
    call.note_mark_sent(name, 100.0, media=0)
    assert not call.note_mark_returned(name, 100.01), 'a mark with no media before it delivered its segment'
    call.note_onset(101.0)
    call.turn_started('yes')
    assert call.pending_question() is None


def a_fragmented_reply_is_timed_from_its_first_piece():
    """The caller began before the mark came back, paused, and went on after it: the reply began too early."""
    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    name = call.mark_for(copy)
    call.spoken[-1].at = 99.0
    call.note_onset(100.5)
    assert call.note_mark_returned(name, 101.0)
    call.note_onset(102.0)
    call.turn_started('yes')
    assert call.pending_question() is None, (
        'the second piece of a reply that began over the read-back approved it'
    )


def marks_that_prove_nothing_change_nothing():
    """Unrelated, duplicate and previous-generation marks, and a mark returned after a clear, are ignored."""
    call = Call()
    call.turn_started('I want a haircut')
    copy = asked(call)
    name = call.mark_for(copy)
    assert not call.note_mark_returned('other-call:1:1~0', 101.0), 'an unrelated mark counted'
    call.note_cleared(100.5)
    assert not call.note_mark_returned(name, 101.0), 'a mark pending at a clear counted'
    assert call.spoken[-1].delivered == 'unknown' and call.spoken[-1].cleared
    again = asked(call)
    second = call.mark_for(again)
    assert second.endswith('~1') and second != name, 'a mark after a clear must carry the new generation'
    assert call.note_mark_returned(second, 102.0)
    assert not call.note_mark_returned(second, 102.5), 'a duplicate mark counted twice'
    call.note_cleared(103.0)
    assert call.spoken[-1].delivered == 'delivered', 'a clear after a mark came back unplayed it'


def main() -> None:
    a_read_back_never_recorded_is_not_heard()
    a_read_back_queued_and_never_played_is_not_heard()
    text_with_no_acknowledgement_is_unknown()
    a_partly_read_read_back_is_not_heard()
    the_full_repeat_after_a_cut_copy_is_heard_once()
    the_same_words_for_another_question_prove_nothing()
    a_late_stop_cannot_play_a_copy_queued_after_the_cut()
    time_passing_is_not_delivery()
    the_stop_signal_alone_is_not_delivery()
    a_marked_read_back_answered_after_it_is_heard()
    an_answer_that_began_before_the_mark_is_not_to_it()
    a_mark_with_no_audio_before_it_proves_nothing()
    a_fragmented_reply_is_timed_from_its_first_piece()
    marks_that_prove_nothing_change_nothing()
    print(
        'delivery ok: no record, queued, unacknowledged, partial, estimated and late copies never authorise'
    )


if __name__ == '__main__':
    main()
