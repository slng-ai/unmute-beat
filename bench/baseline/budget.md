# Turn budget

**Proposals to tune and record, not values anybody has shown to be right.**
None of these is an SDK default and none is a production number. They are
written down so that changing one is a change to a file with a reason beside
it, rather than a different constant in three places.

Defined in `voice.harness.TurnBudget`. One object per accepted caller turn,
threaded through every agent run in that turn.

| Setting | Value | Mechanism | Why this number |
|---|---|---|---|
| `step_runs` | 4 | the hop loop | The longest legitimate chain observed is verify → booking → answer. Four leaves one spare and still stops a ping-pong inside about three round trips. Was 3. |
| `model_requests` | 8 | shared `RunUsage` + `UsageLimits(request_limit=...)` | A step run is not a request: one run that calls two tools and then answers is three. Eight allows four runs averaging two requests each. |
| `tool_calls` | 8 | `UsageLimits(tool_calls_limit=...)` | Its own axis, because a single run can loop on tools without ever hopping, and the hop budget cannot see that. |
| `seconds` | 20.0 | `asyncio.timeout` around the whole turn | An upper bound on silence, not a target. A caller has usually gone by then; this is the point at which saying something is better than continuing. |
| `repair_retries` | 2 | `retries=` on each run | Two chances to correct a refused tool call. One is not enough for a phone number read back wrong; more than two is a model that is not going to get there. |

## What is counted separately

**Semantic repair** is a tool refusing and the model correcting itself
(`ModelRetry` → `RetryPromptPart`). That is `repair_retries`.

**Provider SDK retries and HTTP attempts** are a different layer with their
own counter, inside the model client. They are deliberately not added to
`repair_retries`: folded together, a flaky network would buy extra attempts at
getting a tool call right, and a model that cannot get a tool call right would
look like a network problem.

**Step runs are not model requests.** One agent run that calls a tool and then
answers is one step run and two requests. The two budgets exist because
neither bounds the other.

## Exhaustion

A spent budget yields one fixed line, `voice.processor.EXHAUSTED`, spoken by
the processor. No further provider request, no tool, no state change, and no
refill.

The previous behaviour was one more model run after the hop budget was
declared spent, narrowed to `str`. It bounded nothing: the extra run was
outside the budget, could take as long as it liked, and still held every
ordinary tool, so an exhausted turn could book an appointment on its way out.
`tests/test_voice.py::ping_pong` and `tests/test_salon.py::one_kind_b_per_turn`
expected that fourth run and now expect the fixed line instead. That is a
deliberate contract change, not a regression.

## Speculative execution

Off, and there is none to turn off: the processor makes no preemptive or
speculative model request, and no outer adapter replays a turn. Recorded here
because the contract asks for it explicitly, and because a future runtime
(LiveKit preemptive generation) offers it and it must stay off until it is
measured.

## Evidence

`tests/test_budget.py` — nine checks: exhaustion makes no further request, the
fallback does no business work, a new turn gets a fresh allowance, requests
are counted across agents, the request limit stops a turn that never hops, the
tool limit is its own number, a slow tool hits the turn deadline, a permitted
repair succeeds, and repair retries are bounded.
