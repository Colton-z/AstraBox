"""Engine-neutral pending-interaction validation at turn entry."""

from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.chunk_processing import (
    extract_partial_text,
    serialize_message,
)
from astrabox.core.service.orchestrator.engine.interaction_contract import (
    render_interaction_reply,
    resolve_interaction_permission_mode,
)
from astrabox.core.service.orchestrator.engine.input_content import turn_input_is_empty

logger = get_logger(__name__)


class InteractionFunnel:
    """Resolve the platform's pending interaction before engine dispatch."""


    def __init__(
        self,
        *,
        interaction_snapshots_repo=None,
        session_snapshots_repo=None,
    ) -> None:
        self._interaction_snapshots_repo = interaction_snapshots_repo
        self._session_snapshots_repo = session_snapshots_repo

    # Structural helpers from the interaction contract kit: the record's
    # declared presentation carries everything an answer needs, so the funnel
    # never consults an engine vocabulary.
    _format_interaction_response_content = staticmethod(render_interaction_reply)
    _resolve_interaction_permission_mode = staticmethod(resolve_interaction_permission_mode)

    # Delegate to raw SDK event helpers.
    _serialize_message = staticmethod(serialize_message)
    _extract_partial_text = staticmethod(extract_partial_text)

    @staticmethod
    def _has_turn_dispatch_permission_context(
        *,
        turn_id: str | None,
        command_id: str | None,
        requested_permission_mode: str | None,
    ) -> bool:
        return bool(
            str(turn_id or "").strip()
            or str(command_id or "").strip()
            or requested_permission_mode is not None
        )

    async def _resolve_turn_request(
        self,
        *,
        session: dict[str, Any],
        content: str,
        content_blocks: list[dict[str, Any]] | None,
        interaction_response: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        pending = await self._get_turn_request_pending_interaction(session=session)
        if interaction_response is not None:
            if pending is None:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="there is no pending interaction to answer",
                    status_code=409,
                )
            interaction_id = str(interaction_response.get("interaction_id") or "").strip()
            if not interaction_id:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="interaction_id is required",
                    status_code=400,
                )
            expected = str(pending.get("interaction_id") or "").strip()
            if interaction_id != expected:
                raise APIError(
                    code="INVALID_REQUEST",
                    message="interaction response does not match the latest pending interaction",
                    status_code=409,
                )
            return self._format_interaction_response_content(pending, interaction_response), pending

        if pending is not None:
            raise APIError(
                code="INVALID_REQUEST",
                message="session is waiting for you to answer a pending question",
                status_code=409,
                data={"pending_interaction": pending},
            )
        if turn_input_is_empty(content, content_blocks):
            raise APIError(code="INVALID_REQUEST", message="content is empty", status_code=400)
        return content, None

    async def _get_turn_request_pending_interaction(
        self,
        *,
        session: dict[str, Any],
    ) -> dict[str, Any] | None:

        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            return None
        if self._interaction_snapshots_repo is None or self._session_snapshots_repo is None:
            raise RuntimeError(
                "interaction/session snapshots repos are required for turn gating"
            )

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
        if not isinstance(interaction, dict):
            return None

        if isinstance(snapshot, dict):
            snapshot_active_id = str(snapshot.get("active_interaction_id") or "").strip()
            interaction_id = str(interaction.get("interaction_id") or "").strip()
            if not snapshot_active_id:
                logger.warning(
                    "turn request sees active interaction snapshot without session snapshot pointer session=%s interaction=%s",
                    session_id,
                    interaction_id,
                )
            elif interaction_id and snapshot_active_id != interaction_id:
                logger.warning(
                    "turn request sees active interaction snapshot different from session snapshot pointer session=%s snapshot_active=%s interaction=%s",
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
