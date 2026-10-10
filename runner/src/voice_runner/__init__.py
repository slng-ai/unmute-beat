"""The runner service: every agent in the repository, loaded once, carrying calls by voice or by text.

settings  the runner's settings, what the studio asks of it, where use cases keep their data
agents    every use case's `core.Entry`, imported by package name
calls     one call carried, and the listener that sends its events to the studio
runner    the agents compiled and warmed, the calls carried, and how it stops
link      the one websocket to the studio
app       the HTTP face
"""
