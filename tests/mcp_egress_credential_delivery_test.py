"""Managed MCP credentials stay in the egress sidecar after relay removal."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator.runtime import mcp_credentials as delivery
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.providers.open_sandbox.credential_vault import (
    open_sandbox_vault_write,
)
from astrabox.providers.open_sandbox.executor import apply_open_sandbox_vault
from astrabox.seams.egress_credentials import (
    MCPOutboundCredential,
    MCPOutboundCredentialResolution,
    SandboxEgressCredentialPlan,
)
from astrabox.seams.extensions import MCPGatewayCredential


class _Platform:
    def __init__(
        self,
        credentials: list[MCPOutboundCredential],
        *,
        scope_id: str = "scope-1",
    ) -> None:
        self.credentials = credentials
        self.scope_id = scope_id
        self.calls: list[tuple[str, list[str]]] = []

    async def resolve_session_mcp_credentials(
        self, session_id: str, server_urls: list[str]
    ) -> MCPOutboundCredentialResolution:
        self.calls.append((session_id, list(server_urls)))
        return MCPOutboundCredentialResolution(
            scope_id=self.scope_id,
            credentials=tuple(self.credentials),
        )


def _template(config: dict[str, Any]) -> Any:
    return SimpleNamespace(mcp_servers={"research": config})


def _credential(
    *,
    target: str = "https://upstream.example.test/mcp",
    headers: dict[str, str] | None = None,
) -> MCPOutboundCredential:
    return MCPOutboundCredential(
        credential_id="cred-1",
        target_url=target,
        headers=headers or {"apikey": "upstream-secret"},
    )


def _provider_write(
    plan: SandboxEgressCredentialPlan | None,
) -> tuple[list[Any], list[Any]]:
    assert plan is not None
    translated = open_sandbox_vault_write(plan)
    assert translated is not None
    return translated


@pytest.mark.parametrize(
    ("session_id", "session"),
    [
        ("", None),
        ("missing-session", None),
        ("malformed-session", {"session_id": "malformed-session"}),
    ],
)
async def test_mcp_resolution_fails_when_session_credential_context_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
    session: dict[str, Any] | None,
) -> None:
    class _Sessions:
        async def get_session(self, _session_id: str) -> dict[str, Any] | None:
            return session

    monkeypatch.setattr(runtime_manager_module, "SessionRepository", _Sessions)

    with pytest.raises(APIError) as caught:
        await RemoteAgentRuntimeManager.resolve_session_mcp_credentials(
            SimpleNamespace(),
            session_id,
            ["https://mcp.example.test/mcp"],
        )

    assert caught.value.code == "SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE"


async def test_saved_header_is_bound_to_the_direct_server_not_engine_config() -> None:
    target = "https://mcp.example.test/v1/mcp"
    platform = _Platform([_credential(target=target)])

    plan = await delivery.resolve_mcp_credential_plan(
        platform,
        session_id="session-1",
        template=_template(
            {
                "provider": "builtin",
                "type": "http",
                "url": target,
                "credential_target_url": target,
            }
        ),
        vault_enabled=True,
    )

    credentials, bindings = _provider_write(plan)
    assert platform.calls == [("session-1", [target])]
    assert [item.source.value for item in credentials] == ["upstream-secret"]
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.match.hosts == ["mcp.example.test"]
    assert binding.match.paths == ["/v1/mcp"]
    assert binding.match.methods == ["GET", "POST", "DELETE"]
    assert binding.auth.type == "customHeaders"
    assert [item.name for item in binding.auth.headers or []] == ["apikey"]
    assert "upstream-secret" not in str(binding.model_dump())


async def test_two_values_for_one_header_fail_without_exposing_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = MCPGatewayCredential(
        header="Authorization",
        value="gateway-key",
        base_url="https://gateway.example.test",
    )
    monkeypatch.setattr(
        delivery,
        "extension_provider_for_name",
        lambda _name: SimpleNamespace(mcp_gateway_credential=lambda: gateway),
    )
    platform = _Platform(
        [_credential(headers={"authorization": "Bearer user-token"})]
    )

    with pytest.raises(APIError) as caught:
        await delivery.resolve_mcp_credential_plan(
            platform,
            session_id="session-1",
            template=_template(
                {
                    "provider": "catalog",
                    "type": "http",
                    "url": "https://gateway.example.test/research/mcp",
                    "credential_target_url": "https://upstream.example.test/mcp",
                }
            ),
            vault_enabled=True,
        )

    assert caught.value.code == "VAULT_CREDENTIAL_CONFLICT"
    assert "gateway-key" not in str(caught.value)
    assert "user-token" not in str(caught.value)


async def test_engine_config_cannot_also_claim_an_egress_owned_header() -> None:
    target = "https://mcp.example.test/mcp"

    with pytest.raises(APIError) as caught:
        await delivery.resolve_mcp_credential_plan(
            _Platform([_credential(target=target)]),
            session_id="session-1",
            template=_template(
                {
                    "type": "http",
                    "url": target,
                    "headers": {"ApiKey": "engine-config-value"},
                }
            ),
            vault_enabled=True,
        )

    assert caught.value.code == "VAULT_CREDENTIAL_CONFLICT"
    assert "ApiKey" not in str(caught.value)
    assert "engine-config-value" not in str(caught.value)
    assert "apikey" in str(caught.value).lower()


async def test_authenticated_server_is_refused_when_protected_delivery_is_off() -> None:
    target = "https://mcp.example.test/mcp"
    with pytest.raises(APIError) as caught:
        await delivery.resolve_mcp_credential_plan(
            _Platform([_credential(target=target)]),
            session_id="session-1",
            template=_template(
                {
                    "provider": "builtin",
                    "type": "http",
                    "url": target,
                    "credential_target_url": target,
                }
            ),
            vault_enabled=False,
        )

    assert caught.value.code == "SANDBOX_CREDENTIAL_VAULT_DISABLED"


async def test_archive_refresh_removes_the_sidecar_copy_of_the_secret() -> None:
    target = "https://mcp.example.test/mcp"
    template = _template(
        {
            "provider": "builtin",
            "type": "http",
            "url": target,
            "credential_target_url": target,
        }
    )
    active_plan = await delivery.resolve_mcp_credential_plan(
        _Platform([_credential(target=target)]),
        session_id="session-1",
        template=template,
        vault_enabled=True,
    )
    archived_plan = await delivery.resolve_mcp_credential_plan(
        _Platform([]),
        session_id="session-1",
        template=template,
        vault_enabled=True,
    )
    active = _provider_write(active_plan)
    archived = _provider_write(archived_plan)

    class _Vault:
        def __init__(self) -> None:
            self.patch_kwargs: dict[str, Any] | None = None

        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError("credential vault already exists")

        async def get(self) -> Any:
            return SimpleNamespace(
                revision=3,
                credentials=[SimpleNamespace(name=active[0][0].name)],
                bindings=[SimpleNamespace(name=active[1][0].name)],
            )

        async def patch(self, **kwargs: Any) -> None:
            self.patch_kwargs = kwargs

    vault = _Vault()
    handle = SimpleNamespace(
        sandbox_id="sandbox-1",
        sidecar_faces=SimpleNamespace(credential_vault=vault),
    )
    await apply_open_sandbox_vault(
        handle,
        vault_write=archived,
        session_id="session-1",
        managed_binding_names=(active[1][0].name,),
    )

    assert vault.patch_kwargs is not None
    assert vault.patch_kwargs["credentials"] == {
        "delete": [active[0][0].name]
    }
    assert vault.patch_kwargs["bindings"]["delete"] == [active[1][0].name]
    assert vault.patch_kwargs["bindings"]["add"][0].auth.type == "passthrough"


async def test_sidecar_create_failure_cannot_echo_an_mcp_secret() -> None:
    target = "https://mcp.example.test/mcp"
    plan = await delivery.resolve_mcp_credential_plan(
        _Platform([_credential(target=target)]),
        session_id="session-1",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    write = _provider_write(plan)
    secret = write[0][0].source.value

    class _Vault:
        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError(f"sidecar echoed {secret}")

    handle = SimpleNamespace(
        sandbox_id="sandbox-1",
        sidecar_faces=SimpleNamespace(credential_vault=_Vault()),
    )
    with pytest.raises(RuntimeError) as caught:
        await apply_open_sandbox_vault(
            handle,
            vault_write=write,
            session_id="session-1",
        )

    assert secret not in str(caught.value)
    assert "sidecar echoed ***" in str(caught.value)
    assert caught.value.__cause__ is None


async def test_sidecar_patch_failure_cannot_echo_a_rotated_mcp_secret() -> None:
    target = "https://mcp.example.test/mcp"
    plan = await delivery.resolve_mcp_credential_plan(
        _Platform([_credential(target=target)]),
        session_id="session-1",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    write = _provider_write(plan)
    secret = write[0][0].source.value

    class _Vault:
        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError("credential vault already exists")

        async def get(self) -> Any:
            return SimpleNamespace(revision=7, credentials=[], bindings=[])

        async def patch(self, **_kwargs: Any) -> None:
            raise RuntimeError(f"patch echoed {secret}")

    handle = SimpleNamespace(
        sandbox_id="sandbox-1",
        sidecar_faces=SimpleNamespace(credential_vault=_Vault()),
    )
    with pytest.raises(RuntimeError) as caught:
        await apply_open_sandbox_vault(
            handle,
            vault_write=write,
            session_id="session-1",
        )

    assert secret not in str(caught.value)
    assert "patch echoed ***" in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("authenticated_first", [True, False])
async def test_shared_box_refuses_authenticated_and_anonymous_scopes_for_same_target(
    authenticated_first: bool,
) -> None:
    target = "https://mcp.example.test/mcp"
    authenticated_plan = await delivery.resolve_mcp_credential_plan(
        _Platform([_credential(target=target)], scope_id="vault-scope-auth"),
        session_id="session-auth",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    anonymous_plan = await delivery.resolve_mcp_credential_plan(
        _Platform([], scope_id="vault-scope-anonymous"),
        session_id="session-anonymous",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    authenticated = _provider_write(authenticated_plan)
    anonymous = _provider_write(anonymous_plan)
    existing, desired = (
        (authenticated, anonymous)
        if authenticated_first
        else (anonymous, authenticated)
    )

    class _Vault:
        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError("credential vault already exists")

        async def get(self) -> Any:
            return SimpleNamespace(
                revision=4,
                credentials=[
                    SimpleNamespace(name=item.name) for item in existing[0]
                ],
                bindings=[SimpleNamespace(name=existing[1][0].name)],
            )

        async def patch(self, **_kwargs: Any) -> None:
            raise AssertionError("an incompatible scope must be refused before patch")

    handle = SimpleNamespace(
        sandbox_id="sandbox-shared",
        sidecar_faces=SimpleNamespace(credential_vault=_Vault()),
    )
    with pytest.raises(APIError) as caught:
        await apply_open_sandbox_vault(
            handle,
            vault_write=desired,
            session_id="session-2",
        )

    assert caught.value.code == "VAULT_CREDENTIAL_CONFLICT"


async def test_shared_box_refuses_a_different_credential_source_for_same_header() -> None:
    target = "https://mcp.example.test/mcp"
    first_plan = await delivery.resolve_mcp_credential_plan(
        _Platform([_credential(target=target)], scope_id="vault-scope-1"),
        session_id="session-1",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    second_plan = await delivery.resolve_mcp_credential_plan(
        _Platform(
            [
                MCPOutboundCredential(
                    credential_id="cred-2",
                    target_url=target,
                    headers={"apikey": "another-secret"},
                )
            ],
            scope_id="vault-scope-2",
        ),
        session_id="session-2",
        template=_template({"type": "http", "url": target}),
        vault_enabled=True,
    )
    first = _provider_write(first_plan)
    second = _provider_write(second_plan)

    class _Vault:
        async def create(self, **_kwargs: Any) -> None:
            raise RuntimeError("credential vault already exists")

        async def get(self) -> Any:
            return SimpleNamespace(
                revision=4,
                credentials=[SimpleNamespace(name=first[0][0].name)],
                bindings=[SimpleNamespace(name=first[1][0].name)],
            )

        async def patch(self, **_kwargs: Any) -> None:
            raise AssertionError("a conflicting source must be refused before patch")

    handle = SimpleNamespace(
        sandbox_id="sandbox-shared",
        sidecar_faces=SimpleNamespace(credential_vault=_Vault()),
    )
    with pytest.raises(APIError) as caught:
        await apply_open_sandbox_vault(
            handle,
            vault_write=second,
            session_id="session-2",
        )

    assert caught.value.code == "VAULT_CREDENTIAL_CONFLICT"
    assert "upstream-secret" not in str(caught.value)
    assert "another-secret" not in str(caught.value)
