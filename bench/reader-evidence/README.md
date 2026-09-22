# Separate trace-reader repair

22 September 2026. Completion-plan §6 names the reader at
`/Users/nicoferdi/.codex/worktrees/a809/unmute_cli/scripts/read_langfuse_trace.py`.
That worktree exists at `71304830c04614dacd0f6336e568b1162dfd1ed7`; its reader
and a new offline test were updated as a separate repository patch. The POC,
native example and measured native build are unchanged. No commit was created.

| Defect reproduced | Corrected result |
|---|---|
| N4 observation `56b92da4bdbe2af8` displayed 427 input and 14,812 total because it used Langfuse aliases as independent quantities. | 3,907 input + 19 output = **3,926 total**; 3,480 cached input is a subset. |
| Six lifetimes `[1,2,3,4,5,6]` reported p50=4. | Ordinary median **3.5**, explicitly labeled nearest-rank p90=6. |
| Missing usage could read as zero; character usage was labeled but its numeric value could be omitted. | Unknown fields stay `-`; reported zero stays zero; TTS characters remain separate from tokens. |
| The session listing labeled a trace count as turns. | The column is now labeled traces. |

The reader still reports observation usage and span lifetimes. It does not
pretend those are canonical LLM request counts, caller gaps or client playout.
Actual request/turn analysis remains the job of `bench/turns.py`.

`before.txt` and `after.txt` run the original and repaired readers on identical
inputs. `fixtures.json` compares the repaired usage interpretation against the
existing canonical reader for all **50 historical requests with usage data**.
The same fixture replay confirms N1–N4 **30 caller turns / 53 requests**, including
N2's 7 turns and N4's 9 turns. The separate native Pipecat fixture remains
5 turns / 10 requests and has no usage counts to compare. The missing historical
LiveKit export's 8-turn/16-request claim remains unverified.

Three offline regression checks, Ruff and ty passed against the actual patched
files; their output is retained here. Reproduce from the separate repository:

```sh
python3 -m unittest discover -s scripts -p test_read_langfuse_trace.py -v
ruff check scripts/read_langfuse_trace.py scripts/test_read_langfuse_trace.py
cd scripts
ty check read_langfuse_trace.py test_read_langfuse_trace.py
```

`reader.patch`, both source versions, the test source and `snapshot.json` retain
the exact change and hashes. API field and usage conventions were checked
against the [Langfuse public API](https://langfuse.com/docs/api-and-data-platform/features/public-api)
and [usage documentation](https://langfuse.com/docs/observability/features/token-and-cost-tracking);
the N4 normalization is additionally supported by the retained raw observation.

`verification.json` verifies the applied reader hashes and 45 unchanged POC /
reference-runner files. A read-only Langfuse CLI query at 22:10:59 UTC on
21 September found no exported `conversation` roots starting after 21:04 UTC;
the empty result is retained in `recent-voice-roots.json`. This supplies no new
microphone acceptance and does not rule out a call whose root has not exported.
