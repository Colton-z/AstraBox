"""Codex prepared boxes: spawn fingerprint contract and the claim path.

The fingerprint tests pin WHICH facts are box-frozen: the create fixes the
image, entrypoint, catalog and gateway allowlist, while model, instructions,
sandbox mode, approval policy and collaboration mode ride ``thread/start`` at
claim — an input added to the wrong side surfaces here before it either
retires warm boxes needlessly or serves stale ones.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb
from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.core.service.orchestrator.engine import codex, provisioning, startup


def _template(**overrides: Any) -> Any:
    base = dict(
        agent_id="agent-1",
        engine_kind="codex",
        agent_prewarm_fingerprint="fp-1",
        runtime_generation="fp-1",
        prewarm_enabled=True,
        model_config={},
        system="Be terse.",
        engine_options={"model_catalog": {"models": []}},
        runtime_template_name="astrabox/sandbox-codex:latest",
        sandbox_backend="open_sandbox",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Manager:
    def __init__(self) -> None:
        self.allocations: list[tuple[str, Any]] = []

    def resolve_model_access(self, model_config: Any) -> Any:
        return SimpleNamespace(
            base_url="https://gw.test",
            credential="sk-shared",
            credential_kind="bearer",
            model_name="gpt-5.3-codex",
        )

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.allocations.append((session_id, allocation))

    async def resolve_session_egress_credentials(self, session_id: str, **kwargs: Any) -> list[Any]:
        return []

    @property
    def deployment_settings(self) -> Any:
        return SimpleNamespace(mcp_proxy_base_url="http://backend.codex.test:8000")


def _plan(resume: str | None = None) -> Any:
    return SimpleNamespace(
        resume_engine_session_key=resume,
        cwd="/workspace",
        subject_kind="deployment_conversation",
        agent_id="agent-1",
        assistant_id=None,
        user_id=None,
        engine_kind="codex",
    )


@pytest.fixture(autouse=True)
def _platform_claim_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mounts(**_kwargs: Any) -> tuple[tuple[str, str], ...]:
        return ()

    async def _ready(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(provisioning, "plan_workspace_mounts", _mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", _ready)


class _Backend:
    name = "open_sandbox"

    def __init__(self, *, vault: Any = None) -> None:
        self._vault = vault

    async def connect(self, sandbox_id: str) -> Any:
        return SimpleNamespace(sandbox_id=sandbox_id)

    async def apply_credential_vault(self, sandbox: Any, **kwargs: Any) -> None:
        if self._vault is not None:
            await self._vault(sandbox, **kwargs)


async def _claim(
    manager: _Manager,
    *,
    template: Any | None = None,
    workspace_plan: Any | None = None,
    user_id: str | None = None,
    backend: Any | None = None,
) -> Any:
    selected = template or _template()
    adapter = codex.CodexEngineAdapter()
    access = manager.resolve_model_access(selected.model_config)
    return await provisioning.claim_prepared_engine_sandbox(
        manager,
        session_id="session-1",
        assignment_id="assignment-1",
        template=selected,
        workspace_plan=workspace_plan or _plan(),
        user_id=user_id,
        request=adapter.sandbox_request(template=selected, model_access=access),
        backend_adapter=backend or _Backend(),
        credential="sk-shared",
        mcp_vault_write=None,
        vault_enabled=True,
        session_log=adapter.capabilities.session_log,
    )


# ── spawn fingerprint ─────────────────────────────────────────────────────


def test_spawn_fingerprint_is_stable_for_the_same_frozen_facts() -> None:
    a = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    b = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    assert a == b


@pytest.mark.parametrize(
    ("kwargs", "template_overrides"),
    [
        ({"catalog": '{"models": ["other"]}'}, {}),
        ({"base_url": "https://other-gw.test"}, {}),
        ({}, {"runtime_template_name": "astrabox/sandbox-codex:next"}),
    ],
)
def test_spawn_fingerprint_retires_a_box_when_a_frozen_fact_changes(
    kwargs: dict[str, Any], template_overrides: dict[str, Any]
) -> None:
    base = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    changed = codex._slot_spawn_fingerprint(
        _template(**template_overrides),
        base_url=kwargs.get("base_url", "https://gw.test"),
        catalog=kwargs.get("catalog", '{"models": []}'),
    )
    assert changed != base


def test_thread_borne_facts_do_not_touch_the_fingerprint() -> None:
    base = codex._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", catalog='{"models": []}'
    )
    changed = codex._slot_spawn_fingerprint(
        _template(
            system="Entirely different instructions.",
            engine_options={
                "model_catalog": {"models": []},
                "config": {"approval_policy": "never"},
                "turn_start": {
                    "collaborationMode": {
                        "mode": "plan",
                        "settings": {
                            "reasoning_effort": None,
                            "developer_instructions": None,
                        },
                    }
                },
            },
            model_config={"model": "another-model"},
        ),
        base_url="https://gw.test",
        catalog='{"models": []}',
    )
    # They ride thread/start onto the live runtime; retiring a warm box over
    # them would rebuild boxes for a change activation already carries.
    assert changed == base


# ── claim gates ───────────────────────────────────────────────────────────


def test_a_stale_spawn_fingerprint_discards_the_claimed_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "spawn_fingerprint": "spawn-of-an-older-catalog",
        "gateway_substitution": True,
        "runtime_identity": {"workspace_dir": "/workspace"},
    }

    discards: list[str] = []

    async def _discard(template: Any, manifest: dict[str, Any], *, reason: str) -> None:
        discards.append(reason)

    refills: list[str] = []
    provisioned = provisioning.ProvisionedEngineSandbox(
        sandbox=SimpleNamespace(sandbox_id="box-77"),
        sandbox_id="box-77",
        runtime_identity={"workspace_dir": "/workspace"},
        cwd="/workspace",
        model_credential="placeholder",
        prepared_manifest=claimed,
    )

    async def _provision(*_args: Any, **_kwargs: Any) -> Any:
        return provisioned

    monkeypatch.setattr(startup, "provision_engine_sandbox", _provision)
    monkeypatch.setattr(startup, "_discard_claimed_unit", _discard)
    monkeypatch.setattr(
        startup,
        "schedule_prepared_runtime_refill",
        lambda template, manager: refills.append(template.agent_id),
    )
    with pytest.raises(APIError, match="prepared Codex box") as caught:
        asyncio.run(
            startup.start_platform_runtime(
                _Manager(),
                codex.CodexEngineAdapter(),
                session_id="session-1",
                assignment_id="assignment-1",
                template=_template(),
                workspace_plan=_plan(),
                user_id=None,
                permission_mode=None,
                progress_callback=None,
                callback_url=None,
            )
        )
    assert caught.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert len(discards) == 1 and caught.value.code in discards[0]
    assert refills == ["agent-1"]


# ── activation order ──────────────────────────────────────────────────────


def test_activation_adopts_binds_and_publishes_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim's ordering IS the contract each step's safety rests on.

    Metadata before the allocation record (a half-claimed box must stay the
    reaper's to destroy), the gateway swap and the mirror target before the
    thread exists (first model call and first rollout line both already have
    their destination), and READY only through the publish.
    """

    template = _template()
    manager = _Manager()
    catalog = '{"models": []}'
    events: list[str] = []

    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "cwd": "/workspace",
        "spawn_fingerprint": codex._slot_spawn_fingerprint(
            template, base_url="https://gw.test", catalog=catalog
        ),
        "activation_mcp_servers": [],
        "gateway_substitution": True,
        "engine_kind": "codex",
        "runtime_generation": "fp-1",
        "runtime_env": {},
        "environment_credential_contract": [],
        "workspace_id": "workspace-1",
        "runtime_identity": {"workspace_dir": "/workspace", "session_id": "slot-abc"},
    }

    async def _win(**kwargs: Any) -> dict[str, Any]:
        events.append("claim")
        return dict(claimed)

    cleared: list[str] = []

    async def _clear(*, agent_id: str, slot_id: str) -> None:
        cleared.append(slot_id)

    box_handle = SimpleNamespace(sandbox_id="box-77")

    async def _adopt(
        template_arg: Any,
        manifest: dict[str, Any],
        *,
        session_id: str,
        assignment_id: str,
    ) -> Any:
        assert assignment_id == "assignment-1"
        events.append(f"adopt:{session_id}")
        return box_handle

    real_record = manager.record_startup_allocation

    async def _record(session_id: str, allocation: Any) -> None:
        events.append(f"allocation:{allocation.sandbox_id}:{allocation.scope}")
        await real_record(session_id, allocation)

    class _Endpoint:
        async def ensure_session_credential(self, *, context: Any) -> str:
            assert context.user_id == "user-1"
            events.append(f"mint:{context.conversation_id}")
            return "sk-session-key"

    async def _vault(
        sandbox: Any,
        *,
        vault_write: Any,
        create_if_missing: bool = True,
        **kwargs: Any,
    ) -> None:
        assert create_if_missing is False
        substitution = vault_write.model[0].substitutions[0]
        assert substitution.secret_value == "sk-session-key"
        events.append(f"vault:{substitution.name}")

    async def _mark(*, agent_id: str, slot_id: str, session_id: str) -> None:
        events.append(f"mark:{slot_id}:{session_id}")

    async def _target(sandbox: Any, manager_arg: Any, session_id: str, *, cwd: str) -> None:
        events.append(f"mirror-target:{session_id}:{cwd}")

    async def _connect(sandbox: Any, *, port: int = 44790) -> Any:
        # Box-tenancy paths keep dialing the image's fixed forwarder port.
        assert port == 44790
        events.append("link-connect")
        return SimpleNamespace(kind="link")

    published: list[dict[str, Any]] = []

    async def _publish(**kwargs: Any) -> Any:
        events.append("publish")
        published.append(kwargs)
        return SimpleNamespace(session_id=kwargs["session_id"])

    import astrabox.core.service.orchestrator.engine.transcript_mirror as tm
    import astrabox.seams.model as model_seam

    monkeypatch.setattr(ps, "claim_prepared_slot", _win)
    monkeypatch.setattr(startup, "clear_claimed_slot", _clear)
    monkeypatch.setattr(pb, "adopt_claimed_box", _adopt)
    monkeypatch.setattr(manager, "record_startup_allocation", _record)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda name: _Endpoint())
    monkeypatch.setattr(ps, "mark_gateway_entry_claimed", _mark)
    monkeypatch.setattr(tm, "bind_mirror_target", _target)
    monkeypatch.setattr(codex.CodexAppServerLink, "connect", staticmethod(_connect))
    monkeypatch.setattr(codex, "_publish_runtime", _publish)
    refills: list[str] = []
    monkeypatch.setattr(
        startup,
        "schedule_prepared_runtime_refill",
        lambda template_arg, manager_arg: refills.append(template_arg.agent_id),
    )

    async def _provision(*_args: Any, **_kwargs: Any) -> Any:
        return await _claim(
            manager,
            template=template,
            user_id="user-1",
            backend=_Backend(vault=_vault),
        )

    monkeypatch.setattr(startup, "provision_engine_sandbox", _provision)

    runtime = asyncio.run(
        startup.start_platform_runtime(
            manager,
            codex.CodexEngineAdapter(),
            session_id="session-1",
            assignment_id="assignment-1",
            template=template,
            workspace_plan=_plan(),
            user_id="user-1",
            permission_mode="workspace-write",
            progress_callback=None,
            callback_url=None,
        )
    )
    assert runtime is not None and runtime.session_id == "session-1"
    from astrabox.seams.egress_credentials import workload_credential_name

    assert events == [
        "claim",
        "adopt:session-1",
        "allocation:box-77:sandbox",
        "mark:slot-abc:session-1",
        "mint:session-1",
        f"vault:{workload_credential_name('slot-abc')}",
        "mirror-target:session-1:/workspace",
        "link-connect",
        "publish",
    ]
    assert cleared == ["slot-abc"]
    assert refills == ["agent-1"]
    publish = published[0]
    # Activation rides the vendor RPC: a fresh thread (no engine key), the
    # Session's own mode/instructions/model, the gateway's provider table.
    assert publish["engine_session_key"] is None
    assert publish["permission_mode"] == "workspace-write"
    assert publish["instructions"] == "Be terse."
    assert publish["model"] == "gpt-5.3-codex"
    assert publish["base_url"] == "https://gw.test"
    assert publish["model_catalog"] is True
    # The manifest identity is re-pointed to the Session, never re-planned.
    identity = publish["runtime_identity"]
    assert identity["session_id"] == "session-1"
    assert identity["sandbox_id"] == "box-77"
