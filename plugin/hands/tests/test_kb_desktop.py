"""The keyboard side of the Windows desktop, through ``FakeWin32`` (DESIGN 3.5, 5.4: W1 to W16, W41, S5, S6).

Key injection (``SendInput`` with Unicode events for characters, virtual keys for the controls), the ledger of key-ups
Windows refused, the probes that say where a key would land (the absolute elevation rule included), the yield probes
and the on-screen keyboard launcher. Everything here runs on any OS against the fake DLLs; the read-only checks on the
real thing are in ``test_desktop_windows.py``.

Typed characters never reach a log, an exception text or a repr (SR13): the tests that fail on purpose also look.
"""

from __future__ import annotations

import ast
import ctypes
import logging
import random
from collections.abc import Iterator
from ctypes import wintypes as wt
from pathlib import Path
from typing import Any

import pytest
from test_desktop_windows import (
    HE_HKL,
    HIGH,
    MEDIUM,
    OTHER_PID,
    OUR_PID,
    US_HKL,
    FakeWin,
    FakeWin32,
    install_fake_api,
)

import jarvis_hands
from jarvis_hands.desktop import windows as win
from jarvis_hands.desktop.base import InputBlocked, KeyDesktop, KeyTarget, as_key_desktop
from jarvis_hands.desktop.fake import FakeDesktop, events_balanced
from jarvis_hands.desktop.keys import (
    ALLOWED_CHARS,
    CONTROL_KEYS,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_LSHIFT,
    KeyRefused,
    KeyStroke,
)
from jarvis_hands.desktop.windows import WindowsDesktop

UP = KEYEVENTF_KEYUP
UNI = KEYEVENTF_UNICODE
SYSTEM = 0x4000
LOW = 0x1000
SENTINEL = "q"  # a typed character must never show up in an exception text, a log line or a repr
HE_SENTINEL = "ש"
FOCUS = 0x800  # the foreground window of most tests
EDIT = 0x801  # a child control with the keyboard focus


def char(value: str) -> KeyStroke:
    return KeyStroke("char", value)


def control(name: str) -> KeyStroke:
    return KeyStroke("control", name)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeWin32:
    fake = install_fake_api(monkeypatch)
    fake.add(FakeWin(FOCUS, pid=OTHER_PID, cls="Notepad", title="notes.txt - Notepad"))
    fake.foreground_hwnd = FOCUS
    fake.images[OTHER_PID] = r"C:\Windows\System32\notepad.exe"
    return fake


@pytest.fixture
def wdesk(api: FakeWin32) -> Iterator[WindowsDesktop]:
    desktop = WindowsDesktop(api=api)
    yield desktop
    desktop.close()


def ups_for(events: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    return [e for e in events if e[2] & UP]


# --------------------------------------------------------------------------- W1: layouts and prototypes

SRC = Path(jarvis_hands.__file__).resolve().parent
PTR = "ptr"
#: Widths of the Windows types the module's structures use, by the name they carry in the source. Python's ctypes
#: aliases differ by OS (c_ulong is 8 bytes on Linux), so the structures are laid out from their source instead.
WINDOWS_TYPE_SIZES: dict[str, Any] = {
    "wt.WORD": 2,
    "wt.DWORD": 4,
    "wt.LONG": 4,
    "wt.UINT": 4,
    "wt.BOOL": 4,
    "wt.WCHAR": 2,
    "ctypes.c_uint16": 2,
    "ctypes.c_uint32": 4,
    "ctypes.c_uint64": 8,
    "wt.HWND": PTR,
    "wt.HANDLE": PTR,
    "ULONG_PTR": PTR,
}
RECT_SIZE = 16  # four LONGs


def windows_layout(name: str, pointer: int) -> tuple[int, int]:
    """(size, alignment) of the structure or union ``name`` of windows.py as the C compiler lays it out on Windows."""
    tree = ast.parse((SRC / "desktop" / "windows.py").read_text(encoding="utf-8"))
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def measure(type_name: str) -> tuple[int, int]:
        if type_name == "wt.RECT":
            return RECT_SIZE, 4
        if type_name in classes:
            return of_class(classes[type_name])
        width = WINDOWS_TYPE_SIZES[type_name]
        width = pointer if width == PTR else width
        return width, width

    def of_type(node: ast.expr) -> tuple[int, int]:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):  # an array: ``wt.WCHAR * 32``
            size, align = of_type(node.left)
            assert isinstance(node.right, ast.Constant) and isinstance(node.right.value, int)
            return size * node.right.value, align
        return measure(ast.unparse(node))

    def of_class(node: ast.ClassDef) -> tuple[int, int]:
        union = any(ast.unparse(b) == "ctypes.Union" for b in node.bases)
        fields = next(
            n.value for n in node.body if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "_fields_"
        )
        assert isinstance(fields, ast.List)
        offset, widest, strictest = 0, 0, 1
        for entry in fields.elts:
            assert isinstance(entry, ast.Tuple)
            size, align = of_type(entry.elts[1])
            strictest = max(strictest, align)
            if union:
                widest = max(widest, size)
            else:
                offset = -(-offset // align) * align + size
        end = widest if union else offset
        return -(-end // strictest) * strictest, strictest

    return of_class(classes[name])


#: name -> (Win64 size, Win32 size): the sizes SendInput, GetGUIThreadInfo and GetLastInputInfo check cbSize against.
KNOWN_SIZES = {
    "INPUT": (0x28, 0x1C),
    "MOUSEINPUT": (0x20, 0x18),
    "KEYBDINPUT": (0x18, 0x10),
    "HARDWAREINPUT": (8, 8),
    "_INPUTUNION": (0x20, 0x18),
    "GUITHREADINFO": (0x48, 0x30),
    "LASTINPUTINFO": (8, 8),
    # the layouts the existing Windows-only test pins: they prove the calculator itself
    "MONITORINFOEXW": (0x68, 0x68),
    "DISPLAY_DEVICEW": (0x348, 0x348),
    "DISPLAYCONFIG_PATH_INFO": (0x48, 0x48),
    "DISPLAYCONFIG_MODE_INFO": (0x40, 0x40),
    "DISPLAYCONFIG_TARGET_DEVICE_NAME": (0x1A4, 0x1A4),
}


@pytest.mark.parametrize("name", sorted(KNOWN_SIZES))
def test_w1_structure_sizes_on_64_and_32_bit_windows(name: str) -> None:
    """W1: the sizes, computed from the source on any OS (the real ``sizeof`` is checked on Windows elsewhere)."""
    wide, narrow = KNOWN_SIZES[name]
    assert windows_layout(name, 8)[0] == wide
    assert windows_layout(name, 4)[0] == narrow


def test_w1_the_input_union_holds_its_largest_member() -> None:
    """A union without the mouse member would make ``sizeof(INPUT)`` too small and SendInput would refuse the batch."""
    members = dict(win._INPUTUNION._fields_)
    assert set(members) == {"mi", "ki", "hi"}
    sizes = {n: windows_layout(t.__name__, 8)[0] for n, t in members.items()}
    assert sizes == {"mi": 0x20, "ki": 0x18, "hi": 8}
    assert windows_layout("_INPUTUNION", 8)[0] == max(sizes.values()) > sizes["ki"]
    assert win.INPUT._anonymous_ == ("u",)  # item.ki, item.mi work without going through the union
    assert dict(win.INPUT._fields_)["type"] is wt.DWORD
    assert win.EXTRA_INFO_TAG == 0x4A525653  # "JRVS": keys carry the same tag the mouse events do


def test_w1_the_keyboard_structures_have_the_win32_field_names() -> None:
    assert [n for n, _t in win.GUITHREADINFO._fields_] == [
        "cbSize",
        "flags",
        "hwndActive",
        "hwndFocus",
        "hwndCapture",
        "hwndMenuOwner",
        "hwndMoveSize",
        "hwndCaret",
        "rcCaret",
    ]
    assert [n for n, _t in win.LASTINPUTINFO._fields_] == ["cbSize", "dwTime"]
    assert [n for n, _t in win.KEYBDINPUT._fields_] == ["wVk", "wScan", "dwFlags", "time", "dwExtraInfo"]


class _StubFunction:
    def __init__(self, dll: str) -> None:
        self.dll = dll
        self.argtypes: Any = None
        self.restype: Any = "unset"


class _StubDll:
    def __init__(self, name: str) -> None:
        self.name = name
        self.functions: dict[str, _StubFunction] = {}

    def __getattr__(self, name: str) -> _StubFunction:
        if name.startswith("__"):
            raise AttributeError(name)
        return self.functions.setdefault(name, _StubFunction(self.name))


#: name -> (dll, restype, argtypes): what the Win32 headers say, in ctypes types. A handle is pointer-sized, and an
#: ``int`` would cut it to 32 bits.
EXPECTED_PROTOTYPES: dict[str, tuple[str, Any, list[Any]]] = {
    "GetForegroundWindow": ("user32", wt.HWND, []),
    "GetGUIThreadInfo": ("user32", wt.BOOL, [wt.DWORD, ctypes.POINTER(win.GUITHREADINFO)]),
    "GetKeyboardLayout": ("user32", ctypes.c_void_p, [wt.DWORD]),
    "VkKeyScanExW": ("user32", ctypes.c_short, [wt.WCHAR, ctypes.c_void_p]),
    "MapVirtualKeyExW": ("user32", wt.UINT, [wt.UINT, wt.UINT, ctypes.c_void_p]),
    "GetLastInputInfo": ("user32", wt.BOOL, [ctypes.POINTER(win.LASTINPUTINFO)]),
    "GetAsyncKeyState": ("user32", ctypes.c_short, [ctypes.c_int]),
    "GetKeyState": ("user32", ctypes.c_short, [ctypes.c_int]),
    "GetClassNameW": ("user32", ctypes.c_int, [wt.HWND, wt.LPWSTR, ctypes.c_int]),
    "QueryFullProcessImageNameW": ("kernel32", wt.BOOL, [wt.HANDLE, wt.DWORD, wt.LPWSTR, wt.LPDWORD]),
    "ShellExecuteW": (
        "shell32",
        ctypes.c_void_p,
        [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int],
    ),
    "SHQueryUserNotificationState": ("shell32", win.HRESULT, [ctypes.POINTER(ctypes.c_int)]),
    "SendInput": ("user32", wt.UINT, [wt.UINT, ctypes.POINTER(win.INPUT), ctypes.c_int]),
}


@pytest.fixture
def stub_dlls(monkeypatch: pytest.MonkeyPatch) -> dict[str, _StubDll]:
    dlls: dict[str, _StubDll] = {}
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kw: dlls.setdefault(name, _StubDll(name)), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    return dlls


def test_w1_every_prototype_declares_its_argtypes_and_restype(stub_dlls: dict[str, _StubDll]) -> None:
    """W1: the DLLs are loaded here as stubs that record what ``_fn`` sets, so a function declared without both (the
    default ``int`` truncates 64-bit handles) or on the wrong DLL fails on every OS."""
    wrapped = win._Win32()
    functions = {n: f for n, f in vars(wrapped).items() if isinstance(f, _StubFunction)}
    assert len(functions) >= len(EXPECTED_PROTOTYPES)
    for name, function in functions.items():
        assert function.argtypes is not None, f"{name} has no argtypes"
        assert function.restype != "unset", f"{name} has no restype"
    for name, (dll, restype, argtypes) in EXPECTED_PROTOTYPES.items():
        function = functions[name]
        assert function.dll == dll, name
        assert function.restype is restype, name
        assert function.argtypes == argtypes, name


# --------------------------------------------------------------------------- W2, W3, W4: the events


@pytest.mark.parametrize("value", ["a", "Z", "ש", "ם", ",", "?", "'", "-"])
def test_w2_a_character_is_a_unicode_down_and_up(api: FakeWin32, wdesk: WindowsDesktop, value: str) -> None:
    assert wdesk.send_keys([char(value)]) == 1
    code = ord(value)
    assert api.sent_keys == [[(0, code, UNI), (0, code, UNI | UP)]]  # wVk is 0 with KEYEVENTF_UNICODE, wScan the code


def test_w2_a_character_outside_the_basic_plane_is_refused_before_anything_is_sent(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    with pytest.raises(KeyRefused):
        KeyStroke("char", "\U0001f600")
    forged = char("a")
    object.__setattr__(forged, "value", "\U0001f600")  # forced past the constructor
    with pytest.raises(KeyRefused):
        wdesk.send_keys([forged])
    forged_digit = char("a")
    object.__setattr__(forged_digit, "value", "7")
    with pytest.raises(KeyRefused):
        wdesk.send_keys([char("a"), forged_digit])
    assert api.sent_keys == [] and api.calls["SendInput"] == 0  # not even the allowed stroke before the refused one


@pytest.mark.parametrize("name", ["space", "backspace", "enter"])
def test_w3_a_control_is_a_virtual_key_and_scan_code_pair_without_the_extended_flag(
    api: FakeWin32, wdesk: WindowsDesktop, name: str
) -> None:
    vk, scan = CONTROL_KEYS[name]
    assert wdesk.send_keys([control(name)]) == 1
    assert api.sent_keys == [[(vk, scan, 0), (vk, scan, UP)]]
    assert not any(flags & KEYEVENTF_EXTENDEDKEY for _vk, _scan, flags in api.sent_keys[0])


def test_w4_one_send_input_call_per_stroke_tagged_and_without_a_timestamp(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    strokes = [char("h"), char("ש"), control("space"), control("backspace")]
    assert wdesk.send_keys(strokes) == 4
    assert api.calls["SendInput"] == 4 and api.key_requests == [2, 2, 2, 2]  # a stroke's events go in together
    assert api.key_tags == [(0, win.EXTRA_INFO_TAG)] * 8  # ki.time = 0 (the system stamps it), tagged as ours
    assert all(events_balanced(events) for events in api.sent_keys)


def test_w4_the_call_is_made_under_the_input_lock(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    held: list[bool] = []
    real_send = api.SendInput

    def spy(count: int, inputs: Any, size: int) -> int:
        held.append(wdesk._input_lock._is_owned())  # type: ignore[attr-defined]
        return real_send(count, inputs, size)

    api.SendInput = spy  # type: ignore[method-assign]
    wdesk.send_keys([char("a")])
    assert held == [True]


def test_w4_an_empty_list_sends_nothing(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert wdesk.send_keys([]) == 0
    assert api.calls["SendInput"] == 0


def test_the_inject_mode_must_be_one_of_the_two(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    with pytest.raises(ValueError):
        wdesk.send_keys([char("a")], inject="both")  # type: ignore[arg-type]
    assert api.sent_keys == []


# --------------------------------------------------------------------------- W5, W6, S5: partial and refused batches


def test_w5_a_partial_insert_sends_the_missing_up_at_once(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [1]  # Windows takes the down and not the up
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    assert api.sent_keys == [[(0, ord("a"), UNI)], [(0, ord("a"), UNI | UP)]]  # the rescue is one more call
    wdesk.release_keys()
    assert api.calls["SendInput"] == 2  # nothing is parked: the up went in


def test_w5_a_refused_rescue_is_parked_and_release_keys_sends_it_once(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [1, 0]  # the down goes in, then Windows refuses the up as well
    with pytest.raises(OSError):
        wdesk.send_keys([char("ש")])
    assert api.sent_keys == [[(0, ord("ש"), UNI)], []]
    wdesk.release_keys()
    assert api.sent_keys[-1] == [(0, ord("ש"), UNI | UP)]
    wdesk.release_keys()
    wdesk.close()
    assert api.calls["SendInput"] == 3  # sent exactly once


@pytest.mark.parametrize(
    ("taken", "missing"),
    [
        (1, [(VK_LSHIFT, 0x2A, UP)]),  # only Shift went down
        (2, [(0x41, 0x1E, UP), (VK_LSHIFT, 0x2A, UP)]),  # Shift and the key: the key first, then Shift
        (3, [(VK_LSHIFT, 0x2A, UP)]),  # the key is already up
    ],
)
def test_w5_every_down_that_went_in_gets_its_up_in_one_call_newest_first(
    api: FakeWin32, wdesk: WindowsDesktop, taken: int, missing: list[tuple[int, int, int]]
) -> None:
    api.key_maps[US_HKL]["A"] = 0x141
    api.send_script = [taken]
    with pytest.raises(OSError):
        wdesk.send_keys([char("A")], inject="vk")  # [LShift down, A down, A up, LShift up]
    assert api.sent_keys[1] == missing
    assert api.calls["SendInput"] == 2


def test_w5_a_rescue_that_takes_only_some_parks_the_rest(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [2, 1]  # Shift and A went down; of the two ups only the first goes in
    with pytest.raises(OSError):
        wdesk.send_keys([char("A")], inject="vk")
    assert api.sent_keys[1] == [(0x41, 0x1E, UP)]
    wdesk.release_keys()
    assert api.sent_keys[2] == [(VK_LSHIFT, 0x2A, UP)]  # the one that was refused


def test_w6_zero_events_taken_is_input_blocked_and_nothing_is_parked(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_limit = 0
    with pytest.raises(InputBlocked) as excinfo:
        wdesk.send_keys([char(SENTINEL)])
    assert SENTINEL not in str(excinfo.value)
    api.send_limit = None
    wdesk.release_keys()
    wdesk.close()
    assert api.sent_keys == [[]]  # no down went in, so no up is owed


def test_w6_some_events_taken_is_an_os_error_that_does_not_name_the_character(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    api.send_script = [1]
    with caplog.at_level(logging.DEBUG, logger=win.__name__), pytest.raises(OSError) as excinfo:
        wdesk.send_keys([char(HE_SENTINEL)])
    assert not isinstance(excinfo.value, InputBlocked)
    assert HE_SENTINEL not in str(excinfo.value) and HE_SENTINEL not in caplog.text


def test_w6_a_later_stroke_that_is_refused_whole_ends_the_call_with_the_count_so_far(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.send_script = [2, 2, 0]
    assert wdesk.send_keys([char("a"), char("b"), char("c"), char("d")]) == 2  # the third was refused whole: stop
    assert len(api.sent_keys) == 3 and api.sent_keys[2] == []
    api.send_script = [2, 1]  # the second is taken in part: the caller must hear of it
    with pytest.raises(OSError):
        wdesk.send_keys([char("a"), char("b")])


def test_w6_a_first_stroke_that_is_refused_whole_stops_the_list(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [0]
    with pytest.raises(InputBlocked):
        wdesk.send_keys([char("a"), char("b")])
    assert api.calls["SendInput"] == 1  # the rest was not even tried


def test_s5_send_limit_partial_and_zero_at_the_windows_level(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_limit = 1  # every call takes one event: the down goes in, the rescue (one event) goes in too
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    assert [len(c) for c in api.sent_keys] == [1, 1]
    wdesk.release_keys()
    assert api.calls["SendInput"] == 2  # balanced: down, then up
    api.send_limit = 0
    with pytest.raises(InputBlocked):
        wdesk.send_keys([char("a")])
    api.send_limit = None
    assert api.sent_keys[-1] == []


def test_s6_an_exception_in_the_rescue_is_parked_and_close_drains_it_without_raising(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    real_send = api.SendInput
    calls = [0]

    def flaky(count: int, inputs: Any, size: int) -> int:
        calls[0] += 1
        if calls[0] >= 2:
            raise RuntimeError("boom")  # the rescue itself blows up
        return real_send(count, inputs, size)

    api.SendInput = flaky  # type: ignore[method-assign]
    api.send_script = [1]
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])  # the caller hears of the partial batch, not of the rescue's failure
    wdesk.close()  # still failing: swallowed
    wdesk.close()
    api.SendInput = real_send  # type: ignore[method-assign]
    wdesk.close()  # the ledger is drained by the next close
    assert api.sent_keys[-1] == [(0, ord("a"), UNI | UP)]
    held = [e for events in api.sent_keys for e in events]
    assert sum(1 for e in held if not e[2] & UP) == sum(1 for e in held if e[2] & UP)  # no down remains


def test_the_ledger_is_sent_ahead_of_the_next_stroke(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [1, 0]  # a down is stranded, its up refused
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    wdesk.send_keys([char("b")])
    assert api.sent_keys[2] == [(0, ord("a"), UNI | UP)]  # first the owed up, in a call of its own
    assert api.sent_keys[3] == [(0, ord("b"), UNI), (0, ord("b"), UNI | UP)]


def test_a_stranded_up_is_not_sent_into_the_void_while_windows_still_refuses(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.send_script = [1, 0]
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    api.send_limit = 0
    with pytest.raises(InputBlocked):
        wdesk.send_keys([char("b")])  # the drain is refused too, the stroke is refused, nothing is lost
    api.send_limit = None
    wdesk.release_keys()
    assert api.sent_keys[-1] == [(0, ord("a"), UNI | UP)]


# --------------------------------------------------------------------------- W12: where the ledger is retried


def test_w12_release_keys_input_desktop_ok_and_close_each_drain_the_ledger(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    def strand() -> None:
        api.send_script = [1, 0]
        with pytest.raises(OSError):
            wdesk.send_keys([char("a")])

    up = [(0, ord("a"), UNI | UP)]
    strand()
    wdesk.release_keys()
    assert api.sent_keys[-1] == up
    strand()
    api.input_desktop = "Winlogon"  # a UAC prompt has the input: the check says no and sends nothing
    before = api.calls["SendInput"]
    assert not wdesk.input_desktop_ok()
    assert api.calls["SendInput"] == before
    api.input_desktop = "Default"
    assert wdesk.input_desktop_ok()
    assert api.sent_keys[-1] == up
    strand()
    wdesk.close()
    assert api.sent_keys[-1] == up
    before = api.calls["SendInput"]
    wdesk.close()
    wdesk.release_keys()
    assert wdesk.input_desktop_ok()
    assert api.calls["SendInput"] == before  # each drained once


def test_w12_release_keys_never_raises(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.send_script = [1, 0]
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    api.broken["SendInput"] = OSError("gone")
    wdesk.release_keys()
    api.broken["SendInput"] = RuntimeError("worse")
    wdesk.release_keys()
    wdesk.close()
    del api.broken["SendInput"]
    wdesk.release_keys()
    assert api.sent_keys[-1] == [(0, ord("a"), UNI | UP)]


def test_w12_the_buttons_ledger_still_works_beside_the_keys(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.button("left", True)
    api.send_limit = 0
    with pytest.raises(OSError):
        wdesk.button("left", False)
    api.send_limit = None
    api.send_script = [1, 0]
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    assert wdesk.input_desktop_ok()
    assert [s[0] for s in api.sent][-1] == win.MOUSEEVENTF_LEFTUP  # the button came up
    assert api.sent_keys[-1] == [(0, ord("a"), UNI | UP)]  # and so did the key


# --------------------------------------------------------------------------- W7, W8, W16: the yield probe


def test_w7_the_first_call_only_baselines(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.user_input()  # whatever happened before the first call is not news
    assert wdesk.foreign_input() is False
    assert wdesk.foreign_input() is False


def test_w7_our_own_strokes_are_not_foreign_input(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.foreign_input()
    for value in "hello":
        wdesk.send_keys([char(value)])
        assert wdesk.foreign_input() is False
    wdesk.send_keys([char("a"), char("b")])  # two in one call: the last one's tick is ours
    assert wdesk.foreign_input() is False


def test_w7_a_foreign_tick_is_foreign_once(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.foreign_input()
    api.user_input()  # a physical key
    assert wdesk.foreign_input() is True
    assert wdesk.foreign_input() is False  # seen: only new input counts
    wdesk.send_keys([char("a")])
    api.user_input()  # between our stroke and the next look
    assert wdesk.foreign_input() is True
    assert wdesk.foreign_input() is False


def test_w7_the_mouse_counts_as_foreign_input_too(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.foreign_input()
    api.user_input()  # the real mouse moved: the tick does not say which device it was
    assert wdesk.foreign_input() is True
    wdesk.move_cursor(300, 300)  # the helper's own pointer input is not noted as keys: it reads as foreign too
    assert wdesk.foreign_input() is True


def test_w7_a_tick_that_only_equals_our_last_one_is_ours(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.foreign_input()
    wdesk.send_keys([char("a")])
    wdesk.send_keys([char("b")])
    api.tick -= api.tick_step  # (a clock that reads the earlier of our two ticks): not one of ours, not seen: foreign
    assert wdesk.foreign_input() is True


def test_w8_a_failing_probe_is_foreign_input(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.last_input_ok = False
    assert wdesk.foreign_input() is True  # also on the very first call
    assert wdesk.foreign_input() is True
    api.last_input_ok = True
    assert wdesk.foreign_input() is False  # the first good answer baselines
    api.last_input_ok = False
    assert wdesk.foreign_input() is True
    api.last_input_ok = True
    assert wdesk.foreign_input() is False  # nothing new happened meanwhile


def test_w16_a_failing_tick_read_after_a_stroke_never_changes_the_stroke(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.foreign_input()
    api.last_input_ok = False  # GetLastInputInfo returns 0 right after SendInput took the stroke
    assert wdesk.send_keys([char("a")]) == 1
    assert api.sent_keys == [[(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]]
    api.last_input_ok = True
    assert wdesk.foreign_input() is True  # a tick that is not ours: one yield, which fails safe
    assert wdesk.foreign_input() is False  # then it baselines normally
    wdesk.send_keys([char("b")])
    assert wdesk.foreign_input() is False


def test_w16_an_exception_from_the_tick_read_after_a_stroke_is_swallowed(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    wdesk.foreign_input()
    api.broken["GetLastInputInfo"] = OSError(f"cannot read {SENTINEL}")
    with caplog.at_level(logging.DEBUG, logger=win.__name__):
        assert wdesk.send_keys([char("a"), char("b")]) == 2  # both strokes count, the failure is not theirs
    del api.broken["GetLastInputInfo"]
    assert wdesk.foreign_input() is True
    assert wdesk.foreign_input() is False
    assert SENTINEL not in caplog.text
    assert wdesk.key_counts["own_tick_failed"] == 2


def test_w16_an_exception_from_send_input_itself_still_raises(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.broken["SendInput"] = OSError("the call itself failed")
    with pytest.raises(OSError):
        wdesk.send_keys([char("a")])
    del api.broken["SendInput"]
    assert wdesk.send_keys([char("a")]) == 1
    # and a failure before the call (building the events) raises too, with nothing sent
    sent = api.calls["SendInput"]
    api.broken["GetAsyncKeyState"] = RuntimeError("no state")
    with pytest.raises(RuntimeError):
        wdesk.send_keys([char("a")], inject="vk")
    assert api.calls["SendInput"] == sent


# --------------------------------------------------------------------------- W9: key_target


def test_w9_an_ordinary_window_is_clear_and_names_its_thread_s_layout_and_executable(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.windows[FOCUS].tid = 501
    api.layouts[501] = HE_HKL  # the foreground thread is on the Hebrew layout; ours (and any other) is on the US one
    target = wdesk.key_target()
    assert target == KeyTarget(FOCUS, OTHER_PID, "notepad", 0x040D, None, False, False)
    assert api.layout_queries == [501]  # the foreground thread's layout, never our own thread's (0)
    assert "notepad" not in repr(target)  # the name is for the local screen only


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (r"C:\Windows\System32\notepad.exe", "notepad"),
        (r"C:\Program Files\Microsoft VS Code\Code.exe", "Code"),
        (r"C:\Tools\NOTEPAD.EXE", "NOTEPAD"),
        (r"D:\apps\my.tool.exe", "my.tool"),
        (r"C:\Program Files\app\run", "run"),
    ],
)
def test_w9_the_name_is_the_base_name_without_exe(api: FakeWin32, wdesk: WindowsDesktop, path: str, name: str) -> None:
    api.images[OTHER_PID] = path
    assert wdesk.key_target().name == name


def test_w9_the_name_is_cached_per_process_and_empty_when_it_cannot_be_read(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    for _ in range(3):
        assert wdesk.key_target().name == "notepad"
    assert api.calls["QueryFullProcessImageNameW"] == 1
    del api.images[OTHER_PID]
    wdesk.close()  # forgets the cache
    assert wdesk.key_target().name == ""
    api.images[OTHER_PID] = r"C:\a\b.exe"
    assert wdesk.key_target().name == "b"  # a failed read is not cached


def test_w9_no_foreground_window_is_blocked_none(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.foreground_hwnd = 0
    target = wdesk.key_target()
    assert (target.hwnd, target.blocked) == (0, "none")
    assert wdesk.foreground_window() == 0


def test_w9_our_own_window_is_own_even_when_it_runs_as_administrator(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.add(FakeWin(0x900, pid=OUR_PID, cls="JarvisOverlay"))
    api.foreground_hwnd = 0x900
    assert wdesk.key_target().blocked == "own"


@pytest.mark.parametrize("kind", sorted(win.SHELL_WINDOW_CLASSES))
def test_w9_every_shell_class_is_shell(api: FakeWin32, wdesk: WindowsDesktop, kind: str) -> None:
    api.windows[FOCUS].cls = kind
    assert wdesk.key_target().blocked == "shell"


def test_w9_shell_wins_over_elevated_and_elevated_is_never_cached_as_clear(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.windows[FOCUS].cls = "Shell_TrayWnd"
    api.levels[OTHER_PID] = HIGH
    assert wdesk.key_target().blocked == "shell"
    api.windows[FOCUS].cls = "Notepad"
    assert wdesk.key_target().blocked == "elevated"


def test_w9_an_elevated_window_is_blocked_before_any_key_goes_anywhere(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.levels[OTHER_PID] = HIGH  # an administrator's console: UIPI would drop the keys silently
    target = wdesk.key_target()
    assert target.blocked == "elevated" and target.hwnd == FOCUS and target.pid == OTHER_PID
    assert target.name == "notepad"  # the strip can still say which window it is


def _focus_edit(api: FakeWin32, *, cls: str = "Edit", style: int = win.ES_PASSWORD | 0x50000000) -> None:
    api.add(FakeWin(EDIT, pid=OTHER_PID, cls=cls, style=style))
    api.focus_hwnd = EDIT


@pytest.mark.parametrize(
    ("cls", "style", "password"),
    [
        ("Edit", win.ES_PASSWORD | 0x50000000, True),
        ("Edit", 0x50000000, False),  # an ordinary edit control
        ("Edit", win.ES_PASSWORD, True),
        ("WindowsForms10.EDIT.app.0.141b42a_r6_ad1", win.ES_PASSWORD, True),  # a WinForms TextBox with bullets
        ("RICHEDIT50W", win.ES_PASSWORD, True),
        ("RichEdit20W", win.ES_PASSWORD, True),
        ("Button", win.ES_PASSWORD, False),  # bit 5 means something else to a button
        ("Chrome_RenderWidgetHostHWND", win.ES_PASSWORD, False),  # a browser: undetectable, documented
        ("Edit", -0x80000000 | win.ES_PASSWORD, True),  # a style with the top bit set comes back negative
    ],
)
def test_w9_a_classic_password_edit_with_the_focus_is_password(
    api: FakeWin32, wdesk: WindowsDesktop, cls: str, style: int, password: bool
) -> None:
    _focus_edit(api, cls=cls, style=style)
    assert wdesk.key_target().password is password
    assert wdesk.key_target().blocked is None


def test_w9_the_focus_is_read_from_the_foreground_thread(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    _focus_edit(api)
    wdesk.key_target()
    assert api.calls["GetGUIThreadInfo"] == 1  # asked with 0: the assertion in the fake checks the argument
    api.focus_hwnd = None  # no separate focus window: the foreground window itself
    assert wdesk.key_target().password is False


def test_w9_a_thread_with_no_focus_window_is_no_password_box(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.focus_hwnd = 0  # between two windows: a key goes nowhere, and nothing is hidden
    target = wdesk.key_target()
    assert target.password is False and target.blocked is None


def test_w9_a_blocked_target_does_not_probe_the_focus_or_the_shell(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.levels[OTHER_PID] = HIGH
    wdesk.key_target()
    assert api.calls["GetGUIThreadInfo"] == 0 and api.calls["SHQueryUserNotificationState"] == 0


@pytest.mark.parametrize(
    ("state", "covered"),
    [
        (win.QUNS_NOT_PRESENT, False),
        (win.QUNS_BUSY, True),
        (win.QUNS_RUNNING_D3D_FULL_SCREEN, True),
        (win.QUNS_PRESENTATION_MODE, True),
        (win.QUNS_ACCEPTS_NOTIFICATIONS, False),
        (win.QUNS_QUIET_TIME, False),
        (win.QUNS_APP, False),
    ],
)
def test_w9_busy_full_screen_and_presentation_are_covered(
    api: FakeWin32, wdesk: WindowsDesktop, state: int, covered: bool
) -> None:
    api.notification_state = state
    assert wdesk.key_target().covered is covered


def test_w9_the_quns_values_are_the_shell_s(api: FakeWin32) -> None:
    assert [
        win.QUNS_NOT_PRESENT,
        win.QUNS_BUSY,
        win.QUNS_RUNNING_D3D_FULL_SCREEN,
        win.QUNS_PRESENTATION_MODE,
        win.QUNS_ACCEPTS_NOTIFICATIONS,
        win.QUNS_QUIET_TIME,
        win.QUNS_APP,
    ] == [1, 2, 3, 4, 5, 6, 7]
    assert win.ES_PASSWORD == 0x20 and win.GWL_STYLE == -16


def test_w9_a_steady_state_probe_is_a_handful_of_calls_and_opens_no_process(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    wdesk.key_target()  # fills the caches
    api.calls.clear()
    wdesk.key_target()
    used = +api.calls
    assert used["OpenProcess"] == 0 and used["QueryFullProcessImageNameW"] == 0
    assert sum(used.values()) <= 10, dict(used)  # one fresh read before every typed character (3.6.1)


def test_w9_the_layout_is_per_foreground_thread_and_follows_it(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.add(FakeWin(0x810, pid=OTHER_PID, tid=900))
    api.layouts.update({77: US_HKL, 900: HE_HKL})
    assert wdesk.key_target().lang_id == 0x0409
    api.foreground_hwnd = 0x810
    assert wdesk.key_target().lang_id == 0x040D


# --------------------------------------------------------------------------- W13: a failing API is never "clear"


def clear(target: KeyTarget) -> bool:
    return target.hwnd != 0 and target.blocked is None and not target.password and not target.covered


SAFETY_PROBES = [
    "GetForegroundWindow",
    "GetWindowThreadProcessId",
    "GetClassNameW",
    "OpenProcess",
    "OpenProcessToken",
    "GetTokenInformation",
    "GetGUIThreadInfo",
    "GetWindowLongPtrW",
    "SHQueryUserNotificationState",
]


@pytest.mark.parametrize("name", SAFETY_PROBES)
def test_w13_a_safety_probe_that_raises_never_raises_out_and_never_answers_clear(
    api: FakeWin32, wdesk: WindowsDesktop, name: str
) -> None:
    _focus_edit(api, style=0x50000000)  # an ordinary edit with the focus: every probe is reached
    assert clear(wdesk.key_target())  # the premise
    wdesk.close()
    api.broken[name] = OSError(f"{name} failed for {SENTINEL}")
    target = wdesk.key_target()
    assert not clear(target), name


def test_w13_a_target_that_could_not_be_read_carries_no_name_and_no_text(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    api.broken["GetClassNameW"] = OSError(f"class of {SENTINEL}")
    with caplog.at_level(logging.DEBUG, logger=win.__name__):
        for _ in range(5):
            target = wdesk.key_target()
    assert (target.hwnd, target.blocked) == (0, "none") and target.name == ""
    assert SENTINEL not in caplog.text and "notepad" not in caplog.text
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) <= 1  # said once, not once per character


@pytest.mark.parametrize(
    ("what", "expect"),
    [
        ("foreground", {"hwnd": 0, "blocked": "none"}),
        ("pid", {"blocked": "none"}),
        ("class", {"blocked": "none"}),
        ("gui", {"blocked": "none"}),
        ("style", {"blocked": "none"}),
        ("shell", {"covered": True}),
        ("token", {"blocked": "elevated"}),
        ("process", {"blocked": "elevated"}),
    ],
)
def test_w13_a_probe_that_fails_without_raising_is_not_clear_either(
    api: FakeWin32, wdesk: WindowsDesktop, what: str, expect: dict[str, Any]
) -> None:
    _focus_edit(api, style=0x50000000)
    if what == "foreground":
        api.foreground_hwnd = 0
    elif what == "pid":
        api.windows[FOCUS].pid = 0  # the owner of the window cannot be resolved
    elif what == "class":
        del api.windows[FOCUS]
        api.windows[0x7FF] = FakeWin(0x7FF)  # the foreground handle is not a window any more
    elif what == "gui":
        api.gui_info_ok = False
    elif what == "style":
        api.windows[EDIT].style = None
    elif what == "shell":
        api.notification_hr = -2147467259  # E_FAIL
    elif what == "token":
        api.levels[OTHER_PID] = "no-token"
    elif what == "process":
        del api.levels[OTHER_PID]  # OpenProcess fails for a reason that is not "access denied"
    target = wdesk.key_target()
    assert not clear(target)
    for field_name, value in expect.items():
        assert getattr(target, field_name) == value, (what, field_name)


def test_w13_a_class_name_windows_will_not_give_is_not_clear(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not an exception: GetClassNameW answers 0, once for the foreground window and once for the focused control.

    The ordinary edit control with the focus keeps the first case from being caught by the second probe.
    """
    _focus_edit(api, style=0x50000000)
    real = api.GetClassNameW

    def unreadable(which: int) -> None:
        monkeypatch.setattr(
            api, "GetClassNameW", lambda hwnd, buffer, size: 0 if hwnd == which else real(hwnd, buffer, size)
        )

    unreadable(EDIT)
    assert clear(wdesk.key_target()) is False  # the control with the focus can't be classified
    unreadable(FOCUS)
    target = wdesk.key_target()
    assert (target.hwnd, target.blocked) == (FOCUS, "none")  # the window is known, it is just not a place to type
    unreadable(0)
    assert clear(wdesk.key_target()) is True  # the premise: with every class readable it is clear


def test_w13_cosmetic_probes_may_fail_without_blocking(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    """The name and the layout are for the strip: losing them does not stop a stroke that is otherwise allowed."""
    api.broken["GetKeyboardLayout"] = OSError("no layout")
    target = wdesk.key_target()
    assert (target.blocked, target.lang_id) == (None, 0)
    del api.broken["GetKeyboardLayout"]
    wdesk.close()  # forgets the name the first read cached
    api.broken["QueryFullProcessImageNameW"] = OSError("no image")
    target = wdesk.key_target()
    assert (target.blocked, target.name) == (None, "")


def test_w13_foreground_window_never_raises(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.broken["GetForegroundWindow"] = OSError("no")
    assert wdesk.foreground_window() == 0


# --------------------------------------------------------------------------- W15: the absolute elevation rule


def _elevated(api: FakeWin32, wdesk: WindowsDesktop, *, helper: Any, target: Any, hwnd: int = FOCUS) -> bool:
    api.levels["self"] = helper
    api.levels[OTHER_PID] = target
    desktop = WindowsDesktop(api=api)  # reads the helper's own level at construction
    return desktop.key_target().blocked == "elevated"


@pytest.mark.parametrize(
    ("helper", "target", "elevated"),
    [
        # a normal helper: Medium and Low targets are clear, High and System are not
        (MEDIUM, MEDIUM, False),
        # SendInput says nothing when UIPI drops a key, so anything above our exact level is out up front: a
        # UIAccess window (0x2010) and Medium+ (0x2100) share Medium's band, which a band compare would clear
        (MEDIUM, 0x2010, True),
        (MEDIUM, 0x2100, True),
        (MEDIUM, LOW, False),
        (MEDIUM, 0x0000, False),
        (MEDIUM, HIGH, True),
        (MEDIUM, SYSTEM, True),
        # an elevated helper still refuses an elevated target (the relative rule would clear it)
        (HIGH, HIGH, True),
        (HIGH, SYSTEM, True),
        (HIGH, MEDIUM, False),
        (HIGH, 0x2100, False),
        (0x2100, MEDIUM, False),  # a helper above the target is fine; only the exact level decides
        (0x2100, 0x2100, False),
        # a sandboxed helper: anything above it is out
        (LOW, MEDIUM, True),
        (LOW, LOW, False),
        # the helper cannot read its own level: everything is elevated, a Medium window too
        ("no-token", MEDIUM, True),
        ("no-token", LOW, True),
        ("no-token", HIGH, True),
        # a target that cannot be read, or opened with access denied (a protected process)
        (MEDIUM, "no-token", True),
        (MEDIUM, "denied", True),
        # a process that cannot be opened for another reason (it exited)
        (MEDIUM, None, True),
    ],
)
def test_w15_the_absolute_rule(api: FakeWin32, wdesk: WindowsDesktop, helper: Any, target: Any, elevated: bool) -> None:
    assert _elevated(api, wdesk, helper=helper, target=target) is elevated


def test_w15_the_helper_s_own_level_is_unknown_means_every_window_is_refused(api: FakeWin32) -> None:
    api.levels["self"] = "no-token"
    desktop = WindowsDesktop(api=api)
    assert desktop.integrity is None
    for pid, level in ((OTHER_PID, MEDIUM), (OTHER_PID + 1, LOW)):
        api.levels[pid] = level
        api.add(FakeWin(0x820 + pid, pid=pid))
        api.foreground_hwnd = 0x820 + pid
        assert desktop.key_target().blocked == "elevated"


def test_w15_the_high_rid_constant_sits_beside_the_system_one() -> None:
    assert win.INTEGRITY_HIGH_RID == 0x3000 and win.SECURITY_MANDATORY_SYSTEM_RID == 0x4000


def test_w15_told_answers_are_cached_per_window_and_process(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert wdesk.key_target().blocked is None
    assert wdesk.key_target().blocked is None
    assert api.open_process_calls == 2 or api.open_process_calls == 1  # integrity (+ the name, cached after)
    base = api.open_process_calls
    for _ in range(5):
        wdesk.key_target()
    assert api.open_process_calls == base  # a clear verdict is asked once
    api.levels[OTHER_PID + 5] = HIGH
    api.images[OTHER_PID + 5] = r"C:\Windows\System32\taskmgr.exe"
    api.add(FakeWin(0x830, pid=OTHER_PID + 5))
    api.foreground_hwnd = 0x830
    for _ in range(4):
        assert wdesk.key_target().blocked == "elevated"
    assert api.open_process_calls - base <= 2  # one for the verdict, at most one for the name; then cached


@pytest.mark.parametrize("level", ["denied", "no-token", HIGH])
def test_w15_an_elevated_verdict_that_was_told_is_cached(api: FakeWin32, wdesk: WindowsDesktop, level: Any) -> None:
    api.levels[OTHER_PID] = level
    wdesk.key_target()
    base = api.open_process_calls
    for _ in range(4):
        assert wdesk.key_target().blocked == "elevated"
    assert api.open_process_calls == base


def test_w15_a_process_that_cannot_be_opened_is_elevated_and_asked_again(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    del api.levels[OTHER_PID]  # exited: OpenProcess fails with "invalid parameter", not "access denied"
    assert wdesk.key_target().blocked == "elevated"
    first = api.open_process_calls
    assert wdesk.key_target().blocked == "elevated"
    assert api.open_process_calls > first  # not cached: a not-told answer is asked again
    api.levels[OTHER_PID] = MEDIUM  # it opens now
    assert wdesk.key_target().blocked is None


def test_w15_pid_zero_is_blocked_none_and_our_pid_is_own(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.windows[FOCUS].pid = 0
    assert wdesk.key_target().blocked == "none"
    api.windows[FOCUS].pid = OUR_PID
    assert wdesk.key_target().blocked == "own"


def test_w15_the_cache_is_bounded(api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win, "BLOCKED_CACHE_SIZE", 3)
    for index in range(10):
        api.add(FakeWin(0x840 + index, pid=OTHER_PID))
        api.foreground_hwnd = 0x840 + index
        wdesk.key_target()
    assert len(wdesk._key_blocked) <= 3


def _old_rule(helper: Any, target: Any) -> bool:
    """The mouse path's rule as it stood on main (ed04e05): relative to the helper's own level."""
    if helper is None or helper == "no-token":
        return False  # its own level unknown: rely on the calls' own errors
    told = target != "gone" and target is not None
    if not told:
        return False
    level = None if target in ("denied", "no-token") else target
    return level is None or level > helper


@pytest.mark.parametrize("helper", [MEDIUM, HIGH, LOW, "no-token"])
@pytest.mark.parametrize("target", [MEDIUM, 0x2100, HIGH, SYSTEM, LOW, "denied", "no-token", None])
def test_w15_the_mouse_path_s_is_blocked_is_unchanged(api: FakeWin32, helper: Any, target: Any) -> None:
    api.levels["self"] = helper
    api.levels[OTHER_PID] = target
    desktop = WindowsDesktop(api=api)
    expected = _old_rule(helper, "gone" if target is None else target)
    assert desktop._is_blocked(FOCUS) is expected


# --------------------------------------------------------------------------- W11: the shortcut modifiers


def test_w11_modifiers_down_for_each_of_ctrl_alt_and_the_two_windows_keys(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    assert wdesk.modifiers_down() is False
    for vk in (win.VK_CONTROL, win.VK_MENU, win.VK_LWIN, win.VK_RWIN):
        api.keys_down = {vk}
        assert wdesk.modifiers_down() is True, vk
        api.keys_down = set()
    assert (win.VK_CONTROL, win.VK_MENU, win.VK_LWIN, win.VK_RWIN) == (0x11, 0x12, 0x5B, 0x5C)


def test_w11_shift_and_caps_lock_are_not_shortcut_modifiers(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.keys_down = {win.VK_SHIFT, 0x41}
    api.toggled = {win.VK_CAPITAL}
    assert wdesk.modifiers_down() is False  # a capital letter is not a shortcut


def test_w11_only_the_down_bit_counts(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.GetAsyncKeyState = lambda vk: 1  # type: ignore[method-assign]  # pressed since the last call, not down now
    assert wdesk.modifiers_down() is False
    api.GetAsyncKeyState = lambda vk: -32767  # type: ignore[method-assign]  # down now (and since the last call)
    assert wdesk.modifiers_down() is True


def test_w11_a_key_state_windows_will_not_give_counts_as_down(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.broken["GetAsyncKeyState"] = OSError("no state")
    assert wdesk.modifiers_down() is True


# --------------------------------------------------------------------------- W10: the vk path


def vk_events(api: FakeWin32, wdesk: WindowsDesktop, value: str) -> list[tuple[int, int, int]]:
    assert wdesk.send_keys([char(value)], inject="vk") == 1
    return api.sent_keys[-1]


def test_w10_a_letter_is_its_key_on_the_targets_layout(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert vk_events(api, wdesk, "a") == [(0x41, 0x1E, 0), (0x41, 0x1E, UP)]
    assert vk_events(api, wdesk, ",") == [(0xBC, 0x33, 0), (0xBC, 0x33, UP)]
    assert api.vk_queries[0] == ("a", US_HKL)


def test_w10_a_capital_and_a_question_mark_bring_our_own_shift_in_the_same_call(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    assert vk_events(api, wdesk, "A") == [
        (VK_LSHIFT, 0x2A, 0),
        (0x41, 0x1E, 0),
        (0x41, 0x1E, UP),
        (VK_LSHIFT, 0x2A, UP),
    ]
    events = vk_events(api, wdesk, "?")
    assert events[0] == (VK_LSHIFT, 0x2A, 0) and events[-1] == (VK_LSHIFT, 0x2A, UP) and len(events) == 4
    assert api.key_requests[-1] == 4  # one call, so a physical key cannot fall between Shift and the letter


def test_w10_the_layout_asked_is_the_foreground_threads_and_a_hebrew_window_types_its_own_letters(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.windows[FOCUS].tid = 501
    api.layouts[501] = HE_HKL
    assert vk_events(api, wdesk, "ש") == [(0x41, 0x1E, 0), (0x41, 0x1E, UP)]  # the A key on a Hebrew layout
    assert api.layout_queries == [501] and ("ש", HE_HKL) in api.vk_queries


def test_w10_a_character_the_layout_has_no_key_for_falls_back_to_unicode(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.windows[FOCUS].tid = 501
    api.layouts[501] = HE_HKL
    assert vk_events(api, wdesk, "a") == [(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]  # no Latin 'a' on that layout
    api.layouts[501] = US_HKL
    assert vk_events(api, wdesk, "ש") == [(0, ord("ש"), UNI), (0, ord("ש"), UNI | UP)]


@pytest.mark.parametrize("answer", [0x0641 | 0x0600, 0x0141 | 0x0200, 0x0141 | 0x0400, 0x0141 | 0x0800])
def test_w10_a_layout_that_needs_ctrl_alt_or_hankaku_falls_back_to_unicode(
    api: FakeWin32, wdesk: WindowsDesktop, answer: int
) -> None:
    api.key_maps[US_HKL]["A"] = answer  # AltGr layouts say Ctrl+Alt (6)
    assert vk_events(api, wdesk, "A") == [(0, ord("A"), UNI), (0, ord("A"), UNI | UP)]


def test_w10_a_physical_shift_suppresses_ours_and_never_changes_the_character(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    api.keys_down = {win.VK_SHIFT}
    assert vk_events(api, wdesk, "A") == [(0x41, 0x1E, 0), (0x41, 0x1E, UP)]  # the user's Shift supplies the capital
    # a lower-case letter would come out upper case under the user's Shift: Unicode ignores it
    assert vk_events(api, wdesk, "a") == [(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]


def test_w10_caps_lock_flips_the_shift_a_letter_needs(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.toggled = {win.VK_CAPITAL}
    assert vk_events(api, wdesk, "a") == [
        (VK_LSHIFT, 0x2A, 0),
        (0x41, 0x1E, 0),
        (0x41, 0x1E, UP),
        (VK_LSHIFT, 0x2A, UP),
    ]
    assert vk_events(api, wdesk, "A") == [(0x41, 0x1E, 0), (0x41, 0x1E, UP)]
    assert vk_events(api, wdesk, ",") == [(0xBC, 0x33, 0), (0xBC, 0x33, UP)]  # a mark has no case
    api.windows[FOCUS].tid = 501
    api.layouts[501] = HE_HKL
    assert vk_events(api, wdesk, "ש") == [(0x41, 0x1E, 0), (0x41, 0x1E, UP)]  # nor has a Hebrew letter


def test_w10_a_key_without_a_scan_code_falls_back_to_unicode(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    del api.scan_codes[0x41]
    assert vk_events(api, wdesk, "a") == [(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]


def test_w10_with_no_foreground_thread_the_character_goes_in_as_unicode(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.foreground_hwnd = 0
    assert vk_events(api, wdesk, "a") == [(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]


def test_w10_a_layout_windows_will_not_name_never_falls_back_to_ours(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.default_layout = 0  # GetKeyboardLayout answers NULL; VkKeyScanExW(NULL) would read OUR thread's layout
    assert vk_events(api, wdesk, "a") == [(0, ord("a"), UNI), (0, ord("a"), UNI | UP)]
    assert api.vk_queries == []


def test_w10_unicode_mode_asks_nothing_about_the_layout_or_the_keys(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.send_keys([char("a"), control("space")])
    assert api.layout_queries == [] and api.vk_queries == []
    assert api.calls["GetAsyncKeyState"] == 0 and api.calls["GetKeyState"] == 0


def test_w10_a_control_is_the_same_in_both_modes(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    wdesk.send_keys([control("backspace")], inject="vk")
    wdesk.send_keys([control("backspace")], inject="unicode")
    assert api.sent_keys[0] == api.sent_keys[1] == [(0x08, 0x0E, 0), (0x08, 0x0E, UP)]


@pytest.mark.parametrize("value", sorted(ALLOWED_CHARS))
def test_w10_every_allowed_character_makes_a_balanced_batch_in_either_mode(
    api: FakeWin32, wdesk: WindowsDesktop, value: str
) -> None:
    api.key_maps[US_HKL].update(api.key_maps[HE_HKL])
    wdesk.send_keys([char(value)], inject="unicode")
    wdesk.send_keys([char(value)], inject="vk")
    assert all(events_balanced(events) for events in api.sent_keys)


# --------------------------------------------------------------------------- W14: the on-screen keyboard


def test_w14_open_os_keyboard_runs_osk_by_its_absolute_path(
    api: FakeWin32, wdesk: WindowsDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SYSTEMROOT", raising=False)
    assert wdesk.open_os_keyboard() is True
    assert api.shell_executes == [(None, "open", r"C:\Windows\System32\osk.exe", None, None, win.SW_SHOWNORMAL)]
    monkeypatch.setenv("SYSTEMROOT", r"D:\WinNT")
    wdesk.open_os_keyboard()
    assert api.shell_executes[-1][2] == r"D:\WinNT\System32\osk.exe"
    assert win.SW_SHOWNORMAL == 1


@pytest.mark.parametrize(
    ("result", "started"), [(42, True), (33, True), (32, False), (2, False), (0, False), (None, False)]
)
def test_w14_a_result_above_32_means_it_started(
    api: FakeWin32, wdesk: WindowsDesktop, result: int | None, started: bool
) -> None:
    api.shell_result = result
    assert wdesk.open_os_keyboard() is started


def test_w14_a_launcher_that_raises_is_not_started(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    api.broken["ShellExecuteW"] = OSError("no shell")
    assert wdesk.open_os_keyboard() is False


# --------------------------------------------------------------------------- the protocol, the names, privacy


def test_the_windows_desktop_is_a_key_desktop(api: FakeWin32, wdesk: WindowsDesktop) -> None:
    assert as_key_desktop(wdesk) is wdesk
    assert wdesk.injects_for_real is True and WindowsDesktop.injects_for_real is True
    assert FakeDesktop().injects_for_real is False


def test_the_windows_desktop_answers_the_key_desktop_protocol_like_the_fake_does(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    desktops: list[KeyDesktop] = [wdesk, FakeDesktop(injects_for_real=True)]
    for desktop in desktops:
        assert desktop.send_keys([char("a"), control("space")]) == 2
        assert isinstance(desktop.key_target(), KeyTarget)
        assert isinstance(desktop.foreground_window(), int)
        assert desktop.foreign_input() in (True, False)
        assert desktop.modifiers_down() in (True, False)
        desktop.release_keys()


def test_w41_a_two_hundred_character_run_is_two_hundred_balanced_tagged_calls(
    api: FakeWin32, wdesk: WindowsDesktop
) -> None:
    rng = random.Random(41)
    pool = sorted(ALLOWED_CHARS)
    strokes = [control("space") if rng.random() < 0.15 else char(rng.choice(pool)) for _ in range(200)]
    for stroke in strokes:  # the run lane sends one stroke per call
        assert wdesk.send_keys([stroke]) == 1
    assert api.calls["SendInput"] == 200 and len(api.sent_keys) == 200
    assert all(len(events) == 2 and events_balanced(events) for events in api.sent_keys)
    assert api.key_tags == [(0, win.EXTRA_INFO_TAG)] * 400
    assert api.calls["OpenProcess"] == 0  # sending needs no target probe: the sink does that


def test_sr13_nothing_the_desktop_raises_or_logs_holds_a_typed_character(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    messages: list[str] = []
    with caplog.at_level(logging.DEBUG, logger=win.__name__):
        for script in ([0], [1], [1, 0], [2, 1]):
            api.send_script = list(script)
            try:
                wdesk.send_keys([char(SENTINEL), char(HE_SENTINEL)], inject="vk")
            except (OSError, InputBlocked) as exc:
                messages.append(str(exc))
        wdesk.release_keys()
        wdesk.close()
        wdesk.key_target()
    text = "\n".join(messages) + caplog.text
    assert messages
    assert SENTINEL not in text and HE_SENTINEL not in text
    assert "notepad" not in caplog.text  # nor the name of the program


def test_the_elevated_notice_is_logged_once_per_window_without_a_name(
    api: FakeWin32, wdesk: WindowsDesktop, caplog: pytest.LogCaptureFixture
) -> None:
    api.levels[OTHER_PID] = HIGH
    with caplog.at_level(logging.INFO, logger=win.__name__):
        for _ in range(20):
            wdesk.key_target()
    notices = [r for r in caplog.records if "never sent" in r.getMessage()]
    assert len(notices) == 1 and "notepad" not in caplog.text
