"""Assistant workspace service - materialization, wake, hibernate.

Workspace lifetime is bound to the assistant runtime, not to each user. The
first call to ``materialize_workspace_if_absent`` allocates the
``assistant_workspace`` row with ``_id = sha1(assistant_id)`` as primary-key
fence; subsequent calls from any user return the existing row.

This service only owns workspace state. A conversation Session may materialize
the workspace through the common runtime-subject lifecycle; explicit wake uses
a hidden Session because it has no conversation Session of its own.

MATERIALIZING is not a resting state: it asserts that one materializer Session
is driving the workspace toward READY. That Session owns the whole transition
out of it — ``mark_ready`` on success, ``mark_materialization_failed``
when it fails before binding a sandbox, ``mark_post_commit_failure`` when it
fails after. If the process carrying it dies, none of those run, and
``claim_stalled_materialization`` is how the next wake proves the owner is
gone and takes the rebuild.

HIBERNATING closes workspace admission while AssistantService asks the engine
adapter to stop native writers and confirm state storage in the platform
database. The live sandbox pointer stays named until destruction is confirmed.
During hibernation, ``RECOVERY_REQUIRED`` with ``post_commit_cleanup_pending``
records that native state was saved and destruction still needs proof. A
completed hibernate has no sandbox pointer; wake materializes a fresh box and
restores native state. Workspace files survive that replacement only when
optional persistent workspace storage is configured.
"""

from __future__ import annotations

import hashlib
from typing import Any

from astrabox.persistence.repository.assistant_workspace_repository import (
    AssistantWorkspaceRepository,
    assistant_workspace_fence_id,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.core.service.orchestrator.engine.capabilities import (
    engine_allowed_for_session_kind,
)
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.seams.sandbox_disposal import SandboxDestruction, may_sever_last_name

logger = get_logger(__name__)

# Engine validity comes from the single matrix (domain-model.md §2); the
# workspace layer must be exactly as strict as the product layer.
_VALID_STATES = frozenset({"MATERIALIZING", "READY", "HIBERNATING", "RECOVERY_REQUIRED"})

# Per-profile resident engine control processes, keyed by the same marker key
# as ``assistant_profiles``. A record names the execd PTY holding the
# profile's resident gateway and the spawn fingerprint of the profile
# configuration it was launched from. The record is a rendezvous, not an
# authority: readers verify it against the live sandbox (execd 404 means the
# process is gone), and it is cleared wherever the box's processes are known
# dead — ``converge_dead_sandbox`` and ``finish_hibernation``.
_ASSISTANT_GATEWAYS_FIELD = "assistant_gateways"


def assistant_profile_marker_key(*, user_id: str, assistant_id: str) -> str:
    profile_key = f"{str(user_id or '').strip()}:{str(assistant_id or '').strip()}"
    return hashlib.sha256(f"assistant-profile|{profile_key}".encode("utf-8")).hexdigest()


def _assistant_profile_ready_marker(
    *,
    user_id: str,
    assistant_id: str,
    sandbox_id: str,
) -> tuple[str, dict[str, Any]]:
    normalized_user_id = str(user_id or "").strip()
    normalized_assistant_id = str(assistant_id or "").strip()
    normalized_sandbox_id = str(sandbox_id or "").strip()
    if not normalized_user_id or not normalized_assistant_id or not normalized_sandbox_id:
        raise APIError(
            code="ASSISTANT_PROFILE_MARKER_INVALID",
            message=(
                "assistant profile marker requires user_id, "
                "assistant_id and sandbox_id"
            ),
            status_code=500,
        )
    profile_key = f"{normalized_user_id}:{normalized_assistant_id}"
    marker_key = assistant_profile_marker_key(
        user_id=normalized_user_id,
        assistant_id=normalized_assistant_id,
    )
    return marker_key, {
        "status": "ready",
        "user_id": normalized_user_id,
        "assistant_id": normalized_assistant_id,
        "profile_key": profile_key,
        "sandbox_id": normalized_sandbox_id,
        "control_transport": "opensandbox_execd_pty",
        "ready_at": utcnow_iso(),
    }


def get_assistant_profile_ready_marker(
    workspace: dict[str, Any] | None,
    *,
    user_id: str,
    assistant_id: str,
    sandbox_id: str,
) -> dict[str, Any] | None:
    if not isinstance(workspace, dict):
        return None
    normalized_user_id = str(user_id or "").strip()
    normalized_assistant_id = str(assistant_id or "").strip()
    normalized_sandbox_id = str(sandbox_id or "").strip()
    if not normalized_user_id or not normalized_assistant_id or not normalized_sandbox_id:
        return None

    marker_key = assistant_profile_marker_key(
        user_id=normalized_user_id,
        assistant_id=normalized_assistant_id,
    )
    markers = workspace.get("assistant_profiles")
    marker = markers.get(marker_key) if isinstance(markers, dict) else None
    if isinstance(marker, dict):
        if (
            str(marker.get("status") or "").strip() == "ready"
            and str(marker.get("user_id") or "").strip() == normalized_user_id
            and str(marker.get("assistant_id") or "").strip() == normalized_assistant_id
            and str(marker.get("sandbox_id") or "").strip() == normalized_sandbox_id
        ):
            return marker
        return None

    return None


class AssistantWorkspaceService:
    def __init__(self, *, workspace_repo: AssistantWorkspaceRepository | None = None) -> None:
        self._workspace_repo = workspace_repo or AssistantWorkspaceRepository()

    async def get_workspace(self, *, user_id: str, assistant_id: str) -> dict[str, Any] | None:
        return await self._workspace_repo.get_workspace(user_id, assistant_id)

    async def has_assistant_workspace(self, *, assistant_id: str) -> bool:
        return await self._workspace_repo.has_assistant_workspace(assistant_id)

    async def list_user_workspaces(
        self, *, user_id: str
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_user_workspaces(user_id)

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_workspaces_by_sandbox_id(sandbox_id)

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_dead_binding_probe_candidates(
            now_iso=now_iso,
            limit=limit,
        )

    async def realign_live_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
    ) -> bool:
        """Refresh one live workspace lease behind its exact sandbox fence."""
        resolved_sandbox_id = str(sandbox_id or "").strip()
        resolved_expires_at = str(expires_at or "").strip() or None
        assistant_id = str((workspace or {}).get("assistant_id") or "").strip()
        user_id = str(
            (workspace or {}).get("created_by_user_id")
            or (workspace or {}).get("user_id")
            or ""
        ).strip()
        if not resolved_sandbox_id or not assistant_id:
            return False
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected={"current_sandbox_id": resolved_sandbox_id},
            updates={"current_sandbox_expires_at": resolved_expires_at},
        )

    async def converge_dead_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        last_error: str,
    ) -> bool:
        """Clear one confirmed-dead workspace pointer under an exact CAS fence.

        The shared lifecycle coordinator establishes that the box is terminal.
        This owner-specific transition says what that fact means for an
        Assistant: no conversation may dispatch, every box-derived marker is
        invalid, and the next wake may materialize a replacement workspace.

        Matching both state and ``current_sandbox_id`` prevents a late notice
        about sandbox A from clearing a concurrently published sandbox B.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        assistant_id = str((workspace or {}).get("assistant_id") or "").strip()
        state = str((workspace or {}).get("state") or "").strip()
        if not resolved_sandbox_id or not assistant_id or state not in _VALID_STATES:
            return False
        return await self.transition_state(
            user_id=str(
                (workspace or {}).get("created_by_user_id")
                or (workspace or {}).get("user_id")
                or ""
            ).strip(),
            assistant_id=assistant_id,
            expected_state=state,
            new_state="RECOVERY_REQUIRED",
            expected_extra={"current_sandbox_id": resolved_sandbox_id},
            extra_updates={
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "hibernated_at": None,
                "runtime_identity": None,
                "assistant_profiles": {},
                _ASSISTANT_GATEWAYS_FIELD: {},
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "post_commit_cleanup_pending": False,
                "last_error": str(last_error or "sandbox terminated"),
            },
        )

    async def materialize_workspace_if_absent(
        self,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
        provisioning_session_id: str,
        template_capability_hash: str = "",
    ) -> dict[str, Any]:
        if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_ENGINE",
                message=f"unknown engine_kind={engine_kind!r}",
                status_code=400,
            )
        resolved_provisioning_session_id = str(provisioning_session_id or "").strip()
        if not resolved_provisioning_session_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message="materializing a workspace requires a materializer Session id",
                status_code=500,
            )
        existing = await self._workspace_repo.get_workspace(user_id, assistant_id)
        if existing is not None:
            if str(existing.get("engine_kind") or "").strip() != engine_kind:
                raise APIError(
                    code="ASSISTANT_WORKSPACE_ENGINE_MISMATCH",
                    message=(
                        f"workspace engine_kind={existing.get('engine_kind')!r} "
                        f"cannot change to {engine_kind!r}"
                    ),
                    status_code=409,
                )
            return existing
        payload = {
            "created_by_user_id": user_id,
            "assistant_id": assistant_id,
            "engine_kind": engine_kind,
            "runtime_identity": None,
            "current_sandbox_id": None,
            "current_sandbox_expires_at": None,
            "state": "MATERIALIZING",
            "provisioning_session_id": resolved_provisioning_session_id,
            "provisioning_sandbox_generation": None,
            "last_error": None,
            "template_capability_hash": template_capability_hash,
        }
        try:
            return await self._workspace_repo.materialize_workspace(payload)
        except Exception as exc:
            # Race-loser path: the primary-key fence guarantees one winner.
            fence_id = assistant_workspace_fence_id(user_id, assistant_id)
            logger.info(
                "assistant_workspace race loser: user=%s assistant=%s fence=%s exc=%s",
                user_id,
                assistant_id,
                fence_id,
                exc,
            )
            row = await self._workspace_repo.get_workspace(user_id, assistant_id)
            if row is None:
                raise
            return row

    async def claim_materialization(
        self,
        *,
        user_id: str,
        assistant_id: str,
        expected_state: str,
        provisioning_session_id: str,
    ) -> bool:
        """Give exactly one Session the right to create the workspace sandbox."""

        resolved_session_id = str(provisioning_session_id or "").strip()
        if not resolved_session_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message="claiming materialization requires a materializer Session id",
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state=expected_state,
            new_state="MATERIALIZING",
            expected_extra={"provisioning_session_id": None},
            extra_updates={
                "provisioning_session_id": resolved_session_id,
                "provisioning_sandbox_generation": None,
                "hibernated_at": None,
                "last_error": None,
            },
        )

    async def claim_materialization_generation(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        observed_generation: str | None,
        sandbox_generation: str,
    ) -> bool:
        """Qualify the reserved Session owner with its durable startup generation."""
        if not sandbox_generation:
            raise ValueError("materialization requires a sandbox generation")
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected={
                "state": "MATERIALIZING",
                "provisioning_session_id": provisioning_session_id,
                "provisioning_sandbox_generation": observed_generation,
            },
            updates={"provisioning_sandbox_generation": sandbox_generation},
        )

    async def transition_state(
        self,
        *,
        user_id: str,
        assistant_id: str,
        expected_state: str | None,
        new_state: str,
        extra_updates: dict[str, Any] | None = None,
        expected_extra: dict[str, Any] | None = None,
    ) -> bool:
        """Move the workspace to ``new_state`` under a compare-and-set fence.

        ``expected_state`` fences on the state alone; ``expected_extra`` adds
        further fields to the same predicate, for transitions whose safety
        depends on which sandbox or which provisioning session the row still
        names. A transition that rewrites a pointer must name the value it
        believes that pointer holds, or it can overwrite a newer one.
        """
        if new_state not in _VALID_STATES:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_STATE",
                message=f"unknown state={new_state!r}",
                status_code=400,
            )
        if new_state == "READY":
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_STATE",
                message=(
                    "READY may only be published by mark_ready (a materializer that "
                    "built a box) or restore_ready_after_hibernation_failure "
                    "(the unchanged live box whose commit failed)"
                ),
                status_code=409,
            )
        updates: dict[str, Any] = {"state": new_state}
        if extra_updates:
            supplied_state = extra_updates.get("state")
            if supplied_state is not None and supplied_state != new_state:
                raise APIError(
                    code="ASSISTANT_WORKSPACE_INVALID_STATE",
                    message="extra_updates cannot override the fenced state transition",
                    status_code=400,
                )
            updates.update(extra_updates)
        expected: dict[str, Any] = {}
        if expected_state is not None:
            expected["state"] = expected_state
        if expected_extra:
            expected.update(expected_extra)
        if not expected:
            return await self._workspace_repo.update_workspace(
                user_id, assistant_id, updates
            )
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected=expected,
            updates=updates,
        )

    async def mark_ready(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        sandbox_id: str,
        expires_at: str | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> bool:
        normalized_sandbox_id = str(sandbox_id or "").strip()
        if not normalized_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=f"workspace ready requires sandbox_id user={user_id} assistant={assistant_id}",
                status_code=500,
            )
        profile_marker_key, profile_marker = _assistant_profile_ready_marker(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=normalized_sandbox_id,
        )
        marked = await self._workspace_repo.mark_ready(
            user_id,
            assistant_id,
            provisioning_session_id=provisioning_session_id,
            provisioning_sandbox_generation=provisioning_sandbox_generation,
            sandbox_id=normalized_sandbox_id,
            expires_at=expires_at,
            runtime_identity=runtime_identity,
            profile_marker_key=profile_marker_key,
            profile_marker=profile_marker,
        )
        if not marked:
            raise APIError(
                code="ASSISTANT_WORKSPACE_READY_CONFLICT",
                message=(
                    "workspace READY compare-and-set lost to a concurrent "
                    "state transition"
                ),
                status_code=409,
            )
        return True

    async def mark_post_commit_failure(
        self,
        *,
        user_id: str,
        assistant_id: str,
        cleanup: SandboxDestruction | None,
        sandbox_id: str,
        failure_phase: str,
    ) -> bool:
        """Fence a failure that happened after the workspace bound a sandbox.

        ``cleanup`` is the destruction verdict, not a boolean the caller
        computed. A boolean cannot distinguish a proven destruction from a
        believed one, and the repository underneath clears the pointer — the
        box's last surviving name — only for a proven one. Passing the verdict
        lets that pairing rule be checked against the sandbox this row actually
        names, rather than trusted from wherever the boolean came from.
        """
        sandbox_id = str(sandbox_id or "").strip()
        if not sandbox_id:
            return False
        return await self._workspace_repo.mark_post_commit_failure(
            user_id,
            assistant_id,
            sandbox_id=sandbox_id,
            cleanup_confirmed=may_sever_last_name(cleanup, sandbox_id=sandbox_id),
            failure_phase=failure_phase,
        )

    async def mark_assistant_profile_ready(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> bool:
        marker_key, marker = _assistant_profile_ready_marker(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=sandbox_id,
        )
        return await self._workspace_repo.compare_and_update_workspace(
            str(user_id).strip(),
            str(assistant_id).strip(),
            expected={
                "state": "READY",
                "current_sandbox_id": str(sandbox_id).strip(),
            },
            updates={f"assistant_profiles.{marker_key}": marker},
        )

    async def get_profile_gateway_process(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> dict[str, str] | None:
        """The recorded resident control process for one profile, if current.

        Returns None when there is no record or the record names another
        sandbox — a stale entry a crashed transition left behind, which the
        next :meth:`record_profile_gateway_process` overwrites under its CAS.
        """

        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_sandbox_id:
            return None
        workspace = await self._workspace_repo.get_workspace(user_id, assistant_id)
        if not isinstance(workspace, dict):
            return None
        marker_key = assistant_profile_marker_key(
            user_id=user_id, assistant_id=assistant_id
        )
        records = workspace.get(_ASSISTANT_GATEWAYS_FIELD)
        record = records.get(marker_key) if isinstance(records, dict) else None
        if not isinstance(record, dict):
            return None
        if str(record.get("sandbox_id") or "").strip() != resolved_sandbox_id:
            return None
        pty_session_id = str(record.get("pty_session_id") or "").strip()
        if not pty_session_id:
            return None
        return {
            "pty_session_id": pty_session_id,
            "spawn_fingerprint": str(record.get("spawn_fingerprint") or "").strip(),
        }

    async def record_profile_gateway_process(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        engine_kind: str,
        pty_session_id: str,
        spawn_fingerprint: str,
        expected_pty_session_id: str | None,
    ) -> bool:
        """Swap one profile's resident-process record under a CAS fence.

        ``expected_pty_session_id`` is the PTY the caller last observed for
        this sandbox (None for no current record). The compare-and-set is what
        gives concurrent spawners — including ones in different host
        processes — exactly one winner: the loser's write returns False and
        the loser destroys its own spawn and adopts the published one.
        """

        resolved_sandbox_id = str(sandbox_id or "").strip()
        resolved_pty = str(pty_session_id or "").strip()
        if not resolved_sandbox_id or not resolved_pty:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "recording a profile gateway requires the sandbox and PTY ids "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        marker_key = assistant_profile_marker_key(
            user_id=user_id, assistant_id=assistant_id
        )
        record_path = f"{_ASSISTANT_GATEWAYS_FIELD}.{marker_key}"
        record = {
            "engine_kind": str(engine_kind or "").strip(),
            "sandbox_id": resolved_sandbox_id,
            "pty_session_id": resolved_pty,
            "spawn_fingerprint": str(spawn_fingerprint or "").strip(),
            "recorded_at": utcnow_iso(),
        }
        if expected_pty_session_id is not None:
            return await self._workspace_repo.compare_and_update_workspace(
                user_id,
                assistant_id,
                expected={
                    f"{record_path}.pty_session_id": str(expected_pty_session_id)
                },
                updates={record_path: record},
            )
        workspace = await self._workspace_repo.get_workspace(user_id, assistant_id)
        if not isinstance(workspace, dict):
            return False
        records = workspace.get(_ASSISTANT_GATEWAYS_FIELD)
        raw = records.get(marker_key) if isinstance(records, dict) else None
        stale_pty = (
            str(raw.get("pty_session_id") or "").strip()
            if isinstance(raw, dict)
            else ""
        )
        if stale_pty:
            # The caller saw no record because the stored one names another
            # sandbox; fencing on that stale PTY still gives concurrent
            # writers one winner while letting the stale entry be replaced.
            return await self._workspace_repo.compare_and_update_workspace(
                user_id,
                assistant_id,
                expected={f"{record_path}.pty_session_id": stale_pty},
                updates={record_path: record},
            )
        if not isinstance(raw, dict):
            # The compare-and-set below matches the field's current value
            # exactly, and a document-store filter on None does not reliably
            # match an absent key; write the explicit None so the one-winner
            # CAS is decidable.
            await self._workspace_repo.update_workspace(
                user_id, assistant_id, {record_path: None}
            )
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected={record_path: None},
            updates={record_path: record},
        )

    async def begin_hibernation(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        hibernated_at: str,
    ) -> bool:
        """Freeze a READY workspace before reading its files into the medium."""
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "hibernating a workspace requires its current sandbox "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        resolved_hibernated_at = str(hibernated_at or "").strip()
        if not resolved_hibernated_at:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_HIBERNATE_MARK",
                message=(
                    "hibernating a workspace requires its start time "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state="READY",
            new_state="HIBERNATING",
            expected_extra={"current_sandbox_id": resolved_sandbox_id},
            extra_updates={
                "hibernated_at": resolved_hibernated_at,
                "last_error": None,
            },
        )

    async def mark_hibernation_release_required(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> bool:
        """Record that files committed and only destruction remains.

        This is the crash boundary. Before it, recovery must commit again;
        after it, recovery may destroy the named box without risking file loss.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "recording a committed hibernate requires its sandbox "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected={
                "state": "HIBERNATING",
                "current_sandbox_id": resolved_sandbox_id,
            },
            updates={
                "state": "RECOVERY_REQUIRED",
                "post_commit_cleanup_pending": True,
                "last_error": None,
            },
        )

    async def finish_hibernation(
        self,
        *,
        user_id: str,
        assistant_id: str,
        destroyed_sandbox_id: str | None,
        expected_state: str,
    ) -> bool:
        """Publish a hibernated workspace only after its box is proven gone."""
        resolved_sandbox_id = str(destroyed_sandbox_id or "").strip() or None
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state=expected_state,
            new_state="HIBERNATING",
            expected_extra={"current_sandbox_id": resolved_sandbox_id},
            extra_updates={
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "runtime_identity": None,
                "assistant_profiles": {},
                _ASSISTANT_GATEWAYS_FIELD: {},
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "post_commit_cleanup_pending": False,
                "last_error": None,
            },
        )

    async def restore_ready_after_hibernation_failure(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> bool:
        """Unfreeze the same live box when its storage commit failed."""
        resolved_sandbox_id = str(sandbox_id or "").strip()
        return await self._workspace_repo.compare_and_update_workspace(
            user_id,
            assistant_id,
            expected={
                "state": "HIBERNATING",
                "current_sandbox_id": resolved_sandbox_id,
            },
            updates={
                "state": "READY",
                "hibernated_at": None,
                "last_error": None,
            },
        )

    async def adopt_undestroyed_sandbox(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        reason: str,
        expected_owner: dict[str, Any] | None = None,
    ) -> bool:
        """Take a box the workspace never bound onto the pointer, as its name.

        ``current_sandbox_id`` is written by ``mark_ready`` and by nothing
        else, while a sandbox exists from the moment its create returns. Every
        failed materializer dies inside that window, so an empty workspace
        pointer does not mean no box was built — it means the build did not
        finish. Reading the empty pointer as "no orphan to chase" would publish
        HIBERNATING over a running box and clear the last reference to its
        materializer.

        So when a failure tail finds a box it could not confirm destroyed, the
        box's id is written onto the pointer and the workspace goes to
        RECOVERY_REQUIRED with the cleanup marked pending — exactly the shape
        ``wake`` already retries (``_retry_recovery_destruction`` in
        ``assistant_service.py``). The orphan then converges as an ordinary
        pending destruction.

        Compare-and-set on the pointer still being empty: if a rebuild has
        bound a sandbox meanwhile, that pointer is a live box's name and this
        one must not overwrite it — the id then remains only in the caller's
        error.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "adopting an undestroyed sandbox requires its id "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state=None,
            new_state="RECOVERY_REQUIRED",
            expected_extra={"current_sandbox_id": None, **(expected_owner or {})},
            extra_updates={
                "current_sandbox_id": resolved_sandbox_id,
                "post_commit_cleanup_pending": True,
                "last_error": reason,
            },
        )

    async def release_recovered_sandbox(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
    ) -> bool:
        """Drop the live pointer of a RECOVERY_REQUIRED workspace after a
        confirmed kill of exactly ``sandbox_id``.

        The pointer is what makes the pending destruction retryable: it is the
        only record of the orphan's id. Callers must therefore have proven the
        sandbox is gone before calling — an unconfirmed release turns a
        retryable orphan into a box nothing can ever name again.

        Compare-and-set on RECOVERY_REQUIRED **and on the pointer still naming
        exactly ``sandbox_id``**, staying in that state: this call only releases
        the pointer, and the caller decides what the workspace does next.
        Returns False when the workspace has meanwhile left RECOVERY_REQUIRED
        or the pointer has moved to another sandbox — in both cases a
        concurrent transition owns it now.

        Naming the sandbox in the predicate is what makes the release safe
        under ABA: a caller that read (RECOVERY_REQUIRED, A), stalled while
        another wake rebuilt onto a live sandbox B, and then confirmed A's
        death, must not have its release land on B. ``mark_post_commit_failure``
        fences the same pointer the same way.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "releasing a recovered sandbox requires the sandbox_id that "
                    f"was killed user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state="RECOVERY_REQUIRED",
            new_state="RECOVERY_REQUIRED",
            expected_extra={"current_sandbox_id": resolved_sandbox_id},
            extra_updates={
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "post_commit_cleanup_pending": False,
            },
        )

    async def mark_materialization_failed(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        failure_phase: str,
    ) -> bool:
        """Publish a materializer that ended before any sandbox was bound.

        The caller must first establish that no sandbox survived. An empty
        workspace pointer is insufficient because ``mark_ready`` writes it only
        after sandbox creation. Unconfirmed sandboxes must instead go through
        :meth:`adopt_undestroyed_sandbox`.

        The compare-and-set moves an unchanged MATERIALIZING workspace to
        HIBERNATING, records the failure, and leaves a concurrent wake untouched.
        """
        resolved_phase = str(failure_phase or "").strip()
        if not resolved_phase:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_FAILURE_PHASE",
                message=(
                    "publishing a materialization failure requires the failed phase "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state="MATERIALIZING",
            new_state="HIBERNATING",
            expected_extra={
                "provisioning_session_id": provisioning_session_id,
                "provisioning_sandbox_generation": provisioning_sandbox_generation,
                "current_sandbox_id": None,
            },
            extra_updates={
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "last_error": f"materialization_{resolved_phase}",
            },
        )

    async def claim_stalled_materialization(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        last_error: str,
    ) -> bool:
        """Take over a MATERIALIZING workspace whose materializer Session is gone.

        A materialization is only in flight while the Session that owns it is
        alive. When that Session reached a terminal state without publishing a
        transition out of MATERIALIZING (the process died mid-startup, so
        nothing ran the failure tail), nobody is driving the row and nobody
        ever will.

        The compare-and-set names both facts about the row that this transition
        depends on: it still points at that dead session, and it still holds no
        sandbox. Those make the claim atomic — exactly one concurrent wake
        takes the rebuild and the losers keep reporting MATERIALIZING.

        What ``current_sandbox_id: None`` does not establish is that the dead
        materializer left no box: the pointer is written by ``mark_ready``, so
        an empty one says only that materialization did not finish. The box, if
        there is one, is named on the dead session's own row, and proving it
        gone is the caller's job before calling this
        (``_redrive_stalled_materialization`` in ``assistant_service.py``) —
        because this transition clears ``provisioning_session_id``, the last
        reference to the materializer that would let anyone find that box again.
        """
        resolved_provisioning_session_id = str(provisioning_session_id or "").strip()
        if not resolved_provisioning_session_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message=(
                    "claiming a stalled materialization requires the Session "
                    f"session it stalled on user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state="MATERIALIZING",
            new_state="MATERIALIZING",
            expected_extra={
                "provisioning_session_id": resolved_provisioning_session_id,
                "provisioning_sandbox_generation": provisioning_sandbox_generation,
                "current_sandbox_id": None,
            },
            extra_updates={
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "last_error": last_error,
            },
        )

    async def mark_recovery_required(
        self,
        *,
        user_id: str,
        assistant_id: str,
        reason: str,
    ) -> bool:
        return await self.transition_state(
            user_id=user_id,
            assistant_id=assistant_id,
            expected_state=None,
            new_state="RECOVERY_REQUIRED",
            extra_updates={"last_error": reason},
        )
