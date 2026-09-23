"""Turning a caller's words into typed data, at most once per turn.

`Interpreter` owns what every interpretation must do whatever does the
interpreting: ask at most once per accepted turn, spend the shared budget,
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
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.usage import RunUsage, UsageLimits

from core.classifiers import Classifier
from core.state import CallContext, Interpretation, Request

FAILURES = (ValidationError, UnexpectedModelBehavior, ModelHTTPError, HTTPError)
"""What becomes the safe default instead of an error. Cancellation and budget exhaustion are never in here."""


class Interpreter[C: CallContext, InputT: BaseModel, OutputT: BaseModel](ABC):
    """One way of reading the caller's reply, cached on the call for the turn.

    Shared config, per-call state: an instance holds only its name and its
    default, and everything that changes lives on the call, so one instance
    serves every concurrent call.
    """

    def __init__(self, name: str, fallback: OutputT) -> None:
        self.name = name
        self.fallback = fallback
        """What an interpretation that failed, went stale or never ran reads as. It must authorise nothing."""

    @final
    async def interpret(
        self, call: C, *, usage: RunUsage | None = None, usage_limits: UsageLimits | None = None
    ) -> OutputT:
        """This turn's interpretation, requested now if it has not been yet.

        Failures are cached too, so a tool retry cannot turn uncertainty into
        repeated guesses. The budget is checked before anything changes, so an
        exhausted turn leaves no trace of having tried.
        """
        cached = call._interpretations.get(self.name)
        if (cached is not None and cached.turn == call.turn) or not call.heard.strip():
            return self.current(call)
        value = self._input(call)
        turn, generation = call.turn, call._generation
        if usage is not None and usage_limits is not None:
            usage_limits.check_before_request(usage)
        call._interpretations[self.name] = Interpretation(turn, value, self.fallback)
        if call.attempt == 0:
            call.retain_input()
        request = Request(started=time.perf_counter(), kind='classifier')
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

    def _settle(self, call: C, value: InputT, result: OutputT) -> OutputT:
        """Adjust or record a fresh result before it is cached. Only runs for the current turn."""
        return result


class ClassifierInterpreter[C: CallContext, InputT: BaseModel, OutputT: BaseModel](
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
