"""The plugin, its helper and the Jarvis app carry one version number.

``claude plugin update`` only installs a new copy when the version in
plugin.json changes, so it is bumped with every release; the helper's
version (shown by ``/jarvis status``) and the app's (in its tray menu and
its address file) move with it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import jarvis_voice

HELPER = Path(__file__).resolve().parents[1]
PLUGIN_JSON = HELPER.parent / ".claude-plugin" / "plugin.json"
APP = HELPER.parents[1] / "app"


def test_helper_and_plugin_versions_match() -> None:
    pyproject = tomllib.loads((HELPER / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    assert jarvis_voice.__version__ == pyproject == plugin


def test_app_version_matches() -> None:
    # A checkout without the app (it is optional) has nothing to compare.
    if not (APP / "package.json").is_file():
        pytest.skip("no app/package.json in this checkout")
    package = json.loads((APP / "package.json").read_text(encoding="utf-8"))["version"]
    lock = json.loads((APP / "package-lock.json").read_text(encoding="utf-8"))
    assert package == lock["version"] == lock["packages"][""]["version"] == jarvis_voice.__version__
