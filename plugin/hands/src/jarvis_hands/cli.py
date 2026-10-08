"""Command line: ``run`` (the helper the mod starts), ``setup`` (model download), ``doctor`` and ``preview``.

``run`` is wired like the voice helper's: stdout is claimed for protocol lines
before anything else can write to it, the single-instance lock and the token
are settled before the camera is touched, and every way out (shutdown command,
missed heartbeats, a broken stdout pipe, Ctrl+C) goes through the runtime's
``request_stop`` so the mouse buttons are released and the camera closed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, protocol
from . import platform as plat
from .events import EventWriter, encode_line
from .lifecycle import DEFAULT_INSTANCE_NAME, HeartbeatWatchdog, acquire_instance_lock
from .logs import secret_filter, setup_logging
from .platform import InstanceLock

log = logging.getLogger("jarvis_hands")

FAKE_TOKEN = "jarvis-fake-token"
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_ALREADY_RUNNING = 0, 1, 2, 3


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def default_data_dir() -> Path:
    # Path.home() reads USERPROFILE on Windows, HOME elsewhere.
    return Path(_env("JARVIS_DATA_DIR") or Path.home() / ".jarvis")


def _data_dir(args: argparse.Namespace) -> Path:
    data_dir: Path = (args.data_dir or default_data_dir()).expanduser()
    return data_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis_hands", description="Jarvis hand-gesture helper for Claude Code.")
    parser.add_argument("--version", action="version", version=f"jarvis-hands {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the hand helper (started by the Jarvis mod)")
    run.add_argument("--data-dir", type=Path, default=None)
    run.add_argument(
        "--camera", default=_env("JARVIS_HANDS_CAMERA"), help="camera index, or part of its name (default: the first)"
    )
    run.add_argument("--width", type=int, default=1280, help="requested camera width")
    run.add_argument("--height", type=int, default=720, help="requested camera height")
    run.add_argument("--fps", type=int, default=30, help="requested camera frame rate")
    run.add_argument("--no-overlay", action="store_true", help="do not draw the reticle on screen")
    run.add_argument("--fake", action="store_true", help="scripted camera, tracker and desktop; no hardware (tests)")
    run.add_argument("--fake-script", type=Path, default=None, help="JSON lines of frames for --fake")
    run.add_argument("--instance-name", default=_env("JARVIS_HANDS_INSTANCE_NAME") or DEFAULT_INSTANCE_NAME)
    run.add_argument("--heartbeat-timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    run.add_argument("--heartbeat-grace", type=float, default=60.0, help=argparse.SUPPRESS)
    run.add_argument("--log-level", default=_env("JARVIS_LOG_LEVEL") or "INFO")
    run.set_defaults(func=cmd_run)

    setup = sub.add_parser("setup", help="download the hand model (JSON-lines progress on stdout)")
    setup.add_argument("--data-dir", type=Path, default=None)
    setup.add_argument("--model-url", default=None, help=argparse.SUPPRESS)
    setup.add_argument("--model-sha256", default=None, help=argparse.SUPPRESS)
    setup.set_defaults(func=cmd_setup)

    doctor = sub.add_parser("doctor", help="print a JSON health report")
    doctor.add_argument("--data-dir", type=Path, default=None)
    doctor.add_argument("--no-camera", action="store_true", help="skip opening the camera")
    doctor.set_defaults(func=cmd_doctor)

    preview = sub.add_parser("preview", help="show the camera with the tracked hands drawn on it")
    preview.add_argument("--data-dir", type=Path, default=None)
    preview.add_argument("--camera", default=_env("JARVIS_HANDS_CAMERA"), help="camera index, or part of its name")
    preview.add_argument("--width", type=int, default=1280)
    preview.add_argument("--height", type=int, default=720)
    preview.add_argument("--fps", type=int, default=30)
    preview.set_defaults(func=cmd_preview)
    return parser


def _claim_stdout() -> BinaryIO:
    """Keep stdout for protocol lines only; everything else written to it lands on stderr.

    The protocol gets a private duplicate of file descriptor 1, and fd 1
    itself then points at stderr. That catches stray Python prints and also
    native code writing to the C-level stdout (MediaPipe's glog and TFLite
    delegates, OpenCV's camera backends), which would otherwise corrupt the
    JSON lines. On Windows the C runtime's dup2 onto fd 1 also resets the
    process's STD_OUTPUT_HANDLE (python.exe is a console app), so WriteFile
    users follow too.
    """
    sys.stdout.flush()
    out: BinaryIO = sys.stdout.buffer
    try:
        protocol_fd = os.dup(1)  # not inheritable: children never get the protocol stream
    except OSError:  # no usable fd 1 (e.g. pythonw): settle for the Python-level swap
        protocol_fd = None
    if protocol_fd is not None:
        try:
            os.dup2(2, 1)
        except OSError:
            os.close(protocol_fd)
        else:
            out = os.fdopen(protocol_fd, "wb")
    sys.stdout = sys.stderr
    return out


# --------------------------------------------------------------------------- run


class _NoLock:
    """Stand-in when the OS refuses the lock primitive itself."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        pass


def capabilities(fake: bool) -> list[str]:
    caps = [
        "heartbeat",
        "status",
        "config",
        "pause",
        "resume",
        "engage",
        "disengage",
        "calibrate",
        "shutdown",
        "keyboard",
    ]
    return [*caps, "fake"] if fake else caps


def cmd_run(args: argparse.Namespace) -> int:
    """The helper the mod starts. What this wiring asks of ``runtime.HandsRuntime``:

    - ``hello`` goes out and the control server takes commands *before*
      ``start()`` is called: the mod needs the port to heartbeat while the
      camera opens (seconds on Windows), and it sends ``config`` the moment it
      reads ``hello``. So ``handle_command`` must work before and while
      ``start()`` runs: keep the settings, answer ``status`` from stored
      state, apply them once the loop runs. Dropping that first ``config``
      loses the user's settings silently.
    - ``handle_command`` runs on the control server's threads, several at once,
      next to the runtime's own threads: it must be thread-safe. ``heartbeat``
      and ``shutdown`` never reach it.
    - ``request_stop()`` comes from any thread at any time (a signal, the
      watchdog, a broken stdout, ``shutdown``), also before or during
      ``start()``; ``wait()`` must then return promptly.
    - ``stop()`` is always called on the way out, also when ``start()`` never
      ran or raised: it must be safe then, and release any held mouse button
      and the camera.
    - An int ``exit_code`` attribute, when set, becomes the process's exit code.
    """
    from .control import ControlServer

    data_dir = _data_dir(args)
    data_dir.mkdir(parents=True, exist_ok=True)
    out = _claim_stdout()
    setup_logging(data_dir, args.log_level)
    plat.suppress_crash_dialogs()
    log.info("jarvis-hands %s starting (pid %d, python %s)", __version__, os.getpid(), sys.version.split()[0])

    # Every way out goes through here. Before the runtime exists it only
    # records the wish, and the runtime is then never started.
    stopping = threading.Event()
    runtime_ref: list[Any] = []

    def request_stop() -> None:
        stopping.set()
        if runtime_ref:
            runtime_ref[0].request_stop()

    writer = EventWriter(out, on_broken_pipe=request_stop).start()

    def fatal(code: protocol.ErrorCode, message: str, hint: str | None, exit_code: int) -> int:
        log.error("%s: %s", code, message)
        writer.emit(protocol.error(code, message, hint, fatal=True))
        writer.close()
        return exit_code

    token = _env("JARVIS_TOKEN")
    if not token:
        if not args.fake:
            return fatal(
                "internal",
                "JARVIS_TOKEN is not set; the hand helper is meant to be started by the Jarvis mod.",
                "Turn hand control on with /jarvis hands on.",
                EXIT_USAGE,
            )
        token = FAKE_TOKEN
        log.warning("fake mode without JARVIS_TOKEN: using the fixed test token")
    secret_filter.add(token)

    lock: InstanceLock | None
    try:
        lock = acquire_instance_lock(args.instance_name, data_dir)
    except OSError as exc:  # cannot even check: run anyway rather than refuse hand control
        log.warning("single-instance lock unavailable (%s); continuing without it", exc)
        lock = _NoLock()
    if lock is None:
        return fatal(
            "already_running",
            "Jarvis hand control is already running in another window.",
            "Turn it off there (/jarvis hands off), then run /jarvis hands restart here.",
            EXIT_ALREADY_RUNNING,
        )

    runtime: Any = None
    control: ControlServer | None = None
    watchdog: HeartbeatWatchdog | None = None
    try:
        from .runtime import HandsRuntime, RuntimeOptions

        options = RuntimeOptions(
            data_dir=data_dir,
            camera=args.camera,
            width=args.width,
            height=args.height,
            fps=args.fps,
            overlay=not args.no_overlay,
            fake=args.fake,
            fake_script=args.fake_script,
        )
        runtime = HandsRuntime(options, writer)
        runtime_ref.append(runtime)
        dog = watchdog = HeartbeatWatchdog(request_stop, timeout=args.heartbeat_timeout, grace=args.heartbeat_grace)
        rt = runtime

        def handle(name: str, body: dict[str, Any]) -> dict[str, Any]:
            if name == "heartbeat":
                dog.beat()
                return protocol.ok_response()
            if name == "shutdown":
                log.info("shutdown requested by the mod")
                return protocol.ok_response()  # ``sent`` asks for the stop once this answer is written
            response: dict[str, Any] = rt.handle_command(name, body)
            return response

        def sent(name: str) -> None:
            """A command that has been answered. The process may now wind down (and close this socket)."""
            if name == "shutdown":
                request_stop()

        control = ControlServer(token, handle, on_sent=sent).start()
        writer.emit(protocol.hello(control.port, os.getpid(), plat.name(), __version__, capabilities(args.fake)))
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                try:
                    signal.signal(sig, lambda *_: request_stop())
                except ValueError:  # not the main thread (embedded use): the mod's shutdown still works
                    break
        watchdog.start()
        if not stopping.is_set():  # stdout broke, or a signal came, while starting up
            runtime.start()
            runtime.wait()
        # A runtime that stopped on a fatal error (it already emitted it) may say so with an int exit_code.
        exit_code = getattr(runtime, "exit_code", None)
        code = exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else EXIT_OK
        log.info("exiting with code %d", code)
        return code
    except Exception as exc:
        log.exception("fatal error")
        writer.emit(protocol.error("internal", f"{type(exc).__name__}: {exc}", fatal=True))
        return EXIT_ERROR
    finally:
        if runtime is not None:
            try:
                runtime.stop()  # releases any held mouse button and the camera
            except Exception:
                log.exception("runtime did not stop cleanly")
        if control is not None:
            control.stop()
        if watchdog is not None:
            watchdog.stop()
        writer.close()
        lock.release()


# --------------------------------------------------------------------------- setup / doctor / preview


def cmd_setup(args: argparse.Namespace) -> int:
    from . import models

    data_dir = _data_dir(args)
    out = _claim_stdout()
    setup_logging(data_dir, "INFO")  # without the log file when the folder cannot be made

    def emit(payload: dict[str, Any]) -> None:
        out.write(encode_line({"v": protocol.PROTOCOL_VERSION, **payload}))
        out.flush()

    def fail(message: str) -> int:
        emit({"type": "progress", "step": "error", "message": message})
        return EXIT_ERROR

    try:
        path = models.ensure_model(
            data_dir,
            url=args.model_url or models.MODEL_URL,
            sha256=args.model_sha256 or models.MODEL_SHA256,
            progress=emit,
        )
    except models.ModelError as exc:
        log.error("setup failed: %s", exc)
        return fail(f"Could not install the hand model: {exc}")
    except Exception as exc:
        log.exception("setup failed")
        return fail(f"Setup failed: {type(exc).__name__}: {exc}")
    size_mb = path.stat().st_size / 1e6
    emit({"type": "progress", "step": "done", "pct": 100.0, "message": f"Ready: hand model ({size_mb:.1f} MB)"})
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    out = _claim_stdout()
    setup_logging(None, "WARNING")
    try:
        from .doctor import run_doctor

        report: dict[str, Any] = run_doctor(data_dir, camera=not args.no_camera)
        code = EXIT_OK
    except Exception as exc:  # an unimportable doctor is itself the finding
        log.exception("doctor failed")
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        code = EXIT_ERROR
    out.write((json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode("ascii"))
    out.flush()
    return code


def cmd_preview(args: argparse.Namespace) -> int:
    args.data_dir = _data_dir(args)
    setup_logging(None, "INFO")
    try:
        from .preview import run_preview
    except ImportError as exc:
        log.error("the preview is not available: %s", exc)
        return EXIT_ERROR
    return int(run_preview(args) or EXIT_OK)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
