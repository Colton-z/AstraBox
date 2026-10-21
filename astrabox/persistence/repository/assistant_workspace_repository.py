"""Assistant workspaces: which box an Assistant runs in, and who may change that.

A workspace belongs to the Assistant, not to each user: one row per Assistant,
keyed by its id. User isolation inside the shared box comes from per-user
profiles (``assistant_profiles``) and from Session ownership.

Every transition is one conditional ``UPDATE``. Its predicate names what the
transition depends on — the state, the box the pointer names, the Session
elected to build the next box and its startup generation — so a transition
computed from a row that has since moved changes nothing and returns
``False``. ``AssistantWorkspaceService`` documents what each one fences. The
transitions that retire a box delete its profile rows in the same transaction.
Confirmed releases also clear the conversations' bindings to that exact box.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    cast,
    and_,
    delete,
    exists,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.config.settings import get_settings, validate_execution_target
from astrabox.persistence.repository.assistant_catalog_repository import _ChildObject, _OrderedChildren
from astrabox.persistence.sql import execute, fetch_all, fetch_one, fetch_value, from_iso, to_iso, transaction
from astrabox.persistence.sql.statements import insert_if_absent, upsert_primary_key
from astrabox.persistence.sql.models.assistants import (
    WORKSPACE_STATES,
    Assistant,
    AssistantProfile,
    AssistantWorkspace,
)
from astrabox.persistence.sql.models.sessions import Session

__all__ = ["WORKSPACE_STATES", "AssistantWorkspaceRepository", "MaterializerClaim"]

_ASSISTANTS = Assistant.__table__
_WORKSPACES = AssistantWorkspace.__table__
_PROFILES = AssistantProfile.__table__
_SESSIONS = Session.__table__
_COLUMNS = _WORKSPACES.c

_SWEEP_PAGE_MAX = 500


def _deadline(epoch_seconds: Any) -> datetime:
    return datetime.fromtimestamp(float(epoch_seconds), tz=timezone.utc)


def _profiles_column() -> Any:
    """The workspace's complete profile rows for the public marker-key map."""
    return (
        select(_OrderedChildren(_ChildObject(
            "marker_key", cast(_PROFILES.c.marker_key, Text),
            "user_id", cast(_PROFILES.c.user_id, Text),
            "sandbox_id", cast(_PROFILES.c.sandbox_id, Text),
            "configuration_revision", cast(_PROFILES.c.configuration_revision, Text),
        ), _PROFILES.c.marker_key))
        .where(_PROFILES.c.assistant_id == _COLUMNS.assistant_id)
        .limit(1)
        .scalar_subquery()
        .label("assistant_profiles")
    )


def _view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A stored workspace as services read it."""
    if row is None:
        return None
    workspace: dict[str, Any] = {
        "assistant_id": row["assistant_id"],
        "execution_target": row["execution_target"],
        "created_by_user_id": row["created_by_user_id"],
        "engine_kind": row["engine_kind"],
        "state": row["state"],
        "current_sandbox_id": row["current_sandbox_id"],
        "current_sandbox_expires_at": to_iso(row["current_sandbox_expires_at"]),
        "runtime_identity": row["runtime_identity"],
        "provisioning_session_id": row["provisioning_session_id"],
        "provisioning_sandbox_generation": row["provisioning_sandbox_generation"],
        "last_error": row["last_error"],
        "hibernated_at": to_iso(row["hibernated_at"]),
        "post_commit_cleanup_pending": row["post_commit_cleanup_pending"],
        "profile_refresh": (
            {
                "revision": row["profile_refresh_revision"],
                "session_id": row["profile_refresh_session_id"],
                "started_at": to_iso(row["profile_refresh_started_at"]),
            }
            if row["profile_refresh_revision"] is not None
            else None
        ),
        "prepared_operation": (
            {
                "provider": row["prepared_operation_provider"],
                "operation_id": row["prepared_operation_id"],
                "deadline": row["prepared_operation_deadline"].timestamp(),
            }
            if row["prepared_operation_id"] is not None
            else None
        ),
        "created_at": to_iso(row["created_at"]),
        "updated_at": to_iso(row["updated_at"]),
    }
    if "assistant_profiles" in row:
        workspace["assistant_profiles"] = {
            item["marker_key"]: {key: item[key] for key in (
                "user_id", "sandbox_id", "configuration_revision",
            )} for item in row["assistant_profiles"] or []
        }
    return workspace


@dataclass(frozen=True)
class MaterializerClaim:
    """The materializer a transition expects the workspace still to be claimed by."""

    state: str
    provisioning_session_id: str
    provisioning_sandbox_generation: str | None

    def conditions(self) -> list[ColumnElement[bool]]:
        return [
            _COLUMNS.state == self.state,
            _COLUMNS.provisioning_session_id == self.provisioning_session_id,
            _COLUMNS.provisioning_sandbox_generation.is_not_distinct_from(
                self.provisioning_sandbox_generation
            ),
        ]


#: What clearing a retired box writes, besides the transition's own state.
_BOX_RETIRED = {
    "current_sandbox_id": None,
    "current_sandbox_expires_at": None,
    "runtime_identity": None,
    "provisioning_session_id": None,
    "provisioning_sandbox_generation": None,
    "post_commit_cleanup_pending": False,
}


class AssistantWorkspaceRepository:
    """One workspace row per Assistant, and the profiles prepared in its box."""

    def __init__(self, *, execution_target: str | None = None) -> None:
        self.execution_target = validate_execution_target(
            get_settings().execution_target if execution_target is None else execution_target
        )

    def _runtime_owner(self, assistant_id: str) -> ColumnElement[bool]:
        return and_(
            _COLUMNS.assistant_id == assistant_id,
            _COLUMNS.execution_target == self.execution_target,
        )

    # ── reads ────────────────────────────────────────────────────────────

    async def get_workspace(self, assistant_id: str) -> dict[str, Any] | None:
        """The Assistant's workspace with its profiles, or ``None``."""
        return _view(
            await fetch_one(
                select(_WORKSPACES, _profiles_column()).where(_COLUMNS.assistant_id == assistant_id)
            )
        )

    async def has_workspace(self, assistant_id: str) -> bool:
        return bool(
            await fetch_value(select(exists().where(_COLUMNS.assistant_id == assistant_id)))
        )

    async def list_workspaces_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        """Every workspace whose pointer names the box."""
        target = str(sandbox_id or "").strip()
        if not target:
            return []
        rows = await fetch_all(
            select(_WORKSPACES)
            .where(
                _COLUMNS.current_sandbox_id == target,
                _COLUMNS.execution_target == self.execution_target,
            )
            .order_by(_COLUMNS.assistant_id)
        )
        return [view for view in map(_view, rows) if view is not None]

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, after_assistant_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """One page of the workspaces naming a box whose lease is absent or past.

        Rows come in ``assistant_id`` order after ``after_assistant_id``: a probe
        that cannot settle a binding leaves its row in the set.
        """
        now = from_iso(now_iso)
        return await self._bound_page(
            [
                or_(
                    _COLUMNS.current_sandbox_expires_at.is_(None),
                    _COLUMNS.current_sandbox_expires_at <= now,
                )
            ],
            after=after_assistant_id,
            limit=limit,
        )

    async def list_ready_workspaces(
        self, *, now_iso: str, after_assistant_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """One page of READY workspaces whose box lease is still live.

        These are exactly the bindings :meth:`list_dead_binding_probe_candidates`
        leaves out, so the two never probe the same box in one tick.
        """
        return await self._bound_page(
            [_COLUMNS.state == "READY", _COLUMNS.current_sandbox_expires_at > from_iso(now_iso)],
            after=after_assistant_id,
            limit=limit,
        )

    async def list_lease_renewal_candidates(
        self,
        *,
        now_iso: str,
        renew_before_iso: str,
        after_assistant_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One page of READY workspaces whose live lease ends by ``renew_before_iso``.

        A lease already past ``now_iso`` is the dead-binding sweep's, not a
        renewal's: the box may be gone.
        """
        return await self._bound_page(
            [
                _COLUMNS.state == "READY",
                _COLUMNS.current_sandbox_expires_at > from_iso(now_iso),
                _COLUMNS.current_sandbox_expires_at <= from_iso(renew_before_iso),
            ],
            after=after_assistant_id,
            limit=limit,
        )

    async def _bound_page(
        self, conditions: Sequence[ColumnElement[bool]], *, after: str | None, limit: int
    ) -> list[dict[str, Any]]:
        statement: Select[Any] = select(_WORKSPACES).where(
            _COLUMNS.current_sandbox_id.is_not(None),
            _COLUMNS.execution_target == self.execution_target, *conditions,
        )
        after_id = str(after or "").strip()
        if after_id:
            statement = statement.where(_COLUMNS.assistant_id > after_id)
        rows = await fetch_all(
            statement.order_by(_COLUMNS.assistant_id).limit(
                max(1, min(int(limit or 50), _SWEEP_PAGE_MAX))
            )
        )
        return [view for view in map(_view, rows) if view is not None]

    # ── creation ─────────────────────────────────────────────────────────

    async def create_workspace(
        self,
        *,
        assistant_id: str,
        created_by_user_id: str,
        engine_kind: str,
        provisioning_session_id: str,
    ) -> dict[str, Any] | None:
        """Insert the Assistant's workspace, MATERIALIZING under ``provisioning_session_id``.

        ``None`` when the workspace already exists or the Assistant is not live.
        The Assistant row is share-locked for the insert, so a concurrent
        deletion either waits for it or makes it insert nothing.
        """
        async def create(conn: AsyncConnection) -> dict[str, Any] | None:
            live = await conn.scalar(
                select(_ASSISTANTS.c.assistant_id)
                .where(_ASSISTANTS.c.assistant_id == assistant_id, _ASSISTANTS.c.deleted.is_(False))
                .with_for_update(read=True)
            )
            if live is None:
                return None
            created = await insert_if_absent(conn, _WORKSPACES, {
                "assistant_id": assistant_id,
                "execution_target": self.execution_target,
                "created_by_user_id": created_by_user_id,
                "engine_kind": engine_kind,
                "state": "MATERIALIZING",
                "provisioning_session_id": provisioning_session_id,
            }, index_columns=["assistant_id"])
            if not created:
                return None
            row = (await conn.execute(
                select(_WORKSPACES).where(self._runtime_owner(assistant_id))
            )).mappings().one()
            return _view({**row, "assistant_profiles": None})

        return await transaction(create)

    # ── transitions ──────────────────────────────────────────────────────

    async def _transition(
        self,
        assistant_id: str,
        conditions: Sequence[ColumnElement[bool]],
        values: Mapping[str, Any],
        *,
        retire_profiles: bool = False,
        retired_sandbox_id: str | None = None,
    ) -> bool:
        """Apply ``values`` if the row still satisfies ``conditions``; whether it did."""
        moved = (
            update(_WORKSPACES)
            .where(self._runtime_owner(assistant_id), *conditions)
            .values(**values, updated_at=func.now())
        )
        if not retire_profiles and retired_sandbox_id is None:
            return await execute(moved) == 1

        async def retire(conn: AsyncConnection) -> bool:
            if (await conn.execute(moved)).rowcount != 1:
                return False
            if retire_profiles:
                await conn.execute(delete(_PROFILES).where(_PROFILES.c.assistant_id == assistant_id))
            if retired_sandbox_id is not None:
                await conn.execute(
                    update(_SESSIONS)
                    .where(
                        _SESSIONS.c.execution_target == self.execution_target,
                        _SESSIONS.c.workspace_kind == "assistant",
                        or_(
                            _SESSIONS.c.assistant_id == assistant_id,
                            _SESSIONS.c.owner_id == assistant_id,
                        ),
                        _SESSIONS.c.sandbox_id == retired_sandbox_id,
                    )
                    .values(sandbox_id=None, updated_at=func.now())
                )
            return True

        return await transaction(retire)

    async def record_lease(
        self, assistant_id: str, *, sandbox_id: str, expires_at: str | None
    ) -> bool:
        """Store a renewed lease while the pointer still names ``sandbox_id``."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.current_sandbox_id == sandbox_id],
            {"current_sandbox_expires_at": from_iso(expires_at)},
        )

    async def realign_lease(
        self,
        assistant_id: str,
        *,
        sandbox_id: str,
        observed_expires_at: str | None,
        expires_at: str | None,
    ) -> bool:
        """Store the provider's lease while neither the pointer nor the lease moved."""
        return await self._transition(
            assistant_id,
            [
                _COLUMNS.current_sandbox_id == sandbox_id,
                _COLUMNS.current_sandbox_expires_at.is_not_distinct_from(
                    from_iso(observed_expires_at)
                ),
            ],
            {"current_sandbox_expires_at": from_iso(expires_at)},
        )

    async def converge_dead_sandbox(
        self, assistant_id: str, *, observed_state: str, sandbox_id: str, last_error: str
    ) -> bool:
        """RECOVERY_REQUIRED with no box, while the row is still ``observed_state`` on the box."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.state == observed_state, _COLUMNS.current_sandbox_id == sandbox_id],
            {
                **_BOX_RETIRED,
                "state": "RECOVERY_REQUIRED",
                "hibernated_at": None,
                "last_error": last_error,
            },
            retire_profiles=True,
        )

    async def claim_materialization(
        self, assistant_id: str, *, expected_state: str, provisioning_session_id: str
    ) -> bool:
        """MATERIALIZING under the Session, while the row is ``expected_state`` and unclaimed."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.state == expected_state, _COLUMNS.provisioning_session_id.is_(None)],
            {
                "state": "MATERIALIZING",
                "provisioning_session_id": provisioning_session_id,
                "provisioning_sandbox_generation": None,
                "hibernated_at": None,
                "last_error": None,
            },
        )

    async def claim_materialization_generation(
        self,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        observed_generation: str | None,
        sandbox_generation: str,
    ) -> bool:
        """Qualify the claiming Session with its startup generation."""
        return await self._transition(
            assistant_id,
            MaterializerClaim(
                state="MATERIALIZING",
                provisioning_session_id=provisioning_session_id,
                provisioning_sandbox_generation=observed_generation,
            ).conditions(),
            {"provisioning_sandbox_generation": sandbox_generation},
        )

    async def reopen_materialization(self, assistant_id: str) -> bool:
        """MATERIALIZING again, for a RECOVERY_REQUIRED row with no box and no claim."""
        return await self._transition(
            assistant_id,
            [
                _COLUMNS.state == "RECOVERY_REQUIRED",
                _COLUMNS.current_sandbox_id.is_(None),
                _COLUMNS.provisioning_session_id.is_(None),
            ],
            {"state": "MATERIALIZING"},
        )

    async def claim_profile_refresh(
        self,
        assistant_id: str,
        *,
        sandbox_id: str,
        revision: str,
        session_id: str,
        started_at: datetime,
        retry_before: datetime,
    ) -> bool:
        """Elect ``session_id`` to prepare ``revision`` on the READY box.

        The same revision is claimed again only once the earlier claim started
        before ``retry_before``.
        """
        return await self._transition(
            assistant_id,
            [
                _COLUMNS.state == "READY",
                _COLUMNS.current_sandbox_id == sandbox_id,
                or_(
                    _COLUMNS.profile_refresh_revision.is_distinct_from(revision),
                    _COLUMNS.profile_refresh_started_at < retry_before,
                ),
            ],
            {
                "profile_refresh_revision": revision,
                "profile_refresh_session_id": session_id,
                "profile_refresh_started_at": started_at,
            },
        )

    async def mark_ready(
        self,
        assistant_id: str,
        *,
        provisioning_session_id: str,
        provisioning_sandbox_generation: str,
        sandbox_id: str,
        expires_at: str | None,
        runtime_identity: Mapping[str, Any] | None,
        marker_key: str,
        user_id: str,
        configuration_revision: str,
    ) -> bool:
        """Bind the started box, publish READY and the materializer's profile, in one transaction.

        Only the claiming Session at its generation can publish. A retry after
        a publication that already landed answers ``True`` without writing.
        """
        async def publish(conn: AsyncConnection) -> bool:
            ready = (
                update(_WORKSPACES)
                .where(
                    self._runtime_owner(assistant_id),
                    _COLUMNS.state == "MATERIALIZING",
                    _COLUMNS.provisioning_session_id == provisioning_session_id,
                    _COLUMNS.provisioning_sandbox_generation == provisioning_sandbox_generation,
                )
                .values(
                    state="READY",
                    current_sandbox_id=sandbox_id,
                    current_sandbox_expires_at=from_iso(expires_at),
                    hibernated_at=None,
                    last_error=None,
                    runtime_identity=dict(runtime_identity) if runtime_identity is not None else None,
                    provisioning_session_id=None,
                    provisioning_sandbox_generation=None,
                    updated_at=func.now(),
                )
            )
            if (await conn.execute(ready)).rowcount != 1:
                return False
            await self._profile_upsert(
                conn, assistant_id,
                marker_key=marker_key, user_id=user_id, sandbox_id=sandbox_id,
                configuration_revision=configuration_revision,
            )
            return True

        if await transaction(publish):
            return True
        return bool(
            await fetch_value(
                select(
                    exists().where(
                        self._runtime_owner(assistant_id),
                        _COLUMNS.state == "READY",
                        _COLUMNS.current_sandbox_id == sandbox_id,
                        _COLUMNS.provisioning_session_id.is_(None),
                        _COLUMNS.provisioning_sandbox_generation.is_(None),
                        _PROFILES.c.assistant_id == _COLUMNS.assistant_id,
                        _PROFILES.c.marker_key == marker_key,
                        _PROFILES.c.sandbox_id == sandbox_id,
                    )
                )
            )
        )

    async def mark_profile_ready(
        self,
        assistant_id: str,
        *,
        sandbox_id: str,
        marker_key: str,
        user_id: str,
        configuration_revision: str,
    ) -> bool:
        """Record a user's profile prepared in the box while the workspace is READY on it."""
        async def publish(conn: AsyncConnection) -> bool:
            current = await conn.scalar(
                select(_COLUMNS.assistant_id)
                .where(
                    self._runtime_owner(assistant_id),
                    _COLUMNS.state == "READY",
                    _COLUMNS.current_sandbox_id == sandbox_id,
                )
                .with_for_update(read=True)
            )
            if current is None:
                return False
            await self._profile_upsert(
                conn, assistant_id,
                marker_key=marker_key, user_id=user_id, sandbox_id=sandbox_id,
                configuration_revision=configuration_revision,
            )
            return True

        return await transaction(publish)

    @staticmethod
    async def _profile_upsert(
        conn: AsyncConnection,
        assistant_id: str,
        *,
        marker_key: str,
        user_id: str,
        sandbox_id: str,
        configuration_revision: str,
    ) -> None:
        """Store the profile while the caller holds the workspace's binding lock."""
        await conn.execute(upsert_primary_key(conn, _PROFILES, {
            "assistant_id": assistant_id, "marker_key": marker_key,
            "user_id": user_id, "sandbox_id": sandbox_id,
            "configuration_revision": configuration_revision,
        }, update_columns=["user_id", "sandbox_id", "configuration_revision"],
            update_values={"ready_at": func.now()}))

    async def mark_post_commit_failure(
        self,
        assistant_id: str,
        *,
        sandbox_id: str,
        cleanup_confirmed: bool,
        failure_phase: str,
    ) -> bool:
        """RECOVERY_REQUIRED for a failure after the box was bound.

        A confirmed cleanup clears the pointer; an unconfirmed one keeps it so
        a wake can retry destroying exactly that box. Either way the row must
        still name ``sandbox_id``, which stops a verdict about box A from
        clearing a pointer that has since moved to box B. A retry that finds
        the row already converged answers ``True``.
        """
        last_error = f"post_commit_{failure_phase}"
        values: dict[str, Any] = {
            "state": "RECOVERY_REQUIRED",
            "last_error": last_error,
            "post_commit_cleanup_pending": not cleanup_confirmed,
        }
        if cleanup_confirmed:
            values.update(current_sandbox_id=None, current_sandbox_expires_at=None)
        if await self._transition(
            assistant_id,
            [
                _COLUMNS.state.in_(("MATERIALIZING", "READY", "RECOVERY_REQUIRED")),
                _COLUMNS.current_sandbox_id == sandbox_id,
            ],
            values,
            retired_sandbox_id=sandbox_id if cleanup_confirmed else None,
        ):
            return True
        return bool(
            await fetch_value(
                select(
                    exists().where(
                        self._runtime_owner(assistant_id),
                        _COLUMNS.state == "RECOVERY_REQUIRED",
                        _COLUMNS.last_error == last_error,
                        _COLUMNS.post_commit_cleanup_pending.is_(not cleanup_confirmed),
                    )
                )
            )
        )

    async def begin_hibernation(
        self, assistant_id: str, *, sandbox_id: str, hibernated_at: str
    ) -> bool:
        """HIBERNATING, while READY on ``sandbox_id``."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.state == "READY", _COLUMNS.current_sandbox_id == sandbox_id],
            {"state": "HIBERNATING", "hibernated_at": from_iso(hibernated_at), "last_error": None},
        )

    async def mark_hibernation_release_required(self, assistant_id: str, *, sandbox_id: str) -> bool:
        """Record a completed native save while hibernating or recovering this box."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.state.in_(("HIBERNATING", "RECOVERY_REQUIRED")), _COLUMNS.current_sandbox_id == sandbox_id],
            {"state": "RECOVERY_REQUIRED", "post_commit_cleanup_pending": True, "last_error": None},
        )

    async def finish_hibernation(
        self, assistant_id: str, *, expected_state: str, destroyed_sandbox_id: str | None
    ) -> bool:
        """HIBERNATING with no box, while ``expected_state`` on ``destroyed_sandbox_id``."""
        return await self._transition(
            assistant_id,
            [
                _COLUMNS.state == expected_state,
                _COLUMNS.current_sandbox_id.is_not_distinct_from(destroyed_sandbox_id),
            ],
            {**_BOX_RETIRED, "state": "HIBERNATING", "last_error": None},
            retire_profiles=True,
            retired_sandbox_id=destroyed_sandbox_id,
        )

    async def adopt_undestroyed_sandbox(
        self,
        assistant_id: str,
        *,
        sandbox_id: str,
        reason: str,
        materializer: MaterializerClaim | None,
        runtime_identity: dict[str, Any] | None = None,
        state_save_required: bool = False,
    ) -> bool:
        """Name an unconfirmed box with its remaining save or destruction obligation."""
        conditions: list[ColumnElement[bool]] = [_COLUMNS.current_sandbox_id.is_(None)]
        if materializer is not None:
            conditions.extend(materializer.conditions())
        return await self._transition(
            assistant_id,
            conditions,
            {
                "state": "RECOVERY_REQUIRED",
                "current_sandbox_id": sandbox_id,
                "post_commit_cleanup_pending": not state_save_required,
                "last_error": reason,
                **({"runtime_identity": runtime_identity} if runtime_identity is not None else {}),
            },
        )

    async def release_recovered_sandbox(self, assistant_id: str, *, sandbox_id: str) -> bool:
        """Drop the pointer of a RECOVERY_REQUIRED row that still names ``sandbox_id``."""
        return await self._transition(
            assistant_id,
            [_COLUMNS.state == "RECOVERY_REQUIRED", _COLUMNS.current_sandbox_id == sandbox_id],
            {
                "current_sandbox_id": None,
                "current_sandbox_expires_at": None,
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "post_commit_cleanup_pending": False,
            },
            retired_sandbox_id=sandbox_id,
        )

    async def mark_materialization_failed(
        self, assistant_id: str, *, materializer: MaterializerClaim, last_error: str
    ) -> bool:
        """HIBERNATING with no claim, while the materializer still holds an unbound row."""
        return await self._transition(
            assistant_id,
            [*materializer.conditions(), _COLUMNS.current_sandbox_id.is_(None)],
            {
                "state": "HIBERNATING",
                "current_sandbox_expires_at": None,
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "last_error": last_error,
            },
        )

    async def claim_stalled_materialization(
        self, assistant_id: str, *, materializer: MaterializerClaim, last_error: str
    ) -> bool:
        """Release a dead materializer's claim on an unbound MATERIALIZING row."""
        return await self._transition(
            assistant_id,
            [*materializer.conditions(), _COLUMNS.current_sandbox_id.is_(None)],
            {
                "provisioning_session_id": None,
                "provisioning_sandbox_generation": None,
                "last_error": last_error,
            },
        )

    async def force_recovery_required(self, assistant_id: str, *, reason: str) -> bool:
        """RECOVERY_REQUIRED whatever the row holds; whether a workspace exists.

        Unconditional by design, for an owner's explicit delete or destroy: the
        box pointer and any claim are kept, so the destruction stays retryable
        and an in-flight materializer's own fenced writes fail afterwards.
        """
        return await self._transition(
            assistant_id, [], {"state": "RECOVERY_REQUIRED", "last_error": reason}
        )

    async def swap_prepared_operation(
        self,
        assistant_id: str,
        *,
        expected: Mapping[str, Any] | None,
        replacement: Mapping[str, Any] | None,
    ) -> bool:
        """Replace the in-flight resource synchronization if it is still ``expected``.

        An operation is ``{provider, operation_id, deadline}`` with ``deadline``
        in epoch seconds; ``None`` is no operation.
        """
        if expected is None:
            conditions: list[ColumnElement[bool]] = [_COLUMNS.prepared_operation_id.is_(None)]
        else:
            conditions = [
                _COLUMNS.prepared_operation_provider == str(expected["provider"]),
                _COLUMNS.prepared_operation_id == str(expected["operation_id"]),
                _COLUMNS.prepared_operation_deadline == _deadline(expected["deadline"]),
            ]
        values = (
            {
                "prepared_operation_provider": str(replacement["provider"]),
                "prepared_operation_id": str(replacement["operation_id"]),
                "prepared_operation_deadline": _deadline(replacement["deadline"]),
            }
            if replacement is not None
            else {
                "prepared_operation_provider": None,
                "prepared_operation_id": None,
                "prepared_operation_deadline": None,
            }
        )
        return await self._transition(assistant_id, conditions, values)
