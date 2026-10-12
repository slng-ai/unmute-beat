"""slng-voice-studio: a local studio for slng-voice-core.

`uv run slng studio up` starts Postgres, Redis and Jaeger (compose.yaml), then the engine, which serves
the API under `/api` and the built web app at `/`, and a runner that carries calls. The engine finds
every `agent.yaml` in the repository and reads it with `core.load`: no model, no key, no network.

    cli       the `slng` command
    settings  ports, paths and URLs, from the environment with local defaults
    boot      the start-up sequence the web page's loading screen follows
    agents    every agent: found, checked, drawn, and its config edited
    runs      runs placed on runners, followed live, and kept
    metrics   timings from the local Jaeger
    events    the wire format of a run: what the live view and the store both read
    api       the FastAPI app and its routes
"""
