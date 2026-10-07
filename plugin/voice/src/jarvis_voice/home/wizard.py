"""The home setup wizard: a console where the user adds, names and tries devices.

It runs in its own console window (``launch_in_new_console``), opened by
``/jarvis home setup`` or by asking Jarvis to "open home setup". PINs, keys
and tokens are typed here and go straight into the credential store, so they
never pass through Claude Code, its transcript or the model.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import WIZARD_STEPS, DriverContext, Prompter, load_object
from .model import TIERS, DeviceRecord, HomeConfig
from .service import HomeService
from .store import HomeStore, StoreError

log = logging.getLogger(__name__)

QR_FILE = "pairing-qr.png"


class ConsolePrompter:
    """Prompter on the real console (input/getpass), with QR codes drawn in text and saved as an image."""

    def __init__(self, scratch_dir: Path) -> None:
        self._scratch = scratch_dir
        self._qr_files: list[Path] = []

    def say(self, text: str) -> None:
        print(text, flush=True)

    def ask(self, question: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{question}{suffix}: ").strip()
        return answer or (default or "")

    def ask_secret(self, question: str) -> str:
        return getpass.getpass(f"{question} (hidden as you type): ").strip()

    def choose(self, question: str, options: list[str]) -> int | None:
        print(f"\n{question}")
        for i, option in enumerate(options, 1):
            print(f"  {i}) {option}")
        print("  0) Back")
        while True:
            answer = input("> ").strip()
            if answer in ("0", "", "b", "back", "q"):
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
            print(f"Type a number from 0 to {len(options)}.")

    def confirm(self, question: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = input(f"{question} [{hint}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def show_qr(self, data: str, caption: str) -> None:
        import segno

        qr = segno.make(data, error="m")
        print(f"\n{caption}\n")
        try:
            qr.terminal(compact=True, border=2)
        except (UnicodeEncodeError, OSError):
            qr.terminal(border=2)
        try:
            self._scratch.mkdir(parents=True, exist_ok=True)
            path = self._scratch / QR_FILE
            qr.save(str(path), scale=8, border=4)
            self._qr_files.append(path)
            _open_file(path)
            print(f"\n(The same code is open as an image: {path})")
        except OSError as exc:
            log.debug("could not save the QR image: %s", exc)

    def close(self) -> None:
        for path in self._qr_files:
            with contextlib.suppress(OSError):
                path.unlink()
        self._qr_files.clear()


def _open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------- the menu


def run_wizard(data_dir: Path, ui: Prompter | None = None, *, service: HomeService | None = None) -> int:
    """The interactive menu. Returns a process exit code."""
    console = ui is None
    if console and not all(stream is not None and stream.isatty() for stream in (sys.stdin, sys.stdout)):
        # It asks for PINs, codes and keys: a person types them at a console,
        # never a program (Claude's shell included) through a pipe.
        print("Jarvis home setup asks for codes and keys: run it in a terminal window of your own.", file=sys.stderr)
        return 2
    prompter: Any = ui if ui is not None else ConsolePrompter(Path(data_dir) / "home")
    home = service or HomeService(Path(data_dir))
    store = home.store
    sealed = "encrypted for your Windows account" if store.codec.name == "dpapi" else "readable only by you"
    prompter.say("Jarvis home setup")
    prompter.say(f"Devices are saved in {store.root} (credentials {sealed}).")
    if console and sys.platform == "win32":
        prompter.say(
            "If Windows Firewall asks about Python, allow it on Private networks only: "
            "Jarvis uses it to find devices on your home network."
        )
    menu = [label for _, label, _ in WIZARD_STEPS] + [
        "Find smart devices on my network",
        "Show my devices",
        "Rename a device, set its room, or remove it",
        "Ask before Jarvis uses a device",
        "Try a device",
    ]
    try:
        while True:
            choice = prompter.choose("What would you like to do?", menu)
            if choice is None:
                prompter.say("Done. Jarvis picks up the changes straight away.")
                return 0
            try:
                if choice < len(WIZARD_STEPS):
                    _driver, label, path = WIZARD_STEPS[choice]
                    try:
                        step = load_object(path)
                    except ImportError as exc:
                        prompter.say(
                            f"{label} is not available: {exc}. Run /jarvis setup in Claude Code to repair Jarvis."
                        )
                        continue
                    step(prompter, home.ctx)
                    home.handle({"action": "reload"})
                else:
                    extra = choice - len(WIZARD_STEPS)
                    [_find, _show, _edit, _confirm, _try][extra](prompter, home)
            except KeyboardInterrupt:
                prompter.say("\nCancelled.")
            except StoreError as exc:
                prompter.say(f"Could not save: {exc}")
            except EOFError:
                raise
            except Exception as exc:
                log.exception("wizard step failed")
                prompter.say(f"That step failed ({type(exc).__name__}). The helper's log has the details.")
    except (EOFError, KeyboardInterrupt):
        prompter.say("")
        return 0
    finally:
        if console:
            prompter.close()
        if service is None:
            home.close()


def _pick_device(ui: Prompter, config: HomeConfig, question: str) -> DeviceRecord | None:
    if not config.devices:
        ui.say("No devices yet.")
        return None
    index = ui.choose(question, [_label(d) for d in config.devices])
    return None if index is None else config.devices[index]


def _label(device: DeviceRecord) -> str:
    room = f", {device.room}" if device.room else ""
    asks = "; asks first" if device.confirm == "screen" else ""
    return f"{device.name} ({device.kind}{room}){asks}"


def _find(ui: Prompter, home: HomeService) -> None:
    # Here, in front of the user, so a Windows Firewall question about Python comes up where they can answer it.
    ui.say("Looking for smart devices on your network. This takes about ten seconds.")
    ui.say(home.handle({"action": "scan"})["text"])


def _show(ui: Prompter, home: HomeService) -> None:
    ui.say(home.handle({"action": "list"})["text"])


def _edit(ui: Prompter, home: HomeService) -> None:
    device = _pick_device(ui, home.store.load(), "Which device?")
    if device is None:
        return
    action = ui.choose(f"{device.name}:", ["Rename", "Set the room", "Set other names (aliases)", "Remove it"])
    if action is None:
        return
    if action == 0:
        name = ui.ask("New name", device.name)
        home.store.update(lambda c: _patch(c, device.id, name=name.strip() or device.name))
    elif action == 1:
        room = ui.ask("Room (empty for none)", device.room or "")
        home.store.update(lambda c: _patch(c, device.id, room=room.strip() or None))
    elif action == 2:
        text = ui.ask("Other names, separated by commas", ", ".join(device.aliases))
        aliases = [a.strip() for a in text.split(",") if a.strip()]
        home.store.update(lambda c: _patch(c, device.id, aliases=aliases))
    elif ui.confirm(f"Remove {device.name}? Jarvis forgets it and its credentials.", default=False):
        home.store.update(lambda c: c.remove(device.id))
        home.store.set_secret(device.secret_key, None)
        ui.say(f"Removed {device.name}.")
        return
    ui.say("Saved.")


def _patch(config: HomeConfig, device_id: str, **changes: Any) -> None:
    device = config.device(device_id)
    if device is None:
        return
    for key, value in changes.items():
        setattr(device, key, value)


def _confirm(ui: Prompter, home: HomeService) -> None:
    device = _pick_device(ui, home.store.load(), "Which device should Jarvis ask about first?")
    if device is None:
        return
    now = {"screen": "asks you on screen first", "never": "never allowed"}.get(device.confirm or "", "just does it")
    ui.say(f"Now: {now}.")
    index = ui.choose(
        f"When Jarvis is asked to use {device.name}:",
        ["Just do it", "Ask me on screen first", "Never (Jarvis may only read its state)"],
    )
    if index is None:
        return
    tier = TIERS[index]
    home.store.update(lambda c: _patch(c, device.id, confirm=None if tier == "free" else tier))
    ui.say("Saved.")


def _try(ui: Prompter, home: HomeService) -> None:
    listed = home.handle({"action": "list"})
    config = home.store.load()
    if not config.devices and listed.get("count", 0) == 0:
        ui.say(listed["text"])
        return
    device = _pick_device(ui, config, "Which device?") if config.devices else None
    wanted = device.id if device is not None else ui.ask("Device name")
    if not wanted:
        return
    status = home.handle({"action": "status", "device": wanted})
    ui.say(status["text"])
    # Typing the command here is the user's own confirmation, for the device whose state was just shown.
    shown = status.get("device")
    if isinstance(shown, dict) and isinstance(shown.get("id"), str) and shown["id"]:
        wanted = shown["id"]
    command = ui.ask("Command to try (empty to skip), e.g. turn_on, set_volume")
    if not command:
        return
    value = ui.ask("Value (empty for none)") or None
    result = home.handle({"action": "do", "device": wanted, "command": command, "value": value, "confirmed": True})
    ui.say(result["text"])


# --------------------------------------------------------------------------- opening a window


def setup_argv(data_dir: Path) -> list[str]:
    return [sys.executable, "-m", "jarvis_voice", "home", "setup", "--data-dir", str(data_dir)]


# The window must not hold the caller's pipes open, or the mod waits until the window closes.
_DETACHED_STREAMS: dict[str, Any] = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
}


def launch_in_new_console(data_dir: Path) -> tuple[bool, str]:
    """Opens the wizard in a console window of its own. Returns (opened, what to tell the user)."""
    argv = setup_argv(data_dir)
    manual = shlex.join(argv) if sys.platform != "win32" else subprocess.list2cmdline(argv)
    try:
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            breakaway = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB: outlive the window that asked
            try:
                subprocess.Popen(argv, creationflags=flags | breakaway, close_fds=True, cwd=str(data_dir))
            except OSError:
                subprocess.Popen(argv, creationflags=flags, close_fds=True, cwd=str(data_dir))
            return True, "The Jarvis home setup window is open on your desktop."
        if sys.platform == "darwin":
            script = f'tell application "Terminal" to do script {_applescript_string(manual)}'
            subprocess.Popen(
                ["osascript", "-e", script, "-e", 'tell application "Terminal" to activate'], **_DETACHED_STREAMS
            )
            return True, "The Jarvis home setup window is open in Terminal."
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
                if shutil.which(terminal) is None:
                    continue
                extra = ["--"] if terminal == "gnome-terminal" else ["-e"]
                subprocess.Popen([terminal, *extra, *argv], start_new_session=True, **_DETACHED_STREAMS)
                return True, "The Jarvis home setup window is open."
    except OSError as exc:
        log.warning("could not open the setup window: %s", exc)
    return False, f"Open a terminal and run: {manual}"


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = ["ConsolePrompter", "DriverContext", "HomeStore", "launch_in_new_console", "run_wizard", "setup_argv"]
