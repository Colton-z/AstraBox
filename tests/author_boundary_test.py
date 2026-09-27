"""An Agent's or Assistant's own fields reach only what an administrator made available.

Anyone who may create an Agent writes the fields that decide what its sandbox
clones and connects to. Those fields once widened a limited Environment to the
Docker bridge gateway (``https://172.17.0.1/x.git`` reached the platform API),
copied an arbitrary server environment variable into the sandbox as a deploy
key, and could select the platform's gateway credential for an MCP server. The
cases here fix the rule at both places it applies: where an author saves a
definition (a 4xx, nothing stored) and where the runtime acts on a stored one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError, error_spec
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator import author_boundary
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
from astrabox.core.service.orchestrator.assistant.assistant_service import AssistantService
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    resolve_network_policy,
)
from astrabox.core.service.orchestrator.runtime.storage import _default_repo, _git_clone

_LIMITED = {"type": "limited", "allowed_hosts": [], "allow_mcp_servers": True}
_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


class _AgentRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.agent: dict[str, Any] | None = None

    async def create_agent(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.created.append(dict(doc))
        self.agent = dict(doc)
        return dict(doc)

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if self.agent else None

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        assert self.agent is not None
        self.agent.update(updates)
        return True


class _EnvironmentRepo:
    def __init__(self, networking: dict[str, dict[str, Any] | None]) -> None:
        self._networking = networking

    async def get_any_by_name(self, name: str) -> dict[str, Any] | None:
        if name not in self._networking:
            return None
        return {
            "name": name,
            "engine_kind": "claude_code",
            "networking": self._networking[name],
        }


def _agent_service(**networking: dict[str, Any] | None) -> tuple[AgentConfigService, _AgentRepo]:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    repo = _AgentRepo()
    environments = networking or {"limited": _LIMITED}
    return (
        AgentConfigService(repo, environment_repo=_EnvironmentRepo(environments)),  # type: ignore[arg-type]
        repo,
    )


def _agent(**fields: Any) -> dict[str, Any]:
    return {"name": "probe", "model": "some-model", "environment_name": "limited", **fields}


def _user() -> Any:
    return SimpleNamespace(user_id="author-1", org_id="default", roles=[])


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """What each host name resolves to; an absent name does not resolve."""
    table: dict[str, list[str]] = {}

    async def _resolve(host: str) -> list[str]:
        return list(table.get(host, []))

    monkeypatch.setattr(author_boundary, "resolve_host_addresses", _resolve)
    return table


async def _refused(call: Any) -> APIError:
    with pytest.raises(APIError) as raised:
        await call
    return raised.value


# ── egress: an author's hosts on a limited Environment ─────────────────────


@pytest.mark.parametrize(
    "fields",
    [
        {"plugin_repos": [{"url": "https://172.17.0.1/x.git", "protocol": "https"}]},
        {"skills": ["https://169.254.169.254/latest.git#skill"]},
        {"mcp_servers": {"probe": {"type": "http", "url": "http://127.0.0.1.nip.io/mcp"}}},
        {"plugin_repos": [{"url": "git@10.0.0.5:org/plugins.git", "deploy_key_secret_name": ""}]},
        {"mcp_servers": {"probe": {"type": "http", "url": "http://[::ffff:172.17.0.1]/mcp"}}},
    ],
)
async def test_an_author_cannot_open_the_deployments_own_network(
    dns: dict[str, list[str]], fields: dict[str, Any]
) -> None:
    dns["127.0.0.1.nip.io"] = ["127.0.0.1"]
    service, repo = _agent_service()

    error = await _refused(service.create_agent_config(_user(), _agent(**fields)))

    assert (error.code, error.status_code) == ("AGENT_EGRESS_HOST_REFUSED", 403)
    assert repo.created == []


async def test_a_name_that_resolves_to_a_private_address_is_refused(
    dns: dict[str, list[str]],
) -> None:
    dns["git.attacker.example"] = ["93.184.216.34", "172.17.0.1"]
    service, repo = _agent_service()

    error = await _refused(
        service.create_agent_config(
            _user(),
            _agent(plugin_repos=[{"url": "https://git.attacker.example/x.git", "protocol": "https"}]),
        )
    )

    assert error.code == "AGENT_EGRESS_HOST_REFUSED"
    assert "172.17.0.1" in error.message
    assert repo.created == []


async def test_a_name_that_does_not_resolve_cannot_be_shown_public(
    dns: dict[str, list[str]],
) -> None:
    service, repo = _agent_service()

    error = await _refused(
        service.create_agent_config(
            _user(), _agent(skills=["https://git.internal.corp/skills.git#review"])
        )
    )

    assert error.code == "AGENT_EGRESS_HOST_REFUSED"
    assert repo.created == []


async def test_a_public_git_host_is_still_an_authors_to_choose(
    dns: dict[str, list[str]],
) -> None:
    dns["github.com"] = ["140.82.112.3"]
    service, repo = _agent_service()

    await service.create_agent_config(
        _user(),
        _agent(
            plugin_repos=[{"url": "https://github.com/anthropics/plugins.git", "protocol": "https"}],
            skills=["https://github.com/anthropics/skills.git@main#skills/review"],
        ),
    )

    assert len(repo.created) == 1


async def test_what_the_environment_names_is_available_to_its_agents(
    dns: dict[str, list[str]],
) -> None:
    dns["git.corp.example"] = ["10.20.0.7"]
    service, repo = _agent_service(
        limited={
            "type": "limited",
            "allowed_hosts": ["git.corp.example", "10.0.0.0/8"],
            "allow_mcp_servers": True,
        }
    )

    await service.create_agent_config(
        _user(),
        _agent(
            plugin_repos=[{"url": "https://git.corp.example/p.git", "protocol": "https"}],
            skills=["https://10.9.8.7/skills.git#review"],
        ),
    )

    assert len(repo.created) == 1


async def test_an_unrestricted_environment_is_not_widened_by_an_author(
    dns: dict[str, list[str]],
) -> None:
    service, repo = _agent_service(limited={"type": "unrestricted"})

    await service.create_agent_config(
        _user(), _agent(plugin_repos=[{"url": "https://10.0.0.5/x.git", "protocol": "https"}])
    )

    assert len(repo.created) == 1


async def test_moving_an_agent_to_a_limited_environment_rechecks_its_hosts(
    dns: dict[str, list[str]],
) -> None:
    service, repo = _agent_service(open={"type": "unrestricted"}, limited=_LIMITED)
    created = await service.create_agent_config(
        _user(),
        _agent(
            environment_name="open",
            plugin_repos=[{"url": "https://10.0.0.5/x.git", "protocol": "https"}],
        ),
    )

    error = await _refused(
        service.upsert_agent_config(
            _user(),
            created["agent_id"],
            {"model": "some-model", "environment_name": "limited"},
        )
    )

    assert error.code == "AGENT_EGRESS_HOST_REFUSED"
    assert repo.agent is not None and repo.agent["environment_name"] == "open"


def test_a_stored_definition_cannot_widen_the_policy_at_startup() -> None:
    """The runtime refuses what the save would have, for a stored Agent."""
    view = AgentView(
        name="stored",
        networking=_LIMITED,
        plugin_repos=[{"url": "https://172.17.0.1/x.git", "protocol": "https"}],
    )

    with pytest.raises(APIError) as raised:
        resolve_network_policy(view)

    assert raised.value.code == "AGENT_EGRESS_HOST_REFUSED"


def test_a_catalog_skill_keeps_the_host_an_administrator_published(monkeypatch) -> None:
    monkeypatch.setattr(
        "astrabox.core.service.orchestrator.runtime.config_resolver."
        "platform_callback_egress_targets",
        lambda: [],
    )
    skill = "https://10.1.2.3/skills.git#review"

    authored = AgentView(name="author", networking=_LIMITED, skills=[skill])
    with pytest.raises(APIError):
        resolve_network_policy(authored)

    published = AgentView(
        name="catalog", networking=_LIMITED, skills=[skill], catalog_skills=(skill,)
    )
    assert "10.1.2.3" in resolve_network_policy(published).allowed_hosts


# ── deploy keys: only names an administrator listed ────────────────────────


async def test_a_deploy_key_name_cannot_address_a_server_secret(
    dns: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "team-repo-deploy-key")
    service, repo = _agent_service()

    error = await _refused(
        service.create_agent_config(
            _user(),
            _agent(
                environment_name="limited",
                default_repo={
                    "url": "git@github.com:org/repo.git",
                    "protocol": "ssh",
                    "deploy_key_secret_name": "litellm-master-key",
                },
            ),
        )
    )

    assert (error.code, error.status_code) == ("AGENT_DEPLOY_KEY_NOT_ALLOWED", 403)
    assert repo.created == []


async def test_a_listed_deploy_key_is_available_under_any_spelling(
    dns: dict[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "team-repo-deploy-key")
    service, repo = _agent_service()

    await service.create_agent_config(
        _user(),
        _agent(
            default_repo={
                "url": "git@github.com:org/repo.git",
                "protocol": "ssh",
                "deploy_key_secret_name": "TEAM_REPO_DEPLOY_KEY",
            }
        ),
    )

    assert len(repo.created) == 1


def test_a_stored_unlisted_deploy_key_is_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECPROBE_MARKER", "SECPROBE-MARKER-9f3a1c7e")
    monkeypatch.delenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", raising=False)
    template = SimpleNamespace(
        default_repo={
            "url": "git@10.255.255.1:x/y.git",
            "protocol": "ssh",
            "deploy_key_secret_name": "secprobe-marker",
        }
    )

    with pytest.raises(APIError) as raised:
        _default_repo._build_default_repo_bootstrap_payload(
            template, target_cwd="/workspace", session_id="s-1"
        )

    assert raised.value.code == "AGENT_DEPLOY_KEY_NOT_ALLOWED"
    assert "SECPROBE-MARKER" not in raised.value.message


def test_a_listed_name_that_holds_no_private_key_is_not_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "misfiled-key")
    monkeypatch.setenv("MISFILED_KEY", "sk-not-a-private-key")
    template = SimpleNamespace(
        default_repo={
            "url": "git@github.com:org/repo.git",
            "protocol": "ssh",
            "deploy_key_secret_name": "misfiled-key",
        }
    )

    with pytest.raises(APIError) as raised:
        _default_repo._build_default_repo_bootstrap_payload(
            template, target_cwd="/workspace", session_id="s-1"
        )

    assert raised.value.code == "DEFAULT_REPO_INVALID_KEY"


def test_a_listed_deploy_key_reaches_the_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    monkeypatch.setenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "team-repo-deploy-key")
    monkeypatch.setenv("TEAM_REPO_DEPLOY_KEY", _KEY)
    template = SimpleNamespace(
        default_repo={
            "url": "git@github.com:org/repo.git",
            "protocol": "ssh",
            "deploy_key_secret_name": "team-repo-deploy-key",
        }
    )

    payload = _default_repo._build_default_repo_bootstrap_payload(
        template, target_cwd="/workspace", session_id="s-1"
    )

    assert payload is not None
    assert base64.b64decode(payload["key_b64"]).decode() == _KEY


class _Commands:
    def __init__(self, results: list[Any]) -> None:
        self.calls: list[str] = []
        self._results = list(results)

    async def run(self, command: str) -> Any:
        self.calls.append(command)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(error=None, exit_code=0, stdout="")


async def test_a_failed_clone_takes_its_deploy_key_back_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_DEPLOY_KEY_SECRET_NAMES", "team-repo-deploy-key")
    monkeypatch.setenv("TEAM_REPO_DEPLOY_KEY", _KEY)
    monkeypatch.setattr(_git_clone, "_underlying_requires_https_git", lambda _sandbox: False)
    commands = _Commands(
        [
            SimpleNamespace(error=None, exit_code=0, stdout=""),
            SimpleNamespace(error="exit 128", exit_code=128, stdout=""),
        ]
    )

    with pytest.raises(APIError):
        await _git_clone._clone_git_repo_in_sandbox(
            SimpleNamespace(commands=commands),
            ssh_url="git@10.255.255.1:x/y.git",
            target="/workspace",
            branch="",
            depth=None,
            deploy_key_secret_name="team-repo-deploy-key",
            identity=None,
            error_code="DEFAULT_REPO_CLONE_FAILED",
            label="default_repo",
        )

    assert commands.calls[-1] == "rm -f -- /root/.ssh/id_ed25519"


# ── the Git HTTPS token goes only to its own host ──────────────────────────


async def test_a_declared_https_repository_never_carries_the_deployment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME", "git-https-token")
    monkeypatch.setenv("ASTRABOX_GIT_HTTPS_TOKEN_HOST", "github.com")
    monkeypatch.setenv("GIT_HTTPS_TOKEN", "ghp_deployment_token")
    monkeypatch.setattr(_git_clone, "_underlying_requires_https_git", lambda _sandbox: False)
    commands = _Commands([])

    await _git_clone._clone_git_repo_in_sandbox(
        SimpleNamespace(commands=commands),
        ssh_url="https://github.com/attacker/plugins.git",
        protocol="https",
        target="/opt/plugins/p",
        branch="",
        depth=None,
        deploy_key_secret_name=None,
        identity=None,
        error_code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
        label="plugin_repos[0]",
    )

    assert not any("ghp_deployment_token" in call for call in commands.calls)


def test_the_translation_token_is_bound_to_its_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME", "git-https-token")
    monkeypatch.setenv("ASTRABOX_GIT_HTTPS_TOKEN_HOST", "github.com")
    monkeypatch.setenv("GIT_HTTPS_TOKEN", "ghp_deployment_token")

    assert _git_clone._resolve_git_https_token(host="GitHub.com") == "ghp_deployment_token"
    with pytest.raises(APIError) as raised:
        _git_clone._resolve_git_https_token(host="git.attacker.example")
    assert raised.value.code == "REPO_MISSING_TOKEN"


@pytest.mark.parametrize(
    ("settings", "requires_https_git"),
    [
        ({"ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME": "git-https-token"}, True),
        ({"ASTRABOX_GIT_HTTPS_TOKEN_HOST": "github.com"}, True),
        (
            {
                "ASTRABOX_GIT_HTTPS_TOKEN_SECRET_NAME": "git-https-token",
                "ASTRABOX_GIT_HTTPS_TOKEN_HOST": "github.com",
            },
            False,
        ),
    ],
)
def test_a_git_https_token_that_would_be_unbound_or_unread_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, str], requires_https_git: bool
) -> None:
    from astrabox import bootstrap

    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        "astrabox.seams.sandbox.sandbox_for_name",
        lambda _name: SimpleNamespace(name="probe", requires_https_git=requires_https_git),
    )

    with pytest.raises(bootstrap.BootstrapConfigError):
        bootstrap._assert_git_https_token_is_honored("probe")


# ── MCP: the platform's credential fields are catalog-only ─────────────────


@pytest.mark.parametrize(
    "server",
    [
        {"type": "http", "url": "http://gateway.astrabox.test:4000/x/mcp", "provider": "litellm"},
        {
            "type": "http",
            "url": "https://collector.attacker.example/mcp",
            "credential_target_url": "https://mcp.linear.app/mcp",
        },
    ],
)
async def test_an_author_cannot_choose_the_credential_the_sidecar_attaches(
    dns: dict[str, list[str]], server: dict[str, Any]
) -> None:
    service, repo = _agent_service()

    error = await _refused(
        service.create_agent_config(_user(), _agent(mcp_servers={"probe": server}))
    )

    assert (error.code, error.status_code) == ("AGENT_MCP_FIELD_RESERVED", 403)
    assert repo.created == []


async def test_a_stored_author_server_cannot_select_the_gateway_credential() -> None:
    """A definition saved before the check is refused when the Agent resolves."""
    service, repo = _agent_service()
    repo.agent = {
        "agent_id": "a-1",
        **_agent(
            mcp_servers={
                "probe": {
                    "type": "http",
                    "url": "http://gateway.astrabox.test:4000/x/mcp",
                    "provider": "litellm",
                }
            }
        ),
    }

    with pytest.raises(APIError) as raised:
        await service.resolve_agent_harness("a-1")

    assert raised.value.code == "AGENT_MCP_FIELD_RESERVED"


async def test_a_catalog_assignment_keeps_its_credential_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.core.service.orchestrator import agent_config_service
    from astrabox.seams.extensions import RuntimeMCPServer

    class _Catalog:
        async def resolve_mcp_servers(self, *, org_id: str, item_ids: list[str]) -> Any:
            return [
                RuntimeMCPServer(
                    name="catalog",
                    transport="streamable_http",
                    url="http://gateway.astrabox.test:4000/catalog/mcp",
                    credential_target_url="https://mcp.upstream.example/mcp",
                )
            ]

    monkeypatch.setattr(
        agent_config_service, "extension_provider_for_name", lambda _name: _Catalog()
    )
    service, repo = _agent_service()
    repo.agent = {
        "agent_id": "a-1",
        **_agent(mcp_servers={"own": {"type": "http", "url": "https://mcp.example/mcp"}}),
        "mcp_assignments": [{"provider": "litellm", "item_id": "catalog"}],
    }

    view = await service.resolve_agent_harness("a-1")

    assert view is not None and view.mcp_servers is not None
    assert view.mcp_servers["catalog"]["provider"] == "litellm"
    assert view.mcp_servers["catalog"]["credential_target_url"] == (
        "https://mcp.upstream.example/mcp"
    )


# ── Assistants take the same fields and meet the same rule ─────────────────


class _Environments:
    def __init__(self, networking: dict[str, Any]) -> None:
        self._networking = networking

    async def get_environment(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "enabled": True,
            "engine_kind": "assistant",
            "networking": self._networking,
        }


class _Catalog:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_assistant(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.created.append(dict(payload))
        return dict(payload)


@pytest.mark.parametrize(
    ("server", "code"),
    [
        ({"type": "http", "url": "http://169.254.169.254/mcp"}, "AGENT_EGRESS_HOST_REFUSED"),
        (
            {"type": "http", "url": "https://mcp.example/mcp", "provider": "litellm"},
            "AGENT_MCP_FIELD_RESERVED",
        ),
    ],
)
async def test_an_assistant_meets_the_same_rule(
    dns: dict[str, list[str]], server: dict[str, Any], code: str
) -> None:
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    catalog = _Catalog()
    service = AssistantService(
        agent_config=_Environments(_LIMITED),
        session_kernel=None,
        catalog_repo=catalog,  # type: ignore[arg-type]
        workspace_service=SimpleNamespace(),  # type: ignore[arg-type]
        spawn_background_task=lambda coroutine, **_: coroutine.close(),
    )

    error = await _refused(
        service.create_assistant(
            _user(),
            {
                "display_name": "Jarvis",
                "environment_name": "env-1",
                "mcp_config_override": {"probe": server},
            },
        )
    )

    assert error.code == code
    assert catalog.created == []


def test_every_refusal_is_a_registered_4xx() -> None:
    for code in (
        "AGENT_EGRESS_HOST_REFUSED",
        "AGENT_MCP_FIELD_RESERVED",
        "AGENT_DEPLOY_KEY_NOT_ALLOWED",
    ):
        spec = error_spec(code)
        assert spec.category != "unregistered"
        assert spec.status_code == 403
        assert spec.owner == "template"
