# Current flow: verify before booking or complaints

This implements the user's final sequence on 2026-09-21 and supersedes the earlier
complaint-readback-before-verification design.

- General salon questions remain with concierge without identification.
- Booking creation, changes and cancellations, and complaint requests, require
  `customer_verified` before the destination agent starts through the graph.
- The existing prerequisite mechanism routes to `verify`, retains the original
  request, and resumes `booking` or `complaints` after successful phone verification.
- Already verified callers enter directly. Declining verification abandons the
  pending service request and returns to concierge, where general questions remain available.
- Complaint approval is still required before recording. It happens after identity
  verification; the approved wording and one-write/one-receipt checks remain.

`salon/agents.py` declares the same prerequisite on booking and complaints. The old
complaint-specific verification approval gate and direct complaint-to-verify exit
were removed. No new routing mechanism or model request was introduced to decide
the prerequisite. The complaint prompt remains the user's short, policy-based goal.

## Checks

| Check | Result |
|---|---|
| Full offline suite | **13/13 modules**, `offline.txt` |
| Ruff / formatting / ty | Clean, `ruff.txt`, `format.txt`, `types.txt` |
| Verification-first negative control | Removing the complaint prerequisite fails `service_requests_verify_first`; `negative-prerequisite.txt` |
| Real-model original recovery, adapted to the new order | **23/24**, `verification-first-recovery-20260921T192955Z.jsonl` |
| Real-model continuation suite | **6/12**, `verification-first-20260921T192957Z.jsonl` |

The offline adapter checks general questions and all four service intents, with
both unverified and already verified callers. They check that the owner does not
run before verification, that its request survives the detour, and that declining
verification clears the pending request. Consent, corrections, single receipts,
paired output arbitration and interrupted phone fragments remain covered.

The live continuation split is **phone 3/3, initial complaint through verification
3/3, upcoming-booking complaint 0/3, dated upcoming-booking complaint 0/3**. In the
latter cases the model used ordinary/policy questions instead of structured
readbacks; some answers assumed the past visit qualified for a free redo or promised
that the upcoming visit was free. These are real remaining quality problems.
The failed recovery complaint was not recorded exactly once. The new verification
order held, but this does not establish full complaint-flow acceptance.

Every real-model case was repeated three times with Gemini 3.5 Flash Lite, priority
service, MINIMAL thinking, fresh fixtures and a fixed salon clock. No microphone,
STT, TTS or client playback was exercised. The complaint caller sequence changed
to reflect the new workflow, so these scores are not a matched comparison with the
old verification-after-readback suite.

`snapshot.json` identifies the final source, prompt and dependency hashes; they
match the model run. `results.json` lists every failure. `before/` retains the
preceding code/suites as text and the prior complaint prompt. The
[prompt experiments](../prompt-simplification-evidence/README.md) and
[runtime-repair evidence](../complaint-stall-evidence/README.md) remain historical.

## Run it

From `/Users/nicoferdi/.codex/worktrees/517a/slng_voice_harness`:

```bash
uv run python -m tests
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.recovery --repeats 3 --label verify-first-recheck
uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python -m bench.continuations --repeats 3 --label verify-first-continuations
```

Restart the old bot to load the new graph and prompts:

```bash
SALON_FIXTURE=fresh uv run --env-file /Users/nicoferdi/Documents/GitHub/slng_voice_harness/.env python bot.py --port 7861
```

At `http://localhost:7861/client/`, first ask about prices, then request a booking
or complaint: the phone check should occur before that service starts. Start a new
call to test the other path. On the same verified call, a subsequent booking or
complaint should not repeat identification. Fresh microphone acceptance remains open.
