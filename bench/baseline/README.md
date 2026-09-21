# Baseline snapshot

What the harness was before Phase 01 touched it. Kept so every later change has
something honest to diff against, and so the evidence record has a revision to
name rather than "the working copy on the day".

`manifest.json` holds the commit sha and a SHA-256 of every tracked file,
`uv.lock` included. The repository had no commits at all until this snapshot, so
the digests came first and the commit second; either one alone would do now, and
both are kept because the lock digest is what a future rerun actually needs to
match.

`test_graph.txt`, `test_voice.txt`, `test_salon.txt` and `test_traces.txt` are
the retained stdout of the four offline modules **at that commit**, before any
Phase 01 change. All four ended `ok`. That is the prior result, not a V1 gate:
those four checks do not cover any of the defects Phase 01 exists to fix, which
is why six more modules now run beside them.

`after/` holds the same four plus the six new ones at the end of Phase 01. The
pair is the point. `test_voice.txt` carries the line `handoff budget of 3
spent, making intake answer` and `after/test_voice.txt` does not, because the
extra model run that warning announced is gone -- which is the one behaviour
change in this phase that a reader should be able to see rather than take on
trust.

`evidence/` is one JSON record per verification check, in the shape
`10_VERIFICATION_MATRIX.md` specifies, with honest `passed` / `inconclusive` /
`not-run` statuses and the limitations spelled out. Six passed offline. Nothing
in this directory was produced by a real provider, a real voice or a real
carrier, and none of it transfers to a live call.

## Rerunning the offline suite

```bash
uv run python -m tests
```

No network, no model. `ALLOW_MODEL_REQUESTS` is set to `False` before the
scenario modules are imported, so an accidental live request raises instead of
billing, and a placeholder key stands in so the agents can be built at all.

The long form below is the same thing without the runner, kept because the
verification plan quotes it and because it is what produced the transcripts
here:

```bash
cd /Users/nicoferdi/Documents/GitHub/slng_voice_harness && for m in tests.test_graph tests.test_voice tests.test_salon tests.test_traces tests.test_state tests.test_diary tests.test_flow tests.test_budget tests.test_bench tests.test_core; do PYTHONDONTWRITEBYTECODE=1 GOOGLE_API_KEY=offline-test-placeholder .venv/bin/python -B -c 'import sys, runpy; import pydantic_ai.models as models; models.ALLOW_MODEL_REQUESTS = False; runpy.run_module(sys.argv[1], run_name="__main__")' "$m" || exit "$?"; done
```

`PYDANTIC_AI_NO_BANNER=1` was also set when capturing the transcripts here, to
keep the setup banner out of the retained evidence. It changes nothing else.

## Regenerating the manifest

```bash
git ls-files | xargs shasum -a 256
```

`manifest.json` is that, keyed by path, plus the commit sha and the interpreter
version.
