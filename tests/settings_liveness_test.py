"""Settings with names in the product surface must reach one real authority."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from astrabox.common.utils.settings import AstraBoxRuntimeSettings
from astrabox.config import settings as settings_module
from astrabox.config.settings import AstraBoxSettings
from astrabox.deploy import onebox, sandbox_server
from astrabox.identity import session_signing
from astrabox.providers import secret_store

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_state_dir_resolver_expands_home_for_sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ASTRABOX_STATE_DIR", raising=False)
    monkeypatch.delenv("ASTRABOX_DB_BACKEND", raising=False)
    monkeypatch.delenv("ASTRABOX_DB_URL", raising=False)
    settings = AstraBoxSettings(
        state_dir=Path("~/astrabox"),
        db_backend="sqlite",
        db_url=None,
        _env_file=None,
    )
    expected = home / "astrabox"

    assert settings.resolved_state_dir() == expected
    assert settings.resolved_db_url == (
        f"sqlite+aiosqlite:///{expected / 'astrabox.sqlite'}"
    )
    assert AstraBoxSettings.model_fields["state_dir"].default == Path("./.astrabox")


def test_every_state_dir_consumer_uses_the_resolved_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolved = tmp_path / "operator-home" / "astrabox"

    class _ResolvedOnlySettings:
        def resolved_state_dir(self) -> Path:
            return resolved

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: _ResolvedOnlySettings(),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRABOX_STATE_DIR", "~/wrong-parser-result")
    monkeypatch.setenv(sandbox_server.METADATA_DIR_ENV, "")
    monkeypatch.setenv(onebox.LITELLM_MASTER_KEY_ENV, "")
    monkeypatch.setenv(onebox.LITELLM_API_KEY_ENV_NAME, "")

    key = onebox.ensure_litellm_master_key()

    assert session_signing._key_path() == resolved / session_signing.SESSION_KEY_FILENAME
    assert secret_store._state_dir() == resolved
    assert sandbox_server.metadata_dir() == resolved / sandbox_server.DEFAULT_METADATA_DIRNAME
    assert (resolved / onebox.LITELLM_KEY_FILENAME).read_text().strip() == key
    assert not (tmp_path / "~" / "wrong-parser-result" / onebox.LITELLM_KEY_FILENAME).exists()


def test_every_runtime_setting_name_is_read_outside_its_declaration() -> None:
    """A declared knob must have at least one candidate production reader."""
    unread = set(AstraBoxRuntimeSettings.model_fields)
    declaration = _REPO_ROOT / "astrabox" / "common" / "utils" / "settings.py"
    for path in (_REPO_ROOT / "astrabox").rglob("*.py"):
        if path == declaration:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                unread.discard(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                unread.discard(node.args[1].value)

    assert not unread, (
        "AstraBoxRuntimeSettings field name(s) are never read outside the "
        "declaration module: "
        f"{sorted(unread)}. Wire each field to its real authority or remove it."
    )


def test_idle_hibernate_default_refuses_a_non_positive_value() -> None:
    with pytest.raises(ValidationError, match="agent.idle_hibernate_seconds"):
        AstraBoxRuntimeSettings(
            astrabox={"agent": {"idle_hibernate_seconds": 0}}
        )
