"""Settings with names in the product surface must reach one real authority."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from astrabox.common.utils.settings import AstraBoxRuntimeSettings
from astrabox.config.config import _read_app_yaml
from astrabox.config import settings as settings_module
from astrabox.config.env_registry import concrete_names
from astrabox.config.settings import AstraBoxSettings
from astrabox.deploy import onebox, sandbox_server
from astrabox.identity import session_signing
from astrabox.providers import secret_store

_REPO_ROOT = Path(__file__).resolve().parents[1]

_REMOVED_RUNTIME_FIELDS = frozenset(
    {
        "mongodb_database",
        "mongodb_uri",
        "mongodb_uri_secret_name",
        "agent_scheduler_poll_interval_ms",
        "agent_run_default_timeout_seconds",
        "agent_conversation_workspace_timeout_seconds",
        "agent_sandbox_renew_cooldown_seconds",
        "template_cache_seconds",
        "sandbox_dev_host_aliases",
        "reprovision_max_attempts",
        "engine_memory_base_url",
        "assistant_skill_source_dirs",
        "assistant_skill_repos",
        "mcp_gateway_url",
    }
)

_REMOVED_ENV_NAMES = frozenset(
    {
        "ASTRABOX_MONGODB_DATABASE",
        "ASTRABOX_AGENT_CONVERSATION_WORKSPACE_TIMEOUT_SECONDS",
        "ASTRABOX_AGENT_SANDBOX_RENEW_COOLDOWN_SECONDS",
        "ASTRABOX_SANDBOX_DEV_HOST_ALIASES",
        "ASTRABOX_REPROVISION_MAX_ATTEMPTS",
        "ASTRABOX_ENGINE_MEMORY_BASE_URL",
        "ASTRABOX_ASSISTANT_SKILL_SOURCE_DIRS",
        "ASTRABOX_ASSISTANT_SKILL_REPOS",
        "ASTRABOX_MCP_GATEWAY_URL",
    }
)


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


def _removed_attribute_reads() -> list[str]:
    reads: list[str] = []
    for path in (_REPO_ROOT / "astrabox").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in _REMOVED_RUNTIME_FIELDS
            ):
                reads.append(f"{path.relative_to(_REPO_ROOT)}:{node.lineno}:{node.attr}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _REMOVED_RUNTIME_FIELDS
            ):
                reads.append(
                    f"{path.relative_to(_REPO_ROOT)}:{node.lineno}:{node.args[1].value}"
                )
    return reads


def test_removed_runtime_settings_leave_no_config_or_reader() -> None:
    assert _REMOVED_RUNTIME_FIELDS.isdisjoint(AstraBoxRuntimeSettings.model_fields)
    assert _REMOVED_ENV_NAMES.isdisjoint(concrete_names())

    document: dict[str, Any] = _read_app_yaml()
    astrabox = document["astrabox"]
    mongodb = astrabox["mongodb"]
    agent = astrabox["agent"]
    assert {"uri", "uri_secret_name", "database"}.isdisjoint(mongodb)
    assert {
        "scheduler_poll_interval_ms",
        "run_default_timeout_seconds",
        "conversation_workspace_timeout_seconds",
        "sandbox_renew_cooldown_seconds",
        "reprovision_max_attempts",
    }.isdisjoint(agent)
    assert "template_cache_seconds" not in astrabox
    assert _removed_attribute_reads() == []


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
