"""Let Coval place simulated calls to a use case's `bot.py`, running on this laptop, and grade them.

Ported from `unmute_cli/coval_sim`. Coval reaches the bot over its simulated Twilio websocket, a real
Twilio number, or, for a use case on core, a LiveKit room the bot joins as a LiveKit worker. Run it
with `uv run python -m coval_sim --help`.
"""
