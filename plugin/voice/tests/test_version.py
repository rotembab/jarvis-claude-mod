"""The plugin and its helper carry one version number.

``claude plugin update`` only installs a new copy when the version in
plugin.json changes, so it is bumped with every release; the helper's
version (shown by ``/jarvis status``) moves with it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import jarvis_voice

HELPER = Path(__file__).resolve().parents[1]
PLUGIN_JSON = HELPER.parent / ".claude-plugin" / "plugin.json"


def test_helper_and_plugin_versions_match() -> None:
    pyproject = tomllib.loads((HELPER / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))["version"]
    assert jarvis_voice.__version__ == pyproject == plugin
