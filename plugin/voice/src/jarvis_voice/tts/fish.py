"""Fish Audio live TTS over WebSocket (``/v1/tts/live``, MessagePack frames).

Protocol (docs.fish.audio "WebSocket TTS Live" and fish-audio-sdk 1.3
``fishaudio/resources/tts.py`` / ``realtime.py``, checked 2026-10-06):

* connect with ``Authorization: Bearer <key>`` and an optional ``model`` header;
* send ``{"event": "start", "request": {...}}`` first, then any number of
  ``{"event": "text", "text": ...}`` and ``{"event": "flush"}``, and finally
  ``{"event": "stop"}``;
* receive ``{"event": "audio", "audio": <bytes>}`` frames, then
  ``{"event": "finish", "reason": "stop" | "error"}``; the server then closes.

One connection per reply, opened on the reply's first sentence. Each sentence
is sent as text followed by a flush, so audio starts as soon as possible.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import ormsgpack

from .. import __version__
from .base import KEY_HINT, AudioCallback, SynthError

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "wss://api.fish.audio"
LIVE_PATH = "/v1/tts/live"
DEFAULT_MODEL = "s2.1-pro"
SAMPLE_RATE = 24_000

AUTH_HINT = "Check the Fish Audio API key (and account balance) at fish.audio, then update fishApiKey."
NET_HINT = "Check your internet connection or proxy; Jarvis will keep trying on the next reply."

_AUTH_WORDS = ("unauthor", "forbidden", "api key", "apikey", "invalid key", "authentication", "token")
_BALANCE_WORDS = ("balance", "credit", "payment", "quota", "insufficient")


@dataclass(frozen=True, slots=True)
class FishSettings:
    api_key: str | None
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    sample_rate: int = SAMPLE_RATE
    latency: str = "balanced"
    chunk_length: int = 200
    connect_timeout: float = 10.0
    finish_timeout: float = 20.0  # max silence from the server after we sent "stop"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, fake_url: str | None = None) -> FishSettings:
        env = os.environ if env is None else env
        key = (env.get("FISH_AUDIO_API_KEY") or "").strip() or None
        base = (env.get("JARVIS_FISH_BASE_URL") or "").strip() or DEFAULT_BASE_URL
        if fake_url:
            base = fake_url
            key = key or "fake-key"  # test servers accept any configured key
        model = (env.get("JARVIS_TTS_MODEL") or "").strip() or DEFAULT_MODEL
        return cls(api_key=key, base_url=base, model=model)


def live_url(base_url: str) -> str:
    """``https://host`` / ``wss://host`` / ``http://127.0.0.1:9`` -> the live endpoint URL."""
    parts = urlsplit(base_url.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    if scheme not in ("ws", "wss"):
        raise ValueError(f"unsupported Fish Audio base URL {base_url!r}")
    path = parts.path.rstrip("/")
    if not path.endswith(LIVE_PATH):
        path += LIVE_PATH
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


def _is_loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _classify_text(text: str, default: str = "fish_unreachable") -> tuple[str, str]:
    lowered = text.lower()
    if any(w in lowered for w in _BALANCE_WORDS):
        return "fish_auth_failed", "Check your Fish Audio account balance."
    if any(w in lowered for w in _AUTH_WORDS):
        return "fish_auth_failed", AUTH_HINT
    return default, NET_HINT


def start_request(settings: FishSettings, voice_id: str | None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "text": "",
        "format": "pcm",
        "sample_rate": settings.sample_rate,
        "latency": settings.latency,
        "chunk_length": settings.chunk_length,
        "normalize": True,
        "prosody": {"speed": 1.0, "volume": 0},
    }
    if voice_id:
        request["reference_id"] = voice_id
    return {"event": "start", "request": request}


class FishLiveSynth:
    """SpeechSynth backed by Fish Audio's live WebSocket API."""

    def __init__(self, settings: FishSettings) -> None:
        self.settings = settings
        self.sample_rate = settings.sample_rate
        self._warned_default_voice = False

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key)

    def open_stream(self, on_audio: AudioCallback, *, voice_id: str | None) -> FishStream:
        from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI
        from websockets.sync.client import connect

        s = self.settings
        if not s.api_key:
            raise SynthError("fish_key_missing", "No Fish Audio API key is configured.", KEY_HINT)
        if not voice_id and not self._warned_default_voice:
            self._warned_default_voice = True
            log.info("no voiceId configured; using Fish Audio's default voice (set voiceId to pick one)")
        try:
            url = live_url(s.base_url)
        except ValueError as exc:
            raise SynthError("fish_unreachable", str(exc), NET_HINT) from exc
        # connect() is (becoming) a context manager; an ExitStack lets the
        # connection outlive this call and be closed later by the stream.
        stack = contextlib.ExitStack()
        try:
            ws = stack.enter_context(
                connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {s.api_key}", "model": s.model},
                    user_agent_header=f"jarvis-voice/{__version__}",
                    open_timeout=s.connect_timeout,
                    close_timeout=1.0,
                    max_size=16 * 1024 * 1024,
                    compression=None,
                    # Honour HTTPS_PROXY for the real API; never proxy a local test server.
                    proxy=None if _is_loopback(url) else True,
                )
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            body = (exc.response.body or b"")[:300].decode("utf-8", "replace")
            if status in (401, 403):
                raise SynthError(
                    "fish_auth_failed", f"Fish Audio rejected the API key (HTTP {status}).", AUTH_HINT
                ) from exc
            if status == 402:
                raise SynthError(
                    "fish_auth_failed",
                    "Fish Audio says payment is required (HTTP 402).",
                    "Check your Fish Audio account balance.",
                ) from exc
            code, hint = _classify_text(body)
            raise SynthError(code, f"Fish Audio refused the connection (HTTP {status}) {body}".strip(), hint) from exc  # type: ignore[arg-type]
        except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
            raise SynthError("fish_unreachable", f"Cannot reach Fish Audio: {exc}", NET_HINT) from exc
        stream = FishStream(ws, stack, on_audio, sample_rate=s.sample_rate, finish_timeout=s.finish_timeout)
        try:
            stream.send_event(start_request(s, voice_id))
        except SynthError:
            stream.cancel()
            raise
        stream.start_reader()
        return stream


class FishStream:
    """One live session. The reader thread hands audio to ``on_audio`` in order."""

    def __init__(
        self, ws: Any, stack: contextlib.ExitStack, on_audio: AudioCallback, *, sample_rate: int, finish_timeout: float
    ) -> None:
        self.sample_rate = sample_rate
        self._ws = ws
        self._stack = stack
        self._on_audio = on_audio
        self._finish_timeout = finish_timeout
        self._send_lock = threading.Lock()
        self._done = False
        self._closed = False
        self._cancelled = False
        self._finishing_since: float | None = None
        self._last_rx = time.monotonic()
        self._error: SynthError | None = None
        self.audio_bytes = 0
        self._reader = threading.Thread(target=self._read_loop, name="fish-reader", daemon=True)

    # -- state

    @property
    def done(self) -> bool:
        return self._done

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def error(self) -> SynthError | None:
        return self._error

    # -- sending

    def send_event(self, event: dict[str, Any]) -> None:
        from websockets.exceptions import ConnectionClosed

        if self._closed:
            raise SynthError("fish_unreachable", "the Fish Audio session is already closed", NET_HINT)
        try:
            with self._send_lock:
                self._ws.send(ormsgpack.packb(event))
        except (ConnectionClosed, OSError) as exc:
            raise SynthError("fish_unreachable", f"lost the Fish Audio connection: {exc}", NET_HINT) from exc

    def send_text(self, text: str) -> None:
        # Trailing space keeps words apart when the service concatenates its buffer.
        self.send_event({"event": "text", "text": text.strip() + " "})
        self.send_event({"event": "flush"})

    def finish(self) -> None:
        if self._closed or self._finishing_since is not None:
            return
        self._finishing_since = time.monotonic()
        try:
            self.send_event({"event": "stop"})
        except SynthError as exc:
            self._fail(exc)

    def cancel(self) -> None:
        self._cancelled = True
        self._close_async()

    # -- receiving

    def start_reader(self) -> None:
        self._reader.start()

    def _fail(self, error: SynthError) -> None:
        if self._error is None and not self._cancelled:
            self._error = error
        self._close_async()

    def _close_async(self) -> None:
        if self._closed:
            return
        self._closed = True
        # close() waits for the closing handshake; never block the caller on it.
        threading.Thread(target=self._safe_close, name="fish-close", daemon=True).start()

    def _safe_close(self) -> None:
        try:
            self._stack.close()
        except Exception:
            log.debug("error closing Fish socket", exc_info=True)

    def _read_loop(self) -> None:
        from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

        try:
            while not self._cancelled:
                try:
                    message = self._ws.recv(timeout=0.5)
                except TimeoutError:
                    if (
                        self._finishing_since is not None
                        and time.monotonic() - max(self._last_rx, self._finishing_since) > self._finish_timeout
                    ):
                        self._fail(SynthError("fish_unreachable", "Fish Audio stopped responding.", NET_HINT))
                        return
                    continue
                except ConnectionClosedOK:
                    return  # normal close; if it came before "finish" the caller may reopen
                except ConnectionClosed as exc:
                    if not self._cancelled:
                        rcvd = getattr(exc, "rcvd", None)
                        reason = f"{getattr(rcvd, 'code', '')} {getattr(rcvd, 'reason', '')}".strip()
                        code, hint = _classify_text(reason)
                        if rcvd is not None and getattr(rcvd, "code", None) in (1008, 4001, 4003):
                            code, hint = "fish_auth_failed", AUTH_HINT
                        self._fail(SynthError(code, f"Fish Audio closed the connection: {reason or exc}", hint))  # type: ignore[arg-type]
                    return
                except OSError as exc:
                    self._fail(SynthError("fish_unreachable", f"lost the Fish Audio connection: {exc}", NET_HINT))
                    return
                self._last_rx = time.monotonic()
                if isinstance(message, str):
                    self._handle_text_frame(message)
                    continue
                try:
                    data = ormsgpack.unpackb(message)
                except Exception:  # noqa: BLE001
                    log.warning("ignoring undecodable Fish frame (%d bytes)", len(message))
                    continue
                if not isinstance(data, dict):
                    continue
                event = data.get("event")
                if event == "audio":
                    audio = data.get("audio")
                    if isinstance(audio, (bytes, bytearray)) and not self._cancelled:
                        self.audio_bytes += len(audio)
                        self._on_audio(bytes(audio))
                elif event == "finish":
                    if data.get("reason") == "error":
                        self._fail(SynthError("fish_unreachable", "Fish Audio reported a synthesis error.", NET_HINT))
                    else:
                        self._done = True
                    return
                else:
                    log.debug("ignoring Fish event %r", event)
        except Exception as exc:
            log.exception("Fish reader crashed")
            self._fail(SynthError("internal", f"Fish reader crashed: {exc}"))
        finally:
            self._close_async()

    def _handle_text_frame(self, message: str) -> None:
        # The live API speaks MessagePack; a text frame is most likely an error report.
        log.warning("Fish sent a text frame: %s", message[:300])
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            payload = {"message": message}
        text = json.dumps(payload)
        if any(w in text.lower() for w in _AUTH_WORDS + _BALANCE_WORDS + ("error",)):
            code, hint = _classify_text(text)
            self._fail(SynthError(code, f"Fish Audio error: {message[:200]}", hint))  # type: ignore[arg-type]


def probe(settings: FishSettings, timeout: float = 6.0) -> dict[str, Any]:
    """Handshake-only reachability/auth check for ``doctor`` (sends no text, costs nothing)."""
    from websockets.exceptions import InvalidHandshake, InvalidStatus, InvalidURI
    from websockets.sync.client import connect

    try:
        url = live_url(settings.base_url)
    except ValueError as exc:
        return {"reachable": False, "status": "bad_url", "message": str(exc)}
    headers = {"model": settings.model}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    try:
        with connect(
            url,
            additional_headers=headers,
            open_timeout=timeout,
            close_timeout=1.0,
            proxy=None if _is_loopback(url) else True,
        ):
            pass  # the handshake is the whole test
    except InvalidStatus as exc:
        status = exc.response.status_code
        if status in (401, 402, 403):
            if not settings.api_key:
                return {"reachable": True, "status": "key_missing", "message": f"HTTP {status} without a key"}
            return {"reachable": True, "status": "auth_failed", "message": f"HTTP {status}"}
        return {"reachable": True, "status": "error", "message": f"HTTP {status}"}
    except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
        return {"reachable": False, "status": "unreachable", "message": str(exc)}
    return {"reachable": True, "status": "ok" if settings.api_key else "key_missing", "message": "handshake accepted"}
