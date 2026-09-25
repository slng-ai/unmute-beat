# Faro tools

`bench.py` runs Faro's benchmark cases live: scripted text calls on the real
model, each call in its own process, one Langfuse session per call. It makes
billable model requests.

```bash
uv run bench.py              # every case
uv run bench.py L03 L07-ES   # some of them
```

It imports `faro` from `../faro-language-learning`, which is a workspace
dependency. The printout ends with every session id. Read cost and latency
with `../concierge-tools/ledger.py`.
