"""MCP servers removed from an Assistant leave Hermes' config.

The platform writes the Assistant's MCP servers into the profile's
``config.yaml`` by a recursive merge, which only adds and overwrites. Without a
record of which servers are the platform's, removing one from the Assistant
left it configured in Hermes indefinitely. These run the image's merge program
as the launcher does, against a real profile directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/runtime/hermes_config_merge.py"


def _merge(home: Path, mcp_servers: dict[str, object] | None) -> dict[str, object]:
    overwrite: dict[str, object] = {"model": {"default": "m"}}
    if mcp_servers is not None:
        overwrite["mcp_servers"] = mcp_servers
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "ASTRABOX_HERMES_CONFIG_OVERWRITE": json.dumps(overwrite),
    }
    env.pop("ASTRABOX_HERMES_SOUL_B64", None)
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    loaded = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_a_server_the_assistant_dropped_is_removed(tmp_path: Path) -> None:
    _merge(tmp_path, {"search": {"url": "https://a"}, "docs": {"url": "https://b"}})

    config = _merge(tmp_path, {"docs": {"url": "https://b"}})

    assert config["mcp_servers"] == {"docs": {"url": "https://b"}}


def test_removing_every_server_removes_the_key(tmp_path: Path) -> None:
    _merge(tmp_path, {"search": {"url": "https://a"}})

    config = _merge(tmp_path, None)

    assert "mcp_servers" not in config


def test_a_server_the_user_added_survives(tmp_path: Path) -> None:
    _merge(tmp_path, {"search": {"url": "https://a"}})
    config_path = tmp_path / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["mcp_servers"]["mine"] = {"url": "https://mine"}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    config = _merge(tmp_path, None)

    assert config["mcp_servers"] == {"mine": {"url": "https://mine"}}
