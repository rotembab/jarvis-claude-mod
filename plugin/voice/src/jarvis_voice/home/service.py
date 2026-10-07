"""The ``home`` command: finds the device a request names, checks the command and
its tier, and runs it on the device's driver with a lock and a time limit.

Every answer is ``{"ok": true, "result": "done"|"failed"|"confirm", "code",
"text", ...}``: ``ok`` says the helper handled the request, ``result`` how it
went, and ``text`` is the plain-words summary Claude reads (and may speak).
"confirm" means the command needs the user's OK on screen first: the mod
asks, then sends the same request with ``confirmed: true``.
"""

from __future__ import annotations

import concurrent.futures
import difflib
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from .base import DRIVERS, LABELS, Driver, DriverContext, load_object, quiet_device_loggers
from .model import (
    TIERS,
    Code,
    CommandSpec,
    DeviceRecord,
    Outcome,
    Tier,
    Value,
    canonical_command,
    normalize,
    parse_value,
)
from .store import HomeStore, StoreError

log = logging.getLogger(__name__)

HOME_CALL_TIMEOUT_S = 20.0
LIST_LIMIT = 80
SETUP_HINT = "Run /jarvis home setup, or ask me to open home setup, to add devices."

# Spoken names for kinds, so "the TV" or "the lights" find a device.
KIND_WORDS: dict[str, tuple[str, ...]] = {
    "tv": ("tv", "television", "telly"),
    "media_player": ("media player", "streamer", "player"),
    "speaker": ("speaker",),
    "light": ("light", "lamp", "bulb"),
    "plug": ("plug", "socket", "outlet"),
    "switch": ("switch",),
    "fan": ("fan",),
    "cover": ("curtain", "curtains", "blind", "blinds", "shutter", "shade"),
    "climate": ("ac", "air conditioner", "aircon", "air conditioning", "thermostat", "heat pump"),
    "heater": ("heater", "boiler"),
    "humidifier": ("humidifier", "dehumidifier"),
    "lock": ("lock", "door lock"),
    "alarm": ("alarm",),
    "garage": ("garage", "garage door", "gate"),
    "vacuum": ("vacuum", "robot", "hoover"),
    "scene": ("scene",),
}


def _reply(result: str, code: Code, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "result": result,
        "code": code,
        "text": text,
        **{k: v for k, v in extra.items() if v is not None},
    }


def _from_outcome(outcome: Outcome, device: DeviceRecord | None = None) -> dict[str, Any]:
    ref = {"id": device.id, "name": device.name} if device is not None else None
    return _reply("done" if outcome.ok else "failed", outcome.code, outcome.text, device=ref)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strictest(*tiers: Tier | None) -> Tier:
    return max((t for t in tiers if t is not None), key=TIERS.index, default="free")


def _the(device: DeviceRecord) -> str:
    """ "the Sony TV", but "Movie night" for a scene (it reads as a name)."""
    return device.name if device.kind == "scene" else f"the {device.name}"


class HomeService:
    def __init__(
        self,
        data_dir: Path,
        *,
        store: HomeStore | None = None,
        drivers: Mapping[str, str | Callable[[DriverContext], Driver]] | None = None,
        call_timeout: float = HOME_CALL_TIMEOUT_S,
    ) -> None:
        quiet_device_loggers()
        self.store = store or HomeStore(Path(data_dir))
        self.ctx = DriverContext(self.store, Path(data_dir), call_timeout)
        # Driver classes by name, as import paths (or, in tests, factories).
        self._paths: dict[str, str | Callable[[DriverContext], Driver]] = dict(DRIVERS if drivers is None else drivers)
        self._drivers: dict[str, Driver] = {}
        self._broken: dict[str, str] = {}
        self._mutex = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="home")

    # ------------------------------------------------------------------ entry point

    def handle(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Answers one ``home`` command; never raises."""
        action = body.get("action")
        try:
            if action == "list":
                return self._list(_text(body.get("query")))
            if action == "status":
                return self._status(_text(body.get("device")))
            if action == "do":
                return self._do(
                    _text(body.get("device")),
                    _text(body.get("command")),
                    body.get("value"),
                    body.get("confirmed") is True,
                )
            if action == "info":
                return self._info()
            if action == "reload":
                self._reset()
                return self._info()
            return _reply("failed", "bad_value", f"Unknown home action {action!r}.")
        except StoreError as exc:
            return _reply("failed", "needs_setup", f"Home control could not read its settings: {exc}")
        except Exception:
            log.exception("home %s failed", action)
            return _reply("failed", "failed", "Home control hit an internal error; the helper's log has the details.")

    def close(self) -> None:
        with self._mutex:
            drivers = list(self._drivers.values())
            self._drivers.clear()
        for driver in drivers:
            try:
                driver.close()
            except Exception:
                log.debug("closing the %s driver failed", driver.name, exc_info=True)
        self.ctx.close()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _reset(self) -> None:
        with self._mutex:
            drivers = list(self._drivers.values())
            self._drivers.clear()
            self._broken.clear()
        for driver in drivers:
            try:
                driver.close()
            except Exception:
                log.debug("closing the %s driver failed", driver.name, exc_info=True)

    # ------------------------------------------------------------------ actions

    def _list(self, query: str) -> dict[str, Any]:
        devices, notes = self._devices()
        if not devices:
            text = f"No home devices are set up yet. {SETUP_HINT}"
            return _reply("done", "ok", " ".join([text, *notes]), count=0)
        shown = self._filter(devices, query) if query else devices
        if not shown:
            names = ", ".join(d.name for d in devices[:15])
            return _reply("done", "ok", f'No device matches "{query}". Devices: {names}.', count=0)
        rows: list[str] = []
        asks = False
        for device in sorted(shown[:LIST_LIMIT], key=lambda d: ((d.room or "~").lower(), d.name.lower())):
            driver = self._driver(device.driver)
            if isinstance(driver, Outcome):
                usage = f"unavailable: {driver.text}"
            else:
                parts = []
                for spec in self._specs(driver, device).values():
                    tier = _strictest(spec.tier, device.confirm)
                    if tier == "never":
                        continue
                    asks = asks or tier == "screen"
                    parts.append(spec.usage() + ("*" if tier == "screen" else ""))
                usage = ", ".join(parts) or "no commands"
            where = f", {device.room}" if device.room else ""
            rows.append(f"- {device.name} ({device.kind}{where}) id={device.id}: {usage}")
        header = f"{len(shown)} home device{'s' if len(shown) != 1 else ''}"
        if len(shown) > LIST_LIMIT:
            header += f" (first {LIST_LIMIT}; pass a query to narrow)"
        footer = ["* asks the user to confirm on screen first."] if asks else []
        return _reply("done", "ok", "\n".join([header + ":", *rows, *footer, *notes]), count=len(shown))

    def _status(self, wanted: str) -> dict[str, Any]:
        devices, notes = self._devices()
        found = self._resolve(devices, wanted, notes)
        if isinstance(found, dict):
            return found
        driver = self._driver(found.driver)
        if isinstance(driver, Outcome):
            return _from_outcome(driver, found)
        return _from_outcome(self._call(found, driver, lambda: driver.status(found)), found)

    def _do(self, wanted: str, command: str, value: Value, confirmed: bool) -> dict[str, Any]:
        if not command:
            return _reply("failed", "bad_value", "Which command? Use list to see what each device can do.")
        name = canonical_command(command)
        if name == "status":
            return self._status(wanted)
        devices, notes = self._devices()
        found = self._resolve(devices, wanted, notes)
        if isinstance(found, dict):
            return found
        device = found
        driver = self._driver(device.driver)
        if isinstance(driver, Outcome):
            return _from_outcome(driver, device)
        specs = self._specs(driver, device)
        spec = specs.get(name)
        if spec is None:
            can = ", ".join(s.name for s in specs.values() if _strictest(s.tier, device.confirm) != "never")
            return _from_outcome(
                Outcome.fail(
                    "unsupported", f"The {device.name} can't {name.replace('_', ' ')}. It can: {can or 'nothing'}."
                ),
                device,
            )
        parsed, error = parse_value(spec, value)
        if error is not None:
            return _from_outcome(Outcome.fail("bad_value", error), device)
        tier = _strictest(spec.tier, device.confirm)
        phrase = f"{name.replace('_', ' ')} {_the(device)}" + (f" ({parsed})" if parsed is not None else "")
        if tier == "never":
            return _from_outcome(Outcome.fail("refused", f"Jarvis is not allowed to {phrase}."), device)
        if tier == "screen" and not confirmed:
            return _reply(
                "confirm",
                "confirm",
                f"This needs the user's OK on screen: {phrase}.",
                tier="screen",
                prompt=f"Let Jarvis {phrase}?",
                device={"id": device.id, "name": device.name},
            )
        log.info("home: %s %s%s", device.id, name, "" if parsed is None else " (with a value)")
        return _from_outcome(self._call(device, driver, lambda: driver.run(device, name, parsed)), device)

    def _info(self) -> dict[str, Any]:
        config = self.store.load()
        counts: dict[str, int] = {}
        for device in config.devices:
            counts[device.driver] = counts.get(device.driver, 0) + 1
        lines = []
        if counts:
            lines.append(
                "Devices: "
                + ", ".join(f"{LABELS.get(k, k)} {n}" for k, n in sorted(counts.items(), key=lambda kv: kv[0]))
            )
        else:
            lines.append("No home devices are set up yet.")
        for hub in config.hubs:
            driver = self._driver(hub)
            line = (
                driver.text
                if isinstance(driver, Outcome)
                else (driver.describe() or f"{LABELS.get(hub, hub)} connected")
            )
            lines.append(line)
        for name in sorted(set(counts) - set(config.hubs)):
            driver = self._driver(name)
            if isinstance(driver, Outcome):
                lines.append(driver.text)
            elif (line := driver.describe()) is not None:
                lines.append(line)
        sealed = "encrypted for your Windows account" if self.store.codec.name == "dpapi" else "readable only by you"
        lines.append(f"Devices file: {self.store.devices_path}")
        lines.append(f"Credentials: {self.store.secrets_path} ({sealed})")
        return _reply(
            "done",
            "ok",
            "\n".join(lines),
            devicesFile=str(self.store.devices_path),
            credentialsFile=str(self.store.secrets_path),
            count=sum(counts.values()),
        )

    # ------------------------------------------------------------------ helpers

    def _driver(self, name: str) -> Driver | Outcome:
        label = LABELS.get(name, name)
        with self._mutex:
            if name in self._drivers:
                return self._drivers[name]
            if name in self._broken:
                return Outcome.fail("unsupported", self._broken[name])
            path = self._paths.get(name)
            if path is None:
                return Outcome.fail("unsupported", f"Jarvis does not know how to control {label} devices.")
            try:
                factory = load_object(path) if isinstance(path, str) else path
                driver: Driver = factory(self.ctx)
            except ImportError as exc:
                log.warning("the %s driver cannot be imported: %s", name, exc)
                self._broken[name] = (
                    f"{label} control is not installed here ({exc.name or exc}); run /jarvis setup to repair it."
                )
                return Outcome.fail("unsupported", self._broken[name])
            except Exception as exc:
                log.exception("the %s driver failed to start", name)
                self._broken[name] = f"{label} control failed to start ({type(exc).__name__})."
                return Outcome.fail("failed", self._broken[name])
            self._drivers[name] = driver
            return driver

    def _devices(self) -> tuple[list[DeviceRecord], list[str]]:
        """The saved devices plus each hub's, and a note for every hub that could not answer."""
        config = self.store.load()
        devices = list(config.devices)
        notes: list[str] = []
        for hub in config.hubs:
            driver = self._driver(hub)
            if isinstance(driver, Outcome):
                notes.append(driver.text)
                continue
            future = self._pool.submit(driver.hub_devices)
            try:
                devices.extend(future.result(timeout=self.ctx.call_timeout))
            except concurrent.futures.TimeoutError:
                notes.append(f"{LABELS.get(hub, hub)} did not answer in time, so its devices are missing.")
            except Exception as exc:
                log.warning("%s devices: %s", hub, type(exc).__name__, exc_info=True)
                notes.append(f"{LABELS.get(hub, hub)} could not be reached, so its devices are missing.")
        seen: set[str] = set()
        unique = []
        for device in devices:
            if device.id not in seen:
                seen.add(device.id)
                unique.append(device)
        return unique, notes

    def _specs(self, driver: Driver, device: DeviceRecord) -> dict[str, CommandSpec]:
        try:
            return {spec.name: spec for spec in driver.commands(device)}
        except Exception:
            log.warning("commands for %s failed", device.id, exc_info=True)
            return {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._mutex:
            return self._locks.setdefault(key, threading.Lock())

    def _call(self, device: DeviceRecord, driver: Driver, fn: Callable[[], Outcome]) -> Outcome:
        """Runs a driver call on a worker with the device's lock, within the time limit."""
        try:
            key = driver.lock_key(device)
        except Exception:  # noqa: BLE001
            key = device.id
        lock = self._lock_for(key)
        limit = self.ctx.call_timeout

        def work() -> Outcome:
            if not lock.acquire(timeout=limit):
                return Outcome.fail("busy", f"The {device.name} is still busy with the last request.")
            try:
                return fn()
            finally:
                lock.release()

        future = self._pool.submit(work)
        try:
            # A little past the lock wait, so a busy device answers "busy", not "timeout".
            outcome = future.result(timeout=limit + min(2.0, limit / 4))
        except concurrent.futures.TimeoutError:
            return Outcome.fail("timeout", f"The {device.name} did not answer in time; it may still act on it.")
        except Exception as exc:
            log.error("%s on %s failed: %s", driver.name, device.id, type(exc).__name__, exc_info=True)
            return Outcome.fail("failed", f"Controlling the {device.name} failed ({type(exc).__name__}).")
        if not isinstance(outcome, Outcome):
            return Outcome.fail("failed", f"Controlling the {device.name} gave no answer.")
        return outcome

    # ------------------------------------------------------------------ names

    @staticmethod
    def _labels(device: DeviceRecord) -> set[str]:
        names = {normalize(device.name), *(normalize(a) for a in device.aliases)}
        labels = set(names)
        if device.room:
            room = normalize(device.room)
            labels |= {f"{room} {n}" for n in names}
            labels |= {f"{n} in {room}" for n in names}
            labels |= {f"{room} {w}" for w in KIND_WORDS.get(device.kind, ())}
        return {label for label in labels if label}

    @staticmethod
    def _kind_words(device: DeviceRecord) -> set[str]:
        words = set(KIND_WORDS.get(device.kind, ()))
        words.add(normalize(device.kind.replace("_", " ")))
        return words

    def _filter(self, devices: Iterable[DeviceRecord], query: str) -> list[DeviceRecord]:
        target = normalize(query)
        words = set(target.split())
        found = []
        for device in devices:
            haystack = " ".join([*self._labels(device), *self._kind_words(device), normalize(device.driver)])
            if target in haystack or words <= set(haystack.split()):
                found.append(device)
        return found

    def _resolve(self, devices: list[DeviceRecord], wanted: str, notes: list[str]) -> DeviceRecord | dict[str, Any]:
        if not devices:
            return _reply("failed", "needs_setup", " ".join([f"No home devices are set up yet. {SETUP_HINT}", *notes]))
        if not wanted:
            return _reply("failed", "bad_value", "Which device? Use list to see them.")
        for device in devices:
            if device.id == wanted:
                return device
        target = normalize(wanted)
        singular = target[:-1] if target.endswith("s") else target

        def pick(candidates: list[DeviceRecord]) -> DeviceRecord | dict[str, Any] | None:
            unique = list({d.id: d for d in candidates}.values())
            if len(unique) == 1:
                return unique[0]
            if len(unique) > 1:
                names = ", ".join(f"{d.name}{f' ({d.room})' if d.room else ''}" for d in unique[:8])
                return _reply("failed", "ambiguous", f'"{wanted}" could mean: {names}. Which one?')
            return None

        steps: list[Callable[[DeviceRecord], bool]] = [
            lambda d: target in self._labels(d),
            lambda d: target in self._kind_words(d) or singular in self._kind_words(d),
            lambda d: any(set(target.split()) <= set(label.split()) for label in self._labels(d)),
        ]
        for matches in steps:
            chosen = pick([d for d in devices if matches(d)])
            if chosen is not None:
                return chosen
        by_label: dict[str, list[DeviceRecord]] = {}
        for device in devices:
            for label in self._labels(device):
                by_label.setdefault(label, []).append(device)
        close = difflib.get_close_matches(target, list(by_label), n=4, cutoff=0.75)
        chosen = pick([d for label in close for d in by_label[label]])
        if chosen is not None:
            return chosen
        names = ", ".join(d.name for d in devices[:12])
        text = f'There is no device called "{wanted}". Devices: {names}.'
        return _reply("failed", "not_found", " ".join([text, *notes]))
