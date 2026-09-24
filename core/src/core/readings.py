"""Narrow readings: one small classifier per conversational act, run only when that act can happen.

A caller's reply can do a handful of things every voice agent has to hear:
answer a read-back, pick one of the options they were given, take back
something they agreed to, or end the call. One classifier asked about all of
them at once is sent every class and every fact on every turn, and its
readings blur into each other. Here each act is its own reading:

* its own input: the question and the answer, plus only the facts that act
  needs (the read-back, the options, the thing that could be taken back);
* its own classes, with their descriptions, in its output schema;
* its own `snapshot`: the application's state says whether the act can
  happen this turn and fills the input, so a reading that cannot apply is
  never sent.

The readings that apply run side by side, so a turn waits for the slowest
one, not for all of them in a row. `decide` then tries what the application
does on each act in one fixed order (`Acts`), the same for every business.

What a business writes is data and small functions: a context sentence per
reading, when each one applies, and the action each outcome takes. The class
sets below are generic; a business passes its own `ClassSet` only when its
act really has other outcomes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier, ClassSet, Exchange, output_model
from core.graph import LocalAction
from core.interpreters import ClassifierInterpreter
from core.state import CallContext


class ReadBackInput(Exchange):
    """A reply to a read-back: the exact proposal the person was asked to confirm."""

    proposal: str = Field(description='The exact proposal the person was read back and asked to confirm.')


class TakeBackInput(Exchange):
    """A reply read against something the person agreed to earlier, which they could still take back."""

    agreed: str = Field(
        description='What the person agreed to earlier, or was told had been done, which they could still take back.'
    )


class Option(BaseModel):
    """One option the person can pick, with the id a pick must name."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    id: str
    said: str
    """The option as it was said to the person."""


class ChoiceInput(Exchange):
    """A reply read against the options the person can pick from right now."""

    options: tuple[Option, ...] = Field(
        description='The options the person can pick from, each with its `id`.'
    )


CONFIRM = ClassSet(
    about='How the answer responds to the read-back in `proposal`.',
    classes={
        'accept': 'Clearly approves the exact proposal, without conditions or questions.',
        'reject': 'Declines the proposal.',
        'revise': 'Asks to change the proposal.',
        'conditional': 'Agrees only under an added condition ("yes, if...").',
        'unclear': 'Asks a question, or the answer is ambiguous or incomplete. An answer put as a question, even '
        'one that sounds like a yes, is unclear.',
    },
    default='unclear',
)
"""Version 3 of `harness-reading/confirm` in Langfuse. Over 3 runs on 42 items, it cut false accepts from 1 per
run to 0 on gemini-3.1-flash-lite and to 2 in 3 runs on gemini-3.5-flash-lite ("Go for it?"), with gpt-6-luna at 0.

ponytail: "Go for it?" can still read as accept on 3.5. The application still needs the read-back to have been
heard and the caller to be the one asked; a model or rule that reaches zero there is the upgrade.
"""

TAKE_BACK = ClassSet(
    about='Whether the answer takes back what is in `agreed`, which has already been done.',
    classes={
        'keep': 'Keeps what is in `agreed`, or does not take it back.',
        'withdraw': 'Takes back what is in `agreed`: asks to undo or cancel it, says to leave it, or that they '
        'no longer want it. A condition on it is not taking it back. Asking for it on another day, at another '
        'time or as another service is `change`.',
        'change': 'Wants what is in `agreed` done differently: another day, time or service. It is not undone.',
        'unclear': 'Whether they take it back is unclear, or they put a condition on it ("only if...") that '
        'has not been answered yet.',
    },
    default='unclear',
)
"""For a change already made that the person can still undo. Asking to change it is not taking it back.

Version 2 adds `change`. With only keep, withdraw and unclear, "Sorry, can
you actually move the appointment to Saturday instead?" was read as
`withdraw` (the user's call of 24 September), and the caller who asked to
move was offered a cancel.
"""

STILL_AGREED = ClassSet(
    about='Whether the person still agrees to what is in `agreed`, exactly as it is. It has not been done yet.',
    classes={
        'keep': 'Still agrees to it unchanged, or says nothing about it.',
        'withdraw': 'Takes it back, changes any part of it, or moves on to something else instead.',
        'unclear': 'Whether they still agree is uncertain.',
    },
    default='unclear',
)
"""For an approval the application holds until it can act on it: any change means asking again."""

CLOSING = ClassSet(
    about='Whether the answer ends the whole call.',
    classes={
        'end': 'Ends the whole call with nothing left to ask: a goodbye, "that\'s all", or a "no" to "anything '
        'else?". Thanks on their own, or a yes to what was proposed, do not end it.',
        'continue': 'Has more to discuss, asks a question, or only declines what was just proposed.',
        'unclear': 'Whether they want to end the call is unclear.',
    },
    default='unclear',
)
"""Version 3 names what ends a call and says a yes or thanks alone do not.

The closing reading now also runs beside a read-back (`Conversation.read`).
On version 1, "Yes, that's perfect, thank you." read as `end` on gpt-6-luna in
3 of 3 runs, on gpt-5.6-luna and gemini-3.1-flash-lite in 1 of 3. Version 2
("a yes or a thank-you ... does not, unless ...") fixed the GPT models and
broke gemini-3.5-flash-lite, 3 of 3. Version 3: 1.00 on 18 items, 3 runs on
each of the 4 models, no false `end` (harness-reading/closing, 24 September).
"""


class DetourInput(Exchange):
    """What was said while a prerequisite was checked, and the request that check interrupted.

    `question` is everything the agent said during the check and `answer`
    everything the person said, both in order.
    """

    request: str = Field(description='What the person asked for before the check began, in their words.')


REQUEST = ClassSet(
    about='Whether the person still wants `request` exactly as they asked for it.',
    classes={
        'kept': 'In `answer` the person only answered what they were asked (a yes or no, a number, a '
        'correction of it) and changed or added nothing to `request`.',
        'changed': 'Somewhere in `answer` the person changed `request` or asked for something more: another '
        'day, time or item, a question, a complaint.',
        'unclear': 'It is uncertain.',
    },
    default='unclear',
)
"""Whether a detour's request still stands, so a return from the check may act on it without a model run."""


class Choice(BaseModel):
    """The one option the answer picks, or none."""

    model_config = ConfigDict(extra='forbid', frozen=True)
    choice: str = Field(
        default='',
        description='The `id` of the one option in `options` the answer picks or asks for, copied exactly. It '
        'still counts when the answer also says thanks or goodbye. Empty if the answer picks none of them, puts '
        'a condition on its pick, or could mean more than one.',
    )


class NamedChoice(Choice):
    """The one option the answer picks, and the one it names without simply picking it."""

    named: str = Field(
        default='',
        description='The `id` of the one option in `options` the answer names without simply picking it: it puts '
        'a condition on it, asks about it, or doubts it. Empty if the answer names none, or picks it plainly.',
    )


class Reading[C: CallContext, InputT: Exchange](ClassifierInterpreter[C, InputT, BaseModel]):
    """One act's reading: when it applies, what it reads, and its classes.

    `snapshot` returns the input, or None when the act cannot happen on this
    call right now. A reading is valid only while the snapshot is unchanged,
    so a reading of one question never answers the next. With `rereadable`,
    a snapshot that changed only in its facts (not in the words) is read
    again within the turn: a diary read that puts new options on the table.
    """

    def __init__(
        self,
        name: str,
        classifier: Classifier[InputT, Any],
        snapshot: Callable[[C], InputT | None],
        field: str,
        rereadable: bool = False,
        settle: Callable[[C, str], None] | None = None,
    ) -> None:
        super().__init__(name, classifier, classifier.output_type())
        self.snapshot = snapshot
        self.field = field
        """The output field the label is read from."""
        self.rereadable = rereadable
        self.settle = settle
        """What the application records from a fresh label, once, on the turn it is read. A failure settles as the default."""

    def applies(self, call: C) -> bool:
        return bool(call.heard.strip()) and self.snapshot(call) is not None

    def label(self, call: C) -> str:
        """This turn's label, or the default when the reading did not run, failed or went stale."""
        return getattr(self.current(call), self.field)

    def ran(self, call: C) -> bool:
        """Whether this reading was asked this turn about the call as it is now, so its label is an answer.

        Its default and "it did not run" read the same through `label`; a
        guard that acts on a default has to know which one it has.
        """
        cached = call._interpretations.get(self.name)
        return (
            cached is not None
            and cached.turn == call.turn
            and self._still_valid(call, cast(InputT, cached.input))
        )

    def _input(self, call: C) -> InputT:
        value = self.snapshot(call)
        if value is None:
            raise ValueError(f'{self.name} does not apply to this call')
        return value

    def _still_valid(self, call: C, value: InputT) -> bool:
        return self.snapshot(call) == value

    def _settle(self, call: C, value: InputT, result: BaseModel) -> BaseModel:
        if self.settle is not None:
            self.settle(call, getattr(result, self.field))
        return result

    def _rereadable(self, call: C, value: InputT) -> bool:
        now = self.snapshot(call)
        return (
            self.rereadable
            and now is not None
            and (now.question, now.answer) == (value.question, value.answer)
        )


class ChoiceReading[C: CallContext, InputT: ChoiceInput](Reading[C, InputT]):
    """A reading that picks one of the options its snapshot gives, by id.

    The model names an id; the application decides whether it counts. Only
    an id among the options this reading was sent is a pick, so no reading
    of a reply can choose something that was never offered.
    """

    def picked(self, call: C) -> str:
        """The option this turn's reply picks, or '' when it picks none, names one not offered, or did not run."""
        return self._offered(call, self.label(call))

    @property
    def names(self) -> bool:
        """Whether this reading also says which option was named without being picked (`NamedChoice`)."""
        return 'named' in self.classifier.output_type.model_fields

    def named(self, call: C) -> str:
        """The option this turn's reply names with a condition, a question or a doubt, or ''. It authorises nothing."""
        return self._offered(call, getattr(self.current(call), 'named', ''))

    def _offered(self, call: C, option: str) -> str:
        options = self.snapshot(call)
        return option if option and options is not None and option in {o.id for o in options.options} else ''


def _title(name: str) -> str:
    return name.title().replace('_', '')


def classify[C: CallContext, InputT: Exchange](
    name: str,
    classes: ClassSet,
    input_type: type[InputT],
    snapshot: Callable[[C], InputT | None],
    *,
    context: str,
    model: Model | str,
    settings: ModelSettings | None = None,
    settle: Callable[[C, str], None] | None = None,
) -> Reading[C, InputT]:
    """A reading with fixed classes: its output is one field, named after the reading."""
    output = output_model(f'{_title(name)}Reading', {name: classes})
    classifier = Classifier(model, input_type, output, context=context, model_settings=settings)
    return Reading(name, classifier, snapshot, field=name, settle=settle)


def choose[C: CallContext, InputT: ChoiceInput](
    name: str,
    input_type: type[InputT],
    snapshot: Callable[[C], InputT | None],
    *,
    context: str,
    model: Model | str,
    settings: ModelSettings | None = None,
    named: bool = False,
) -> ChoiceReading[C, InputT]:
    """A reading that picks one of the options the snapshot gives, by id. `ChoiceReading.picked` checks the id.

    Each gets its own output model, named after the reading, so two choices
    in one call are two schemas and a trace says which one answered. With
    `named`, the reading also says which option was named but not simply
    picked (`NamedChoice`), under the same model name: a prompt change,
    measured on the reading's dataset before it is turned on.
    """
    output = create_model(f'{_title(name)}Choice', __base__=NamedChoice if named else Choice)
    classifier = Classifier(model, input_type, output, context=context, model_settings=settings)
    return ChoiceReading(name, classifier, snapshot, field='choice', rereadable=True)


async def read(
    call: CallContext,
    readings: Sequence[Reading[Any, Any]],
    *,
    usage: RunUsage | None = None,
    usage_limits: UsageLimits | None = None,
) -> None:
    """Run every reading that applies to this turn, side by side. Each runs at most once per turn."""
    live = [r for r in readings if r.applies(call)]
    await asyncio.gather(*(r.interpret(call, usage=usage, usage_limits=usage_limits) for r in live))


type Rule[C] = Callable[[C], LocalAction | None]
"""What the application does on one act, from the readings: an action, or None to let the next rule try."""


@dataclass(frozen=True)
class Acts[C: CallContext]:
    """What a business does on each act, tried in this order, the same for every business.

    1. `answer`: the question the call is waiting on is answered. It comes
       first because it is what the person was asked: "Yes, cancel it" to a
       cancel question is the answer to it, not a new request to cancel.
    2. `take_back`: something they agreed to and can still undo is taken back.
       Before a pick, so "leave it, thanks" is never lost to a goodbye.
    3. `pick`: one of the options on the table is picked, with or without a
       question pending.
    4. `goodbye`: the call is over. Last, so "Yes, go ahead, bye" does the
       yes before it hangs up.

    An answer and a goodbye in the same reply are both done: the answer
    first, then the goodbye, as one action. "Yes, that's perfect. I do not
    need anything else. Goodbye." (Coval run 4bLgLR4t, case 09) used to book
    and then ask "Anything else?". While the answer runs the call is marked
    as leaving (`CallContext.leaving`), so its confirmation asks nothing more.
    A refused answer is not followed by the goodbye.
    """

    answer: Sequence[Rule[C]] = ()
    take_back: Sequence[Rule[C]] = ()
    pick: Sequence[Rule[C]] = ()
    goodbye: Sequence[Rule[C]] = ()

    def decide(self, call: C) -> LocalAction | None:
        """The first action any rule takes, in the fixed order, or None to let the step's model answer."""
        for rule in self.answer:
            if (action := rule(call)) is not None:
                bye = next((b for r in self.goodbye if (b := r(call)) is not None), None)
                return action if bye is None else _then_goodbye(call, action, bye)
        for rule in (*self.take_back, *self.pick, *self.goodbye):
            if (action := rule(call)) is not None:
                return action
        return None


def _then_goodbye(call: CallContext, answer: LocalAction, goodbye: LocalAction) -> LocalAction:
    """The answer, said as a caller who is leaving hears it, then the goodbye."""

    async def both() -> object:
        call._leaving = True
        done = await answer.run()
        await goodbye.run()
        return done

    return LocalAction(answer.name, both)
