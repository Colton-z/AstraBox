"""Periodic background watcher: the pull backstop of sandbox-death convergence.

Owned by :class:`AgentPlatformService`, started after bootstrap and cancelled by
``platform_service.quiesce``. The sandbox-death convergence capability has two
layers: the status callback (push — a provider delivers a terminal notification
to ``/api/v1/sandbox-callback/...``) converges owners when their sandbox
dies, but delivery is best-effort and some providers have no emitter at all
(their sandboxes die silently). This watcher is the pull layer: each tick asks
every durable owner for suspicious bindings, deduplicates them by physical
sandbox, and asks the provider's lifecycle probe. A control-plane-confirmed
answer realigns or converges every owner of that box through the same lifecycle
service the callback uses. A transient probe failure never changes an owner.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.settings import (
    AstraBoxRuntimeSettings,
    load_astrabox_settings,
)
from astrabox.common.utils.time_utils import utcnow_iso
from astrabox.core.service.orchestrator.bootstrap_reconciler import (
    STARTUP_ALLOCATION_GRACE_SECONDS,
)
logger = get_logger(__name__)

# Per-tick probe budget: bounds control-plane load when many sessions lapse at
# once; the remainder is picked up by the following ticks.
_DEAD_BINDING_SCAN_LIMIT = 50

# Per-tick parking budget. Lower than the probe budget on purpose: each parking
# holds a commit open for tens of seconds on the cluster, where a probe is one
# cheap read.
_IDLE_SWEEP_SCAN_LIMIT = 10

# Conversation states in which no turn is running, so nothing is lost by freeing
# the compute. Anything else — PROCESSING, STREAMING, INTERRUPTING, or waiting on
# a tool permission — is a turn whose processes a pause would kill.
_IDLE_CONVERSATION_STATES = frozenset({"IDLE"})


def _parse_iso(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ExpirationWatcher:
    """Periodic background watcher.

    Owned by :class:`AgentPlatformService`. Started via :meth:`ensure_started`
    after bootstrap, cancelled by ``platform_service.quiesce``.
    """

    def __init__(self, *, platform_service: Any) -> None:
        self._platform = platform_service
        self._task: asyncio.Task | None = None
        self._closed = False

    # ── Lifecycle ───────────────────────────────────────────────────────

    def ensure_started(self) -> None:
        settings = self._settings()
        if not settings.expiration_watcher_enabled:
            logger.info("expiration_watcher: disabled by settings")
            return
        if self._closed:
            logger.warning("expiration_watcher: not starting (closed)")
            return
        task = self._task
        if isinstance(task, asyncio.Task) and not task.done():
            return
        self._task = self._platform._spawn_background_task(
            self._loop(), name="expiration-watcher",
        )
        logger.info(
            "expiration_watcher: started interval=%ds threshold=%ds cooldown=%ds",
            settings.expiration_watcher_interval_seconds,
            settings.expiration_watcher_threshold_seconds,
            settings.reprovision_cooldown_seconds,
        )

    def quiesce(self) -> None:
        self._closed = True
        task = self._task
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel("shutdown")

    # ── Loop ────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        try:
            while not self._closed:
                # The configured interval is honoured, floored at 1s only to
                # keep a zero or negative value from becoming a busy loop. A
                # higher floor would silently override any smaller value a
                # deployment sets, including the few-second intervals that make
                # idle parking and dead-binding convergence observable in a
                # live run.
                interval = max(1, int(self._settings().expiration_watcher_interval_seconds or 300))
                try:
                    summary = await self.scan_once()
                    if any(summary.values()):
                        logger.info("expiration_watcher: tick %s", summary)
                except Exception:
                    if self._closed:
                        return
                    logger.exception("expiration_watcher: scan_once failed")
                if self._closed:
                    return
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            logger.warning("expiration_watcher: loop cancelled")
            raise

    # ── Single tick ─────────────────────────────────────────────────────

    async def scan_once(self) -> dict[str, int]:
        """One tick: release abandoned starts, converge deaths, then park idle boxes.

        The two sweeps are independent — a failure in either must not cost the
        other its tick — and they cannot contend, because one takes the bindings
        whose lease has lapsed and the other only the bindings whose lease is
        still good.
        """
        summary: dict[str, int] = {}
        try:
            summary.update(
                await self._platform._runtime_manager.reconcile_startup_allocations(
                    stale_before=datetime.now(timezone.utc)
                    - timedelta(seconds=STARTUP_ALLOCATION_GRACE_SECONDS),
                    limit=_DEAD_BINDING_SCAN_LIMIT,
                )
            )
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: startup-allocation sweep failed")
        if self._closed:
            return summary
        try:
            summary.update(
                await self._platform._runtime_manager.reap_abandoned_agent_boxes()
            )
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: agent-box reap failed")
        if self._closed:
            return summary
        try:
            summary.update(
                await self._platform._runtime_manager.reap_ownerless_sandboxes()
            )
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: ownerless reap failed")
        if self._closed:
            return summary
        try:
            from astrabox.core.service.orchestrator.runtime.storage.mounts import reconcile_workspace_mounts

            summary.update(await reconcile_workspace_mounts())
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: workspace-mount reap failed")
        if self._closed:
            return summary
        try:
            summary.update(
                await self._platform._runtime_manager.keep_prewarmed_agents_ready()
            )
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: prewarm sweep failed")
        if self._closed:
            return summary
        try:
            summary.update(await self._sweep_dead_bindings())
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: dead-binding sweep failed")
        if self._closed:
            return summary
        try:
            summary.update(await self._sweep_idle_bindings())
        except Exception:
            if self._closed:
                raise
            logger.exception("expiration_watcher: idle sweep failed")
        return summary

    async def _sweep_dead_bindings(self) -> dict[str, int]:
        """Probe suspicious owner bindings whose callback may have been lost."""
        runtime_manager = self._platform._runtime_manager
        lifecycle = self._platform._sandbox_lifecycle_service
        candidates = await lifecycle.list_dead_sandbox_probe_candidates(
            now_iso=utcnow_iso(),
            limit=_DEAD_BINDING_SCAN_LIMIT,
        )
        if not candidates:
            return {}
        summary = {
            "candidates": sum(candidates.values()),
            "converged": 0,
            "alive": 0,
            "probe_failed": 0,
        }

        for sandbox_id, owner_count in candidates.items():
            try:
                probe = await runtime_manager.get_sandbox_lifecycle_probe(sandbox_id)
            except Exception as exc:  # noqa: BLE001 — probe is evidence, not an answer
                summary["probe_failed"] += owner_count
                logger.warning(
                    "expiration_watcher: lifecycle probe errored sandbox=%s: %s",
                    sandbox_id,
                    exc,
                )
                continue
            if not runtime_manager._is_terminal_sandbox_lifecycle_probe(probe):
                probe_status = str(getattr(probe, "probe_status", "") or "").strip()
                if probe_status == "OK":
                    summary["alive"] += owner_count
                    try:
                        real_expiry = await runtime_manager.get_sandbox_expires_at(
                            sandbox_id
                        )
                        await lifecycle.realign_live_sandbox_owners(
                            sandbox_id,
                            expires_at=(
                                real_expiry.isoformat()
                                if real_expiry is not None
                                else None
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "expiration_watcher: live-owner realignment failed "
                            "sandbox=%s",
                            sandbox_id,
                        )
                else:
                    summary["probe_failed"] += owner_count
                continue
            probe_state = str(getattr(probe, "sandbox_state", "") or "").strip()
            try:
                convergence = await lifecycle.converge_dead_sandbox_owners(
                    sandbox_id,
                    last_error="sandbox terminated",
                    reason=(
                        "dead_binding_reconcile:"
                        f"{str(getattr(probe, 'probe_status', '') or '').strip()}"
                        + (f":{probe_state}" if probe_state else "")
                    ),
                )
                summary["converged"] += (
                    len(convergence.converged_sessions)
                    + len(convergence.converged_agents)
                    + len(convergence.converged_assistant_workspaces)
                )
            except Exception:
                logger.exception(
                    "expiration_watcher: convergence failed sandbox=%s",
                    sandbox_id,
                )
        return summary

    # ── Idle reclamation ────────────────────────────────────────────────

    async def _sweep_idle_bindings(self) -> dict[str, int]:
        """Park the sandbox of a conversation that has gone quiet.

        Parking is per environment, so the decision is per session rather than per
        tick: an environment's ``idle_action`` says what becomes of the boxes it
        shapes, and the deployment's ``ASTRABOX_SANDBOX_IDLE_ACTION`` answers for
        the environments that do not say. Under ``terminate`` there is nothing for
        a sweeper to do — the lease runs out and the control plane destroys the
        box. ``terminate`` is the deployment default.

        Scope is a conversation with an Agent. An assistant's workspace has its own
        wake/hibernate lifecycle over a shared box, and one sweeper deciding for
        both would be two authorities over the same sandbox.
        """
        settings = self._settings()
        sessions_repo = self._platform._sessions_repo
        candidates = await sessions_repo.list_idle_reclaim_candidates(
            now_iso=utcnow_iso(),
            limit=_IDLE_SWEEP_SCAN_LIMIT,
        )
        if not candidates:
            return {}
        retention_seconds = max(60, int(settings.sandbox_parked_retention_seconds or 0))
        default_idle_seconds = settings.agent_idle_hibernate_seconds
        summary = {
            "idle_candidates": len(candidates),
            "idle_parked": 0,
            "idle_active": 0,
            "idle_failed": 0,
        }
        for session in candidates:
            session_id = str((session or {}).get("session_id") or "").strip()
            sandbox_id = str((session or {}).get("sandbox_id") or "").strip()
            if not session_id or not sandbox_id:
                continue
            idle_after_seconds = await self._idle_window_if_parking(
                session,
                default_idle_seconds=default_idle_seconds,
            )
            if idle_after_seconds is None:
                continue
            # Re-read at decision time because the candidate row predates this
            # sweep's per-session work. The fresh row here and the snapshot read
            # in _is_idle_past prevent a newly dispatched turn from being judged
            # by stale idle evidence.
            fresh = await sessions_repo.get_session(session_id)
            if not isinstance(fresh, dict):
                continue
            if not await self._is_idle_past(
                fresh, idle_after_seconds=idle_after_seconds
            ):
                summary["idle_active"] += 1
                continue
            # A quiet conversation may still be working: a background child
            # launched from it runs in this box and reports through the
            # journal, which moves neither the snapshot's state nor its clock.
            # The same native-activity read that projects BACKGROUND_RUNNING to
            # the page keeps the box off the sweep.
            if await self._platform._get_background_task_state(session_id) is not None:
                summary["idle_active"] += 1
                continue
            try:
                parked = await self._park_sandbox(
                    session_id=session_id,
                    sandbox_id=sandbox_id,
                    retention_seconds=retention_seconds,
                )
            except Exception:
                summary["idle_failed"] += 1
                logger.exception(
                    "expiration_watcher: parking failed session=%s sandbox=%s",
                    session_id, sandbox_id,
                )
                continue
            if parked:
                summary["idle_parked"] += 1
                logger.info(
                    "expiration_watcher: parked idle sandbox session=%s sandbox=%s "
                    "idle_after_s=%d retention_s=%d",
                    session_id, sandbox_id, idle_after_seconds, retention_seconds,
                )
            else:
                summary["idle_failed"] += 1
        return summary

    async def _park_sandbox(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        retention_seconds: int,
    ) -> bool:
        """Renew to the retention window, mark the binding parked, then pause.

        The order is what makes this correct:

        1. **Renew first.** A paused box still expires on its lease, and renewing
           one that is already paused makes the control plane fail it outright — so
           the retention window has to be bought while the box is still running. A
           renew that does not take means no parking: pausing anyway would promise
           files for a week and lose them at the old lease.
        2. **Mark before pausing.** The commit takes tens of seconds, during which
           a turn can arrive. The mark is what the ensure path reads to take the
           wake branch, so that turn waits out the commit and resumes this box
           instead of finding an unreachable one and cold-creating over it.
        3. **Unmark on refusal.** A failed commit leaves the sandbox RUNNING, so a
           mark left behind would describe a box that is not parked at all.
        """
        runtime_manager = self._platform._runtime_manager
        sessions_repo = self._platform._sessions_repo
        if not await self._box_is_this_session_s_alone(
            session_id=session_id, sandbox_id=sandbox_id
        ):
            return False
        renewed_until = await runtime_manager.renew_sandbox_by_id(
            sandbox_id, retention_seconds
        )
        if renewed_until is None:
            logger.warning(
                "expiration_watcher: not parking — retention renew did not take "
                "session=%s sandbox=%s retention_s=%d",
                session_id, sandbox_id, retention_seconds,
            )
            return False
        parked_at = utcnow_iso()
        await sessions_repo.update_session(
            session_id,
            {
                "sandbox_parked_at": parked_at,
                "expires_at": renewed_until.isoformat(),
            },
            touch_updated_at=False,
        )
        try:
            paused = await runtime_manager.pause_sandbox_by_id(sandbox_id)
        except Exception:
            await self._clear_parked_mark(session_id)
            raise
        # Pausing removes the sandbox compute, so every cached runtime for this
        # session is stale: its WebSocket targets the stopped Pod. Eviction makes
        # the next turn consult the parked mark and run the wake path instead of
        # returning an in-memory runtime whose transport cannot serve the turn.
        with contextlib.suppress(Exception):
            await runtime_manager.evict_runtime(session_id)
        if not paused:
            await self._clear_parked_mark(session_id)
            logger.warning(
                "expiration_watcher: pause did not settle, sandbox left running "
                "session=%s sandbox=%s",
                session_id, sandbox_id,
            )
            return False
        return True

    async def _box_is_this_session_s_alone(
        self, *, session_id: str, sandbox_id: str
    ) -> bool:
        """False when other live sessions are bound to the same box.

        Parking is a whole-box operation: the pause commits the filesystem and
        frees the compute, which takes the egress sidecar and every
        conversation's runner down with it. Under the shared tenancy that cuts
        somebody else's conversation off mid-turn, and the wake path cannot undo
        it — it restores the one conversation that woke the box, while the
        siblings' isolated sessions are gone.

        The question is put to the session rows rather than to this session's
        runtime because parking is a sweeper decision that any replica may make,
        and the replica sweeping is usually not the one holding the runtime.

        Under the per-session tenancy a box carrying two live sessions is a
        defect rather than a design, and refusing to park it is still the right
        answer: a box two rows point at is not one row's to freeze.
        """
        try:
            bound = await self._platform._sessions_repo.list_sessions_by_sandbox_id(
                sandbox_id
            )
        except Exception:
            # Not knowing who else is in the box is not a licence to freeze it.
            logger.exception(
                "expiration_watcher: not parking — could not list the sessions "
                "bound to sandbox=%s (session=%s)",
                sandbox_id, session_id,
            )
            return False
        others = [
            str((row or {}).get("session_id") or "")
            for row in bound
            if str((row or {}).get("session_id") or "") != session_id
        ]
        if not others:
            return True
        logger.info(
            "expiration_watcher: not parking — sandbox=%s carries %d other "
            "conversation(s) (%s); an idle conversation does not get to freeze "
            "a box its siblings are working in",
            sandbox_id, len(others), ", ".join(sorted(others)[:5]),
        )
        return False

    async def _clear_parked_mark(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._platform._sessions_repo.update_session(
                session_id,
                {"sandbox_parked_at": None},
                touch_updated_at=False,
            )

    async def _idle_window_if_parking(
        self,
        session: dict[str, Any],
        *,
        default_idle_seconds: int,
    ) -> int | None:
        """This session's idle window, or None when its box must not be parked.

        Two settings from one resolve, because they come from two resources and
        both have to hold:

        * the Environment's ``idle_action`` — what becomes of a box of this
          shape. It is settled when the environment is written, so nothing is
          re-derived here; anything but ``pause`` means this sweep has no
          business with the session. An environment stating none is left alone
          and logged, for the same reason an unresolvable agent is: acting on a
          guess is what costs files.
        * the Agent's ``idle_hibernate_seconds`` — how long this harness may sit
          quiet. A long-running research agent and a webhook that answers in
          seconds do not want the same window. An agent that never set one takes
          the deployment's configured default, because the deployment asking for
          ``pause`` is the request; requiring every agent to opt in as well would
          leave that setting configured and inert.

        A session whose agent cannot be resolved is a different case and is left
        alone: the box costs money, but reclaiming one on a guess costs files.
        """
        session_kind = str((session or {}).get("session_kind") or "").strip()
        if session_kind == "assistant_chat":
            # An Assistant's workspace box is parked by the Assistant service's
            # own wake/hibernate, and two authorities over one sandbox is the
            # failure this scope exists to prevent. Stated here rather than left
            # to the agent_id check below: that check drops these sessions only
            # incidentally, because an Assistant session carries no agent_id, so
            # the rule would stop holding the day one did.
            return None
        agent_id = str((session or {}).get("agent_id") or "").strip()
        if not agent_id:
            return None
        try:
            view = await self._platform._agent_config.resolve_agent_harness(agent_id)
        except Exception:
            logger.exception(
                "expiration_watcher: agent resolve failed agent=%s", agent_id
            )
            return None
        if view is None:
            return None
        action = str(getattr(view, "idle_action", None) or "").strip().lower()
        if not action:
            logger.warning(
                "expiration_watcher: environment states no idle_action "
                "agent=%s; leaving the sandbox alone",
                agent_id,
            )
            return None
        if action != "pause":
            return None
        configured = getattr(view, "idle_hibernate_seconds", None)
        if configured is None:
            return default_idle_seconds
        try:
            seconds = int(configured)
        except (TypeError, ValueError):
            return None
        return seconds if seconds > 0 else None

    async def _is_idle_past(
        self,
        session: dict[str, Any],
        *,
        idle_after_seconds: int,
    ) -> bool:
        """Whether this conversation is between turns and has been for long enough.

        Two readings, and both have to agree, because the cost of getting this
        wrong is a turn killed mid-flight: a conversation state that is not
        mid-turn, and no snapshot activity inside the window. Both come from the
        snapshot — the single authority for "is a turn running".
        """
        now = datetime.now(timezone.utc)
        session_id = str((session or {}).get("session_id") or "").strip()
        snapshot = await self._platform._session_snapshots_repo.get_snapshot(session_id)
        if not isinstance(snapshot, dict):
            # No snapshot means no evidence of quiet, not evidence of quiet.
            return False
        if str(snapshot.get("conversation_state") or "").strip() not in _IDLE_CONVERSATION_STATES:
            return False
        last_activity = _parse_iso(snapshot.get("updated_at")) or _parse_iso(
            session.get("updated_at")
        )
        if last_activity is None:
            return False
        return (now - last_activity) >= timedelta(seconds=int(idle_after_seconds))

    # ── Shared helpers ──────────────────────────────────────────────────

    def _settings(self) -> AstraBoxRuntimeSettings:
        return load_astrabox_settings()
