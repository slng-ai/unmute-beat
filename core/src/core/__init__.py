"""slng-voice-core: a conversation runtime for voice agents, on Pydantic AI and Pydantic Graph.

The public boundary, in the order an application uses it:

    import core

    spec = core.load('clinic/flow.yaml')        # read and check the flow: no model, no key, no network
    flow = spec.compile(model=model)             # build its agents: the one place a provider is resolved
    call = flow.open(ClinicState(), services=backend, clock=clock)   # one session per call
    reply = await core.text_turn(flow, call, 'I need an appointment')
    call.acknowledge(*(s.id for s in reply.segments))   # the reader got it: only now can it be agreed to

The application supplies a plain Pydantic model for its state, its services,
prompts and operations. Core keeps the session: where the caller is, the
turn and its generation, history, what was said and whether it was heard,
the question waiting for an answer, the budget, and the evidence of every
receipt. Importing this module loads no `.env`, builds no client and imports
no Pipecat or Langfuse.

Voice is `core.processor.PydanticAgentProcessor` (the `voice` extra), and
tracing is `core.observability.setup` (the `tracing` extra).

ponytail: salon-graph, Faro, faro-tools and the salon evaluators still
import core's modules directly and subclass `CallContext`; those deep
imports stay until their migration sessions (4, 5 and 6).
"""

from core.flowspec import FlowSpec, FlowSpecError
from core.flowspec import load_file as load
from core.operations import Operation, OutcomeUnknownError, Refused
from core.policies import TurnBudget
from core.proposals import BaseProposal
from core.state import Intent
from core.steps import ConfirmedOperation, FlowCall, StepFlow
from core.turn import TextReply, greet, text_turn

__all__ = [
    'BaseProposal',
    'ConfirmedOperation',
    'FlowCall',
    'FlowSpec',
    'FlowSpecError',
    'Intent',
    'Operation',
    'OutcomeUnknownError',
    'Refused',
    'StepFlow',
    'TextReply',
    'TurnBudget',
    'greet',
    'load',
    'text_turn',
]
