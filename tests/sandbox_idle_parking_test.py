"""Idle reclamation by parking: what a paused box means, and who may make one.

``ASTRABOX_SANDBOX_IDLE_ACTION=pause`` is a promise about DATA — a conversation
left alone can be picked up later in its own box, with its files. These tests pin
the three things that promise rests on, each of which fails silently rather than
loudly when it breaks:

* **A paused box is not a gone box.** Two separate predicates answer "is any
  execution still in flight" (paused: yes, it is over) and "is the resource gone,
  drop the binding" (paused: no, the files and the id are still there). Collapsing
  them into one would send the next turn to cold-create over a live snapshot, and
  every test but this one would still pass.
* **Retention is bought before the box is parked.** A paused sandbox still expires
  on its lease, and renewing one that is already paused makes the control plane
  fail it — so the renew has to happen while the box is still running, and a renew
  that does not take must abort the parking rather than promise files for a week
  and lose them at the old lease.
* **The mark goes down before the commit and comes back up if it fails.** The
  commit takes tens of seconds; a turn arriving inside that window has to find a
  mark that sends it to the wake branch. A commit that fails leaves the sandbox
  RUNNING, so the mark must not survive it.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from astrabox.core.model import SessionState
from astrabox.core.service.orchestrator.expiration_watcher import ExpirationWatcher
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager
from astrabox.common.utils.time_utils import utcnow_iso


#: An agent that RESOLVES but carries no idle window of its own, as distinct from
#: an agent that cannot be resolved at all (``None``). The two must not behave the
#: same: one takes the declared default, the other is left strictly alone.
_UNSET = object()


def _probe(state: str, *, status: str = "OK") -> Any:
    return SimpleNamespace(probe_status=status, sandbox_state=state, error_text=None)


class ParkedIsNotGoneTests(unittest.TestCase):
    """The predicate split. Two questions, two answers, one state."""

    def test_paused_box_keeps_its_binding(self) -> None:
        # "Should the session stop naming this box?" — no. The name is the only
        # route back to the committed filesystem.
        for state in ("paused", "PAUSED", "pausing"):
            with self.subTest(state=state):
                self.assertFalse(
                    RemoteAgentRuntimeManager._is_terminal_sandbox_lifecycle_probe(
                        RemoteAgentRuntimeManager, _probe(state)
                    )
                )

    def test_paused_box_still_ends_a_turn(self) -> None:
        # "Is execution still in flight?" — no. Pause kills every process, so a
        # turn that was running is over and must be settled rather than awaited.
        for state in ("paused", "pausing"):
            with self.subTest(state=state):
                self.assertTrue(
                    RemoteAgentRuntimeManager._is_terminal_interaction_broker_sandbox_state(
                        _probe(state).sandbox_state
                    )
                )

    def test_genuinely_dead_states_still_drop_the_binding(self) -> None:
        for state in ("terminated", "failed", "error", "exited", "dead", "stopped"):
            with self.subTest(state=state):
                self.assertTrue(
                    RemoteAgentRuntimeManager._is_terminal_sandbox_lifecycle_probe(
                        RemoteAgentRuntimeManager, _probe(state)
                    )
                )

    def test_transient_probe_failure_drops_nothing(self) -> None:
        # An empty state under PROBE_FAILED is "I cannot tell", which must never
        # read as "nothing is there".
        self.assertFalse(
            RemoteAgentRuntimeManager._is_terminal_sandbox_lifecycle_probe(
                RemoteAgentRuntimeManager, _probe("", status="PROBE_FAILED")
            )
        )


class _FakeSessionsRepo:
    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        self._candidates = [dict(item) for item in candidates]
        self.updates: list[dict[str, Any]] = []
        #: The row as a re-read would see it; None means "same as the candidate".
        self.fresh: dict[str, Any] | None = None

    async def list_idle_reclaim_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return [dict(item) for item in self._candidates]

    async def list_dead_binding_probe_candidates(
        self, *, now_iso: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return []

    async def list_sessions_by_sandbox_id(self, sandbox_id: str) -> list[dict[str, Any]]:
        # Matches the store: every LIVE row bound to this box, this session's
        # included. A fake that answered [] would make every park look safe.
        return [
            dict(item)
            for item in self._candidates
            if str(item.get("sandbox_id") or "") == str(sandbox_id)
            and item.get("deleted") is not True
        ]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        # What the row says NOW, which is the whole point of re-reading it.
        return dict(self.fresh) if self.fresh is not None else (
            dict(self._candidates[0]) if self._candidates else None
        )

    async def update_session(
        self, session_id: str, updates: dict[str, Any], *, touch_updated_at: bool = True
    ) -> bool:
        self.updates.append(dict(updates))
        return True


class _FakeRuntimeManager:
    def __init__(self, *, renewed: Any = "far-future", paused: bool = True) -> None:
        self.calls: list[str] = []
        self._renewed = renewed
        self._paused = paused

    async def renew_sandbox_by_id(self, sandbox_id: str, ttl_seconds: int) -> Any:
        self.calls.append(f"renew:{ttl_seconds}")
        if self._renewed is None:
            return None
        return SimpleNamespace(isoformat=lambda: "2099-01-01T00:00:00+00:00")

    async def pause_sandbox_by_id(self, sandbox_id: str) -> bool:
        self.calls.append("pause")
        return self._paused

    async def evict_runtime(self, session_id: str) -> None:
        self.calls.append("evict")

    async def reconcile_startup_allocations(self, **_kwargs: Any) -> dict[str, int]:
        return {}

    async def reap_abandoned_agent_boxes(self) -> dict[str, int]:
        return {}

    async def reap_ownerless_sandboxes(self) -> dict[str, int]:
        return {}

    async def keep_prewarmed_agents_ready(self) -> dict[str, int]:
        return {}


class _NoDeadSandboxCandidates:
    async def list_dead_sandbox_probe_candidates(
        self, *, now_iso: str, limit: int
    ) -> dict[str, int]:
        return {}


def _watcher(
    *,
    candidates: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    idle_hibernate_seconds: Any = 1800,
    renewed: Any = "far-future",
    paused: bool = True,
    idle_action: str = "pause",
    default_idle_hibernate_seconds: int = 1800,
    # The environment's own action, which is the only one the sweep reads.
    # ``None`` means "same as the installation setting", the ordinary case now
    # that writes settle the field; pass ``""`` for an environment that states
    # none, which no migration has reached.
    environment_idle_action: str | None = None,
    background_task_state: dict[str, Any] | None = None,
) -> tuple[ExpirationWatcher, _FakeSessionsRepo, _FakeRuntimeManager]:
    sessions_repo = _FakeSessionsRepo(candidates)
    runtime_manager = _FakeRuntimeManager(renewed=renewed, paused=paused)

    async def _get_snapshot(session_id: str) -> dict[str, Any] | None:
        return dict(snapshot) if snapshot else None

    async def _resolve_agent_harness(agent_id: str) -> Any:
        if idle_hibernate_seconds is None:
            return None
        return SimpleNamespace(
            idle_hibernate_seconds=(
                None if idle_hibernate_seconds is _UNSET else idle_hibernate_seconds
            ),
            idle_action=(
                idle_action
                if environment_idle_action is None
                else environment_idle_action
            ),
        )

    async def _get_background_task_state(session_id: str) -> dict[str, Any] | None:
        return dict(background_task_state) if background_task_state else None

    platform = SimpleNamespace(
        _sessions_repo=sessions_repo,
        _runtime_manager=runtime_manager,
        _sandbox_lifecycle_service=_NoDeadSandboxCandidates(),
        _session_snapshots_repo=SimpleNamespace(get_snapshot=_get_snapshot),
        _agent_config=SimpleNamespace(resolve_agent_harness=_resolve_agent_harness),
        _get_background_task_state=_get_background_task_state,
    )
    watcher = ExpirationWatcher(platform_service=platform)
    settings = SimpleNamespace(
        agent_idle_hibernate_seconds=default_idle_hibernate_seconds,
        sandbox_idle_action=idle_action,
        sandbox_parked_retention_seconds=604800,
    )
    watcher._settings = lambda: settings  # type: ignore[method-assign]
    return watcher, sessions_repo, runtime_manager


_LONG_AGO = "2020-01-01T00:00:00+00:00"
_IDLE_SNAPSHOT = {"conversation_state": "IDLE", "updated_at": _LONG_AGO}
_CANDIDATE = {
    "session_id": "s-1",
    "sandbox_id": "sbx-1",
    "agent_id": "a-1",
    "state": SessionState.READY.value,
}


class ParkingScopeTests(unittest.IsolatedAsyncioTestCase):
    """This sweep parks Agent conversations, and only those.

    An Assistant's workspace box is parked by the Assistant service's own
    wake/hibernate, which renews, marks HIBERNATING, and commits in an order
    that a second parker would cut across. Two authorities over one sandbox is
    the failure the scope exists to prevent.

    The scope held before it was stated, but only by accident: the candidate
    query does not filter on session_kind, so an Assistant session IS handed to
    the decision — it was dropped further down for carrying no ``agent_id``.
    That is a property of what an Assistant session happens to store, not of
    what this sweep is for, and the day one carried an agent_id the Agent
    sweeper would have started parking Assistant boxes with nothing to catch it.
    """

    async def test_an_assistant_session_is_never_parked_by_this_sweep(self) -> None:
        assistant = {
            "session_id": "s-assistant",
            "sandbox_id": "sbx-assistant",
            "session_kind": "assistant_chat",
            "state": SessionState.READY.value,
        }
        watcher, _repo, runtime_manager = _watcher(
            candidates=[assistant], snapshot=_IDLE_SNAPSHOT
        )

        summary = await watcher.scan_once()

        # Not parked, and not counted as a live conversation this sweep chose
        # to leave alone either: it was never this sweep's to decide.
        self.assertEqual(summary.get("idle_parked", 0), 0)
        self.assertEqual(runtime_manager.calls, [])

    async def test_an_assistant_session_carrying_an_agent_id_is_still_refused(
        self,
    ) -> None:
        """The input an incidental guard lets through.

        An Assistant session that does carry an agent_id, otherwise identical to
        a parkable Agent conversation. Scoping by session_kind refuses it;
        refusing it because Assistant sessions happen to have no agent_id parks
        an Assistant's workspace box the day one does.
        """

        assistant = {
            "session_id": "s-assistant",
            "sandbox_id": "sbx-assistant",
            "session_kind": "assistant_chat",
            "agent_id": "a-1",
            "state": SessionState.READY.value,
        }
        watcher, _repo, runtime_manager = _watcher(
            candidates=[assistant], snapshot=_IDLE_SNAPSHOT
        )

        summary = await watcher.scan_once()

        self.assertEqual(summary.get("idle_parked", 0), 0)
        self.assertEqual(runtime_manager.calls, [])

    async def test_an_agent_conversation_is_still_parked(self) -> None:
        """The scope narrowed, and it narrowed to the right thing."""

        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT
        )

        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 1)
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])


class ParkingOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_renew_precedes_pause_and_the_mark_precedes_the_commit(self) -> None:
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT
        )
        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 1)
        # Retention is bought while the box still runs; renewing a paused sandbox
        # is what the control plane answers by failing it.
        # Evicting the in-memory runtime is part of parking, not an afterthought:
        # the turn path returns a live runtime BEFORE it looks at the parked mark, so
        # a session parked and woken inside one process would skip the resume and
        # serve its next turn against a Pod that has already been torn down: the
        # woken turn never logs a resume and comes back with a partial answer.
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])
        # And the mark is durable BEFORE the commit starts, so a turn arriving
        # inside those tens of seconds takes the wake branch instead of finding an
        # unreachable box.
        self.assertEqual(len(sessions_repo.updates), 1)
        self.assertTrue(sessions_repo.updates[0]["sandbox_parked_at"])
        self.assertEqual(
            sessions_repo.updates[0]["expires_at"], "2099-01-01T00:00:00+00:00"
        )

    async def test_a_renew_that_does_not_take_parks_nothing(self) -> None:
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT, renewed=None
        )
        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 0)
        self.assertEqual(summary["idle_failed"], 1)
        self.assertNotIn("pause", runtime_manager.calls)
        self.assertEqual(sessions_repo.updates, [])

    async def test_a_failed_commit_takes_the_mark_back_down(self) -> None:
        # A commit that fails leaves the sandbox RUNNING. A mark left behind would
        # describe a box that is not parked, and the next turn would try to resume
        # one that never stopped.
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT, paused=False
        )
        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 0)
        self.assertEqual(summary["idle_failed"], 1)
        self.assertIsNone(sessions_repo.updates[-1]["sandbox_parked_at"])


class IdleJudgementTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_in_flight_is_never_parked(self) -> None:
        for state in ("PROCESSING", "STREAMING", "INTERRUPTING", "WAITING_FOR_INTERACTION"):
            with self.subTest(conversation_state=state):
                watcher, sessions_repo, runtime_manager = _watcher(
                    candidates=[_CANDIDATE],
                    snapshot={"conversation_state": state, "updated_at": _LONG_AGO},
                )
                summary = await watcher.scan_once()
                self.assertEqual(summary["idle_active"], 1)
                self.assertEqual(runtime_manager.calls, [])
                self.assertEqual(sessions_repo.updates, [])

    async def test_open_background_work_keeps_a_quiet_box_off_the_sweep(self) -> None:
        """A background child launched from the conversation is work, even
        though nothing has been typed: parking it would kill the child."""

        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot=_IDLE_SNAPSHOT,
            background_task_state={"state": "OPEN", "pending_task_count": 1},
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(summary["idle_parked"], 0)
        self.assertEqual(runtime_manager.calls, [])
        self.assertEqual(sessions_repo.updates, [])

    async def test_a_conversation_quiet_for_less_than_its_window_is_left_alone(self) -> None:
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot={"conversation_state": "IDLE", "updated_at": utcnow_iso()},
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(runtime_manager.calls, [])

    async def test_the_window_is_the_agents_own(self) -> None:
        # idle_hibernate_seconds is the agent's field, and it decides. A window
        # wide enough that this conversation is not yet idle must hold the sweep
        # off even though the same row would qualify under the default.
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot={"conversation_state": "IDLE", "updated_at": utcnow_iso()},
            idle_hibernate_seconds=86400,
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(runtime_manager.calls, [])

    async def test_an_agent_that_never_set_a_window_takes_the_declared_default(self) -> None:
        # The deployment asking for `pause` IS the request. Requiring every agent
        # to opt in as well would leave the setting configured and inert — the
        # exact failure this path exists to remove: without it, a live deployment
        # reports idle candidates and parks none.
        from astrabox.common.utils.settings import AstraBoxRuntimeSettings

        declared = AstraBoxRuntimeSettings.model_fields[
            "agent_idle_hibernate_seconds"
        ].default
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot={"conversation_state": "IDLE", "updated_at": _LONG_AGO},
            idle_hibernate_seconds=_UNSET,
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_parked"], 1)
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])
        # The live runtime setting owns the default the sweep consumes.
        self.assertEqual(declared, 1800)

    async def test_an_unset_agent_window_uses_the_deployment_setting(self) -> None:
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot={"conversation_state": "IDLE", "updated_at": two_hours_ago},
            idle_hibernate_seconds=_UNSET,
            default_idle_hibernate_seconds=86400,
        )

        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(runtime_manager.calls, [])

    async def test_an_unresolvable_agent_is_left_alone(self) -> None:
        # The box costs money, but reclaiming one on a guess costs files.
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot=_IDLE_SNAPSHOT,
            idle_hibernate_seconds=None,
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_parked"], 0)
        self.assertEqual(runtime_manager.calls, [])

    async def test_a_session_without_an_agent_is_left_alone(self) -> None:
        # An assistant workspace has its own wake/hibernate lifecycle over a
        # shared box; two sweepers deciding for one sandbox is two authorities.
        watcher, _repo, runtime_manager = _watcher(
            candidates=[{"session_id": "s-2", "sandbox_id": "sbx-2"}],
            snapshot=_IDLE_SNAPSHOT,
        )
        await watcher.scan_once()
        self.assertEqual(runtime_manager.calls, [])

    async def test_no_snapshot_is_not_evidence_of_quiet(self) -> None:
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=None
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(runtime_manager.calls, [])


class EnvironmentWriteGateTests(unittest.TestCase):
    """``pause`` is refused at the form on a backend that cannot snapshot.

    The startup gate vets the deployment-wide setting, but an environment can now
    ask for ``pause`` on a deployment whose default is ``terminate`` — so the same
    reasoning has to be applied where that choice is written. Recording an action
    the backend cannot carry out would keep destroying the boxes: nothing would
    look broken, and the loss would surface later as missing files.
    """

    def _payload(self, **extra: Any) -> dict[str, Any]:
        from astrabox.core.service.orchestrator.engine.registry import known_engine_kinds
        from astrabox.providers import register_builtin_providers

        register_builtin_providers()
        return {
            "name": "claude-code",
            "engine_kind": known_engine_kinds()[0],
            **extra,
        }

    def test_pause_is_accepted_on_a_backend_that_can_snapshot(self) -> None:
        from astrabox.core.service.orchestrator.environment_schema import (
            validate_environment_payload,
        )

        validate_environment_payload(
            self._payload(idle_action="pause", sandbox_backend="open_sandbox")
        )

    def test_an_omitted_action_is_settled_from_the_installation_at_write_time(
        self,
    ) -> None:
        """A form that states no action stores one anyway, before validation.

        The sweep reads only what the environment carries, so an empty value
        left in the document would be swept by no rule at all. Normalization is
        what keeps "the form may omit it" from meaning "the stored environment
        may omit it".
        """
        from astrabox.core.service.orchestrator.environment_schema import (
            normalize_environment_payload,
            validate_environment_payload,
        )

        for stated, expected in (("", "pause"), (None, "pause"), ("terminate", "terminate")):
            with self.subTest(stated=stated):
                payload = self._payload()
                if stated is not None:
                    payload["idle_action"] = stated
                with patch(
                    "astrabox.common.utils.settings.load_astrabox_settings",
                    return_value=SimpleNamespace(sandbox_idle_action="pause"),
                ):
                    settled = normalize_environment_payload(payload)
                self.assertEqual(settled["idle_action"], expected)
                # Validation runs on the settled payload, never the raw one.
                if expected == "pause":
                    settled["sandbox_backend"] = "open_sandbox"
                validate_environment_payload(settled)

    def test_an_empty_action_is_refused_by_the_enum(self) -> None:
        """Nothing downstream may store an empty action by skipping normalize."""
        from astrabox.common.utils.errors import APIError
        from astrabox.core.service.orchestrator.environment_schema import (
            validate_environment_payload,
        )

        with self.assertRaises(APIError):
            validate_environment_payload(self._payload(idle_action=""))

    def test_pause_is_refused_on_a_backend_that_cannot(self) -> None:
        from astrabox.common.utils.errors import APIError
        from astrabox.core.service.orchestrator.environment_schema import (
            validate_environment_payload,
        )
        from astrabox.seams.sandbox import sandbox_for_name

        provider = sandbox_for_name("open_sandbox")
        with patch.object(type(provider), "supports_pause", False):
            with self.assertRaises(APIError) as caught:
                validate_environment_payload(
                    self._payload(idle_action="pause", sandbox_backend="open_sandbox")
                )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("can snapshot", str(caught.exception))

    def test_an_unknown_action_is_refused_by_the_enum(self) -> None:
        from astrabox.common.utils.errors import APIError
        from astrabox.core.service.orchestrator.environment_schema import (
            validate_environment_payload,
        )

        with self.assertRaises(APIError):
            validate_environment_payload(self._payload(idle_action="hibernate"))


class SharedBoxParkingTests(unittest.IsolatedAsyncioTestCase):
    """An idle conversation does not get to freeze a box its siblings are in.

    Parking is whole-box: the pause commits the filesystem and frees the
    compute, taking the egress sidecar and every conversation's runner with it.
    The wake path restores the ONE conversation that woke the box; the siblings'
    isolated sessions are gone, mid-turn, with nothing recording why.
    """

    async def test_a_box_another_conversation_is_bound_to_is_not_parked(self) -> None:
        sibling = {**_CANDIDATE, "session_id": "s-2"}  # same sandbox_id
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE, sibling], snapshot=_IDLE_SNAPSHOT
        )
        summary = await watcher.scan_once()

        self.assertEqual(summary.get("idle_parked", 0), 0)
        # Refused BEFORE the renew: nothing about the box was touched, and no
        # parked mark was written that a wake would then have to undo.
        self.assertEqual(runtime_manager.calls, [])
        self.assertEqual(sessions_repo.updates, [])

    async def test_a_box_only_this_conversation_is_bound_to_still_parks(self) -> None:
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT
        )
        summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 1)
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])

    async def test_not_knowing_who_is_in_the_box_refuses_the_park(self) -> None:
        """The lookup failing is not a licence to freeze the box."""
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT
        )

        async def _boom(sandbox_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("the store said no")

        sessions_repo.list_sessions_by_sandbox_id = _boom  # type: ignore[assignment]
        summary = await watcher.scan_once()

        self.assertEqual(summary.get("idle_parked", 0), 0)
        self.assertEqual(runtime_manager.calls, [])


class IdleActionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminate_parks_nothing(self) -> None:
        # Under the default action the control plane's own TTL does the reclaiming,
        # exactly as it always has. Nothing is paused and no binding is touched.
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT, idle_action="terminate"
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary.get("idle_parked", 0), 0)
        self.assertEqual(runtime_manager.calls, [])
        self.assertEqual(sessions_repo.updates, [])

    async def test_an_environment_may_park_where_the_deployment_would_not(self) -> None:
        # The environment's idle_action is the one that decides; the deployment
        # setting only answers for environments that do not say. Without this the
        # per-environment field would be recordable and inert.
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot=_IDLE_SNAPSHOT,
            idle_action="terminate",
            environment_idle_action="pause",
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_parked"], 1)
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])

    async def test_an_environment_may_keep_a_box_the_deployment_would_park(self) -> None:
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot=_IDLE_SNAPSHOT,
            idle_action="pause",
            environment_idle_action="terminate",
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary.get("idle_parked", 0), 0)
        self.assertEqual(runtime_manager.calls, [])

    async def test_an_idle_sweep_failure_does_not_cost_the_death_sweep_its_tick(self) -> None:
        watcher, _repo, _rm = _watcher(candidates=[_CANDIDATE], snapshot=_IDLE_SNAPSHOT)

        async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, int]:
            raise RuntimeError("control plane down")

        with (
            patch.object(watcher, "_sweep_idle_bindings", _boom),
            patch(
                "astrabox.core.service.orchestrator.runtime.storage.mounts.reconcile_workspace_mounts",
                new=AsyncMock(return_value={}),
            ),
        ):
            summary = await watcher.scan_once()

        self.assertEqual(summary, {})

    async def test_a_death_sweep_failure_does_not_cost_the_idle_sweep_its_tick(
        self,
    ) -> None:
        watcher, _repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            snapshot=_IDLE_SNAPSHOT,
        )

        async def _boom(*_args: Any, **_kwargs: Any) -> dict[str, int]:
            raise RuntimeError("owner store down")

        with patch.object(watcher, "_sweep_dead_bindings", _boom):
            summary = await watcher.scan_once()

        self.assertEqual(summary["idle_parked"], 1)
        self.assertEqual(runtime_manager.calls, ["renew:604800", "pause", "evict"])


if __name__ == "__main__":
    unittest.main()


class WakeTests(unittest.IsolatedAsyncioTestCase):
    """Waking a parked box, and what must be forgotten when it comes back.

    A resume keeps the sandbox id and the files and loses everything about the
    box's previous body. The address is the one that bites: the attach path
    prefers a STORED ``sandbox_endpoint`` over resolving one, so a stale copy
    sends every request of the woken turn to a place nothing answers at — which
    is what a live run did, with the box healthy on its own port and its log
    recording no host request at all.
    """

    def _ensure(self, session: dict[str, Any], *, resumed: bool) -> Any:
        from astrabox.core.service.orchestrator import runtime_ensure

        calls: list[str] = []
        updates: list[dict[str, Any]] = []

        class _Repo:
            async def update_session(
                self, session_id: str, upd: dict[str, Any], *, touch_updated_at: bool = True
            ) -> bool:
                updates.append(dict(upd))
                return True

        class _Manager:
            async def resume_sandbox_by_id(self, sandbox_id: str) -> bool:
                calls.append(f"resume:{sandbox_id}")
                return resumed

        target = runtime_ensure.RuntimeEnsure.__new__(runtime_ensure.RuntimeEnsure)
        target._sessions_repo = _Repo()  # type: ignore[attr-defined]
        target._runtime_manager = _Manager()  # type: ignore[attr-defined]
        return target, calls, updates

    async def test_a_resumed_box_forgets_its_pre_pause_address(self) -> None:
        session = {
            "session_id": "s-1",
            "sandbox_id": "sbx-1",
            "sandbox_parked_at": "2026-07-30T04:00:00+00:00",
            "sandbox_endpoint": "10.42.0.11:8000",
        }
        target, calls, updates = self._ensure(session, resumed=True)
        await target._wake_parked_sandbox(
            session_id="s-1", session=session, sandbox_id="sbx-1"
        )
        self.assertEqual(calls, ["resume:sbx-1"])
        # both the durable row and the dict this turn goes on to read
        self.assertIsNone(updates[-1]["sandbox_endpoint"])
        self.assertIsNone(updates[-1]["sandbox_parked_at"])
        self.assertIsNone(session["sandbox_endpoint"])
        self.assertIsNone(session["sandbox_parked_at"])

    async def test_a_box_that_was_never_parked_is_left_alone(self) -> None:
        session = {"session_id": "s-2", "sandbox_id": "sbx-2",
                   "sandbox_endpoint": "10.42.0.12:8000"}
        target, calls, updates = self._ensure(session, resumed=True)
        await target._wake_parked_sandbox(
            session_id="s-2", session=session, sandbox_id="sbx-2"
        )
        self.assertEqual(calls, [])
        self.assertEqual(updates, [])
        self.assertEqual(session["sandbox_endpoint"], "10.42.0.12:8000")

    async def test_a_resume_that_does_not_take_keeps_the_mark(self) -> None:
        # The mark is the only record that those files are still reachable, so a
        # transient control-plane failure must not erase it. The turn falls through
        # to the ordinary gone-sandbox re-borrow, loudly.
        session = {
            "session_id": "s-3",
            "sandbox_id": "sbx-3",
            "sandbox_parked_at": "2026-07-30T04:00:00+00:00",
            "sandbox_endpoint": "10.42.0.13:8000",
        }
        target, calls, updates = self._ensure(session, resumed=False)
        await target._wake_parked_sandbox(
            session_id="s-3", session=session, sandbox_id="sbx-3"
        )
        self.assertEqual(calls, ["resume:sbx-3"])
        self.assertEqual(updates, [])
        self.assertEqual(session["sandbox_parked_at"], "2026-07-30T04:00:00+00:00")


class ReattachSinglePathTests(unittest.IsolatedAsyncioTestCase):
    """ONE attach path for every backend — with the gone classification ON it.

    The runner world has no dispatch-register round trip, so there is no branch
    to order any more; what must hold instead is that the single path
    classifies a SANDBOX_GONE from the attach into the durable write-back the
    lapsed-lease re-borrow keys on. Reachable only when a session has no live
    in-memory runtime — a PARKED box has none by construction, so a wake lands
    a turn exactly here, and a box that died out-of-band must arm the
    re-borrow, not just fail the turn.
    """

    def _subject(self, *, ensure: Any) -> tuple[Any, list[dict[str, Any]]]:
        from astrabox.core.service.orchestrator import runtime_ensure

        writes: list[dict[str, Any]] = []

        class _Repo:
            async def update_session(self, _sid: str, patch: dict[str, Any]) -> bool:
                writes.append(dict(patch))
                return True

        target = runtime_ensure.RuntimeEnsure.__new__(runtime_ensure.RuntimeEnsure)
        target._sessions_repo = _Repo()  # type: ignore[attr-defined]
        target._ensure_lightweight_runtime_for_turn = ensure  # type: ignore[attr-defined]
        target._sandbox_lifecycle_service = AsyncMock()  # type: ignore[attr-defined]
        return target, writes

    async def _attach(self, target: Any) -> Any:
        return await target._attach_runtime_for_turn(
            session={"session_id": "s-1", "sandbox_backend": "open_sandbox"},
            sandbox_id="sbx-1",
            engine_session_key=None,
            workspace_plan=SimpleNamespace(engine_kind="claude_code"),
            session_kind="agent_chat",
            template_name="t",
            runtime_identity={"user": "agent"},
            turn_id="turn-1",
            command_id="cmd-1",
            requested_permission_mode=None,
            log_context="test",
        )

    async def test_attach_succeeds_on_the_single_path(self) -> None:
        async def _ensure(**kwargs: Any) -> tuple[Any, dict[str, Any]]:
            return SimpleNamespace(sandbox_id="sbx-1"), {"sandbox_id": "sbx-1"}

        target, writes = self._subject(ensure=_ensure)
        result = await self._attach(target)
        self.assertIsNotNone(getattr(result, "runtime", None))
        self.assertFalse(getattr(result, "sandbox_gone", False))
        # The attach outcome was written back durably.
        self.assertEqual(writes, [{"sandbox_id": "sbx-1"}])

    async def test_sandbox_gone_arms_the_lapsed_lease_re_borrow(self) -> None:
        from astrabox.common.utils.errors import APIError

        async def _ensure(**kwargs: Any) -> tuple[Any, dict[str, Any]]:
            raise APIError(
                code="SANDBOX_GONE",
                message="sandbox sbx-1 is gone",
                status_code=409,
                data={"code": "SANDBOX_GONE", "sandbox_id": "sbx-1"},
            )

        target, writes = self._subject(ensure=_ensure)
        result = await self._attach(target)
        self.assertIsNone(getattr(result, "runtime", None))
        self.assertTrue(result.sandbox_gone)
        self.assertTrue(writes and writes[-1]["runtime_unavailable"])
        # A gone sandbox's effective lease is over: expires_at is stamped so
        # the row-level lapsed-lease gate re-borrows on the next message.
        self.assertIn("expires_at", writes[-1])
        target._sandbox_lifecycle_service.converge_dead_sandbox_owners.assert_awaited_once_with(
            "sbx-1",
            last_error="sandbox sbx-1 is gone",
            reason="runtime_attach:SANDBOX_GONE",
        )


class SnapshotReclamationTests(unittest.IsolatedAsyncioTestCase):
    """A parked box's snapshot must be reclaimed BEFORE the box, or never.

    Deleting a sandbox drops its snapshot records with it, so after the destroy there
    is no id left to ask about while the images those records named stay in the
    registry. Thirty orphans in one day of pausing, combined with build cache,
    filled the node's disk until kubelet reported DiskPressure and every sandbox
    create failed with a network error three layers from the cause.
    """

    def _manager(self, *, discard_raises: bool = False) -> tuple[Any, list[str]]:
        from astrabox.core.service.orchestrator import runtime_manager as rm

        calls: list[str] = []

        class _Provider:
            name = "open_sandbox"

            async def discard_snapshots(self, sandbox_id: str) -> int:
                calls.append("discard")
                if discard_raises:
                    raise RuntimeError("the control plane refused")
                return 2

            async def confirm_destroyed(self, sandbox_id: str) -> Any:
                calls.append("destroy")
                return SimpleNamespace(confirmed=True, leaked_sandbox_id=None, detail="")

        manager = rm.RemoteAgentRuntimeManager.__new__(rm.RemoteAgentRuntimeManager)

        async def _resolve(_sandbox_id: str) -> str:
            return "open_sandbox"

        manager._resolve_sandbox_backend = _resolve  # type: ignore[attr-defined]
        return (manager, calls, _Provider())

    async def test_the_snapshot_goes_before_the_box(self) -> None:
        from astrabox.core.service.orchestrator import runtime_manager as rm

        manager, calls, provider = self._manager()
        with patch.object(rm, "sandbox_for_name", lambda _name: provider):
            result = await manager.destroy_sandbox_by_id("sbx-1")
        self.assertEqual(calls, ["discard", "destroy"])
        self.assertTrue(result.confirmed)

    async def test_an_unreclaimable_snapshot_never_keeps_a_box_alive(self) -> None:
        # Leaking an image costs disk; leaking a running box costs money and
        # privacy. The destroy proceeds either way.
        from astrabox.core.service.orchestrator import runtime_manager as rm

        manager, calls, provider = self._manager(discard_raises=True)
        with patch.object(rm, "sandbox_for_name", lambda _name: provider):
            result = await manager.destroy_sandbox_by_id("sbx-2")
        self.assertEqual(calls, ["discard", "destroy"])
        self.assertTrue(result.confirmed)


class FreshRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_turn_dispatched_during_the_sweep_stops_the_park(self) -> None:
        """The candidate list is as old as the sweep; a turn claimed in that window
        is invisible in it. The park decision reads at decide time — the snapshot
        is fetched inside _is_idle_past, so a dispatch that flipped it to
        PROCESSING after the sweep began still stops the park. Otherwise the
        stale candidate state could park a sandbox that an active turn owns.
        """
        watcher, sessions_repo, runtime_manager = _watcher(
            candidates=[_CANDIDATE],
            # Idle when the candidate list was built; the dispatch has since
            # projected PROCESSING, which is what the decide-time fetch sees.
            snapshot={"conversation_state": "PROCESSING", "updated_at": _LONG_AGO},
        )
        summary = await watcher.scan_once()
        self.assertEqual(summary["idle_active"], 1)
        self.assertEqual(runtime_manager.calls, [], "nothing may be paused")
        self.assertEqual(sessions_repo.updates, [])
