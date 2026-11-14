"""What a RuntimeProfile may join and mount: the operator's allowlists and the box's own paths.

The same resolvers decide at save (``runtime_profile_schema``) and for every box
created (``engine_create_spec``, the Agent client pool), so a value the operator
later removes from an allowlist is refused at the next create.
"""

from __future__ import annotations

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_sandbox_mounts,
    resolve_sandbox_networks,
)
from astrabox.core.service.orchestrator.runtime_profile_schema import (
    normalize_runtime_profile_payload,
)
from astrabox.seams.sandbox import SandboxSharedMount


def test_a_runtime_profile_joins_only_networks_the_operator_still_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_ATTACHABLE_NETWORKS", "foundry-shared, git")
    assert resolve_sandbox_networks({"sandbox_networks": ["git", "foundry-shared"]}) == (
        "git",
        "foundry-shared",
    )
    assert resolve_sandbox_networks({}) == ()
    with pytest.raises(APIError) as refused:
        resolve_sandbox_networks({"sandbox_networks": ["astrabox_database"]})
    assert refused.value.code == "SANDBOX_NETWORK_NOT_ALLOWED"

    monkeypatch.setenv("ASTRABOX_SANDBOX_ATTACHABLE_NETWORKS", "foundry-shared")
    with pytest.raises(APIError) as shrunk:
        resolve_sandbox_networks({"sandbox_networks": ["git"]})
    assert shrunk.value.code == "SANDBOX_NETWORK_NOT_ALLOWED"


def test_mount_sources_are_named_exactly_and_read_only_unless_stated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_MOUNTABLE_HOST_PATHS", "/srv/git-objects")
    monkeypatch.setenv("ASTRABOX_SANDBOX_MOUNTABLE_VOLUMES", "npm-cache")
    saved = normalize_runtime_profile_payload(
        {
            "idle_action": "terminate",
            "sandbox_mounts": [
                {"source": "/srv/git-objects", "target": "/mnt/git-objects"},
                {"source": "npm-cache", "target": "/mnt/npm", "read_only": False},
            ],
        }
    )
    assert saved["sandbox_mounts"][0]["read_only"] is True
    assert resolve_sandbox_mounts(saved) == (
        SandboxSharedMount(source="/srv/git-objects", target="/mnt/git-objects", read_only=True),
        SandboxSharedMount(source="npm-cache", target="/mnt/npm", read_only=False),
    )
    for source in ("/srv/git-objects/objects", "/srv", "pip-cache"):
        with pytest.raises(APIError) as refused:
            resolve_sandbox_mounts({"sandbox_mounts": [{"source": source, "target": "/mnt/x"}]})
        assert refused.value.code == "SANDBOX_MOUNT_NOT_ALLOWED", source


@pytest.mark.parametrize(
    ("target", "accepted"),
    [
        ("/mnt/git-objects", True),
        ("/srv/cache/npm", True),
        ("/opt/foundry", True),
        ("/var/cache/foundry", True),
        ("/workspace", False),
        ("/workspace/cache", False),
        ("/home/agent/.m2", False),
        ("/tmp/cache", False),
        ("/opt", False),
        ("/var", False),
        ("/opt/opensandbox", False),
        ("/var/lib/astrabox/container-engine", False),
        ("/usr/local/cache", False),
        ("/", False),
        ("mnt/cache", False),
        ("/mnt/../etc", False),
        ("/mnt/cache/", False),
    ],
)
def test_a_mount_target_stays_clear_of_what_the_box_runs_from(
    monkeypatch: pytest.MonkeyPatch, target: str, accepted: bool
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_MOUNTABLE_VOLUMES", "npm-cache")
    profile = {"sandbox_mounts": [{"source": "npm-cache", "target": target}]}
    if accepted:
        assert resolve_sandbox_mounts(profile)[0].target == target
        return
    with pytest.raises(APIError) as refused:
        resolve_sandbox_mounts(profile)
    assert refused.value.code == "SANDBOX_MOUNT_NOT_ALLOWED"


def test_a_target_is_mounted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_MOUNTABLE_VOLUMES", "npm-cache,pip-cache")
    with pytest.raises(APIError) as refused:
        resolve_sandbox_mounts(
            {
                "sandbox_mounts": [
                    {"source": "npm-cache", "target": "/mnt/cache"},
                    {"source": "pip-cache", "target": "/mnt/cache"},
                ]
            }
        )
    assert refused.value.code == "SANDBOX_MOUNT_NOT_ALLOWED"


@pytest.mark.usefixtures("database")
async def test_saving_checks_the_allowlists_and_keeps_what_was_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 - registers the engine
    import astrabox.providers.open_sandbox.sandbox  # noqa: F401 - registers the backend
    from astrabox.common.utils.user_context import UserContext
    from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService
    from astrabox.persistence.repository.runtime_profile_repository import (
        RuntimeProfileRepository,
    )

    monkeypatch.delenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", raising=False)
    monkeypatch.setenv("ASTRABOX_SANDBOX_SERVER_RUNTIME", "docker")
    monkeypatch.setenv("ASTRABOX_SANDBOX_ATTACHABLE_NETWORKS", "foundry-shared")
    monkeypatch.setenv("ASTRABOX_SANDBOX_MOUNTABLE_HOST_PATHS", "/srv/git-objects")
    daemon = {"ApiVersion": "1.52", "Version": "29.1.3"}
    monkeypatch.setattr("astrabox.deploy.sandbox_server.docker_engine_version", lambda: daemon)
    svc = AgentConfigService(
        agent_repo=object(),  # type: ignore[arg-type]  # unused on the RuntimeProfile path
        runtime_profile_repo=RuntimeProfileRepository(),
        assistant_repo=object(),  # type: ignore[arg-type]  # unused
    )
    admin = UserContext(user_id="admin", roles=["admin"])
    profile = {
        "engine_kind": "claude_code",
        "sandbox_networks": ["foundry-shared"],
        "sandbox_mounts": [{"source": "/srv/git-objects", "target": "/mnt/git-objects"}],
    }
    await svc.upsert_runtime_profile_config(admin, "foundry", profile)
    stored = await svc.get_runtime_profile("foundry")
    assert stored is not None
    assert stored["sandbox_networks"] == ["foundry-shared"]
    assert stored["sandbox_mounts"] == [
        {"source": "/srv/git-objects", "target": "/mnt/git-objects", "read_only": True}
    ]

    for change, code in (
        ({"sandbox_networks": ["astrabox_database"]}, "SANDBOX_NETWORK_NOT_ALLOWED"),
        (
            {"sandbox_mounts": [{"source": "/srv/git-objects", "target": "/workspace/git"}]},
            "SANDBOX_MOUNT_NOT_ALLOWED",
        ),
    ):
        with pytest.raises(APIError) as refused:
            await svc.upsert_runtime_profile_config(admin, "foundry", {**profile, **change})
        assert refused.value.code == code

    # Docker 26 and 27 cannot keep a joined network off the sandbox's gateway.
    daemon.update(ApiVersion="1.47", Version="27.5.1")
    with pytest.raises(APIError) as too_old:
        await svc.upsert_runtime_profile_config(admin, "foundry", profile)
    assert too_old.value.code == "SANDBOX_NETWORK_NOT_ALLOWED"
    assert "Docker Engine 28.0 (API 1.48)" in too_old.value.message
    await svc.upsert_runtime_profile_config(admin, "foundry", {**profile, "sandbox_networks": []})
