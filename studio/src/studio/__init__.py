"""slng-voice-studio: a local studio for slng-voice-core.

`uv run slng studio up` starts Postgres and Redis (compose.yaml), then the engine, which serves
the API under `/api` and the built web app at `/`. The engine finds every `agent.yaml` in the
repository and reads it with `core.load`: no model, no key, no network.

The modules, in the order a request meets them:

    cli        the `slng` command: up, down, status, agents
    settings   ports, paths and URLs, from the environment with local defaults
    boot       the start-up sequence the web page's loading screen follows
    discovery  every agent.yaml, read into a step graph the web page draws
    events     the wire format of a run: what the live view and the store both read
    server     the FastAPI app
"""
