# Offline suites, parent and branch

Each member: `uv run python -m tests`, from its folder, `ALLOW_MODEL_REQUESTS` off.

## Parent (origin/main fc2cfa4)

- `concierge-tools`: all 5 modules ok
  - ledger ok
  - audio timing ok
  - matrix ok: rows, missing results, native gaps, pin, tolerance
  - all 5 modules ok
- `core`: all 16 modules ok
  - classes ok: ledger, default operation, rule interpreter, final methods held
  - reuse ok: a second use case on the same core classes, with no salon code
  - clock ok: labels, calendar, one reading per call
  - package ok: importing and checking loads no env, no voice, no tracing, no provider
  - delivery ok: no record, queued, unacknowledged, partial, estimated and late copies never authorise
  - public ok: load, compile, open, text turns, acknowledgement, one guarded booking
  - session ok: atomic state, owned fields, isolation, freshness, stale work, evidence
  - sdk ok: whole candidates, owned fields, typed prompts, AgentSpec on the graph, state-derived schemas
  - authoring ok: manifest, operations, choices, take-back, three patterns, early diagnostics
  - nodes ok: no reader, hostile calls, mixed reply, holding, code node, late options, unheard receipt, no echo, code read-back, a question is not a no
  - intents ok: I1-I15
  - tickets ok: R1-R23, no phone, date, appointment or slot
  - outcomes ok: O1-O9
  - speech ok: S1-S8, a rendered step says no model words; a prose claim nothing backs is corrected
  - webrtc ok: W1-W10 on the real SmallWebRTC transport, an aiortc browser and the desk flow
  - all 16 modules ok
- `faro-language-learning`: all 1 modules ok
  - faro ok: identity, consent, onboarding, lessons, resume and restart, on a real SQLite file
  - all 1 modules ok
- `salon-graph`: all 21 modules ok
  - graph ok: legal moves, interposition, refused moves, tools per node, pinned requests
  - state ok: whole writes, owned identity, corrections, phone and time types, isolation
  - walkthrough ok: detour kept the time, same-node tool result, waiting, bare yes, read-back, one booking, handoff
  - business ok: A-L on the diary, identity, dates, claims
  - moves ok: V1-V13, refusals bound to their request, picks kept across a day change, no yes by repetition
  - requests ok: K1-K8, the same words and diary with fewer booking requests
  - continuation ok: joined pieces, unheard speech, spent answers, cut read-backs, unheard receipt, mixed yes
  - overlap ok
  - marks ok: M1-M8 on the real adapter, transport and Twilio serializer
  - browser ok: BR1-BR5, the documented entry point serves Pipecat's client with playback reports, refuses one without, and confirms by the page's reports only
  - recovery ok: bounded read-backs, fresh yes, one budget per joined turn, cut copies said again
  - ceiling ok: owned, stopped on every ending, one per call
  - audit export ok: E1 complete evidence passes; E2 missing or corrupted binding and delivery do not
  - store ok: B1-B9, several processes on one SQLite file, judged from the file
  - runner ok: A1-A8, bounded admission, one claim, nothing held by the dead, live calls counted
  - all 21 modules ok
- `salon-pipecat-flows`: all 1 modules ok
  - graph salon ok
  - all 1 modules ok
- `salon-prompt`: all 1 modules ok
  - all 1 modules ok

## This branch (final state)

- `concierge-tools`: all 5 modules ok
  - ledger ok
  - audio timing ok
  - matrix ok: rows, missing results, native gaps, pin, tolerance
  - all 5 modules ok
- `core`: all 17 modules ok
  - classes ok: ledger, default operation, a yes bound to its question, final methods held
  - engine ok: E1-E7, stalls, holding lines, late reports, the date and errors, on the public clinic
  - clock ok: labels, calendar, one reading per call
  - package ok: importing and checking loads no env, no voice, no tracing, no provider
  - delivery ok: no record, queued, unacknowledged, partial, estimated and late copies never authorise
  - public ok: load, compile, open, text turns, acknowledgement, one guarded booking
  - session ok: atomic state, owned fields, isolation, freshness, stale work, evidence
  - sdk ok: whole candidates, owned fields, typed prompts, AgentSpec on the graph, state-derived schemas
  - authoring ok: manifest, operations, choices, take-back, three patterns, early diagnostics
  - nodes ok: no reader, hostile calls, mixed reply, holding, code node, late options, unheard receipt, no echo, code read-back, a question is not a no
  - intents ok: I1-I15
  - tickets ok: R1-R23, no phone, date, appointment or slot
  - flow rules ok: F1-F7, language per call, carried and routed operations, greetings, heard
  - outcomes ok: O1-O9
  - speech ok: S1-S8, a rendered step says no model words; a prose claim nothing backs is corrected
  - webrtc ok: W1-W10 on the real SmallWebRTC transport, an aiortc browser and the desk flow
  - all 17 modules ok
- `faro-language-learning`: all 1 modules ok
  - faro ok: L01-L16 and F1-F9 on the public path, a real SQLite file, no reader imported
  - all 1 modules ok
- `salon-graph`: all 21 modules ok
  - graph ok: legal moves, interposition, refused moves, tools per node, pinned requests
  - state ok: whole writes, owned identity, corrections, phone and time types, isolation
  - walkthrough ok: detour kept the time, same-node tool result, waiting, bare yes, read-back, one booking, handoff
  - business ok: A-L on the diary, identity, dates, claims
  - moves ok: V1-V13, refusals bound to their request, picks kept across a day change, no yes by repetition
  - requests ok: K1-K8, the same words and diary with fewer booking requests
  - continuation ok: joined pieces, unheard speech, spent answers, cut read-backs, unheard receipt, mixed yes
  - overlap ok
  - marks ok: M1-M8 on the real adapter, transport and Twilio serializer
  - browser ok: BR1-BR5, the documented entry point serves Pipecat's client with playback reports, refuses one without, and confirms by the page's reports only
  - recovery ok: bounded read-backs, fresh yes, one budget per joined turn, cut copies said again
  - ceiling ok: owned, stopped on every ending, one per call
  - audit export ok: E1 complete evidence passes; E2 missing or corrupted binding and delivery do not
  - store ok: B1-B9, several processes on one SQLite file, judged from the file
  - runner ok: A1-A8, bounded admission, one claim, nothing held by the dead, live calls counted
  - all 21 modules ok
- `salon-pipecat-flows`: all 1 modules ok
  - graph salon ok
  - all 1 modules ok
- `salon-prompt`: all 1 modules ok
  - all 1 modules ok

The branch was also run after the last commit (`tests.test_bot` added to Faro): `all 2 modules ok`.
