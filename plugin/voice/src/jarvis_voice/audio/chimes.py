"""Short generated UI tones (no asset files)."""

from __future__ import annotations

import numpy as np

Note = tuple[float, float]  # (frequency Hz, duration s); frequency 0 = rest


def tone(notes: list[Note], samplerate: int, volume: float = 0.18) -> np.ndarray:
    """Concatenate sine notes, each with a 5 ms raised-cosine fade to avoid clicks."""
    parts: list[np.ndarray] = []
    fade = max(1, int(0.005 * samplerate))
    for freq, duration in notes:
        n = max(1, int(duration * samplerate))
        if freq <= 0:
            parts.append(np.zeros(n, dtype=np.float32))
            continue
        t = np.arange(n, dtype=np.float32) / samplerate
        wave = np.sin(2 * np.pi * freq * t).astype(np.float32) * volume
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, min(fade, n // 2 or 1), dtype=np.float32))
        wave[: ramp.size] *= ramp
        wave[-ramp.size :] *= ramp[::-1]
        parts.append(wave)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def listen_start(samplerate: int) -> np.ndarray:
    return tone([(660.0, 0.06), (0, 0.015), (880.0, 0.07)], samplerate)


def listen_stop(samplerate: int) -> np.ndarray:
    return tone([(880.0, 0.05), (0, 0.015), (590.0, 0.07)], samplerate, volume=0.14)


def error(samplerate: int) -> np.ndarray:
    return tone([(220.0, 0.09), (0, 0.05), (220.0, 0.09)], samplerate, volume=0.16)
