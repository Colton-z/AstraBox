"""Admin operations: list sessions, kill session, system overview."""

from __future__ import annotations

import asyncio
import json
import os
import resource
import socket
import threading
from collections.abc import AsyncIterator
from typing import Any

from astrabox.core.service.orchestrator.session_message_view import SessionMessageView
from astrabox.persistence.repository import (
    SessionEventRepository,
    SessionRepository,
)
from astrabox.persistence.repository.session_snapshot_repository import (
    SessionSnapshotRepository,
)
from astrabox.persistence.repository.user_profile_repository import UserProfileRepository
from astrabox.persistence.repository.transcript_entry_repository import (
    TranscriptEntryRepository,
)
from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent_access import (
    can_manage_agent,
    can_view_agent,
    is_platform_admin,
)
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.common.utils.time_utils import parse_iso, utcnow_iso
from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.sandbox_names import keep_name_updates
from astrabox.core.service.orchestrator.admin_error_capture import list_runtime_errors
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
)
from astrabox.core.service.orchestrator.runtime_binding import (
    is_assistant_user_conversation,
    runtime_subject_kind,
)
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.core.service.orchestrator.agent_config_service import AgentConfigService

logger = get_logger(__name__)


def _normalize_admin_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_admin_value(item) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) == "_id":
                continue
            normalized[str(key)] = _normalize_admin_value(item)
        return normalized
    return str(value)


def _truncate_admin_text(value: Any, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 3, 0)]}..."


def _extract_content_preview(content: Any) -> str:
    if isinstance(content, str):
        return _truncate_admin_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
                continue
            event = item.get("event")
            tool_name = str(
                item.get("name")
                or item.get("tool_name")
                or item.get("type")
                or (event.get("type") if isinstance(event, dict) else "")
                or ""
            ).strip()
            if tool_name:
                parts.append(f"[{tool_name}]")
        return _truncate_admin_text(" ".join(parts))
    if isinstance(content, dict):
        text = str(content.get("text") or content.get("content") or "").strip()
        if text:
            return _truncate_admin_text(text)
    normalized = _normalize_admin_value(content)
    if normalized in (None, "", [], {}):
        return ""
    return _truncate_admin_text(normalized)


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                parts.append(text)
                continue
            event = item.get("event")
            tool_name = str(
                item.get("name")
                or item.get("tool_name")
                or item.get("type")
                or (event.get("type") if isinstance(event, dict) else "")
                or ""
            ).strip()
            if tool_name:
                parts.append(f"[{tool_name}]")
        return "\n".join(parts)
    if isinstance(content, dict):
        text = str(content.get("text") or content.get("content") or "").strip()
        if text:
            return text
    normalized = _normalize_admin_value(content)
    if normalized in (None, "", [], {}):
        return ""
    return str(normalized)


def _extract_content_events(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []

    events: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        raw_event = item.get("event")
        normalized_event = _normalize_admin_value(raw_event) if isinstance(raw_event, dict) else None
        if normalized_event is None:
            continue

        event_type = str(
            normalized_event.get("type")
            or item.get("type")
            or ""
        ).strip()
        event_name = str(
            item.get("name")
            or item.get("tool_name")
            or normalized_event.get("name")
            or ""
        ).strip()
        event_entry = {
            "type": event_type,
            "name": event_name,
            "event": normalized_event,
        }
        if not event_entry["name"]:
            event_entry.pop("name", None)
        if not event_entry["type"]:
            event_entry.pop("type", None)
        events.append(event_entry)
    return events


def _normalize_export_block(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw = _normalize_admin_value(value)
    block_type = str(raw.get("type") or "").strip()
    if not block_type:
        return None

    if block_type == "text":
        return {
            "type": "text",
            "text": str(raw.get("text") or ""),
        }
    if block_type == "thinking":
        return {
            "type": "thinking",
            "thinking": str(raw.get("thinking") or ""),
        }
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or ""),
            "input": raw.get("input") if isinstance(raw.get("input"), dict) else {},
        }
    if block_type == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": str(raw.get("tool_use_id") or ""),
            "content": str(raw.get("content") or ""),
            "is_error": bool(raw.get("is_error")),
        }
    if block_type == "result":
        entry = {
            "type": "result",
            "result": str(raw.get("result") or ""),
        }
        for key in ("duration_ms", "duration_api_ms", "total_cost_usd", "num_turns", "usage"):
            value = raw.get(key)
            if value not in (None, "", []):
                entry[key] = value
        return entry
    return raw


def _extract_message_blocks(raw: dict[str, Any]) -> list[dict[str, Any]]:
    for candidate in (raw.get("blocks"), raw.get("content")):
        if not isinstance(candidate, list):
            continue
        blocks = [
            normalized
            for normalized in (_normalize_export_block(item) for item in candidate)
            if normalized is not None
        ]
        if blocks:
            return blocks
    return []


def _normalize_export_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe_export_blocks(
    text: str,
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    canonical_text = _normalize_export_text(text)
    deduped: list[dict[str, Any]] = []

    for block in blocks:
        item = dict(block)
        block_type = str(item.get("type") or "").strip()

        if block_type == "text":
            if canonical_text and _normalize_export_text(item.get("text")) == canonical_text:
                continue

        if block_type == "result":
            result_text = _normalize_export_text(item.get("result"))
            if canonical_text and result_text == canonical_text:
                item.pop("result", None)

        deduped.append(item)

    return deduped


class AdminService:
    def __init__(
        self,
        *,
        sessions_repo: SessionRepository,
        message_view: SessionMessageView,
        session_events_repo: SessionEventRepository,
        agent_config: AgentConfigService,
        runtime_manager: RemoteAgentRuntimeManager,
        sanitize_session,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._message_view = message_view
        self._session_events_repo = session_events_repo
        self._user_profiles_repo = UserProfileRepository()
        self._transcript_entries_repo = TranscriptEntryRepository()
        self._session_snapshots_repo = SessionSnapshotRepository()
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._sanitize_session = sanitize_session

    @staticmethod
    def _derive_conversation_state(snapshot: dict[str, Any] | None) -> str | None:
        """The state a user is shown for this session, or ``None`` if unknown.

        Delegates to the same pure function the user-facing reads use, so the
        two surfaces cannot drift into two different derivations of one answer.
        A session with no snapshot yet has no conversation state — reporting a
        default here would invent agreement rather than report its absence.
        """
        if not isinstance(snapshot, dict):
            return None
        from astrabox.core.service.orchestrator.session_kernel.service_mixins.session_read import (
            SessionReadRenderingMixin,
        )

        return str(SessionReadRenderingMixin.derive_ui_state(snapshot)["state"])

    async def _assert_can_manage_session(self, user, session: dict[str, Any]) -> None:
        """Authorize ``user`` against the Session's durable owner.

        Agent Sessions inherit their Agent's management ACL. Assistant Sessions
        inherit their Assistant owner's identity; the Environment name stored in
        ``agent_id`` is runtime configuration, not an Agent ACL key. Platform
        administrators may manage either subject. Every per-Session admin
        view/action funnels through this gate and fails loud with 403.
        """
        viewer = getattr(user, "user_id", "") if user is not None else ""
        roles = getattr(user, "roles", ()) if user is not None else ()
        try:
            subject_kind = runtime_subject_kind(session)
        except ValueError:
            subject_kind = None
        if subject_kind == "assistant_workspace":
            owner = str(session.get("user_id") or "").strip()
            if is_platform_admin(roles) or (viewer and viewer == owner):
                return
            raise APIError(
                code="FORBIDDEN",
                message="you are not the owner or a platform admin of this Assistant session",
                status_code=403,
            )

        agent_id = str(session.get("agent_id") or "").strip()
        access_doc = (
            await self._agent_config.get_agent_access_doc(agent_id)
            if agent_id
            else None
        )
        if not access_doc or not can_manage_agent(access_doc, viewer, roles):
            raise APIError(
                code="FORBIDDEN",
                message="you are not an owner or admin of this agent",
                status_code=403,
            )

    async def _nick_map_for_sessions(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        user_ids = sorted({
            str(row.get("user_id") or "").strip()
            for row in rows
            if str(row.get("user_id") or "").strip()
        })
        if not user_ids:
            return {}
        return await self._user_profiles_repo.batch_get_display_names(user_ids)

    @staticmethod
    def _apply_user_display(row: dict[str, Any], nick_map: dict[str, str]) -> None:
        user_id = str(row.get("user_id") or "").strip()
        display_name = str(row.get("display_name") or nick_map.get(user_id) or "").strip()
        row["display_name"] = display_name or user_id or None

    @classmethod
    def _persisted_runtime_versions(cls, session: dict[str, Any]) -> dict[str, Any]:
        identity = session.get("runtime_identity") if isinstance(session.get("runtime_identity"), dict) else {}
        workspace_ref = session.get("workspace_ref") if isinstance(session.get("workspace_ref"), dict) else {}
        stage_evidence = identity.get("stage_evidence") if isinstance(identity.get("stage_evidence"), dict) else {}
        return {
            "engine_kind": (
                str(session.get("engine_kind") or "").strip()
                or str(workspace_ref.get("engine_kind") or "").strip()
                or None
            ),
            "runtime_identity_status": str(identity.get("status") or "").strip() or None,
            "bootstrap_transport": str(stage_evidence.get("bootstrap_transport") or "").strip() or None,
        }

    def _summarize_runtime_versions(
        self,
        session: dict[str, Any],
        *,
        has_local_runtime: bool,
    ) -> dict[str, Any]:
        persisted = self._persisted_runtime_versions(session)
        return {
            **persisted,
            "has_local_runtime": bool(has_local_runtime),
        }

    @staticmethod
    def _fd_count() -> int | None:
        for path in ("/proc/self/fd", "/dev/fd"):
            try:
                return len(os.listdir(path))
            except Exception:
                continue
        return None

    @staticmethod
    def _current_rss_mb() -> float | None:
        status_path = "/proc/self/status"
        try:
            with open(status_path, encoding="utf-8") as fp:
                for line in fp:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            return round(int(parts[1]) / 1024, 1)
        except Exception:
            return None
        return None

    @staticmethod
    def _max_rss_mb() -> float | None:
        try:
            raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        except Exception:
            return None
        if raw <= 0:
            return None
        if raw > 10_000_000:
            return round(raw / 1024 / 1024, 1)
        return round(raw / 1024, 1)

    @staticmethod
    def _severity_for_process_health(
        *,
        thread_count: int,
        fd_count: int | None,
        fd_soft_limit: int | None,
        pending_task_count: int,
    ) -> str:
        fd_ratio = (
            float(fd_count) / float(fd_soft_limit)
            if fd_count is not None and fd_soft_limit not in (None, 0)
            else 0.0
        )
        if thread_count >= 500 or fd_ratio >= 0.9 or pending_task_count >= 5000:
            return "error"
        if thread_count >= 200 or fd_ratio >= 0.75 or pending_task_count >= 1000:
            return "warning"
        return "ok"

    @staticmethod
    def _runtime_map(runtime_manager: Any) -> dict[str, Any]:
        runtimes_obj = getattr(runtime_manager, "_runtimes", None)
        if not isinstance(runtimes_obj, dict):
            runtimes_obj = getattr(runtime_manager, "runtimes", {})
        return dict(runtimes_obj) if isinstance(runtimes_obj, dict) else {}

    @staticmethod
    def _runtime_rows(runtimes: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for session_id, runtime in sorted(runtimes.items()):
            current_task = getattr(runtime, "current_task", None)
            lock = getattr(runtime, "lock", None)
            rows.append({
                "session_id": str(session_id),
                "sandbox_id": str(getattr(runtime, "sandbox_id", "") or "") or None,
                "current_task_done": (
                    bool(current_task.done())
                    if isinstance(current_task, asyncio.Task)
                    else None
                ),
                "lock_locked": bool(lock.locked()) if isinstance(lock, asyncio.Lock) else None,
                "owner_loop_running": (
                    bool(getattr(runtime, "owner_loop").is_running())
                    if getattr(runtime, "owner_loop", None) is not None
                    else None
                ),
            })
        return rows

    def admin_process_health(self) -> dict[str, Any]:
        threads = threading.enumerate()
        try:
            tasks = list(asyncio.all_tasks(asyncio.get_running_loop()))
        except RuntimeError:
            tasks = []
        pending_tasks = [task for task in tasks if not task.done()]
        fd_count = self._fd_count()
        try:
            fd_soft_limit, fd_hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        except Exception:
            fd_soft_limit, fd_hard_limit = None, None
        runtimes = self._runtime_map(self._runtime_manager)
        runtime_rows = self._runtime_rows(runtimes)
        severity = self._severity_for_process_health(
            thread_count=len(threads),
            fd_count=fd_count,
            fd_soft_limit=fd_soft_limit if isinstance(fd_soft_limit, int) else None,
            pending_task_count=len(pending_tasks),
        )
        try:
            load_avg = list(os.getloadavg())
        except Exception:
            load_avg = []
        return {
            "severity": severity,
            "machine_id": socket.gethostname(),
            "pid": os.getpid(),
            "captured_at": utcnow_iso(),
            "threads": {
                "count": len(threads),
                "non_daemon_count": sum(1 for thread in threads if not thread.daemon),
                "items": [
                    {
                        "name": thread.name,
                        "daemon": bool(thread.daemon),
                        "alive": bool(thread.is_alive()),
                        "ident": thread.ident,
                        "native_id": getattr(thread, "native_id", None),
                    }
                    for thread in threads[:200]
                ],
            },
            "file_descriptors": {
                "count": fd_count,
                "soft_limit": fd_soft_limit,
                "hard_limit": fd_hard_limit,
                "usage_ratio": (
                    round(float(fd_count) / float(fd_soft_limit), 4)
                    if fd_count is not None and fd_soft_limit not in (None, 0)
                    else None
                ),
            },
            "asyncio": {
                "task_count": len(tasks),
                "pending_task_count": len(pending_tasks),
                "sample_pending_tasks": [
                    {
                        "name": task.get_name(),
                        "coro": str(task.get_coro()),
                    }
                    for task in pending_tasks[:100]
                ],
            },
            "memory": {
                "rss_mb": self._current_rss_mb(),
                "max_rss_mb": self._max_rss_mb(),
            },
            "load_avg": load_avg,
            "runtimes": {
                "count": len(runtime_rows),
                "current_task_count": sum(1 for row in runtime_rows if row["current_task_done"] is False),
                "locked_count": sum(1 for row in runtime_rows if row["lock_locked"] is True),
                "items": runtime_rows,
            },
        }

    async def _manageable_template_names(self, user) -> set[str]:
        """Agent names the user creates or co-administers."""
        viewer = getattr(user, "user_id", "") if user is not None else ""
        if not viewer:
            return set()
        docs = await self._agent_config.list_agent_access_docs()
        return {
            str(d.get("name") or "").strip()
            for d in docs
            if str(d.get("name") or "").strip() and can_manage_agent(d, viewer, user.roles)
        }

    async def admin_navigation_summary(self, user) -> dict[str, int]:
        """Counts for the management rail, under each collection's read scope."""
        docs, environments = await asyncio.gather(
            self._agent_config.list_agent_access_docs(),
            self._agent_config.count_environment_configs(),
        )
        viewer = getattr(user, "user_id", "") if user is not None else ""
        roles = getattr(user, "roles", ()) if user is not None else ()
        manageable_names = (
            sorted(
                {
                    name
                    for doc in docs
                    if can_manage_agent(doc, viewer, roles)
                    if (name := str(doc.get("name") or "").strip())
                }
            )
            if viewer
            else []
        )
        sessions = await self._sessions_repo.count_all_sessions(template_names=manageable_names)
        return {
            "agents": sum(can_view_agent(doc, viewer, roles) for doc in docs),
            "environments": environments,
            "sessions": sessions,
        }

    async def admin_list_sessions_page(
        self,
        user,
        *,
        page: int = 1,
        page_size: int = 50,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """One page of the sessions this user administers, with the real total.

        The owner scope is a query term, not a pass over the result. Fetching
        `limit` rows and then keeping the ones under a manageable agent would,
        at a hundred agents and a thousand conversations a day, return an
        arbitrary sliver of one page — and the page would say nothing about how
        much it had dropped, so the console could not tell "these are all of
        them" from "these are the few that survived a filter".

        `total_items` is counted at the source for the same reason: it is a
        number about the collection, and deriving it from a page would print the
        page size no matter how many exist.
        """
        manageable = sorted(await self._manageable_template_names(user))
        # An empty scope is a real answer, and `[]` says it in the query: match
        # nothing. Never `None`, which the repository reads as unscoped.
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        total = await self._sessions_repo.count_all_sessions(
            template_names=manageable, agent_id=agent_id, since=since, until=until
        )
        rows = await self._sessions_repo.list_all_sessions(
            limit=page_size,
            skip=(page - 1) * page_size,
            template_names=manageable,
            agent_id=agent_id,
            since=since,
            until=until,
        )
        items = await self._enrich_session_rows(rows)
        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_items": total,
                "total_pages": (total + page_size - 1) // page_size if page_size else 0,
            },
        }

    async def admin_session_totals(self, user) -> dict[str, Any]:
        """The overview's session facts: how many, and how they are distributed.

        The total includes every aggregate bucket before the distribution is
        narrowed to modelled states. An unknown state can therefore be absent
        from ``by_state`` without silently shrinking ``total``.
        """
        manageable = sorted(await self._manageable_template_names(user))
        return await self._sessions_repo.count_session_totals(template_names=manageable)

    async def admin_iter_manageable_sessions(
        self,
        user,
        *,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        batch_size: int = 200,
    ) -> AsyncIterator[dict[str, Any]]:
        """Every session matching the filter, walked a page at a time.

        The export reads this rather than a capped list: an operator asking for
        one agent's day should get that day, not its first five hundred rows.
        Rows only — enrichment is per-session work the export does not need.
        """
        skip = 0
        manageable = sorted(await self._manageable_template_names(user))
        while True:
            rows = await self._sessions_repo.list_all_sessions(
                limit=batch_size,
                skip=skip,
                template_names=manageable,
                agent_id=agent_id,
                since=since,
                until=until,
            )
            if not rows:
                return
            for row in rows:
                yield row
            if len(rows) < batch_size:
                return
            skip += batch_size

    async def admin_list_agent_sessions(
        self, user, agent_id: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        """All conversations under one Agent, keyed by ``agent_id``.
        Caller must manage the agent."""
        target = str(agent_id or "").strip()
        access_doc = (
            await self._agent_config.get_agent_access_doc(target) if target else None
        )
        viewer = getattr(user, "user_id", "") if user is not None else ""
        if not access_doc or not can_manage_agent(access_doc, viewer, user.roles):
            raise APIError(
                code="FORBIDDEN",
                message="you are not an owner or admin of this agent",
                status_code=403,
            )
        rows = await self._sessions_repo.find_sessions_by_agent_id(target, limit=limit)
        return await self._enrich_session_rows(rows)

    async def _enrich_session_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach user display, duration, and runtime-version summary to rows."""
        nick_map = await self._nick_map_for_sessions(rows)
        runtimes: dict[str, Any] = {}
        for row in rows:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            runtime = self._runtime_manager.get_runtime(
                session_id,
                sandbox_id=str(row.get("sandbox_id") or "").strip() or None,
            )
            if runtime is not None:
                runtimes[session_id] = runtime
        result = []
        for row in rows:
            clean = self._sanitize_session(row)
            self._apply_user_display(clean, nick_map)
            if clean.get("created_at") and clean.get("updated_at"):
                try:
                    created = parse_iso(clean["created_at"])
                    updated = parse_iso(clean["updated_at"])
                    clean["duration_seconds"] = int((updated - created).total_seconds())
                except Exception:
                    pass
            session_id = str(clean.get("session_id") or "")
            runtime = runtimes.get(session_id)
            clean["has_local_runtime"] = runtime is not None
            clean["runtime_versions"] = self._summarize_runtime_versions(
                row,
                has_local_runtime=runtime is not None,
            )
            result.append(clean)
        return result


    async def admin_list_global_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._sessions_repo.list_all_sessions(limit=limit)
        nick_map = await self._nick_map_for_sessions(rows)
        result = []
        for row in rows:
            clean = self._sanitize_session(row)
            self._apply_user_display(clean, nick_map)
            if clean.get("created_at") and clean.get("updated_at"):
                try:
                    created = parse_iso(clean["created_at"])
                    updated = parse_iso(clean["updated_at"])
                    clean["duration_seconds"] = int((updated - created).total_seconds())
                except Exception:
                    pass
            clean["runtime_versions"] = self._summarize_runtime_versions(
                row,
                has_local_runtime=False,
            )
            result.append(clean)
        return result

    @staticmethod
    def _error_sort_key(error: dict[str, Any]) -> str:
        return str(
            error.get("updated_at")
            or error.get("failed_at")
            or error.get("blocked_at")
            or error.get("created_at")
            or ""
        )

    def _summarize_session_error(
        self,
        session: dict[str, Any],
        *,
        nick_map: dict[str, str],
    ) -> dict[str, Any] | None:
        last_error = str(session.get("last_error") or "").strip()
        runtime_unavailable = bool(session.get("runtime_unavailable"))
        state = str(session.get("state") or "").strip()
        if not last_error and not runtime_unavailable:
            return None
        session_id = str(session.get("session_id") or "").strip()
        display_row = dict(session)
        self._apply_user_display(display_row, nick_map)
        return {
            "source": "session",
            "severity": "error" if runtime_unavailable or last_error else "warning",
            "state": state or None,
            "session_id": session_id,
            "turn_id": str(session.get("current_turn_id") or "").strip() or None,
            "sandbox_id": str(session.get("sandbox_id") or "").strip() or None,
            "user_id": str(session.get("user_id") or "").strip() or None,
            "display_name": str(display_row.get("display_name") or "").strip() or None,
            "template_name": str(session.get("template_name") or "").strip() or None,
            "message": _truncate_admin_text(last_error or state, limit=500),
            "last_error": last_error or None,
            "runtime_unavailable": runtime_unavailable,
            "created_at": str(session.get("created_at") or ""),
            "updated_at": str(session.get("updated_at") or ""),
            "runtime_versions": self._summarize_runtime_versions(
                session,
                has_local_runtime=False,
            ),
        }

    @staticmethod
    def _summarize_runtime_error(doc: dict[str, Any]) -> dict[str, Any]:
        message = str(doc.get("message") or "").strip()
        return {
            "source": "runtime",
            "severity": (str(doc.get("severity") or "error").lower() or "error"),
            "message": _truncate_admin_text(message, limit=500),
            "last_error": message or None,
            "error_type": str(doc.get("error_type") or "").strip() or None,
            "location": str(doc.get("location") or "").strip() or None,
            "logger_name": str(doc.get("logger") or "").strip() or None,
            "count": int(doc.get("count") or 0),
            "traceback": (str(doc.get("traceback") or "")[:2000] or None),
            "created_at": str(doc.get("first_seen") or ""),
            "updated_at": str(doc.get("last_seen") or ""),
        }

    async def admin_list_errors(self, limit: int = 200) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 200), 500))
        session_rows = await self._sessions_repo.list_all_sessions(limit=500)
        sessions_by_id = {
            str(row.get("session_id") or "").strip(): row
            for row in session_rows
            if str(row.get("session_id") or "").strip()
        }
        nick_map = await self._nick_map_for_sessions(list(sessions_by_id.values()))
        errors: list[dict[str, Any]] = []
        for row in sessions_by_id.values():
            item = self._summarize_session_error(row, nick_map=nick_map)
            if item is not None:
                errors.append(item)

        # Runtime errors captured at the logging layer (e.g. MCP runtime failures)
        # that never land in session state — see admin_error_capture.
        for doc in await list_runtime_errors(limit=safe_limit):
            errors.append(self._summarize_runtime_error(doc))

        errors.sort(key=self._error_sort_key, reverse=True)
        limited = errors[:safe_limit]
        counts: dict[str, int] = {
            "total": len(limited),
            "session": 0,
            "error": 0,
            "warning": 0,
        }
        for item in limited:
            source = str(item.get("source") or "")
            severity = str(item.get("severity") or "")
            if source in counts:
                counts[source] += 1
            if severity in counts:
                counts[severity] += 1
        return {
            "errors": limited,
            "counts": counts,
            "limit": safe_limit,
        }

    async def admin_get_session_detail(self, user, session_id: str) -> dict[str, Any]:
        """One session for an operator, carrying both answers to "what state?".

        ``state`` is the session's **lifecycle**, read from the ``sessions``
        row: is this session alive and able to accept work. ``conversation_state``
        is what a **user** is shown, derived from the conversation snapshot: does
        the last turn have proof it finished.

        They answer different questions and can legitimately disagree — a
        session whose box is ready but whose last terminal frame was lost is
        READY and PROCESSING at the same time, and both are true. Carrying both
        here is what makes that difference readable: an operator holding one
        value has no way to tell a real disagreement from two definitions, and
        the second value is otherwise only derived on user-facing surfaces.
        """
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)
        clean = self._sanitize_session(session)
        clean["conversation_state"] = self._derive_conversation_state(
            await self._session_snapshots_repo.get_snapshot(session_id)
        )
        self._apply_user_display(clean, await self._nick_map_for_sessions([session]))
        runtime = self._runtime_manager.get_runtime(
            session_id,
            sandbox_id=str(session.get("sandbox_id") or "").strip() or None,
        )
        clean["has_local_runtime"] = runtime is not None
        clean["runtime_versions"] = self._summarize_runtime_versions(
            session,
            has_local_runtime=runtime is not None,
        )

        # Reading configured extensions is not permission to start a runtime.
        template = await self._agent_config.resolve_session_harness(
            session, require_enabled_environment=False
        )
        if template:
            clean["template_skills"] = list(template.skills) if template.skills else []
            mcp_servers = template.mcp_servers or {}
            clean["template_mcp_servers"] = list(mcp_servers.keys()) if isinstance(mcp_servers, dict) else []
            clean["template_mcp_config"] = template.mcp_servers
        else:
            clean["template_skills"] = []
            clean["template_mcp_servers"] = []
            clean["template_mcp_config"] = None

        # The sandbox opens its own MCP connections, so this server holds no
        # registry of them and has none to expose.
        clean["mcp_connections"] = []
        return clean

    @staticmethod
    def _summarize_message(
        message: dict[str, Any],
        *,
        include_raw: bool = True,
        flatten_content: bool = False,
    ) -> dict[str, Any]:
        raw = _normalize_admin_value(message)
        content = raw.get("content")
        blocks = _extract_message_blocks(raw)
        result = {
            "message_id": str(raw.get("message_id") or raw.get("id") or ""),
            "turn_id": str(raw.get("turn_id") or ""),
            "role": str(raw.get("role") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
        }
        if flatten_content:
            text = _extract_content_text(content)
            deduped_blocks = _dedupe_export_blocks(text, blocks)
            if text:
                result["text"] = text
            events = _extract_content_events(content)
            if events:
                result["events"] = events
            if deduped_blocks:
                result["blocks"] = deduped_blocks
            interaction_response = raw.get("interaction_response")
            if isinstance(interaction_response, dict):
                result["interaction_response"] = interaction_response
            answered_pending = raw.get("answered_pending_interaction")
            if isinstance(answered_pending, dict):
                result["answered_pending_interaction"] = answered_pending
        else:
            result["content"] = content
            result["content_preview"] = _extract_content_preview(content)
            if blocks:
                result["blocks"] = blocks
        if include_raw:
            result["raw"] = raw
        return result

    @staticmethod
    def _summarize_frame(
        frame: dict[str, Any],
        *,
        include_raw: bool = True,
    ) -> dict[str, Any]:
        raw = _normalize_admin_value(frame)
        payload = raw.get("payload")
        frame_type = (
            str(payload.get("type") or "")
            if isinstance(payload, dict)
            else str(raw.get("type") or "")
        )
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("delta") or payload.get("text") or payload.get("errorText") or "")
        result = {
            "turn_id": str(raw.get("turn_id") or ""),
            "command_id": str(raw.get("command_id") or ""),
            "seq": raw.get("frame_seq"),
            "type": frame_type,
            "created_at": str(raw.get("created_at") or ""),
            "text": text,
            "text_preview": _truncate_admin_text(text, limit=320),
        }
        if include_raw:
            result["raw"] = raw
        return result

    @staticmethod
    def _build_turn_summaries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for message in messages:
            turn_id = str(message.get("turn_id") or "").strip()
            if not turn_id:
                continue
            if turn_id not in summaries:
                summaries[turn_id] = {
                    "turn_id": turn_id,
                    "message_count": 0,
                    "roles": [],
                    "latest_created_at": "",
                    "user_preview": "",
                    "assistant_preview": "",
                }
            if turn_id in order:
                order.remove(turn_id)
            order.append(turn_id)

            summary = summaries[turn_id]
            summary["message_count"] += 1

            role = str(message.get("role") or "").strip()
            if role and role not in summary["roles"]:
                summary["roles"].append(role)

            created_at = str(message.get("created_at") or "")
            if created_at:
                summary["latest_created_at"] = created_at

            preview = str(message.get("content_preview") or "").strip()
            if role == "user" and preview and not summary["user_preview"]:
                summary["user_preview"] = preview
            if role == "assistant" and preview:
                summary["assistant_preview"] = preview

        return [summaries[turn_id] for turn_id in reversed(order)]

    async def admin_get_session_trace(
        self,
        user,
        session_id: str,
        *,
        turn_id: str | None = None,
        message_limit: int = 100,
        frame_limit: int = 500,
    ) -> dict[str, Any]:
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)

        safe_message_limit = max(1, min(int(message_limit), 200))
        safe_frame_limit = max(1, min(int(frame_limit), 1000))

        # Admin and chat read the same canonical message view derived from the
        # durable Session event log. This keeps turn summaries and frame lookup
        # aligned with the transcript the Session API exposes.
        raw_messages, _has_more = await self._message_view.list_page(
            session_id, limit=safe_message_limit
        )
        messages = [self._summarize_message(item) for item in raw_messages]
        turns = self._build_turn_summaries(messages)

        current_turn_id = str(session.get("current_turn_id") or "").strip()
        selected_turn_id = str(turn_id or "").strip() or current_turn_id
        if not selected_turn_id and turns:
            selected_turn_id = str(turns[0].get("turn_id") or "").strip()

        raw_frames: list[dict[str, Any]] = []
        truncated_frame_count = 0
        if selected_turn_id:
            raw_frames = await self._session_events_repo.list_frames(
                session_id,
                turn_id=selected_turn_id,
                after_seq=-1,
                limit=safe_frame_limit + 1,
            )
            if len(raw_frames) > safe_frame_limit:
                truncated_frame_count = len(raw_frames) - safe_frame_limit
                raw_frames = raw_frames[-safe_frame_limit:]

        if selected_turn_id and not any(str(item.get("turn_id") or "") == selected_turn_id for item in turns):
            turns.insert(0, {
                "turn_id": selected_turn_id,
                "message_count": 0,
                "roles": [],
                "latest_created_at": "",
                "user_preview": "",
                "assistant_preview": "",
            })

        return {
            "session_id": session_id,
            "state": str(session.get("state") or ""),
            "current_turn_id": current_turn_id or None,
            "selected_turn_id": selected_turn_id or None,
            "message_limit": safe_message_limit,
            "frame_limit": safe_frame_limit,
            "messages": messages,
            "turns": turns,
            "frames": [self._summarize_frame(item) for item in raw_frames],
            "truncated_frame_count": truncated_frame_count,
        }

    async def admin_session_transcript_files(
        self, user, session_id: str
    ) -> list[dict[str, Any]]:
        """This session's transcript in the Claude Agent SDK's own on-disk shape.

        One dict per file: ``{"path": ..., "sdk_session_id": ..., "jsonl": bytes}``.
        The point of the export is that the result drops into
        ``~/.claude/projects/<any-dir>/`` on an operator's machine and
        ``claude --resume <sdk-session-id>`` continues the conversation. So this
        emits what the SDK wrote and nothing else: one ``SessionStoreEntry`` per
        line, in append order, re-serialized but not reshaped. The store contract
        requires deep-equal round-trip, not byte-equal, so re-serializing is
        within it; adding a field, or wrapping the lines in a platform-defined
        envelope, would not be.

        A session is not one file. The main conversation and each subagent are
        separate SessionStore keys and separate JSONL files on disk, so each
        scope is loaded on its own — see
        :meth:`TranscriptEntryRepository.list_scopes_by_platform_session` for why
        the flattened read is the wrong one here.

        Mind the two ids: ``session_id`` here is the platform's, while the file
        is named for the SDK's, which is what ``--resume`` takes. A file named
        after the platform id is one Claude Code cannot find.

        Empty list when the mirror holds nothing — a session whose sandbox never
        started has no transcript, which is a different answer from an empty one.
        """
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)

        scopes = await self._transcript_entries_repo.list_scopes_by_platform_session(session_id)
        files: list[dict[str, Any]] = []
        for scope in scopes:
            entries = await self._transcript_entries_repo.load_entries(
                scope["project_key"],
                scope["session_id"],
                scope["subpath"],
                platform_session_id=session_id,
            )
            if not entries:
                continue
            body = "".join(
                json.dumps(entry, ensure_ascii=False, default=str) + "\n" for entry in entries
            )
            # The subpath already carries the SDK's own layout below the project
            # directory (`subagents/agent-<id>`); the main transcript is the
            # session id at its root.
            stem = scope["subpath"] or scope["session_id"]
            files.append(
                {
                    "path": f"{scope['project_key']}/{stem}.jsonl",
                    "sdk_session_id": scope["session_id"],
                    "subpath": scope["subpath"],
                    "jsonl": body.encode("utf-8"),
                }
            )
        return files

    async def admin_iter_batch_transcript_files(
        self,
        user,
        *,
        agent_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield one session's transcript files at a time for batch export.

        :meth:`admin_iter_manageable_sessions` applies the owner scope and the
        agent/date filters in its query, then walks the result page by page.
        This generator materializes one session's files at a time so the caller
        can write and release each group without retaining the full archive.

        Sessions whose mirror holds nothing are skipped rather than emitted as
        empty files: a session whose sandbox never started has no transcript,
        and a zero-byte ``.jsonl`` in the archive would read as one that does.
        """
        rows = self.admin_iter_manageable_sessions(
            user, agent_id=agent_id, since=since, until=until
        )
        async for row in rows:
            session_id = str(row.get("session_id") or "").strip()
            if not session_id:
                continue
            try:
                files = await self.admin_session_transcript_files(user, session_id)
            except APIError as exc:
                # The listing and the per-session read are two moments; a session
                # deleted between them must not take the whole archive down. An
                # authorization failure still propagates — the list is already
                # owner-scoped, so a 403 here means the two gates disagree, and
                # that is worth failing on.
                if exc.status_code != 404:
                    raise
                logger.info(
                    "batch transcript export: session vanished between listing and read: %s",
                    session_id,
                )
                continue
            if not files:
                continue
            yield {"session_id": session_id, "files": files}

    async def admin_kill_session(self, user, session_id: str) -> dict[str, Any]:
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)
        if is_assistant_user_conversation(session):
            raise APIError(
                code="ASSISTANT_WORKSPACE_SHARED_RUNTIME",
                message="assistant user conversations share an assistant workspace sandbox; kill the workspace owner runtime instead",
                status_code=409,
            )

        sandbox_id = str(session.get("sandbox_id") or "").strip() or None
        try:
            destruction = await self._runtime_manager.terminate_runtime(
                session_id, fallback_sandbox_id=sandbox_id,
            )
        except Exception as exc:
            logger.warning("admin kill failed session=%s err=%s", session_id, exc)
            raise APIError(
                code="ADMIN_KILL_FAILED",
                message=f"failed to terminate session runtime: {exc}",
                status_code=502,
            ) from exc

        # TERMINATED takes this row out of the expiration watcher's probe set,
        # so a box whose destruction was not confirmed would stop being looked
        # at by anything. Record its id on the row's ledger in the same write,
        # and report it to the operator, who needs to know whether the kill
        # worked.
        keep = keep_name_updates(destruction, row=session)
        if keep:
            logger.error(
                "admin kill could not confirm the destruction of session=%s "
                "sandbox=%s; it is recorded as undestroyed: %s",
                session_id,
                sandbox_id,
                destruction.detail,
            )
        await self._sessions_repo.update_session(session_id, {
            **keep,
            "state": SessionState.TERMINATED.value,
            "runtime_unavailable": True,
            "last_error": "killed by admin",
        })

        return {
            "session_id": session_id,
            "sandbox_id": sandbox_id,
            "killed": destruction.confirmed,
            "destruction": destruction.outcome,
            "destruction_detail": destruction.detail,
        }

    def admin_system_overview(self) -> dict[str, Any]:
        from astrabox.persistence.repository.backend import active_backend_name

        runtimes = self._runtime_map(self._runtime_manager)
        settings = load_astrabox_settings()
        health = self.admin_process_health()
        return {
            "machine_id": socket.gethostname(),
            "server_env": os.getenv("SERVER_ENV", "unknown"),
            "persistence_backend": active_backend_name(),
            "active_runtimes": len(runtimes),
            "mcp_proxy_base_url": settings.mcp_proxy_base_url,
            "runtime_session_ids": list(runtimes.keys()),
            "process_health": {
                "severity": health.get("severity"),
                "thread_count": (health.get("threads") or {}).get("count"),
                "fd_count": (health.get("file_descriptors") or {}).get("count"),
                "pending_task_count": (health.get("asyncio") or {}).get("pending_task_count"),
            },
        }
