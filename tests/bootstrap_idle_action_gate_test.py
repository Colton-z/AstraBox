"""The composition root refuses an idle action the selected backend cannot carry out.

``ASTRABOX_SANDBOX_IDLE_ACTION`` answers the half of idle handling that
``ASTRABOX_SANDBOX_LEASE_SECONDS`` does not: the lease says how long an unused
sandbox lives, this says what happens when that runs out.

``pause`` is a promise about DATA — that an abandoned conversation can be picked
up later with its workspace intact. A backend that cannot snapshot would go on
terminating, and nothing would look wrong: boxes would still be reclaimed, on
schedule, and the loss would surface only as users finding their files gone. A
setting whose failure mode is silent data loss is either effective or fatal, so
it is checked where every other unhonourable combination is — at composition,
with the backend named.

These pin the four answers: refuse on a backend that cannot pause, allow on one
that can, refuse a value that is neither, and stay out of the way at the default
(which is what every other test in the suite runs under).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import astrabox.seams.sandbox as sandbox_seam
import astrabox.bootstrap as bootstrap_module
from astrabox.providers import model as _model_provider  # noqa: F401
from astrabox.bootstrap import BootstrapConfigError, _assert_idle_action_is_honored
from astrabox.seams.sandbox import SandboxProvider, register_sandbox


class _Provider(SandboxProvider):
    """Every abstract stubbed; only ``supports_pause`` matters to this gate."""

    def __init__(self, name: str, *, can_pause: bool) -> None:
        self.name = name
        self.supports_pause = can_pause

    def connection_config(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def secret_material(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def build_dataplane(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def kill(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _isolated_sandbox_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", dict(sandbox_seam._BACKENDS))


@pytest.fixture(autouse=True)
def _clean_idle_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ASTRABOX_SANDBOX_IDLE_ACTION", raising=False)
    monkeypatch.setenv("ASTRABOX_AUTH_SESSION_SECRET", "test-only-model-signing-secret")
    monkeypatch.delenv("ASTRABOX_LITELLM_API_KEY", raising=False)


def test_pause_on_a_backend_that_cannot_snapshot_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_sandbox(_Provider("no-snapshots", can_pause=False))
    monkeypatch.setenv("ASTRABOX_SANDBOX_IDLE_ACTION", "pause")

    with pytest.raises(BootstrapConfigError) as excinfo:
        _assert_idle_action_is_honored("no-snapshots")

    message = str(excinfo.value)
    # Named backend, named consequence, and both ways out — an operator must not
    # have to read source to learn why the process will not start.
    assert "no-snapshots" in message
    assert "workspace" in message
    assert "terminate" in message


def test_pause_is_allowed_on_a_backend_that_can(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob is not decorative: a snapshotting backend gets to use it."""
    register_sandbox(_Provider("snapshots", can_pause=True))
    monkeypatch.setenv("ASTRABOX_SANDBOX_IDLE_ACTION", "pause")

    _assert_idle_action_is_honored("snapshots")


def test_an_unknown_action_is_refused_rather_than_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not read as 'terminate' and destroy what was meant to be kept."""
    register_sandbox(_Provider("snapshots", can_pause=True))
    monkeypatch.setenv("ASTRABOX_SANDBOX_IDLE_ACTION", "pasue")

    with pytest.raises(BootstrapConfigError) as excinfo:
        _assert_idle_action_is_honored("snapshots")
    assert "pasue" in str(excinfo.value)


def test_the_default_asks_nothing_of_the_backend() -> None:
    """Terminate is what has always happened, so it must not need a capability.

    This is also what keeps the gate out of the way of every deployment that
    never sets the variable — including the rest of this suite.
    """
    register_sandbox(_Provider("no-snapshots", can_pause=False))
    _assert_idle_action_is_honored("no-snapshots")


def test_the_seam_default_is_that_a_backend_cannot_pause() -> None:
    """So a backend must OPT IN, and one that never considered it is refused."""
    assert SandboxProvider.supports_pause is False


def test_the_open_sandbox_backend_declares_it_can() -> None:
    from astrabox.providers.open_sandbox.sandbox import OpenSandboxSandboxProvider

    assert OpenSandboxSandboxProvider.supports_pause is True


# ── the box-facing callback base must be routable from a sandbox ──────────────


def test_kubernetes_runtime_refuses_a_derived_bridge_callback_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unset + inside a container derives this server's own Docker-bridge IP, which
    # is right for the quickstart and unroutable from a Pod. Measured cost of not
    # catching it: the in-box transcript mirror times out 3x30s INSIDE the first
    # turn, so a one-word answer took ~93s and read as a slow model.
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "kubernetes")
    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)

    class _Settings:
        mcp_proxy_base_url = "http://172.17.0.3:8000"

    monkeypatch.setattr(
        "astrabox.common.utils.settings.load_astrabox_settings", lambda: _Settings()
    )
    with pytest.raises(bootstrap_module.BootstrapConfigError) as caught:
        bootstrap_module._assert_box_callback_base_is_reachable()
    assert "Pod" in str(caught.value)
    assert "172.17.0.3" in str(caught.value)


def test_an_explicit_callback_base_is_never_second_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "kubernetes")
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://10.0.1.7:8088")
    bootstrap_module._assert_box_callback_base_is_reachable()


def test_the_docker_runtime_keeps_the_derived_bridge_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On the docker runtime a sandbox IS a container on that bridge, so the derived
    # address is the correct one and must not be refused.
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "docker")
    monkeypatch.delenv("ASTRABOX_MCP_PROXY_BASE_URL", raising=False)
    bootstrap_module._assert_box_callback_base_is_reachable()


# ── a team gateway can require an HTTPS sandbox-facing endpoint ─────────────


def _model_gateway_settings(*, require_https: bool, vault_enabled: bool = True) -> Any:
    return type(
        "_Settings",
        (),
        {
            "model_endpoint_provider": "litellm",
            "model_gateway_require_https": require_https,
            "sandbox_credential_vault_enabled": vault_enabled,
        },
    )()


def _patch_model_gateway_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    require_https: bool,
    vault_enabled: bool = True,
) -> None:
    current = _model_gateway_settings(
        require_https=require_https,
        vault_enabled=vault_enabled,
    )
    monkeypatch.setattr(
        "astrabox.common.utils.settings.load_astrabox_settings",
        lambda: current,
    )


def test_https_requirement_refuses_the_embedded_http_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=True)
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)

    with pytest.raises(BootstrapConfigError) as caught:
        bootstrap_module._assert_model_gateway_https_requirement()

    message = str(caught.value)
    assert "ASTRABOX_LITELLM_BASE_URL" in message
    assert "https://" in message


@pytest.mark.parametrize(
    "base_url",
    [
        "http://gateway.example.com",
        "https://gateway.example.com:8443",
        "https://192.0.2.10",
        "https://gateway",
    ],
)
def test_https_requirement_refuses_a_gateway_the_vault_cannot_secure(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=True)
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", base_url)

    with pytest.raises(BootstrapConfigError) as caught:
        bootstrap_module._assert_model_gateway_https_requirement()

    assert base_url in str(caught.value)


@pytest.mark.parametrize("vault_enabled", [True, False])
def test_https_requirement_accepts_a_standard_https_gateway_independently_of_vault(
    monkeypatch: pytest.MonkeyPatch,
    vault_enabled: bool,
) -> None:
    _patch_model_gateway_settings(
        monkeypatch,
        require_https=True,
        vault_enabled=vault_enabled,
    )
    monkeypatch.setenv(
        "ASTRABOX_LITELLM_BASE_URL",
        "https://gateway.example.com/v1",
    )

    bootstrap_module._assert_model_gateway_https_requirement()


def test_external_http_gateway_logs_a_security_error_when_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=False)
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "http://gateway.example.com")

    with caplog.at_level(logging.ERROR):
        bootstrap_module._assert_model_gateway_https_requirement()

    assert "SECURITY" in caplog.text
    assert "ASTRABOX_MODEL_GATEWAY_REQUIRE_HTTPS" in caplog.text


def test_embedded_local_gateway_does_not_emit_the_external_http_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_model_gateway_settings(monkeypatch, require_https=False)
    monkeypatch.delenv("ASTRABOX_LITELLM_BASE_URL", raising=False)

    with caplog.at_level(logging.ERROR):
        bootstrap_module._assert_model_gateway_https_requirement()

    assert "SECURITY" not in caplog.text
