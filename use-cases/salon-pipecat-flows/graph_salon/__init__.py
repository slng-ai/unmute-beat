"""The salon on declarative Pipecat flows, with core's guarantees written as application code.

`bot.py` beside this package puts it where core's processor stands in
the salon's `bot.py`. The pipeline around it is the same: STT, VAD,
smart-turn, TTS, the transports and the salon's diary. The conversation is
Pipecat's own: a `FlowConfig` (`flow.yaml`)
joined to direct functions (`handlers.py`) and run by `FlowManager`. Every
rule core enforces (consent, one action per reply, the turn budget,
validated receipts, speech from receipts) is plain code in this package.
`README.md` maps each rule to where it lives.
"""

from typing import Any

from pipecat.flows import FlowManager
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.llm_service import LLMService

from graph_salon.call import SalonCall
from graph_salon.flow import FLOW, seeded
from graph_salon.speech import GREETING, ClaimFilter, EmptyReplyGuard
from salon.diary import Diary


class GraphSalon:
    """One call: its state, the LLM the flow runs on, and the two processors between the LLM and TTS.

    Args:
        diary: The salon.
        call_id: The call's id at the diary.
        llm: The LLM service the flow runs on. It carries no system instruction: the first node sets it.
        caller: The carrier's caller ID, read back before it is trusted.
    """

    def __init__(self, diary: Diary, call_id: str, llm: LLMService[Any], caller: str = '') -> None:
        self.call = SalonCall(diary, call_id, caller)
        self.llm = llm
        self.context = LLMContext()
        self.empty_guard = EmptyReplyGuard()
        self.claim_filter = ClaimFilter(self.call)
        self._manager: FlowManager | None = None

    @property
    def processors(self) -> list:
        """What goes between the LLM and TTS, in order: the filter first, so a reply it empties is an empty reply."""
        return [self.claim_filter, self.empty_guard]

    def attach(self, worker: PipelineWorker, aggregators: LLMContextAggregatorPair) -> None:
        """Give the flow its worker and aggregators, and seed the state its placeholders read."""
        self._manager = FlowManager(llm=self.llm, context_aggregator=aggregators, worker=worker)
        self._manager.state.update(seeded(self.call))

        async def rerun() -> None:
            await worker.queue_frames([LLMRunFrame()])

        self.empty_guard.rerun = rerun

    async def start(self) -> None:
        """Enter the first node. It speaks the greeting (`{{ say }}`) and waits for the caller."""
        if self._manager is None:
            raise RuntimeError('attach the worker before starting the flow')
        await self._manager.initialize(FLOW.initial_node)


__all__ = ['GREETING', 'GraphSalon']
