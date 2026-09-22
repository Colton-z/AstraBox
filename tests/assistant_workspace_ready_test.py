"""Assistant workspace READY on the single (non-durable) backend.

``mark_ready`` both binds ``current_sandbox_id`` and publishes READY: the
startup worker is the one that has actually created and started the sandbox,
so there is no separate provisioning commit that already wrote the pointer,
and no ``applied_fingerprint`` to demand a match against. These pin that
contract — without them this path has no regression net.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
    get_assistant_profile_ready_marker,
)
from astrabox.core.service.orchestrator.runtime_binding import (
    resolve_assistant_workspace_binding,
)


class _FakeWorkspaceRepo:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.mark_ready_calls: list[dict[str, Any]] = []
        self.compare_and_update_calls: list[dict[str, Any]] = []
        self.marked = True

    async def get_workspace(self, user_id: str, assistant_id: str) -> Any:
        return dict(self.row) if self.row else None

    async def mark_ready(
        self,
        user_id: str,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: dict[str, Any] | None,
        profile_marker_key: str,
        profile_marker: dict[str, Any],
    ) -> bool:
        self.mark_ready_calls.append(
            {
                "user_id": user_id,
                "assistant_id": assistant_id,
                "provisioning_session_id": provisioning_session_id,
                "provisioning_sandbox_generation": provisioning_sandbox_generation,
                "sandbox_id": sandbox_id,
                "expires_at": expires_at,
                "runtime_identity": runtime_identity,
                "profile_marker_key": profile_marker_key,
                "profile_marker": dict(profile_marker),
            }
        )
        if self.row is not None and self.marked:
            self.row.update(
                {
                    "state": "READY",
                    "current_sandbox_id": sandbox_id,
                    "current_sandbox_expires_at": expires_at,
                    "runtime_identity": runtime_identity,
                    "provisioning_session_id": None,
                    "provisioning_sandbox_generation": None,
                }
            )
            self.row.setdefault("assistant_profiles", {})[
                profile_marker_key
            ] = dict(profile_marker)
        return self.marked

    async def mark_post_commit_failure(
        self,
        user_id: str,
        assistant_id: str,
        *,
        sandbox_id: str,
        cleanup_confirmed: bool,
        failure_phase: str,
    ) -> bool:
        return True

    async def compare_and_update_workspace(
        self,
        user_id: str,
        assistant_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        self.compare_and_update_calls.append(
            {"expected": dict(expected), "updates": dict(updates)}
        )
        row = self.row or {}
        if any(row.get(key) != value for key, value in expected.items()):
            return False
        row.update(updates)
        self.row = row
        return True


async def test_mark_ready_is_fenced_on_the_bootstrap_that_claimed_materialization() -> None:
    repo = _FakeWorkspaceRepo(
        {
            "state": "MATERIALIZING",
            "provisioning_session_id": "bootstrap-1",
        }
    )
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.mark_ready(
        user_id="u-1",
        assistant_id="a-1",
        provisioning_session_id="bootstrap-1",
        provisioning_sandbox_generation="generation-1",
        sandbox_id="sb-1",
        expires_at="2026-01-01T00:00:00Z",
        runtime_identity={"workspace_dir": "/home/conversations/u-1/a-1/workspace"},
    )
    # The sandbox pointer is written here — nothing upstream binds it now.
    assert repo.mark_ready_calls == [
        {
            "user_id": "u-1",
            "assistant_id": "a-1",
            "provisioning_session_id": "bootstrap-1",
            "provisioning_sandbox_generation": "generation-1",
            "sandbox_id": "sb-1",
            "expires_at": "2026-01-01T00:00:00Z",
            "runtime_identity": {"workspace_dir": "/home/conversations/u-1/a-1/workspace"},
            "profile_marker_key": repo.mark_ready_calls[0]["profile_marker_key"],
            "profile_marker": repo.mark_ready_calls[0]["profile_marker"],
        }
    ]
    assert get_assistant_profile_ready_marker(
        repo.row,
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-1",
    ) == repo.mark_ready_calls[0]["profile_marker"]


async def test_mark_ready_still_refuses_an_empty_sandbox_id() -> None:
    service = AssistantWorkspaceService(workspace_repo=_FakeWorkspaceRepo())
    with pytest.raises(APIError) as raised:
        await service.mark_ready(
            user_id="u-1",
            assistant_id="a-1",
            provisioning_session_id="bootstrap-1",
            provisioning_sandbox_generation="generation-1",
            sandbox_id="   ",
            expires_at=None,
            runtime_identity=None,
        )
    assert raised.value.code == "ASSISTANT_WORKSPACE_INVALID_SANDBOX"


async def test_mark_ready_surfaces_a_lost_compare_and_set() -> None:
    repo = _FakeWorkspaceRepo()
    repo.marked = False
    service = AssistantWorkspaceService(workspace_repo=repo)
    with pytest.raises(APIError) as raised:
        await service.mark_ready(
            user_id="u-1",
            assistant_id="a-1",
            provisioning_session_id="bootstrap-1",
            provisioning_sandbox_generation="generation-1",
            sandbox_id="sb-1",
            expires_at=None,
            runtime_identity=None,
        )
    assert raised.value.code == "ASSISTANT_WORKSPACE_READY_CONFLICT"


async def test_profile_readiness_is_fenced_on_the_current_ready_sandbox() -> None:
    repo = _FakeWorkspaceRepo(
        {
            "state": "READY",
            "current_sandbox_id": "sb-2",
        }
    )
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.mark_assistant_profile_ready(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-1",
    )
    assert repo.compare_and_update_calls == [
        {
            "expected": {
                "state": "READY",
                "current_sandbox_id": "sb-1",
            },
            "updates": repo.compare_and_update_calls[0]["updates"],
        }
    ]
    assert repo.row == {
        "state": "READY",
        "current_sandbox_id": "sb-2",
    }


async def test_materialize_returns_a_ready_row_without_an_applied_binding() -> None:
    # A READY row carrying no applied binding is accepted: on the non-durable
    # backend that describes every row that ever reaches READY.
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "READY",
        "current_sandbox_id": "sb-1",
    }
    service = AssistantWorkspaceService(workspace_repo=_FakeWorkspaceRepo(row))
    got = await service.materialize_workspace_if_absent(
        user_id="u-1",
        assistant_id="a-1",
        engine_kind="assistant",
        provisioning_session_id="bootstrap-new",
    )
    assert got["state"] == "READY"


async def test_confirmed_death_invalidates_every_box_derived_workspace_marker() -> None:
    row = {
        "assistant_id": "a-1",
        "created_by_user_id": "u-1",
        "engine_kind": "assistant",
        "state": "READY",
        "current_sandbox_id": "sb-1",
        "current_sandbox_expires_at": "2099-01-01T00:00:00Z",
        "hibernated_at": None,
        "runtime_identity": {"sandbox_id": "sb-1"},
        "assistant_profiles": {"u-1": {"sandbox_id": "sb-1"}},
        "provisioning_session_id": "bootstrap-old",
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.converge_dead_sandbox(
        workspace=dict(row),
        sandbox_id="sb-1",
        last_error="sandbox terminated",
    )

    (call,) = repo.compare_and_update_calls
    assert call["expected"] == {
        "state": "READY",
        "current_sandbox_id": "sb-1",
    }
    assert call["updates"]["state"] == "RECOVERY_REQUIRED"
    assert call["updates"]["current_sandbox_id"] is None
    assert call["updates"]["current_sandbox_expires_at"] is None
    assert call["updates"]["runtime_identity"] is None
    assert call["updates"]["assistant_profiles"] == {}
    assert call["updates"]["provisioning_session_id"] is None


async def test_late_workspace_death_cannot_clear_a_replacement_box() -> None:
    observed = {
        "assistant_id": "a-1",
        "created_by_user_id": "u-1",
        "state": "READY",
        "current_sandbox_id": "sb-A",
    }
    repo = _FakeWorkspaceRepo(
        {
            **observed,
            "current_sandbox_id": "sb-B",
        }
    )
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.converge_dead_sandbox(
        workspace=observed,
        sandbox_id="sb-A",
        last_error="sandbox terminated",
    )
    assert repo.row is not None
    assert repo.row["state"] == "READY"
    assert repo.row["current_sandbox_id"] == "sb-B"


# ── hibernating: commit boundary and pointer release ─────────────────────────


async def test_begin_hibernation_freezes_the_exact_ready_sandbox() -> None:
    """No file read starts until the caller owns READY plus this exact pointer."""
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "READY",
        "current_sandbox_id": "sb-1",
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.begin_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-1",
        hibernated_at="2026-08-23T00:00:00Z",
    )
    (call,) = repo.compare_and_update_calls
    assert call["expected"] == {
        "state": "READY",
        "current_sandbox_id": "sb-1",
    }
    assert call["updates"]["state"] == "HIBERNATING"
    assert call["updates"]["hibernated_at"] == "2026-08-23T00:00:00Z"
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-1"


async def test_begin_hibernation_loses_to_a_replacement_sandbox() -> None:
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "READY",
        "current_sandbox_id": "sb-B",  # the rebuild landed while hibernate stalled
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.begin_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-A",
        hibernated_at="2026-08-23T00:00:00Z",
    )
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-B"
    assert repo.row["state"] == "READY"


async def test_committed_hibernation_marks_only_destruction_pending() -> None:
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "HIBERNATING",
        "current_sandbox_id": "sb-1",
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.mark_hibernation_release_required(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-1"
    )
    (call,) = repo.compare_and_update_calls
    assert call["expected"] == {
        "state": "HIBERNATING",
        "current_sandbox_id": "sb-1",
    }
    assert call["updates"]["state"] == "RECOVERY_REQUIRED"
    assert call["updates"]["post_commit_cleanup_pending"] is True
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-1"


async def test_finish_hibernation_never_severs_a_replacement_pointer() -> None:
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": "sb-new",
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.finish_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        destroyed_sandbox_id="sb-old",
        expected_state="RECOVERY_REQUIRED",
    )
    assert repo.row is not None
    assert repo.row["current_sandbox_id"] == "sb-new"


async def test_finish_hibernation_clears_every_box_derived_field() -> None:
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": "sb-old",
        "current_sandbox_expires_at": "2099-01-01T00:00:00Z",
        "runtime_identity": {"sandbox_id": "sb-old"},
        "assistant_profiles": {"u-1": {"sandbox_id": "sb-old"}},
        "post_commit_cleanup_pending": True,
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.finish_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        destroyed_sandbox_id="sb-old",
        expected_state="RECOVERY_REQUIRED",
    )
    assert repo.row is not None
    assert repo.row["state"] == "HIBERNATING"
    assert repo.row["current_sandbox_id"] is None
    assert repo.row["current_sandbox_expires_at"] is None
    assert repo.row["runtime_identity"] is None
    assert repo.row["assistant_profiles"] == {}
    assert repo.row["post_commit_cleanup_pending"] is False


async def test_an_undestroyed_orphan_is_adopted_onto_the_empty_pointer() -> None:
    """The window every failed bootstrap dies inside.

    ``current_sandbox_id`` is written by ``mark_ready``; a box exists from the
    moment its create returns. A convergence that read the empty pointer as "no
    orphan to chase" published a state over a running box. The id now takes the
    pointer, which turns a nameless orphan into the ordinary pending
    destruction wake already retries.
    """
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "MATERIALIZING",
        "current_sandbox_id": None,
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.adopt_undestroyed_sandbox(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-orphan",
        reason="bootstrap_runtime_start_failed_sandbox_undestroyed",
    )
    (call,) = repo.compare_and_update_calls
    assert call["expected"] == {"current_sandbox_id": None}
    assert call["updates"]["state"] == "RECOVERY_REQUIRED"
    assert call["updates"]["current_sandbox_id"] == "sb-orphan"
    assert call["updates"]["post_commit_cleanup_pending"] is True


async def test_adoption_never_overwrites_a_pointer_a_rebuild_has_bound() -> None:
    row = {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "READY",
        "current_sandbox_id": "sb-live",
    }
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.adopt_undestroyed_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-orphan", reason="x"
    )
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-live"


# ── releasing a recovered sandbox pointer ────────────────────────────────────
#
# The other half of the collapsed path: nothing writes ``current_sandbox_id``
# to None on the recovery route any more, so the workspace service owns the
# one release primitive, and it is deliberately narrow.


def _recovering_row() -> dict[str, Any]:
    return {
        "assistant_id": "a-1",
        "engine_kind": "assistant",
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": "sb-old",
        "current_sandbox_expires_at": "2026-01-01T00:00:00Z",
        "post_commit_cleanup_pending": True,
    }


async def test_release_clears_the_pointer_and_stays_in_recovery() -> None:
    repo = _FakeWorkspaceRepo(_recovering_row())
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert await service.release_recovered_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-old"
    )

    (call,) = repo.compare_and_update_calls
    # Fenced on RECOVERY_REQUIRED *and* on the pointer still naming the sandbox
    # the caller proved dead: the release must lose both to a concurrent
    # transition that took the workspace elsewhere and to one that repointed it.
    assert call["expected"] == {
        "state": "RECOVERY_REQUIRED",
        "current_sandbox_id": "sb-old",
    }
    assert call["updates"]["state"] == "RECOVERY_REQUIRED"
    assert call["updates"]["current_sandbox_id"] is None
    assert call["updates"]["current_sandbox_expires_at"] is None
    assert call["updates"]["post_commit_cleanup_pending"] is False
    assert repo.row is not None and repo.row["current_sandbox_id"] is None


async def test_release_loses_when_the_workspace_left_recovery() -> None:
    row = _recovering_row()
    row["state"] = "MATERIALIZING"
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.release_recovered_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-old"
    )
    # The pointer survives a lost race — the other writer owns it now.
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-old"


async def test_release_loses_when_the_pointer_already_names_another_sandbox() -> None:
    """The ABA the sandbox_id argument exists to stop.

    wake#1 reads (RECOVERY_REQUIRED, A) and stalls. wake#2 runs the whole
    recovery and rebuilds, so the pointer now names a LIVE sandbox B while the
    state is still RECOVERY_REQUIRED. wake#1 then confirms A's death and
    releases. A state-only fence matches — and erases the only record of B.
    """
    row = _recovering_row()
    row["current_sandbox_id"] = "sb-new"
    repo = _FakeWorkspaceRepo(row)
    service = AssistantWorkspaceService(workspace_repo=repo)

    assert not await service.release_recovered_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-old"
    )

    (call,) = repo.compare_and_update_calls
    assert call["expected"]["current_sandbox_id"] == "sb-old"
    # B survives: nothing else knows its id.
    assert repo.row is not None and repo.row["current_sandbox_id"] == "sb-new"


async def test_release_refuses_without_the_sandbox_id_that_was_killed() -> None:
    # The caller must name what it proved gone; a blank id is a caller bug,
    # not a licence to drop the pointer.
    repo = _FakeWorkspaceRepo(_recovering_row())
    service = AssistantWorkspaceService(workspace_repo=repo)
    with pytest.raises(APIError) as raised:
        await service.release_recovered_sandbox(
            user_id="u-1", assistant_id="a-1", sandbox_id="  "
        )
    assert raised.value.code == "ASSISTANT_WORKSPACE_INVALID_SANDBOX"
    assert repo.compare_and_update_calls == []


class _FakeWorkspaceService:
    def __init__(self, workspace: dict[str, Any] | None) -> None:
        self._workspace = workspace

    async def get_workspace(self, *, user_id: str, assistant_id: str) -> Any:
        return dict(self._workspace) if self._workspace else None


def _assistant_session() -> dict[str, Any]:
    return {
        "session_id": "s-1",
        "session_kind": "assistant_chat",
        "user_id": "u-1",
        "workspace_ref": {
            "kind": "assistant",
            "assistant_id": "a-1",
            "user_id": "u-1",
            "engine_kind": "assistant",
        },
    }


async def test_ready_workspace_with_a_sandbox_dispatches() -> None:
    resolution = await resolve_assistant_workspace_binding(
        session=_assistant_session(),
        assistant_workspace_service=_FakeWorkspaceService(
            {
                "state": "READY",
                "engine_kind": "assistant",
                "current_sandbox_id": "sb-1",
                "current_sandbox_expires_at": "2026-01-01T00:00:00Z",
            }
        ),
    )
    assert resolution.status == "READY"
    assert resolution.can_dispatch is True
    assert resolution.sandbox_id == "sb-1"


async def test_ready_workspace_without_a_sandbox_does_not_dispatch() -> None:
    resolution = await resolve_assistant_workspace_binding(
        session=_assistant_session(),
        assistant_workspace_service=_FakeWorkspaceService(
            {
                "state": "READY",
                "engine_kind": "assistant",
                "current_sandbox_id": None,
            }
        ),
    )
    assert resolution.status == "UNAVAILABLE"
    assert resolution.reason_code == "ASSISTANT_WORKSPACE_SANDBOX_MISSING"
    assert resolution.can_dispatch is False


async def test_workspace_engine_conflict_is_a_named_unavailable_state() -> None:
    resolution = await resolve_assistant_workspace_binding(
        session=_assistant_session(),
        assistant_workspace_service=_FakeWorkspaceService(
            {
                "state": "READY",
                "engine_kind": "claude_code",
                "current_sandbox_id": "sb-1",
            }
        ),
    )

    assert resolution.status == "UNAVAILABLE"
    assert resolution.reason_code == "ASSISTANT_WORKSPACE_ENGINE_MISMATCH"
    assert resolution.can_dispatch is False


async def test_uninstalled_assistant_engine_is_a_named_unavailable_state() -> None:
    session = _assistant_session()
    session["workspace_ref"]["engine_kind"] = "uninstalled"
    resolution = await resolve_assistant_workspace_binding(
        session=session,
        assistant_workspace_service=_FakeWorkspaceService(None),
    )

    assert resolution.status == "UNAVAILABLE"
    assert resolution.reason_code == "SESSION_RUNTIME_IDENTITY_INVALID"
    assert "uninstalled" in str(resolution.reason_message)
