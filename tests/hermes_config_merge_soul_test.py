"""Who owns a Hermes profile's SOUL.md, across the Assistant's system prompt changing.

Hermes seeds ``$HERMES_HOME/SOUL.md`` with its own identity the first time it
loads a profile and never overwrites an existing one. So the platform writing
the Assistant's system prompt there is only half of it: when the owner clears
the prompt, the file the platform wrote would otherwise keep answering as the
old identity forever. These run the image's own merge program against a real
directory and pin the three owners the file can have.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/runtime/hermes_config_merge.py"
_HERMES_DEFAULT = "You are Hermes Agent, built by Nous Research."


def _merge_program() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hermes_config_merge", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _configure(monkeypatch: pytest.MonkeyPatch, system: str | None) -> None:
    if system is None:
        monkeypatch.delenv("ASTRABOX_HERMES_SOUL_B64", raising=False)
    else:
        monkeypatch.setenv(
            "ASTRABOX_HERMES_SOUL_B64",
            base64.b64encode(system.encode("utf-8")).decode("ascii"),
        )


def test_a_configured_prompt_is_the_soul_and_rewrites_nothing_when_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge = _merge_program()
    (tmp_path / "SOUL.md").write_text(_HERMES_DEFAULT, encoding="utf-8")
    _configure(monkeypatch, "You are Quill.\n")

    assert merge.materialize_soul(tmp_path) is True
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "You are Quill.\n"
    assert (tmp_path / "SOUL.md").stat().st_mode & 0o777 == 0o600

    assert merge.materialize_soul(tmp_path) is False


def test_clearing_the_prompt_hands_the_soul_back_to_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removed, not emptied: Hermes seeds its own default only for a missing file.

    An empty SOUL.md would make Hermes fall back to its built-in identity for
    now, but it is still a file the user never wrote, and Hermes never replaces
    an existing one with its seeded default.
    """

    merge = _merge_program()
    _configure(monkeypatch, "You are Quill.\n")
    merge.materialize_soul(tmp_path)

    _configure(monkeypatch, None)

    assert merge.materialize_soul(tmp_path) is True
    assert not (tmp_path / "SOUL.md").exists()
    assert list(tmp_path.iterdir()) == []


def test_a_soul_the_user_rewrote_survives_the_prompt_being_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge = _merge_program()
    _configure(monkeypatch, "You are Quill.\n")
    merge.materialize_soul(tmp_path)
    (tmp_path / "SOUL.md").write_text("You are my own assistant.\n", encoding="utf-8")

    _configure(monkeypatch, None)
    merge.materialize_soul(tmp_path)

    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == (
        "You are my own assistant.\n"
    )
    assert [p.name for p in tmp_path.iterdir()] == ["SOUL.md"]


def test_a_profile_the_platform_never_configured_is_left_to_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merge = _merge_program()
    (tmp_path / "SOUL.md").write_text(_HERMES_DEFAULT, encoding="utf-8")
    _configure(monkeypatch, None)

    assert merge.materialize_soul(tmp_path) is False
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == _HERMES_DEFAULT
