"""BackgroundContinuationMixin — leaf mixin for :class:`SessionKernelService`."""

from __future__ import annotations

import asyncio
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.persistence.repository.backend import (
    get_async_collection,
    run_mongo_with_retry,
)
from astrabox.persistence.repository.session_event_repository import (
    COLLECTION_NAME as SESSION_EVENTS_COLLECTION,
)
from astrabox.core.service.orchestrator.engine.base import (
    ENGINE_MESSAGE_EVENT_TYPE,
    EngineAdapter,
)
from astrabox.core.service.orchestrator.engine.child_runs import (
    canonical_child_run_data,
    public_child_run_id,
)
from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter
from astrabox.core.service.orchestrator.engine_kind_utils import (
    resolve_session_engine_kind,
)
from astrabox.core.service.orchestrator.message_blocks import (
    canonicalize_terminal_message_blocks,
    merge_projected_message_blocks,
)

logger = get_logger(__name__)

_BACKGROUND_CONTINUATION_CONCURRENCY = 5
# How many opened manifests one pass reads, newest first, while looking for
# ones that are still open. Manifests are appended and never updated, so every
# settled one stays in the journal ahead of any older open one; the ceiling
# bounds the read amplification of a long history and is logged when hit.
_BACKGROUND_MANIFEST_SCAN_CEILING = 500


def _discard_settled_child_run(
    terminal: dict[str, str],
    *,
    pending_transcript_refs: set[str],
    pending_engine_refs: set[str],
    transcript_to_engine_ref: dict[str, str],
) -> None:
    """Cross a settled child off the adapter-authored manifest.

    The engine reference is the logical child identity. The transcript
    reference names its engine-owned durable history and must settle with it.
    """
    engine_ref = terminal.get("engine_ref") or ""
    transcript_ref = terminal.get("transcript_ref") or ""
    if engine_ref:
        pending_engine_refs.discard(engine_ref)
        for mapped_transcript_ref, mapped_engine_ref in transcript_to_engine_ref.items():
            if mapped_engine_ref == engine_ref:
                pending_transcript_refs.discard(mapped_transcript_ref)
    if transcript_ref:
        pending_transcript_refs.discard(transcript_ref)
        mapped_engine_ref = transcript_to_engine_ref.get(transcript_ref, "")
        if mapped_engine_ref:
            pending_engine_refs.discard(mapped_engine_ref)


class BackgroundContinuationMixin:
    """Records settled background-subagent results in the session event log.

    The child-run view reads these facts in their own engine scope, not as
    parent assistant speech. This mixin also owns the session status probe.
    """

    @staticmethod
    def _background_materialized_causation_id(opened_event: dict[str, Any]) -> str:
        opened_seq = int(opened_event.get("event_seq") or 0)
        return f"{opened_seq}:background-tasks-materialized"

    async def _get_background_task_state(self, session_id: str) -> dict[str, Any] | None:
        """Read native child activity for both Session status and idle retention.

        Adapters interpret their own folded lifecycle facts. Result collection
        manifests describe pending custody work, not whether an engine is busy.
        This read never reconnects an engine or changes its execution state.
        """
        child_runs = await self._child_run_view.list_child_runs(session_id)
        active_children = [child for child in child_runs if child["active"]]
        if not active_children:
            return None
        opened_events = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="conversation",
            event_type="turn.background_tasks_opened",
            limit=100,
        )
        materialized_events = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="conversation",
            event_type="turn.background_tasks_materialized",
            limit=100,
        )
        materialized_by_opened_seq: dict[int, dict[str, Any]] = {}
        for event in materialized_events:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            opened_seq = int(payload.get("source_opened_event_seq") or 0)
            if opened_seq:
                materialized_by_opened_seq[opened_seq] = dict(event)
        pending_events: list[dict[str, Any]] = []
        for event in opened_events:
            opened_seq = int(event.get("event_seq") or 0)
            materialized_event = materialized_by_opened_seq.get(opened_seq)
            if not isinstance(materialized_event, dict):
                pending_events.append(event)
        first_event = pending_events[0] if pending_events else {}
        return {
            "state": "OPEN",
            "pending_manifest_count": len(pending_events),
            "pending_task_count": len(active_children),
            "opened_event_seq": int(first_event.get("event_seq") or 0) or None,
            "source_turn_id": str(first_event.get("turn_id") or "").strip() or None,
        }

    async def _list_background_task_opened_events(
        self,
        *,
        limit: int = 50,
        skip: int = 0,
    ) -> list[dict[str, Any]]:
        collection = await get_async_collection(SESSION_EVENTS_COLLECTION)

        async def _list() -> list[dict[str, Any]]:
            cursor = (
                collection.find(
                    {
                        "channel": "conversation",
                        "event_type": "turn.background_tasks_opened",
                    }
                )
                .sort([("occurred_at", -1), ("event_seq", -1)])
                .skip(max(0, int(skip)))
                .limit(max(1, int(limit)))
            )
            return [doc async for doc in cursor]

        return await run_mongo_with_retry(
            "session_events.list_background_task_opened_events",
            _list,
        )

    async def _get_background_materialized_event(
        self,
        opened_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        session_id = str(opened_event.get("session_id") or "").strip()
        if not session_id:
            return None
        events = await self._session_events_repo.list_events(
            session_id,
            after_seq=0,
            channel="conversation",
            event_type="turn.background_tasks_materialized",
            causation_id=self._background_materialized_causation_id(opened_event),
            limit=1,
        )
        return dict(events[0]) if events else None

    def _background_continuation_gate_notices(self) -> dict[tuple[str, int], str]:
        notices = getattr(self, "_background_gate_notices", None)
        if notices is None:
            notices = {}
            self._background_gate_notices = notices
        return notices

    def _hold_background_continuation(
        self,
        opened_event: dict[str, Any],
        gate: str,
        *,
        detail: str = "",
        level: str = "info",
    ) -> bool:
        """Name the gate an open manifest is waiting behind, and stay there.

        Returns False so a caller reads as ``return self._hold_...(...)``: the
        early exit and the reason for it are one statement, which is what keeps
        a new gate from being added without one.

        Logged once per gate per manifest, not once per pass. The reconcile loop
        re-reads every open manifest every ``ASTRABOX_RECONCILE_SCAN_INTERVAL_S``
        (10s) and carries up to 50 of them, so a line per early return is 300 a
        minute saying nothing changed — which hides the one line that says
        something did. The gate a manifest is held at is the signal; repeating
        it is not.
        """
        session_id = str(opened_event.get("session_id") or "")
        event_seq = int(opened_event.get("event_seq") or 0)
        notices = self._background_continuation_gate_notices()
        key = (session_id, event_seq)
        if notices.get(key) == gate:
            return False
        notices[key] = gate
        emit = logger.warning if level == "warning" else logger.info
        emit(
            "background continuation: held at gate=%s session=%s turn=%s "
            "opened_event_seq=%s detail=%s",
            gate,
            session_id,
            str(opened_event.get("turn_id") or ""),
            event_seq,
            detail or "-",
        )
        return False

    async def _list_open_background_task_manifests(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The newest ``limit`` opened manifests that have no materialized
        counterpart yet.

        A manifest is settled by a second event, not by an update to the
        first, so the newest page of opened manifests is mostly settled ones
        once a deployment has run for a while; reading only that page would
        leave an older open manifest — a user's background result — waiting
        behind fifty finished ones forever. The scan pages past settled
        manifests until it has ``limit`` open ones or reaches the ceiling.
        """
        open_events: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        scanned = 0
        while len(open_events) < limit and scanned < _BACKGROUND_MANIFEST_SCAN_CEILING:
            page = await self._list_background_task_opened_events(limit=limit, skip=scanned)
            if not page:
                break
            scanned += len(page)
            for opened_event in page:
                key = (str(opened_event.get("session_id") or ""), int(opened_event.get("event_seq") or 0))
                if key in seen:
                    continue
                seen.add(key)
                if await self._get_background_materialized_event(opened_event) is not None:
                    continue
                open_events.append(opened_event)
                if len(open_events) >= limit:
                    break
            if len(page) < limit:
                break
        if scanned >= _BACKGROUND_MANIFEST_SCAN_CEILING and len(open_events) < limit:
            logger.warning(
                "background continuation: scan ceiling reached scanned=%s open=%s; "
                "older open manifests wait for a later pass",
                scanned,
                len(open_events),
            )
        return open_events

    async def _materialize_background_continuations_once(self, *, limit: int = 50) -> int:
        opened_events = await self._list_open_background_task_manifests(limit=limit)
        # The memo covers only the manifests this pass carries. A manifest that
        # settles and later reopens is a manifest to report again, and a note
        # kept past its manifest would silence it.
        notices = self._background_continuation_gate_notices()
        live_keys = {
            (str(event.get("session_id") or ""), int(event.get("event_seq") or 0))
            for event in opened_events
        }
        for stale_key in [key for key in notices if key not in live_keys]:
            del notices[stale_key]
        limiter = asyncio.Semaphore(_BACKGROUND_CONTINUATION_CONCURRENCY)

        async def _materialize_one(opened_event: dict[str, Any]) -> bool:
            async with limiter:
                try:
                    return await self._materialize_background_continuation_event(
                        opened_event
                    )
                except Exception:
                    logger.exception(
                        "background continuation: failed session=%s opened_event_seq=%s",
                        str(opened_event.get("session_id") or ""),
                        opened_event.get("event_seq"),
                    )
                    return False

        results = await asyncio.gather(
            *(_materialize_one(opened_event) for opened_event in opened_events)
        )
        return sum(results)

    async def _materialize_background_continuation_event(
        self,
        opened_event: dict[str, Any],
    ) -> bool:
        materialized_event = await self._get_background_materialized_event(opened_event)
        if isinstance(materialized_event, dict):
            return False

        session_id = str(opened_event.get("session_id") or "").strip()
        parent_turn_id = str(opened_event.get("turn_id") or "").strip()
        payload = opened_event.get("payload")
        if not session_id or not parent_turn_id or not isinstance(payload, dict):
            return self._hold_background_continuation(
                opened_event,
                "manifest_fields_missing",
                detail=f"payload_type={type(payload).__name__}",
            )

        snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        conversation_state = str((snapshot or {}).get("conversation_state") or "").strip()
        if conversation_state != "IDLE":
            return self._hold_background_continuation(
                opened_event,
                "conversation_not_idle",
                detail=f"conversation_state={conversation_state or '<none>'}",
            )

        existing_message = await self._message_view.get_assistant_message_for_turn(
            session_id,
            turn_id=parent_turn_id,
        )
        if not isinstance(existing_message, dict):
            return self._hold_background_continuation(
                opened_event,
                "parent_message_missing",
                level="warning",
            )

        session = await self._sessions_repo.get_session(session_id)
        if not isinstance(session, dict):
            return self._hold_background_continuation(opened_event, "session_row_missing")
        # No sandbox involved: completions are read from two durable engine
        # sources (live SDK messages and SessionStore entries). A background task
        # routinely outlives the box that launched it (parked, replaced, dead),
        # and requiring a live endpoint here would hold every such completion
        # hostage to a box nothing can reach.
        projection = await self._collect_background_continuation_projection(
            session_id=session_id,
            parent_turn_id=parent_turn_id,
            manifest_payload=payload,
            adapter=get_engine_adapter(resolve_session_engine_kind(session)),
            opened_event=opened_event,
        )
        if not isinstance(projection, dict):
            # Which of the projection's own exits was taken is logged there;
            # this line only says the manifest is still held.
            return self._hold_background_continuation(opened_event, "projection_unavailable")
        latest_snapshot = await self._session_snapshots_repo.get_snapshot(session_id)
        latest_state = str((latest_snapshot or {}).get("conversation_state") or "").strip()
        if latest_state != "IDLE":
            # A turn started while the projection was being read. Distinct from
            # the check above: here the work was done and then thrown away.
            return self._hold_background_continuation(
                opened_event,
                "conversation_not_idle_after_projection",
                detail=f"conversation_state={latest_state or '<none>'}",
            )

        event_doc = {
            "session_id": session_id,
            "channel": "conversation",
            "turn_id": parent_turn_id,
            "event_type": "turn.background_tasks_materialized",
            "causation_id": self._background_materialized_causation_id(opened_event),
            "correlation_id": opened_event.get("correlation_id"),
            "payload": {
                "source": "engine_detached_child",
                "source_opened_event_seq": int(opened_event.get("event_seq") or 0),
                "source_opened_causation_id": str(opened_event.get("causation_id") or ""),
                **projection,
            },
        }
        materialized_event, created = await self._session_events_repo.try_claim_event(event_doc)
        if created:
            logger.info(
                "background continuation: materialized session=%s turn=%s "
                "opened_event_seq=%s materialized_event_seq=%s",
                session_id,
                parent_turn_id,
                opened_event.get("event_seq"),
                materialized_event.get("event_seq"),
            )
        return bool(created)

    async def _collect_background_continuation_projection(
        self,
        *,
        session_id: str,
        parent_turn_id: str,
        manifest_payload: dict[str, Any],
        adapter: EngineAdapter,
        opened_event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Project the manifest's settled child runs from durable engine evidence.

        The resident SDK stream covers terminal messages that never append a
        parent transcript row. SessionStore covers the inverse restart case:
        the in-box SDK keeps appending after the host reader disappears. The
        engine matches both sources by content, never position, so this needs
        no turn counter and no live sandbox. A projection is returned only when
        every pending child has settled because the materialized event is
        claimed exactly once and must carry the complete answer.
        """
        pending_transcript_refs: set[str] = set()
        if isinstance(manifest_payload.get("transcript_refs"), list):
            pending_transcript_refs = {
                str(item).strip()
                for item in manifest_payload.get("transcript_refs", [])
                if str(item).strip()
            }
        pending_engine_refs: set[str] = set()
        if isinstance(manifest_payload.get("engine_refs"), list):
            pending_engine_refs = {
                str(item).strip()
                for item in manifest_payload.get("engine_refs", [])
                if str(item).strip()
            }
        raw_transcript_to_engine = manifest_payload.get("transcript_to_engine_ref")
        if not isinstance(raw_transcript_to_engine, dict):
            raw_transcript_to_engine = {}
        transcript_to_engine_ref = {
            str(key).strip(): str(value).strip()
            for key, value in raw_transcript_to_engine.items()
            if str(key).strip() and str(value).strip()
        }
        raw_control_to_engine = manifest_payload.get("control_to_engine_ref")
        if not isinstance(raw_control_to_engine, dict):
            raw_control_to_engine = {}
        control_to_engine_ref = {
            str(key).strip(): str(value).strip()
            for key, value in raw_control_to_engine.items()
            if str(key).strip() and str(value).strip()
        }
        raw_activations = manifest_payload.get("activation_to_engine_ref")
        if not isinstance(raw_activations, dict):
            raise RuntimeError("detached-child manifest lacks activation references")
        activation_to_engine_ref = {
            str(key).strip(): str(value).strip()
            for key, value in raw_activations.items()
            if str(key).strip() and str(value).strip()
        }
        if not pending_engine_refs:
            self._hold_background_continuation(opened_event, "manifest_names_no_tasks")
            return None
        if set(transcript_to_engine_ref) != pending_transcript_refs or set(
            transcript_to_engine_ref.values()
        ) != pending_engine_refs:
            raise RuntimeError("detached-child manifest has inconsistent identity mappings")
        if set(activation_to_engine_ref.values()) != pending_engine_refs:
            raise RuntimeError("detached-child manifest has inconsistent activation mappings")

        engine_items = await self._load_background_engine_messages(
            session_id=session_id,
            engine_kind=adapter.engine_kind,
        )
        transcript_items = (
            await self._transcript_entries_repo.load_subpath_entries_by_platform_session(
                session_id,
                subpath=None,
            )
        )
        if not engine_items and not transcript_items:
            self._hold_background_continuation(opened_event, "durable_evidence_empty")
            return None

        records = self._background_terminal_records(
            [engine_items, transcript_items],
            adapter=adapter,
            transcript_refs=pending_transcript_refs,
            engine_refs=pending_engine_refs,
            transcript_to_engine_ref=transcript_to_engine_ref,
            control_to_engine_ref=control_to_engine_ref,
            activation_to_engine_ref=activation_to_engine_ref,
        )
        if not records:
            self._hold_background_continuation(
                opened_event,
                "no_terminal_records_yet",
                detail=f"pending={len(pending_engine_refs)}",
            )
            return None

        remaining_transcript_refs = set(pending_transcript_refs)
        remaining_engine_refs = set(pending_engine_refs)
        for record in records.values():
            _discard_settled_child_run(
                record,
                pending_transcript_refs=remaining_transcript_refs,
                pending_engine_refs=remaining_engine_refs,
                transcript_to_engine_ref=transcript_to_engine_ref,
            )
        if remaining_transcript_refs or remaining_engine_refs:
            # The ordinary "still running" answer, and the one the symptom
            # "polling with nothing to show" almost always resolves to.
            self._hold_background_continuation(
                opened_event,
                "tasks_still_pending",
                detail=f"remaining={len(remaining_engine_refs)}",
            )
            return None

        projected_blocks = self._background_blocks_from_records(
            records,
            session_id=session_id,
            block_scope=parent_turn_id,
            engine_kind=adapter.engine_kind,
        )
        transcript_blocks = await self._load_detached_child_run_transcript_blocks(
            session_id=session_id,
            records=records,
            adapter=adapter,
        )
        return {
            "blocks": canonicalize_terminal_message_blocks(
                merge_projected_message_blocks(
                    projected_blocks,
                    transcript_blocks,
                )
            ),
            "remaining_transcript_refs": [],
            "remaining_engine_refs": [],
        }

    async def _load_background_engine_messages(
        self,
        *,
        session_id: str,
        engine_kind: str,
    ) -> list[dict[str, Any]]:
        """Read every durable raw message for one session and engine.

        The terminal event may commit just before the launching turn appends
        ``turn.background_tasks_opened``: the resident RunnerLink persists it
        before routing, while the turn worker processes Result concurrently.
        Content ids, not journal position, therefore select the matching task.
        """

        messages: list[dict[str, Any]] = []
        after_seq = 0
        while True:
            events = await self._session_events_repo.list_events(
                session_id,
                after_seq=after_seq,
                channel="conversation",
                event_type=ENGINE_MESSAGE_EVENT_TYPE,
                limit=500,
            )
            if not events:
                return messages
            for event in events:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    raise RuntimeError("durable engine message has no payload object")
                event_engine_kind = str(payload.get("engine_kind") or "").strip()
                if not event_engine_kind:
                    raise RuntimeError("durable engine message has no engine_kind")
                if event_engine_kind != engine_kind:
                    continue
                message = payload.get("message")
                if not isinstance(message, dict):
                    raise RuntimeError("durable engine message has no raw message object")
                messages.append(dict(message))
            next_seq = max(int(event.get("event_seq") or 0) for event in events)
            if next_seq <= after_seq:
                raise RuntimeError("durable engine message pagination did not advance")
            if len(events) < 500:
                return messages
            after_seq = next_seq

    @staticmethod
    def _background_terminal_records(
        raw_sources: list[list[dict[str, Any]]],
        *,
        adapter: EngineAdapter,
        transcript_refs: set[str],
        engine_refs: set[str],
        transcript_to_engine_ref: dict[str, str],
        control_to_engine_ref: dict[str, str],
        activation_to_engine_ref: dict[str, str],
    ) -> dict[str, dict[str, str]]:
        """First result-bearing terminal for each declared child activation.

        A terminal update can precede its result-bearing notification. Treat
        the latter as enrichment only when identity and status are unchanged;
        once a record carries a result, later re-settlements cannot replace it.
        """
        records: dict[str, dict[str, str]] = {}
        # Each source owns its chronology. A later SDK activation must not
        # identify an earlier, id-less terminal at the start of SessionStore.
        for source in raw_sources:
            observed_activations: dict[str, str] = {}
            for raw in source:
                record = adapter.detached_child_run_terminal(
                    raw,
                    transcript_refs=transcript_refs,
                    engine_refs=engine_refs,
                    transcript_to_engine_ref=transcript_to_engine_ref,
                    control_to_engine_ref=control_to_engine_ref,
                    activation_to_engine_ref=activation_to_engine_ref,
                    observed_activations=observed_activations,
                )
                if record is None:
                    continue
                key = str(record.get("engine_ref") or "")
                if not key:
                    continue
                existing = records.get(key)
                if existing is None:
                    records[key] = record
                    continue
                same_identity = all(
                    not existing.get(field) or not record.get(field) or existing[field] == record[field]
                    for field in ("transcript_ref", "engine_ref", "control_ref")
                )
                if (
                    same_identity
                    and not str(existing.get("result") or "").strip()
                    and str(record.get("result") or "").strip()
                ):
                    records[key] = record
        return records

    async def _load_detached_child_run_transcript_blocks(
        self,
        *,
        session_id: str,
        records: dict[str, dict[str, str]],
        adapter: EngineAdapter,
    ) -> list[dict[str, Any]]:
        """Replay each settled child's complete engine-owned transcript tree."""
        scopes = await self._transcript_entries_repo.list_scopes_by_platform_session(session_id)
        raw_scopes: list[dict[str, Any]] = []
        for scope in scopes:
            subpath = scope.get("subpath")
            if not isinstance(subpath, str) or not subpath:
                continue
            entries = await self._transcript_entries_repo.load_subpath_entries_by_platform_session(
                session_id,
                subpath=subpath,
            )
            raw_scopes.append({"subpath": subpath, "entries": entries})

        blocks: list[dict[str, Any]] = []
        for record in records.values():
            transcript_ref = str(record.get("transcript_ref") or "").strip()
            engine_ref = str(record.get("engine_ref") or "").strip()
            if not transcript_ref or not engine_ref:
                continue
            blocks = merge_projected_message_blocks(
                blocks,
                adapter.detached_child_run_transcript_blocks(
                    raw_scopes,
                    root_transcript_ref=transcript_ref,
                    engine_ref=engine_ref,
                ),
            )
        return blocks

    @staticmethod
    def _background_blocks_from_records(
        records: dict[str, dict[str, str]],
        *,
        session_id: str,
        block_scope: str,
        engine_kind: str,
    ) -> list[dict[str, Any]]:
        """Terminal lifecycle blocks in their child-run scopes, one task each.

        Block ids embed ``block_scope`` (the parent platform turn id) plus the
        public child-run id, so re-materializing the same manifest merges into
        the existing blocks instead of duplicating them. A record with no
        child-run id gets no block. Summary stays lifecycle metadata; messages
        come from the engine-owned transcript with their native identities.
        """
        blocks: list[dict[str, Any]] = []
        for record in records.values():
            engine_ref = str(record.get("engine_ref") or "").strip()
            if not engine_ref:
                continue
            child_run_id = public_child_run_id(
                session_id=session_id,
                engine_kind=engine_kind,
                engine_ref=engine_ref,
            )
            lifecycle_data: dict[str, Any] = {
                "kind": "lifecycle",
                "engineRef": engine_ref,
                "event": str(record.get("event") or ""),
                "engineEvent": str(record.get("engine_event") or ""),
                "engineStatus": str(record.get("engine_status") or ""),
                "operations": [],
            }
            control_ref = str(record.get("control_ref") or "").strip()
            if control_ref:
                lifecycle_data["controlRef"] = control_ref
            summary = str(record.get("summary") or "").strip()
            if summary:
                lifecycle_data["summary"] = summary
            lifecycle_data = canonical_child_run_data(
                lifecycle_data,
                engine_kind=engine_kind,
            )
            blocks.append(
                {
                    "type": "subagent",
                    "id": (f"subagent:lifecycle:background:{block_scope}:{child_run_id}"),
                    "data": lifecycle_data,
                }
            )
        return blocks
