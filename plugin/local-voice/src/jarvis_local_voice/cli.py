"""``serve`` (the voice process the helper starts) and ``download`` (used by /jarvis setup local)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

from . import __version__

log = logging.getLogger("jarvis_local_voice")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis_local_voice", description="Local text-to-speech for Jarvis.")
    parser.add_argument("--version", action="version", version=f"jarvis-local-voice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="speak sentences from stdin (started by the voice helper)")
    serve.add_argument("--models-dir", type=Path, default=None)
    serve.add_argument("--voice", type=Path, default=None, help="reference clip to copy the voice from (5 s or more)")
    serve.add_argument("--device", default="auto", choices=["auto", "cuda", "mps", "cpu"])
    serve.add_argument("--fake", action="store_true", help="a tone instead of the model (tests)")
    serve.add_argument("--fake-delay", type=float, default=0.0, help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)

    download = sub.add_parser("download", help="download the model (JSON-lines progress on stdout)")
    download.add_argument("--models-dir", type=Path, default=None)
    download.add_argument("--verify", action="store_true", help="load the model and speak one test sentence")
    download.add_argument("--voice", type=Path, default=None)
    download.add_argument("--device", default="auto", choices=["auto", "cuda", "mps", "cpu"])
    download.set_defaults(func=cmd_download)
    return parser


def default_models_dir() -> Path:
    data = os.environ.get("JARVIS_DATA_DIR", "").strip()
    return (Path(data) if data else Path.home() / ".jarvis") / "models"


def _claim_stdout() -> BinaryIO:
    """Keep stdout for protocol lines; prints and native libraries writing to fd 1 go to stderr instead."""
    sys.stdout.flush()
    out: BinaryIO = sys.stdout.buffer
    try:
        protocol_fd = os.dup(1)
    except OSError:
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


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import FakeBackend, serve

    out = _claim_stdout()
    models_dir = (args.models_dir or default_models_dir()).expanduser()
    voice = args.voice.expanduser() if args.voice else None

    def load() -> Any:
        if args.fake:
            return FakeBackend(delay=args.fake_delay)
        from .chatterbox_backend import ChatterboxBackend

        return ChatterboxBackend(models_dir, voice, args.device)

    code = serve(load, sys.stdin.buffer, out)
    # The request reader may still be blocked on stdin; a normal interpreter
    # shutdown would abort on its lock, so leave directly.
    sys.stderr.flush()
    os._exit(code)


def cmd_download(args: argparse.Namespace) -> int:
    out = _claim_stdout()
    models_dir = (args.models_dir or default_models_dir()).expanduser()

    def progress(step: str, message: str) -> None:
        line = json.dumps({"type": "progress", "step": step, "message": message}, ensure_ascii=True)
        out.write((line + "\n").encode("ascii"))
        out.flush()

    try:
        from .chatterbox_backend import ChatterboxBackend, download, is_downloaded

        if is_downloaded(models_dir):
            progress("download", "the local voice model is already downloaded")
        else:
            progress("download", "downloading the local voice model (about 3 GB)")
            download(models_dir)
        if not args.verify:
            progress("done", "local voice model downloaded")
            return 0
        progress("verify", "loading the local voice model")
        backend = ChatterboxBackend(models_dir, args.voice.expanduser() if args.voice else None, args.device)
        started = time.monotonic()
        pcm = backend.synthesize("Good evening, sir. All systems are online.")
        took = time.monotonic() - started
        seconds = len(pcm) / 2 / backend.sample_rate
        progress(
            "done",
            f"local voice ready on {backend.device}: {seconds:.1f} s of speech took {took:.1f} s"
            f" (voice {backend.voice})",
        )
        return 0
    except Exception as exc:
        log.exception("local voice setup failed")
        progress("error", f"local voice setup failed: {type(exc).__name__}: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    return int(args.func(args))
