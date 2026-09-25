"""Turning a caller's words into typed data, at most once per turn.

`Interpreter` owns what every interpretation must do whatever does the
interpreting: ask at most once per accepted turn (unless the application
itself widened the question, see `_rereadable`), spend the shared budget,
retain the caller's input before the request can be cancelled, time and trace
the request, turn a failure into the safe default, and never publish an answer
to a turn the caller has moved on from. A subclass supplies only the input it
sends and how it decides.

The public methods are `@final`: a business subclass cannot skip the checks,
only fill in the hooks.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import cast, final

from httpx import HTTPError
from loguru import logger
from pydantic import BaseModel, ValidationError
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier, Exchange
from core.state import CallContext, Interpretation, Request

try:
    from httpx2 import HTTPError as HTTPError2
except ImportError:
    HTTPError2 = HTTPError

FAILURES = (ValidationError, UnexpectedModelBehavior, ModelAPIError, HTTPError, HTTPError2)
"""What becomes the safe default instead of an error. Cancellation and budget exhaustion are never in here.

`ModelAPIError` covers every provider error Pydantic AI raises, `ModelHTTPError`
included. The two `HTTPError`s are what a client raises before any answer
comes back: the Google client sends through `httpx2`, Pydantic's fork of
httpx, whose errors are not `httpx` errors. Without it, a DNS failure on one
reading (trace fbdd81b6, 2026-09-25) ended the whole turn in silence.
"""


class Interpreter[C: CallContext, InputT: BaseModel, OutputT: BaseModel](ABC):
    """One way of reading the caller's reply, cached on the call for the turn.

    Shared config, per-call state: an instance holds only its name and its
    default, and everything that changes lives on the call, so one instance
    serves every concurrent call.
    """

    requests_model: bool = True
    """Whether deciding costs a provider request, so the budget is checked and the request traced."""

    def __init__(self, name: str, fallback: OutputT) -> None:
        self.name = name
        self.fallback = fallback
        """What an interpretation that failed, went stale or never ran reads as. It must authorise nothing."""

    @final
    async def interpret(
        self, call: C, *, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
    ) -> OutputT:
        """This turn's interpretation, requested now if it has not been yet.

        At most once per turn, with one exception. Failures are cached too, so
        a tool retry cannot turn uncertainty into repeated guesses, and a
        reading gone stale because the caller's identity, the question or the
        words changed is never asked again in that turn: it reads as the safe
        default. The exception is `_rereadable`: the only change since is
        application-owned context that widens the question (a diary read put
        new times on the table), which gets a new reading within the turn's
        budget. The budget is checked before anything changes, so an exhausted
        turn leaves no trace of having tried.
        """
        cached = call._interpretations.get(self.name)
        if not call.heard.strip() or (
            cached is not None
            and cached.turn == call.turn
            and (
                self._still_valid(call, cast(InputT, cached.input))
                or not self._rereadable(call, cast(InputT, cached.input))
            )
        ):
            return self.current(call)
        value = self._input(call)
        turn, generation = call.turn, call._generation
        if self.requests_model and usage is not None and usage_limits is not None:
            usage_limits.check_before_request(usage)
        call._interpretations[self.name] = Interpretation(turn, value, self.fallback)
        if call.attempt == 0:
            call.retain_input()
        request = Request(started=time.perf_counter(), kind='classifier')
        if self.requests_model:
            call.requests.append(request)
        try:
            result = await self._decide(value, usage=usage, usage_limits=usage_limits)
        except FAILURES as error:
            logger.warning('{} interpretation failed: {}', self.name, type(error).__name__)
            result = self.fallback
            request.fallback = True
        finally:
            request.duration = time.perf_counter() - request.started
        request.labels = result.model_dump_json()
        if call.turn != turn or not call.is_current(generation):
            return self.fallback
        settled = self._settle(call, value, result)
        call._interpretations[self.name] = Interpretation(turn, value, settled)
        return self.current(call)

    @final
    def current(self, call: C) -> OutputT:
        """This turn's interpretation, only while it is still about the call as it is now."""
        cached = call._interpretations.get(self.name)
        if (
            cached is None
            or cached.turn != call.turn
            or not self._still_valid(call, cast(InputT, cached.input))
        ):
            return self.fallback
        return cast(OutputT, cached.output)

    @abstractmethod
    def _input(self, call: C) -> InputT:
        """The snapshot to interpret: the caller's words and the application-owned context they answer."""

    @abstractmethod
    async def _decide(
        self, value: InputT, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> OutputT:
        """Interpret one snapshot. Raise one of `FAILURES` rather than guess."""

    def _still_valid(self, call: C, value: InputT) -> bool:
        """Whether an interpretation of `value` still describes this call. Override to compare what matters."""
        return True

    def _rereadable(self, call: C, value: InputT) -> bool:
        """Whether a stale reading of `value` may be asked again this turn. Never, unless a subclass says why."""
        return False

    def _settle(self, call: C, value: InputT, result: OutputT) -> OutputT:
        """Adjust or record a fresh result before it is cached. Only runs for the current turn."""
        return result


class ClassifierInterpreter[C: CallContext, InputT: Exchange, OutputT: BaseModel](
    Interpreter[C, InputT, OutputT]
):
    """An interpreter that decides with one structured model request and no repair loop."""

    def __init__(self, name: str, classifier: Classifier[InputT, OutputT], fallback: OutputT) -> None:
        super().__init__(name, fallback)
        self.classifier = classifier

    async def _decide(
        self, value: InputT, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> OutputT:
        return await self.classifier.classify(value, usage=usage, usage_limits=usage_limits)


class RuleInterpreter[C: CallContext, InputT: BaseModel, OutputT: BaseModel](Interpreter[C, InputT, OutputT]):
    """An interpreter that decides in code, with no model: keypad input, buttons, or a baseline to beat.

    It still gets everything `Interpreter.interpret` guarantees except a
    request: no budget is spent and nothing is traced as a model request.
    """

    requests_model = False

    async def _decide(
        self, value: InputT, *, usage: RunUsage | None, usage_limits: UsageLimits | None
    ) -> OutputT:
        return self._rule(value)

    @abstractmethod
    def _rule(self, value: InputT) -> OutputT:
        """Decide from the snapshot alone. Return the fallback rather than guess."""
