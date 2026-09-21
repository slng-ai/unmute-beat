"""What a call to Sage and Stone knows.

Three of the original's six call variables are gone: the date, the weekday and
the salon clock. They existed so the model could work out which day "next
Friday" was and whether an afternoon slot had already passed. `salon.diary` does
both in code now, so nothing has to be told to the model and nothing can be got
wrong. What is left is what only the conversation can establish.
"""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime
from datetime import time as _time
from typing import Annotated, Literal

import phonenumbers
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PrivateAttr,
    TypeAdapter,
    model_validator,
)
from pydantic_extra_types.phone_numbers import PhoneNumberValidator

from salon import diary
from voice import harness

AgentName = Literal['concierge', 'verify', 'booking', 'complaints']
Service = Literal['haircut', 'hair-color', 'blowout']
# The lookup outcome, and only ever one of these. It used to be a bare `str`,
# which meant a typo in a service response became a verified caller.
Verification = Literal['', 'existing', 'created']


def _shape(pattern: str, message: str):
    compiled = re.compile(pattern)

    def check(value: str) -> str:
        # Empty is not a wrong value, it is no value yet: it is what a field
        # holds before anything fills it. Refusing it deadlocks the one step
        # whose job is to fill it, because it has nothing else to send.
        if value and not compiled.match(value):
            raise ValueError(message)
        return value

    return AfterValidator(check)


# Spain only, and Google's libphonenumber rather than a regex of our own. The
# call that made this necessary offered +348340464 and the old E.164 pattern
# took it: eight national digits is a real-looking number and nothing here knew
# the Spanish plan has nine. libphonenumber knows, and it also knows which
# ranges are ordinary contact numbers -- +34500111222 is the right length and
# still not a number anybody has.
#
# No `default_region`. With one, a bare "600111222" would be silently completed
# to +34600111222, which is exactly the guess `verify.md` tells the model never
# to make. Without one it is refused and the agent asks which country.
_spanish = TypeAdapter(
    Annotated[
        str | phonenumbers.PhoneNumber,
        PhoneNumberValidator(number_format='E164', supported_regions=['ES']),
    ]
)


def _spanish_or_absent(value: str) -> str:
    # Empty is not a wrong value, it is no value yet -- the same sentinel the
    # other shapes keep, and the only absence this field has.
    return value if value == '' else _spanish.validate_python(value)


def _spanish_and_present(value: str) -> str:
    """A number somebody actually gave us. Absence is not one of them.

    The state's `Phone` has to permit '', because that is what the field holds
    before anybody has said a number. A tool *argument* is the opposite case:
    it is a number being submitted for a lookup, and an empty one submitted as
    if it were a number is how `confirm_customer(phone='')` used to create an
    empty customer record, come back 'created', and leave the call verified as
    nobody. Same digits, different question, so a different type.
    """
    if not value:
        raise ValueError('no number was given. Ask the caller for one; never look up an empty number')
    return _spanish.validate_python(value)


def _one_day(value: object) -> _date:
    """A day, as a `date` or the ISO string a service sends. Nothing else is a day.

    The narrow reading matters more than the parse. Pydantic's ordinary lax
    mode would take `1700000000` here and call it a day in 2023: a number that
    reached a date field by accident would become a real, plausible, wrong
    appointment. `datetime` is refused too -- an appointment is on a day, and a
    day carrying a time of its own is two answers to one question.
    """
    if isinstance(value, datetime):
        raise ValueError('expected a day, not a moment in time')
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        # `date.fromisoformat` is the calendar check as well as the shape one:
        # it refuses 2026-02-30, which the regex this replaced accepted.
        return _date.fromisoformat(value)
    raise ValueError('expected a day written year-month-day, like 2026-03-19')


def _one_time(value: object) -> _time:
    """A wall-clock time in this salon's own day: no timezone, no seconds.

    The salon has one timezone and a half-hour grid. A time arriving with an
    offset is asserting something about where it is, and a time with seconds
    on it did not come off that grid; both are a service disagreeing with the
    diary rather than answering it.
    """
    if isinstance(value, str):
        value = _time.fromisoformat(value)
    if not isinstance(value, _time) or isinstance(value, datetime):
        raise ValueError('expected a 24-hour time, like 09:30')
    if value.tzinfo is not None:
        raise ValueError('expected a local salon time, with no timezone on it')
    if value.second or value.microsecond:
        raise ValueError('expected a time on the half hour, with no seconds on it')
    return value


# AfterValidator over a plain `str`, never the `PhoneNumber` type itself and
# never a `pattern=` constraint: both put a keyword in the schema the model is
# sent (`format: phone`, `pattern`) and some providers reject it. The
# description is how the shape reaches the model instead -- it travels as prose,
# so no strict schema converter can strip it.
Phone = Annotated[
    str,
    AfterValidator(_spanish_or_absent),
    Field(description='A Spanish phone number in full international form, like +34600111222.'),
]
# Native `date` and `time`, validated by Pydantic, rather than strings the
# application checks the shape of. The diary has always worked in these
# internally; what changes is that the boundary now agrees with it, so a
# comparison is a comparison of days and not of spellings. `_one_day` and
# `_one_time` are what keep the old refusals: an impossible date, a numeric
# timestamp, a timezone, a stray second.
Date = Annotated[_date, BeforeValidator(_one_day)]
Time = Annotated[_time, BeforeValidator(_one_time)]
Id = Annotated[str, _shape(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$', 'expected an identifier')]
# For tool arguments only. Never for state: see `_spanish_and_present`.
SubmittedPhone = Annotated[
    str,
    AfterValidator(_spanish_and_present),
    Field(description='A Spanish phone number in full international form, like +34600111222.'),
]


def unsupported_region(error: Exception) -> bool:
    """Whether a rejected number was well-formed but not Spanish.

    Two different conversations. A number from another country is a question --
    which country are you calling from, we can only look up Spanish numbers --
    and a malformed one is a re-ask. libphonenumber distinguishes them and the
    only carrier of that distinction is the message.
    """
    return 'not from a supported region' in str(error)


class Appointment(BaseModel):
    """The last booking action that actually succeeded.

    Every field comes from the diary, never from the model: `spoken` in
    particular is the one phrase to say this booking out loud, computed once so
    two agents cannot describe the same appointment two different ways.
    """

    model_config = harness.STRICT

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
    hash: the action, the booking and the offered slot cannot be read back
    out of it. Holding only the token, recovery took the action from
    whatever status came back -- so a `cancelled` result, correctly refused
    on the first attempt as an answer to a `book` request, was accepted on
    the second as an ordinary cancellation nobody had asked for.
    """

    model_config = harness.STRICT

    token: str
    # What was asked for, so what comes back can be checked against it.
    # `complaint` is here because a complaint is a write like any other and
    # fails the same three ways; it is settled by its own branch.
    action: Literal['book', 'move', 'cancel', 'complaint']
    booking_id: str = ''
    # The slot this caller was offered, exactly as `diary.offer_of` returned
    # it, so the recovered result meets the check the first answer met.
    offered: dict[str, str] = {}
    # What a complaint asked the salon to do, kept because it decides what the
    # caller is told happens next -- and a recovered write has to say the same
    # sentence the first attempt would have.
    wanted: str = ''


# --- what the caller actually said -------------------------------------------

# Closed word lists over the accepted utterance, not language understanding.
# Every one of them fails towards *asking*: an utterance none of these
# recognises is a reason to put the question again, never a reason to act. So
# the cost of a miss is one clarifying turn, and the cost of a false positive
# -- which is what a permissive "does it contain yes" check buys -- is a
# booking nobody asked for.
#
# ponytail: regex, not a classifier. Upgrade path, if the live calls show too
# many clarifications: run a small model *only* on the utterances these return
# nothing for, and keep them as the fast path.

# Anchored at the start. "Yes" has to be the answer, not a word inside one.
_YES = re.compile(
    r'^(?:yes|yeah|yep|yup|aye|sure|please|perfect|great|exactly|correct|right|'
    r'ok|okay|k|go ahead|do it|book it|sounds (?:good|right|fine|great)|'
    r"that(?:'s| is|s) (?:right|correct|it|fine|great|perfect)|that works|"
    r"i(?:'m| am) happy (?:with that|enough))\b",
    re.IGNORECASE,
)
# Anywhere in it. A yes that arrives with one of these attached is answering
# some other question, or changing the one that was asked.
_NOT_YES = re.compile(
    r"\b(?:no|nope|nah|not|don'?t|but|actually|instead|change|wait|hold on|rather|"
    r'different|another|else|sorry|maybe|unless|though)\b',
    re.IGNORECASE,
)
_BYE = re.compile(
    r"\b(?:bye|goodbye|good ?bye|see you|that(?:'s| is|s) (?:all|everything|it)|"
    r"nothing else|no more|i(?:'m| am) (?:good|fine|all set|done|done thanks)|"
    r"we(?:'re| are) done|thanks?,? (?:that(?:'s| is) )?all)\b",
    re.IGNORECASE,
)
# A goodbye with any of these in it is not only a goodbye. The live call of
# 2026-09-21 12:57 hung up on "Can you help me moving it to Saturday, the same
# time?" -- a question, answered with a goodbye string the model wrote itself.
_STILL_ASKING = re.compile(
    r'\?|\b(?:book|booking|move|moving|switch|change|cancel|reschedule|complain|complaint|'
    r'can you|could you|would you|what|when|where|how|why|which|who|also|another|instead|'
    r'but|actually|still|help|about)\b',
    re.IGNORECASE,
)
_MOVE_IT = re.compile(
    r'\b(?:move|moving|switch|swap|change|reschedule|instead of|rather than|replace)\b', re.IGNORECASE
)
_ADD_ONE = re.compile(
    r'\b(?:another|second|additional|extra|as well|also|on top|both|two appointments|'
    r'keep (?:the|that|it|both))\b',
    re.IGNORECASE,
)


def agrees(heard: str) -> bool:
    """Whether this utterance is the caller agreeing to the question they were asked.

    Not "does it contain a yes". The live call of 2026-09-21 13:02 asked the
    caller which country they were ringing from, took their yes, and used it
    as agreement to a phone number nobody had read back.
    """
    text = heard.strip()
    # A bound, so a yes buried at the front of a paragraph of new instructions
    # is not read as an answer to anything.
    return bool(text) and len(text.split()) <= 12 and bool(_YES.match(text)) and not _NOT_YES.search(text)


def ends(heard: str) -> bool:
    """Whether the caller's own words say the call is over.

    A question is never one, however tired it sounds, and neither is a goodbye
    with a request attached to it.
    """
    text = heard.strip()
    return bool(text) and bool(_BYE.search(text)) and not _STILL_ASKING.search(text)


def move_or_add(heard: str) -> Literal['move', 'add', '']:
    """Which of the two the caller asked for, or neither, which means ask them."""
    moving, adding = bool(_MOVE_IT.search(heard)), bool(_ADD_ONE.search(heard))
    if moving == adding:
        return ''
    return 'move' if moving else 'add'


# How many days back the caller's own words are kept. Four is two corrections
# and the original, which is more than any call here has needed.
_DAYS_REMEMBERED = 4


class Asked(BaseModel):
    """A question this call put to the caller, and is waiting on the answer to.

    Application-owned. A tool may *propose* the question; nothing a model
    writes can create one, and nothing it writes can claim one was answered.
    The turn it was asked on is part of it, so an agreement two turns later is
    not an answer to it.
    """

    model_config = harness.STRICT

    about: Literal['phone', 'booking', 'move_or_add', 'complaint']
    # The exact thing agreement would be agreement *to*: the number read back,
    # the slot offered, or the complaint summary paraphrased.
    value: str = ''
    action: Literal['book', 'move', 'cancel', ''] = ''
    booking_id: str = ''
    # The caller turn it went out on. Set by `SalonCall.ask`, never by a tool.
    turn: int = 0


class Handoff(harness.Handoff):
    to: AgentName


class SalonState(harness.VoiceState):
    agent: AgentName = 'concierge'
    # Narrowed from the base `str | None`: an unknown step name is now a
    # validation error rather than a `KeyError` on the next turn.
    pending: AgentName | None = None

    # Prefetched from the carrier, and a guess until the caller agrees to it.
    # Only `verify.md` renders it; no other prompt mentions it, so no other
    # agent can read somebody else's number back to them.
    customer_phone: Phone = ''
    # Three separate facts, kept apart on purpose. This one says only that the
    # digits are a real Spanish number; the next says the caller agreed to them
    # when they were read back; the third says what the customer lookup found.
    # They used to be one field, which let structural validity stand in for
    # identity -- and a valid-length wrong number is still the wrong customer.
    phone_confirmed: bool = False
    # '' until verification has run. Every prompt branches on this, and every
    # tool that touches a customer record refuses without it.
    #
    # Written only by `confirm_customer`, from what the customer lookup came
    # back with. It is a fact the service established, not a value the model
    # may assert: a correctly typed 'existing' is not evidence that a lookup
    # happened, and the invariant below is what makes the difference
    # enforceable rather than a convention.
    customer_verified: Verification = ''
    # The number that lookup was performed against. Verification belongs to an
    # identity, so this is what ties the two together -- without it, a caller
    # who corrects their number keeps the authorisation earned by the old one.
    verified_phone: Phone = ''

    @model_validator(mode='after')
    def _verification_belongs_to_one_identity(self) -> SalonState:
        """Drop verification that does not belong to the number now on the call.

        Two things this makes unrepresentable rather than merely discouraged:

        * A status nobody earned. `SalonState(customer_verified='existing')`
          with no matching `verified_phone` is not a verified caller, however
          well-typed the status is, so the booking prerequisite stays unmet.
        * Authorisation surviving a correction. A caller verified as one
          number who then gives a different one is a different person until
          the new number is looked up. Their old business record is untouched
          -- only the authority to act on this call goes.

        Runs on every commit, because `commit` rebuilds the whole model. That
        is the point: there is no write path that can skip it.
        """
        if self.customer_phone != self.verified_phone:
            # Through `__dict__`, because a plain assignment here recurses:
            # `validate_assignment` re-runs the model validators on every set,
            # and this is one of them. Checked, not assumed -- the plain form
            # raises `RecursionError` on construction.
            self.__dict__['customer_verified'] = ''
            self.__dict__['verified_phone'] = ''
            self.__dict__['phone_confirmed'] = False
        return self

    # The day the diary was last read for, written by `read_diary` and never by
    # the model. A slot is only bookable on this day, so a caller who changes
    # their mind about the day cannot be put back onto the old one: the offer
    # died with the question. The consequences of a correction are computed, not
    # asked about, because no prompt sentence can hold an invariant.
    booking_day: Date | None = None
    appointment: Appointment | None = None
    # How many business writes this call has already had accepted. Part of the
    # name of the next one, and that is its whole job: the operation token is
    # derived from what the action *is*, so a caller who books a slot, cancels
    # it and then books the same slot again derived the same token twice and
    # the second write returned the first one's outcome without writing
    # anything -- the diary empty, the call state holding an appointment that
    # no longer existed. A retry of one intent happens with this unchanged and
    # still reconciles; a new intent follows an accepted write and cannot.
    writes: int = 0
    # The write this call started and never learned the outcome of, and the
    # request that started it. While it is set, no other write may run: an
    # unanswered
    # write is not a write that did not happen, and the only thing that used
    # to stand between it and a second appointment was a sentence of retry
    # text asking the model not to try again.
    unresolved: Unresolved | None = None


# Keypad spellings of the words a carrier substitutes for a withheld number.
# They pass a shape check because they are the right shape: digits, about as
# many as a phone number has.
_WITHHELD = ('266696687', '7378742833', '8656696', '86282452253', '7748283')


def prefetch_number(raw: str) -> str:
    """The caller's number, or '' when the carrier sent a placeholder instead.

    A withheld caller ID arrives three ways: empty, as a word, or as the keypad
    digits of a word an upstream carrier sent. All three mean the same thing,
    and a value that reaches a prompt is one the agent reads back as their own.
    """
    text = (raw or '').strip()
    digits = text[1:] if text.startswith('+') else text
    if not (text.startswith('+') and digits.isascii() and digits.isdigit() and 8 <= len(digits) <= 15):
        return ''
    # Only this direction: a value that *begins* with a spelling is a real
    # number. "unknown" spells 8656696, and +86 5669 6xxx is an ordinary mobile.
    return '' if any(word.startswith(digits) for word in _WITHHELD) else text


# A first-person claim that a booking has already been changed. Only the
# perfect forms, and only first person: "you're booked for Friday" is the
# agent recounting what the caller holds, which is true and ordinary, while
# "I've cancelled that" is a report of work just done.
_CLAIMED = re.compile(
    r"\b(?:i|we)(?:'ve| have|'ll have| had)?\s+(?:just\s+)?"
    r'(?:cancelled|canceled|booked|moved|rebooked)\b'
    r"|\bi(?:'ve| have)\s+(?:got\s+)?you\s+(?:down|booked|in)\b",
    re.IGNORECASE,
)


class SalonCall(harness.CallContext):
    """This call's state, history and line. The agents' `deps`."""

    state: SalonState = Field(default_factory=SalonState)
    # What `writes` stood at when this turn began, so "did anything land in
    # this turn" is answerable without the turn having to carry it.
    _writes_before: int = PrivateAttr(default=0)
    # The question this call is waiting on an answer to, and nothing else is
    # authority for a consequential action. It replaces a boolean the model
    # filled in: `confirmed=True` was the caller's agreement as asserted by the
    # party that wanted the write to go through.
    _asked: Asked | None = PrivateAttr(default=None)
    # Every day the caller has named, in their own words, from whichever turn
    # they said it on. The live call of 2026-09-21 12:58 heard "Friday" and
    # looked up "next Friday", which under that clock is a week later, and
    # booked it. Bounded, because a long call naming a dozen days would
    # otherwise end up permitting all of them.
    _day_words: list[str] = PrivateAttr(default_factory=list)

    def turn_started(self, heard: str = '') -> None:
        """Also age out a question nobody answered, and mark where the write count stood."""
        super().turn_started(heard)
        self._writes_before = self.state.writes
        # A question is answerable on the turn after it was asked and no
        # later. Left standing, a "yes" to something else entirely three turns
        # on would still find it.
        if self._asked is not None and self._asked.turn < self._turn - 1:
            self._asked = None
        # The caller's own words for the day, from whichever turn they said
        # them on. Separate from the resolved date on purpose: Pydantic can
        # validate that a date exists, but only the utterance says whether
        # "Friday" was turned into "next Friday" on the way to the diary.
        for words in diary.day_phrases(heard):
            if words not in self._day_words:
                self._day_words = [*self._day_words, words][-_DAYS_REMEMBERED:]

    def ask(self, question: Asked) -> None:
        """Record that this question has gone out to the caller, on this turn."""
        self._asked = question.model_copy(update={'turn': self._turn})

    def pending(self, about: str, **fields: str) -> Asked | None:
        """The question the caller is answering right now, if it is this one.

        Only the turn after it was asked: a question put two turns ago has
        been overtaken by whatever the caller said in between.
        """
        asked = self._asked
        if asked is None or asked.about != about or asked.turn != self._turn - 1:
            return None
        return asked if all(getattr(asked, name) == value for name, value in fields.items()) else None

    def answered(self) -> None:
        """Consume the pending question. One answer authorises one action."""
        self._asked = None

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
        asked of one clause on its way out. Asked *before* the words are said,
        so on a step that can write the answer can be to not say them at all
        -- which is the difference between this and the correction that
        follows an invention the caller has already heard.
        """
        return not self.wrote_this_turn() and bool(self._invented(clause))

    def _invented(self, text: str) -> str:
        """The first-person claim of a change this state does not bear out, or ''."""
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

        Reproduced on the live call of 2026-09-21 12:23: *"I've cancelled
        your appointment, and I have you down for a redo on Saturday at
        2:30 PM."* No `save_booking` ran in that turn or after it. The
        appointment was still there, there was no redo, and the next turn
        promised the redo would be free. Every guard in this agency
        constrains a write that happens; nothing constrained a sentence
        about one that did not.

        Three conditions, because any one of them alone is ordinary speech:
        nothing was written in this turn, the model said in the first person
        that it had just changed a booking, and the state does not bear that
        out. An agent recounting an appointment made earlier in the call has
        a state that agrees with it and does not trip this.
        """
        if self.wrote_this_turn():
            return ''
        for segment in self.spoken:
            if segment.kind == 'model' and (claim := self._invented(segment.text)):
                return claim
        return ''


def open_call(from_number: str = '') -> SalonCall:
    """A fresh call. Nothing here can fail it; a fact we cannot get is left unset.

    It does **not** reset the diary. It used to, and that made every new call
    erase the salon's bookings: call B opening deleted the appointment call A
    had just made. The diary is the business, shared by everyone who rings;
    only the conversation is per-call. Fixtures reset it explicitly in their own
    setup, which is the only place that should.
    """
    return SalonCall(state=SalonState(customer_phone=prefetch_number(from_number)))
