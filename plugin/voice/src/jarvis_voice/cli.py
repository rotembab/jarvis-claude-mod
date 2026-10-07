"""Command line: ``run`` (the daemon), ``setup`` (model download), ``doctor`` and the internal ``probe-cuda``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__, protocol
from . import platform as plat
from .events import EventWriter, encode_line
from .lifecycle import DEFAULT_INSTANCE_NAME, acquire_instance_lock
from .logs import secret_filter, setup_logging
from .platform import InstanceLock

log = logging.getLogger("jarvis_voice")

FAKE_TOKEN = "jarvis-fake-token"
EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_ALREADY_RUNNING = 0, 1, 2, 3


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def default_data_dir() -> Path:
    # Path.home() reads USERPROFILE on Windows, HOME elsewhere.
    return Path(_env("JARVIS_DATA_DIR") or Path.home() / ".jarvis")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis_voice", description="Jarvis voice helper for Claude Code.")
    parser.add_argument("--version", action="version", version=f"jarvis-voice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the voice daemon (started by the Jarvis mod)")
    run.add_argument("--data-dir", type=Path, default=None)
    run.add_argument("--fake-audio", action="store_true", help="no audio hardware or keyboard hook (tests)")
    run.add_argument("--fake-stt", action="store_true", help="fixed transcription instead of faster-whisper (tests)")
    run.add_argument("--fake-fish", metavar="URL", default=None, help="Fish Audio base URL of a fake server (tests)")
    run.add_argument("--stt-model", default=_env("JARVIS_STT_MODEL") or "auto")
    run.add_argument("--ptt-key", default=_env("JARVIS_PTT_KEY") or "right ctrl")
    run.add_argument("--voice-id", default=_env("JARVIS_VOICE_ID"))
    run.add_argument(
        "--tts-engine", choices=["fish", "local"], default=_env("JARVIS_TTS_ENGINE") or "fish", help="who speaks"
    )
    run.add_argument("--local-voice", default=_env("JARVIS_LOCAL_VOICE"), help="reference clip for the local voice")
    run.add_argument("--local-python", default=_env("JARVIS_LOCAL_PYTHON"), help=argparse.SUPPRESS)
    run.add_argument("--fake-local", action="store_true", help="local voice plays a tone instead of the model (tests)")
    run.add_argument("--language", default=_env("JARVIS_LANGUAGE") or "en")
    run.add_argument("--input-device", default=_env("JARVIS_INPUT_DEVICE"))
    run.add_argument("--output-device", default=_env("JARVIS_OUTPUT_DEVICE"))
    run.add_argument("--instance-name", default=_env("JARVIS_INSTANCE_NAME") or DEFAULT_INSTANCE_NAME)
    run.add_argument("--heartbeat-timeout", type=float, default=15.0, help=argparse.SUPPRESS)
    run.add_argument("--heartbeat-grace", type=float, default=60.0, help=argparse.SUPPRESS)
    run.add_argument("--log-level", default=_env("JARVIS_LOG_LEVEL") or "INFO")
    run.set_defaults(func=cmd_run)

    setup = sub.add_parser("setup", help="download the speech model (JSON-lines progress on stdout)")
    setup.add_argument("--data-dir", type=Path, default=None)
    setup.add_argument("--stt-model", default=_env("JARVIS_STT_MODEL") or "auto")
    setup.add_argument("--no-verify", action="store_true", help="skip loading the model after download")
    setup.set_defaults(func=cmd_setup)

    doctor = sub.add_parser("doctor", help="print a JSON health report")
    doctor.add_argument("--data-dir", type=Path, default=None)
    doctor.add_argument("--no-network", action="store_true", help="skip the Fish Audio reachability check")
    doctor.add_argument("--no-mic", action="store_true", help="skip the one-second microphone test")
    doctor.set_defaults(func=cmd_doctor)

    # Internal: run/setup test the GPU in this child because a broken CUDA install can abort the process.
    probe = sub.add_parser("probe-cuda", help="(internal) load a model on the GPU once and report")
    probe.add_argument("--model", type=Path, required=True, help="model directory")
    probe.set_defaults(func=cmd_probe_cuda)
    return parser


def _claim_stdout() -> BinaryIO:
    """Keep stdout for protocol lines only; everything else written to it lands on stderr.

    The protocol gets a private duplicate of file descriptor 1, and fd 1
    itself then points at stderr. That catches stray Python prints and also
    native code writing to the C-level stdout (CTranslate2, cuDNN's loader,
    onnxruntime, PortAudio), which would otherwise corrupt the JSON lines.
    On Windows the C runtime's dup2 onto fd 1 also resets the process's
    STD_OUTPUT_HANDLE (python.exe is a console app), so WriteFile users follow too.
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


def _capabilities(args: argparse.Namespace) -> list[str]:
    caps = [
        "heartbeat",
        "speak",
        "stop",
        "listen",
        "config",
        "status",
        "test_voice",
        "shutdown",
        "level",
        "ptt",
        "barge_in.ptt",
        "barge_in.speech",
        "wake",
        "follow_up",
        "stt.faster-whisper",
        "tts.local" if args.tts_engine == "local" else "tts.fish-live",
    ]
    fakes = (("audio", args.fake_audio), ("stt", args.fake_stt), ("fish", args.fake_fish), ("local", args.fake_local))
    caps += [f"fake.{n}" for n, on in fakes if on]
    return caps


def _build_daemon(args: argparse.Namespace, data_dir: Path, writer: EventWriter) -> Any:
    from .daemon import Daemon, DaemonConfig
    from .tts.fish import FishLiveSynth, FishSettings

    if args.fake_audio:
        from .audio.fake import FakeCapture, FakePlayback
        from .ptt.fake import FakePushToTalk

        capture: Any = FakeCapture()
        playback: Any = FakePlayback()
        ptt: Any = FakePushToTalk()  # fake mode never installs a global keyboard hook
        rescan = None
    else:
        from .audio.sd_backend import SoundDeviceCapture, SoundDevicePlayback, rescan_devices
        from .ptt.pynput_backend import PynputPushToTalk

        capture = SoundDeviceCapture(args.input_device)
        playback = SoundDevicePlayback(args.output_device)
        ptt = PynputPushToTalk()
        rescan = rescan_devices

    if args.fake_stt:
        from .stt.fake import FakeTranscriber

        fake_text = _env("JARVIS_FAKE_STT_TEXT") or "Hello Jarvis, this is a test."

        def transcriber_factory(requested: str) -> Any:
            return FakeTranscriber(fake_text, name=f"fake:{requested}")
    else:
        from .stt.faster_whisper_engine import FasterWhisperTranscriber
        from .stt.models import models_dir

        def transcriber_factory(requested: str) -> Any:
            return FasterWhisperTranscriber(requested, models_dir(data_dir))

    synth: Any
    if args.tts_engine == "local":
        from .stt.models import models_dir as local_models_dir
        from .tts.local import LocalVoiceSettings, LocalVoiceSynth, default_python

        synth = LocalVoiceSynth(
            LocalVoiceSettings(
                python=Path(args.local_python).expanduser() if args.local_python else default_python(data_dir),
                models_dir=local_models_dir(data_dir),
                voice_clip=Path(args.local_voice).expanduser() if args.local_voice else None,
                fake=args.fake_local,
            )
        )
        synth.start()  # load the model now, while nobody is waiting for a reply
    else:
        settings = FishSettings.from_env(fake_url=args.fake_fish)
        secret_filter.add(settings.api_key)
        synth = FishLiveSynth(settings)
    vad: Any = None
    try:
        from .listen.vad import SileroVad

        vad = SileroVad()
    except Exception as exc:  # noqa: BLE001 - hands-free is off, push-to-talk still works
        log.warning("voice activity detection unavailable, hands-free listening is off: %s", exc)
    wake_loader = None
    if not args.fake_audio:  # fake audio never hears anything; don't download for it

        def wake_loader() -> Any:
            from .listen.models import download, is_downloaded, wake_dir
            from .listen.wakeword import OpenWakeWord
            from .stt.models import models_dir as wake_models_dir

            folder = wake_dir(wake_models_dir(data_dir))
            if not is_downloaded(folder):
                download(folder)  # about 3.7 MB, once
            return OpenWakeWord(folder)

    config = DaemonConfig(
        stt_model=args.stt_model,
        language=args.language,
        ptt_key=args.ptt_key,
        voice_id=args.voice_id,
        heartbeat_timeout=args.heartbeat_timeout,
        heartbeat_grace=args.heartbeat_grace,
    )
    return Daemon(
        config,
        events=writer,
        capture=capture,
        playback=playback,
        synth=synth,
        ptt=ptt,
        transcriber_factory=transcriber_factory,
        platform_name=plat.name(),
        rescan_audio=rescan,
        vad=vad,
        wake_loader=wake_loader,
    )


def cmd_run(args: argparse.Namespace) -> int:
    from .control import ControlServer
    from .ptt.keys import parse_hotkey

    data_dir: Path = (args.data_dir or default_data_dir()).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    out = _claim_stdout()
    setup_logging(data_dir, args.log_level.upper())
    log.info("jarvis-voice %s starting (pid %d, python %s)", __version__, os.getpid(), sys.version.split()[0])

    daemon_ref: list[Any] = []
    writer = EventWriter(
        out, on_broken_pipe=lambda: daemon_ref[0].request_exit(0) if daemon_ref else os._exit(0)
    ).start()

    def fatal(code: protocol.ErrorCode, message: str, hint: str | None, exit_code: int) -> int:
        log.error("%s: %s", code, message)
        writer.emit(protocol.Error(code=code, message=message, hint=hint, fatal=True))
        writer.close()
        return exit_code

    fake = bool(args.fake_audio or args.fake_stt or args.fake_fish or args.fake_local)
    token = _env("JARVIS_TOKEN")
    if not token:
        if not fake:
            return fatal(
                "internal",
                "JARVIS_TOKEN is not set; the helper is meant to be started by the Jarvis mod.",
                None,
                EXIT_USAGE,
            )
        token = FAKE_TOKEN
        log.warning("fake mode without JARVIS_TOKEN: using the fixed test token")
    secret_filter.add(token)
    try:
        parse_hotkey(args.ptt_key)
    except ValueError as exc:
        return fatal(
            "bad_request", f"invalid push-to-talk key: {exc}", "Use a name like 'right ctrl' or 'f13'.", EXIT_USAGE
        )

    lock: InstanceLock | None
    try:
        lock = acquire_instance_lock(args.instance_name, data_dir)
    except OSError as exc:  # cannot even check: run anyway rather than leave the user voiceless
        log.warning("single-instance lock unavailable (%s); continuing without it", exc)
        lock = _NoLock()
    if lock is None:
        return fatal(
            "already_running",
            "Jarvis voice is already running in another window.",
            "Close the other Claude Code window (or its Jarvis), then run /jarvis here.",
            EXIT_ALREADY_RUNNING,
        )

    server = None
    try:
        daemon = _build_daemon(args, data_dir, writer)
        daemon_ref.append(daemon)
        server = ControlServer(token, daemon.handle_command).start()
        writer.emit(
            protocol.Hello(
                port=server.port,
                pid=os.getpid(),
                platform=plat.name(),
                version=__version__,
                capabilities=tuple(_capabilities(args)),
            )
        )
        for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if sig is not None:
                signal.signal(sig, lambda *_: daemon.request_exit(0))
        daemon.start()
        code = daemon.run()
        log.info("exiting with code %d", code)
        return code
    except Exception as exc:
        log.exception("fatal error")
        writer.emit(protocol.Error(code="internal", message=f"{type(exc).__name__}: {exc}", fatal=True))
        return EXIT_ERROR
    finally:
        if server is not None:
            server.stop()
        writer.close()
        lock.release()


# --------------------------------------------------------------------------- setup / doctor


def cmd_setup(args: argparse.Namespace) -> int:
    from .setup_cmd import run_setup

    data_dir: Path = (args.data_dir or default_data_dir()).expanduser()
    out = _claim_stdout()
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir, "INFO")

    def emit(payload: dict[str, Any]) -> None:
        out.write(encode_line(payload))
        out.flush()

    return run_setup(data_dir, args.stt_model, emit, verify=not args.no_verify)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    data_dir: Path = (args.data_dir or default_data_dir()).expanduser()
    out = _claim_stdout()
    setup_logging(None, "WARNING")
    secret_filter.add(_env("FISH_AUDIO_API_KEY"))
    report = run_doctor(data_dir, network=not args.no_network, test_mic=not args.no_mic)
    out.write((json.dumps(report, indent=2, ensure_ascii=True) + "\n").encode("ascii"))
    out.flush()
    return EXIT_OK


def cmd_probe_cuda(args: argparse.Namespace) -> int:
    from .stt.faster_whisper_engine import load_on_cuda

    out = _claim_stdout()
    setup_logging(None, "WARNING")
    plat.current().suppress_crash_dialogs()
    try:
        load_on_cuda(args.model)  # a broken CUDA install may abort right here; the parent sees the exit code
    except Exception as exc:  # noqa: BLE001 - any failure means "no GPU" and is reported
        log.warning("GPU test failed: %s: %s", type(exc).__name__, exc)
        report: dict[str, Any] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        report = {"ok": True}
    out.write(encode_line(report))
    out.flush()
    return EXIT_OK if report["ok"] else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
