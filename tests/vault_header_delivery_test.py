"""Header-delivery Vault credentials reach only a backend that performs them.

An ``environment_variable`` credential with ``delivery="header"`` is delivered
by the sandbox backend itself: the box holds a placeholder, and the backend
replaces it inside one named request header for the listed domains. The
platform's part is a security boundary with two sides, and these cases pin
both on the PostgreSQL test store:

* With ``ASTRABOX_SANDBOX_CREDENTIAL_VAULT`` off — the configuration of a
  deployment whose model credentials take another path — the credential still
  reaches the plan handed to a backend that declares
  ``supports_header_placeholder_credentials``, and the Agent process is given
  the placeholder and never the value.
* A backend that does not declare it is refused before a sandbox exists, even
  when it substitutes credentials at its egress, and a Vault refuses to store a
  header credential no enabled backend can deliver, or one carrying rules that
  header delivery would silently not apply.

Revocation is part of the same boundary: an environment credential archived or
deleted after a live box received it must leave that box's egress vault when
the box is next refreshed, even when nothing else is written.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 - registers the adapter an agent_chat Session defaults to
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.user_context import UserContext
from astrabox.core.service.orchestrator import runtime_manager as runtime_manager_module
from astrabox.core.service.orchestrator import session_service as session_service_module
from astrabox.core.service.orchestrator.engine.provisioning import (
    resolve_session_environment_credentials,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.session_service import SessionService
from astrabox.core.service.orchestrator.vault_service import VaultService
from astrabox.seams import sandbox as sandbox_seam
from astrabox.seams.egress_credentials import (
    DELIVERY_HEADER,
    DELIVERY_SUBSTITUTION,
    EgressCredential,
    SandboxEgressCredentialPlan,
    mint_placeholder,
)

pytestmark = pytest.mark.usefixtures("database")

_USER = UserContext(user_id="vault-admin")
_SECRET = "tk-live-secret-that-never-enters-the-box"
_HOSTS = ["api.tracker.example", "*.tracker.example"]


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed master key, so the local store needs no key file."""
    monkeypatch.delenv("ASTRABOX_SECRET_STORE", raising=False)
    monkeypatch.setenv(
        "ASTRABOX_VAULT_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode()
    )


@pytest.fixture(autouse=True)
def _environment_membership(environment_member_accounts: set[str]) -> None:
    environment_member_accounts.add(_USER.user_id)


@pytest.fixture
def backends(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make the given providers the deployment's only enabled backends.

    Registration goes through ``register_sandbox``, so a fake that declared a
    capability without its obligations would be refused here as in production.
    """
    monkeypatch.setattr(sandbox_seam, "_BACKENDS", {})
    monkeypatch.setattr(sandbox_seam, "_ENABLED_BACKENDS", None)

    def install(*providers: Any) -> None:
        for provider in providers:
            sandbox_seam.register_sandbox(provider)

    return install


class _NoSandbox:
    """Fails the test on any attempt to create or write to a sandbox."""

    name: str

    async def create_sandbox(self, _spec: Any) -> Any:
        raise AssertionError(f"{self.name} was asked to create a sandbox")

    async def apply_credential_vault(self, _sandbox: Any, **_kwargs: Any) -> None:
        raise AssertionError(f"{self.name} was asked to write credentials")


class _HeaderBackend(_NoSandbox):
    """Performs header delivery itself and has no egress substitution."""

    name = "header-backend"
    supports_header_placeholder_credentials = True


class _SubstitutionBackend(_NoSandbox):
    """Substitutes at its egress; that is not header delivery."""

    name = "substitution-backend"
    supports_egress_credential_injection = True


class _Harness:
    """The resolved Agent a conversation starts from, bound to one Vault."""

    def __init__(self, *, backend: str, vault_ids: list[str]) -> None:
        self._view = SimpleNamespace(
            name="Tracker triage",
            sandbox_backend=backend,
            credential_vault_ids=vault_ids,
        )

    async def resolve_session_harness(self, _session: dict[str, Any]) -> Any:
        return self._view


class _Runtime:
    @staticmethod
    def resolve_template_model_name(_template: Any) -> str | None:
        return "model-1"


class _CapturingSessions:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    async def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = dict(payload)
        return dict(payload)


class _SessionRows:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        return dict(self._row) if session_id == self._row["session_id"] else None


class _SessionPlatform:
    """The Session platform's credential reads, exactly as the runtime manager does them."""

    async def resolve_session_egress_credentials(
        self, session_id: str, *, placeholder_context: str | None = None
    ) -> list[Any]:
        return await RemoteAgentRuntimeManager.resolve_session_egress_credentials(
            self,  # type: ignore[arg-type]
            session_id,
            placeholder_context=placeholder_context,
        )

    async def session_placeholder_context(self, session_id: str) -> str:
        return await RemoteAgentRuntimeManager.session_placeholder_context(
            self,  # type: ignore[arg-type]
            session_id,
        )


def _header_auth(**extra: Any) -> dict[str, Any]:
    return {
        "type": "environment_variable",
        "secret_name": "TRACKER_API_KEY",
        "secret_value": _SECRET,
        "delivery": "header",
        "header_name": "X-Api-Key",
        "networking": {"type": "limited", "allowed_hosts": list(_HOSTS)},
        **extra,
    }


def _session_service(
    sessions: _CapturingSessions, *, backend: Any, vault_ids: list[str], vaults: VaultService,
    monkeypatch: pytest.MonkeyPatch,
) -> SessionService:
    monkeypatch.setattr(session_service_module, "sandbox_for_name", lambda _name: backend)
    return SessionService(
        sessions_repo=sessions,  # type: ignore[arg-type]
        agent_config=_Harness(backend=backend.name, vault_ids=vault_ids),  # type: ignore[arg-type]
        runtime_manager=_Runtime(),  # type: ignore[arg-type]
        ttl_seconds=3600,
        spawn_background_task=lambda *args, **kwargs: None,
        vault_service=vaults,
    )


async def _vault_with_header_credential(vaults: VaultService) -> tuple[str, dict[str, Any]]:
    vault_id = (await vaults.create_vault(_USER, display_name="tracker"))["vault_id"]
    credential = await vaults.create_credential(_USER, vault_id, auth=_header_auth())
    return vault_id, credential


async def test_header_credential_reaches_the_plan_with_the_vault_switched_off(
    backends: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deployment this mode exists for: the platform vault off, and a
    # backend that replaces the placeholder itself. Each step below must
    # accept the credential without the vault.
    monkeypatch.setenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", "false")
    header_backend = _HeaderBackend()
    backends(header_backend)
    vaults = VaultService()
    vault_id, credential = await _vault_with_header_credential(vaults)
    assert credential["auth"] == {
        "type": "environment_variable",
        "header_name": "X-Api-Key",
        "secret_name": "TRACKER_API_KEY",
        "delivery": DELIVERY_HEADER,
        "networking": {"type": "limited", "allowed_hosts": _HOSTS},
    }
    assert _SECRET not in json.dumps(credential)

    sessions = _CapturingSessions()
    service = _session_service(
        sessions, backend=header_backend, vault_ids=[vault_id], vaults=vaults,
        monkeypatch=monkeypatch,
    )
    await service.create_session_record(
        _USER, workspace_ref={"kind": "agent", "agent_id": "agent-1"}, agent_id="agent-1",
    )
    row = sessions.payload
    assert row is not None and row["vault_ids"] == [vault_id]

    # Provisioning reads the Session row the way every start, claim and
    # reattach does, with the deployment's own vault switch, and mints under
    # the context a cold create records for the box.
    monkeypatch.setattr(runtime_manager_module, "SessionRepository", lambda: _SessionRows(row))
    vault_enabled = load_astrabox_settings().sandbox_credential_vault_enabled
    assert vault_enabled is False
    platform = _SessionPlatform()
    context = await platform.session_placeholder_context(row["session_id"])
    plan, runtime_env = await resolve_session_environment_credentials(
        platform,
        session_id=row["session_id"],
        vault_enabled=vault_enabled,
        vault_write=None,
        backend=header_backend,
        placeholder_context=context,
    )

    assert plan is not None
    (item,) = plan.environment
    assert item.delivery == DELIVERY_HEADER
    assert item.header_name == "X-Api-Key"
    assert item.networking == {"type": "limited", "allowed_hosts": _HOSTS}
    assert item.secret_value == _SECRET
    # The placeholder is the one this Session's sandbox generation keeps
    # stable, so the process and the backend name the same value.
    assert item.placeholder == mint_placeholder(
        credential["credential_id"],
        context=f"{row['session_id']}:{row['sandbox_generation']}",
    )
    # Nothing that needs the platform vault rides along: no model or MCP
    # binding, and no empty HTTP Basic selection for a backend that keeps none.
    assert (plan.model, plan.mcp, plan.http_basic) == ((), (), ())
    assert runtime_env == {"TRACKER_API_KEY": item.placeholder}
    assert _SECRET not in json.dumps(runtime_env)

    # A runtime that recorded no placeholder context cannot have the
    # placeholders its process holds reproduced; minting fresh ones would make
    # the backend bind values the process never sends, so it is refused.
    with pytest.raises(APIError) as unrecorded:
        await resolve_session_environment_credentials(
            platform,
            session_id=row["session_id"],
            vault_enabled=vault_enabled,
            vault_write=None,
            backend=header_backend,
        )
    assert unrecorded.value.code == "SANDBOX_CREDENTIAL_CONTEXT_UNAVAILABLE"


async def test_a_backend_without_header_delivery_is_refused_before_allocation(
    backends: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Header delivery is enabled somewhere in the deployment, so the Vault
    # accepts the credential; the Agent's own backend substitutes at its
    # egress instead, which must not be taken for header delivery.
    substitution_backend = _SubstitutionBackend()
    backends(_HeaderBackend(), substitution_backend)
    vaults = VaultService()
    vault_id, _credential = await _vault_with_header_credential(vaults)

    sessions = _CapturingSessions()
    service = _session_service(
        sessions, backend=substitution_backend, vault_ids=[vault_id], vaults=vaults,
        monkeypatch=monkeypatch,
    )
    with pytest.raises(APIError) as refused:
        await service.create_session_record(
            _USER, workspace_ref={"kind": "agent", "agent_id": "agent-1"}, agent_id="agent-1",
        )
    assert refused.value.code == "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
    assert refused.value.status_code == 400
    # No Session row, so nothing exists for a sandbox to be allocated to.
    assert sessions.payload is None

    # A Session that already exists (its Agent switched to this backend after
    # the Session was created) is refused by provisioning before its plan
    # reaches any sandbox, and with the vault switched on.
    monkeypatch.setattr(
        runtime_manager_module,
        "SessionRepository",
        lambda: _SessionRows(
            {"session_id": "session-1", "vault_ids": [vault_id], "sandbox_generation": "g-1"}
        ),
    )
    with pytest.raises(APIError) as provisioning_refused:
        await resolve_session_environment_credentials(
            _SessionPlatform(),
            session_id="session-1",
            vault_enabled=True,
            vault_write=None,
            backend=substitution_backend,
        )
    assert provisioning_refused.value.code == "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
    assert "supports_header_placeholder_credentials" in provisioning_refused.value.message


async def test_create_refuses_header_delivery_no_enabled_backend_performs(backends: Any) -> None:
    backends(_SubstitutionBackend())
    vaults = VaultService()
    vault_id = (await vaults.create_vault(_USER, display_name="no header backend"))["vault_id"]

    with pytest.raises(APIError) as refused:
        await vaults.create_credential(_USER, vault_id, auth=_header_auth())
    assert refused.value.code == "VAULT_ENV_CREDENTIAL_UNSUPPORTED"
    assert "supports_header_placeholder_credentials" in refused.value.message
    assert await vaults.list_credentials(_USER, vault_id) == []

    # The same variable as a substitution credential is what this deployment
    # can deliver, and its view says which delivery it has.
    substitution = await vaults.create_credential(
        _USER,
        vault_id,
        auth={
            "type": "environment_variable",
            "secret_name": "TRACKER_API_KEY",
            "secret_value": _SECRET,
            "networking": {"type": "limited", "allowed_hosts": ["api.tracker.example"]},
        },
    )
    assert substitution["auth"]["delivery"] == DELIVERY_SUBSTITUTION


@pytest.mark.parametrize(
    "extra",
    [
        # Rules header delivery has no way to honour: accepting them would
        # store a limit an administrator believes is enforced.
        {"injection_location": {"header": True, "body": True}},
        {"allowed_requests": {"methods": ["GET"]}},
        {"allowed_requests": {"paths": ["/v1/issues/*"]}},
        {"allow_insecure_http": True},
        # Destinations a backend matching by domain name cannot scope.
        {"networking": {"type": "unrestricted"}},
        {"networking": {"type": "limited", "allowed_hosts": ["api.tracker.example:443"]}},
        {"networking": {"type": "limited", "allowed_hosts": ["https://api.tracker.example"]}},
        {"networking": {"type": "limited", "allowed_hosts": ["10.0.0.7"]}},
        {"networking": {"type": "limited", "allowed_hosts": ["tracker"]}},
        {"networking": {"type": "limited", "allowed_hosts": ["api.*.example"]}},
        # Without a valid header there is nowhere the placeholder is replaced.
        {"header_name": ""},
        {"header_name": "X Api Key"},
    ],
)
async def test_header_delivery_refuses_what_it_would_not_enforce(
    backends: Any, extra: dict[str, Any]
) -> None:
    backends(_HeaderBackend())
    vaults = VaultService()
    vault_id = (await vaults.create_vault(_USER, display_name="refusals"))["vault_id"]

    with pytest.raises(APIError) as refused:
        await vaults.create_credential(_USER, vault_id, auth=_header_auth(**extra))
    assert refused.value.code == "INVALID_REQUEST"
    assert refused.value.status_code == 400
    assert await vaults.list_credentials(_USER, vault_id) == []


async def test_header_delivery_is_fixed_once_created(backends: Any) -> None:
    backends(_HeaderBackend())
    vaults = VaultService()
    vault_id, credential = await _vault_with_header_credential(vaults)
    credential_id = credential["credential_id"]

    # Switching delivery would change what the stored hosts and header mean;
    # adding a substitution rule would store a limit nothing applies.
    for change in (
        {"delivery": DELIVERY_SUBSTITUTION},
        {"allowed_requests": {"methods": ["GET"]}},
        {"header_name": "Authorization"},
    ):
        with pytest.raises(APIError) as refused:
            await vaults.update_credential(_USER, vault_id, credential_id, auth=change)
        assert refused.value.status_code == 400

    rotated = await vaults.update_credential(
        _USER, vault_id, credential_id, auth={"secret_value": "tk-rotated"}
    )
    assert rotated["auth"] == credential["auth"]
    (resolved,) = await vaults.resolve_env_credentials([vault_id])
    assert (resolved.delivery, resolved.secret_value) == (DELIVERY_HEADER, "tk-rotated")


def _substitution_credential(credential_id: str, *, cleartext: bool = False) -> EgressCredential:
    hosts = ["api.tracker.example", *(["api.tracker.example:80"] if cleartext else [])]
    return EgressCredential(
        credential_id=credential_id,
        secret_name=f"TOKEN_{credential_id.upper().replace('-', '_')}",
        secret_value=f"{credential_id}-secret",
        placeholder=mint_placeholder(credential_id, context="session-1:generation-1"),
        networking={"type": "limited", "allowed_hosts": hosts},
        injection_location={"header": True, "body": False},
        allow_insecure_http=cleartext,
    )


async def test_a_withdrawn_environment_credential_leaves_a_live_vault() -> None:
    # A live box's vault holds credentials X (with its https and cleartext
    # bindings), Y and Z. X was archived after the box received it; Z belongs to
    # a sibling workload this refresh does not manage. The refresh names X and
    # Y as delivered before and carries Y, so X must go and Y and Z must stay.
    # Without the names, the archived X would keep being substituted.
    from unittest.mock import AsyncMock

    from astrabox.providers.open_sandbox.credential_vault import open_sandbox_vault_write
    from astrabox.providers.open_sandbox.executor import apply_open_sandbox_vault

    withdrawn = _substitution_credential("cred-x", cleartext=True)
    kept = _substitution_credential("cred-y")
    sibling = _substitution_credential("cred-z")
    existing = open_sandbox_vault_write(
        SandboxEgressCredentialPlan(environment=(withdrawn, kept, sibling))
    )
    desired = open_sandbox_vault_write(SandboxEgressCredentialPlan(environment=(kept,)))
    assert existing is not None and desired is not None
    vault = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                revision=3, credentials=existing[0], bindings=existing[1]
            )
        ),
        patch=AsyncMock(),
    )
    handle = SimpleNamespace(
        sandbox_id="box-1", sidecar_faces=SimpleNamespace(credential_vault=vault)
    )

    await apply_open_sandbox_vault(
        handle,
        vault_write=desired,
        session_id="session-1",
        managed_credential_names=("cred-x", "cred-y"),
        create_if_missing=False,
    )
    mutations = vault.patch.call_args.kwargs
    assert mutations["credentials"]["delete"] == ["astrabox-vault-cred-x"]
    assert mutations["bindings"]["delete"] == [
        "astrabox-vault-cred-x",
        "astrabox-vault-cred-x-http",
    ]

    # Every delivered credential withdrawn and nothing else to write: the
    # refresh still reaches the vault and removes them.
    vault.patch.reset_mock()
    await apply_open_sandbox_vault(
        handle,
        vault_write=([], []),
        session_id="session-1",
        managed_credential_names=("cred-x", "cred-y"),
        create_if_missing=False,
    )
    mutations = vault.patch.call_args.kwargs
    assert mutations["credentials"]["delete"] == [
        "astrabox-vault-cred-x",
        "astrabox-vault-cred-y",
    ]
    assert mutations["bindings"]["delete"] == [
        "astrabox-vault-cred-x",
        "astrabox-vault-cred-x-http",
        "astrabox-vault-cred-y",
    ]
