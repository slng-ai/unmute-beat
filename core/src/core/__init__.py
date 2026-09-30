"""slng-voice-core: a conversation runtime for voice agents, on Pydantic AI and Pydantic Graph.

The public boundary, in the order an application uses it:

    import core

    spec = core.load('clinic/flow.yaml')        # read and check the flow: no model, no key, no network
    flow = spec.compile(model=model)             # build its agents: the one place a provider is resolved
    call = flow.open(ClinicState(), services=backend, clock=clock)   # one session per call
    reply = await core.text_turn(flow, call, 'I need an appointment')
    call.acknowledge(*(s.id for s in reply.segments))   # the reader got it: only now can it be agreed to

The application supplies a plain Pydantic model for its state, its services,
prompts and operations: typed functions, or classes (`core.Lookup`,
`core.Write`) whose receipts are `core.Receipt` models. Core keeps the session: where the caller is, the
turn and its generation, history, what was said and whether it was heard,
the question waiting for an answer, the budget, and the evidence of every
receipt. Importing this module loads no `.env`, builds no client and imports
no Pipecat or Langfuse.

Voice is `core.processor.PydanticAgentProcessor` (the `voice` extra), and
tracing is `core.observability.setup` (the `tracing` extra). Launchers and
tests still import those adapters, and a few engine helpers, by their module.
"""

from core.actions import Lookup, Write
from core.flowspec import FlowSpec, FlowSpecError
from core.flowspec import load_file as load
from core.operations import Operation, OutcomeUnknownError, Refused
from core.policies import TurnBudget
from core.proposals import BaseProposal
from core.state import Intent
from core.steps import ConfirmedOperation, FlowCall, StepFlow
from core.turn import TextReply, greet, text_turn
from core.values import NotPast, Phone, Quoted, Receipt, Rejection, Subject, Version

__all__ = [
    'BaseProposal',
    'ConfirmedOperation',
    'FlowCall',
    'FlowSpec',
    'FlowSpecError',
    'Intent',
    'Lookup',
    'NotPast',
    'Operation',
    'OutcomeUnknownError',
    'Phone',
    'Quoted',
    'Receipt',
    'Refused',
    'Rejection',
    'StepFlow',
    'Subject',
    'TextReply',
    'TurnBudget',
    'Version',
    'Write',
    'greet',
    'load',
    'text_turn',
]
