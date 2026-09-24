"""A conversation's acts as classes: a use case gives each one its information, and core runs them all alike.

Every voice agent that changes something for a caller hears the same few
things: a yes to a read-back, a pick from options on the table, a choice
between fixed options, a value said out loud, a change taken back, an
approval kept through a detour, a goodbye. Each is a class here. What a use
case writes is only the information each one needs:

| Class | The use case gives it |
|---|---|
| `ReadBack` | the question it answers, the action a yes runs, and when a yes counts |
| `FixedChoice` | the question, its options, and the action for each option |
| `Offer` | where the options on the table come from, and what a pick does |
| `HeardValue` | a parser from words to a value, and how the value is read back |
| `TakeBack` | the kind of thing held, and what taking it back does |
| `HeldApproval` | the read-back whose yes is kept, and how it is kept |
| `Detour` | the question that ends a prerequisite's check |
| `Closing` | the goodbye |

`Conversation` puts them together with the use case's context sentence and
model. It builds each act's narrow reading (`core.readings`), runs the ones
that apply to a turn side by side, and tries each act's rule in core's
fixed order (`Acts`): answers to the question waiting, then take-backs, then
picks, then goodbye. Inside each slot, the order given is the order tried.

A rule reads the reply through `Conversation.label`, one merged view, so a
use case that reads replies some other way (the salon's opt-in solver) can
stand in for the readings by overriding that one method.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import ClassSet, Exchange
from core.graph import LocalAction
from core.readings import (
    CLOSING,
    CONFIRM,
    REQUEST,
    STILL_AGREED,
    TAKE_BACK,
    Acts,
    ChoiceInput,
    ChoiceReading,
    DetourInput,
    Option,
    ReadBackInput,
    Reading,
    TakeBackInput,
    choose,
    classify,
    read,
)
from core.state import CallContext, Held


def _always(*_: Any) -> bool:
    return True


def _never(*_: Any) -> bool:
    return False


def _no_facts(call: Any) -> Mapping[str, Any]:
    return {}


@dataclass(kw_only=True)
class Act[C: CallContext]:
    """One conversational act. `Conversation` attaches it once, which is when its reading is built."""

    conversation: Conversation[C] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        self.conversation = conversation

    def label(self, call: C, name: str) -> str:
        return self.conversation.label(call, name)


@dataclass(kw_only=True)
class ReadBack[C: CallContext](Act[C]):
    """A question read back word for word, and the one action a yes to it runs.

    Every read-back of a conversation shares one reading, `confirmation`,
    with core's `CONFIRM` classes. A yes runs the action only while this
    question is the one waiting (`CallContext.pending`) and `when` holds;
    the action's own guard still checks the question before it writes.
    """

    about: str
    """The question, as the call records it (`Proposal.about`)."""
    action: str
    """The action's name, as the call records what it did."""
    run: Callable[[C, Any], Awaitable[object]]
    """What a yes runs, given the question that was agreed to."""
    when: Callable[[C, Any], bool] = _always
    """Whether a yes to this question counts here, given the question."""

    def rule(self, call: C) -> LocalAction | None:
        if self.label(call, 'confirmation') != 'accept':
            return None
        question = call.pending(self.about)
        if question is None or not self.when(call, question):
            return None
        return LocalAction(self.action, lambda: self.run(call, question))


@dataclass(kw_only=True)
class FixedChoice[C: CallContext](Act[C]):
    """A question with fixed options, such as "move the booking you hold, or add another?", and what each does."""

    name: str
    """The reading's name, which is also its Langfuse dataset's."""
    about: str
    """The question the options answer."""
    options: tuple[Option, ...]
    context: str
    """What this reading is for, after the conversation's own context sentence. Prompt text."""
    actions: Mapping[str, Callable[[C, Any], LocalAction | None]]
    """For each option id, the action choosing it takes, given the question. None to let the step's model answer."""
    also: Callable[[C], bool] = _never
    """When the reply can make this choice with no question asked, such as a time picked by someone who holds one."""
    when: Callable[[C], bool] = _always
    """Whether a choice counts here."""
    reading: ChoiceReading[C, ChoiceInput] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.choose(self.name, ChoiceInput, self._snapshot, context=self.context)

    def _snapshot(self, call: C) -> ChoiceInput | None:
        question = call.pending_question()
        asked = question is not None and question.about == self.about
        if not asked and not self.also(call):
            return None
        return ChoiceInput(question=call.said_last_turn(), answer=call.heard, options=self.options)

    def rule(self, call: C) -> LocalAction | None:
        question = call.pending(self.about)
        if question is None or not self.when(call):
            return None
        act = self.actions.get(self.label(call, self.name))
        return act(call, question) if act is not None else None


@dataclass(kw_only=True)
class Offer[C: CallContext, InputT: ChoiceInput](Act[C]):
    """Options put on the table, such as free times. A pick is the caller's choice, and `on_pick` says what it does.

    A pick is not a yes: `on_pick` normally reads the option back. Only an
    id among the options the reading was sent counts (`ChoiceReading.picked`).
    """

    name: str
    about: str
    """The question that puts the options on the table."""
    options: Callable[[C], tuple[Option, ...]]
    """The options the caller can pick from right now. Empty means there is nothing to pick."""
    context: str
    on_pick: Callable[[C, str], LocalAction | None]
    """What picking this option id does."""
    input_type: type[InputT]
    """What the reading is sent: `ChoiceInput`, or a subclass with the use case's own facts."""
    also_on: tuple[str, ...] = ()
    """Other questions a pick can answer, besides this offer and no question at all."""
    facts: Callable[[C], Mapping[str, Any]] = _no_facts
    """More input for the reading, for a use case whose `input_type` carries more facts than the options."""
    named: bool = False
    """Whether the reading also says which option was named with a condition or a question (`NamedChoice`).

    A prompt change: measured on the reading's dataset before it is turned on.
    """
    reading: ChoiceReading[C, InputT] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.choose(
            self.name, self.input_type, self._snapshot, context=self.context, named=self.named
        )

    def _snapshot(self, call: C) -> InputT | None:
        question = call.pending_question()
        if question is not None and question.about not in (self.about, *self.also_on):
            return None
        if not (table := self.options(call)):
            return None
        return self.input_type(
            question=call.said_last_turn(), answer=call.heard, options=table, **self.facts(call)
        )

    def rule(self, call: C) -> LocalAction | None:
        option = self.label(call, self.name)
        return self.on_pick(call, option) if option else None

    def refuses(self, call: C, option: str) -> bool:
        """Whether reading `option` back now would skip what the caller asked about it.

        The caller named it with a condition, a question or a doubt, and did
        not simply pick it: "nine, but only if it is under 30 euros". Answer
        that first. Only with `named`, and only once this turn's reading ran.
        """
        return (
            self.reading.names
            and self.reading.ran(call)
            and self.reading.named(call) == option
            and not self.label(call, self.name)
        )


@dataclass(kw_only=True)
class HeardValue[C: CallContext](Act[C]):
    """A value code can read from the caller's words, such as a phone number, read back with no model request.

    After the read-backs in its slot, so a yes that repeats the value is the
    yes. The same value said again to its own read-back is left to the model.
    """

    about: str
    """The read-back the value becomes."""
    action: str
    parse: Callable[[str], str]
    """The value in the caller's words, or '' when there is none. Code, never a model."""
    read_back: Callable[[C, str], Awaitable[object]]
    when: Callable[[C], bool] = _always

    def rule(self, call: C) -> LocalAction | None:
        if not self.when(call) or not (value := self.parse(call.heard)):
            return None
        asked = call.last_proposal()
        if asked is not None and asked[0].about == self.about and asked[0].value == value:
            return None
        return LocalAction(self.action, lambda: self.read_back(call, value))


@dataclass(kw_only=True)
class TakeBack[C: CallContext](Act[C]):
    """Something done in this call that the caller can still take back, on whatever turn they do.

    Only `withdraw` acts. A `change` (the same thing, another day, time or
    service) is left to the step's model, which can move it; offering the
    undo there was offering a cancel to a caller who asked to move.
    """

    name: str
    kind: str
    """What is held on the call (`CallContext.hold`)."""
    context: str
    on_withdraw: Callable[[C, Held], LocalAction | None]
    """What taking it back does, normally a read-back of the undo."""
    still_holds: Callable[[C, Held], bool] = _always
    """Whether it still stands, for a use case whose records can change under the call."""
    reading: Reading[C, TakeBackInput] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.classify(
            self.name, TAKE_BACK, TakeBackInput, self._snapshot, context=self.context
        )

    def _standing(self, call: C) -> Held | None:
        held = call.held(self.kind)
        return held if held is not None and self.still_holds(call, held) else None

    def _snapshot(self, call: C) -> TakeBackInput | None:
        if (held := self._standing(call)) is None:
            return None
        return TakeBackInput(question=call.said_last_turn(), answer=call.heard, agreed=held.told)

    def rule(self, call: C) -> LocalAction | None:
        held = self._standing(call)
        if held is None or self.label(call, self.name) != 'withdraw':
            return None
        return self.on_withdraw(call, held)


@dataclass(kw_only=True)
class HeldApproval[C: CallContext](Act[C]):
    """A yes to a read-back, kept while a prerequisite runs, and dropped on any change of mind.

    The yes is kept by `keep` on the turn it is given. Every later reply is
    read against it (`STILL_AGREED`), and anything but a clear keep lets it go.
    """

    name: str
    kind: str
    about: str
    """The read-back whose yes is kept."""
    context: str
    keep: Callable[[C, Any], None]
    """Hold the approved question on the call, as this kind."""
    reading: Reading[C, TakeBackInput] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.classify(
            self.name, STILL_AGREED, TakeBackInput, self._snapshot, context=self.context, settle=self._settle
        )

    def approve(self, call: C, label: str) -> None:
        """Keep the yes, if this reply accepted this read-back."""
        if label == 'accept' and (question := call.pending(self.about)):
            self.keep(call, question)

    def _snapshot(self, call: C) -> TakeBackInput | None:
        """Not on the turn of the yes itself, which the read-back's reading already read."""
        held = call.held(self.kind)
        if held is None or held.turn + 1 >= call.turn:
            return None
        return TakeBackInput(question=call.said_last_turn(), answer=call.heard, agreed=held.told)

    def _settle(self, call: C, label: str) -> None:
        if label != 'keep':
            call.release(self.kind)


@dataclass(kw_only=True)
class Detour[C: CallContext](Act[C]):
    """A prerequisite's check that interrupted a request: whether the caller still wants it as they asked.

    Read only on the reply to `ends_on`, the question that ends the check,
    because that is the turn a return to the request can happen on.
    """

    name: str
    ends_on: str
    context: str
    reading: Reading[C, DetourInput] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.classify(
            self.name, REQUEST, DetourInput, self._snapshot, context=self.context, settle=self._settle
        )

    def _snapshot(self, call: C) -> DetourInput | None:
        if not call.detour_request or call.pending(self.ends_on) is None:
            return None
        return DetourInput(
            question=call.detour_speech(), answer=' '.join(call.detour_words()), request=call.detour_request
        )

    def _settle(self, call: C, label: str) -> None:
        if label == 'kept':
            call.note_request_kept()


@dataclass(kw_only=True)
class Closing[C: CallContext](Act[C]):
    """The end of the call: read on any question but a read-back, and last in the order, after every agreed action."""

    goodbye: Callable[[C], Awaitable[object]]
    """What ending the call does."""
    name: str = 'conversation'
    about: str = 'closing'
    """The "anything else?" question."""
    action: str = 'hang_up'
    context: str = ''
    skip_before_model: tuple[str, ...] = ()
    """Readings left out on the closing question before any model runs: the ones that never decided one there."""
    reading: Reading[C, Exchange] = field(init=False, repr=False)

    def attach(self, conversation: Conversation[C]) -> None:
        super().attach(conversation)
        self.reading = conversation.classify(
            self.name, CLOSING, Exchange, self._snapshot, context=self.context
        )

    def _snapshot(self, call: C) -> Exchange:
        """The words of the question waiting, which is what the caller answers, or else the last thing said."""
        question = call.pending_question()
        said = question.spoken if question is not None and question.spoken else call.said_last_turn()
        return Exchange(question=said, answer=call.heard)

    def rule(self, call: C) -> LocalAction | None:
        if call.pending_question() is not None and self.label(call, self.name) == 'end':
            return LocalAction(self.action, lambda: self.goodbye(call))
        return None


class Conversation[C: CallContext]:
    """A use case's acts, with its context sentence and model: the readings they need, and the order they act in.

    The readings are built once, here, from what each act was given, and
    are what a trace, a test and a Langfuse dataset know them by. Nothing in
    this class names a business.
    """

    def __init__(
        self,
        *,
        context: str,
        model: Model | str,
        closing: Closing[C],
        settings: ModelSettings | None = None,
        read_back_context: str = '',
        read_backs: Sequence[ReadBack[C]] = (),
        choices: Sequence[FixedChoice[C]] = (),
        offers: Sequence[Offer[C, Any]] = (),
        values: Sequence[HeardValue[C]] = (),
        take_backs: Sequence[TakeBack[C]] = (),
        held: Sequence[HeldApproval[C]] = (),
        detour: Detour[C] | None = None,
    ) -> None:
        self.context = context
        """Who is talking, in one sentence. Every reading's context starts with it. Prompt text."""
        self.model = model
        self.settings = settings
        self.read_backs = tuple(read_backs)
        self.offers = tuple(offers)
        self.held = tuple(held)
        self.closing = closing
        acts: list[Act[C]] = [*read_backs, *choices, *offers, *values, *take_backs, *held]
        for act in (*acts, *([detour] if detour else []), closing):
            act.attach(self)
        self.confirmation = (
            self.classify(
                'confirmation',
                CONFIRM,
                ReadBackInput,
                self._read_back,
                context=read_back_context,
                settle=self._approve,
            )
            if read_backs
            else None
        )
        self.readings: tuple[Reading[C, Any], ...] = (
            *([self.confirmation] if self.confirmation else []),
            *(c.reading for c in choices),
            *(o.reading for o in offers),
            *(t.reading for t in take_backs),
            *(h.reading for h in held),
            *([detour.reading] if detour else []),
        )
        """Every reading that runs on its own when it applies. The closing reading joins them as `read` says."""
        self._by_name = {r.name: r for r in (*self.readings, closing.reading)}
        self.acts = Acts[C](
            answer=tuple(a.rule for a in (*choices, *read_backs, *values)),
            take_back=tuple(t.rule for t in take_backs),
            pick=tuple(o.rule for o in offers),
            goodbye=(closing.rule,),
        )

    @property
    def read_back_abouts(self) -> tuple[str, ...]:
        """The questions that wait on a yes to an exact proposal."""
        return tuple(r.about for r in self.read_backs)

    def reading(self, name: str) -> Reading[C, Any]:
        return self._by_name[name]

    def classify[InputT: Exchange](
        self,
        name: str,
        classes: ClassSet,
        input_type: type[InputT],
        snapshot: Callable[[C], InputT | None],
        *,
        context: str,
        settle: Callable[[C, str], None] | None = None,
    ) -> Reading[C, InputT]:
        """A reading with fixed classes, on this conversation's context and model."""
        return classify(
            name,
            classes,
            input_type,
            snapshot,
            context=self._context(context),
            model=self.model,
            settings=self.settings,
            settle=settle,
        )

    def choose[InputT: ChoiceInput](
        self,
        name: str,
        input_type: type[InputT],
        snapshot: Callable[[C], InputT | None],
        *,
        context: str,
        named: bool = False,
    ) -> ChoiceReading[C, InputT]:
        """A reading that picks one option by id, on this conversation's context and model."""
        return choose(
            name,
            input_type,
            snapshot,
            context=self._context(context),
            model=self.model,
            settings=self.settings,
            named=named,
        )

    def offer(self, name: str) -> Offer[C, Any]:
        """The offer whose reading is called `name`."""
        return next(o for o in self.offers if o.name == name)

    def _context(self, own: str) -> str:
        return ' '.join(part for part in (self.context, own) if part)

    def _read_back(self, call: C) -> ReadBackInput | None:
        question = call.pending_question()
        if question is None or question.about not in self.read_back_abouts:
            return None
        return ReadBackInput(question=call.said_last_turn(), answer=call.heard, proposal=question.spoken)

    def _approve(self, call: C, label: str) -> None:
        """A yes to a read-back whose approval is kept is kept, on the turn it is given."""
        for held in self.held:
            held.approve(call, label)

    def reading_label(self, call: C, name: str) -> str:
        """What this turn's reading says: for a choice, only an option it was sent; otherwise its label or default."""
        reading = self._by_name[name]
        return reading.picked(call) if isinstance(reading, ChoiceReading) else reading.label(call)

    def label(self, call: C, name: str) -> str:
        """The label every rule acts on. Override it to read replies another way; the rules then follow that."""
        return self.reading_label(call, name)

    def labels(self, call: C) -> dict[str, str]:
        """Every reading's label this turn, from the readings themselves."""
        return {name: self.reading_label(call, name) for name in self._by_name}

    async def read(
        self,
        call: C,
        *,
        usage: RunUsage | None = None,
        usage_limits: UsageLimits | None = None,
        closing: bool = False,
        before_model: bool = False,
    ) -> None:
        """Run every reading that applies to this turn, side by side. Each runs at most once per turn.

        Whether the call is over is read beside every other reading whenever a
        question waits, a read-back included: a yes and a goodbye in one reply
        are both acted on (`Acts.decide`). It costs one more small request on
        a read-back turn, side by side, so no longer a wait. `closing` reads it
        with no question waiting, when the step's model asks to hang up.

        `before_model` is a turn read before any model runs: on the closing
        question it leaves out the readings `Closing.skip_before_model` names,
        which a tool that needs them still runs itself.
        """
        question = call.pending_question()
        ending = closing or question is not None
        deferred = before_model and question is not None and question.about == self.closing.about
        readings = [r for r in self.readings if not (deferred and r.name in self.closing.skip_before_model)]
        await read(
            call,
            (*readings, *((self.closing.reading,) if ending else ())),
            usage=usage,
            usage_limits=usage_limits,
        )

    def decide(self, call: C) -> LocalAction | None:
        """The action this turn's readings decide on their own, or None to let the step's model answer."""
        return self.acts.decide(call)
