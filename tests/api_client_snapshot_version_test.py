"""Reject a client snapshot generated with another checkout's package metadata."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("snapshot_version", ["0.0.0", None])
def test_wrong_snapshot_version_is_refused_before_codegen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    snapshot_version: str | None,
) -> None:
    path = Path(__file__).parents[1] / "scripts/check_api_client.py"
    spec = importlib.util.spec_from_file_location("check_api_client", path)
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"info": {"version": snapshot_version}}))
    client = tmp_path / "schema.d.ts"
    client.write_text("unchanged client")
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(checker, "SNAPSHOT", snapshot)
    monkeypatch.setattr(checker, "COMMITTED_CLIENT", client)

    def unexpected_codegen(*args: object, **kwargs: object) -> None:
        pytest.fail("version mismatch must be diagnosed before invoking code generation")

    monkeypatch.setattr(checker.subprocess, "run", unexpected_codegen)
    assert checker.main() == 1
    assert "differs from pyproject.toml '1.2.3'" in capsys.readouterr().err
    assert client.read_text() == "unchanged client"
