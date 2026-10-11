"""A deployment uses only the sandbox backends it enables.

Installing a provider package registers it, and a deployment may install
providers it does not run. Every question the platform asks of the registry —
which backend serves a record, which backends an Environment may name, which
permission levels and credential types are on offer, which provider owns a live
handle — has to be answered from the enabled set. Answered from the installed
set instead, a backend the deployment does not run is offered to authors,
contributes capabilities nobody can use, and serves records it cannot reach.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from astrabox.api import app as app_module
from astrabox.bootstrap import BootstrapConfigError, _resolve_enabled_sandbox_backends
from astrabox.core.service.orchestrator import environment_schema
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.sandbox import (
    SandboxBackendDisabled,
    SandboxIsolationCapability,
    any_enabled_backend_supports_egress_credential_injection,
    enabled_sandbox_names,
    enabled_sandbox_permission_levels,
    installed_sandbox_names,
    register_sandbox,
    sandbox_for_name,
    sandbox_for_sandbox,
    set_default_sandbox_backend,
    set_enabled_sandbox_backends,
)


class _Backend:
    """A registered backend with just the declarations the registry reads."""

    def __init__(
        self,
        name: str,
        *,
        levels: tuple[str, ...] = ("default",),
        egress: bool = False,
    ) -> None:
        self.name = name
        self.supported_permission_levels = levels
        self.supports_egress_credential_injection = egress

    def owns_sandbox(self, sandbox: Any) -> bool:
        return getattr(sandbox, "backend", None) == self.name

    async def read_isolation_capability(self, sandbox_id: str) -> SandboxIsolationCapability:
        return SandboxIsolationCapability(sandbox_id=sandbox_id, available=True)

    async def apply_credential_vault(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no case here writes a credential vault")


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", {})
    monkeypatch.setattr(sandbox_seam, "_ENABLED_BACKENDS", None)
    monkeypatch.setattr(sandbox_seam, "_DEFAULT_BACKEND", "")


def _install_two() -> tuple[_Backend, _Backend]:
    """``primary`` is the default; ``other`` offers more than ``primary`` does."""
    primary = _Backend("primary")
    other = _Backend("other", levels=("default", "advanced"), egress=True)
    register_sandbox(primary)  # type: ignore[arg-type]
    register_sandbox(other)  # type: ignore[arg-type]
    set_default_sandbox_backend("primary")
    return primary, other


def test_a_record_naming_a_disabled_backend_is_refused_not_redirected() -> None:
    primary, _other = _install_two()
    set_enabled_sandbox_backends(["primary"])

    assert sandbox_for_name("primary") is primary
    assert sandbox_for_name("") is primary
    with pytest.raises(SandboxBackendDisabled) as refused:
        sandbox_for_name("other")
    # A record naming the disabled backend must not reach the default one: a
    # box created by one backend is not addressable through another.
    assert refused.value.code == "SANDBOX_BACKEND_DISABLED"
    assert refused.value.status_code == 409
    assert "'other'" in refused.value.message
    assert "ASTRABOX_SANDBOX_BACKENDS" in refused.value.message
    # A name nothing installed is a different fault and keeps its own error.
    with pytest.raises(RuntimeError, match="installed"):
        sandbox_for_name("ghost")


def test_only_enabled_backends_answer_registry_questions() -> None:
    _install_two()
    set_enabled_sandbox_backends(["primary"])

    assert installed_sandbox_names() == ["other", "primary"]
    assert enabled_sandbox_names() == ["primary"]
    # The Environment form offers what this deployment runs, nothing more.
    assert environment_schema._field_enum({"key": "sandbox_backend"}) == ["primary"]
    # `other` attests `advanced` and injects egress credentials, but no
    # Session in this deployment can run on it.
    assert enabled_sandbox_permission_levels() == ["default"]
    assert any_enabled_backend_supports_egress_credential_injection() is False
    assert sandbox_for_sandbox(SimpleNamespace(backend="other")) is None


def test_every_enabled_backend_answers() -> None:
    primary, other = _install_two()
    set_enabled_sandbox_backends(["other", "primary"])

    assert sandbox_for_name("primary") is primary
    assert sandbox_for_name("other") is other
    assert enabled_sandbox_permission_levels() == ["default", "advanced"]
    assert any_enabled_backend_supports_egress_credential_injection() is True
    assert sandbox_for_sandbox(SimpleNamespace(backend="other")) is other


def test_registering_after_composition_does_not_enable_a_backend() -> None:
    _install_two()
    set_enabled_sandbox_backends(["primary"])

    register_sandbox(_Backend("late"))  # type: ignore[arg-type]

    assert "late" in installed_sandbox_names()
    assert "late" not in enabled_sandbox_names()
    with pytest.raises(SandboxBackendDisabled):
        sandbox_for_name("late")


def test_an_uninstalled_backend_cannot_be_enabled() -> None:
    _install_two()

    with pytest.raises(RuntimeError, match="not installed"):
        set_enabled_sandbox_backends(["primary", "ghost"])


@pytest.mark.parametrize(
    ("configured", "message"),
    [
        (["ghost"], "no installed provider registers"),
        (["primary", "primary"], "more than once"),
        (["other"], "does not enable the default"),
    ],
)
def test_startup_refuses_an_enabled_set_that_cannot_work(
    configured: list[str], message: str
) -> None:
    _install_two()

    with pytest.raises(BootstrapConfigError, match=message):
        _resolve_enabled_sandbox_backends(configured)


def test_an_empty_setting_enables_only_the_default_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_two()
    monkeypatch.delenv("ASTRABOX_SANDBOX_BACKENDS", raising=False)
    from astrabox.config.settings import get_settings

    get_settings.cache_clear()
    try:
        assert _resolve_enabled_sandbox_backends(None) == ["primary"]
    finally:
        get_settings.cache_clear()


def test_the_setting_is_read_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_two()
    monkeypatch.setenv("ASTRABOX_SANDBOX_BACKENDS", " Primary , other ")
    from astrabox.config.settings import get_settings

    get_settings.cache_clear()
    try:
        assert _resolve_enabled_sandbox_backends(None) == ["primary", "other"]
    finally:
        get_settings.cache_clear()


async def test_startup_names_enabled_environments_on_a_disabled_backend(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_two()
    set_enabled_sandbox_backends(["primary"])
    stored = AsyncMock()
    stored.list_all.return_value = [
        {"name": "on-other", "enabled": True, "sandbox_backend": "other"},
        {"name": "switched-off", "enabled": False, "sandbox_backend": "other"},
        {"name": "on-the-default", "sandbox_backend": ""},
        {"name": "on-primary", "sandbox_backend": "primary"},
    ]
    monkeypatch.setattr(
        "astrabox.persistence.repository.EnvironmentRepository", lambda: stored
    )

    with caplog.at_level(logging.WARNING):
        await app_module._warn_about_environments_on_disabled_backends()

    named = [
        record.getMessage()
        for record in caplog.records
        if "SANDBOX_BACKEND_DISABLED" in record.getMessage()
    ]
    assert len(named) == 1
    assert "'on-other'" in named[0] and "'other'" in named[0]
