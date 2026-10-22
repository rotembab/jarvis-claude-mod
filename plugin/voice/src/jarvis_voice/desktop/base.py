"""What every desktop backend shares: the action names, the backend protocol, the
outcomes, and the pure helpers (target sorting, the link allowlist, name
matching, the Start-menu list, the PNG writer, volume arithmetic).

Nothing here touches the OS except ``classify_target``, which asks the file
system whether a path is a folder; everything else is a plain function tested
on every OS.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import os
import re
import struct
import threading
import time
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Protocol

import numpy as np

log = logging.getLogger(__name__)

Action = Literal["open", "focus", "media", "volume", "screenshot", "lock", "clipboard_read", "clipboard_write"]
MediaKey = Literal["play_pause", "next", "previous", "stop"]
VolumeChange = Literal["up", "down", "mute", "unmute"]
Result = Literal["done", "failed", "refused", "unsupported"]

ACTIONS: tuple[Action, ...] = (
    "open",
    "focus",
    "media",
    "volume",
    "screenshot",
    "lock",
    "clipboard_read",
    "clipboard_write",
)
MEDIA_KEYS: tuple[MediaKey, ...] = ("play_pause", "next", "previous", "stop")
VOLUME_CHANGES: tuple[VolumeChange, ...] = ("up", "down", "mute", "unmute")

#: The only link schemes ``open`` hands to the shell. file:, shell:, search-ms:, ms-msdt: and the rest are refused.
ALLOWED_SCHEMES = ("http", "https", "spotify", "ms-settings", "mailto")
#: Points per "up" or "down".
VOLUME_STEP = 10
#: Characters of clipboard text handed back.
CLIPBOARD_READ_LIMIT = 4000
#: The schema's maxLength on DesktopCommand.text.
CLIPBOARD_WRITE_LIMIT = 20000
#: difflib ratio a misheard name needs to count as a match.
CLOSE_MATCH_CUTOFF = 0.75
#: Names listed when a request matches several.
TIE_LIST_LIMIT = 5

MEDIA_TEXT: dict[MediaKey, str] = {
    "play_pause": "Pressed the play/pause media key.",
    "next": "Pressed the next-track media key.",
    "previous": "Pressed the previous-track media key.",
    "stop": "Pressed the stop media key.",
}

FILES_REFUSED = (
    "Jarvis does not open files, because opening one runs whatever program it belongs to; open its folder instead."
)
NETWORK_REFUSED = "Jarvis does not open network paths."

# A scheme has two characters at least, so "C:\..." stays a path. A space right after the
# colon means a name ("Halo: Infinite"), never a link.
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):(?!\s)")
_UNSAFE_URI_CHARS = re.compile(r'[\s"<>\x00-\x1f\x7f]')
_WORD = re.compile(r"[^\W_]+")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_UNINSTALL = re.compile(r"\buninstall(er)?\b")


class Failed(RuntimeError):
    """The action did not happen; the message is the plain-words answer."""


class Refused(RuntimeError):
    """Not allowed: by Jarvis's own limits, or because Windows said no. The message is the answer."""


class Unsupported(RuntimeError):
    """This computer cannot do it (no backend, no audio device). The message is the answer."""


@dataclass(frozen=True)
class StartApp:
    """A launchable Start-menu entry: an AppUserModelID (Get-StartApps) or, failing that, its shortcut file."""

    name: str
    app_id: str = ""
    link: str = ""

    @property
    def launch_target(self) -> str:
        """What the shell's open verb gets: Store apps and desktop apps alike start from shell:AppsFolder."""
        return f"shell:AppsFolder\\{self.app_id}" if self.app_id else self.link


@dataclass(frozen=True)
class TopWindow:
    handle: int
    title: str
    #: The image file name, e.g. "Spotify.exe" ("" when Windows would not say).
    exe: str = ""


@dataclass(frozen=True)
class Shot:
    path: Path
    width: int
    height: int


class DesktopBackend(Protocol):
    """One OS's desktop. Every method runs on the service's single worker thread.

    The service has already checked the arguments and the open target's kind.
    Methods return the plain-words answer and raise Failed, Refused or
    Unsupported (or anything else, which the service reports as a failure).
    """

    def prepare_thread(self) -> None:
        """Once, on the worker thread, before its first action (COM, for example)."""
        ...

    def warm(self) -> None:
        """Start loading slow lookups (the Start-menu app list) in the background. Must not block."""
        ...

    def open_uri(self, uri: str) -> str: ...

    def open_folder(self, path: Path) -> str: ...

    def open_app(self, name: str, deadline: float) -> str:
        """``deadline`` is a time.monotonic() value: give up on slow lookups before it."""
        ...

    def focus(self, name: str) -> str: ...

    def media(self, key: MediaKey) -> str: ...

    def volume(self, level: int | None, change: VolumeChange | None) -> str:
        """Neither argument: report the volume."""
        ...

    def screenshot(self) -> Shot: ...

    def lock(self) -> str: ...

    def clipboard_read(self) -> str | None:
        """The clipboard's text, or None when it holds none."""
        ...

    def clipboard_write(self, text: str) -> str: ...


# --------------------------------------------------------------------------- open targets


def allowed_uri(uri: str) -> bool:
    """True for a well-formed link with a scheme on the allowlist (http and https need a host)."""
    match = _SCHEME.match(uri)
    if match is None or match.group(1).lower() not in ALLOWED_SCHEMES:
        return False
    if _UNSAFE_URI_CHARS.search(uri):  # a real link percent-encodes these; a quote could split the handler's command
        return False
    rest = uri[match.end() :]
    if match.group(1).lower() in ("http", "https"):
        return rest.startswith("//") and len(rest) > 2 and not rest.startswith("///")
    return bool(rest)


def _is_network_path(path: str) -> bool:
    # \\server\share, \\?\..., \\.\device and //server: even asking whether one is a folder
    # makes Windows connect to the server (and offer it the user's credentials).
    return path.startswith(("\\\\", "//", "\\/", "/\\"))


def _is_local_full_path(path: str) -> bool:
    """A full path on a drive letter (C:\\ or C:/); off Windows, where only the tests run, one rooted at a single /.

    An allowlist: any other rooted form (\\??\\UNC\\..., \\Device\\..., a share) may reach a server.
    """
    if _DRIVE_PATH.match(path):
        return True
    return os.name != "nt" and path.startswith("/") and not path.startswith(("//", "/\\"))


def _looks_like_path(target: str) -> bool:
    return (
        "\\" in target
        or "/" in target
        or target.startswith(("~", "%"))
        or (len(target) >= 2 and target[1] == ":" and target[0].isalpha())
    )


def classify_target(target: str) -> tuple[Literal["uri", "folder", "app"], str]:
    """Sorts an ``open`` target: an allowed link, an existing folder, or an app name.

    Raises Refused for a link outside the allowlist, a network path or a file,
    and Failed for a path that is not an existing folder. Only folder paths are
    checked on disk; an app name is matched against the Start menu, never run.
    """
    text = target.strip()
    scheme = _SCHEME.match(text)
    if scheme is not None:
        if not allowed_uri(text):
            allowed = ", ".join(ALLOWED_SCHEMES[:-1]) + " and " + ALLOWED_SCHEMES[-1]
            raise Refused(f"Only {allowed} links can be opened, and only well-formed ones; not {scheme.group(1)}:.")
        return "uri", text
    if not _looks_like_path(text):
        return "app", text
    if _is_network_path(text):
        raise Refused(NETWORK_REFUSED)
    expanded = os.path.expandvars(os.path.expanduser(text))
    if not _is_local_full_path(expanded):
        # Decided before the disk is asked anything.
        if expanded.startswith(("\\", "/")):
            raise Refused(NETWORK_REFUSED)
        raise Failed(f"{text!r} is not a full folder path.")
    if os.path.isdir(expanded):
        return "folder", expanded
    if os.path.exists(expanded):
        raise Refused(FILES_REFUSED)
    raise Failed(f"There is no folder at {expanded}.")


# --------------------------------------------------------------------------- matching names


def normalize_name(text: str) -> str:
    """Lower case, letters and digits only, single spaces: "Spotify  Premium!" -> "spotify premium"."""
    return " ".join(_WORD.findall(text.casefold()))


def best_matches(query: str, names: Sequence[str]) -> list[int]:
    """Indices of ``names`` in the best tier that has any: exact, every word, then close spelling.

    "Every word" holds each name in which every query word starts a word,
    wherever it stands: "chrome" is as much Google Chrome as Chrome Remote Desktop.
    """
    wanted = normalize_name(query)
    if not wanted:
        return []
    keys = [normalize_name(name) for name in names]
    exact = [i for i, key in enumerate(keys) if key == wanted]
    if exact:
        return exact
    query_words = wanted.split()
    every_word = [
        i for i, key in enumerate(keys) if all(any(w.startswith(q) for w in key.split()) for q in query_words)
    ]
    if every_word:
        return every_word
    scores = [(difflib.SequenceMatcher(None, wanted, key).ratio(), i) for i, key in enumerate(keys) if key]
    close = [(score, i) for score, i in scores if score >= CLOSE_MATCH_CUTOFF]
    if not close:
        return []
    top = max(score for score, _ in close)
    return [i for score, i in close if score == top]


def _listing(names: Sequence[str]) -> str:
    unique = list(dict.fromkeys(names))
    shown = ", ".join(unique[:TIE_LIST_LIMIT])
    more = len(unique) - TIE_LIST_LIMIT
    return f"{shown} and {more} more" if more > 0 else shown


def pick_app(name: str, apps: Sequence[StartApp]) -> StartApp:
    """The one Start-menu app ``name`` means. Uninstallers are never picked. Raises Failed on no match or a tie.

    The name as written wins first: "Notepad++" is not "Notepad", though both
    normalize to "notepad". Entries that share one name are one app.
    """
    usable = [app for app in apps if not _UNINSTALL.search(normalize_name(app.name))]
    as_written = [app for app in usable if app.name.strip().casefold() == name.strip().casefold()]
    if as_written:
        return as_written[0]
    hits = [usable[i] for i in best_matches(name, [app.name for app in usable])]
    if not hits:
        raise Failed(f"No Start-menu app matches {name.strip()!r}.")
    if len({app.name.strip().casefold() for app in hits}) > 1:
        raise Failed(f"{name.strip()!r} matches several apps: {_listing([a.name for a in hits])}. Say which one.")
    return hits[0]


def _app_key(window: TopWindow) -> str:
    exe = window.exe.casefold()
    # Store apps all run in ApplicationFrameHost.exe: their titles tell them apart.
    return f"{exe}|{window.title.casefold()}" if exe == "applicationframehost.exe" else exe


def pick_window(name: str, windows: Sequence[TopWindow]) -> TopWindow:
    """The window ``name`` means: by program name first, then by title. ``windows`` is topmost first.

    Several windows of one program are no tie: the topmost wins. Raises Failed
    when nothing matches (the app is not open) or two programs do.
    """
    wanted = normalize_name(name)
    by_exe = [w for w in windows if w.exe and normalize_name(PureWindowsPath(w.exe).stem) == wanted]
    if by_exe:
        return by_exe[0]
    hits = [windows[i] for i in best_matches(name, [w.title for w in windows])]
    if not hits:
        raise Failed(f"{name.strip()} is not open; use open.")
    if len({_app_key(w) for w in hits}) > 1:
        raise Failed(f"{name.strip()!r} matches several windows: {_listing([w.title for w in hits])}. Say which one.")
    return hits[0]


def window_label(window: TopWindow) -> str:
    title = window.title if len(window.title) <= 80 else window.title[:77] + "..."
    return f'"{title}"'


# --------------------------------------------------------------------------- the Start-menu list


def parse_start_apps(text: str) -> list[StartApp]:
    """Get-StartApps | ConvertTo-Json: a list, a single object for one app, or nothing at all."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        return []
    data = json.loads(text)
    rows = data if isinstance(data, list) else [data]
    apps: list[StartApp] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, app_id = row.get("Name"), row.get("AppID")
        if isinstance(name, str) and isinstance(app_id, str) and name.strip() and app_id.strip():
            apps.append(StartApp(name.strip(), app_id=app_id.strip()))
    return apps


def start_menu_links(folders: Sequence[Path]) -> list[StartApp]:
    """The fallback list: every .lnk under the Start Menu\\Programs folders, named after the file."""
    apps: list[StartApp] = []
    seen: set[str] = set()
    for folder in folders:
        try:
            links = sorted(folder.rglob("*.lnk"))
        except OSError:
            continue
        for link in links:
            key = normalize_name(link.stem)
            if key and key not in seen:
                seen.add(key)
                apps.append(StartApp(link.stem, link=str(link)))
    return apps


class AppList:
    """The launchable apps, loaded on a background thread and kept for ``ttl`` seconds.

    Loading takes PowerShell a second or two, so it starts when the helper
    does and an ``open`` waits for it only as long as its own time limit allows.
    """

    def __init__(
        self, loader: Callable[[], list[StartApp]], *, ttl: float = 600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._loader = loader
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._apps: list[StartApp] | None = None
        self._loaded_at = -math.inf
        self._loading: threading.Event | None = None

    def warm(self) -> None:
        with self._lock:
            self._start_locked()

    def age(self) -> float:
        with self._lock:
            return self._clock() - self._loaded_at

    def get(self, wait: float, max_age: float | None = None) -> list[StartApp] | None:
        """The list when it is younger than ``max_age`` (default: the ttl); otherwise a reload, awaited up to
        ``wait`` seconds. A reload that takes longer keeps going and the older list (None if none) is returned."""
        limit = self._ttl if max_age is None else max_age
        with self._lock:
            if self._apps is not None and self._clock() - self._loaded_at <= limit:
                return self._apps
            loading = self._start_locked()
        loading.wait(max(0.0, wait))
        with self._lock:
            return self._apps

    def _start_locked(self) -> threading.Event:
        if self._loading is None:
            self._loading = threading.Event()
            threading.Thread(target=self._load, args=(self._loading,), name="jarvis-desktop-apps", daemon=True).start()
        return self._loading

    def _load(self, done: threading.Event) -> None:
        try:
            apps: list[StartApp] | None = self._loader()
        except Exception:  # an open says the list is missing; the log has why
            log.exception("could not list the Start-menu apps")
            apps = None
        with self._lock:
            if apps is not None:
                self._apps, self._loaded_at = apps, self._clock()
            self._loading = None
        done.set()


# --------------------------------------------------------------------------- volume


def clamp_level(value: float) -> int:
    """A volume in whole percent, 0 to 100."""
    return max(0, min(100, round(value)))


def plan_volume(current: int, muted: bool, level: int | None, change: VolumeChange | None) -> tuple[int, bool]:
    """The (level, muted) a request asks for. Setting a level above 0 or turning it up unmutes, as Windows' own
    slider and keys do."""
    if level is not None:
        level = clamp_level(level)
        return level, muted and level == 0
    if change == "up":
        return clamp_level(current + VOLUME_STEP), False
    if change == "down":
        return clamp_level(current - VOLUME_STEP), muted
    if change == "mute":
        return current, True
    if change == "unmute":
        return current, False
    return current, muted


def volume_text(level: int, muted: bool, change: VolumeChange | None) -> str:
    if change == "mute":
        return f"Muted; the volume stays at {level}%."
    if change == "unmute":
        return f"Unmuted; the volume is {level}%."
    return f"Volume is {level}%{', muted' if muted else ''}."


# --------------------------------------------------------------------------- PNG


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(rgb: Any, *, level: int = 3) -> bytes:
    """An 8-bit RGB PNG of an (height, width, 3) uint8 array, with zlib and struct only.

    Every row uses the Sub filter (each byte minus the one a pixel to its
    left), which shrinks the flat areas of a screenshot a lot for one numpy
    subtraction. ``level`` is zlib's: a 4K screen must stay well inside the
    action's time limit.
    """
    pixels = np.ascontiguousarray(rgb, dtype=np.uint8)
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.shape[0] < 1 or pixels.shape[1] < 1:
        raise ValueError(f"expected a (height, width, 3) array, got {pixels.shape}")
    height, width = pixels.shape[:2]
    filtered = np.empty((height, width * 3 + 1), dtype=np.uint8)
    filtered[:, 0] = 1  # filter type Sub
    body = filtered[:, 1:].reshape(height, width, 3)
    body[:, 0] = pixels[:, 0]
    np.subtract(pixels[:, 1:], pixels[:, :-1], out=body[:, 1:])  # uint8 arithmetic wraps, as PNG wants
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8 bits, truecolour, no interlace
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(filtered.tobytes(), level)),
            _chunk(b"IEND", b""),
        )
    )


def screenshot_name(stamp: time.struct_time) -> str:
    return time.strftime("Jarvis %Y-%m-%d %H.%M.%S.png", stamp)


def write_new_file(folder: Path, name: str, data: bytes) -> Path:
    """Writes ``data`` to ``folder/name``, or to "name (2)" and so on: an existing file is never replaced."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(name)
    for n in range(1, 100):
        path = folder / (name if n == 1 else f"{stem} ({n}){suffix}")
        try:
            with path.open("xb") as out:
                out.write(data)
        except FileExistsError:
            continue
        return path
    raise Failed(f"Could not find a free file name for {name} in {folder}.")
