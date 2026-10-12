"""Runs: where they are placed, how they are followed, and where they are kept.

hub      the runners linked to the studio, each run placed on one, every event stored and streamed
store    Redis for a week and Postgres behind it, or memory (`--no-services`, tests)
process  a runner the studio starts itself
"""
