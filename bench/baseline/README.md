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

## Rerunning the offline suite

No network, no model. `ALLOW_MODEL_REQUESTS = False` is set before the scenario
modules are imported, so an accidental live request raises instead of billing.

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
