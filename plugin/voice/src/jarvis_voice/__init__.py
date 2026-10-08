"""jarvis-voice: the local voice helper behind the Jarvis Claude Code mod.

The mod spawns ``python -m jarvis_voice run`` and talks to it over a
newline-delimited JSON event stream (stdout) and a token-protected HTTP
control server on 127.0.0.1. See ``plugin/protocol/schema.json``.
"""

__version__ = "0.6.3"
