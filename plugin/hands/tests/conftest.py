"""Fixtures shared by the suite, and the real hand model every real-model test file works from.

The real-model tests only run with ``JARVIS_HANDS_MODELS_DIR`` set (CI sets
it to a folder of the job's own, which starts out empty). The model and
MediaPipe's test photos are downloaded into it here, once per session and
checked against their checksum, so whichever file runs first fills the folder
and none of them skips because another one would have downloaded the model.
"""

from __future__ import annotations

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for the scripted helper module

from jarvis_hands import models
from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.gestures import GestureEngine
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.settings import HandsSettings

from scripted import Script

MODELS_ENV = "JARVIS_HANDS_MODELS_DIR"
IMAGE_URL = "https://storage.googleapis.com/mediapipe-assets/{name}.jpg"

#: On every test (and every module, through ``pytestmark``) that needs the real model.
real_model = pytest.mark.skipif(not os.environ.get(MODELS_ENV), reason=f"set {MODELS_ENV} to run the real model")


@pytest.fixture
def settings() -> HandsSettings:
    return HandsSettings()


@pytest.fixture
def desktop() -> FakeDesktop:
    """One 1920 x 1080 primary display with a 40 px taskbar."""
    return FakeDesktop()


@pytest.fixture
def mapper(desktop: FakeDesktop, settings: HandsSettings) -> ScreenMapper:
    return ScreenMapper(desktop.displays(), settings)


@pytest.fixture
def engine(settings: HandsSettings, mapper: ScreenMapper) -> GestureEngine:
    return GestureEngine(settings, mapper)


@pytest.fixture
def script() -> Script:
    return Script()


# --------------------------------------------------------------------------- the real model and its test photos


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url: str, path: Path, sha256: str | None = None) -> Path:
    """``url`` downloaded to ``path`` unless it is already there (with that checksum, when one is given)."""
    if path.exists() and (sha256 is None or _sha256(path) == sha256):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    with urllib.request.urlopen(url, timeout=120) as response:
        partial.write_bytes(response.read())
    if sha256 is not None and _sha256(partial) != sha256:
        partial.unlink()
        raise AssertionError(f"{url} does not match its sha256")
    os.replace(partial, path)
    return path


def real_models_dir() -> Path:
    """``JARVIS_HANDS_MODELS_DIR``; only call it under the ``real_model`` mark."""
    return Path(os.environ[MODELS_ENV])


def fetch_real_model() -> Path:
    return fetch(models.MODEL_URL, real_models_dir() / models.MODEL_NAME, models.MODEL_SHA256)


def fetch_photo(name: str) -> Path:
    """One of MediaPipe's own test photos, by its asset name (no checksum published for these)."""
    return fetch(IMAGE_URL.format(name=name), real_models_dir() / f"{name}.jpg")


@pytest.fixture(scope="session")
def real_model_path() -> Path:
    """The real hand model, downloaded into the models folder when it is not there yet."""
    return fetch_real_model()
