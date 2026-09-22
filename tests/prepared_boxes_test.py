"""Conversation-tenancy prepared boxes: gate, publish CAS, reap dispatch.

The fake agent repository reproduces the document store's exact-match update
semantics (an absent field matches nothing, not even None), the same behaviour
the slot allocator's suite holds.
"""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb
from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EnginePreparationContext,
    EngineStartupContext,
)


class FakeAgentRepo:
    def __init__(self, row: dict[str, Any]) -> None:
        self.rows = {str(row["agent_id"]): dict(row)}

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        row = self.rows.get(agent_id)
        return copy.deepcopy(row) if row is not None else None

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        row = self.rows.get(agent_id)
        if row is None:
            return False
        row.update(updates)
        return True

    async def compare_and_update_agent(
        self,
        agent_id: str,
        *,
        expected: dict[str, Any],
        updates: dict[str, Any],
    ) -> bool:
        row = self.rows.get(agent_id)
        if row is None:
            return False
        for key, value in expected.items():
            if value == {"$exists": False}:
                if key in row:
                    return False
                continue
            if key not in row or row[key] != value:
                return False
        row.update(updates)
        return True


class _FakeProvider:
    name = "open_sandbox"

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.adopted: list[tuple[str, str, str]] = []

    async def confirm_destroyed(self, sandbox_id: str) -> Any:
        self.destroyed.append(sandbox_id)
        return SimpleNamespace(confirmed=True, detail="fake destroy")

    async def connect(self, sandbox_id: str) -> Any:
        return SimpleNamespace(sandbox_id=sandbox_id)

    async def adopt_sandbox_identity(
        self,
        sandbox: Any,
        *,
        session_id: str,
        assignment_id: str,
    ) -> None:
        self.adopted.append((sandbox.sandbox_id, session_id, assignment_id))


class _BoxAdapter(EngineAdapter):
    """The whole-box preparation form, recording what the allocator hands it."""

    prepares_conversation_box = True

    def __init__(self) -> None:
        self.calls: list[EnginePreparationContext] = []
        self.receipt: dict[str, Any] = {
            "engine_kind": "codex",
            "spawn_fingerprint": "spawn-1",
            "activation_mcp_servers": [],
        }

    @property
    def engine_kind(self) -> str:  # type: ignore[override]
        return "codex"

    @property
    def engine_client_type(self) -> type:  # type: ignore[override]
        return object

    @property
    def capabilities(self) -> Any:  # type: ignore[override]
        return SimpleNamespace()

    def sandbox_request(self, *, template: Any, model_access: Any) -> Any:
        _ = (template, model_access)
        return SimpleNamespace()

    async def activate_runtime(self, context: EngineStartupContext) -> Any:
        _ = context
        raise AssertionError("not part of preparation")

    async def prepare_runtime(
        self, context: EnginePreparationContext
    ) -> dict[str, Any]:
        self.calls.append(context)
        return dict(self.receipt)


class _SlotOnlyAdapter(_BoxAdapter):
    """Implements the seam but needs a runner slot, not a whole box."""

    prepares_conversation_box = False


def _template(**overrides: Any) -> Any:
    return SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        runtime_generation="fp-1",
        sandbox_backend="open_sandbox",
        model_config={},
        **overrides,
    )


def _row(manifest: Any = None, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"agent_id": "agent-1", ps.PREPARED_SLOT_FIELD: manifest}
    row.update(overrides)
    return row


def _box_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slot_id": "slot-abc",
        "state": "prepared",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "engine_kind": "codex",
        "sandbox_id": "box-77",
        "sandbox_backend": "open_sandbox",
        "cwd": "/workspace",
        "workspace_id": "workspace-1",
        "runtime_generation": "fp-1",
        "spawn_fingerprint": "spawn-1",
        "activation_mcp_servers": [],
        "gateway_substitution": True,
        "runtime_identity": {"workspace_dir": "/workspace"},
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    base.update(overrides)
    return base


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    adapter: Any,
    provider: _FakeProvider | None = None,
) -> _FakeProvider:
    provider = provider or _FakeProvider()
    monkeypatch.setattr(pb, "get_engine_adapter", lambda kind: adapter)
    monkeypatch.setattr(pb, "sandbox_for_template", lambda template: provider)
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)
    from astrabox.core.service.orchestrator.engine import provisioning

    async def _provision(*args: Any, **kwargs: Any) -> Any:
        _ = args
        return SimpleNamespace(
            sandbox=SimpleNamespace(sandbox_id="box-77"),
            sandbox_id="box-77",
            cwd="/workspace",
            workspace_id="workspace-1",
            gateway_substitution=True,
            model_credential="slot-placeholder",
            runtime_env={},
            environment_credential_contract=(),
        )

    monkeypatch.setattr(provisioning, "provision_engine_slot_sandbox", _provision)
    monkeypatch.setattr(provisioning, "_session_log_declaration", lambda template: None)
    monkeypatch.setattr(
        pb,
        "build_conversation_identity",
        lambda *, session_id, agent_id, template: {
            "session_id": session_id,
            "workspace_dir": "/workspace",
        },
    )
    return provider


def _runtime_manager() -> Any:
    return SimpleNamespace(
        resolve_model_access=lambda config: SimpleNamespace(configuration=config),
        deployment_settings=SimpleNamespace(),
    )


# ── the placement gate ────────────────────────────────────────────────────


def test_a_slot_form_engine_gets_no_box(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _SlotOnlyAdapter()
    _wire(monkeypatch, adapter=adapter)
    repo = FakeAgentRepo(_row())

    result = asyncio.run(
        pb.prepare_box_for_agent(
            _template(), runtime_manager=_runtime_manager(), agent_repo=repo
        )
    )
    # Declared absence, checked before any box exists to leak.
    assert result is None
    assert adapter.calls == []


# ── publish ───────────────────────────────────────────────────────────────


def test_prepare_publishes_the_receipt_as_a_box_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _BoxAdapter()
    adapter.receipt.update(
        {
            # Platform-owned facts in a receipt may never override the facts
            # established by the box preparation transaction.
            "engine_kind": "pi",
            "sandbox_id": "box-from-engine",
            "cwd": "/engine-cwd",
            "gateway_substitution": False,
            "runtime_identity": {"workspace_dir": "/engine-workspace"},
            "runtime_generation": "engine-generation",
            "sandbox_backend": "engine-backend",
            "workspace_id": "engine-workspace-id",
            "model_credential": "engine-credential",
            "runtime_env": {"ENGINE_ENV": "wrong"},
            "environment_credential_contract": [{"engine": "wrong"}],
            # Engine-owned evidence must survive the platform transaction
            # verbatim; pi uses the parked pipe id during claim.
            "parked_pty_session_id": "pty-42",
            "vendor_native_session": "native-7",
        }
    )
    _wire(monkeypatch, adapter=adapter)
    repo = FakeAgentRepo(_row())

    manifest = asyncio.run(
        pb.prepare_box_for_agent(
            _template(), runtime_manager=_runtime_manager(), agent_repo=repo
        )
    )
    assert manifest is not None
    stored = repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD]
    assert stored == manifest
    assert stored["placement"] == pb.PLACEMENT_CONVERSATION_BOX
    assert stored["state"] == "prepared"
    assert stored["sandbox_id"] == "box-77"
    assert stored["sandbox_backend"] == "open_sandbox"
    assert stored["engine_kind"] == "codex"
    assert stored["cwd"] == "/workspace"
    assert stored["workspace_id"] == "workspace-1"
    assert stored["gateway_substitution"] is True
    assert stored["model_credential"] == "slot-placeholder"
    assert stored["runtime_env"] == {}
    assert stored["environment_credential_contract"] == []
    assert stored["runtime_identity"]["workspace_dir"] == "/workspace"
    assert stored["runtime_identity"]["sandbox_id"] == "box-77"
    assert stored["spawn_fingerprint"] == "spawn-1"
    assert stored["runtime_generation"] == "fp-1"
    assert stored["parked_pty_session_id"] == "pty-42"
    assert stored["vendor_native_session"] == "native-7"
    # A whole-box unit has no runner barrier; the manifest carries no token
    # that nothing would ever check.
    assert "activation_token" not in stored
    call = adapter.calls[0]
    assert call.runner_uri is None
    assert call.slot_id == stored["slot_id"]
    assert call.preparation_fingerprint == "fp-1"
    # The gateway ledger gained the slot's entry for later key GC.
    entries = repo.rows["agent-1"][ps.GATEWAY_ENTRIES_FIELD]
    assert entries == [{"slot_id": stored["slot_id"], "session_id": None}]


@pytest.mark.parametrize("failure_stage", ["marker", "adapter"])
def test_preparation_failure_destroys_the_unpublished_box(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """The platform owns and cleans the box until its manifest is published."""

    from astrabox.core.service.orchestrator.engine import provisioning
    from astrabox.core.service.orchestrator.engine import transcript_mirror

    adapter = _BoxAdapter()
    provider = _wire(monkeypatch, adapter=adapter)
    repo = FakeAgentRepo(_row())
    events: list[str] = []

    monkeypatch.setattr(
        provisioning,
        "_session_log_declaration",
        lambda _template: object(),
    )

    async def mark_unclaimed(
        _sandbox: Any,
        *,
        unclaimed_for: timedelta,
    ) -> None:
        events.append(f"marker:{int(unclaimed_for.total_seconds())}")
        if failure_stage == "marker":
            raise RuntimeError("marker refused")

    async def prepare_runtime(_context: EnginePreparationContext) -> dict[str, Any]:
        events.append("adapter")
        if failure_stage == "adapter":
            raise RuntimeError("engine preparation refused")
        raise AssertionError("the parameterization names no success path")

    monkeypatch.setattr(transcript_mirror, "mark_mirror_unclaimed", mark_unclaimed)
    monkeypatch.setattr(adapter, "prepare_runtime", prepare_runtime)

    with pytest.raises(RuntimeError):
        asyncio.run(
            pb.prepare_box_for_agent(
                _template(), runtime_manager=_runtime_manager(), agent_repo=repo
            )
        )

    marker = f"marker:{ps.PREPARED_SLOT_TTL_SECONDS + 300}"
    assert events == ([marker] if failure_stage == "marker" else [marker, "adapter"])
    assert provider.destroyed == ["box-77"]
    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None


def test_an_existing_matching_manifest_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _BoxAdapter()
    _wire(monkeypatch, adapter=adapter)
    existing = _box_manifest()
    repo = FakeAgentRepo(_row(existing))

    result = asyncio.run(
        pb.prepare_box_for_agent(
            _template(), runtime_manager=_runtime_manager(), agent_repo=repo
        )
    )
    assert result == existing
    assert adapter.calls == []


# ── reap dispatch ─────────────────────────────────────────────────────────


def test_a_stale_box_manifest_is_reaped_by_destroying_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)
    manifest = _box_manifest(runtime_generation="fp-old")
    repo = FakeAgentRepo(_row(manifest))

    reason = asyncio.run(
        ps.reap_slot_manifest_if_stale(_template(), agent_repo=repo)
    )
    assert reason == "runtime generation is stale"
    assert provider.destroyed == ["box-77"]
    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None


def test_an_adopted_claimed_box_keeps_its_box_and_loses_its_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)

    async def _adopted(manifest: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(pb, "claimed_session_adopted_box", _adopted)
    manifest = _box_manifest(
        state="claimed",
        claimed_session_id="session-1",
        claimed_at=(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 60)
        ).isoformat(),
    )
    repo = FakeAgentRepo(_row(manifest))

    reason = asyncio.run(
        ps.reap_slot_manifest_if_stale(_template(), agent_repo=repo)
    )
    assert reason == "claimed manifest was never cleared by its Session"
    # Destroying an adopted box would kill the live conversation.
    assert provider.destroyed == []
    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None


def test_an_unadopted_claimed_box_orphan_is_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)

    async def _not_adopted(manifest: dict[str, Any]) -> bool:
        return False

    monkeypatch.setattr(pb, "claimed_session_adopted_box", _not_adopted)
    manifest = _box_manifest(
        state="claimed",
        claimed_session_id="session-dead",
        claimed_at=(
            datetime.now(timezone.utc)
            - timedelta(seconds=ps.CLAIMED_SLOT_ORPHAN_SECONDS + 60)
        ).isoformat(),
    )
    repo = FakeAgentRepo(_row(manifest))

    asyncio.run(ps.reap_slot_manifest_if_stale(_template(), agent_repo=repo))
    assert provider.destroyed == ["box-77"]
    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] is None


def test_an_unconfirmed_box_destroy_keeps_the_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnconfirmedProvider(_FakeProvider):
        async def confirm_destroyed(self, sandbox_id: str) -> Any:
            self.destroyed.append(sandbox_id)
            return SimpleNamespace(confirmed=False, detail="control plane timed out")

    provider = _UnconfirmedProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)
    manifest = _box_manifest()
    repo = FakeAgentRepo(_row(manifest))

    with pytest.raises(APIError, match="manifest was retained"):
        asyncio.run(
            pb.discard_prepared_box(
                "agent-1", manifest, reason="activation failed", agent_repo=repo
            )
        )

    assert provider.destroyed == ["box-77"]
    assert repo.rows["agent-1"][ps.PREPARED_SLOT_FIELD] == manifest


# ── adoption evidence ─────────────────────────────────────────────────────


def test_adoption_is_the_session_rows_own_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astrabox.persistence.repository.session_repository as session_repo_module

    class _Sessions:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        async def get_session(self, session_id: str) -> dict[str, Any] | None:
            return self._row

    manifest = _box_manifest(state="claimed", claimed_session_id="session-1")

    monkeypatch.setattr(
        session_repo_module,
        "SessionRepository",
        lambda: _Sessions({"startup_allocation": {"sandbox_id": "box-77"}}),
    )
    assert asyncio.run(pb.claimed_session_adopted_box(manifest)) is True

    monkeypatch.setattr(
        session_repo_module,
        "SessionRepository",
        lambda: _Sessions({"startup_allocation": {"sandbox_id": "box-OTHER"}}),
    )
    assert asyncio.run(pb.claimed_session_adopted_box(manifest)) is False

    monkeypatch.setattr(
        session_repo_module, "SessionRepository", lambda: _Sessions(None)
    )
    assert asyncio.run(pb.claimed_session_adopted_box(manifest)) is False


def test_adopting_a_claimed_box_repoints_its_reverse_lookup_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(pb, "sandbox_for_name", lambda name: provider)
    handle = asyncio.run(
        pb.adopt_claimed_box(
            _template(),
            _box_manifest(),
            session_id="session-1",
            assignment_id="assignment-1",
        )
    )
    assert handle.sandbox_id == "box-77"
    assert provider.adopted == [("box-77", "session-1", "assignment-1")]
