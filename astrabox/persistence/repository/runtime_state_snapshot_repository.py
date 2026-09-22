"""Opaque runtime archives, published only after every immutable chunk is verified.

The chunk store composes the existing idempotent transcript batch writer in a
separate collection. Its sequence allocator is not a completeness signal. A
single owner-scoped head is the only publication point; interrupted writes leave
the previous head readable. No chat Session identity is involved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from astrabox.persistence.repository._compat import DuplicateKeyError, ReturnDocument
from astrabox.persistence.repository.backend import get_async_collection, run_mongo_with_retry
from astrabox.persistence.repository.transcript_entry_repository import TranscriptEntryRepository

CHUNK_SIZE = 256 * 1024
HEAD_COLLECTION_NAME = "runtime_state_snapshot_heads"
CHUNK_COLLECTION_NAME = "runtime_state_snapshot_chunks"


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
        if self.subject_kind not in {"assistant", "agent"}:
            raise ValueError("runtime state owner subject_kind must be assistant or agent")

    def canonical_key(self) -> str:
        return json.dumps(
            [self.user_id, self.subject_kind, self.subject_id, self.engine_kind],
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class RuntimeStateSnapshot:
    snapshot_id: str
    payload: bytes
    sha256: str
    size: int


class RuntimeStateSnapshotConflict(RuntimeError):
    """Another complete snapshot replaced the caller's expected predecessor."""


class RuntimeStateSnapshotCorrupt(RuntimeError):
    """A published snapshot or its chunks failed integrity validation."""


class RuntimeStateSnapshotRepository:
    def __init__(self) -> None:
        self._chunks = TranscriptEntryRepository(collection_name=CHUNK_COLLECTION_NAME)

    @staticmethod
    def _owner_fields(owner: RuntimeStateOwner) -> dict[str, str]:
        return {
            "_id": _sha256(owner.canonical_key().encode("utf-8")),
            "user_id": owner.user_id,
            "subject_kind": owner.subject_kind,
            "subject_id": owner.subject_id,
            "engine_kind": owner.engine_kind,
        }

    @staticmethod
    def _snapshot_id(
        owner: RuntimeStateOwner, sha256: str, previous_snapshot_id: str | None
    ) -> str:
        # A retry has the same identity; restoring old bytes after a newer head
        # creates a different identity, so an old writer cannot pass an ABA CAS.
        return _sha256(
            json.dumps(
                [owner.canonical_key(), previous_snapshot_id, sha256],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    def _validate_head(self, owner: RuntimeStateOwner, head: dict[str, Any]) -> None:
        previous = head.get("previous_snapshot_id")
        size = head.get("size")
        count = head.get("chunk_count")
        if (
            any(head.get(key) != value for key, value in self._owner_fields(owner).items())
            or not _is_digest(head.get("sha256"))
            or not _is_digest(head.get("snapshot_id"))
            or (previous is not None and not _is_digest(previous))
            or type(size) is not int
            or size < 0
            or type(count) is not int
            or count != max(1, (size + CHUNK_SIZE - 1) // CHUNK_SIZE)
            or head["snapshot_id"] != self._snapshot_id(owner, head["sha256"], previous)
        ):
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot head is invalid")

    async def _head(self, owner: RuntimeStateOwner) -> dict[str, Any] | None:
        collection = await get_async_collection(HEAD_COLLECTION_NAME)
        value = await collection.find_one({"_id": self._owner_fields(owner)["_id"]})
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot head is not an object")
        self._validate_head(owner, value)
        return value

    async def _read_snapshot(
        self, owner: RuntimeStateOwner, head: dict[str, Any]
    ) -> RuntimeStateSnapshot:
        self._validate_head(owner, head)
        try:
            entries = await self._chunks.load_entries(
                owner.canonical_key(), owner.subject_id, head["snapshot_id"]
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunks are invalid") from exc
        if entries is None or len(entries) != head["chunk_count"]:
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunks are incomplete")
        chunks: list[bytes] = []
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or type(entry.get("index")) is not int
                or entry["index"] != index
                or not isinstance(entry.get("data"), str)
            ):
                raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunk order is invalid")
            try:
                chunk = base64.b64decode(entry["data"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunk is not base64") from exc
            expected_size = min(CHUNK_SIZE, head["size"] - index * CHUNK_SIZE)
            if len(chunk) != expected_size:
                raise RuntimeStateSnapshotCorrupt("runtime state snapshot chunk size is invalid")
            chunks.append(chunk)
        payload = b"".join(chunks)
        if len(payload) != head["size"] or _sha256(payload) != head["sha256"]:
            raise RuntimeStateSnapshotCorrupt("runtime state snapshot checksum does not match")
        return RuntimeStateSnapshot(head["snapshot_id"], payload, head["sha256"], head["size"])

    async def load(self, owner: RuntimeStateOwner) -> RuntimeStateSnapshot | None:
        """Return the complete published archive, or None if none was published."""

        async def _op() -> RuntimeStateSnapshot | None:
            head = await self._head(owner)
            return None if head is None else await self._read_snapshot(owner, head)

        return await run_mongo_with_retry("runtime_state_snapshots.load", _op)

    async def _same_content(
        self, owner: RuntimeStateOwner, head: dict[str, Any] | None, sha256: str, size: int
    ) -> str | None:
        if head is None or head["sha256"] != sha256 or head["size"] != size:
            return None
        snapshot = await self._read_snapshot(owner, head)
        return snapshot.snapshot_id

    async def save(
        self,
        owner: RuntimeStateOwner,
        payload: bytes,
        expected_snapshot_id: str | None = None,
    ) -> str:
        """Publish verified bytes with a predecessor CAS; identical head bytes are a no-op.

        None expects no published snapshot. A different current predecessor raises
        RuntimeStateSnapshotConflict, without replacing it. An interrupted batch
        can be completed by submitting the same bytes and predecessor again.
        """
        if not isinstance(payload, bytes):
            raise ValueError("runtime state snapshot payload must be bytes")
        if expected_snapshot_id is not None and not _is_digest(expected_snapshot_id):
            raise ValueError("expected_snapshot_id must be a snapshot digest or None")
        sha256 = _sha256(payload)
        size = len(payload)

        async def _op() -> str:
            current = await self._head(owner)
            same = await self._same_content(owner, current, sha256, size)
            if same is not None:
                return same
            actual = current["snapshot_id"] if current is not None else None
            if actual != expected_snapshot_id:
                raise RuntimeStateSnapshotConflict("runtime state snapshot predecessor changed")
            snapshot_id = self._snapshot_id(owner, sha256, expected_snapshot_id)
            entries = [
                {
                    "index": index,
                    "data": base64.b64encode(payload[offset:offset + CHUNK_SIZE]).decode("ascii"),
                }
                for index, offset in enumerate(range(0, max(size, 1), CHUNK_SIZE))
            ]
            # These are private storage keys, not SDK or platform Session IDs.
            # The full four-part owner fences both append and load identically.
            await self._chunks.append_entries(
                owner.canonical_key(), owner.subject_id, snapshot_id, entries,
                append_id=snapshot_id,
            )
            head = {
                **self._owner_fields(owner),
                "snapshot_id": snapshot_id,
                "previous_snapshot_id": expected_snapshot_id,
                "sha256": sha256,
                "size": size,
                "chunk_count": len(entries),
            }
            await self._read_snapshot(owner, head)
            collection = await get_async_collection(HEAD_COLLECTION_NAME)
            if current is None:
                try:
                    await collection.insert_one(head)
                    return snapshot_id
                except DuplicateKeyError:
                    pass
            else:
                published = await collection.find_one_and_update(
                    {**self._owner_fields(owner), "snapshot_id": expected_snapshot_id},
                    {"$set": {key: value for key, value in head.items() if key != "_id"}},
                    return_document=ReturnDocument.AFTER,
                )
                if published is not None:
                    return snapshot_id
            winner = await self._head(owner)
            same = await self._same_content(owner, winner, sha256, size)
            if same is not None:
                return same
            raise RuntimeStateSnapshotConflict("runtime state snapshot predecessor changed")

        return await run_mongo_with_retry("runtime_state_snapshots.save", _op)
