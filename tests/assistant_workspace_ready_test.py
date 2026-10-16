"""Assistant workspace READY, hibernation and recovery transitions, on the real store.

``mark_ready`` both binds ``current_sandbox_id`` and publishes READY: the
startup worker is the one that has actually created and started the sandbox,
so there is no separate provisioning commit that already wrote the pointer.
Each transition is one conditional write whose condition names the facts it
depends on; these cases start from a stored row, run the transition, and read
the row back.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.assistant.assistant_workspace_service import (
    AssistantWorkspaceService,
    get_assistant_profile_ready_marker,
)
from astrabox.providers import register_builtin_providers
from tests import _assistant_rows as rows

# These tests describe a deployment with the assistant engine installed, which
# the platform's startup does by installing its built-in adapters. Without it
# `assistant` is an unknown engine, whatever tests ran earlier in the process.
register_builtin_providers()

_SERVICE = AssistantWorkspaceService()


@pytest.fixture
async def stored(database: str) -> Any:
    """Write the Assistant ``a-1`` of ``u-1`` and its workspace in the given state."""

    async def write(**workspace: Any) -> None:
        await rows.assistant("a-1")
        await rows.workspace("a-1", **workspace)

    return write


async def _row() -> dict[str, Any]:
    row = await _SERVICE.get_workspace(user_id="u-1", assistant_id="a-1")
    assert row is not None
    return row


async def _mark_ready(session_id: str, *, revision: str = "rev-1") -> bool:
    return await _SERVICE.mark_ready(
        user_id="u-1",
        assistant_id="a-1",
        provisioning_session_id=session_id,
        provisioning_sandbox_generation="generation-1",
        sandbox_id="sb-1",
        configuration_revision=revision,
        expires_at="2026-01-01T00:00:00+00:00",
        runtime_identity={"workspace_dir": "/home/conversations/u-1/a-1/workspace"},
    )


async def test_mark_ready_is_fenced_on_the_bootstrap_that_claimed_materialization(
    stored: Any,
) -> None:
    await stored(
        state="MATERIALIZING",
        provisioning_session_id="bootstrap-1",
        provisioning_sandbox_generation="generation-1",
    )

    # Another Session's startup cannot publish this workspace.
    with pytest.raises(APIError) as stolen:
        await _mark_ready("bootstrap-2")
    assert stolen.value.code == "ASSISTANT_WORKSPACE_READY_CONFLICT"
    assert (await _row())["state"] == "MATERIALIZING"

    assert await _mark_ready("bootstrap-1")
    # The sandbox pointer is written here — nothing upstream binds it.
    row = await _row()
    assert row["state"] == "READY"
    assert row["current_sandbox_id"] == "sb-1"
    assert row["current_sandbox_expires_at"] == "2026-01-01T00:00:00+00:00"
    assert row["runtime_identity"] == {"workspace_dir": "/home/conversations/u-1/a-1/workspace"}
    assert row["provisioning_session_id"] is None
    assert row["provisioning_sandbox_generation"] is None
    assert get_assistant_profile_ready_marker(
        row, user_id="u-1", assistant_id="a-1", sandbox_id="sb-1", configuration_revision="rev-1",
    ) is not None
    # A retried startup whose publication already landed succeeds.
    assert await _mark_ready("bootstrap-1")


async def test_mark_ready_is_fenced_on_the_startup_generation(stored: Any) -> None:
    # A competing command of the same Session advanced the generation.
    await stored(
        state="MATERIALIZING",
        provisioning_session_id="bootstrap-1",
        provisioning_sandbox_generation="generation-2",
    )
    with pytest.raises(APIError) as raised:
        await _mark_ready("bootstrap-1")
    assert raised.value.code == "ASSISTANT_WORKSPACE_READY_CONFLICT"
    row = await _row()
    assert (row["state"], row["current_sandbox_id"]) == ("MATERIALIZING", None)


async def test_profile_readiness_is_fenced_on_the_current_ready_sandbox(stored: Any) -> None:
    await stored(state="READY", current_sandbox_id="sb-2")

    assert not await _SERVICE.mark_assistant_profile_ready(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-1",
        configuration_revision="rev-1",
    )
    row = await _row()
    assert (row["state"], row["current_sandbox_id"]) == ("READY", "sb-2")
    assert row["assistant_profiles"] == {}

    assert await _SERVICE.mark_assistant_profile_ready(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-2", configuration_revision="rev-1",
    )
    assert get_assistant_profile_ready_marker(
        await _row(), user_id="u-1", assistant_id="a-1", sandbox_id="sb-2",
        configuration_revision="rev-1",
    ) is not None


async def test_late_workspace_death_cannot_clear_a_replacement_box(stored: Any) -> None:
    await stored(state="READY", current_sandbox_id="sb-B", profiles={"u-1": "sb-B"})
    observed = {"assistant_id": "a-1", "state": "READY", "current_sandbox_id": "sb-A"}

    assert not await _SERVICE.converge_dead_sandbox(
        workspace=observed, sandbox_id="sb-A", last_error="sandbox terminated",
    )
    row = await _row()
    assert (row["state"], row["current_sandbox_id"]) == ("READY", "sb-B")
    assert set(row["assistant_profiles"]) != set()


# ── hibernating: commit boundary and pointer release ─────────────────────────


async def test_begin_hibernation_loses_to_a_replacement_sandbox(stored: Any) -> None:
    # The rebuild landed while hibernate stalled.
    await stored(state="READY", current_sandbox_id="sb-B")

    assert not await _SERVICE.begin_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        sandbox_id="sb-A",
        hibernated_at="2026-08-23T00:00:00+00:00",
    )
    row = await _row()
    assert (row["state"], row["current_sandbox_id"]) == ("READY", "sb-B")


async def test_finish_hibernation_never_severs_a_replacement_pointer(stored: Any) -> None:
    await stored(state="RECOVERY_REQUIRED", current_sandbox_id="sb-new")

    assert not await _SERVICE.finish_hibernation(
        user_id="u-1",
        assistant_id="a-1",
        destroyed_sandbox_id="sb-old",
        expected_state="RECOVERY_REQUIRED",
    )
    assert (await _row())["current_sandbox_id"] == "sb-new"


async def test_adoption_never_overwrites_a_pointer_a_rebuild_has_bound(stored: Any) -> None:
    await stored(state="READY", current_sandbox_id="sb-live")

    assert not await _SERVICE.adopt_undestroyed_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-orphan", reason="x"
    )
    row = await _row()
    assert (row["state"], row["current_sandbox_id"]) == ("READY", "sb-live")


# ── releasing a recovered sandbox pointer ────────────────────────────────────
#
# The pointer of a RECOVERY_REQUIRED workspace is the only record of a box
# whose destruction is pending, so the one transition that drops it is narrow.


async def _recovering(stored: Any, **overrides: Any) -> None:
    await stored(
        **{
            "state": "RECOVERY_REQUIRED",
            "current_sandbox_id": "sb-old",
            "current_sandbox_expires_at": "2026-01-01T00:00:00+00:00",
            "post_commit_cleanup_pending": True,
            **overrides,
        }
    )


async def test_release_loses_when_the_workspace_left_recovery(stored: Any) -> None:
    await _recovering(stored, state="MATERIALIZING")

    assert not await _SERVICE.release_recovered_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-old"
    )
    # The pointer survives a lost race — the other writer owns it now.
    assert (await _row())["current_sandbox_id"] == "sb-old"


async def test_release_loses_when_the_pointer_already_names_another_sandbox(
    stored: Any,
) -> None:
    """The ABA the sandbox_id argument exists to stop.

    wake#1 reads (RECOVERY_REQUIRED, A) and stalls. wake#2 runs the whole
    recovery and rebuilds, so the pointer now names a LIVE sandbox B while the
    state is still RECOVERY_REQUIRED. wake#1 then confirms A's death and
    releases. A state-only condition would match — and erase the only record
    of B.
    """
    await _recovering(stored, current_sandbox_id="sb-new")

    assert not await _SERVICE.release_recovered_sandbox(
        user_id="u-1", assistant_id="a-1", sandbox_id="sb-old"
    )
    # B survives: nothing else knows its id.
    assert (await _row())["current_sandbox_id"] == "sb-new"
