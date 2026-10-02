"""Reconcile native DSH replay with this adapter's already committed output."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from typing import Any, NoReturn

from astrabox.core.service.orchestrator.engine.base import EngineStreamDetached


class DshOutputReplay:
    """Consume an exact semantic prefix across different native chunk splits.

    DSH's live attempt and its durable settlement use different presentation
    identifiers. A replayed block adopts the committed block's id only when
    its native turn, step, block index and ordered output agree. The journal
    remains the authority for what was published; the native stream remains
    the authority for the suffix. No content is persisted here.
    """

    def __init__(self, session_id: str, committed_frames: tuple[dict[str, Any], ...]) -> None:
        self._session_id = session_id
        self._pending: deque[dict[str, Any]] = deque(
            self._public(row["payload"])
            for row in committed_frames
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("__engine_public_ui") is True
            and row.get("scope", "turn") == "turn"
        )
        self._block_ids: dict[str, str] = {}

    @staticmethod
    def _public(frame: dict[str, Any]) -> dict[str, Any]:
        return {key: deepcopy(value) for key, value in frame.items() if not key.startswith("__")}

    def _block_coordinates(self, frame: dict[str, Any]) -> tuple[str, str, str] | None:
        kind = str(frame.get("type") or "").split("-", 1)[0]
        identity = frame.get("id")
        prefix = f"dsh-{kind}:{self._session_id}:"
        if not isinstance(identity, str) or not identity.startswith(prefix):
            return None
        pieces = identity[len(prefix):].split(":", 2)
        if len(pieces) != 3 or ":" not in pieces[2]:
            return None
        return pieces[0], pieces[1], pieces[2].rsplit(":", 1)[1]

    def _remap_block(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind not in {"text-start", "text-delta", "text-end", "reasoning-start", "reasoning-delta", "reasoning-end"}:
            return
        identity = frame.get("id")
        if not isinstance(identity, str):
            return
        if identity not in self._block_ids and self._pending and kind in {"text-start", "reasoning-start"}:
            expected = self._pending[0]
            coordinates = self._block_coordinates(frame)
            if (
                expected.get("type") == kind
                and coordinates is not None
                and coordinates == self._block_coordinates(expected)
            ):
                self._block_ids[identity] = expected["id"]
        frame["id"] = self._block_ids.get(identity, identity)

    def accept(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        """Return only the unpublished suffix, retaining committed block ids."""
        if not self._pending and not self._block_ids:
            return [frame]
        value = deepcopy(frame)
        kind = value.get("type")
        if kind == "data-raw-event":
            return [value]
        if kind == "result":
            if self._pending:
                self._mismatch("native terminal preceded the committed prefix")
            return [value]
        self._remap_block(value)
        if not self._pending:
            return [value]
        if kind not in {"text-delta", "reasoning-delta"}:
            if self._public(value) != self._pending[0]:
                self._mismatch(f"expected {self._pending[0].get('type')}, received {kind}")
            self._pending.popleft()
            return []
        remaining = value.get("delta")
        if not isinstance(remaining, str):
            self._mismatch("native delta is not text")
        while self._pending and remaining:
            expected = self._pending[0]
            actual_shape = {key: field for key, field in self._public(value).items() if key != "delta"}
            expected_shape = {key: field for key, field in expected.items() if key != "delta"}
            if actual_shape != expected_shape or not isinstance(expected.get("delta"), str):
                self._mismatch("native text does not match the committed block")
            prefix = expected["delta"]
            size = min(len(remaining), len(prefix))
            if remaining[:size] != prefix[:size]:
                self._mismatch("native text differs from committed text")
            remaining = remaining[size:]
            if size == len(prefix):
                self._pending.popleft()
            else:
                expected["delta"] = prefix[size:]
        return [{**value, "delta": remaining}] if remaining else []

    @staticmethod
    def _mismatch(detail: str) -> NoReturn:
        raise EngineStreamDetached(f"deepseek_harness cannot reconcile output replay: {detail}")
