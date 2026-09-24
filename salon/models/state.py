"""What a call to Sage and Stone knows, and the call itself.

Only what the conversation can establish is here. The date, the weekday and
the salon clock are not: the diary works those out in code, so nothing has to
be told to the model and nothing can be got wrong.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, computed_field, model_validator

from core import state as core
from core.proposals import Proposal
from salon.diary import Diary, day_phrases
from salon.models.consent import Asked, ConsentInput, OfferedTime, ReplyClassification
from salon.models.receipts import Slot
from salon.models.shapes import AgentName, Date, Id, Phone, Service, Time, Verification


class Appointment(BaseModel):
    """The last booking action that actually succeeded.

    Every field comes from the diary, never from the model: `spoken` in
    particular is the one phrase to say this booking out loud, computed once so
    two agents cannot describe the same appointment two different ways.
    """

    model_config = core.STRICT

    booking_id: Id
    service: Service
    date: Date
    time: Time
    spoken: str
    action: Literal['book', 'move', 'cancel']


class Unresolved(BaseModel):
    """A write this call started and never learned the outcome of, and what it asked for.

    The token on its own was not enough. Reconciliation has to check the
    answer it finds against the request that produced it, and a token is a
    hash: the action, the booking and the offered slot cannot be read back out
    of it. Holding only the token, recovery took the action from whatever
    status came back, so a `cancelled` result refused on the first attempt as
    an answer to a `book` request was accepted on the second as a cancellation
    nobody had asked for.
    """

    model_config = core.STRICT

    token: str
    action: Literal['book', 'move', 'cancel', 'complaint']
    """What was asked for, so what comes back can be checked against it. A complaint is a write like any other."""
    booking_id: str = ''
    offered: dict[str, str] = {}
    """The slot this caller was offered, exactly as the diary returned it, so the recovered result meets the check the first answer met."""
    wanted: str = ''
    """What a complaint asked the salon to do, because it decides what the caller is told happens next."""


class BookingRequest(BaseModel):
    """What the caller asked to book before anyone knew who they were, in their own words.

    Carried by the front desk's handoff so that, once verification completes,
    the booking step can offer times without a model request to re-read a
    sentence the caller said a turn ago. The words stay the caller's: the diary
    still resolves the day, and still refuses a day they never named.
    """

    model_config = core.STRICT

    service: Service
    when: str = Field(min_length=1)
    at: str = ''


class Handoff(core.Handoff):
    to: AgentName


class SalonState(core.VoiceState):
    """The business facts of one salon call.

    Three facts about the caller's number are kept apart on purpose:
    `customer_phone` says the digits are a real Spanish number, `phone_confirmed`
    that the caller agreed to them when they were read back, and
    `customer_verified` what the customer lookup found. They used to be one
    field, which let structural validity stand in for identity, and a
    valid-length wrong number is still the wrong customer.
    """

    agent: AgentName = 'concierge'
    pending: AgentName | None = None
    """Narrowed from the base `str | None`: an unknown step name is a validation error, not a `KeyError` next turn."""

    customer_phone: Phone = ''
    """Prefetched from the carrier, and a guess until the caller agrees to it. Only the verify prompt renders it."""
    phone_confirmed: bool = False
    customer_verified: Verification = ''
    """'' until verification has run. Every prompt branches on this and every tool touching a customer record refuses without it.

    Written only by the verify step's finisher, from what the customer lookup
    came back with. It is a fact the service established, not a value the
    model may assert; the invariant below is what makes that enforceable.
    """
    verified_phone: Phone = ''
    """The number the lookup was performed against. Verification belongs to an identity, and this ties the two together."""

    @model_validator(mode='after')
    def _verification_belongs_to_one_identity(self) -> SalonState:
        """Drop verification that does not belong to the number now on the call.

        Two things this makes unrepresentable rather than merely discouraged: a
        status nobody earned, since `SalonState(customer_verified='existing')`
        with no matching `verified_phone` is not a verified caller; and
        authorisation surviving a correction, since a caller verified as one
        number who then gives a different one is a different person until the
        new number is looked up. Times offered to the old number go with it.

        Runs on every commit, because `commit` rebuilds the whole model, so
        there is no write path that can skip it. Writes go through `__dict__`
        because a plain assignment here recurses: `validate_assignment` re-runs
        the model validators on every set, and this is one of them.
        """
        if self.customer_phone != self.verified_phone:
            self.__dict__['customer_verified'] = ''
            self.__dict__['verified_phone'] = ''
            self.__dict__['phone_confirmed'] = False
            self.__dict__['offered'] = []
        return self

    booking_day: Date | None = None
    """The day the diary was last read for, written by the read and never by the model."""
    offered: list[Slot] = Field(default_factory=list)
    """The times just spoken to the caller, with the diary's ids, written by the offer and never by the model.

    `offer_times` ends its run, so its result reaches history as a bare
    acknowledgement and the ids it offered were invisible: the model re-read
    the diary to find one before it could save, a whole extra request on the
    turn the caller picks a time. The booking prompt renders these instead.
    Emptied by the next read, a save, or a change of identity.
    """
    wanted: BookingRequest | None = None
    """The booking the caller asked for at the front desk, until the diary is read for it."""
    appointment: Appointment | None = None
    writes: int = 0
    """How many business writes this call has had accepted.

    Part of the name of the next one: an operation token is derived from what
    the action *is*, so booking a slot, cancelling it and booking the same slot
    again must derive a new token, or the second write returns the first one's
    outcome and writes nothing. A retry of one intent happens with this
    unchanged and still reconciles; a new intent follows an accepted write.
    """
    unresolved: Unresolved | None = None
    """The write this call started and never learned the outcome of.

    While it is set, no other write may run: an unanswered write is not a
    write that did not happen, and a sentence of retry text asking the model
    not to try again is not a guard.
    """


_WITHHELD = ('266696687', '7378742833', '8656696', '86282452253', '7748283')
"""Keypad spellings of the words a carrier substitutes for a withheld number. They pass a shape check because they are the right shape."""


def prefetch_number(raw: str) -> str:
    """The caller's number, or '' when the carrier sent a placeholder instead.

    A withheld caller ID arrives three ways: empty, as a word, or as the keypad
    digits of a word an upstream carrier sent. All three mean the same thing,
    and a value that reaches a prompt is one the agent reads back as theirs.
    Only a value that *begins* with a spelling is refused: "unknown" spells
    8656696, and +86 5669 6xxx is an ordinary mobile.
    """
    text = (raw or '').strip()
    digits = text[1:] if text.startswith('+') else text
    if not (text.startswith('+') and digits.isascii() and digits.isdigit() and 8 <= len(digits) <= 15):
        return ''
    return '' if any(word.startswith(digits) for word in _WITHHELD) else text


_CLAIMED = re.compile(
    r"\b(?:i|we)(?:'ve| have|'ll have| had)?\s+(?:just\s+)?"
    r'(?:cancelled|canceled|booked|moved|rebooked)\b'
    r"|\bi(?:'ve| have)\s+(?:got\s+)?you\s+(?:down|booked|in)\b",
    re.IGNORECASE,
)
"""A first-person claim that a booking has already been changed.

Only the perfect forms, and only first person: "you're booked for Friday" is
the agent recounting what the caller holds, while "I've cancelled that" is a
report of work just done.
"""
_RECORDED = re.compile(
    r"\b(?:i|we)(?:'ve| have)?\s+(?:just\s+)?(?:recorded|logged|noted|written down)\b"
    r"|\b(?:that|it|your complaint)(?:'s| is| has been)\s+(?:on record|recorded|logged)\b",
    re.IGNORECASE,
)
_DAYS_REMEMBERED = 4
"""How many days back the caller's own words are kept: two corrections and the original."""


class SalonCall(core.CallContext):
    """This call's state, history, line and salon. The agents' `deps`.

    The question the call is waiting on an answer to is the only authority for
    a consequential action. It replaced a boolean the model filled in:
    `confirmed=True` was the caller's agreement as asserted by the party that
    wanted the write to go through.
    """

    state: SalonState = Field(default_factory=SalonState)
    _diary: Diary = PrivateAttr(default_factory=Diary)
    """The salon's books. A private attribute because the prompt templates are compiled against this model's schema."""
    _writes_before: int = PrivateAttr(default=0)
    """What `writes` stood at when this turn began, so "did anything land in this turn" is answerable."""
    _classified_turn: int = PrivateAttr(default=-1)
    _classification_input: ConsentInput | None = PrivateAttr(default=None)
    _classification: ReplyClassification = PrivateAttr(default_factory=ReplyClassification)
    _complaint: Asked | None = PrivateAttr(default=None)
    """A complaint readback the caller approved, kept until it is recorded or the caller moves on."""
    _undo: tuple[str, str, int] | None = PrivateAttr(default=None)
    """The booking just made, as (booking id, what the caller was told, turn), so the caller can take it back.

    A pick of an offered time books at once, with no read-back. The caller
    who then hears the price and says "leave it" was told goodbye with the
    booking still in the diary. The very next reply is read against it by the
    consent classifier (`undoable`); after that, each step's model can offer
    to cancel it (`offer_cancel`) for as long as the caller still holds it.
    """
    _day_words: list[str] = PrivateAttr(default_factory=list)
    """Every day the caller has named, in their own words, from whichever turn.

    Separate from the resolved date on purpose: Pydantic can validate that a
    date exists, but only the utterance says whether "Friday" was turned into
    "next Friday" on the way to the diary. Bounded, or a long call naming a
    dozen days would end up permitting all of them.
    """

    @property
    def diary(self) -> Diary:
        """The salon's books this call reads and writes."""
        return self._diary

    def turn_started(self, heard: str = '') -> None:
        """Also age out a question nobody answered, and mark where the write count stood.

        The count is marked when a caller turn begins, not again for a later
        piece of the same turn: a booking written on its first piece is still
        this turn's write, and its confirmation is not an unbacked claim.

        A question is answerable on the turn after it was asked and no later;
        left standing, a "yes" to something else entirely three turns on would
        still find it. An approved complaint survives only while the caller is
        being verified for it and only under the identity it was approved for.
        """
        super().turn_started(heard)
        if not self.continuing:
            self._writes_before = self.state.writes
        self._classified_turn = -1
        self._classification_input = None
        self._classification = ReplyClassification()
        if self._complaint and self.state.agent != 'verify':
            self._complaint = None
        if self._complaint and self._complaint.owner and self._complaint.owner != self.state.customer_phone:
            self._complaint = None
        for words in day_phrases(heard):
            if words not in self._day_words:
                self._day_words = [*self._day_words, words][-_DAYS_REMEMBERED:]

    def identity(self) -> str:
        """The caller is whoever the number on the call says, until corrected."""
        return self.state.customer_phone

    def ask(self, proposal: Proposal) -> None:
        """Record a salon question: validated as `Asked`, and a new complaint readback forgets an approved one."""
        question = Asked.model_validate(proposal.model_dump())
        if question.about == 'complaint':
            self._complaint = None
        super().ask(question)

    def pending_question(self) -> Asked | None:
        """The current proposal, excluding stale identities and interrupted readbacks."""
        return super().pending_question()

    def pending(self, about: str, **fields: str) -> Asked | None:
        """The question the caller is answering right now, if it is this one.

        Only the turn after it was asked: a question put two turns ago has
        been overtaken by whatever the caller said in between.
        """
        return super().pending(about, **fields)

    def last_proposal(self) -> tuple[Asked, bool] | None:
        """Last turn's question under this identity, and whether the caller heard it in full.

        Wider than `pending_question`, for interpretation only: a readback the
        caller spoke over can still be corrected or abandoned, but it can never
        be accepted, so nothing that authorises an action reads this.
        """
        return super().last_proposal()

    def consent_snapshot(self) -> ConsentInput:
        """The application-owned context a consent answer is an answer to, as of now."""
        previous = [segment for segment in self.all_spoken if segment.turn < self.turn]
        last_turn = max((segment.turn for segment in previous), default=-1)
        return ConsentInput(
            answer=self.heard,
            question=' '.join(s.text for s in previous if s.turn == last_turn).strip(),
            owner=self.state.customer_phone,
            pending=self.pending_question(),
            retained_complaint=self._complaint,
            just_changed=self._undo[1] if self._undo is not None and self.undoable else '',
            offered=self._table(),
            holding=tuple(
                f'{b["service"]} {b["spoken"]}'
                for b in self._diary.bookings_for(self.state.customer_phone, self._diary.now().date())
            )
            if self.state.customer_phone
            else (),
        ).model_copy(deep=True)

    def _table(self) -> tuple[OfferedTime, ...]:
        """The times the caller can pick from right now.

        The pending offer's, when the question is an offer. Otherwise the ones
        the diary has on the table for this call, so a caller who names a time
        before anything was offered ("Friday at nine") is read against real
        slots too. Either way only slots the diary offered this call are here.
        """
        question = self.pending('offer')
        if question is not None and question.value:
            ids = question.value.split(',')
            return tuple(
                OfferedTime(slot_id=slot.slot_id, spoken=slot.spoken)
                for slot in self.state.offered
                if slot.slot_id in ids
            )
        phone = self.state.customer_phone
        return tuple(
            OfferedTime(slot_id=row['slot_id'], spoken=row['spoken'])
            for row in self._diary.offered_to(self.id, phone)
        )

    def picked_offer(self) -> str:
        """The slot on the table this reply picks or asks for, or ''.

        The classifier names it; the application decides whether it counts:
        only an id on the table for this call, so no reading of the reply can
        authorise a time that was never offered.
        """
        chosen = self.classification.chosen_offer
        return chosen if chosen and chosen in {o.slot_id for o in self._table()} else ''

    def offer_undo(self, booking_id: str, told: str) -> None:
        """A booking was just made and confirmed in these words: the caller's next reply may take it back."""
        self._undo = (booking_id, told, self.turn)

    @property
    def undo_booking(self) -> str:
        """The booking made in this call that the caller could still take back, while they still hold it, or ''."""
        if self._undo is None:
            return ''
        held = self._diary.bookings_for(self.state.customer_phone, self._diary.now().date())
        return self._undo[0] if any(b['booking_id'] == self._undo[0] for b in held) else ''

    @property
    def undoable(self) -> str:
        """The booking this reply could take back without a model: made on the caller turn before this one, or ''.

        Only the very next reply is read against it, because only then is a
        question already pending and its classification already paid for.
        Later on, the step's own model offers the undo (`offer_cancel`).
        """
        return self.undo_booking if self._undo is not None and self._undo[2] == self.turn - 1 else ''

    def record_classification(self, snapshot: ConsentInput, result: ReplyClassification) -> None:
        """Keep this turn's consent reading, bound to the snapshot it read. Only an interpreter calls this."""
        self._classified_turn = self.turn
        self._classification_input = snapshot
        self._classification = result

    @property
    def classification(self) -> ReplyClassification:
        """Only an interpretation of this input, identity and still-current proposal."""
        snapshot = self._classification_input
        if self._classified_turn != self.turn or snapshot is None:
            return ReplyClassification()
        now = self.consent_snapshot()
        if (
            snapshot.answer != now.answer
            or snapshot.owner != now.owner
            or snapshot.pending != now.pending
            or snapshot.offered != now.offered
            or snapshot.holding != now.holding
        ):
            return ReplyClassification()
        return self._classification

    @computed_field
    @property
    def complaint_approved(self) -> bool:
        """Whether approved complaint wording still belongs to this caller. Never model-authored."""
        approved = self._complaint
        return approved is not None and (not approved.owner or approved.owner == self.state.customer_phone)

    @property
    def day_words(self) -> tuple[str, ...]:
        """The days the caller has named, in their own words, most recent last."""
        return tuple(self._day_words)

    def wrote_this_turn(self) -> bool:
        """Whether this turn has already changed the salon's books."""
        return self.state.writes != self._writes_before

    def claims_completion(self, clause: str) -> bool:
        """Whether this clause tells the caller a booking changed, when none has.

        The same three conditions `unbacked_claim` applies to a whole turn,
        asked of one clause on its way out, so on a step that can write the
        answer can be to not say them at all.
        """
        return not self.wrote_this_turn() and bool(self._invented(clause))

    def _invented(self, text: str) -> str:
        """The first-person claim of a change this state does not bear out, or ''."""
        if hit := _RECORDED.search(text):
            return hit.group(0)
        if not (hit := _CLAIMED.search(text)):
            return ''
        said = hit.group(0).lower()
        held = self.state.appointment
        if 'cancel' in said and (held is None or held.action != 'cancel'):
            return hit.group(0)
        if 'cancel' not in said and held is None:
            return hit.group(0)
        return ''

    def unbacked_claim(self) -> str:
        """Model speech this turn claiming a booking change the salon cannot corroborate.

        Three conditions, because any one of them alone is ordinary speech:
        nothing was written in this turn, the model said in the first person
        that it had just changed a booking, and the state does not bear that
        out. An agent recounting an appointment made earlier in the call has a
        state that agrees with it and does not trip this.
        """
        if self.wrote_this_turn():
            return ''
        for segment in self.spoken:
            if segment.kind == 'model' and (claim := self._invented(segment.text)):
                return claim
        return ''


def open_call(from_number: str = '', diary: Diary | None = None) -> SalonCall:
    """A fresh call on the salon's books. Nothing here can fail it; a fact we cannot get is left unset.

    It does **not** empty the diary. The diary is the business, shared by
    everyone who rings; only the conversation is per-call. Without a diary the
    call gets its own empty one, which is what a test wants and a bot must
    never rely on.
    """
    call = SalonCall(state=SalonState(customer_phone=prefetch_number(from_number)))
    if diary is not None:
        call._diary = diary
    return call
