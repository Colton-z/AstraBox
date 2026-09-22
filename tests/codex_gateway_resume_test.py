"""Codex carries one platform-resolved model access value into its box contract.

The platform resolves an Environment's model access once. The Codex adapter
declares how that neutral value is consumed, and the platform provisioning
layer turns the declaration into network and credential delivery. Neither
side may quietly omit an endpoint: that would let the server boot and defer a
configuration error until the first turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine import provisioning
from astrabox.core.service.orchestrator.engine.codex import CodexEngineAdapter
from astrabox.core.service.orchestrator.engine.provisioning import (
    resolve_model_credential_delivery,
)
from astrabox.seams.model import ResolvedModelAccess


class _Manager:
    def __init__(self, base_url: str | None) -> None:
        self._base_url = base_url

    def resolve_model_access(self, model_config: dict) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration=model_config,
            base_url=self._base_url,
            model_name="m",
            credential="sk-model",
            credential_kind="bearer",
            endpoint_provider="test",
        )


class _Backend:
    name = "fake"
    supports_create_network_policy = True
    supports_egress_credential_injection = True


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        engine_kind="codex",
        model_config={},
        engine_options={},
        networking={"type": "limited", "allowed_hosts": []},
        skills=None,
        tracing=None,
    )


def test_the_resolved_access_and_its_endpoint_reach_provisioning_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter and platform consume the same resolution, never two reads."""

    monkeypatch.setattr(
        provisioning,
        "load_astrabox_settings",
        lambda: SimpleNamespace(sandbox_credential_vault_enabled=False),
    )
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.config_resolver."
        "platform_callback_egress_targets",
        lambda: [],
    )
    template = _template()
    access = _Manager("http://gw.test").resolve_model_access(template.model_config)
    request = CodexEngineAdapter().sandbox_request(
        template=template,
        model_access=access,
    )

    credential, network_policy, vault_write = resolve_model_credential_delivery(
        template=template,
        backend_adapter=_Backend(),
        credential=request.credential,
    )

    assert request.credential.access is access
    assert credential == "sk-model"
    assert network_policy.allowed_hosts == ("gw.test",)
    assert vault_write is None


@pytest.mark.parametrize("missing", ["", "   ", None])
def test_an_unresolvable_gateway_is_refused_not_omitted(missing: str | None) -> None:
    """A box with no model endpoint is rejected before any engine is started."""

    template = _template()
    access = _Manager(missing).resolve_model_access(template.model_config)
    request = CodexEngineAdapter().sandbox_request(
        template=template,
        model_access=access,
    )

    with pytest.raises(APIError) as caught:
        resolve_model_credential_delivery(
            template=template,
            backend_adapter=_Backend(),
            credential=request.credential,
        )

    assert caught.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"
    assert "model gateway credential/base URL" in str(caught.value.message)
