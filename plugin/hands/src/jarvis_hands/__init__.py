"""jarvis-hands: webcam hand-gesture control behind the Jarvis Claude Code mod.

The mod spawns ``python -m jarvis_hands run`` and talks to it the way it talks
to the voice helper: JSON events on stdout, commands over a token-protected
HTTP server on 127.0.0.1. See ``plugin/protocol/hands.schema.json``.
"""

__version__ = "0.1.0"
