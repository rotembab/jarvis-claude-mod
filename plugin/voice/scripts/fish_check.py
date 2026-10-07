"""Free Fish Audio account check: live-TTS handshake per model, plus API and plan balances.

Run with the helper's Python (``~/.jarvis/venv/Scripts/python.exe`` on Windows):

    python plugin/voice/scripts/fish_check.py [--models s2.1-pro-free s2.1-pro]

It sends no text, so it spends nothing. The key is read only from FISH_AUDIO_API_KEY
and is never printed; every output line is redacted before it is shown.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

BASE = "api.fish.audio"
KEY = (os.environ.get("FISH_AUDIO_API_KEY") or "").strip()


def show(line: str) -> None:
    print(line.replace(KEY, "<FISH_KEY>") if KEY else line, flush=True)


def handshake(model: str) -> None:
    try:
        with connect(
            f"wss://{BASE}/v1/tts/live",
            additional_headers={"Authorization": f"Bearer {KEY}", "model": model},
            open_timeout=8,
            close_timeout=1,
        ):
            show(f"live TTS, model {model}: handshake accepted")
    except InvalidStatus as exc:
        r = exc.response
        body = (r.body or b"")[:300].decode("utf-8", "replace")
        show(f"live TTS, model {model}: HTTP {r.status_code} {body}")
    except Exception as exc:  # noqa: BLE001 - report anything, this is a diagnostic
        show(f"live TTS, model {model}: {type(exc).__name__}: {exc}")


def wallet(path: str, fields: tuple[str, ...]) -> None:
    req = urllib.request.Request(
        f"https://{BASE}{path}", headers={"Authorization": f"Bearer {KEY}", "User-Agent": "jarvis-voice-check"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        show(f"GET {path.split('?')[0]}: " + json.dumps({k: data.get(k) for k in fields}))
    except urllib.error.HTTPError as exc:
        show(f"GET {path.split('?')[0]}: HTTP {exc.code} {exc.read()[:200].decode('utf-8', 'replace')}")
    except Exception as exc:  # noqa: BLE001
        show(f"GET {path.split('?')[0]}: {type(exc).__name__}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=["s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"])
    args = parser.parse_args()
    show(f"FISH_AUDIO_API_KEY set: {bool(KEY)}")
    if not KEY:
        return
    for model in args.models:
        handshake(model)
    # API credit pays for the developer API; the plan ("package") credit only pays for the web app.
    wallet("/wallet/self/api-credit?check_free_credit=true", ("credit", "cumulative_top_up", "has_free_credit"))
    wallet("/wallet/self/package", ("type", "total", "balance"))


if __name__ == "__main__":
    main()
