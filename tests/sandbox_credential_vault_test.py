"""The model credential never reaches the sandbox, or the request fails.

The whole value of the Credential Vault is a negative: the secret is not in the
box, so nothing running there — including whatever a page or a repository talked
the model into running — can read it. A feature whose value is a negative has one
catastrophic failure mode, and it is not "it broke". It is a deployment that
believes the credential is held outside while it is quietly still being shipped
inside.

So these pin the refusals as hard as the happy path: every partial configuration
raises, and the placeholder that goes to the box is asserted to be nothing that
could authenticate anything.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.providers.open_sandbox.credential_vault import (
    build_env_credential_vault_write,
    build_vault_write,
    open_sandbox_vault_write,
    require_vault_preconditions,
)
from astrabox.seams.egress_credentials import (
    EGRESS_HELD_PLACEHOLDER,
    EgressCredential,
    HTTPBasicEgressCredential,
    HTTPBasicEgressCredentialSet,
    SandboxEgressCredentialPlan,
    mint_placeholder,
)

_REAL = "sk-this-must-never-reach-a-sandbox"


def test_http_basic_uses_native_auth_for_only_the_repository_and_its_children() -> None:
    item = HTTPBasicEgressCredential(
        credential_id="git-1", url="https://github.com/owner/private.git",
        username="x-access-token", password=_REAL,
    )
    plan = SandboxEgressCredentialPlan(http_basic=(HTTPBasicEgressCredentialSet("scope-a", (item,)),))
    translated = open_sandbox_vault_write(plan)
    assert translated is not None
    credentials, bindings = translated
    encoded = base64.b64encode(f"x-access-token:{_REAL}".encode()).decode()
    assert credentials[0].source.value == encoded
    assert bindings[0].auth.type == "basic"
    assert bindings[0].auth.credential == credentials[0].name
    assert bindings[0].match.hosts == ["github.com"]
    assert bindings[0].match.schemes == ["https"]
    assert bindings[0].match.paths == ["/owner/private.git", "/owner/private.git/*"]
    assert _REAL not in repr(plan)
    assert encoded not in repr(bindings)


@pytest.mark.asyncio
async def test_http_basic_never_becomes_a_session_environment_variable() -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import resolve_session_environment_credentials

    item = HTTPBasicEgressCredential(
        credential_id="git-1", url="https://github.com/owner/private.git",
        username="x-access-token", password=_REAL,
    )

    class Manager:
        async def resolve_session_egress_credentials(self, session_id: str) -> list[object]:
            return [HTTPBasicEgressCredentialSet("scope-a", (item,))]

    plan, env = await resolve_session_environment_credentials(
        Manager(), session_id="session-1", vault_enabled=True, vault_write=None,
    )
    assert plan is not None and plan.http_basic[0].credentials == (item,)
    assert plan.environment == ()
    assert env == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [True, False])
async def test_http_basic_apply_removes_old_auth_without_removing_other_scopes(replacement: bool) -> None:
    from astrabox.providers.open_sandbox.executor import apply_open_sandbox_vault

    def plan(scope: str, identity: str, url: str) -> SandboxEgressCredentialPlan:
        return SandboxEgressCredentialPlan(http_basic=(HTTPBasicEgressCredentialSet(
            scope, (HTTPBasicEgressCredential(identity, url, "git", _REAL),),
        ),))

    target = "https://github.com/owner/private.git"
    old = open_sandbox_vault_write(plan("scope-a", "old", target))
    sibling = open_sandbox_vault_write(plan("scope-b", "sibling", "https://github.com/other/repo.git"))
    desired = open_sandbox_vault_write(plan("scope-a", "new", target)) if replacement else ([], [])
    assert old is not None and sibling is not None and desired is not None
    vault = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(
            revision=7, credentials=old[0] + sibling[0], bindings=old[1] + sibling[1],
        )),
        patch=AsyncMock(),
    )
    handle = SimpleNamespace(sandbox_id="box", sidecar_faces=SimpleNamespace(credential_vault=vault))
    await apply_open_sandbox_vault(
        handle, vault_write=desired, session_id="session", create_if_missing=False,
        managed_basic_scopes=("scope-a",),
    )
    mutations = vault.patch.call_args.kwargs
    assert mutations["expected_revision"] == 7
    assert mutations["credentials"]["delete"] == [old[0][0].name]
    assert mutations["bindings"]["delete"] == [old[1][0].name]
    if replacement:
        assert mutations["bindings"]["add"][0].name == desired[1][0].name

    vault.patch.reset_mock()
    await apply_open_sandbox_vault(
        handle, vault_write=([], []), session_id="unrelated-refresh", create_if_missing=False,
    )
    vault.patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_basic_refuses_another_scope_at_the_same_destination() -> None:
    from astrabox.providers.open_sandbox.executor import apply_open_sandbox_vault

    item = HTTPBasicEgressCredential("git", "https://github.com/owner/private.git", "git", _REAL)
    old = open_sandbox_vault_write(SandboxEgressCredentialPlan(
        http_basic=(HTTPBasicEgressCredentialSet("scope-a", (item,)),),
    ))
    desired = open_sandbox_vault_write(SandboxEgressCredentialPlan(
        http_basic=(HTTPBasicEgressCredentialSet("scope-b", (item,)),),
    ))
    assert old is not None and desired is not None
    vault = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(revision=1, credentials=old[0], bindings=old[1])),
        patch=AsyncMock(),
    )
    with pytest.raises(APIError) as error:
        await apply_open_sandbox_vault(
            SimpleNamespace(sandbox_id="box", sidecar_faces=SimpleNamespace(credential_vault=vault)),
            vault_write=desired, session_id="foreign", create_if_missing=False,
            managed_basic_scopes=("scope-b",),
        )
    assert error.value.code == "VAULT_CREDENTIAL_CONFLICT"
    vault.patch.assert_not_awaited()


def test_the_placeholder_is_not_a_secret() -> None:
    """It is handed to the box, so it must be inert AND recognisable.

    Inert so that leaking it costs nothing; recognisable so that a reader who
    finds it in a log or a transcript can tell at a glance that nothing leaked.
    """
    assert "astrabox" in EGRESS_HELD_PLACEHOLDER
    assert "sidecar" in EGRESS_HELD_PLACEHOLDER
    assert not EGRESS_HELD_PLACEHOLDER.startswith(("sk-", "sk_"))
    assert len(EGRESS_HELD_PLACEHOLDER) > 16


def test_the_real_value_goes_to_the_vault_and_the_binding_names_it() -> None:
    """The binding refers to the credential BY NAME — that is the mechanism.

    If a binding carried the value, the value would travel with every policy
    read and the indirection would buy nothing.
    """
    credentials, bindings = build_vault_write(
        credential=_REAL, credential_header="Authorization", base_url="https://gw.example.com/v1"
    )
    assert credentials[0].source.value == _REAL
    substitutions = bindings[0].auth.substitutions or []
    assert [entry.credential for entry in substitutions] == [credentials[0].name]
    assert _REAL not in str(bindings[0].model_dump())


def test_the_binding_is_scoped_to_the_endpoint_the_credential_belongs_to() -> None:
    """A binding matching everything would attach the secret to every request."""
    _, bindings = build_vault_write(
        credential=_REAL,
        credential_header="Authorization",
        base_url="https://gw.example.com/v1",
        request_methods=["POST"],
        request_paths=["/v1/chat/completions"],
    )
    assert bindings[0].match.hosts == ["gw.example.com"]
    assert bindings[0].match.schemes == ["https"]
    assert bindings[0].match.methods == ["POST"]
    assert bindings[0].match.paths == ["/v1/chat/completions"]


@pytest.mark.parametrize("egress_image", ["", "   "])
def test_a_missing_credential_proxy_is_refused(egress_image: str) -> None:
    """Protected delivery never falls back to shipping the real credential.

    A silent fallback would put the credential inside the sandbox while the
    operator believes vault isolation is active.
    """
    with pytest.raises(APIError) as caught:
        require_vault_preconditions(egress_image=egress_image, egress_mode="dns+nft")
    assert caught.value.code == "SANDBOX_CONFIG_INVALID"
    assert "missing:" in str(caught.value)


def test_the_flag_is_on_by_default_and_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protected delivery is the default; the operator still owns the switch."""
    from astrabox.common.utils.settings import AstraBoxRuntimeSettings

    monkeypatch.delenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", raising=False)
    assert AstraBoxRuntimeSettings().sandbox_credential_vault_enabled is True
    monkeypatch.setenv("ASTRABOX_SANDBOX_CREDENTIAL_VAULT", "false")
    assert AstraBoxRuntimeSettings().sandbox_credential_vault_enabled is False


async def test_an_environment_variable_credential_is_refused_when_vault_is_off() -> None:
    from astrabox.core.service.orchestrator.engine.provisioning import resolve_session_environment_credentials

    class _Platform:
        async def resolve_session_egress_credentials(self, session_id: str) -> list[object]:
            assert session_id == "session-1"
            return [_env_cred()]

    with pytest.raises(APIError) as caught:
        await resolve_session_environment_credentials(
            _Platform(),
            session_id="session-1",
            vault_enabled=False,
            vault_write=None,
        )
    assert caught.value.code == "SANDBOX_CREDENTIAL_VAULT_DISABLED"
    assert caught.value.status_code == 400
    assert "ASTRABOX_SANDBOX_CREDENTIAL_VAULT=1" in str(caught.value)
    assert "managed Agent or Assistant" in str(caught.value)


# --- environment_variable credentials -> the upstream vault's match language ---
#
# These pin the TRANSLATION, and they pin the refusals harder than the happy
# path for the same reason as above: a binding written LOOSER than the one its
# owner asked for still works, still passes a smoke test, and quietly sends the
# secret somewhere the allowlist never approved.


def _env_cred(
    cid: str = "cred-1",
    *,
    hosts: list[str] | None = None,
    unrestricted: bool = False,
    header: bool = True,
    body: bool = False,
    allow_insecure_http: bool = False,
    methods: list[str] | None = None,
    paths: list[str] | None = None,
) -> EgressCredential:
    return EgressCredential(
        credential_id=cid,
        secret_name="GITHUB_TOKEN",
        secret_value="ghp_real",
        placeholder=mint_placeholder(cid),
        networking=(
            {"type": "unrestricted"}
            if unrestricted
            else {"type": "limited", "allowed_hosts": hosts or ["api.github.com"]}
        ),
        injection_location={"header": header, "body": body},
        allow_insecure_http=allow_insecure_http,
        allowed_requests={
            key: value
            for key, value in (("methods", methods), ("paths", paths))
            if value is not None
        },
    )


def test_the_placeholder_and_only_the_placeholder_reaches_the_binding() -> None:
    """The box's literal is what upstream replaces; the secret rides in the credential."""
    cred = _env_cred(body=True)
    credentials, bindings = build_env_credential_vault_write([cred])

    assert [c.name for c in credentials] == ["astrabox-vault-cred-1"]
    assert credentials[0].source.value == "ghp_real"

    assert len(bindings) == 1
    binding = bindings[0]
    assert binding.match.hosts == ["api.github.com"]
    assert binding.match.schemes == ["https"]
    # passthrough: no typed header is attached, only the replacement rule.
    assert binding.auth.type == "passthrough"
    substitution = binding.auth.substitutions[0]
    assert substitution.placeholder == cred.placeholder
    assert substitution.credential == "astrabox-vault-cred-1"
    assert sorted(substitution.in_) == ["body", "header"]


def test_only_the_enabled_surfaces_are_replaceable() -> None:
    """A credential that disabled body injection must not be replaced in a body."""
    _credentials, bindings = build_env_credential_vault_write([_env_cred(header=True, body=False)])
    assert bindings[0].auth.substitutions[0].in_ == ["header"]


def test_optional_request_limits_reach_the_upstream_binding() -> None:
    """The managed rule must not become broader while translated to OpenSandbox."""
    _credentials, bindings = build_env_credential_vault_write(
        [_env_cred(methods=["GET", "POST"], paths=["/repos/acme/private/*"])]
    )
    assert len(bindings) == 1
    assert bindings[0].match.methods == ["GET", "POST"]
    assert bindings[0].match.paths == ["/repos/acme/private/*"]


def test_unrestricted_networking_is_refused_not_widened() -> None:
    """A credential that may go anywhere cannot be bound to somewhere."""
    with pytest.raises(APIError) as caught:
        build_env_credential_vault_write([_env_cred(unrestricted=True)])
    assert "unrestricted" in str(caught.value)


def test_cleartext_needs_the_credentials_own_opt_in() -> None:
    with pytest.raises(APIError) as caught:
        build_env_credential_vault_write([_env_cred(hosts=["internal.svc:80"])])
    assert "allow_insecure_http" in str(caught.value)


async def test_a_gateway_server_with_no_url_gets_no_guessed_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back to a glob here is how the unmatched binding got installed."""

    from astrabox.core.service.orchestrator.runtime import mcp_credentials
    from astrabox.seams.extensions import MCPGatewayCredential

    class _Provider:
        def mcp_gateway_credential(self) -> MCPGatewayCredential:
            return MCPGatewayCredential(
                header="Authorization",
                value="gateway-key",
                base_url="https://gateway.test",
            )

    monkeypatch.setattr(mcp_credentials, "extension_provider_for_name", lambda _n: _Provider())
    template = SimpleNamespace(
        credential_vault_ids=[],
        mcp_servers={
            "bright_data": {"provider": "litellm", "type": "streamable_http", "url": ""}
        },
    )

    plan = await mcp_credentials.resolve_agent_mcp_credential_plan(template, vault_enabled=True)
    assert plan is None
