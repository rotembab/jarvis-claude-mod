"""Signal level helpers shared by capture, playback and the daemon."""

from __future__ import annotations

import math

import numpy as np

FLOOR_DB = -60.0


def rms(block: np.ndarray) -> float:
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64))))


def meter(block: np.ndarray) -> float:
    """RMS mapped from -60..0 dBFS onto 0..1 (a perceptually even meter)."""
    value = rms(block)
    if value <= 0.0:
        return 0.0
    db = 20.0 * math.log10(value)
    return min(1.0, max(0.0, (db - FLOOR_DB) / -FLOOR_DB))


def is_silent(pcm: np.ndarray, peak_threshold: float = 0.003) -> bool:
    """True when nothing in the clip rises above roughly -50 dBFS."""
    return pcm.size == 0 or float(np.max(np.abs(pcm))) < peak_threshold


def is_digital_silence(pcm: np.ndarray) -> bool:
    """Exact zeros: a real microphone always has some noise floor."""
    return pcm.size > 0 and not np.any(pcm)
