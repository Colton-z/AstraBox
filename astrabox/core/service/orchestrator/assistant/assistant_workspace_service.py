"""Assistant workspace service - materialization, wake, hibernate.

Workspace lifetime is bound to the assistant runtime, not to each user. The
first call to ``materialize_workspace_if_absent`` inserts the Assistant's one
``assistant_workspaces`` row, keyed by the Assistant; later calls from any
user return the existing row.

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

Every transition below is one conditional write in
:class:`~astrabox.persistence.repository.assistant_workspace_repository.AssistantWorkspaceRepository`;
each method's docstring says which facts its condition names and why.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.time_utils import utcnow
from astrabox.core.service.orchestrator.engine.capabilities import (
    engine_allowed_for_session_kind,
)
from astrabox.persistence.repository.assistant_workspace_repository import (
    WORKSPACE_STATES,
    AssistantWorkspaceRepository,
    MaterializerClaim,
)
from astrabox.seams.sandbox_disposal import SandboxDestruction, may_sever_last_name

logger = get_logger(__name__)

_VALID_STATES = frozenset(WORKSPACE_STATES)


def assistant_profile_marker_key(*, user_id: str, assistant_id: str) -> str:
    profile_key = f"{str(user_id or '').strip()}:{str(assistant_id or '').strip()}"
    return hashlib.sha256(f"assistant-profile|{profile_key}".encode("utf-8")).hexdigest()


def _profile_marker_identity(
    *,
    user_id: str,
    assistant_id: str,
    sandbox_id: str,
    configuration_revision: str,
) -> tuple[str, str, str, str]:
    """The marker key, user, box and revision a ready profile is recorded under."""
    normalized_user_id = str(user_id or "").strip()
    normalized_assistant_id = str(assistant_id or "").strip()
    normalized_sandbox_id = str(sandbox_id or "").strip()
    normalized_revision = str(configuration_revision or "").strip()
    if (
        not normalized_user_id
        or not normalized_assistant_id
        or not normalized_sandbox_id
        or not normalized_revision
    ):
        raise APIError(
            code="ASSISTANT_PROFILE_MARKER_INVALID",
            message=(
                "assistant profile marker requires user_id, assistant_id, "
                "sandbox_id and the configuration revision it was prepared from"
            ),
            status_code=500,
        )
    marker_key = assistant_profile_marker_key(
        user_id=normalized_user_id,
        assistant_id=normalized_assistant_id,
    )
    return marker_key, normalized_user_id, normalized_sandbox_id, normalized_revision


def get_assistant_profile_ready_marker(
    workspace: dict[str, Any] | None,
    *,
    user_id: str,
    assistant_id: str,
    sandbox_id: str,
    configuration_revision: str | None,
) -> dict[str, Any] | None:
    """The user's profile prepared in ``sandbox_id`` from ``configuration_revision``, if any.

    A profile prepared from a different revision is not reused: the Agent
    program reads its profile once, at start, so a reused one keeps answering
    from the definition the owner replaced.
    """
    if not isinstance(workspace, dict):
        return None
    normalized_user_id = str(user_id or "").strip()
    normalized_assistant_id = str(assistant_id or "").strip()
    normalized_sandbox_id = str(sandbox_id or "").strip()
    revision = str(configuration_revision or "").strip()
    if not normalized_user_id or not normalized_assistant_id or not normalized_sandbox_id:
        return None
    if not revision:
        return None
    marker_key = assistant_profile_marker_key(
        user_id=normalized_user_id,
        assistant_id=normalized_assistant_id,
    )
    markers = workspace.get("assistant_profiles")
    marker = markers.get(marker_key) if isinstance(markers, dict) else None
    if (
        isinstance(marker, dict)
        and str(marker.get("user_id") or "").strip() == normalized_user_id
        and str(marker.get("sandbox_id") or "").strip() == normalized_sandbox_id
        and str(marker.get("configuration_revision") or "").strip() == revision
    ):
        return marker
    return None


def _materializer(owner: dict[str, Any]) -> MaterializerClaim:
    """The materializer claim named by a caller's ``state`` / session / generation triple."""
    return MaterializerClaim(
        state=str(owner["state"]),
        provisioning_session_id=str(owner["provisioning_session_id"]),
        provisioning_sandbox_generation=owner.get("provisioning_sandbox_generation"),
    )


class AssistantWorkspaceService:
    def __init__(self, *, workspace_repo: AssistantWorkspaceRepository | None = None) -> None:
        self._workspace_repo = workspace_repo or AssistantWorkspaceRepository()

    async def get_workspace(self, *, user_id: str, assistant_id: str) -> dict[str, Any] | None:
        """The Assistant's workspace; ``user_id`` names the caller, not the row."""
        _ = user_id
        return await self._workspace_repo.get_workspace(assistant_id)

    async def get_owned_workspace(self, *, user_id: str, assistant_id: str) -> dict[str, Any] | None:
        """Read a workspace for a physical operation in this execution target."""
        from astrabox.core.service.orchestrator.runtime.execution_target import require_execution_target

        workspace = await self.get_workspace(user_id=user_id, assistant_id=assistant_id)
        if workspace is not None:
            require_execution_target(workspace)
        return workspace

    async def has_assistant_workspace(self, *, assistant_id: str) -> bool:
        return await self._workspace_repo.has_workspace(assistant_id)

    async def list_lease_renewal_candidates(
        self,
        *,
        now_iso: str,
        renew_before_iso: str,
        after_assistant_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_lease_renewal_candidates(
            now_iso=now_iso,
            renew_before_iso=renew_before_iso,
            after_assistant_id=after_assistant_id,
            limit=limit,
        )

    async def list_ready_workspaces(
        self, *, now_iso: str, after_assistant_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_ready_workspaces(
            now_iso=now_iso, after_assistant_id=after_assistant_id, limit=limit
        )

    async def record_sandbox_lease(
        self, *, assistant_id: str, sandbox_id: str, expires_at: str | None
    ) -> bool:
        """Store a renewed lease, only while the workspace still holds ``sandbox_id``."""
        return await self._workspace_repo.record_lease(
            assistant_id, sandbox_id=sandbox_id, expires_at=expires_at
        )

    async def list_workspaces_by_sandbox_id(
        self, sandbox_id: str
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_workspaces_by_sandbox_id(sandbox_id)

    async def list_dead_binding_probe_candidates(
        self,
        *,
        now_iso: str,
        after_assistant_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._workspace_repo.list_dead_binding_probe_candidates(
            now_iso=now_iso,
            after_assistant_id=after_assistant_id,
            limit=limit,
        )

    async def realign_live_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        expires_at: str | None,
    ) -> bool:
        """Refresh a workspace whose binding and lease have not changed.

        The condition names the lease the caller read as well as the box, so a
        renewal published while the provider was being asked is not replaced
        by that stale answer.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        resolved_expires_at = str(expires_at or "").strip() or None
        assistant_id = str((workspace or {}).get("assistant_id") or "").strip()
        if not resolved_sandbox_id or not assistant_id:
            return False
        return await self._workspace_repo.realign_lease(
            assistant_id,
            sandbox_id=resolved_sandbox_id,
            observed_expires_at=workspace.get("current_sandbox_expires_at"),
            expires_at=resolved_expires_at,
        )

    async def converge_dead_sandbox(
        self,
        *,
        workspace: dict[str, Any],
        sandbox_id: str,
        last_error: str,
    ) -> bool:
        """Clear one confirmed-dead workspace pointer under an exact fence.

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
        return await self._workspace_repo.converge_dead_sandbox(
            assistant_id,
            observed_state=state,
            sandbox_id=resolved_sandbox_id,
            last_error=str(last_error or "sandbox terminated"),
        )

    async def materialize_workspace_if_absent(
        self,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
        provisioning_session_id: str,
    ) -> dict[str, Any]:
        """The Assistant's workspace, inserted MATERIALIZING under the Session when absent.

        The insert is keyed by the Assistant, so concurrent first calls create
        one row and every caller reads that one.
        """
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
        created = await self._workspace_repo.create_workspace(
            assistant_id=assistant_id,
            created_by_user_id=user_id,
            engine_kind=engine_kind,
            provisioning_session_id=resolved_provisioning_session_id,
        )
        if created is not None:
            return created
        existing = await self.get_owned_workspace(user_id=user_id, assistant_id=assistant_id)
        if existing is None:
            # Nothing was inserted and nothing exists: the Assistant was
            # deleted after the caller resolved it.
            raise APIError(
                code="ASSISTANT_NOT_FOUND",
                message=f"assistant={assistant_id} not found",
                status_code=404,
            )
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

    async def claim_materialization(
        self,
        *,
        user_id: str,
        assistant_id: str,
        expected_state: str,
        provisioning_session_id: str,
    ) -> bool:
        """Give exactly one Session the right to create the workspace sandbox.

        The condition is the state the caller read and an empty claim, so of
        concurrent acquirers exactly one moves the workspace to MATERIALIZING.
        """

        resolved_session_id = str(provisioning_session_id or "").strip()
        if not resolved_session_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message="claiming materialization requires a materializer Session id",
                status_code=500,
            )
        if expected_state not in _VALID_STATES:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_STATE",
                message=f"unknown state={expected_state!r}",
                status_code=400,
            )
        return await self._workspace_repo.claim_materialization(
            assistant_id,
            expected_state=expected_state,
            provisioning_session_id=resolved_session_id,
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
        return await self._workspace_repo.claim_materialization_generation(
            assistant_id,
            provisioning_session_id=provisioning_session_id,
            observed_generation=observed_generation,
            sandbox_generation=sandbox_generation,
        )

    async def reopen_materialization(self, *, user_id: str, assistant_id: str) -> bool:
        """MATERIALIZING again after a stalled materializer's box was proven destroyed.

        The condition is a RECOVERY_REQUIRED row with no box and no claim, so
        the reopened workspace can be claimed by exactly one acquirer.
        """
        return await self._workspace_repo.reopen_materialization(assistant_id)

    async def claim_profile_refresh(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        revision: str,
        session_id: str,
        retry_before: datetime,
    ) -> bool:
        """Elect one workspace Session to prepare ``revision`` on the READY box.

        A refresh of the same revision is claimed again only once the earlier
        one has had until ``retry_before`` to publish its profile.
        """
        return await self._workspace_repo.claim_profile_refresh(
            assistant_id,
            sandbox_id=sandbox_id,
            revision=revision,
            session_id=session_id,
            started_at=utcnow(),
            retry_before=retry_before,
        )

    async def mark_ready(
        self,
        *,
        user_id: str,
        assistant_id: str,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str | None,
        sandbox_id: str,
        configuration_revision: str,
        expires_at: str | None = None,
        runtime_identity: dict[str, Any] | None = None,
    ) -> bool:
        """Bind the started sandbox onto the workspace and publish READY.

        This is the single writer of a built box onto ``current_sandbox_id``:
        the caller has already created and started the sandbox, so this
        transition both records that pointer and moves MATERIALIZING to READY,
        fenced on the materializer Session and its startup generation. A
        retried startup whose publication already landed succeeds.
        """
        normalized_sandbox_id = str(sandbox_id or "").strip()
        if not normalized_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=f"workspace ready requires sandbox_id user={user_id} assistant={assistant_id}",
                status_code=500,
            )
        resolved_session_id = str(provisioning_session_id or "").strip()
        if not resolved_session_id or not provisioning_sandbox_generation:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_PROVISIONING_SESSION",
                message=(
                    "publishing READY requires the materializer Session and its "
                    f"startup generation user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        marker_key, marker_user, _, revision = _profile_marker_identity(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=normalized_sandbox_id,
            configuration_revision=configuration_revision,
        )
        marked = await self._workspace_repo.mark_ready(
            assistant_id,
            provisioning_session_id=resolved_session_id,
            provisioning_sandbox_generation=provisioning_sandbox_generation,
            sandbox_id=normalized_sandbox_id,
            expires_at=expires_at,
            runtime_identity=runtime_identity,
            marker_key=marker_key,
            user_id=marker_user,
            configuration_revision=revision,
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
        resolved_phase = str(failure_phase or "").strip().lower()
        if not sandbox_id:
            return False
        if not resolved_phase:
            raise ValueError("failure_phase is required")
        return await self._workspace_repo.mark_post_commit_failure(
            assistant_id,
            sandbox_id=sandbox_id,
            cleanup_confirmed=may_sever_last_name(cleanup, sandbox_id=sandbox_id),
            failure_phase=resolved_phase,
        )

    async def mark_assistant_profile_ready(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        configuration_revision: str,
    ) -> bool:
        """Record a user's profile prepared on the READY box, while it is still that box."""
        marker_key, marker_user, normalized_sandbox_id, revision = _profile_marker_identity(
            user_id=user_id,
            assistant_id=assistant_id,
            sandbox_id=sandbox_id,
            configuration_revision=configuration_revision,
        )
        return await self._workspace_repo.mark_profile_ready(
            str(assistant_id).strip(),
            sandbox_id=normalized_sandbox_id,
            marker_key=marker_key,
            user_id=marker_user,
            configuration_revision=revision,
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
        return await self._workspace_repo.begin_hibernation(
            assistant_id, sandbox_id=resolved_sandbox_id, hibernated_at=resolved_hibernated_at
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
        return await self._workspace_repo.mark_hibernation_release_required(
            assistant_id, sandbox_id=resolved_sandbox_id
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
        return await self._workspace_repo.finish_hibernation(
            assistant_id,
            expected_state=expected_state,
            destroyed_sandbox_id=resolved_sandbox_id,
        )

    async def adopt_undestroyed_sandbox(
        self,
        *,
        user_id: str,
        assistant_id: str,
        sandbox_id: str,
        reason: str,
        expected_owner: dict[str, Any] | None = None,
        runtime_identity: dict[str, Any] | None = None,
        state_save_required: bool = False,
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
        RECOVERY_REQUIRED. An unconfirmed native save keeps cleanup uncommitted
        and retains the private runtime identity; wake must complete that
        barrier before destruction. Otherwise only confirmed destruction is
        still pending.

        The condition is the pointer still being empty, and with
        ``expected_owner`` (``state``, ``provisioning_session_id``,
        ``provisioning_sandbox_generation``) that materializer still holding
        the claim: if a rebuild has bound a sandbox meanwhile, that pointer is
        a live box's name and this one must not overwrite it — the id then
        remains only in the caller's error.
        """
        resolved_sandbox_id = str(sandbox_id or "").strip()
        if state_save_required and not runtime_identity:
            raise ValueError("retaining native state requires its runtime identity")
        if not resolved_sandbox_id:
            raise APIError(
                code="ASSISTANT_WORKSPACE_INVALID_SANDBOX",
                message=(
                    "adopting an undestroyed sandbox requires its id "
                    f"user={user_id} assistant={assistant_id}"
                ),
                status_code=500,
            )
        return await self._workspace_repo.adopt_undestroyed_sandbox(
            assistant_id,
            sandbox_id=resolved_sandbox_id,
            reason=reason,
            materializer=_materializer(expected_owner) if expected_owner else None,
            runtime_identity=runtime_identity,
            state_save_required=state_save_required,
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

        The condition is RECOVERY_REQUIRED **and the pointer still naming
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
        return await self._workspace_repo.release_recovered_sandbox(
            assistant_id, sandbox_id=resolved_sandbox_id
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

        The condition is an unchanged MATERIALIZING workspace held by this
        materializer at this generation with no box; it moves to HIBERNATING
        and records the failure, leaving a concurrent wake untouched.
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
        return await self._workspace_repo.mark_materialization_failed(
            assistant_id,
            materializer=MaterializerClaim(
                state="MATERIALIZING",
                provisioning_session_id=provisioning_session_id,
                provisioning_sandbox_generation=provisioning_sandbox_generation,
            ),
            last_error=f"materialization_{resolved_phase}",
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

        The condition names both facts about the row that this transition
        depends on: it still points at that dead session, and it still holds no
        sandbox. Those make the claim atomic — exactly one concurrent wake
        takes the rebuild and the losers keep reporting MATERIALIZING.

        What an empty ``current_sandbox_id`` does not establish is that the dead
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
        return await self._workspace_repo.claim_stalled_materialization(
            assistant_id,
            materializer=MaterializerClaim(
                state="MATERIALIZING",
                provisioning_session_id=resolved_provisioning_session_id,
                provisioning_sandbox_generation=provisioning_sandbox_generation,
            ),
            last_error=last_error,
        )

    async def mark_recovery_required(
        self,
        *,
        user_id: str,
        assistant_id: str,
        reason: str,
    ) -> bool:
        """Put the workspace in RECOVERY_REQUIRED whatever it holds.

        Unconditional, for an owner who deletes the Assistant or destroys its
        workspace: the box pointer and any materializer claim are kept, so the
        destruction that follows stays retryable and an in-flight
        materializer's own fenced writes fail. ``False`` when the Assistant has
        no workspace.
        """
        return await self._workspace_repo.force_recovery_required(assistant_id, reason=reason)
