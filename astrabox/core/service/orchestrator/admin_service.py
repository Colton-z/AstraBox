"""Admin operations: list sessions, kill session, system overview.

Session reads come from the session's row and the runtime's recorded facts:
its inputs, responses and interactions, and the browser parts each response
produced, the same records the conversation's own pages read.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import socket
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrabox.core.service.orchestrator.session_lifecycle.service import SessionLifecycleService
    from astrabox.core.service.orchestrator.session_runtime.service import SessionRuntimeService

from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.session_runtime_repository import SessionRuntimeRepository
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
from astrabox.core.service.orchestrator.admin_error_capture import list_runtime_errors
from astrabox.core.service.orchestrator.engine.capabilities import (
    capabilities_for_engine_kind,
)
from astrabox.core.service.orchestrator.runtime_binding import runtime_subject_kind
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


def _message_preview(message: dict[str, Any]) -> str:
    """A message's text, with each tool call or file it holds named in brackets."""
    pieces: list[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "")
        if kind == "text":
            text = str(part.get("text") or "").strip()
            if text:
                pieces.append(text)
        elif kind == "dynamic-tool":
            pieces.append(f"[{part.get('toolName') or 'tool'}]")
        elif kind.startswith("tool-"):
            pieces.append(f"[{kind.removeprefix('tool-')}]")
        elif kind == "file":
            pieces.append("[file]")
    return " ".join(pieces)


class AdminService:
    def __init__(
        self,
        *,
        sessions_repo: SessionRepository,
        session_runtime: SessionRuntimeService,
        session_lifecycle: SessionLifecycleService,
        agent_config: AgentConfigService,
        runtime_manager: RemoteAgentRuntimeManager,
        sanitize_session,
    ) -> None:
        self._sessions_repo = sessions_repo
        self._session_runtime = session_runtime
        self._facts = SessionRuntimeRepository()
        self._session_lifecycle = session_lifecycle
        self._user_profiles_repo = UserProfileRepository()
        self._transcript_entries_repo = TranscriptEntryRepository()
        self._agent_config = agent_config
        self._runtime_manager = runtime_manager
        self._sanitize_session = sanitize_session

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
        observation = session.get("runtime_versions")
        observation = observation if isinstance(observation, dict) else {
            "components": [], "reason": "runtime_not_observed",
        }
        return {
            **observation,
            "matches_bound_sandbox": bool(
                observation.get("sandbox_id")
                and observation["sandbox_id"] == session.get("sandbox_id")
            ),
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
            lock = getattr(runtime, "lock", None)
            rows.append({
                "session_id": str(session_id),
                "sandbox_id": str(getattr(runtime, "sandbox_id", "") or "") or None,
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
        session_feed = self._session_runtime.notification_health()
        if session_feed["started"] and not session_feed["ready"]:
            severity = "error"
        try:
            load_avg = list(os.getloadavg())
        except Exception:
            load_avg = []
        return {
            "severity": severity,
            "session_feed": session_feed,
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
                "locked_count": sum(1 for row in runtime_rows if row["lock_locked"] is True),
                "items": runtime_rows,
            },
        }

    async def _manageable_agent_ids(self, user) -> list[str]:
        """The ids of the Agents the user creates or co-administers.

        Sessions are scoped by the Agent id they name: Agent names are not
        unique, so a scope of names would hand one manager the conversations of
        every other Agent that happens to share a name with theirs.
        """
        viewer = getattr(user, "user_id", "") if user is not None else ""
        if not viewer:
            return []
        return await self._agent_config.manageable_agent_ids(user)

    async def admin_navigation_summary(self, user) -> dict[str, int]:
        """Counts for the management rail, under each collection's read scope."""
        manageable, agents, environments = await asyncio.gather(
            self._manageable_agent_ids(user),
            self._agent_config.count_visible_agents(user),
            self._agent_config.count_environment_configs(),
        )
        sessions = await self._sessions_repo.count_all_sessions(agent_ids=manageable)
        return {
            "agents": agents,
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
        manageable = await self._manageable_agent_ids(user)
        # An empty scope is a real answer, and `[]` says it in the query: match
        # nothing. Never `None`, which the repository reads as unscoped.
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        total = await self._sessions_repo.count_all_sessions(
            agent_ids=manageable, agent_id=agent_id, since=since, until=until
        )
        rows = await self._sessions_repo.list_all_sessions(
            limit=page_size,
            skip=(page - 1) * page_size,
            agent_ids=manageable,
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
        manageable = await self._manageable_agent_ids(user)
        return await self._sessions_repo.count_session_totals(agent_ids=manageable)

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
        manageable = await self._manageable_agent_ids(user)
        while True:
            rows = await self._sessions_repo.list_all_sessions(
                limit=batch_size,
                skip=skip,
                agent_ids=manageable,
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
            "sandbox_id": str(session.get("sandbox_id") or "").strip() or None,
            "user_id": str(session.get("user_id") or "").strip() or None,
            "display_name": str(display_row.get("display_name") or "").strip() or None,
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
        is what a **user** is shown for it (READY, PROCESSING, WAITING_INPUT or
        the lifecycle state), from the runtime's recorded status and its open
        responses, interactions and held inputs, which ride along as
        ``current_responses``, ``last_response``, ``pending_interaction`` and
        ``held_inputs``.

        They answer different questions and can legitimately disagree: a
        READY session whose runtime is producing a response is PROCESSING to
        its user, and both are true. Carrying both here is what makes that
        difference readable; the second is computed by the same function the
        user-facing reads use.
        """
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)
        clean = self._sanitize_session(session)
        view = await self._session_runtime.runtime_view(session)
        clean["conversation_state"] = view["state"]
        for key in ("current_responses", "last_response", "pending_interaction", "held_inputs"):
            clean[key] = view[key]
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
    def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
        """One history message for the trace: who, when, a preview and the message itself."""
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        return {
            "message_id": str(message.get("id") or ""),
            "role": str(message.get("role") or ""),
            "input_id": metadata.get("input_id"),
            "response_id": metadata.get("response_id"),
            "created_at": str(metadata.get("created_at") or metadata.get("started_at") or ""),
            "content_preview": _truncate_admin_text(_message_preview(message)),
            "raw": _normalize_admin_value(message),
        }

    @staticmethod
    def _summarize_part(row: dict[str, Any]) -> dict[str, Any]:
        part = row.get("part") if isinstance(row.get("part"), dict) else {}
        text = str(part.get("delta") or part.get("text") or part.get("errorText") or "")
        return {
            "seq": row.get("seq"),
            "type": str(part.get("type") or ""),
            "created_at": str(row.get("created_at") or ""),
            "text": text,
            "text_preview": _truncate_admin_text(text, limit=320),
            "raw": _normalize_admin_value(part),
        }

    @staticmethod
    def _response_summaries(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each response on the page with what it answered, newest first."""
        summaries: list[dict[str, Any]] = []
        user_preview = ""
        for message in messages:
            if message["role"] == "user":
                user_preview = message["content_preview"]
                continue
            metadata = message["raw"].get("metadata") or {}
            summaries.append({
                "response_id": message["response_id"],
                "origin": metadata.get("origin"),
                "status": metadata.get("status"),
                "outcome": metadata.get("outcome"),
                "native_reason": metadata.get("native_reason"),
                "started_at": metadata.get("started_at"),
                "ended_at": metadata.get("ended_at"),
                "user_preview": user_preview if metadata.get("origin") != "engine" else "",
                "assistant_preview": message["content_preview"],
            })
        return list(reversed(summaries))

    async def admin_get_session_trace(
        self,
        user,
        session_id: str,
        *,
        response_id: str | None = None,
        message_limit: int = 100,
        part_limit: int = 500,
    ) -> dict[str, Any]:
        """The session's latest messages beside one response's browser parts.

        Messages are the conversation's history page, as its user reads it.
        The parts are the response's own output log, the latest ``part_limit``
        of them; ``truncated_part_count`` says how many earlier ones the limit
        left out. Without ``response_id`` the trace opens on the newest
        response still running, else the newest on the page.
        """
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)

        safe_message_limit = max(1, min(int(message_limit), 100))
        safe_part_limit = max(1, min(int(part_limit), 1000))
        history = await self._session_runtime.history_for(session_id, limit=safe_message_limit)
        messages = [self._summarize_message(item) for item in history["messages"]]
        responses = self._response_summaries(messages)
        current = [
            str(row["response_id"])
            for row in (await self._facts.activity(session_id))["open_responses"]
        ]
        selected = str(response_id or "").strip() or (current[-1] if current else "")
        if not selected and responses:
            selected = str(responses[0]["response_id"] or "")

        parts: list[dict[str, Any]] = []
        total = 0
        if selected:
            parts, total = await self._facts.response_output_tail(
                session_id, selected, limit=safe_part_limit
            )
        return {
            "session_id": session_id,
            "state": str(session.get("state") or ""),
            "current_response_ids": current,
            "selected_response_id": selected or None,
            "message_limit": safe_message_limit,
            "part_limit": safe_part_limit,
            "messages": messages,
            "responses": responses,
            "parts": [self._summarize_part(row) for row in parts],
            "truncated_part_count": total - len(parts),
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

    async def admin_import_session_history(
        self, session_id: str, messages: Any,
    ) -> dict[str, int]:
        """Import a migrated conversation's earlier messages for reading only."""
        from astrabox.core.service.orchestrator.session_history_import import (
            SessionHistoryImporter,
            validate_history_import,
        )

        session = await self._sessions_repo.get_session(session_id)
        if session is None or str(session.get("state") or "") == SessionState.DELETED.value:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        validated = validate_history_import(messages)
        result = await SessionHistoryImporter(self._facts).import_messages(session_id, validated)
        logger.info(
            "session history imported: session=%s imported=%s unchanged=%s",
            session_id, result["imported"], result["unchanged"],
        )
        return result

    async def admin_kill_session(self, user, session_id: str) -> dict[str, Any]:
        session = await self._sessions_repo.get_session(session_id)
        if session is None:
            raise APIError(code="SESSION_NOT_FOUND", message="session not found", status_code=404)
        await self._assert_can_manage_session(user, session)
        return await self._session_lifecycle.kill_session(session)

    def admin_system_overview(self) -> dict[str, Any]:
        runtimes = self._runtime_map(self._runtime_manager)
        settings = load_astrabox_settings()
        health = self.admin_process_health()
        return {
            "machine_id": socket.gethostname(),
            "server_env": os.getenv("SERVER_ENV", "unknown"),
            "persistence_backend": "postgresql",
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
