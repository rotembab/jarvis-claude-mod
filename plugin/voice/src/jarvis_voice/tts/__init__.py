"""Text-to-speech: the SpeechSynth interface and the Fish Audio live client."""

from .base import Pcm16Decoder, SpeechSynth, SynthError, SynthStream

__all__ = ["Pcm16Decoder", "SpeechSynth", "SynthError", "SynthStream"]
