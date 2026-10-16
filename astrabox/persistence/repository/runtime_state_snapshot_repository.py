"""Opaque engine state archives, one published snapshot per owner and kind.

An owner (a user's Assistant, Agent or session, for one engine) holds up to
two kinds of archive, saved and restored independently: the engine's native
session store, and the profile state beside it (an Assistant's memories,
skills and scheduled jobs). The platform stores the bytes without reading
them, in 256 KiB chunks.

A save names the snapshot it replaces. Publishing is one transaction: it moves
the head from that predecessor to the new snapshot, stores the new chunks and
deletes the replaced snapshot's, so a head always names a complete snapshot
and an owner never keeps more than one. A save whose predecessor is not the
current head changes nothing and raises :class:`RuntimeStateSnapshotConflict`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from astrabox.persistence.sql import fetch_all, fetch_one, transaction
from astrabox.persistence.sql.models.sessions import RuntimeStateChunk, RuntimeStateHead

CHUNK_SIZE = 256 * 1024

#: The engine's native session store.
NATIVE_SESSION_STORE = "native_session_store"
#: An Assistant profile's state: its memories, skills and scheduled jobs.
PROFILE_STATE = "profile_state"

_ARCHIVES = frozenset({NATIVE_SESSION_STORE, PROFILE_STATE})
_HEADS = RuntimeStateHead.__table__
_CHUNKS = RuntimeStateChunk.__table__


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class RuntimeStateOwner:
    user_id: str
    subject_kind: str
    subject_id: str
    engine_kind: str

    def __post_init__(self) -> None:
        for field in ("user_id", "subject_kind", "subject_id", "engine_kind"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"runtime state owner {field} must be non-empty")
        if self.subject_kind not in {"assistant", "agent", "session"}:
            raise ValueError("runtime state owner subject_kind must be assistant, agent or session")

    def canonical_key(self) -> str:
        return json.dumps(
            [self.user_id, self.subject_kind, self.subject_id, self.engine_kind],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def storage_key(self) -> str:
        """The owner's key in the archive tables: the digest of its canonical key."""
        return _sha256(self.canonical_key().encode("utf-8"))


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    snapshot_id: str
    payload: bytes
    sha256: str
    size: int


class RuntimeStateSnapshotConflict(RuntimeError):
    """Another snapshot replaced the caller's expected predecessor."""


class RuntimeStateSnapshotCorrupt(RuntimeError):
    """A published snapshot or its chunks failed integrity validation."""


class _NotPublished(Exception):
    """The head names another snapshot than the predecessor a save expected."""


def _snapshot_id(owner: RuntimeStateOwner, sha256: str, previous_snapshot_id: str | None) -> str:
    """The identity of a snapshot of ``sha256`` published over ``previous_snapshot_id``.

    A retry of one save has the same identity. Restoring older bytes over a
    newer head gets a different one, so a writer holding an old head cannot
    pass the compare-and-set that a newer snapshot moved past.
    """
    return _sha256(
        json.dumps(
            [owner.canonical_key(), previous_snapshot_id, sha256],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _chunk_count(size: int) -> int:
    return max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)


class RuntimeStateSnapshotRepository:
    """One kind of archive: ``NATIVE_SESSION_STORE`` or ``PROFILE_STATE``."""

    def __init__(self, archive: str = NATIVE_SESSION_STORE) -> None:
        if archive not in _ARCHIVES:
            raise ValueError(f"unknown runtime state archive {archive!r}")
        self._archive = archive

    def _owner_values(self, owner: RuntimeStateOwner) -> dict[str, str]:
        return {
            "archive": self._archive,
            "owner_key": owner.storage_key(),
            "user_id": owner.user_id,
            "subject_kind": owner.subject_kind,
            "subject_id": owner.subject_id,
            "engine_kind": owner.engine_kind,
        }

    async def _head(self, owner: RuntimeStateOwner) -> dict[str, Any] | None:
        return await fetch_one(
            select(_HEADS).where(
                _HEADS.c.archive == self._archive, _HEADS.c.owner_key == owner.storage_key()
            )
        )

    async def load(self, owner: RuntimeStateOwner) -> RuntimeStateSnapshot | None:
        """The published snapshot, verified whole; ``None`` when nothing was published."""
        rows = await fetch_all(
            select(_HEADS, _CHUNKS.c.chunk_index, _CHUNKS.c.data)
            .select_from(
                _HEADS.outerjoin(
                    _CHUNKS,
                    (_CHUNKS.c.archive == _HEADS.c.archive)
                    & (_CHUNKS.c.owner_key == _HEADS.c.owner_key)
                    & (_CHUNKS.c.snapshot_id == _HEADS.c.snapshot_id),
                )
            )
            .where(_HEADS.c.archive == self._archive, _HEADS.c.owner_key == owner.storage_key())
            .order_by(_CHUNKS.c.chunk_index)
        )
        if not rows:
            return None
        head = rows[0]
        if (
            any(head[key] != value for key, value in self._owner_values(owner).items())
            or head["snapshot_id"]
            != _snapshot_id(owner, head["sha256"], head["previous_snapshot_id"])
            or head["chunk_count"] != _chunk_count(head["size"])
        ):
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot head is invalid")
        chunks = [row for row in rows if row["chunk_index"] is not None]
        if [row["chunk_index"] for row in chunks] != list(range(head["chunk_count"])):
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunks are incomplete")
        for row in chunks:
            expected = min(CHUNK_SIZE, head["size"] - row["chunk_index"] * CHUNK_SIZE)
            if len(row["data"]) != expected:
                raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunk size is invalid")
        payload = b"".join(bytes(row["data"]) for row in chunks)
        if _sha256(payload) != head["sha256"]:
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot checksum does not match")
        return RuntimeStateSnapshot(head["snapshot_id"], payload, head["sha256"], head["size"])

    async def save(
        self,
        owner: RuntimeStateOwner,
        payload: bytes,
        expected_snapshot_id: str | None = None,
    ) -> str:
        """Publish ``payload`` over ``expected_snapshot_id``; the published snapshot's id.

        ``None`` expects no published snapshot. Saving the bytes the head
        already holds is a no-op that returns the head, so a retried save
        succeeds. A different current head raises
        :class:`RuntimeStateSnapshotConflict` and changes nothing.
        """
        if not isinstance(payload, bytes):
            raise ValueError("runtime state snapshot payload must be bytes")
        if expected_snapshot_id is not None and not _is_digest(expected_snapshot_id):
            raise ValueError("expected_snapshot_id must be a snapshot digest or None")
        sha256 = _sha256(payload)
        size = len(payload)
        current = await self._head(owner)
        if current is not None and current["sha256"] == sha256 and current["size"] == size:
            return str(current["snapshot_id"])
        if (current["snapshot_id"] if current is not None else None) != expected_snapshot_id:
            raise RuntimeStateSnapshotConflict("runtime state snapshot predecessor changed")
        snapshot_id = _snapshot_id(owner, sha256, expected_snapshot_id)
        head = {
            **self._owner_values(owner),
            "snapshot_id": snapshot_id,
            "previous_snapshot_id": expected_snapshot_id,
            "sha256": sha256,
            "size": size,
            "chunk_count": _chunk_count(size),
        }
        chunks = [
            {
                "archive": self._archive,
                "owner_key": head["owner_key"],
                "snapshot_id": snapshot_id,
                "chunk_index": index,
                "data": payload[offset:offset + CHUNK_SIZE],
            }
            for index, offset in enumerate(range(0, max(size, 1), CHUNK_SIZE))
        ]

        async def publish(conn: AsyncConnection) -> None:
            # The head moves first: its row lock orders concurrent saves, and a
            # save that lost the compare-and-set stops before writing chunks.
            if expected_snapshot_id is None:
                moved = await conn.execute(
                    pg_insert(_HEADS)
                    .values(**head)
                    .on_conflict_do_nothing()
                    .returning(_HEADS.c.snapshot_id)
                )
            else:
                moved = await conn.execute(
                    update(_HEADS)
                    .where(
                        _HEADS.c.archive == self._archive,
                        _HEADS.c.owner_key == head["owner_key"],
                        _HEADS.c.snapshot_id == expected_snapshot_id,
                    )
                    .values(
                        snapshot_id=snapshot_id,
                        previous_snapshot_id=expected_snapshot_id,
                        sha256=sha256,
                        size=size,
                        chunk_count=head["chunk_count"],
                    )
                    .returning(_HEADS.c.snapshot_id)
                )
            if moved.first() is None:
                raise _NotPublished
            await conn.execute(insert(_CHUNKS), chunks)
            await conn.execute(
                delete(_CHUNKS).where(
                    _CHUNKS.c.archive == self._archive,
                    _CHUNKS.c.owner_key == head["owner_key"],
                    _CHUNKS.c.snapshot_id != snapshot_id,
                )
            )

        try:
            await transaction(publish)
        except _NotPublished:
            winner = await self._head(owner)
            if winner is not None and winner["sha256"] == sha256 and winner["size"] == size:
                return str(winner["snapshot_id"])
            raise RuntimeStateSnapshotConflict(
                "runtime state snapshot predecessor changed"
            ) from None
        return snapshot_id
