"""Each conversation and each workspace has a durable directory of its own.

The planner keys a conversation's files by its session, not by the account it
runs as — that account is the same in every box — and two workspaces never
share a directory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from astrabox.core.service.orchestrator.runtime.storage import (
    plan_subject_storage_mounts,
)
from astrabox.providers import register_builtin_providers

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
