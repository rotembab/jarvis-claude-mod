"""Device selection by name, WASAPI-first on Windows (pure logic, no PortAudio)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal["input", "output"]


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float

    def supports(self, kind: Kind) -> bool:
        return (self.max_input_channels if kind == "input" else self.max_output_channels) > 0


def select_device(
    devices: list[DeviceInfo],
    kind: Kind,
    *,
    wanted: str | None,
    hostapi: str | None,
    default_index: int | None,
) -> DeviceInfo | None:
    """Pick a device: name match (exact, then substring) within the preferred
    host API, else that host API's default device, else any device of the kind.

    Names are matched case-insensitively; Windows decorates names (for example
    "Headset Microphone (PRO X Wireless Gaming Headset)"), so a substring such
    as "PRO X" is enough.
    """
    candidates = [d for d in devices if d.supports(kind)]
    if hostapi:
        preferred = [d for d in candidates if d.hostapi == hostapi]
        if preferred:
            candidates = preferred
    if not candidates:
        return None
    if wanted:
        needle = wanted.strip().casefold()
        for d in candidates:
            if d.name.casefold() == needle:
                return d
        for d in candidates:
            if needle in d.name.casefold():
                return d
    for d in candidates:
        if d.index == default_index:
            return d
    return candidates[0]
