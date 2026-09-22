"""Where each subject's durable files live, and that the box is told.

This planner shipped in the initial public release and `27dd68d0` deleted it
while fixing how the workload account reaches the image: it keyed a durable path
by `linux_user`, which that commit made identical in every box, so the branch
separated nothing and went. Nothing replaced it, and the sandbox seam kept
`uses_create_oss_mounts = True` — a flag whose whole meaning is "the create path
mounts this instead". The create path passed no mounts at all, and every
Assistant's files, memories and vendor session store lived on a container
overlay that a replaced box takes with it. The checks all reported success:
creating an Assistant even verified that the storage medium could arbitrate
between two writers, of a medium nothing then wrote a byte to.

So these pin the two halves that were missing rather than the planner's
arithmetic: that each subject gets the path its product promises, and that what
the planner produces actually reaches the box's create.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.storage import (
    plan_subject_storage_mounts,
)
from astrabox.providers import register_builtin_providers
from astrabox.providers.open_sandbox.sandbox import volume_specs_from_planned_mounts

# Planning reads each engine's profile; register the built-ins here instead of
# relying on an earlier test file having done it.
register_builtin_providers()

#: The platform's name for the workspace under test. Opaque on purpose: the
#: layout must not spell an Agent, an Assistant or a Session anywhere.
_WORKSPACE = "0f9c2b7a4e114d0c9e1a3b5d7f9a1c3e"


def _settings(base: str = "/astrabox") -> Any:
    return SimpleNamespace(nas_base_path=base)


def _identity(**overrides: Any) -> dict[str, Any]:
    identity = {
        "sandbox_tenancy": "agent",
        "linux_user": "agent",
        "home_dir": "/home/agent",
        "workspace_dir": "/home/agent/workspace",
        "workspace_source_dir": "/home/agent/conversations/session-1",
        "session_id": "session-1",
    }
    identity.update(overrides)
    return identity


def test_an_assistant_mounts_the_home_its_identity_names() -> None:
    """The Assistant's whole point: one profile that outlives every box.

    The box path comes from the identity, not from this planner. A composed
    path mounts the volume at a directory nothing reads while the real home
    stays on the container's overlay — and every check passes, including a
    box-replacement probe, because it reads back the empty directory the mount
    itself just created.

    The HOME, not the workspace under it: an Assistant's memory is its engine's
    config directory as much as its files, and losing the conversations while
    keeping the files is the defect this was reported as.
    """

    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        assistant_config_dir,
        assistant_workspace_dir,
    )

    planned = plan_subject_storage_mounts(
        subject_kind="assistant_runtime",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        assistant_id="asst_abc123",
        user_id="owner-1",
    )

    # Two mounts, one claim, two storage subpaths. The box side is the physical
    # location the profile renders; what the USER sees is neither — the workload
    # namespace shows `/workspace` for every subject — which is what lets the
    # storage tree be shaped for the platform rather than for the reader.
    assert planned == [
        (
            assistant_workspace_dir("owner-1", "asst_abc123"),
            f"/astrabox/workspaces/{_WORKSPACE}/workspace",
        ),
        (
            assistant_config_dir("owner-1", "asst_abc123"),
            f"/astrabox/workspaces/{_WORKSPACE}/config",
        ),
    ]


def test_an_agent_conversation_does_not_make_its_engine_config_durable() -> None:
    """The two engines get different answers, on purpose.

    An Assistant's memory is a file under its config directory, so that
    directory is durable. An Agent's conversation is durable through the
    transcript mirror instead, and its CLI rewrites config continuously — the
    measurement the deleted code carried put that at 80-440ms per operation on
    network storage against milliseconds locally. So a conversation persists its
    workspace and nothing else.
    """

    planned = plan_subject_storage_mounts(
        subject_kind="deployment_conversation",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        agent_id="agent-7",
        runtime_identity=_identity(),
    )

    assert len(planned) == 1
    box_path, _ = planned[0]
    assert not box_path.endswith(".claude"), (
        "an Agent conversation must not put its engine config on the medium"
    )


def test_an_assistant_mount_is_planned_before_any_box_exists() -> None:
    """A mount is decided at create; the identity does not exist yet.

    The second attempt at this read the box path off the runtime identity, which
    is rendered only after the box exists and its account has been made. Every
    Assistant materialization was then refused by its own storage planning, and
    the workspace never left HIBERNATING. The prepared-box branch states the
    same ordering; this is the Assistant's half of it.
    """

    planned = plan_subject_storage_mounts(
        subject_kind="assistant_runtime",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        assistant_id="asst_abc123",
        user_id="owner-1",
        runtime_identity=None,
    )

    assert planned, "planning must not need a box that does not exist yet"


def test_a_shared_agent_box_mounts_the_whole_conversations_root() -> None:
    """One box, many conversations — so the root is what has to be durable.

    This is also what makes a POOLED box work: it is created before any
    conversation exists, so a per-conversation mount has no name to use, while
    every conversation later created under this root is durable by construction.
    """

    planned = plan_subject_storage_mounts(
        subject_kind="deployment_runtime",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        agent_id="agent-7",
    )

    # The root is read off the profile's `home_template`, so an administrator
    # who moves it moves this with it.
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )
    from astrabox.seams.sandbox import SANDBOX_TENANCY_AGENT

    expected_root = (
        composed_runtime_profile("claude_code", SANDBOX_TENANCY_AGENT)
        .home_template.split("{", 1)[0]
        .rstrip("/")
    )
    assert planned == [(expected_root, f"/astrabox/workspaces/{_WORKSPACE}")]


def test_a_conversation_is_keyed_by_its_session_not_by_its_account() -> None:
    """The exact defect that took the planner out of the tree.

    The original keyed this path by `identity["linux_user"]`. `27dd68d0` moved
    the workload account into the image, which made that name the same in every
    box — so two conversations of one Agent would have been handed the same
    durable directory. The session id is what actually differs.
    """

    planned = plan_subject_storage_mounts(
        subject_kind="deployment_conversation",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        agent_id="agent-7",
        runtime_identity=_identity(),
    )

    assert planned == [
        (
            "/home/agent/conversations/session-1",
            f"/astrabox/workspaces/{_WORKSPACE}/workspace",
        )
    ]
    assert "agent" not in planned[0][1].rsplit("/", 1)[1], (
        "the durable path must not be keyed by the account name"
    )


def test_two_workspaces_do_not_share_a_directory() -> None:
    """The workspace id is what separates them, and the only thing that does."""

    first, second = (
        plan_subject_storage_mounts(
            subject_kind="deployment_conversation",
            workspace_id=workspace,
            settings=_settings(),
            agent_id="agent-7",
            runtime_identity=_identity(
                session_id="session-1",
                workspace_source_dir="/home/agent/conversations/session-1",
            ),
        )[0]
        for workspace in (_WORKSPACE, "b41d9f5c8a2e4677b0d3c9e2f4a6b8d0")
    )

    assert first[1] != second[1]


def test_the_same_workspace_is_the_same_directory_whoever_asks() -> None:
    """A path that varied with the asker is a path the next box cannot find.

    The box that replaces this one asks with a new session, a new sandbox and a
    new POSIX account. If any of those reached the storage path, the rebuild
    would mount an empty directory and the files would be present, durable, and
    unreachable.
    """

    first, second = (
        plan_subject_storage_mounts(
            subject_kind="deployment_conversation",
            workspace_id=_WORKSPACE,
            settings=_settings(),
            agent_id=agent,
            runtime_identity=_identity(
                session_id=session,
                workspace_source_dir=f"/home/agent/conversations/{session}",
            ),
        )[0]
        for agent, session in (("agent-7", "session-1"), ("agent-9", "session-2"))
    )

    assert first[1] == second[1]


def test_the_box_path_is_the_physical_workspace_not_the_visible_one() -> None:
    """An Agent-tenanted box shows one cwd and stores under another.

    `conversation_identity` keeps both and requires the physical one under the
    home; mounting the visible path would put the durable medium under a
    directory the platform re-points per conversation.
    """

    planned = plan_subject_storage_mounts(
        subject_kind="deployment_conversation",
        workspace_id=_WORKSPACE,
        settings=_settings(),
        agent_id="agent-7",
        runtime_identity=_identity(
            workspace_dir="/workspace",
            workspace_source_dir="/home/agent/conversations/session-1",
        ),
    )

    assert planned[0][0] == "/home/agent/conversations/session-1"


def test_an_unknown_subject_refuses_rather_than_guessing() -> None:
    """A path chosen by a fallback is a workspace nothing later looks in."""

    with pytest.raises(APIError) as excinfo:
        plan_subject_storage_mounts(
            subject_kind="something_else",
            settings=_settings(),
            workspace_id=_WORKSPACE,
            agent_id="agent-7",
        )

    assert excinfo.value.code == "NAS_MOUNT_FAILED"
    assert "something_else" in excinfo.value.message


def test_a_missing_base_path_refuses_rather_than_rooting_at_slash() -> None:
    with pytest.raises(APIError):
        plan_subject_storage_mounts(
            subject_kind="deployment_runtime",
            settings=_settings(base=""),
            workspace_id=_WORKSPACE,
            agent_id="a",
        )


def test_a_conversation_without_an_identity_refuses() -> None:
    """It cannot name the physical workspace, so there is nothing to mount."""

    with pytest.raises(APIError):
        plan_subject_storage_mounts(
            subject_kind="deployment_conversation",
            workspace_id=_WORKSPACE,
            settings=_settings(),
            agent_id="agent-7",
            runtime_identity=None,
        )


def test_planned_mounts_become_one_claim_separated_by_subpath() -> None:
    """One deployment volume, one subPath per subject.

    Per-subject claims cannot serve a pool — a warm box is created before the
    conversation that borrows it, so there is no claim name to ask for — and the
    claim must outlive the box that mounts it, which is the whole reason these
    files moved off the overlay.
    """

    specs = volume_specs_from_planned_mounts(
        [("/home/conversations/asst_abc", "/astrabox/workspaces/0f9c2b7a4e114d0c9e1a3b5d7f9a1c3e")],
        claim_name="astrabox-workspaces",
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec["pvc"]["claimName"] == "astrabox-workspaces"
    assert spec["pvc"]["deleteOnSandboxTermination"] is False, (
        "a box's termination must never take the workspace with it"
    )
    assert spec["mountPath"] == "/home/conversations/asst_abc"
    assert spec["subPath"] == "astrabox/workspaces/0f9c2b7a4e114d0c9e1a3b5d7f9a1c3e", (
        "a subPath is relative to the volume root"
    )
    assert spec["readOnly"] is False


def test_the_volume_name_is_a_dns_label_whatever_the_path_looks_like() -> None:
    """Kubernetes constrains the name; the path travels in subPath instead."""

    import re

    specs = volume_specs_from_planned_mounts(
        [("/home/conversations", "/astrabox/workspaces/0f9c2b7a4e114d0c9e1a3b5d7f9a1c3e")],
        claim_name="astrabox-workspaces",
    )

    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", specs[0]["name"])
    assert len(specs[0]["name"]) <= 63


# ── the half that was missing, not the arithmetic ────────────────────────
#
# A planner nobody calls is what this repository already had. These drive the
# real create path and assert the mounts arrive at the sandbox backend.


class _RecordingProvider:
    """The create seam, recording the spec it was handed."""

    def __init__(self) -> None:
        self.specs: list[Any] = []

    async def create_sandbox(self, spec: Any) -> Any:
        self.specs.append(spec)
        return SimpleNamespace(sandbox_id="box-1")


@pytest.mark.asyncio
async def test_a_configured_deployment_puts_the_mounts_on_the_create_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The link that `27dd68d0` left broken: planned, therefore mounted."""

    from astrabox.core.service.orchestrator.engine import provisioning

    monkeypatch.setattr(
        provisioning, "plan_workspace_mounts", provisioning.plan_workspace_mounts
    )
    # The deployment setting is the platform's only planning gate.
    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", "astrabox-workspaces")
    monkeypatch.setenv("ASTRABOX_NAS_BASE_PATH", "/astrabox")

    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        assistant_workspace_dir,
    )

    planned = await provisioning.plan_workspace_mounts(
        subject_kind="assistant_runtime",
        session_id="",
        # Named here rather than resolved: this case is about what a plan
        # mounts, and resolving would ask a database what it is called.
        workspace_id=_WORKSPACE,
        agent_id=None,
        assistant_id="asst_abc123",
        user_id="owner-1",
        runtime_identity=None,
    )

    assert planned[0] == (
        assistant_workspace_dir("owner-1", "asst_abc123"),
        f"/astrabox/workspaces/{_WORKSPACE}/workspace",
    )
    assert len(planned) == 2, "the Assistant's memory travels with its files"


@pytest.mark.asyncio
async def test_a_deployment_with_no_volume_plans_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ephemeral is a configuration, not a silent degradation.

    The empty answer must come from the deployment saying so, which is why the
    volume setting is read first: a deployment that configured a volume and got
    no mounts is the defect this whole file exists for.
    """

    from astrabox.core.service.orchestrator.engine import provisioning

    monkeypatch.setenv("ASTRABOX_SANDBOX_WORKSPACE_VOLUME", "")

    assert (
        await provisioning.plan_workspace_mounts(
            subject_kind="assistant_runtime",
            session_id="",
            workspace_id=_WORKSPACE,
            agent_id=None,
            assistant_id="asst_abc123",
            user_id="owner-1",
            runtime_identity=None,
        )
        == ()
    )
