"""Pending-interaction resolution for the turn worker.

``_resolve_pending_interaction`` is mixed into ``TurnWorker`` as
:class:`_AnswerStreamMixin`: the interaction_snapshots-backed authority the
bridge consults when a turn starts (and the engine-client answer path reads
through the kernel service).
"""
from __future__ import annotations

from typing import Any

from astrabox.common.logger.logger_factory import get_logger

logger = get_logger(__name__)


class _AnswerStreamMixin:
    """Pending-interaction resolution mixed into :class:`TurnWorker`.

    ``self`` is the ``TurnWorker`` instance.
    """

    async def _resolve_pending_interaction(
        self,
        *,
        session: dict[str, Any],
        session_id: str,
        interaction_response: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        expected_interaction_id = str((interaction_response or {}).get("interaction_id") or "").strip()
        # interaction_snapshots is the sole authority for pending
        # interactions.  Do not read from sessions.pending_interaction.
        #
        # For AnswerInteraction, the worker persists interaction.answer_persisted
        # before deciding which execution path to take.  That projection marks
        # the interaction inactive, so command-path resolution must be able to
        # recover the exact snapshot by interaction_id instead of relying solely
        # on the active=true lookup.
        if expected_interaction_id:
            interaction = await self._interaction_snapshots_repo.get_interaction(
                session_id,
                expected_interaction_id,
            )
        else:
            interaction = await self._interaction_snapshots_repo.get_active_interaction(session_id)
        if not isinstance(interaction, dict):
            return None
        interaction_id = str(interaction.get("interaction_id") or "").strip()
        if expected_interaction_id and interaction_id != expected_interaction_id:
            return None

        # Cross-check against session_snapshots, the same way the GET read
        # path in _get_pending_interaction does. That pointer is a projection
        # field and never vetoes a live interaction snapshot, so a
        # disagreement is logged rather than acted on.
        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        if isinstance(snapshot, dict):
            snapshot_active_id = str(snapshot.get("active_interaction_id") or "").strip()
            if not snapshot_active_id:
                logger.warning(
                    "active interaction snapshot exists without session snapshot pointer session=%s interaction=%s (command path)",
                    session_id,
                    interaction_id,
                )
            elif interaction_id and snapshot_active_id != interaction_id:
                logger.warning(
                    "active interaction snapshot differs from session snapshot pointer session=%s snapshot_active=%s interaction=%s (command path)",
                    session_id,
                    snapshot_active_id,
                    interaction_id,
                )

        return dict(interaction)
