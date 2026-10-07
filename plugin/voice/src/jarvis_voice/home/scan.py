"""A read-only look at the home network: which smart devices answer, what they are, and whether Jarvis can control them.

``scan`` runs four searches at once, each on a thread of its own, for about seven seconds:

- mDNS (python-zeroconf, IPv4 only): a fixed list of service types, each answer resolved for its TXT record;
- SSDP: an M-SEARCH for a handful of search targets, then each device's own description, fetched over
  plain HTTP from the device that answered and from no other address;
- Tuya: listening for the devices' UDP broadcasts (tinytuya's scanner without polling, so no connection);
- the UDP discovery messages of Kasa, LIFX, WiZ, Yeelight and Govee.

Everything here only asks "who is there". Nothing pairs, signs in, writes or changes a device: there is
no HomeKit pair-setup, no Hue POST, no LG or Samsung websocket (both put a prompt on the TV), no Tuya TCP
connection, no Cast connection and no Kasa ``set_*`` call, because the code for them does not exist.

The answers are merged per address inside this module only. The report names devices and kinds and never
carries an address, a MAC, a serial or an id (``clean_name`` strips them from the devices' own names), and
nothing is written to devices.json. Raw TXT records and SSDP bodies are never logged. Each search fails
soft: a port in use, a firewall or a missing library becomes a note in the report, not an exception.
"""

from __future__ import annotations

import contextlib
import html
import json
import logging
import re
import select
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

from . import bravia, net
from .model import DeviceRecord, HomeConfig, normalize

log = logging.getLogger(__name__)

SCAN_S = 7.0
# The searches are joined this long after ``seconds``; whatever is still running then is left out.
JOIN_MARGIN_S = 2.0
# One scan per minute: each one sends a burst of multicast and broadcast traffic.
MIN_GAP_S = 60.0

RESOLVE_TIMEOUT_MS = 1500
MAX_MDNS_NAMES = 96
MDNS_WORKERS = 16
SSDP_S = 3.0  # with MX 2, every answer is in well before this (Windows Firewall lets replies in for 3 s)
DESCRIPTION_TIMEOUT_S = 1.5
MAX_DESCRIPTIONS = 24
UDP_S = 3.0

# Browsed for one answer each; the printer types only so that printers can be left out by name.
MDNS_TYPES = tuple(
    f"{t}.local."
    for t in (
        "_hap._tcp",
        "_hap._udp",
        "_matter._tcp",
        "_matterc._udp",
        "_googlecast._tcp",
        "_airplay._tcp",
        "_raop._tcp",
        "_companion-link._tcp",
        "_device-info._tcp",
        "_androidtvremote2._tcp",
        "_hue._tcp",
        "_shelly._tcp",
        "_http._tcp",
        "_esphomelib._tcp",
        "_sonos._tcp",
        "_spotify-connect._tcp",
        "_nanoleafapi._tcp",
        "_nanoleafms._tcp",
        "_elg._tcp",
        "_miio._udp",
        "_ewelink._tcp",
        "_lutron._tcp",
        "_lifx._udp",
        "_wled._tcp",
        "_home-assistant._tcp",
        "_meshcop._udp",
        "_ipp._tcp",
        "_ipps._tcp",
        "_printer._tcp",
    )
)

SSDP_TARGETS = (
    "upnp:rootdevice",
    bravia.SSDP_ST,
    "urn:lge-com:service:webos-second-screen:1",
    "urn:samsung.com:device:RemoteControlReceiver:1",
    "roku:ecp",
    "urn:dial-multiscreen-org:service:dial:1",
    "urn:schemas-upnp-org:device:ZonePlayer:1",
    "urn:Belkin:service:basicevent:1",
)

# What a failed or skipped search is called in the report.
PROBE_LABELS = {
    "mdns": "mDNS (Bonjour)",
    "ssdp": "UPnP",
    "tuya": "Tuya",
    "udp": "Kasa, LIFX, WiZ, Yeelight and Govee",
}

FIREWALL_HINT = (
    "Windows Firewall may be blocking Python on this network, or the network may be set to Public. "
    "In Windows Security > Firewall & network protection > Allow an app through firewall, allow Python "
    "on Private networks, and check that this network is set to Private."
)

Status = Literal["set_up", "not_set_up", "could_add", "apple_home", "homekit_free", "matter", "ignored"]

# HomeKit's accessory categories (the ``ci`` TXT key), as plain words.
HOMEKIT_KINDS = {
    1: "device",
    2: "bridge",
    3: "fan",
    4: "garage door opener",
    5: "light",
    6: "lock",
    7: "outlet",
    8: "switch",
    9: "thermostat",
    10: "sensor",
    11: "security system",
    12: "door",
    13: "window",
    14: "blind",
    15: "button",
    16: "range extender",
    17: "camera",
    18: "doorbell",
    19: "air purifier",
    20: "heater",
    21: "air conditioner",
    22: "humidifier",
    23: "dehumidifier",
    24: "Apple TV",
    25: "HomePod",
    26: "speaker",
    27: "AirPort",
    28: "sprinkler",
    29: "faucet",
    30: "shower head",
    31: "TV",
    32: "remote",
    33: "router",
    34: "audio receiver",
    35: "TV box",
    36: "streaming stick",
}

# The start of a HomeKit ``md`` (model) value, and the brand it names (after Home Assistant's table).
HOMEKIT_BRANDS = (
    ("BSB00", "Philips Hue"),
    ("NL", "Nanoleaf"),
    ("LIFX", "LIFX"),
    ("Smart Bridge", "Lutron Caseta"),
    ("TRADFRI", "IKEA"),
    ("Wemo", "Belkin Wemo"),
    ("Socket", "Belkin Wemo"),
    ("YL", "Yeelight"),
    ("ecobee", "ecobee"),
    ("EB", "ecobee"),
    ("tado", "tado"),
    ("AC02", "tado"),
    ("Sensibo", "Sensibo"),
    ("Rachio", "Rachio"),
    ("3810X", "Roku"),
    ("3820X", "Roku"),
    ("4660X", "Roku"),
    ("7820X", "Roku"),
    ("C105X", "Roku"),
    ("C135X", "Roku"),
    ("HHKBridge", "Hive"),
    ("iSmartGate", "iSmartGate"),
    ("PowerView", "Hunter Douglas"),
    ("Eve", "Eve"),
)

# Matter device types (the ``DT`` TXT key of a device waiting to be set up), as plain words.
MATTER_KINDS = {
    0x0100: "light",
    0x0101: "light",
    0x010C: "light",
    0x010D: "light",
    0x010A: "plug",
    0x010B: "plug",
    0x0103: "switch",
    0x0104: "dimmer switch",
    0x000F: "button",
    0x000A: "lock",
    0x0202: "blind",
    0x0301: "thermostat",
    0x002B: "fan",
    0x0072: "air conditioner",
    0x0074: "robot vacuum",
    0x002D: "air purifier",
    0x0022: "speaker",
    0x0023: "TV",
    0x0028: "TV",
    0x000E: "bridge",
    0x0015: "contact sensor",
    0x0107: "motion sensor",
    0x0302: "temperature sensor",
    0x0091: "Thread border router",
}

# Apple model names (``rpMd``, ``model``, ``am``) of computers and phones, left out of the report.
APPLE_COMPUTERS = ("Mac", "iMac", "iPhone", "iPad", "iPod", "RackMac", "Xserve", "Watch", "RealityDevice")

# Why something was left out, singular and plural.
IGNORED = {
    "printer": ("printer", "printers"),
    "computer": ("computer or phone", "computers or phones"),
    "network": ("router or network box", "routers or network boxes"),
    "media_server": ("media server", "media servers"),
    "other": ("other device", "other devices"),
}


# --------------------------------------------------------------------------- what the searches hear


@dataclass(slots=True)
class Sighting:
    """One answer: an mDNS service, an SSDP device, a Tuya broadcast or a UDP discovery reply.

    ``host``, ``server`` and ``props`` only merge and classify answers inside this module; they
    never reach the report (``props`` may hold ids, which is why the repr leaves them out)."""

    source: str  # "mdns", "ssdp", "tuya" or "udp"
    service: str  # "_hap._tcp", "ssdp", "tuya", "kasa", ...
    host: str | None = None
    name: str = ""
    props: dict[str, str] = field(default_factory=dict)
    server: str = ""  # the mDNS host name, which merges a Thread device's records (they carry no IPv4 address)

    def __repr__(self) -> str:
        return f"Sighting({self.source!r}, {self.service!r})"


# A search: given the seconds it may take and a list for spoken notes, the answers it heard.
Probe = Callable[[float, list[str]], list[Sighting]]


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """One device in the report. ``name`` is the device's own name with ids stripped ("" when it has none)."""

    kind: str  # plain words: "TV", "light", "bridge", "printer", ...
    name: str
    brand: str  # "Sony Bravia", "Philips Hue", "HomeKit", "Matter", or "" for something left out
    status: Status
    jarvis_name: str | None = None  # the name Jarvis knows it by, when set up
    # A short spoken hint: "needs the button on the bridge pressed once"; for "not_set_up", how to add it.
    detail: str = ""
    count: int = 1  # Matter devices in one Matter home are one entry

    def label(self) -> str:
        """``Sony Bravia TV``, ``HomeKit light``, ``Apple TV``."""
        if not self.brand or normalize(self.kind).startswith(normalize(self.brand)):
            return self.kind
        return f"{self.brand} {self.kind}"

    def shown_name(self) -> str:
        """The device's own name when it says more than its label."""
        name = normalize(self.name)
        if not name or name in {normalize(self.label()), normalize(self.brand), normalize(self.kind)}:
            return ""
        return self.name

    def line(self) -> str:
        if self.status == "matter" and not self.detail:  # the devices of one Matter home
            return f"- {_count(self.count, 'Matter device')} in one Matter home."
        text = self.label()
        if self.shown_name():
            text += f' "{self.shown_name()}"'
        parts: list[str] = []
        if self.status == "set_up":
            parts.append(f"set up as {self.jarvis_name}" if self.jarvis_name else "set up")
        if self.detail:
            parts.append(f"not set up yet: {self.detail}" if self.status == "not_set_up" else self.detail)
        elif self.status == "not_set_up":
            parts.append("not set up yet")
        return f"- {text}" + (f": {'; '.join(parts)}" if parts else "") + "."

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "name": self.name, "brand": self.brand, "status": self.status}
        if self.jarvis_name:
            data["jarvis_name"] = self.jarvis_name
        if self.detail:
            data["detail"] = self.detail
        if self.count != 1:
            data["count"] = self.count
        return data


# The report's groups, in order: (heading, statuses).
GROUPS: tuple[tuple[str, tuple[Status, ...]], ...] = (
    ("Jarvis controls these:", ("set_up", "not_set_up")),
    ("Jarvis could control these with a new driver:", ("could_add",)),
    ("In Apple Home (Siri controls these; Jarvis can't share them):", ("apple_home",)),
    ("HomeKit devices not in any home yet:", ("homekit_free",)),
    ("Matter devices (Jarvis can't control Matter yet; the app that set them up can):", ("matter",)),
)


@dataclass(slots=True)
class ScanReport:
    """What one scan found. ``text`` is what Jarvis reads out or shows; ``to_json`` is the same as data."""

    entries: list[ScanEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0
    answered: bool = False  # anything at all answered, even a printer
    note: str = ""  # said first: why this is an earlier report, or why no scan ran
    ran: bool = True

    def devices(self) -> list[ScanEntry]:
        """The smart devices, without what was left out."""
        return [e for e in self.entries if e.status != "ignored"]

    def text(self) -> str:
        lines: list[str] = [self.note] if self.note else []
        if not self.ran:
            return "\n".join(lines)
        seconds = max(1, round(self.seconds))
        if not self.answered:
            lines.append(f"I searched the network for {seconds} seconds and nothing answered. {FIREWALL_HINT}")
            lines.extend(self.notes)
            return "\n".join(lines)
        found = sum(e.count for e in self.devices())
        if found:
            lines.append(f"I searched the network for {seconds} seconds and found {_count(found, 'smart device')}.")
        else:
            lines.append(f"I searched the network for {seconds} seconds and found no smart devices.")
        for heading, statuses in GROUPS:
            group = [e for e in self.entries if e.status in statuses]
            if group:
                lines.append("")
                lines.append(heading)
                lines.extend(e.line() for e in group)
        ignored = _ignored_line([e for e in self.entries if e.status == "ignored"])
        if ignored:
            lines.append("")
            lines.append(ignored)
        if self.notes:
            lines.append("")
            lines.extend(self.notes)
        lines.append("Bluetooth and Zigbee devices don't show up in a network search; only their hub does.")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "entries": [e.to_json() for e in self.entries],
            "notes": list(self.notes),
            "seconds": round(self.seconds, 1),
            "answered": self.answered,
        }
        if self.note:
            data["note"] = self.note
        return data


def _count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else plural or noun + 's'}"


def _ignored_line(entries: list[ScanEntry]) -> str:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    parts = []
    for singular, plural in IGNORED.values():
        if counts.get(singular):
            parts.append(_count(counts[singular], singular, plural))
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"I left out {joined}."


# --------------------------------------------------------------------------- the scan


class _History:
    """The last scan, for the once-a-minute limit (one per process: the helper keeps it)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.started: float | None = None
        self.report: ScanReport | None = None


_history = _History()


def default_probes() -> dict[str, Probe]:
    return {"mdns": _mdns_probe, "ssdp": _ssdp_probe, "tuya": _tuya_probe, "udp": _udp_probe}


def scan(
    config: HomeConfig | None = None,
    *,
    seconds: float = SCAN_S,
    probes: Mapping[str, Probe] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanReport:
    """Searches the home network for about ``seconds`` (all searches at once) and says what answered.

    ``config`` is the saved configuration, to say which devices Jarvis already has. Within a minute of
    the last scan, that scan's report comes back with a note instead. Never raises for a network problem.
    """
    with _history.lock:
        now = clock()
        if _history.running:
            return ScanReport(note="I'm already searching the network. Ask again in a few seconds.", ran=False)
        last, started = _history.report, _history.started
        if last is not None and started is not None and now - started < MIN_GAP_S:
            ago = max(1, round(now - started))
            note = f"I searched {_count(ago, 'second')} ago and search at most once a minute, so this is from then."
            return replace(last, note=note)
        _history.running, _history.started = True, now
    report: ScanReport | None = None
    try:
        report = _run(config or HomeConfig(), seconds, dict(probes) if probes is not None else default_probes())
        return report
    finally:
        with _history.lock:
            _history.running = False
            if report is not None:
                _history.report = report


def _run(config: HomeConfig, seconds: float, probes: dict[str, Probe]) -> ScanReport:
    start = time.monotonic()
    results: dict[str, list[Sighting]] = {}
    notes: dict[str, list[str]] = {name: [] for name in probes}
    failed: set[str] = set()

    def work(name: str, probe: Probe) -> None:
        try:
            results[name] = list(probe(seconds, notes[name]))
        except Exception as exc:  # noqa: BLE001 - one search must not end the others
            log.warning("home scan: the %s search raised %s", name, type(exc).__name__)
            failed.add(name)

    threads = {
        name: threading.Thread(target=work, args=(name, probe), name=f"home-scan-{name}", daemon=True)
        for name, probe in probes.items()
    }
    for thread in threads.values():
        thread.start()
    deadline = start + seconds + JOIN_MARGIN_S
    for thread in threads.values():
        thread.join(max(0.0, deadline - time.monotonic()))

    sightings: list[Sighting] = []
    spoken: list[str] = []
    heard: dict[str, int] = {}
    for name, thread in threads.items():
        label = PROBE_LABELS.get(name, name)
        if thread.is_alive():
            spoken.append(f"The {label} search took too long, so its answers are left out.")
            continue
        if name in failed:
            spoken.append(f"The {label} search failed.")
        spoken.extend(notes[name])
        heard[name] = len(results.get(name, []))
        sightings.extend(results.get(name, []))

    own = set(bravia._lan_addresses())
    sightings = [s for s in sightings if s.host is None or s.host not in own]
    if sightings and heard.get("mdns") == 0 and "mdns" not in failed and not notes.get("mdns"):
        spoken.append(f"Nothing answered the mDNS search, which most smart devices use. {FIREWALL_HINT}")
    entries = classify(sightings, config)
    report = ScanReport(entries, spoken, time.monotonic() - start, answered=bool(sightings))
    log.info(
        "home scan: %d answers, %d devices, %d left out",
        len(sightings),
        len(report.devices()),
        len(entries) - len(report.devices()),
    )
    return report


# --------------------------------------------------------------------------- names

_UUID = re.compile(r"(?:uuid:)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_MAC_TEXT = re.compile(r"(?<![0-9a-z])[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}(?![0-9a-z])", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_RAOP_PREFIX = re.compile(r"^[0-9a-f]{12}@", re.IGNORECASE)
_HEX_RUN = re.compile(r"(?<![0-9a-z])[0-9a-f]{8,}(?![0-9a-z])", re.IGNORECASE)
# A MAC's last three bytes, as in "esp-kitchen-a1b2c3": six or more hex digits with a digit and a letter.
_HEX_TAIL = re.compile(r"(?<![0-9a-z])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,}(?![0-9a-z])", re.IGNORECASE)
_EDGE = re.compile(r"^[\s\-_:@.,#/|]+|[\s\-_:@.,#/|]+$")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
NAME_MAX = 60


def clean_name(text: str) -> str:
    """A device's own name without addresses, MACs, UUIDs or hex ids (a serial, a HomeKit or Tuya id)."""
    name = _RAOP_PREFIX.sub("", str(text or "").strip())
    for pattern in (_UUID, _MAC_TEXT, _IPV4, _HEX_RUN, _HEX_TAIL):
        name = pattern.sub(" ", name)
    name = _EMPTY_BRACKETS.sub(" ", name.replace("_", " "))
    name = re.sub(r"\s*-\s*(?:-\s*)+", " - ", name)
    name = _EDGE.sub("", re.sub(r"\s+", " ", name))
    return name[:NAME_MAX].strip()


# --------------------------------------------------------------------------- classification


class _Host:
    """Everything one address (or one Thread device) answered, for the rules below."""

    def __init__(self, sightings: list[Sighting]) -> None:
        self.all = sightings
        self.address = next((s.host for s in sightings if s.host), None)

    def of(self, *services: str) -> list[Sighting]:
        return [s for s in self.all if s.service in services]

    def has(self, *services: str) -> bool:
        return any(s.service in services for s in self.all)

    def prop(self, key: str, *services: str) -> str:
        for s in self.all:
            if (not services or s.service in services) and s.props.get(key):
                return s.props[key]
        return ""

    def name(self, *services: str) -> str:
        for service in services:
            for s in self.of(service):
                cleaned = clean_name(s.name)
                if cleaned:
                    return cleaned
        return ""

    def ssdp(self, key: str) -> str:
        return self.prop(key, "ssdp")

    def in_apple_home(self) -> bool:
        return any(_homekit_paired(s) for s in self.of("_hap._tcp", "_hap._udp"))


def classify(sightings: Iterable[Sighting], config: HomeConfig) -> list[ScanEntry]:
    """One entry per device (answers merged by address), in report order."""
    groups: dict[str, list[Sighting]] = {}
    for s in sightings:
        key = s.host or (f"server:{s.server.lower()}" if s.server else f"name:{s.service}:{s.name}")
        groups.setdefault(key, []).append(s)
    entries: list[ScanEntry] = []
    fabrics: dict[str, set[str]] = {}
    for group in groups.values():
        host = _Host(group)
        found, nodes = _classify_host(host, config)
        entries.extend(found)
        for fabric, node in nodes:
            fabrics.setdefault(fabric, set()).add(node)
    for nodes in sorted(fabrics.values(), key=len, reverse=True):
        entries.append(ScanEntry("device", "", "Matter", "matter", count=len(nodes)))
    order = {status: i for i, (_, statuses) in enumerate(GROUPS) for status in statuses}
    return sorted(entries, key=lambda e: (order.get(e.status, len(GROUPS)), e.label().lower(), e.name.lower()))


def _classify_host(h: _Host, config: HomeConfig) -> tuple[list[ScanEntry], list[tuple[str, str]]]:
    """The host's entries, and the (fabric, node) pairs of Matter devices that only say they are in a home."""
    if h.has("tuya"):
        return [_tuya_entry(h, config)], []
    if h.has("_googlecast._tcp") and "group" in h.prop("md", "_googlecast._tcp").lower():
        return [], []  # a speaker group made in Google Home, not a device
    for rule in (_bravia, _home_assistant, _apple, _branded):
        entry = rule(h, config)
        if entry is not None:
            if entry.status != "ignored" and h.in_apple_home():
                extra = "also in Apple Home"
                entry = replace(entry, detail=f"{entry.detail}; {extra}" if entry.detail else extra)
            return [entry], []
    if h.has("_hap._tcp", "_hap._udp"):
        return [_homekit_entry(s) for s in h.of("_hap._tcp", "_hap._udp")], []
    if h.has("_matter._tcp", "_matterc._udp"):
        entries = [_matter_waiting(s) for s in h.of("_matterc._udp")]
        nodes = []
        for s in h.of("_matter._tcp"):
            fabric, _, node = s.name.partition("-")
            nodes.append((fabric.upper(), (node or s.name).upper()))
        return entries, nodes
    return [_ignored(h)], []


def _saved(config: HomeConfig, driver: str) -> list[DeviceRecord]:
    return [d for d in config.devices if d.driver == driver]


def _not_set_up(kind: str, name: str, brand: str, detail: str = "add it in home setup") -> ScanEntry:
    """A device Jarvis supports but does not have yet; ``detail`` says how to add it."""
    return ScanEntry(kind, name, brand, "not_set_up", detail=detail)


# -- supported now


def _tuya_entry(h: _Host, config: HomeConfig) -> ScanEntry:
    gw_id = h.prop("id", "tuya")
    saved = [d for d in _saved(config, "tuya") if d.kind != "scene"]
    own = _names(d.name for d in saved if d.settings.get("tuya_id") == gw_id)
    if gw_id and own:
        return ScanEntry("device", "", "Tuya", "set_up", jarvis_name=_join(own))
    children = _names(d.name for d in saved if d.settings.get("parent") == gw_id)
    if gw_id and children:
        return ScanEntry("gateway", "", "Tuya", "set_up", detail=f"Jarvis reaches {_join(children)} through it")
    if saved:
        return _not_set_up("device", "", "Tuya", "refresh the Tuya link in home setup")
    return _not_set_up("device", "", "Tuya", "link Tuya in home setup")


def _names(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(n for n in names if n))


def _join(names: list[str]) -> str:
    if len(names) > 3:
        names = [*names[:3], f"{len(names) - 3} more"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _bravia(h: _Host, config: HomeConfig) -> ScanEntry | None:
    scalar = h.ssdp("scalar").split()
    saved = next(
        (
            d
            for d in _saved(config, "bravia")
            if h.address and bravia.host_of(str(d.settings.get("address") or "")) == h.address
        ),
        None,
    )
    # A Google TV's Cast and Android TV remote records, on the same address, say Sony without SSDP.
    sony_tv = "bravia" in h.prop("md", "_googlecast._tcp").lower() or (
        h.prop("manufacturer", "_airplay._tcp").lower().startswith("sony") and h.has("_androidtvremote2._tcp")
    )
    if not ("videoScreen" in scalar or saved is not None or sony_tv):
        if scalar and h.ssdp("manufacturer").lower().startswith("sony"):
            return _could("sound system", clean_name(h.ssdp("friendlyname")), "Sony")  # a soundbar or receiver
        return None
    # The Cast name is the one the user chose; the UPnP one is often just the model.
    name = _cast_name(h) or clean_name(h.ssdp("friendlyname"))
    if saved is not None:
        return ScanEntry("TV", name, "Sony Bravia", "set_up", jarvis_name=saved.name)
    return _not_set_up("TV", name, "Sony Bravia")


def _home_assistant(h: _Host, config: HomeConfig) -> ScanEntry | None:
    if not h.has("_home-assistant._tcp"):
        return None
    name = clean_name(h.prop("location_name", "_home-assistant._tcp"))
    saved = config.hubs.get("homeassistant") or {}
    saved_host = _url_host(str(saved.get("url") or ""))
    hosts = {h.address} | {
        _url_host(h.prop(key, "_home-assistant._tcp")) for key in ("base_url", "internal_url", "external_url")
    }
    if saved_host and saved_host in hosts:
        return ScanEntry("hub", name, "Home Assistant", "set_up")
    return _not_set_up("hub", name, "Home Assistant", "connect it in home setup")


def _url_host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname if url else None
    except ValueError:
        return None


def _apple_model(h: _Host) -> str:
    for key, service in (
        ("rpmd", "_companion-link._tcp"),
        ("model", "_airplay._tcp"),
        ("model", "_device-info._tcp"),
        ("am", "_raop._tcp"),
    ):
        value = h.prop(key, service)
        if value:
            return value
    return ""


def _apple(h: _Host, config: HomeConfig) -> ScanEntry | None:
    model = _apple_model(h)
    name = h.name("_airplay._tcp", "_companion-link._tcp", "_raop._tcp")
    if model.startswith("AppleTV"):
        ids = _apple_ids(h)
        for device in _saved(config, "appletv"):
            saved_ids = {_plain_id(i) for i in device.settings.get("identifiers") or [] if isinstance(i, str)}
            if (h.address and device.settings.get("address") == h.address) or (ids & saved_ids):
                return ScanEntry("Apple TV", name, "Apple", "set_up", jarvis_name=device.name)
        return _not_set_up("Apple TV", name, "Apple")
    if model.startswith("AudioAccessory"):
        return ScanEntry("HomePod", name, "Apple", "could_add", detail="volume and audio only")
    if model.startswith("AirPort"):
        return ScanEntry(IGNORED["network"][0], "", "", "ignored")
    if model.startswith(APPLE_COMPUTERS) or (h.has("_companion-link._tcp") and not model):
        return ScanEntry(IGNORED["computer"][0], "", "", "ignored")
    return None


def _plain_id(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.lower())


def _apple_ids(h: _Host) -> set[str]:
    """The ids pyatv saves for an Apple TV (the AirPlay device id, the RAOP name's MAC), compared only."""
    ids = {_plain_id(h.prop(key)) for key in ("deviceid", "rpmrtid", "rpba", "psi", "pi")}
    for s in h.of("_raop._tcp"):
        mac, at, _ = s.name.partition("@")
        if at:
            ids.add(_plain_id(mac))
    return {i for i in ids if i}


# -- could be added with a new driver


def _could(kind: str, name: str, brand: str, detail: str = "") -> ScanEntry:
    return ScanEntry(kind, name, brand, "could_add", detail=detail)


def _homekit_brand(h: _Host) -> str:
    model = h.prop("md", "_hap._tcp", "_hap._udp")
    return next((brand for prefix, brand in HOMEKIT_BRANDS if model.lower().startswith(prefix.lower())), "")


def _branded(h: _Host, config: HomeConfig) -> ScanEntry | None:
    """Brands with a local interface Jarvis could use, then TVs and speakers it could reach."""
    hk_brand = _homekit_brand(h)
    hap_name = h.name("_hap._tcp", "_hap._udp")
    manufacturer = h.ssdp("manufacturer").lower()
    device_type = h.ssdp("devicetype")
    st = h.ssdp("st")
    server = h.ssdp("server").lower()
    friendly = clean_name(h.ssdp("friendlyname"))
    if (
        h.has("_hue._tcp")
        or "hue bridge" in h.ssdp("modelname").lower()
        or "ipbridge" in server
        or hk_brand == "Philips Hue"
    ):
        name = h.name("_hue._tcp") or friendly or hap_name
        return _could("bridge", name, "Philips Hue", "needs the button on the bridge pressed once")
    shelly = [s for s in h.of("_http._tcp") if s.name.lower().startswith("shelly")]
    if h.has("_shelly._tcp") or shelly:
        return _could("device", h.name("_shelly._tcp") or clean_name(shelly[0].name if shelly else ""), "Shelly")
    if h.has("_esphomelib._tcp"):
        name = clean_name(h.prop("friendly_name", "_esphomelib._tcp")) or h.name("_esphomelib._tcp")
        return _could("device", name, "ESPHome")
    if h.has("_sonos._tcp") or "ZonePlayer" in st or "ZonePlayer" in device_type or "sonos" in server:
        return _could("speaker", friendly or h.name("_sonos._tcp"), "Sonos")
    if (
        h.has("_nanoleafapi._tcp", "_nanoleafms._tcp")
        or "nanoleaf" in (st + device_type).lower()
        or hk_brand == "Nanoleaf"
    ):
        name = h.name("_nanoleafapi._tcp", "_nanoleafms._tcp") or hap_name
        return _could("light", name, "Nanoleaf", "needs its power button held once")
    if h.has("_elg._tcp"):
        return _could("light", h.name("_elg._tcp"), "Elgato")
    if h.has("_lutron._tcp") or hk_brand == "Lutron Caseta":
        return _could("bridge", "", "Lutron Caseta", "needs the button on the bridge pressed once")
    if h.has("_lifx._udp", "lifx") or hk_brand == "LIFX":
        return _could("light", h.name("_lifx._udp") or hap_name, "LIFX")
    if h.has("_wled._tcp"):
        return _could("light", h.name("_wled._tcp"), "WLED")
    if h.has("yeelight") or hk_brand == "Yeelight":
        return _could("light", hap_name, "Yeelight")
    if h.has("_miio._udp"):
        if any(s.name.lower().startswith("yeelink") for s in h.of("_miio._udp")):
            return _could("light", "", "Yeelight", "needs LAN Control switched on in the Yeelight app")
        return _could("device", "", "Xiaomi", "needs a key from the Xiaomi account")
    if h.has("_ewelink._tcp"):
        return _could("switch", "", "Sonoff (eWeLink)", "needs its key from the eWeLink account, or DIY mode")
    if h.has("kasa"):
        return _could(h.prop("kind", "kasa") or "plug", clean_name(h.prop("alias", "kasa")), "TP-Link Kasa")
    if h.has("wiz"):
        return _could("light", "", "WiZ")
    if h.has("govee"):
        return _could("light", "", "Govee")
    if h.has("_airplay._tcp") and h.prop("manufacturer", "_airplay._tcp").lower().startswith("samsung"):
        return _could("TV", h.name("_airplay._tcp"), "Samsung", "the TV asks you to allow Jarvis once")
    if "webos-second-screen" in st or manufacturer.startswith("lg electronics"):
        return _could("TV", friendly, "LG", "the TV asks you to allow Jarvis once")
    if "RemoteControlReceiver" in st + device_type or (
        manufacturer.startswith("samsung") and "[tv]" in friendly.lower()
    ):
        return _could("TV", friendly.replace("[TV]", "").strip(), "Samsung", "the TV asks you to allow Jarvis once")
    if "roku:ecp" in st or "roku" in device_type.lower() or manufacturer.startswith("roku") or hk_brand == "Roku":
        detail = "needs Control by mobile apps switched on in the Roku's settings"
        return _could("player", friendly or hap_name, "Roku", detail)
    if manufacturer.startswith("belkin") or "basicevent" in st or hk_brand == "Belkin Wemo":
        return _could("switch", friendly or hap_name, "Belkin Wemo")
    if h.has("_googlecast._tcp"):
        return _cast_entry(h)
    if h.has("_androidtvremote2._tcp"):
        return _could("TV", h.name("_androidtvremote2._tcp"), "Android TV or Google TV", "needs a code shown on the TV")
    if h.has("_airplay._tcp", "_raop._tcp") and not h.has("_hap._tcp", "_hap._udp"):
        brand = clean_name(h.prop("manufacturer", "_airplay._tcp")) or "AirPlay"
        return _could("speaker", h.name("_airplay._tcp", "_raop._tcp"), brand, "volume and audio only")
    return None


def _cast_name(h: _Host) -> str:
    return clean_name(h.prop("fn", "_googlecast._tcp"))


def _cast_entry(h: _Host) -> ScanEntry:
    model = h.prop("md", "_googlecast._tcp")
    lower = model.lower()
    if "tv" in lower or h.has("_androidtvremote2._tcp"):
        kind = "TV"
    elif "chromecast" in lower:
        kind = "Chromecast"
    elif "hub" in lower:
        kind = "display"
    elif any(word in lower for word in ("mini", "home", "audio", "speaker", "nest", "max")):
        kind = "speaker"
    else:
        kind = "device"
    brand = "Google" if lower.startswith(("google", "chromecast", "nest")) else "Google Cast"
    return _could(kind, _cast_name(h), brand)


# -- Apple Home, Matter and what is left out


def _homekit_flags(s: Sighting) -> int | None:
    try:
        return int(s.props.get("sf", ""), 10)
    except ValueError:
        return None


def _homekit_paired(s: Sighting) -> bool:
    """sf bit 0 set means "not paired": anything else (sf=0, or a problem flag alone) is in a home."""
    flags = _homekit_flags(s)
    return flags is not None and not flags & 0x01


def _homekit_entry(s: Sighting) -> ScanEntry:
    try:
        category = int(s.props.get("ci", ""), 10)
    except ValueError:
        category = 1
    kind = HOMEKIT_KINDS.get(category, "device")
    model = s.props.get("md", "")
    brand = next((b for prefix, b in HOMEKIT_BRANDS if model.lower().startswith(prefix.lower())), "HomeKit")
    name = clean_name(s.name)
    flags = _homekit_flags(s)
    if flags is not None and flags & 0x01:
        return ScanEntry(kind, name, brand, "homekit_free", detail="add it in the Home app for Siri")
    return ScanEntry(kind, name, brand, "apple_home")


def _matter_waiting(s: Sighting) -> ScanEntry:
    try:
        kind = MATTER_KINDS.get(int(s.props.get("dt", ""), 10), "device")
    except ValueError:
        kind = "device"
    return ScanEntry(kind, clean_name(s.props.get("dn", "")), "Matter", "matter", detail="waiting to be set up")


def _ignored(h: _Host) -> ScanEntry:
    device_type = h.ssdp("devicetype")
    if h.has("_ipp._tcp", "_ipps._tcp", "_printer._tcp") or "Printer" in device_type:
        reason = "printer"
    elif "InternetGatewayDevice" in device_type or "WFADevice" in device_type or h.has("_meshcop._udp"):
        reason = "network"
    elif "MediaServer" in device_type:
        reason = "media_server"
    else:
        reason = "other"
    return ScanEntry(IGNORED[reason][0], "", "", "ignored")


# --------------------------------------------------------------------------- mDNS


def _zeroconf() -> Any:
    """python-zeroconf, imported on first use (tests replace this)."""
    import zeroconf

    return zeroconf


def _mdns_probe(seconds: float, notes: list[str]) -> list[Sighting]:
    """Browses MDNS_TYPES and resolves each name as it appears, on a small pool of threads."""
    try:
        zc_mod = _zeroconf()
    except ImportError:
        notes.append("The mDNS search was skipped: its library is missing. /jarvis setup repairs it.")
        return []
    start = time.monotonic()
    try:
        zc = zc_mod.Zeroconf(
            interfaces=bravia._lan_addresses() or zc_mod.InterfaceChoice.All, ip_version=zc_mod.IPVersion.V4Only
        )
    except OSError as exc:
        log.warning("home scan: mDNS could not start: %s", type(exc).__name__)
        notes.append("The mDNS search couldn't start on this PC.")
        return []
    lock = threading.Lock()
    closed = threading.Event()
    seen: set[tuple[str, str]] = set()
    pending: list[tuple[str, str, Future[Sighting]]] = []
    pool = ThreadPoolExecutor(max_workers=MDNS_WORKERS, thread_name_prefix="home-scan-mdns")

    def resolve(service_type: str, name: str) -> Sighting:
        info = zc_mod.ServiceInfo(service_type, name)
        if not info.load_from_cache(zc):
            info = zc.get_service_info(service_type, name, timeout=RESOLVE_TIMEOUT_MS) or info
        return _mdns_sighting(service_type, name, info, zc_mod)

    def on_change(zeroconf: Any, service_type: str, name: str, state_change: Any) -> None:
        # Called on the browser's thread: it must not block, so the lookup goes to the pool.
        if state_change is zc_mod.ServiceStateChange.Removed:
            return
        with lock:
            if closed.is_set() or (service_type, name) in seen or len(seen) >= MAX_MDNS_NAMES:
                return
            seen.add((service_type, name))
            pending.append((service_type, name, pool.submit(resolve, service_type, name)))

    try:
        browser = zc_mod.ServiceBrowser(zc, list(MDNS_TYPES), handlers=[on_change])
        closed.wait(max(0.5, seconds - 1.0 - (time.monotonic() - start)))
        with contextlib.suppress(Exception):
            browser.cancel()
        with lock:
            closed.set()
            waiting = list(pending)
        wait_futures([f for _, _, f in waiting], timeout=max(0.0, start + seconds - time.monotonic()))
        found: list[Sighting] = []
        for service_type, name, future in waiting:
            sighting = None
            if future.done() and not future.cancelled() and future.exception() is None:
                sighting = future.result()
            # A name with no details still counts (a Thread device has no IPv4 address to resolve).
            found.append(sighting or _mdns_sighting(service_type, name, None, zc_mod))
        return found
    finally:
        closed.set()
        pool.shutdown(wait=False, cancel_futures=True)
        with contextlib.suppress(Exception):
            zc.close()


def _mdns_sighting(service_type: str, name: str, info: Any, zc_mod: Any) -> Sighting:
    short = service_type.removesuffix(".local.").rstrip(".")
    instance = name[: -len(service_type) - 1] if name.endswith("." + service_type) else name
    props: dict[str, str] = {}
    host, server = None, ""
    if info is not None:
        try:
            decoded = info.decoded_properties or {}
        except Exception:  # noqa: BLE001 - a malformed TXT record is no answer, not a failure
            decoded = {}
        for key, value in decoded.items():
            if isinstance(key, str):
                props[key.lower()] = value if isinstance(value, str) else ""
        with contextlib.suppress(Exception):
            addresses = info.parsed_addresses(zc_mod.IPVersion.V4Only)
            host = addresses[0] if addresses else None
        server = str(getattr(info, "server", "") or "")
    return Sighting("mdns", short, host, instance, props, server)


# --------------------------------------------------------------------------- SSDP


@dataclass(slots=True)
class _SsdpHost:
    targets: set[str] = field(default_factory=set)
    server: str = ""
    locations: list[str] = field(default_factory=list)


def _ssdp_probe(seconds: float, notes: list[str], *, target: tuple[str, int] | None = None) -> list[Sighting]:
    """M-SEARCH for SSDP_TARGETS (bravia's sockets: one per adapter), then each answering device's description."""
    start = time.monotonic()
    where = target or bravia.SSDP_ADDRESS
    head = 'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 2\r\n'
    messages = [f"{head}ST: {st}\r\n\r\n".encode("ascii") for st in SSDP_TARGETS]
    hosts: dict[str, _SsdpHost] = {}
    locations = 0
    with contextlib.ExitStack() as stack:
        sockets: list[socket.socket] = []
        for local in [None, *(bravia._lan_addresses() if target is None else [])]:
            sock = bravia._ssdp_socket(local)
            if sock is None:
                continue
            stack.enter_context(sock)
            try:
                for _ in range(2):  # UDP gets lost; ask twice
                    for message in messages:
                        sock.sendto(message, where)
            except OSError as exc:
                log.debug("home scan: SSDP search failed: %s", type(exc).__name__)
                continue
            sock.setblocking(False)
            sockets.append(sock)
        if not sockets:
            notes.append("The UPnP search couldn't send anything from this PC.")
            return []
        deadline = start + min(SSDP_S, max(0.5, seconds / 2))
        while sockets and (left := deadline - time.monotonic()) > 0:
            try:
                readable, _, _ = select.select(sockets, [], [], left)
            except (OSError, ValueError):
                break
            if not readable:
                break
            for sock in readable:
                try:
                    data, sender = sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError):
                    continue  # nothing after all, or (Windows) an earlier ICMP "port unreachable"
                except OSError:
                    sockets.remove(sock)
                    continue
                entry = hosts.setdefault(sender[0], _SsdpHost())
                st = bravia._ssdp_header(data, "st")
                if st:
                    entry.targets.add(st)
                entry.server = entry.server or (bravia._ssdp_header(data, "server") or "")
                location = bravia._ssdp_header(data, "location")
                # Only descriptions on the device that answered, over plain http: nothing else gets fetched.
                if (
                    location
                    and location not in entry.locations
                    and locations < MAX_DESCRIPTIONS
                    and _same_host_http(location, sender[0])
                ):
                    entry.locations.append(location)
                    locations += 1
    descriptions = _fetch_descriptions(
        [loc for entry in hosts.values() for loc in entry.locations], start + seconds - time.monotonic()
    )
    found = []
    for address, entry in hosts.items():
        props = {"st": " ".join(sorted(entry.targets)), "server": entry.server}
        for location in entry.locations:
            for key, value in (descriptions.get(location) or {}).items():
                if key in ("devicetype", "services", "scalar"):
                    props[key] = " ".join(filter(None, (props.get(key, ""), value)))
                elif value and not props.get(key):
                    props[key] = value
        found.append(Sighting("ssdp", "ssdp", address, clean_name(props.get("friendlyname", "")), props))
    return found


def _same_host_http(location: str, sender: str) -> bool:
    try:
        parts = urlsplit(location)
        return parts.scheme == "http" and parts.hostname == sender
    except ValueError:
        return False


def _fetch_descriptions(locations: list[str], budget: float) -> dict[str, dict[str, str]]:
    if not locations or budget <= 0:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(12, len(locations)), thread_name_prefix="home-scan-upnp")
    try:
        futures = {location: pool.submit(_describe, location) for location in locations}
        wait_futures(list(futures.values()), timeout=budget)
        return {
            location: future.result()
            for location, future in futures.items()
            if future.done() and future.exception() is None and future.result()
        }
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _describe(location: str) -> dict[str, str]:
    try:
        response = net.request("GET", location, timeout=DESCRIPTION_TIMEOUT_S)
    except net.HttpError:
        return {}
    if response.status != 200:
        return {}
    return parse_description(response.text)


def parse_description(text: str) -> dict[str, str]:
    """The fields of a UPnP description the classifier reads (the root device's name, maker and model)."""
    return {
        "friendlyname": _xml_first(text, "friendlyName"),
        "manufacturer": _xml_first(text, "manufacturer"),
        "modelname": _xml_first(text, "modelName"),
        "devicetype": " ".join(_xml_all(text, "deviceType")),
        "services": " ".join(_xml_all(text, "serviceType")),
        "scalar": " ".join(_xml_all(text, "X_ScalarWebAPI_ServiceType")),
    }


def _xml_all(text: str, tag: str) -> list[str]:
    return [v for v in re.findall(rf"<(?:[\w-]+:)?{tag}>\s*([^<]*?)\s*</", text) if v]


def _xml_first(text: str, tag: str) -> str:
    values = _xml_all(text, tag)
    return html.unescape(values[0]).strip() if values else ""


# --------------------------------------------------------------------------- Tuya


def _tuya_probe(seconds: float, notes: list[str]) -> list[Sighting]:
    """Listens for Tuya broadcasts (UDP 6666, 6667, 7000) with tinytuya's scanner; poll=False never connects."""
    try:
        from . import tuya

        scanner = tuya._scanner()
    except ImportError:
        notes.append("The Tuya search was skipped: its library is missing. /jarvis setup repairs it.")
        return []
    try:
        # A placeholder entry: an empty list would make the scanner load ./devices.json.
        result = scanner.devices(
            verbose=False,
            scantime=max(1.0, seconds - 0.5),
            color=False,
            poll=False,
            byID=True,
            tuyadevices=[{"id": "-", "name": "", "key": ""}],
        )
    except OSError as exc:
        # Windows does not share UDP 6666/6667/7000: the setup window, or Jarvis's own Tuya search, has them.
        log.warning("home scan: Tuya could not listen: %s", type(exc).__name__)
        notes.append("I couldn't listen for Tuya devices: another program, or Jarvis's own Tuya search, had the ports.")
        return []
    found = []
    for gw_id, info in (result or {}).items():
        if isinstance(gw_id, str) and gw_id and isinstance(info, dict):
            address = info.get("ip") if isinstance(info.get("ip"), str) else None
            found.append(Sighting("tuya", "tuya", address or None, props={"id": gw_id}))
    return found


# --------------------------------------------------------------------------- Kasa, LIFX, WiZ, Yeelight, Govee

KASA_PORT = 9999
LIFX_PORT = 56700
WIZ_PORT = 38899
YEELIGHT_ADDRESS = ("239.255.255.250", 1982)
GOVEE_ADDRESS = ("239.255.255.250", 4001)
GOVEE_REPLY_PORT = 4002
_LIFX_SOURCE = 0x4A525653  # any non-zero source makes the bulbs answer us directly


def kasa_crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    """TP-Link's XOR "autokey" (key 171) for the legacy Kasa protocol on UDP 9999."""
    key, out = 171, bytearray()
    for byte in data:
        value = byte ^ key
        key = byte if decrypt else value
        out.append(value)
    return bytes(out)


KASA_QUERY = kasa_crypt(b'{"system":{"get_sysinfo":{}}}')
# LIFX GetService (type 2): a 36-byte header, tagged and addressable, protocol 1024.
LIFX_GET_SERVICE = struct.pack("<HHI8s6sBB8sHH", 36, 0x3400, _LIFX_SOURCE, b"\0" * 8, b"\0" * 6, 0, 0, b"\0" * 8, 2, 0)
# register:false asks the bulb to say who it is without adding this PC as its app.
WIZ_QUERY = (
    b'{"method":"registration","params":{"phoneMac":"AAAAAAAAAAAA","register":false,"phoneIp":"1.2.3.4","id":"1"}}'
)
YEELIGHT_QUERY = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1982\r\nMAN: "ssdp:discover"\r\nST: wifi_bulb\r\n'
GOVEE_QUERY = b'{"msg":{"cmd":"scan","data":{"account_topic":"reserve"}}}'
BROADCAST_QUERIES = ((KASA_QUERY, KASA_PORT), (LIFX_GET_SERVICE, LIFX_PORT), (WIZ_QUERY, WIZ_PORT))


def _udp_probe(seconds: float, notes: list[str]) -> list[Sighting]:
    """The brands' own discovery messages, from each adapter, then the answers for a few seconds."""
    start = time.monotonic()
    with contextlib.ExitStack() as stack:
        sockets: list[socket.socket] = []
        for local in [None, *bravia._lan_addresses()]:
            sock = bravia._ssdp_socket(local)
            if sock is None:
                continue
            stack.enter_context(sock)
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sent = False
            for _ in range(2):
                for target in net.broadcast_targets(local):
                    for message, port in BROADCAST_QUERIES:
                        with contextlib.suppress(OSError):
                            sock.sendto(message, (target, port))
                            sent = True
                for message, address in ((YEELIGHT_QUERY, YEELIGHT_ADDRESS), (GOVEE_QUERY, GOVEE_ADDRESS)):
                    with contextlib.suppress(OSError):
                        sock.sendto(message, address)
                        sent = True
            if sent:
                sock.setblocking(False)
                sockets.append(sock)
        # Govee answers on a port of its own, which needs a socket bound to it.
        govee = None
        try:
            govee = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP))
            govee.bind(("", GOVEE_REPLY_PORT))
            govee.setblocking(False)
            sockets.append(govee)
        except OSError:
            notes.append(f"Govee lights couldn't be checked: another program is using UDP port {GOVEE_REPLY_PORT}.")
        if not any(s is not govee for s in sockets):
            notes.append("The search for Kasa, LIFX, WiZ, Yeelight and Govee couldn't send anything from this PC.")
        found: dict[tuple[str, str], Sighting] = {}
        deadline = start + min(UDP_S, seconds)
        while sockets and (left := deadline - time.monotonic()) > 0:
            try:
                readable, _, _ = select.select(sockets, [], [], left)
            except (OSError, ValueError):
                break
            if not readable:
                break
            for sock in readable:
                try:
                    data, sender = sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError):
                    continue
                except OSError:
                    sockets.remove(sock)
                    continue
                sighting = parse_udp_reply(data, sender[0], govee=sock is govee)
                if sighting is not None:
                    found.setdefault((sender[0], sighting.service), sighting)
    return list(found.values())


def parse_udp_reply(data: bytes, sender: str, *, govee: bool = False) -> Sighting | None:
    """Which brand's discovery reply ``data`` is, or None for anything else (our own messages included)."""
    if govee:
        reply = _json(data)
        message = reply.get("msg") if isinstance(reply, dict) else None
        data_part = message.get("data") if isinstance(message, dict) else None
        # A reply names its model (sku); the scan request itself does not.
        if isinstance(data_part, dict) and data_part.get("sku") and message.get("cmd") == "scan":
            return Sighting("udp", "govee", sender)
        return None
    if data.startswith(b"HTTP/1.1 200") and b"yeelight://" in data.lower():
        return Sighting("udp", "yeelight", sender, props={"model": bravia._ssdp_header(data, "model") or ""})
    if len(data) >= 36:
        size, protocol = struct.unpack_from("<HH", data, 0)
        (kind,) = struct.unpack_from("<H", data, 32)
        if size == len(data) and protocol & 0x0FFF == 1024 and kind == 3:  # StateService
            return Sighting("udp", "lifx", sender)
    reply = _json(data)
    if isinstance(reply, dict):
        if reply.get("method") == "registration" and isinstance(reply.get("result"), dict):
            return Sighting("udp", "wiz", sender)
        return None
    reply = _json(kasa_crypt(data, decrypt=True))
    info = reply.get("system", {}).get("get_sysinfo") if isinstance(reply, dict) else None
    if isinstance(info, dict) and info:
        device_type = str(info.get("mic_type") or info.get("type") or "").upper()
        kind = "light" if "BULB" in device_type or "LIGHT" in device_type else "plug"
        alias = info.get("alias") if isinstance(info.get("alias"), str) else ""
        return Sighting("udp", "kasa", sender, props={"alias": alias, "kind": kind})
    return None


def _json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
