"""Host-side ``SessionStore`` adapter over the durable transcript repository.

The Claude Agent SDK defines ``SessionStore`` (``append``/``load`` plus
optional listing/deletion) as the official contract for externalized session
transcripts; resume, fork, and subagent restoration all operate through it.
:class:`~astrabox.persistence.repository.transcript_entry_repository.TranscriptEntryRepository`
already persists that data — the in-box mirror posts into it over HTTP. This
module is the *host-side* protocol view of the same storage: an object the SDK
(or host code using the SDK's store-backed functions) can hold directly.

Two consumers:

* the SessionStore conformance gate
  (``tests/session_store_conformance_test.py``) — the SDK ships the suite;
  this adapter is what it runs against, proving this repository's storage
  semantics match the contract the SDK relies on for resume;
* the translation-shell runner path (``docs/design-translation-shell-2026-07.md``),
  where host code refills UI-journal gaps and drives store-backed session
  operations.

``list_session_summaries`` is deliberately not implemented: the repository
keeps no summary sidecar, and the SDK's documented fallback (``list_sessions``
plus a per-session ``load``) is correct, just slower. Add the sidecar if
listing latency ever matters; do not fake it here.

Entries are opaque pass-through blobs (contract: ``load`` returns them
deep-equal). This adapter adds nothing to them — turn stamping is the in-box
mirror's concern, not the store's.
"""

from __future__ import annotations

from typing import Any

from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)


class TranscriptSessionStore:
    """SDK ``SessionStore`` protocol over :class:`TranscriptEntryRepository`.

    ``platform_session_id`` scopes every operation to one platform session
    (the repository's tenant fence). ``None`` means an unfenced,
    host-privileged view — correct for host-side use; never hand an unfenced
    store to sandbox-reachable code.
    """

    def __init__(
        self,
        repo: TranscriptEntryRepository | None = None,
        *,
        platform_session_id: str | None = None,
    ) -> None:
        self._repo = repo if repo is not None else TranscriptEntryRepository()
        self._platform_session_id = platform_session_id

    @staticmethod
    def _parts(key: dict[str, Any]) -> tuple[str, str, str | None]:
        subpath = key.get("subpath")
        return (
            str(key["project_key"]),
            str(key["session_id"]),
            str(subpath) if subpath is not None else None,
        )

    async def append(self, key: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        project_key, session_id, subpath = self._parts(key)
        # No ``append_id``: the SDK's ``append`` carries no batch identity and
        # this adapter is called in-process, so each call is one append. The
        # repository mints an id for it. A caller whose request can be
        # re-delivered supplies its own instead.
        await self._repo.append_entries(
            project_key,
            session_id,
            subpath,
            entries,
            platform_session_id=self._platform_session_id,
        )

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        project_key, session_id, subpath = self._parts(key)
        return await self._repo.load_entries(
            project_key,
            session_id,
            subpath,
            platform_session_id=self._platform_session_id,
        )

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        return await self._repo.list_sessions(
            project_key, platform_session_id=self._platform_session_id
        )

    async def delete(self, key: dict[str, Any]) -> None:
        project_key, session_id, subpath = self._parts(key)
        await self._repo.delete(project_key, session_id, subpath)

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        return await self._repo.list_subkeys(
            str(key["project_key"]),
            str(key["session_id"]),
            platform_session_id=self._platform_session_id,
        )
