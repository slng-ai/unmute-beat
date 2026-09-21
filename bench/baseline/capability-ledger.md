# SDK capability ledger

What the installed SDKs actually do, at the invocation paths this harness
uses. Pinned versions: **pydantic-ai 2.46.0**, **pydantic 2.13.5**,
**pydantic-extra-types 2.11.1**, **phonenumbers 9.0.39**, **pipecat-ai
1.11.0**, Python 3.14.5. Exact resolutions in `uv.lock`; its digest is in
`manifest.json`.

The **How** column is the point of this file. A native annotation, a
generated schema and a graph-level check are three different guarantees, and
a documentation page is a fourth thing again. Anything marked *doc only* has
not been run here and should not be relied on without running it.

## State writes

| Behaviour | Finding | How |
|---|---|---|
| `model_copy(update=...)` | Copies updates in **without validating them**. `state.model_copy(update={'customer_verified': 'nonsense'})` returns a model holding `'nonsense'`. | probe, then pinned in `tests/test_state.py::a_commit_validates_rather_than_copies` |
| Default validation | Off unless `validate_default=True`. An authored default that breaks its own type sits there until something reads it. | test |
| Assignment validation | Off unless `validate_assignment=True`, and separate from the above. | test |
| Nested model handed in ready-made | Not revalidated unless `revalidate_instances='always'`. A `model_construct`ed instance carries illegal values straight through. | test |
| Nested *assignment* | The parent's `validate_assignment` does **not** cover a write to a field of a model it holds. The child needs its own config. | test |
| Assignment inside `model_validator(mode='after')` | **Recurses.** Under `validate_assignment=True` an assignment re-runs the model validators, so a self-correcting validator must write through `__dict__`. | probe (`RecursionError` on construction) |

## Ordinary versus schema-defined arguments

| Behaviour | Finding | How |
|---|---|---|
| Ordinary typed callables | pydantic-ai derives the argument validator from the annotations. Every tool in `salon/agents.py` is one of these, so there is nothing to opt into. | test (`no_tool_is_defined_from_a_raw_schema`) |
| `Tool.from_schema` | Reported to skip automatic schema validation; an explicit `args_validator` still runs. **Not probed here**, because this harness defines no such tool — the test above is what keeps that true. | *doc only* (phase note, `tools.py#L481`) |
| Injected state values | Not an SDK feature. Tools read `customer_phone` off the state and no tool but `confirm_customer` exposes a phone parameter, so there is nothing for the model to override. | test (`no_tool_but_the_verifier_takes_a_phone_number`) |
| `deps_type` | Static typing only. It does not validate the dependency object at runtime. | *doc only* |

## Service results

| Behaviour | Finding | How |
|---|---|---|
| Return annotations | Not enforced. Python does not check them, so a service returning the wrong shape reaches the state unless something validates it. | test (`a_malformed_service_response_writes_nothing`) |
| Validation at the boundary | Every `salon.diary` response is validated into a model in `salon/agents.py` before a field is read. | test |

## Task and flow completion

| Behaviour | Finding | How |
|---|---|---|
| `FunctionToolResultEvent` | Carries `.part`, **not** `.result`. A refusal arrives as a `RetryPromptPart` in that same field. | probe (AttributeError, then fixed) |
| `FunctionToolCallEvent` | Fires when the model *asks* for a tool — before it runs, and regardless of what it then does. Not evidence of success. | probe |
| Output tool naming | A bare function in `output_type` is renamed `final_result_<name>` as soon as a step has two outputs. An explicit `ToolOutput(fn, name=...)` keeps the name. | found the hard way: the narrowing hook matched by name and silently stopped narrowing |
| `end_strategy` | `'graceful'` is the v2 default: function tools emitted before an output tool still run, and text before a tool does not end the run. | test (`text_before_a_tool_streams_and_the_tool_still_runs`) |

## Transition checks

| Behaviour | Finding | How |
|---|---|---|
| `prepare_tools` / `prepare_output_tools` | **Two distinct hooks.** Filtering ordinary tools does nothing to the handoff and finisher alternatives. | probe, then test |
| Refresh frequency | Both re-run before *every* model request, not once per run: `ModelRequestNode._prepare_request` calls `ToolManager.for_run_step` (`_agent_graph.py:1600`). | probe, then `tests/test_flow.py::the_schema_is_rebuilt_between_requests_in_one_run` |
| `after_model_request` | Receives the complete `ModelResponse` **before** any tool executes, so the whole proposed call set can be preflighted without controlled iteration. | probe |
| `before_tool_execute` | **Does not fire for output tools.** A `ToolOutput` ran with the hook never called. This is why output tools call `claim_or_refuse` themselves. Recheck on any upgrade. | probe |
| `sequential=True` | Available on both `Tool` and `ToolOutput`. | signature inspection |
| `parallel_tool_calls=False` | Not relied on, and not probed. The conflict policy does not assume it. | not run |

## Request and history controls

| Behaviour | Finding | How |
|---|---|---|
| Shared usage | A single `RunUsage` passed as `usage=` accumulates across separate `run`/`run_stream_events` calls, so one turn's allowance spans four agents. | test (`model_requests_are_counted_across_agents`) |
| `UsageLimits` | `request_limit` **defaults to 50** and `tool_calls_limit` to `None`. Neither default is this project's contract; both are set explicitly. | signature inspection + test |
| Limit breach | Raises `UsageLimitExceeded` out of the run. It must be re-raised past the generic handler or it becomes one more apologising agent run. | probe, then test |
| `retries=` | Semantic repair only. Exhaustion raises `UnexpectedModelBehavior('Exceeded maximum output retries')`. Provider HTTP retries are a separate layer with their own counter. | probe, then test |
| `cancellation_token=` | A first-class parameter on `run`/`iter`/`run_stream_events`. **Not used here** — barge-in cancels the event stream directly. Worth revisiting in Phase 02. | signature inspection |
| Speculative execution | None exists in this harness to disable. Recorded because a LiveKit runtime offers it and it must stay off until measured. | inspection |

## Pipecat

| Behaviour | Finding | How |
|---|---|---|
| Frame contract | `LLMFullResponseStartFrame` / `LLMTextFrame` / `LLMFullResponseEndFrame` is what TTS, the aggregators and interruption are all built on. | test (`the_processor_still_speaks_pipecat`) |
| Unbalanced frames | The core can unwind through an exception without emitting its end event, so pairing is the adapter's job. | found by inspection after extraction; now `tests/test_core.py::every_response_frame_is_closed` |
| STT/TTS in Langfuse | Exported as `GENERATION` observations. They are not model requests. | `tests/test_traces.py` asserts the mapping; `bench/turns.py` excludes them by name and structurally |
