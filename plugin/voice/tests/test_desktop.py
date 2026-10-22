"""Desktop actions: the pure helpers, the service over the fake desktop, the routing and the capabilities.

The fake records actions instead of performing them: nothing here presses a
key, locks the screen or touches the clipboard.
"""

from __future__ import annotations

import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis_voice.desktop import create_service, desktop_capabilities, route_desktop
from jarvis_voice.desktop.base import (
    ACTIONS,
    CLIPBOARD_READ_LIMIT,
    AppList,
    Failed,
    Refused,
    StartApp,
    TopWindow,
    Unsupported,
    allowed_uri,
    best_matches,
    clamp_level,
    classify_target,
    encode_png,
    parse_start_apps,
    pick_app,
    pick_window,
    plan_volume,
    start_menu_links,
    volume_text,
    write_new_file,
)
from jarvis_voice.desktop.fake import FakeDesktop
from jarvis_voice.desktop.service import DesktopService

APPS = [
    StartApp("Spotify", app_id="SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"),
    StartApp("Visual Studio Code", app_id="Microsoft.VisualStudioCode"),
    StartApp("Visual Studio 2022", app_id="VisualStudio.17"),
    StartApp("Microsoft Edge", app_id="MSEdge"),
    StartApp("Microsoft Store", app_id="Microsoft.WindowsStore_8wekyb3d8bbwe!App"),
    StartApp("Notepad", app_id="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    StartApp("Uninstall Zoom", link=r"C:\Start Menu\Zoom\Uninstall Zoom.lnk"),
]


def result(answer: dict[str, Any]) -> tuple[str, str]:
    assert answer["ok"] is True
    return answer["desktop"]["result"], answer["desktop"]["text"]


@pytest.fixture
def fake() -> FakeDesktop:
    return FakeDesktop()


@pytest.fixture
def service(fake: FakeDesktop) -> Any:
    svc = DesktopService(fake, action_timeout=2.0)
    yield svc
    svc.close()


# --------------------------------------------------------------------------- capabilities and routing


def test_capabilities_are_per_action_on_windows_and_in_fake_mode_only(monkeypatch: pytest.MonkeyPatch) -> None:
    per_action = [f"desktop.{a}" for a in ACTIONS]
    assert "desktop.clipboard_read" in per_action and "desktop.clipboard_write" in per_action
    assert desktop_capabilities(fake=True) == [*per_action, "fake.desktop"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop_capabilities(fake=False) == per_action
    for other in ("linux", "darwin"):
        monkeypatch.setattr(sys, "platform", other)
        assert desktop_capabilities(fake=False) == []


def test_off_windows_every_action_is_unsupported(monkeypatch: pytest.MonkeyPatch, reference_validator: Any) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    svc = create_service(fake=False)
    assert svc.backend is None
    for action in ACTIONS:
        answer = svc.handle({"action": action})
        reference_validator(answer, "CommandResponse")
        assert result(answer) == ("unsupported", "Desktop actions work on Windows only for now.")


def test_a_broken_windows_backend_leaves_the_helper_running(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis_voice.desktop import windows

    def broken() -> None:
        raise OSError("no user32 today")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows, "WindowsDesktop", broken)
    svc = create_service(fake=False)
    assert svc.backend is None
    assert result(svc.handle({"action": "lock"}))[0] == "unsupported"


def test_fake_mode_uses_the_recording_desktop() -> None:
    svc = create_service(fake=True)
    assert isinstance(svc.backend, FakeDesktop) and svc.backend.warmed
    assert result(svc.handle({"action": "lock"})) == ("done", "Locked the screen.")
    assert svc.backend.calls == [("lock",)]
    svc.close()


def test_route_sends_only_desktop_to_the_service(fake: FakeDesktop, service: DesktopService) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def daemon(name: str, body: dict[str, Any]) -> dict[str, Any]:
        seen.append((name, body))
        return {"ok": True}

    route = route_desktop(daemon, service)
    assert route("heartbeat", {}) == {"ok": True}
    assert route("listen", {"action": "start"}) == {"ok": True}
    assert result(route("desktop", {"action": "media", "key": "next"}))[0] == "done"
    assert seen == [("heartbeat", {}), ("listen", {"action": "start"})]
    assert fake.calls == [("media", "next")]


# --------------------------------------------------------------------------- open targets


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com",
        "http://localhost:8080/x?y=1",
        "HTTPS://Example.com/a%20b",
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        "ms-settings:display",
        "mailto:someone@example.com?subject=Hi%20there",
    ],
)
def test_allowed_links(uri: str) -> None:
    assert allowed_uri(uri)
    assert classify_target(f"  {uri} ") == ("uri", uri)


@pytest.mark.parametrize(
    "uri",
    [
        "file:///C:/Windows/System32/calc.exe",
        "shell:startup",
        "search-ms:query=x&crumb=location:\\\\evil\\share",
        "ms-msdt:/id PCWDiagnostic",
        "javascript:alert(1)",
        "steam://run/123",
        "vbscript:x",
        "https:example.com",  # no host
        "https:///path",
        "http://",
        "https://example.com/a b",  # a raw space: not a real link
        'https://example.com/"--x',  # a quote could split the handler's command line
        "https://example.com/\x07",
        "spotify:",
    ],
)
def test_other_links_are_refused(uri: str) -> None:
    assert not allowed_uri(uri)
    with pytest.raises(Refused, match="links can be opened"):
        classify_target(uri)


def test_names_with_a_colon_are_app_names_not_links() -> None:
    assert classify_target("Halo: Infinite") == ("app", "Halo: Infinite")
    assert classify_target(" Spotify ") == ("app", "Spotify")
    assert classify_target("Visual Studio Code") == ("app", "Visual Studio Code")


def test_an_existing_folder_opens_and_a_file_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    file = folder / "setup.exe"
    file.write_bytes(b"MZ")
    assert classify_target(str(folder)) == ("folder", str(folder))
    with pytest.raises(Refused, match="does not open files"):
        classify_target(str(file))
    with pytest.raises(Failed, match="There is no folder at"):
        classify_target(str(tmp_path / "missing"))
    with pytest.raises(Failed, match="not a full folder path"):
        classify_target("Downloads/stuff")


def test_home_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "Music").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    kind, value = classify_target("~/Music")
    assert kind == "folder" and Path(value) == tmp_path / "Music"


@pytest.mark.parametrize(
    "path",
    [
        "\\\\evil.example\\share",
        "//evil.example/share",
        "\\\\?\\C:\\Windows",
        "\\\\.\\PhysicalDrive0",
        # NT object paths: \??\UNC\ is a share, and only drive-letter paths are let through at all.
        "\\??\\UNC\\evil.example\\share",
        "\\??\\C:\\Windows",
        "\\Device\\Mup\\evil.example\\share",
    ],
)
def test_network_and_device_paths_are_refused_before_the_disk_is_asked(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_disk(*_args: Any) -> bool:
        raise AssertionError("a network path must not reach the file system")

    monkeypatch.setattr("os.path.isdir", no_disk)
    monkeypatch.setattr("os.path.exists", no_disk)
    with pytest.raises(Refused, match="network paths"):
        classify_target(path)


def test_a_home_that_expands_to_a_share_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "//evil.example/share")
    monkeypatch.setenv("USERPROFILE", "//evil.example/share")
    monkeypatch.setattr("os.path.isdir", lambda _p: pytest.fail("asked the disk"))
    with pytest.raises(Refused, match="network paths"):
        classify_target("~/Documents")


# --------------------------------------------------------------------------- matching names


def test_matching_tiers_exact_words_then_spelling() -> None:
    names = ["Spotify", "Spotify Helper", "Visual Studio Code", "Notepad"]
    assert best_matches("spotify", names) == [0]  # exact beats every word
    assert best_matches("spot", names) == [0, 1]
    assert best_matches("studio code", names) == [2]
    assert best_matches("vis cod", names) == [2]  # every word starts a word
    assert best_matches("notpad", names) == [3]  # misheard
    assert best_matches("calculator", names) == []
    assert best_matches("  !! ", names) == []


def test_pick_app() -> None:
    assert pick_app("Spotify", APPS).name == "Spotify"
    assert pick_app("spotfy", APPS).name == "Spotify"
    assert pick_app("studio code", APPS).name == "Visual Studio Code"
    assert pick_app("note", APPS).name == "Notepad"
    with pytest.raises(Failed, match=r"matches several apps: Visual Studio Code, Visual Studio 2022\. Say which one"):
        pick_app("Visual Studio", APPS)
    with pytest.raises(Failed, match="No Start-menu app matches 'Calculator'"):
        pick_app("Calculator", APPS)


def test_a_word_anywhere_in_the_name_ties_with_one_at_its_start() -> None:
    apps = [StartApp("Google Chrome", app_id="Chrome"), StartApp("Chrome Remote Desktop", app_id="crd")]
    assert best_matches("chrome", [app.name for app in apps]) == [0, 1]
    with pytest.raises(Failed, match=r"matches several apps: Google Chrome, Chrome Remote Desktop\. Say which one"):
        pick_app("chrome", apps)
    assert pick_app("google chrome", apps).app_id == "Chrome"
    assert pick_app("chrome remote", apps).app_id == "crd"


def test_names_that_differ_only_in_punctuation_are_different_apps() -> None:
    apps = [StartApp("Notepad", app_id="Notepad!App"), StartApp("Notepad++", link=r"C:\Start\Notepad++.lnk")]
    assert pick_app("Notepad++", apps).name == "Notepad++"
    assert pick_app("notepad", apps).name == "Notepad"
    with pytest.raises(Failed, match=r"matches several apps: Notepad, Notepad\+\+\. Say which one"):
        pick_app("note", apps)


def test_two_entries_with_one_name_are_no_tie() -> None:
    twice = [StartApp("Spotify", app_id="store"), StartApp("Spotify", link=r"C:\x\Spotify.lnk")]
    assert pick_app("spotify", twice).app_id == "store"


def test_uninstallers_are_never_opened() -> None:
    with pytest.raises(Failed, match="No Start-menu app"):
        pick_app("Zoom", APPS)
    with pytest.raises(Failed, match="No Start-menu app"):
        pick_app("Uninstall Zoom", APPS)


def test_a_tie_lists_five_names_at_most() -> None:
    apps = [StartApp(f"Tool {n}", app_id=str(n)) for n in range(8)]
    with pytest.raises(Failed, match=r"Tool 0, Tool 1, Tool 2, Tool 3, Tool 4 and 3 more"):
        pick_app("tool", apps)


def test_launch_targets() -> None:
    assert APPS[0].launch_target == "shell:AppsFolder\\SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
    assert StartApp("X", link=r"C:\Start\X.lnk").launch_target == r"C:\Start\X.lnk"


WINDOWS = [
    TopWindow(1, "README.md - jarvis - Visual Studio Code", "Code.exe"),
    TopWindow(2, "Spotify Premium", "Spotify.exe"),
    TopWindow(3, "Inbox - Outlook", "olk.exe"),
    TopWindow(4, "Docs - Visual Studio Code", "Code.exe"),
    TopWindow(5, "Calculator", "ApplicationFrameHost.exe"),
    TopWindow(6, "Settings", "ApplicationFrameHost.exe"),
]


def test_pick_window_by_program_then_title_topmost_first() -> None:
    assert pick_window("spotify", WINDOWS).handle == 2
    assert pick_window("code", WINDOWS).handle == 1  # the program, its topmost window
    assert pick_window("visual studio code", WINDOWS).handle == 1  # two windows of one program: no tie
    assert pick_window("outlook", WINDOWS).handle == 3
    assert pick_window("calculator", WINDOWS).handle == 5
    with pytest.raises(Failed, match="Teams is not open; use open"):
        pick_window("Teams", WINDOWS)


def test_two_programs_matching_a_title_are_a_tie() -> None:
    windows = [TopWindow(1, "Notes - Obsidian", "Obsidian.exe"), TopWindow(2, "Notes - OneNote", "ONENOTE.EXE")]
    with pytest.raises(Failed, match="matches several windows"):
        pick_window("notes", windows)
    store = [
        TopWindow(5, "Photos", "ApplicationFrameHost.exe"),
        TopWindow(6, "Photos Legacy", "ApplicationFrameHost.exe"),
    ]
    with pytest.raises(Failed, match="matches several windows"):
        pick_window("photo", store)  # Store apps share ApplicationFrameHost.exe: their titles tell them apart


# --------------------------------------------------------------------------- the Start-menu list


def test_parse_start_apps() -> None:
    many = '[{"Name":"Spotify","AppID":"SpotifyAB!Spotify"},{"Name":" Notepad ","AppID":"Notepad!App"}]'
    assert parse_start_apps(many) == [StartApp("Spotify", "SpotifyAB!Spotify"), StartApp("Notepad", "Notepad!App")]
    assert parse_start_apps('\ufeff{"Name":"Only","AppID":"one"}\r\n') == [StartApp("Only", "one")]
    assert parse_start_apps("") == [] and parse_start_apps("  \n") == []
    junk = '[{"Name":"","AppID":"x"},{"Name":"No id"},{"Name":3,"AppID":"y"},"text",{"Name":"Ok","AppID":"ok"}]'
    assert parse_start_apps(junk) == [StartApp("Ok", "ok")]


def test_start_menu_links(tmp_path: Path) -> None:
    user, everyone = tmp_path / "user", tmp_path / "everyone"
    (user / "Spotify").mkdir(parents=True)
    (user / "Spotify" / "Spotify.lnk").write_bytes(b"")
    (everyone / "Accessories").mkdir(parents=True)
    (everyone / "Accessories" / "Notepad.lnk").write_bytes(b"")
    (everyone / "spotify.lnk").write_bytes(b"")  # the user's copy wins
    (everyone / "readme.txt").write_text("not a shortcut")
    apps = start_menu_links([user, everyone, tmp_path / "missing"])
    assert [a.name for a in apps] == ["Spotify", "Notepad"]
    assert apps[0].link == str(user / "Spotify" / "Spotify.lnk") and not apps[0].app_id


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_app_list_loads_in_the_background_and_caches() -> None:
    clock, calls, gate = Clock(), [], threading.Event()

    def loader() -> list[StartApp]:
        calls.append(threading.current_thread().name)
        gate.wait(5)
        return [StartApp(f"App {len(calls)}", app_id="x")]

    apps = AppList(loader, ttl=600, clock=clock)
    apps.warm()
    assert apps.get(wait=0) is None  # still loading: nothing yet
    gate.set()
    assert apps.get(wait=5) == [StartApp("App 1", app_id="x")]
    assert calls == ["jarvis-desktop-apps"]
    clock.now += 599
    assert apps.get(wait=5)[0].name == "App 1"  # type: ignore[index]
    assert apps.get(wait=5, max_age=5)[0].name == "App 2"  # type: ignore[index]  # a refresh on a miss
    assert apps.age() == 0
    clock.now += 601
    assert apps.get(wait=5)[0].name == "App 3"  # type: ignore[index]  # older than the ttl


def test_app_list_keeps_the_old_list_when_a_reload_fails() -> None:
    clock, results = Clock(), [[StartApp("Spotify", app_id="s")], RuntimeError("PowerShell is gone")]

    def loader() -> list[StartApp]:
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    apps = AppList(loader, ttl=10, clock=clock)
    assert apps.get(wait=5) == [StartApp("Spotify", app_id="s")]
    clock.now += 11
    assert apps.get(wait=5) == [StartApp("Spotify", app_id="s")]
    assert apps.age() == 11


# --------------------------------------------------------------------------- volume


def test_clamp_and_plan_volume() -> None:
    assert [clamp_level(v) for v in (-5, 0, 29.6, 100, 140)] == [0, 0, 30, 100, 100]
    assert plan_volume(50, False, 30, None) == (30, False)
    assert plan_volume(50, True, 30, None) == (30, False)  # a level unmutes, like Windows' slider
    assert plan_volume(50, True, 0, None) == (0, True)
    assert plan_volume(50, True, None, "up") == (60, False)
    assert plan_volume(95, False, None, "up") == (100, False)
    assert plan_volume(5, True, None, "down") == (0, True)
    assert plan_volume(40, False, None, "mute") == (40, True)
    assert plan_volume(40, True, None, "unmute") == (40, False)
    assert plan_volume(40, True, None, None) == (40, True)


def test_volume_text() -> None:
    assert volume_text(30, False, None) == "Volume is 30%."
    assert volume_text(30, True, "down") == "Volume is 30%, muted."
    assert volume_text(30, True, "mute") == "Muted; the volume stays at 30%."
    assert volume_text(30, False, "unmute") == "Unmuted; the volume is 30%."


# --------------------------------------------------------------------------- PNG


def decode_png(data: bytes) -> np.ndarray:
    """A minimal reader for what encode_png writes (checks every CRC, undoes the Sub filter)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        chunks[kind] = body
        pos += 12 + length
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour, _c, _f, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, colour, interlace) == (8, 2, 0)
    rows = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), dtype=np.uint8).reshape(height, width * 3 + 1)
    assert (rows[:, 0] == 1).all()  # Sub
    return np.cumsum(rows[:, 1:].reshape(height, width, 3), axis=1, dtype=np.uint8)


def test_png_round_trip() -> None:
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8)
    rgb[:10] = (255, 128, 0)  # a flat area too
    assert np.array_equal(decode_png(encode_png(rgb)), rgb)
    one = np.array([[[1, 2, 3]]], dtype=np.uint8)
    assert np.array_equal(decode_png(encode_png(one)), one)


def test_png_takes_rgb_only() -> None:
    with pytest.raises(ValueError):
        encode_png(np.zeros((2, 2, 4), dtype=np.uint8))
    with pytest.raises(ValueError):
        encode_png(np.zeros((0, 2, 3), dtype=np.uint8))


def test_new_files_never_replace_old_ones(tmp_path: Path) -> None:
    first = write_new_file(tmp_path / "Screenshots", "Jarvis 1.png", b"one")
    second = write_new_file(tmp_path / "Screenshots", "Jarvis 1.png", b"two")
    assert (first.name, second.name) == ("Jarvis 1.png", "Jarvis 1 (2).png")
    assert first.read_bytes() == b"one" and second.read_bytes() == b"two"


# --------------------------------------------------------------------------- the service


def test_every_action_answers_schema_valid(
    fake: FakeDesktop, service: DesktopService, tmp_path: Path, reference_validator: Any
) -> None:
    fake.screenshots = tmp_path
    bodies = [
        {"action": "open", "target": "Spotify"},
        {"action": "open", "target": "spotify:playlist:abc"},
        {"action": "open", "target": str(tmp_path)},
        {"action": "focus", "target": "Visual Studio Code"},
        {"action": "media", "key": "play_pause"},
        {"action": "volume", "level": 30},
        {"action": "volume", "change": "mute"},
        {"action": "volume"},
        {"action": "screenshot"},
        {"action": "lock"},
        {"action": "clipboard_write", "text": "hello"},
        {"action": "clipboard_read"},
    ]
    for body in bodies:
        answer = service.handle(body)
        reference_validator(answer, "CommandResponse")
        assert result(answer)[0] == "done", (body, answer)
    assert fake.calls == [
        ("open_app", "Spotify"),
        ("open_uri", "spotify:playlist:abc"),
        ("open_folder", str(tmp_path)),
        ("focus", "README.md - jarvis - Visual Studio Code"),
        ("media", "play_pause"),
        ("volume", 30, None),
        ("volume", None, "mute"),
        ("volume", None, None),
        ("screenshot",),
        ("lock",),
        ("clipboard_write", 5),
        ("clipboard_read",),
    ]


def test_answers_in_plain_words(fake: FakeDesktop, service: DesktopService) -> None:
    assert result(service.handle({"action": "open", "target": "spotfy"})) == ("done", "Opened Spotify.")
    assert result(service.handle({"action": "media", "key": "next"})) == ("done", "Pressed the next-track media key.")
    assert result(service.handle({"action": "volume", "level": 30})) == ("done", "Volume is 30%.")
    assert result(service.handle({"action": "focus", "target": "teams"})) == ("failed", "teams is not open; use open.")
    refused = result(service.handle({"action": "open", "target": "file:///C:/x.exe"}))
    assert refused[0] == "refused" and "file:" in refused[1]
    assert ("open_uri", "file:///C:/x.exe") not in fake.calls


def test_screenshot_answer_has_the_path(fake: FakeDesktop, service: DesktopService, tmp_path: Path) -> None:
    fake.screenshots = tmp_path / "Screenshots"
    answer = service.handle({"action": "screenshot"})["desktop"]
    path = Path(answer["path"])
    assert path.parent == tmp_path / "Screenshots" and path.name.startswith("Jarvis ") and path.suffix == ".png"
    assert answer["text"].startswith(f"Saved a 4x3 screenshot to {path}.")
    assert "Look at it with Read only if the user asked" in answer["text"]
    assert decode_png(path.read_bytes())[0, 0].tolist() == [255, 0, 0]


def test_clipboard_read_is_cut_and_says_when_empty(fake: FakeDesktop, service: DesktopService) -> None:
    assert service.handle({"action": "clipboard_read"})["desktop"] == {
        "result": "done",
        "text": "The clipboard holds no text.",
    }
    fake.clipboard = "x" * (CLIPBOARD_READ_LIMIT + 10)
    answer = service.handle({"action": "clipboard_read"})["desktop"]
    assert answer["clipboard"] == "x" * CLIPBOARD_READ_LIMIT
    assert answer["text"] == "The clipboard holds 4,010 characters of text; here are the first 4,000."


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"action": "open"}, "The open action needs a target."),
        ({"action": "focus", "target": "   "}, "The focus action needs a target."),
        ({"action": "open", "target": "x" * 401}, "The target is longer than 400 characters."),
        ({"action": "lock", "target": "x"}, "The lock action takes no target."),
        ({"action": "screenshot", "key": "next", "text": "x"}, "The screenshot action takes no key, text."),
        ({"action": "media"}, "The media action needs key: play_pause, next, previous, stop."),
        ({"action": "media", "key": "volume_up"}, "The media action needs key: play_pause, next, previous, stop."),
        ({"action": "volume", "level": 30, "change": "up"}, "Give the volume a level or a change, not both."),
        ({"action": "volume", "level": True}, "The volume level is a whole number from 0 to 100."),
        ({"action": "volume", "level": 101}, "The volume level is a whole number from 0 to 100."),
        ({"action": "volume", "change": "louder"}, "The volume change is one of up, down, mute, unmute."),
        ({"action": "clipboard_write"}, "The clipboard_write action needs text."),
        ({"action": "clipboard_write", "text": ""}, "The clipboard_write action needs text."),
        ({"action": "clipboard_write", "text": "x" * 20001}, "Clipboard text is limited to 20,000 characters."),
        ({"action": "type", "text": "rm -rf"}, "Unknown desktop action 'type'."),
        ({}, "Unknown desktop action None."),
    ],
)
def test_bad_requests_fail_without_reaching_the_desktop(
    fake: FakeDesktop, service: DesktopService, body: dict[str, Any], message: str
) -> None:
    assert result(service.handle(body)) == ("failed", message)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Refused("Windows said no."), ("refused", "Windows said no.")),
        (Unsupported("No speakers."), ("unsupported", "No speakers.")),
        (Failed("It broke."), ("failed", "It broke.")),
        (OSError("Access is denied"), ("failed", "The lock action failed: OSError: Access is denied")),
        (KeyError("x"), ("failed", "The lock action failed: KeyError: 'x'")),
    ],
)
def test_backend_errors_become_results(
    fake: FakeDesktop, service: DesktopService, error: Exception, expected: tuple[str, str]
) -> None:
    def fail(_action: str) -> None:
        raise error

    fake.hook = fail
    assert result(service.handle({"action": "lock"})) == expected


def test_actions_run_one_at_a_time_on_the_desktop_thread(fake: FakeDesktop, service: DesktopService) -> None:
    lock, running, most, threads = threading.Lock(), [0], [0], set()

    def slow(_action: str) -> None:
        threads.add(threading.current_thread().name)
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1

    fake.hook = slow
    answers: list[dict[str, Any]] = []
    callers = [
        threading.Thread(target=lambda: answers.append(service.handle({"action": "media", "key": "next"})))
        for _ in range(4)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(5)
    assert [result(a)[0] for a in answers] == ["done"] * 4
    assert most == [1] and threads == {"jarvis-desktop"}
    assert fake.thread == "jarvis-desktop"  # prepared once, on that thread


def test_a_slow_action_times_out_and_the_one_behind_it_never_runs(fake: FakeDesktop) -> None:
    service = DesktopService(fake, action_timeout=0.3)
    started, release = threading.Event(), threading.Event()

    def stuck(action: str) -> None:
        if action == "volume":
            started.set()
            release.wait(5)

    fake.hook = stuck
    slow: list[dict[str, Any]] = []
    first = threading.Thread(target=lambda: slow.append(service.handle({"action": "volume", "level": 10})))
    first.start()
    assert started.wait(5)
    queued = service.handle({"action": "lock"})  # waits behind it, then gives up
    first.join(5)
    assert result(slow[0]) == ("failed", "The volume action is taking longer than 0.3 s; it may still finish.")
    assert result(queued) == ("failed", "The desktop is still busy with an earlier action, so lock was not done.")
    release.set()
    assert result(service.handle({"action": "media", "key": "stop"}))[0] == "done"
    assert ("lock",) not in fake.calls  # a late lock never happens
    assert [c[0] for c in fake.calls] == ["volume", "media"]
    service.close()


def test_warm_failures_and_close(fake: FakeDesktop) -> None:
    class Cold(FakeDesktop):
        def warm(self) -> None:
            raise OSError("no PowerShell")

    cold = DesktopService(Cold())
    assert result(cold.handle({"action": "lock"}))[0] == "done"
    cold.close()
    assert result(cold.handle({"action": "lock"})) == (
        "failed",
        "Desktop actions have stopped; the helper is shutting down.",
    )
