"""SessionReadRenderingMixin — read/GET projection surface for
:class:`SessionKernelService`.

List/get/messages read paths, snapshot->DTO rendering, UI-state
derivation, the agent-runtime overlay, active-turn overlay build,
delivery_failure synthesis, and the session-state guard
(``_require_turn_eligible``) that every write
mixin calls via ``self``.
"""
from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.user_context import UserContext
from astrabox.core.model import (
    AgentState,
    SessionState,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    require_session_kind,
    resolve_session_capabilities,
)
from astrabox.core.service.orchestrator.engine.registry import (
    EngineKindNotRegistered,
)
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.history_blocks import (
    HistoryBlock,
    decode_block_cursor,
    encode_block_cursor,
    preceding_user_text,
    process_summary_input,
    project_history_blocks,
    project_record,
)
from astrabox.core.service.orchestrator.session_kernel.active_turn_projection import (
    build_active_engine_fifo_messages,
    build_active_turn_message,
)
from astrabox.core.service.orchestrator.session_kernel.conversation_recovery import (
    coerce_int as _coerce_int,
    needs_turn_recovery as _needs_recovery,
    normalize_live_source_cursor,
)
from astrabox.core.service.orchestrator.session_kernel.service_mixins._helpers import (
    _ACTIVE_CONVERSATION_SNAPSHOT_STATES,
    _ACTIVE_SESSION_STATES,
    _completed_turn_has_terminal_frame_proof,
)


logger = get_logger(__name__)


def _installed_session_capabilities(session: dict[str, Any] | None) -> Any | None:
    """Read an installed adapter declaration without poisoning history reads.

    Durable Sessions outlive a third-party engine installation. Their explicit
    ``engine_kind`` remains authoritative, but a GET must still let an owner
    inspect or delete the Session after that plugin is removed. Active control
    paths use the strict resolver and continue to refuse an unavailable engine.
    """

    try:
        return resolve_session_capabilities(session)
    except EngineKindNotRegistered:
        return None


_SESSION_LIST_ROW_PROJECTION: dict[str, int] = {
    "_id": 0,
    "session_id": 1,
    "user_id": 1,
    "template_name": 1,
    "state": 1,
    "permission_mode": 1,
    "sandbox_id": 1,
    "engine_session_key": 1,
    "terminal_cwd": 1,
    "title": 1,
    "source_type": 1,
    "agent_id": 1,
    "deployment_name": 1,
    "session_kind": 1,
    "engine_kind": 1,
    "expires_at": 1,
    "created_at": 1,
    "updated_at": 1,
    "runtime_unavailable": 1,
    "last_error": 1,
    "startup_progress": 1,
    "recovery_policy": 1,
    "recovery_reason": 1,
    "workspace_ref": 1,
}

_SESSION_LIST_SNAPSHOT_PROJECTION: dict[str, int] = {
    "_id": 0,
    "session_id": 1,
    "session_lifecycle_state": 1,
    "runtime_connectivity_state": 1,
    "conversation_state": 1,
    "terminal_state": 1,
    "terminal_cwd": 1,
    "permission_mode": 1,
    "current_turn_id": 1,
    "last_turn_id": 1,
    "last_turn_status": 1,
    "last_turn_error": 1,
    "last_turn_command_id": 1,
    "last_turn_terminal_frame": 1,
    "delivery_state": 1,
    "last_turn_failure_phase": 1,
    "last_turn_terminal_reason": 1,
    "active_interaction_id": 1,
    "agent_binding": 1,
    "current_turn_remote_anchor": 1,
    "turn_recovery_phase": 1,
}

_SESSION_LIST_AGENT_PROJECTION: dict[str, int] = {
    "_id": 0,
    "agent_id": 1,
    "name": 1,
    "state": 1,
    "sandbox_id": 1,
    "expires_at": 1,
    "startup_progress": 1,
    "last_error": 1,
}

_SESSION_LIST_SUMMARY_FIELDS = frozenset(
    {
        "session_id",
        "user_id",
        "template_name",
        "state",
        "permission_mode",
        "sandbox_id",
        "title",
        "source_type",
        "agent_id",
        "deployment_name",
        "agent_runtime",
        "session_kind",
        "engine_kind",
        "expires_at",
        "created_at",
        "updated_at",
        "runtime_unavailable",
        "last_error",
        "startup_progress",
        "current_turn_id",
        "last_turn_id",
        "last_turn_status",
        "last_turn_error",
        "last_turn_command_id",
        "delivery_state",
        "last_turn_failure_phase",
        "last_turn_terminal_reason",
        "background_task_state",
        "runtime_warning",
        "recovery_policy",
        "recovery_reason",
        "deleted",
    }
)


class SessionReadRenderingMixin:
    """Read/GET projection surface for :class:`SessionKernelService`."""

    async def list_sessions(self, user: UserContext) -> list[dict[str, Any]]:
        rows = await self._sessions_repo.list_user_sessions(user.user_id)
        return await self._render_session_rows(rows)

    async def list_sessions_page(
        self,
        user: UserContext,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        page = await self._sessions_repo.list_user_sessions_page(
            user.user_id,
            limit=limit,
            cursor=cursor,
            projection=_SESSION_LIST_ROW_PROJECTION,
        )
        rows = list(page.get("sessions") or [])
        rendered = await self._render_session_rows(rows)
        return {
            "sessions": [self._summarize_session_list_row(row) for row in rendered],
            "has_more": bool(page.get("has_more")),
            "next_cursor": page.get("next_cursor"),
        }

    @staticmethod
    def _summarize_session_list_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            field: row[field]
            for field in _SESSION_LIST_SUMMARY_FIELDS
            if field in row
        }

    async def _render_session_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if rows:
            # Read path (list): overlay binding in-memory only. A GET must not
            # write to the database and, for agent_chat, persistence could
            # clobber a historical session's sandbox_id with the agent's transient
            # None during a hibernate window.
            rows = [await self._reconcile_runtime_binding(row, persist=False) for row in rows]
        session_ids_for_snapshots = [
            str(r.get("session_id") or "")
            for r in rows
            if str(r.get("session_id") or "").strip()
        ]
        agent_runtime_map = await self._load_agent_runtime_map(rows)
        snapshots_map = (
            await self._session_snapshots_repo.get_snapshots_batch(
                session_ids_for_snapshots,
                projection=_SESSION_LIST_SNAPSHOT_PROJECTION,
            )
            if session_ids_for_snapshots
            else {}
        )
        rendered: list[dict[str, Any]] = []
        for row in rows:
            sid = str(row.get("session_id") or "")
            snapshot = await self._converge_lifecycle_projection(
                session=row,
                snapshot=snapshots_map.get(sid),
            )
            rendered_row = self._apply_agent_runtime_overlay(
                self._render_projection_backed_session(
                    row,
                    snapshot=snapshot,
                    pending_interaction=None,
                ),
                agent_runtime_map=agent_runtime_map,
            )
            # The registry query and snapshot batch are separate reads. A
            # deletion between them appears only in the later snapshot, so the
            # rendered visibility boundary must honor that later observation.
            if rendered_row.get("deleted") is True:
                continue
            rendered.append(rendered_row)
        return rendered

    async def _load_agent_runtime_map(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        agent_ids = sorted(
            {
                str(row.get("agent_id") or "").strip()
                for row in rows
                if str(row.get("session_kind") or "").strip() == "agent_chat"
                and str(row.get("agent_id") or "").strip()
            }
        )
        if not agent_ids:
            return {}
        return await self._agent_repo.list_agents_by_ids(
            agent_ids,
            projection=_SESSION_LIST_AGENT_PROJECTION,
        )

    @staticmethod
    def _build_agent_runtime_view(agent_id: str, agent: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(agent, dict):
            return {
                "agent_id": agent_id,
                "state": AgentState.DELETED.value,
                "sandbox_id": None,
                "expires_at": None,
                "startup_progress": None,
                "last_error": None,
                "runtime_unavailable": False,
            }
        state = str(agent.get("state") or "").strip() or None
        sandbox_id = str(agent.get("sandbox_id") or "").strip() or None
        # Agent availability is the catalogue state. Environments using Agent
        # tenancy may also record the shared physical sandbox here; conversation
        # tenancy leaves it empty.
        active = state == AgentState.ACTIVE.value
        provisioning = state == AgentState.PROVISIONING.value
        return {
            "agent_id": agent_id,
            "state": state,
            "sandbox_id": sandbox_id if active else None,
            "expires_at": (str(agent.get("expires_at") or "").strip() or None) if active else None,
            "startup_progress": (
                str(agent.get("startup_progress") or "").strip() or "creating_sandbox"
                if provisioning
                else None
            ),
            "last_error": str(agent.get("last_error") or "").strip() or None,
            "runtime_unavailable": not active and not provisioning,
        }

    def _apply_agent_runtime_overlay(
        self,
        session: dict[str, Any],
        *,
        agent_runtime_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        clean = dict(session)
        if str(clean.get("session_kind") or "").strip() != "agent_chat":
            return clean
        agent_id = str(clean.get("agent_id") or "").strip()
        if not agent_id:
            return clean
        agent = agent_runtime_map.get(agent_id)
        runtime = self._build_agent_runtime_view(agent_id, agent)
        clean["agent_runtime"] = runtime
        if isinstance(agent, dict):
            deployment_name = str(agent.get("name") or "").strip()
            if deployment_name:
                clean["deployment_name"] = deployment_name
        return clean

    async def get_session(
        self,
        user: UserContext,
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_session = (
            dict(session)
            if isinstance(session, dict)
            else await self._must_get_owned_session(user, session_id)
        )
        base_session = await self._touch_projection_session(
            session_id,
            session=base_session,
        )
        # Read path (GET /sessions/{id}): overlay binding in-memory only. The list
        # renderer documents why persisting during a hibernate window is unsafe.
        base_session = await self._reconcile_runtime_binding(base_session, persist=False)
        effective_session_id = str(base_session.get("session_id") or session_id)
        snapshot = await self._converge_lifecycle_projection(
            session=base_session,
            snapshot=await self._get_kernel_session_snapshot(effective_session_id),
        )
        pending_interaction = await self._get_pending_interaction(
            effective_session_id,
            snapshot=snapshot,
        )
        background_task_state = await self._get_background_task_state(
            effective_session_id,
        )
        rendered = self._render_projection_backed_session(
            base_session,
            snapshot=snapshot,
            pending_interaction=pending_interaction,
            background_task_state=background_task_state,
        )
        rendered = self._apply_agent_runtime_overlay(
            rendered,
            agent_runtime_map=await self._load_agent_runtime_map([base_session]),
        )
        delivery_failure = await self._build_delivery_failure(
            effective_session_id,
            snapshot=snapshot,
        )
        rendered["delivery_failure"] = delivery_failure
        rendered["pending_interaction"] = pending_interaction
        rendered["pending_inputs"] = [
            {
                "command_id": row["command_id"],
                "input_id": row["input_id"],
                "client_message_id": row["client_message_id"],
                "content": row["content"],
                "sequence": row["sequence"],
                "status": str(row["state"]).lower(),
            }
            for row in await self._turn_service.pending_input_rows(
                effective_session_id
            )
        ]
        # Contract: session detail does not include messages or has_more_messages.
        return rendered

    async def get_messages(
        self,
        user: UserContext,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_session = dict(session) if isinstance(session, dict) else await self._must_get_owned_session(user, session_id)
        base_session = await self._touch_projection_session(
            session_id,
            session=base_session,
        )
        # Read path (GET /sessions/{id}/messages): overlay binding in-memory only,
        # for the same hibernate-window reason as _render_session_rows.
        base_session = await self._reconcile_runtime_binding(base_session, persist=False)
        effective_session_id = str(base_session.get("session_id") or session_id)
        snapshot = await self._get_kernel_session_snapshot(effective_session_id)

        # The messages array is durable-only; live state has a separate overlay.
        messages, has_more = await self._get_messages_page(
            effective_session_id,
            limit=limit,
            before=before,
        )
        visible = [self._session_service._sanitize_message(item) for item in messages]

        # The active-turn overlay belongs only on the first page and only while
        # conversation_state is active.
        active_turn_overlay: dict[str, Any] | None = None
        pending_interaction: dict[str, Any] | None = None
        if before is None:
            pending_interaction = await self._get_pending_interaction(
                effective_session_id,
                snapshot=snapshot,
            )
            active_turn_id = self._resolve_active_turn_id_for_messages(
                session=base_session,
                snapshot=snapshot,
                pending_interaction=pending_interaction,
            )
            if active_turn_id:
                overlay_message = await self._build_active_turn_overlay_message(
                    effective_session_id,
                    turn_id=active_turn_id,
                    rows=messages,
                    session=base_session,
                    snapshot=snapshot,
                )
                if isinstance(overlay_message, dict):
                    fifo_messages = overlay_message.pop("__engine_fifo_messages", None)
                    sanitized = self._session_service._sanitize_message(overlay_message)
                    watermark = overlay_message.get("source_frame_seq_applied")
                    active_turn_overlay = {
                        "turn_id": active_turn_id,
                        "message": sanitized,
                        "resume_cursor": {
                            "turn_id": active_turn_id,
                            "frame_seq": int(watermark) if watermark is not None else None,
                        },
                    }
                    live_source_cursor = normalize_live_source_cursor(
                        overlay_message.get("live_source_cursor")
                    )
                    if isinstance(live_source_cursor, dict):
                        active_turn_overlay["live_source_cursor"] = live_source_cursor
                    if isinstance(fifo_messages, list):
                        active_turn_overlay["messages"] = [
                            self._session_service._sanitize_message(message)
                            for message in fifo_messages
                            if isinstance(message, dict)
                        ]

        # Where the session's frames currently end. A client opening the
        # output channel with no turn in flight has nothing else to pass as a
        # cursor, and "from the beginning" would replay the whole history.
        session_frame_seq = None
        if before is None:
            session_frame_seq = await self._session_events_repo.get_max_session_frame_seq(
                effective_session_id
            )
            # Read authority once more after the overlay and cursor. An
            # interaction still active at that boundary must be visible here;
            # one committed later will arrive in the stream suffix.
            pending_interaction = await self._get_pending_interaction(
                effective_session_id,
            )

        return {
            "messages": visible,
            "has_more": has_more,
            "active_turn_overlay": active_turn_overlay,
            "session_frame_seq": session_frame_seq,
            "pending_interaction": pending_interaction,
        }

    async def get_history_blocks(
        self,
        user: UserContext,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return one page of the transcript with settled work folded away.

        This is ``get_messages`` with a different pagination unit and a
        different record shape: the same durable records, the same live-turn
        overlay on the first page, but each settled response's tool work
        replaced by a ``process_block`` the reader opens through
        ``get_history_block_details``. ``before`` is a cursor this method
        issued, which pins every page of one read to the same history.
        """

        base_session = dict(session) if isinstance(session, dict) else await self._must_get_owned_session(user, session_id)
        base_session = await self._touch_projection_session(
            session_id,
            session=base_session,
        )
        # Read path: overlay binding in-memory only, for the same
        # hibernate-window reason as _render_session_rows.
        base_session = await self._reconcile_runtime_binding(base_session, persist=False)
        effective_session_id = str(base_session.get("session_id") or session_id)
        snapshot = await self._get_kernel_session_snapshot(effective_session_id)

        through_seq: int | None = None
        before_block_id: str | None = None
        if before is not None:
            through_seq, before_block_id = self._decode_history_cursor(before)
        try:
            page = await self._message_view.list_history_blocks_page(
                effective_session_id,
                limit=limit,
                through_seq=through_seq,
                before_block_id=before_block_id,
            )
        except ValueError as exc:
            raise APIError(
                code="INVALID_REQUEST",
                message=str(exc),
                status_code=400,
            ) from exc

        records = page["records"]
        checkpoint = int(page["through_seq"])
        blocks = project_history_blocks(
            records,
            encode_block_cursor(checkpoint, None),
        )
        await self._attach_process_summaries(
            effective_session_id,
            blocks,
            through_seq=checkpoint,
        )
        visible = [
            self._session_service._sanitize_message(block.record) for block in blocks
        ]

        # The active-turn overlay belongs only on the first page and only while
        # conversation_state is active.
        active_turn_overlay: dict[str, Any] | None = None
        pending_interaction: dict[str, Any] | None = None
        if before is None:
            pending_interaction = await self._get_pending_interaction(
                effective_session_id,
                snapshot=snapshot,
            )
            active_turn_id = self._resolve_active_turn_id_for_messages(
                session=base_session,
                snapshot=snapshot,
                pending_interaction=pending_interaction,
            )
            if active_turn_id:
                overlay_message = await self._build_active_turn_overlay_message(
                    effective_session_id,
                    turn_id=active_turn_id,
                    rows=records,
                    session=base_session,
                    snapshot=snapshot,
                )
                if isinstance(overlay_message, dict):
                    fifo_messages = overlay_message.pop("__engine_fifo_messages", None)
                    sanitized = self._session_service._sanitize_message(overlay_message)
                    watermark = overlay_message.get("source_frame_seq_applied")
                    active_turn_overlay = {
                        "turn_id": active_turn_id,
                        "message": sanitized,
                        "resume_cursor": {
                            "turn_id": active_turn_id,
                            "frame_seq": int(watermark) if watermark is not None else None,
                        },
                    }
                    live_source_cursor = normalize_live_source_cursor(
                        overlay_message.get("live_source_cursor")
                    )
                    if isinstance(live_source_cursor, dict):
                        active_turn_overlay["live_source_cursor"] = live_source_cursor
                    if isinstance(fifo_messages, list):
                        active_turn_overlay["messages"] = [
                            self._session_service._sanitize_message(message)
                            for message in fifo_messages
                            if isinstance(message, dict)
                        ]

        session_frame_seq = None
        if before is None:
            session_frame_seq = await self._session_events_repo.get_max_session_frame_seq(
                effective_session_id
            )
            # Read authority once more after the overlay and cursor. An
            # interaction still active at that boundary must be visible here;
            # one committed later will arrive in the stream suffix.
            pending_interaction = await self._get_pending_interaction(
                effective_session_id,
            )

        next_before = page["next_before"]
        return {
            "messages": visible,
            "has_more": bool(page["has_more"]),
            "active_turn_overlay": active_turn_overlay,
            "session_frame_seq": session_frame_seq,
            "pending_interaction": pending_interaction,
            "paging_mode": "blocks",
            "next_cursor": (
                encode_block_cursor(checkpoint, next_before) if next_before else None
            ),
            "block_count": len(visible),
        }

    async def get_history_block_details(
        self,
        user: UserContext,
        session_id: str,
        *,
        block_id: str,
        cursor: str,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the blocks one ``process_block`` header stands for.

        ``cursor`` is the checkpoint the header was issued with, so the reply
        is what that page folded rather than what the record holds after later
        turns. A header the pinned record does not produce is a 404, not an
        empty list: the two mean different things to a reader who is waiting
        for the work to appear.
        """

        base_session = dict(session) if isinstance(session, dict) else await self._must_get_owned_session(user, session_id)
        effective_session_id = str(base_session.get("session_id") or session_id)
        through_seq, before_block_id = self._decode_history_cursor(cursor)
        if before_block_id is not None:
            raise APIError(
                code="INVALID_REQUEST",
                message="block details need a checkpoint cursor, not a page cursor",
                status_code=400,
            )
        message_id = str(block_id).rsplit(":p", 1)[0]
        record = await self._message_view.get_history_block_record(
            effective_session_id,
            message_id=message_id,
            through_seq=through_seq,
        )
        details = project_record(record).details.get(block_id) if isinstance(record, dict) else None
        if not isinstance(record, dict) or not details:
            raise APIError(
                code="NOT_FOUND",
                message="process block not found at this checkpoint",
                status_code=404,
            )
        message = self._session_service._sanitize_message(
            {
                **record,
                "blocks": details,
                "history_block_id": block_id,
            }
        )
        return {"messages": [message], "has_more": False}

    async def generate_process_summary(
        self,
        user: UserContext,
        session_id: str,
        message_id: str,
        *,
        retry_failed: bool = False,
        session: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write, or hand back, the label for one response's folded work.

        A response whose page shows every block in full has nothing to name and
        is refused, so a caller cannot mint a label for work the reader can
        already see. ``retry_failed`` reopens a row whose model call failed;
        a completed label is returned as it stands.
        """

        base_session = dict(session) if isinstance(session, dict) else await self._must_get_owned_session(user, session_id)
        effective_session_id = str(base_session.get("session_id") or session_id)
        title_service = self._session_title_service
        if title_service is None:
            raise APIError(
                code="INVALID_REQUEST",
                message="this deployment generates no process summaries",
                status_code=400,
            )

        existing = await title_service.read_process_summaries(
            effective_session_id,
            [message_id],
        )
        row = existing.get(message_id) if isinstance(existing, dict) else None
        if isinstance(row, dict) and row:
            if not (retry_failed and str(row.get("status") or "") == "failed"):
                return dict(row)

        record = await self._message_view.get_message(effective_session_id, message_id)
        if not isinstance(record, dict) or str(record.get("role") or "").strip() != "assistant":
            raise APIError(
                code="INVALID_REQUEST",
                message="a process summary describes an assistant response",
                status_code=400,
            )
        summary_input = process_summary_input(record)
        if summary_input is None:
            raise APIError(
                code="INVALID_REQUEST",
                message="response has no completed or interrupted tool process",
                status_code=400,
            )
        turn_id = str(record.get("turn_id") or "").strip()
        turn_messages = (
            await self._message_view._messages_for_turn(
                effective_session_id,
                turn_id=turn_id,
            )
            if turn_id
            else []
        )
        return await title_service.generate_process_summary(
            session_id=effective_session_id,
            message_id=message_id,
            user_text=preceding_user_text(turn_messages, message_id),
            process_text=summary_input.process_text,
            through_seq=await self._message_view.history_checkpoint_seq(
                effective_session_id
            ),
            turn_completed=summary_input.turn_completed,
            retry_failed=retry_failed,
        )

    @staticmethod
    def _decode_history_cursor(value: str) -> tuple[int, str | None]:
        try:
            return decode_block_cursor(value)
        except ValueError as exc:
            raise APIError(
                code="INVALID_REQUEST",
                message=str(exc),
                status_code=400,
            ) from exc

    async def _attach_process_summaries(
        self,
        session_id: str,
        blocks: list[HistoryBlock],
        *,
        through_seq: int,
    ) -> None:
        """Fill in each summarizable header's stored label, where it is current.

        A label written against a later checkpoint describes blocks this page
        did not fold, so it is left off rather than shown against the wrong
        work.
        """

        headers: dict[str, list[dict[str, Any]]] = {}
        for block in blocks:
            for item in block.record.get("blocks") or []:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").strip() != "process_block":
                    continue
                details = item.get("process_details")
                if not isinstance(details, dict) or not details.get("summarize"):
                    continue
                owner = str(details.get("message_id") or "").strip()
                if owner:
                    headers.setdefault(owner, []).append(details)
        if not headers or self._session_title_service is None:
            return
        rows = await self._session_title_service.read_process_summaries(
            session_id,
            sorted(headers),
        )
        for owner, owned_details in headers.items():
            row = rows.get(owner) if isinstance(rows, dict) else None
            if not isinstance(row, dict):
                continue
            row_seq = row.get("through_seq")
            if isinstance(row_seq, bool) or not isinstance(row_seq, int):
                continue
            if row_seq > through_seq:
                continue
            for details in owned_details:
                details["summary"] = {
                    "status": row.get("status"),
                    "summary": row.get("summary"),
                    "error": row.get("error"),
                }

    async def _must_get_projection_backed_session(
        self,
        user: UserContext,
        session_id: str,
        *,
        reconcile_conversation: bool = False,
        reconcile_agent_binding: bool = True,
        internal_wiring: bool = False,
    ) -> dict[str, Any]:
        base_session = await self._must_get_owned_session(user, session_id)
        base_session = await self._touch_projection_session(
            session_id,
            session=base_session,
        )
        if reconcile_agent_binding:
            base_session = await self._reconcile_runtime_binding(base_session)
        snapshot = await self._converge_lifecycle_projection(
            session=base_session,
            snapshot=await self._get_kernel_session_snapshot(session_id),
        )
        if reconcile_conversation:
            snapshot = await self._reconcile_stuck_turn(
                session_id=session_id,
                session=base_session,
                snapshot=snapshot,
            )
        pending_interaction = await self._get_pending_interaction(
            session_id,
            snapshot=snapshot,
        )
        rendered = self._render_projection_backed_session(
            base_session,
            snapshot=snapshot,
            pending_interaction=pending_interaction,
            internal_wiring=internal_wiring,
        )
        # Snapshot is the sole authority — no sessions.state cross-referencing.
        return rendered

    async def _touch_projection_session(
        self,
        session_id: str,
        *,
        session: dict[str, Any],
    ) -> dict[str, Any]:
        touch_fn = getattr(self._session_service, "_touch_session", None)
        if not callable(touch_fn):
            return session
        try:
            touched = await touch_fn(session_id, session=session)
        except Exception as exc:
            logger.warning(
                "projection session touch failed session=%s err=%s",
                session_id,
                exc,
            )
            return session
        return dict(touched) if isinstance(touched, dict) else session

    async def _get_messages_page(
        self,
        session_id: str,
        *,
        limit: int,
        before: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return messages derived from durable session events only.

        Overlay building is done separately in ``get_messages()`` and
        returned as the independent ``active_turn_overlay`` field.
        """
        rows, has_more = await self._message_view.list_page(
            session_id,
            limit=limit,
            before=before,
        )
        return rows, has_more

    @staticmethod
    def _resolve_active_turn_id_for_messages(
        *,
        session: dict[str, Any] | None,
        snapshot: dict[str, Any] | None,
        pending_interaction: dict[str, Any] | None,
    ) -> str | None:
        pending_turn_id = str((pending_interaction or {}).get("turn_id") or "").strip()
        if pending_turn_id:
            return pending_turn_id

        snapshot_conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        snapshot_turn_id = str((snapshot or {}).get("current_turn_id") or "").strip()
        if snapshot_turn_id and snapshot_conversation_state in _ACTIVE_CONVERSATION_SNAPSHOT_STATES:
            return snapshot_turn_id

        base_state = str((session or {}).get("state") or "").strip()
        base_turn_id = str((session or {}).get("current_turn_id") or "").strip()
        if base_turn_id and base_state in _ACTIVE_SESSION_STATES:
            return base_turn_id
        return None

    async def _build_active_turn_overlay_message(
        self,
        session_id: str,
        *,
        turn_id: str,
        rows: list[dict[str, Any]],
        session: dict[str, Any] | None,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        async def _list_overlay_frames(after_seq: int) -> list[dict[str, Any]]:
            page_size = 500
            collected: list[dict[str, Any]] = []
            cursor = int(after_seq)
            while True:
                batch = await self._session_events_repo.list_frames(
                    session_id,
                    turn_id=turn_id,
                    after_seq=cursor,
                    limit=page_size,
                )
                if not batch:
                    break
                collected.extend(batch)
                next_cursor = max(
                    int(frame.get("frame_seq") or cursor)
                    for frame in batch
                )
                if next_cursor <= cursor:
                    break
                cursor = next_cursor
                if len(batch) < page_size:
                    break
            return collected

        def _max_live_source_cursor(frames: list[dict[str, Any]]) -> dict[str, int] | None:
            best: dict[str, int] | None = None
            for frame in frames:
                cursor = normalize_live_source_cursor(
                    {
                        "live_seq": frame.get("live_seq"),
                        "sandbox_turn_id": frame.get("source_sandbox_turn_id"),
                        "sandbox_seq": frame.get("source_sandbox_seq"),
                    }
                )
                if not isinstance(cursor, dict):
                    continue
                if best is None:
                    best = cursor
                    continue
                best_seq = _coerce_int(best.get("sandbox_seq"))
                next_seq = _coerce_int(cursor.get("sandbox_seq"))
                if next_seq is not None and (best_seq is None or next_seq > best_seq):
                    best = cursor
                    continue
                if best_seq is None and _coerce_int(cursor.get("live_seq")) is not None:
                    best_live = _coerce_int(best.get("live_seq"))
                    next_live = _coerce_int(cursor.get("live_seq"))
                    if next_live is not None and (best_live is None or next_live > best_live):
                        best = cursor
            return best

        async def _advance_resume_watermark_past_terminal_frames(
            base_watermark: int | None,
        ) -> int | None:
            if base_watermark is None:
                return None
            next_after_seq = int(base_watermark)
            while True:
                trailing = await self._session_events_repo.list_frames(
                    session_id,
                    turn_id=turn_id,
                    after_seq=next_after_seq,
                    limit=4,
                )
                advanced = False
                for frame in trailing:
                    frame_seq = int(frame.get("frame_seq") or -1)
                    payload = frame.get("payload")
                    payload_type = str((payload or {}).get("type") or "").strip()
                    if frame_seq <= next_after_seq:
                        continue
                    if payload_type not in {"finish", "error"}:
                        return next_after_seq
                    next_after_seq = frame_seq
                    advanced = True
                if not advanced:
                    return next_after_seq

        existing_message = next(
            (
                dict(item)
                for item in reversed(rows)
                if str(item.get("turn_id") or "").strip() == turn_id
                and str(item.get("role") or "").strip() == "assistant"
            ),
            None,
        )
        if existing_message is None:
            existing_message = await self._message_view.get_assistant_message_for_turn(
                session_id,
                turn_id=turn_id,
            )

        active_interaction = await self._interaction_snapshots_repo.get_active_interaction(
            session_id
        )
        # --- Frame watermark: incremental overlay ---
        wm_raw = (existing_message or {}).get("source_frame_seq_applied")
        watermark: int | None = int(wm_raw) if wm_raw is not None else None
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()

        replay_waiting_turn_from_origin = (
            conversation_state == "WAITING_FOR_INTERACTION"
            and isinstance(existing_message, dict)
            and watermark is not None
            and isinstance(active_interaction, dict)
        )

        # Short-circuit: WAITING_FOR_INTERACTION + durable row + watermark → return durable row
        # Only safe when there is no active interaction snapshot to reconcile.
        if (
            conversation_state == "WAITING_FOR_INTERACTION"
            and isinstance(existing_message, dict)
            and watermark is not None
            and not replay_waiting_turn_from_origin
        ):
            return {
                **dict(existing_message),
                "source_frame_seq_applied": await _advance_resume_watermark_past_terminal_frames(
                    watermark,
                ),
            }

        overlay_existing_message = existing_message
        after_seq = watermark if watermark is not None else -1
        if replay_waiting_turn_from_origin:
            after_seq = -1
        frames = await _list_overlay_frames(after_seq)
        if not frames and not isinstance(existing_message, dict):
            return None

        max_message_seq = max(
            [int(item.get("message_seq") or 0) for item in rows],
            default=0,
        )
        engine_fifo_messages = build_active_engine_fifo_messages(
            session_id=session_id,
            turn_id=turn_id,
            frames=frames,
            default_message_seq=max_message_seq + 1,
            user_id=str((session or {}).get("user_id") or ""),
            active_interaction=active_interaction,
        )
        if engine_fifo_messages:
            active_message = dict(engine_fifo_messages[-1])
            if not str(active_message.get("message_id") or "").strip():
                raise RuntimeError("engine FIFO message is missing message_id")
        else:
            if isinstance(overlay_existing_message, dict):
                active_message_id = str(
                    overlay_existing_message.get("message_id") or ""
                ).strip()
                if not active_message_id:
                    raise RuntimeError("active message is missing message_id")
            else:
                # Engines without native message identities use the platform turn
                # id as their explicit UI-message identity. Consumers must still
                # treat message_id and turn_id as separate fields.
                active_message_id = turn_id
            active_message = build_active_turn_message(
                session_id=session_id,
                turn_id=turn_id,
                message_id=active_message_id,
                frames=frames,
                existing_message=overlay_existing_message,
                default_message_seq=max_message_seq + 1,
                user_id=str((session or {}).get("user_id") or ""),
                active_interaction=active_interaction,
                incremental=watermark is not None and not replay_waiting_turn_from_origin,
            )
        if not isinstance(active_message, dict):
            return active_message
        active_watermark_raw = active_message.get("source_frame_seq_applied")
        active_watermark = (
            int(active_watermark_raw)
            if active_watermark_raw is not None
            else None
        )
        return {
            **active_message,
            **(
                {"__engine_fifo_messages": engine_fifo_messages}
                if engine_fifo_messages
                else {}
            ),
            "source_frame_seq_applied": await _advance_resume_watermark_past_terminal_frames(
                active_watermark,
            ),
            "live_source_cursor": _max_live_source_cursor(frames),
        }

    # Pending interaction authority is interaction_snapshots only.
    # Read paths do not mutate broker/transcript truth directly.
    # Shared agent sandbox renewal may still update lease metadata.

    async def _get_pending_interaction(
        self,
        session_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        # interaction_snapshots(active=true) is the sole authority for whether
        # an interaction is currently pending. session_snapshots.active_interaction_id
        # remains a projection/debug field and must not veto a real active
        # interaction snapshot.
        if not isinstance(snapshot, dict):
            snapshot = await self._session_snapshots_repo.get_snapshot(session_id)

        interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
        if not isinstance(interaction, dict):
            return None

        if isinstance(snapshot, dict):
            snapshot_active_id = str(snapshot.get("active_interaction_id") or "").strip()
            interaction_id = str(interaction.get("interaction_id") or "").strip()
            if not snapshot_active_id:
                logger.warning(
                    "active interaction snapshot exists without session snapshot pointer session=%s interaction=%s",
                    session_id,
                    interaction_id,
                )
            elif interaction_id and snapshot_active_id != interaction_id:
                logger.warning(
                    "active interaction snapshot differs from session snapshot pointer session=%s snapshot_active=%s interaction=%s",
                    session_id,
                    snapshot_active_id,
                    interaction_id,
                )

        clean = dict(interaction)
        clean.pop("_id", None)
        clean.pop("source_event_seq_applied", None)
        clean.pop("updated_at", None)
        clean.pop("active", None)
        clean.pop("interaction_state", None)
        return clean

    async def _get_kernel_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return await self._session_snapshots_repo.get_snapshot(session_id)

    def _render_projection_backed_session(
        self,
        session: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None,
        pending_interaction: dict[str, Any] | None,
        background_task_state: dict[str, Any] | None = None,
        internal_wiring: bool = False,
    ) -> dict[str, Any]:
        clean = self._session_service._sanitize_session(session)
        if internal_wiring:
            # The sanitizer strips wiring the API must not expose, but the
            # dispatch machinery consumes this same rendered dict and the
            # assistant attach planner resolves by workspace_ref: without it
            # an assistant turn falls into the generic attach and fails with a
            # 500. Restored for internal callers only, field by named field.
            clean["workspace_ref"] = session.get("workspace_ref")
        terminal_cwd = self._resolve_rendered_terminal_cwd(clean, snapshot)
        if terminal_cwd:
            clean["terminal_cwd"] = terminal_cwd
        if not isinstance(snapshot, dict):
            clean["engine_kind"] = resolve_session_engine_kind(clean)
            capabilities = _installed_session_capabilities(clean)
            clean["engine_available"] = capabilities is not None
            return clean
        snapshot = self._normalize_projection_snapshot(clean, snapshot)

        permission_mode = str(snapshot.get("permission_mode") or "").strip()
        if permission_mode:
            clean["permission_mode"] = permission_mode
        pending_turn_id = str((pending_interaction or {}).get("turn_id") or "").strip()
        current_turn_id = pending_turn_id or str(snapshot.get("current_turn_id") or "").strip()
        clean["current_turn_id"] = current_turn_id or None
        clean["last_turn_id"] = str(snapshot.get("last_turn_id") or "").strip() or None
        clean["last_turn_status"] = str(snapshot.get("last_turn_status") or "").strip() or None
        clean["last_turn_error"] = str(snapshot.get("last_turn_error") or "").strip() or None
        clean["last_turn_command_id"] = str(snapshot.get("last_turn_command_id") or "").strip() or None
        delivery_state = str(snapshot.get("delivery_state") or "").strip()
        clean["delivery_state"] = delivery_state or None
        last_turn_failure_phase = str(snapshot.get("last_turn_failure_phase") or "").strip()
        clean["last_turn_failure_phase"] = last_turn_failure_phase or None
        last_turn_terminal_reason = str(snapshot.get("last_turn_terminal_reason") or "").strip()
        clean["last_turn_terminal_reason"] = last_turn_terminal_reason or None
        agent_binding = snapshot.get("agent_binding")
        if isinstance(agent_binding, dict):
            agent_id = str(agent_binding.get("agent_id") or "").strip()
            if agent_id:
                clean["agent_id"] = agent_id
            session_kind = str(agent_binding.get("session_kind") or "").strip()
            if session_kind:
                clean["session_kind"] = session_kind
        clean["engine_kind"] = resolve_session_engine_kind(clean)
        capabilities = _installed_session_capabilities(clean)
        clean["engine_available"] = capabilities is not None
        ui = self.derive_ui_state(
            snapshot,
            pending_interaction=pending_interaction,
            background_task_state=background_task_state,
        )
        runtime_binding = clean.get("runtime_binding")
        binding_can_dispatch = (
            isinstance(runtime_binding, dict)
            and runtime_binding.get("state") == "READY"
            and runtime_binding.get("can_dispatch")
        )
        ui_state = ui["state"]
        if ui_state == "TERMINATED" and clean.get("state") == "READY":
            ui_state = "READY"
        clean["state"] = ui_state
        clean["background_task_state"] = background_task_state
        clean["runtime_warning"] = ui["runtime_warning"]
        clean["runtime_unavailable"] = self._derive_runtime_unavailable(
            clean,
            snapshot=snapshot,
        )
        if (
            not bool(clean.get("runtime_unavailable"))
            and str(snapshot.get("runtime_connectivity_state") or "").strip() == "CONNECTED"
        ):
            clean["last_error"] = None
        # Recovery posture derives from the final rendered state (the ui
        # overlay above may differ from the row's), through the same shared
        # helper other reads use.
        (
            clean["recovery_policy"],
            clean["recovery_reason"],
        ) = self._session_service.derive_recovery_fields(ui_state, clean)
        if str(snapshot.get("session_lifecycle_state") or "").strip() == "DELETED":
            clean["deleted"] = True
        return clean

    def _resolve_rendered_terminal_cwd(
        self,
        session: dict[str, Any],
        snapshot: dict[str, Any] | None,
    ) -> str | None:
        if isinstance(snapshot, dict):
            snapshot_cwd = str(snapshot.get("terminal_cwd") or "").strip()
            if snapshot_cwd:
                return snapshot_cwd

        session_cwd = str(session.get("terminal_cwd") or "").strip()
        if session_cwd:
            return session_cwd

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None

        agent_binding = snapshot.get("agent_binding") if isinstance(snapshot, dict) else None
        if not isinstance(agent_binding, dict):
            agent_binding = {}
        session_kind = (
            str(agent_binding.get("session_kind") or "").strip()
            or str(session.get("session_kind") or "").strip()
        )
        session_kind = require_session_kind(session_kind)
        engine_session_key = str(session.get("engine_session_key") or "").strip() or None
        resolved = self._runtime_manager.resolve_session_terminal_cwd(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
            session_kind=session_kind,
            engine_session_key=engine_session_key,
        )
        return str(resolved or "").strip() or None

    async def _build_delivery_failure(
        self,
        session_id: str,
        *,
        snapshot: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Synthesize delivery_failure from session_events.

        Only returns non-null when delivery_state === 'NOT_RECEIVED' in
        snapshot.  Queries the last ``command.accepted(StartTurn)`` journal
        event and extracts content + client_message_id.
        """
        if not isinstance(snapshot, dict):
            return None
        delivery_state = str(snapshot.get("delivery_state") or "").strip()
        if delivery_state != "NOT_RECEIVED":
            return None
        last_turn_id = str(snapshot.get("last_turn_id") or "").strip()
        if not last_turn_id:
            return None
        events = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="command",
            turn_id=last_turn_id,
            event_type="command.accepted",
            limit=20,
        )
        for event in reversed(events):
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if str(payload.get("command_type") or "").strip() != "StartTurn":
                continue
            content_text = str(payload.get("content") or "").strip()
            client_message_id = str(payload.get("client_message_id") or "").strip() or None
            return {
                "turn_id": last_turn_id,
                "client_message_id": client_message_id,
                "text": content_text,
                "summary": "the sandbox did not receive this message",
            }
        return None

    @staticmethod
    def _normalize_projection_snapshot(
        session: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Snapshot is the sole authority — no session.state cross-referencing."""
        return dict(snapshot)

    @staticmethod
    def derive_ui_state(
        snapshot: dict[str, Any],
        *,
        pending_interaction: dict[str, Any] | None = None,
        background_task_state: dict[str, Any] | None = None,
    ) -> dict[str, str | bool]:
        """Pure function: snapshot -> {state, runtime_warning}.

        Returns the user-facing session state and a degradation flag.
        No ``sessions`` collection data needed.
        """
        lifecycle = str(snapshot.get("session_lifecycle_state") or "").strip()
        conversation = str(snapshot.get("conversation_state") or "").strip()
        runtime = str(snapshot.get("runtime_connectivity_state") or "").strip()
        terminal = str(snapshot.get("terminal_state") or "").strip()
        active_iid = str(snapshot.get("active_interaction_id") or "").strip()

        runtime_warning = (
            runtime in {"DEGRADED", "LOST"}
            or _needs_recovery(snapshot)
        )
        has_background_work = (
            isinstance(background_task_state, dict)
            and str(background_task_state.get("state") or "").strip() == "OPEN"
        )

        if lifecycle == "CREATING":
            return {"state": SessionState.CREATING.value, "runtime_warning": False}
        if lifecycle == "TERMINATED":
            return {"state": SessionState.TERMINATED.value, "runtime_warning": False}
        if lifecycle == "DELETED":
            return {"state": SessionState.DELETED.value, "runtime_warning": False}
        # lifecycle is ACTIVE from here
        if terminal == "INTERRUPTING" or conversation == "INTERRUPTING":
            return {"state": SessionState.INTERRUPTING.value, "runtime_warning": runtime_warning}
        if terminal == "RUNNING":
            return {"state": "PROCESSING", "runtime_warning": runtime_warning}
        if conversation == "WAITING_FOR_INTERACTION" or active_iid or pending_interaction is not None:
            return {"state": "WAITING_INPUT", "runtime_warning": runtime_warning}
        if conversation in {"PROCESSING", "STREAMING"}:
            return {"state": "PROCESSING", "runtime_warning": runtime_warning}
        if conversation == "IDLE" and not _completed_turn_has_terminal_frame_proof(snapshot):
            return {"state": "PROCESSING", "runtime_warning": runtime_warning}
        if has_background_work:
            return {
                "state": SessionState.BACKGROUND_RUNNING.value,
                "runtime_warning": runtime_warning,
            }
        return {"state": SessionState.READY.value, "runtime_warning": runtime_warning}

    @staticmethod
    def _derive_runtime_unavailable(
        session: dict[str, Any],
        *,
        snapshot: dict[str, Any],
    ) -> bool:
        lifecycle_state = str(snapshot.get("session_lifecycle_state") or "").strip()
        runtime_state = str(snapshot.get("runtime_connectivity_state") or "").strip()
        runtime_binding = session.get("runtime_binding")
        if isinstance(runtime_binding, dict):
            binding_state = str(runtime_binding.get("state") or "").strip()
            if binding_state == "READY" and runtime_binding.get("can_dispatch"):
                return False
            if binding_state in {"UNAVAILABLE", "RECOVERY_REQUIRED", "HIBERNATING"}:
                return True
            if binding_state == "DELETED":
                return False
        if lifecycle_state in {"TERMINATED", "DELETED"}:
            return True
        if runtime_state == "CONNECTED":
            return False
        if runtime_state in {"DEGRADED", "LOST"}:
            return True
        return bool(session.get("runtime_unavailable"))

    def _require_turn_eligible(
        self,
        session: dict[str, Any],
        *,
        channel: str,
        reject_creating: bool = True,
        reject_terminated: bool = True,
        reject_deleted: bool = True,
    ) -> None:
        state = str(session.get("state") or "")
        if reject_creating and state == SessionState.CREATING.value:
            raise APIError(
                code="SESSION_BUSY",
                message="session is still creating, please retry",
                status_code=409,
            )
        if reject_terminated and state == SessionState.TERMINATED.value:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"{channel} is unavailable because the session is terminated",
                status_code=409,
            )
        if reject_deleted and state == SessionState.DELETED.value:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=f"{channel} is unavailable because the session is deleted",
                status_code=409,
            )
