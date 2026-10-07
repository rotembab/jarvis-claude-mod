from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # for the scripted helper module

from jarvis_hands.desktop.fake import FakeDesktop
from jarvis_hands.gestures import GestureEngine
from jarvis_hands.mapping import ScreenMapper
from jarvis_hands.settings import HandsSettings

from scripted import Script


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
