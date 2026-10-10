"""The engine's HTTP face: `Studio` and its routes, grouped by what they are about.

app      the studio: store, hub, boot, its own runner, and the FastAPI app
agents   /api/agents: each agent, its state schema, its config
runs     /api/runners and /api/runs: runners linking, runs started, followed and gone back to
metrics  /api/metrics: timings from Jaeger
"""
