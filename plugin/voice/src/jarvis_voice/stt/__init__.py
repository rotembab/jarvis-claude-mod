"""Speech-to-text: the Transcriber interface and its engines."""

from .base import SttDevice, SttError, Transcriber, Transcript

__all__ = ["SttDevice", "SttError", "Transcriber", "Transcript"]
