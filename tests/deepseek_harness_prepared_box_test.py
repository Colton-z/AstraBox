"""DeepSeek Harness prepared boxes: the placement, the fingerprint, the claim.

The harness's runtime is a supervised image service, so its prepared unit is
the booted box and activation is `session.create` on the claiming Session's
own connection. Three things have to hold for that to be safe, and each is
pinned here:

* the placement declaration the allocator gates on, because a regression
  there stops preparation silently rather than loudly;
* WHICH facts are box-frozen — the image, the gateway allowlist, the agent
  preset and the Agent's instructions all retire a warm box, because the
  conversation is composed from the last two at PREPARE and a claim rejoins
  it by name; the permission preset rides the claim and must not;
* the activation ORDER — the mirror target precedes the first turn, because
  the relay starts a fail-loud clock on a session log it cannot deliver and
  preparation's unclaimed marker is what holds that clock off until here.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb
from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.core.service.orchestrator.engine import deepseek_harness as dsh
from astrabox.core.service.orchestrator.engine import provisioning, startup
from astrabox.core.service.orchestrator.engine import transcript_mirror as tm
from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    EnginePreparationContext,
    EngineStartupContext,
)


def _template(**overrides: Any) -> Any:
    base = dict(
        agent_id="agent-1",
        engine_kind="deepseek_harness",
        agent_prewarm_fingerprint="fp-1",
        runtime_generation="fp-1",
        prewarm_enabled=True,
        model_config={},
        system="Be terse.",
        engine_options={"session_create": {"agentPreset": "code"}},
        runtime_template_name="astrabox/sandbox-deepseek-harness:latest",
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
            model_name="deepseek-chat",
        )

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.allocations.append((session_id, allocation))

    async def resolve_session_egress_credentials(
        self, session_id: str, **kwargs: Any
    ) -> list[Any]:
        return []

    @property
    def deployment_settings(self) -> Any:
        return SimpleNamespace(mcp_proxy_base_url="http://backend.dsh.test:8000")


def _plan(resume: str | None = None) -> Any:
    return SimpleNamespace(
        resume_engine_session_key=resume,
        cwd="/workspace",
        subject_kind="deployment_conversation",
        agent_id="agent-1",
        assistant_id=None,
        user_id=None,
        engine_kind="deepseek_harness",
    )


@pytest.fixture(autouse=True)
def _platform_claim_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mounts(**kwargs: Any) -> tuple[tuple[str, str], ...]:
        return ()

    async def _ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(provisioning, "plan_workspace_mounts", _mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", _ready)


def _preparation_context(
    *,
    template: Any | None = None,
    sandbox: Any | None = None,
    slot_id: str = "slot-abc",
) -> EnginePreparationContext:
    selected = template or _template()
    return EnginePreparationContext(
        template=selected,
        slot_id=slot_id,
        activation_token="token-ignored",
        placement=pb.PLACEMENT_CONVERSATION_BOX,
        sandbox=sandbox or SimpleNamespace(sandbox_id="box-9"),
        sandbox_id="box-9",
        cwd="/workspace",
        runtime_identity={"workspace_dir": "/workspace", "sandbox_id": "box-9"},
        model_access=_Manager().resolve_model_access(selected.model_config),
        model_credential="placeholder",
        runtime_env={},
        runner_uri=None,
        preparation_fingerprint="fp-1",
        deployment_settings=_Manager().deployment_settings,
        workspace_id="workspace-1",
        gateway_substitution=True,
    )


def _startup_context(
    *,
    template: Any | None = None,
    prepared: dict[str, Any] | None = None,
    sandbox: Any | None = None,
    permission_mode: str | None = None,
) -> EngineStartupContext:
    selected = template or _template()
    return EngineStartupContext(
        session_id="session-1",
        template=selected,
        workspace_plan=_plan(),
        sandbox=sandbox or SimpleNamespace(sandbox_id="box-77"),
        sandbox_id="box-77",
        cwd="/workspace",
        runtime_identity={
            "workspace_dir": "/workspace",
            "sandbox_id": "box-77",
            "session_id": "session-1",
        },
        model_access=_Manager().resolve_model_access(selected.model_config),
        model_credential="placeholder",
        resume_session_key=None,
        prepared_manifest=prepared,
        permission_mode=permission_mode,
        deployment_settings=_Manager().deployment_settings,
    )


# ── placement ─────────────────────────────────────────────────────────────


def test_the_adapter_declares_the_whole_box_placement() -> None:
    """Both halves of what ``prepare_box_for_agent`` gates on, before building.

    The allocator refuses to build anything for an adapter that either
    inherits the refusing default or does not declare the whole-box form. The
    harness qualifies for one reason it owns — its server is a boot-time
    service, so there is no engine child a shared-box slot could park — and
    losing either half here would stop preparation with no error anywhere.
    """

    adapter = dsh.DeepSeekHarnessEngineAdapter()
    assert type(adapter).prepares_conversation_box is True
    assert (
        type(adapter).prepare_runtime is not EngineAdapter.prepare_runtime
    )
    # The placement is per-conversation now, but the PREPARED unit is still
    # a whole box for that tenancy; the Agent-shared tenancy takes a slot the
    # platform mints (prepare_runtime returns the shared_slot receipt)
    # rather than a box-shaped unit.
    assert (
        adapter.capabilities.conversation_placement
        == "per_conversation_account"
    )


# ── spawn fingerprint ─────────────────────────────────────────────────────


def test_spawn_fingerprint_is_stable_for_the_same_frozen_facts() -> None:
    assert dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test"
    ) == dsh._slot_spawn_fingerprint(_template(), base_url="https://gw.test")


@pytest.mark.parametrize(
    ("base_url", "template_overrides"),
    [
        # The egress allowlist and the server's own DEEPSEEK_BASE_URL are both
        # composed from this at create; a claimed box would dead-end.
        ("https://other-gw.test", {}),
        # The image carries the pinned vendor release, the profile overlay
        # that selects the mirror's encoding, and the mirror's root/glob.
        (
            "https://gw.test",
            {"runtime_template_name": "astrabox/sandbox-deepseek-harness:next"},
        ),
    ],
)
def test_spawn_fingerprint_retires_a_box_when_a_frozen_fact_changes(
    base_url: str, template_overrides: dict[str, Any]
) -> None:
    base = dsh._slot_spawn_fingerprint(_template(), base_url="https://gw.test")
    changed = dsh._slot_spawn_fingerprint(
        _template(**template_overrides), base_url=base_url
    )
    assert changed != base


@pytest.mark.parametrize(
    "overrides",
    [
        {"engine_options": {"session_create": {"agentPreset": "cordis"}}},
        {"system": "Entirely different instructions."},
    ],
    ids=["preset", "instructions"],
)
def test_a_change_to_the_prepared_conversation_retires_the_box(
    overrides: dict[str, Any],
) -> None:
    """Both facts the prepared conversation is composed with are frozen.

    The harness pins a preset into a session when it creates it, and the
    instructions plugin composes AGENTS.md at that same moment. A claim
    rejoins that conversation by name and re-asserts neither — so if either
    changed, serving the warm box would run this Session under the previous
    Agent, and the change would take effect only once the box aged out.
    """

    base = dsh._slot_spawn_fingerprint(_template(), base_url="https://gw.test")
    changed = dsh._slot_spawn_fingerprint(
        _template(**overrides), base_url="https://gw.test"
    )
    assert changed != base


def test_claim_borne_facts_do_not_touch_the_fingerprint() -> None:
    """What activation still delivers must not retire a warm box.

    The permission preset is a ``commands/execute`` line every publish
    re-asserts, start and rejoin alike, and the model name never reaches the
    box at all — the harness's provider posts to the gateway, which routes.
    """

    base = dsh._slot_spawn_fingerprint(_template(), base_url="https://gw.test")
    changed = dsh._slot_spawn_fingerprint(
        _template(model_config={"model": "another-model"}),
        base_url="https://gw.test",
    )
    assert changed == base


# ── preparation ───────────────────────────────────────────────────────────


class _FakeLink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, dict(payload)))
        return {"sessionId": "dsh-native-1"}

    async def close(self) -> None:
        self.closed = True


def test_preparation_creates_the_conversation_a_claim_will_rejoin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conversation is composed at prepare, not at claim.

    Every ``session.create`` field belongs to the Agent, so preparation owns
    the call and claim names the resulting id. Two writes must precede it: the
    unclaimed marker makes a targetless session log legitimate until a stated
    deadline, and AGENTS.md supplies the instructions plugin's source.
    """

    ordered: list[str] = []
    link = _FakeLink()

    async def _connect(sandbox: Any, *, port: int = 44780, launch_url_path: str) -> Any:
        # Box-tenancy paths keep dialing the image's fixed forwarder port.
        assert port == 44780
        ordered.append("link-connect")
        return link

    async def _agents_md(sandbox: Any, *, cwd: str, instructions: str) -> None:
        ordered.append(f"agents-md:{cwd}:{instructions}")

    monkeypatch.setattr(dsh.DshApiLink, "connect", staticmethod(_connect))
    monkeypatch.setattr(dsh, "_install_agent_instructions", _agents_md)

    receipt = asyncio.run(
        dsh.DeepSeekHarnessEngineAdapter().prepare_runtime(_preparation_context())
    )

    assert ordered == [
        "agents-md:/workspace:Be terse.",
        "link-connect",
    ]
    assert link.calls == [
        ("session/create", {"args": {"request": {"cwd": "/workspace", "agentPreset": "code"}}})
    ]
    assert link.closed is True
    # The id is what the claim rejoins; without it a claim would have no
    # choice but to create a second conversation.
    assert receipt["prepared_native_session"] == "dsh-native-1"
    assert receipt["engine_kind"] == dsh.ENGINE_KIND
    assert receipt["spawn_fingerprint"] == dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test"
    )


# ── claim gates ───────────────────────────────────────────────────────────


def _claimed_manifest(template: Any) -> dict[str, Any]:
    return {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "cwd": "/workspace",
        "spawn_fingerprint": dsh._slot_spawn_fingerprint(
            template, base_url="https://gw.test"
        ),
        "activation_mcp_servers": [],
        "gateway_substitution": True,
        "prepared_native_session": "dsh-native-1",
        "engine_kind": "deepseek_harness",
        "runtime_generation": "fp-1",
        "runtime_env": {},
        "environment_credential_contract": [],
        "workspace_id": "workspace-1",
        "runtime_identity": {"workspace_dir": "/workspace", "session_id": "slot-abc"},
    }


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
    adapter = dsh.DeepSeekHarnessEngineAdapter()
    model_access = manager.resolve_model_access(selected.model_config)
    return await provisioning.claim_prepared_engine_sandbox(
        manager,
        session_id="session-1",
        assignment_id="assignment-1",
        template=selected,
        workspace_plan=workspace_plan or _plan(),
        user_id=user_id,
        request=adapter.sandbox_request(
            template=selected,
            model_access=model_access,
        ),
        backend_adapter=backend or _Backend(),
        credential="sk-shared",
        mcp_vault_write=None,
        vault_enabled=True,
        session_log=adapter.capabilities.session_log,
    )


def test_a_stale_spawn_fingerprint_discards_the_claimed_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "spawn_fingerprint": "spawn-of-an-older-image",
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

    async def _provision(*args: Any, **kwargs: Any) -> Any:
        return provisioned

    monkeypatch.setattr(startup, "provision_engine_sandbox", _provision)
    monkeypatch.setattr(startup, "_discard_claimed_unit", _discard)
    monkeypatch.setattr(
        startup,
        "schedule_prepared_runtime_refill",
        lambda template, manager: refills.append(template.agent_id),
    )
    with pytest.raises(APIError, match="prepared harness box") as mismatch:
        asyncio.run(
            startup.start_platform_runtime(
                _Manager(),
                dsh.DeepSeekHarnessEngineAdapter(),
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
    assert mismatch.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert len(discards) == 1
    assert "AGENT_PREWARM_CONFIG_INVALID" in discards[0]
    assert refills == ["agent-1"]


def test_a_manifest_naming_no_conversation_is_discarded_not_papered_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim rejoins; it must never quietly fall back to creating.

    Preparation records the conversation unconditionally, so a manifest
    without one means the manifest and this adapter disagree about what a
    prepared box holds. Creating a conversation here would restore exactly
    the nine seconds this form removed and hide the disagreement inside
    them — the claim would still work, only slowly, forever.
    """

    claimed = _claimed_manifest(_template())
    claimed.pop("prepared_native_session")

    async def _connect(sandbox: Any, *, port: int = 44780, launch_url_path: str) -> Any:
        # Box-tenancy paths keep dialing the image's fixed forwarder port.
        assert port == 44780
        raise AssertionError("no conversation may be created at claim")

    monkeypatch.setattr(dsh.DshApiLink, "connect", staticmethod(_connect))
    with pytest.raises(APIError, match="names no prepared conversation"):
        asyncio.run(
            dsh.DeepSeekHarnessEngineAdapter().activate_runtime(
                _startup_context(prepared=claimed)
            )
        )


def test_an_endpoint_with_no_session_identity_keeps_the_shared_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-Session attribution is optional at the model-provider seam.

    Which endpoint is asked comes from the Environment's declared name — core
    never names a gateway product. ``None`` means this endpoint does not add
    per-Session attribution; it does not mean authorization failed, so the
    prepared claim keeps the shared credential instead of blocking the user.
    """

    asked: list[str | None] = []
    template = _template(model_config={"endpoint_provider": "acme-gateway"})

    class _NoIdentityEndpoint:
        async def ensure_session_credential(self, *, context: Any) -> str | None:
            return None

    def _endpoint_for(name: str | None) -> Any:
        asked.append(name)
        return _NoIdentityEndpoint()

    import astrabox.seams.model as model_seam

    monkeypatch.setattr(model_seam, "model_endpoint_for_name", _endpoint_for)

    credential = asyncio.run(
        ps.claimed_gateway_credential(
            template=template,
            session_id="session-1",
            user_id="user-1",
            shared_credential="sk-shared",
        )
    )
    assert credential == "sk-shared"
    assert asked == ["acme-gateway"]


# ── activation order ──────────────────────────────────────────────────────


def test_activation_adopts_binds_and_publishes_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim's ordering IS the contract each step's safety rests on.

    Metadata before the allocation record (a half-claimed box must stay the
    reaper's to destroy); the gateway swap before anything can reach a model;
    the mirror target and AGENTS.md before the vendor conversation exists —
    the relay treats a session log with no target as a misconfiguration and
    the instructions plugin composes from the file at session creation; and
    READY only through the publish.
    """

    template = _template()
    manager = _Manager()
    events: list[str] = []
    claimed = _claimed_manifest(template)

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

    asked: list[str | None] = []

    class _Endpoint:
        async def ensure_session_credential(self, *, context: Any) -> str:
            events.append(f"mint:{context.conversation_id}:{context.user_id}")
            return "sk-session-key"

    def _endpoint_for(name: str | None) -> Any:
        asked.append(name)
        return _Endpoint()

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

    async def _target(
        sandbox: Any, manager_arg: Any, session_id: str, *, cwd: str
    ) -> None:
        events.append(f"mirror-target:{session_id}:{cwd}")

    async def _connect(sandbox: Any, *, port: int = 44780, launch_url_path: str) -> Any:
        # Box-tenancy paths keep dialing the image's fixed forwarder port.
        assert port == 44780
        events.append("link-connect")
        return _FakeLink()

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
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", _endpoint_for)
    monkeypatch.setattr(ps, "mark_gateway_entry_claimed", _mark)
    monkeypatch.setattr(tm, "bind_mirror_target", _target)
    monkeypatch.setattr(dsh.DshApiLink, "connect", staticmethod(_connect))
    monkeypatch.setattr(dsh, "_publish_runtime", _publish)
    refills: list[str] = []
    monkeypatch.setattr(
        startup,
        "schedule_prepared_runtime_refill",
        lambda template_arg, manager_arg: refills.append(template_arg.agent_id),
    )

    async def _provision(*args: Any, **kwargs: Any) -> Any:
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
            dsh.DeepSeekHarnessEngineAdapter(),
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

    # Resolved through the model seam by the Environment's declared endpoint
    # name — never by naming a gateway product in core (provider_boundary).
    assert asked == [None]
    assert events == [
        "claim",
        "adopt:session-1",
        "allocation:box-77:sandbox",
        "mark:slot-abc:session-1",
        "mint:session-1:user-1",
        f"vault:{workload_credential_name('slot-abc')}",
        "mirror-target:session-1:/workspace",
        "link-connect",
        "publish",
    ]
    assert cleared == ["slot-abc"]
    assert refills == ["agent-1"]
    publish = published[0]
    # Activation rejoins the prepared conversation rather than creating one,
    # and asserts this Session's permission preset on it.
    assert publish["engine_session_key"] == "dsh-native-1"
    assert publish["session_create"] == {"agentPreset": "code"}
    assert publish["permission_mode"] == "workspace-write"
    assert publish["terminal_cwd"] == "/workspace"
    # The manifest identity is re-pointed to the Session, never re-planned.
    identity = publish["runtime_identity"]
    assert identity["session_id"] == "session-1"
    assert identity["sandbox_id"] == "box-77"


def test_a_failed_activation_discards_the_box_and_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a silent retry into the same box.

    Activation writes into the box — the gateway identity, the mirror target,
    the instructions file — so a failure part-way leaves state no second
    attempt can reason about. The box is destroyed, this startup fails, and
    the refill reseeds warmth for the next one.
    """

    template = _template()
    claimed = _claimed_manifest(template)
    manager = _Manager()

    class _Endpoint:
        async def ensure_session_credential(self, *, context: Any) -> str:
            assert context.conversation_id == "session-1"
            return "sk-session-key"

    import astrabox.seams.model as model_seam

    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda name: _Endpoint())

    async def _win(**kwargs: Any) -> dict[str, Any]:
        return dict(claimed)

    async def _adopt(
        template_arg: Any,
        manifest: dict[str, Any],
        *,
        session_id: str,
        assignment_id: str,
    ) -> Any:
        return SimpleNamespace(sandbox_id="box-77")

    async def _connect(sandbox: Any, *, port: int = 44780, launch_url_path: str) -> Any:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the box went away between prepare and activation",
            status_code=502,
        )

    discards: list[str] = []

    async def _discard(template_arg: Any, manifest: dict[str, Any], *, reason: str) -> None:
        discards.append(reason)

    refills: list[str] = []
    monkeypatch.setattr(ps, "claim_prepared_slot", _win)
    monkeypatch.setattr(pb, "adopt_claimed_box", _adopt)
    monkeypatch.setattr(startup, "_discard_claimed_unit", _discard)
    monkeypatch.setattr(dsh.DshApiLink, "connect", staticmethod(_connect))
    monkeypatch.setattr(
        startup,
        "schedule_prepared_runtime_refill",
        lambda template_arg, manager_arg: refills.append(template_arg.agent_id),
    )

    async def _mark(**kwargs: Any) -> None:
        return None

    async def _vault(sandbox: Any, **kwargs: Any) -> None:
        return None

    async def _target(*args: Any, **kwargs: Any) -> None:
        return None

    async def _provision(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(ps, "mark_gateway_entry_claimed", _mark)
        monkeypatch.setattr(tm, "bind_mirror_target", _target)
        return await _claim(manager, template=template, backend=_Backend(vault=_vault))

    monkeypatch.setattr(startup, "provision_engine_sandbox", _provision)

    with pytest.raises(APIError, match="between prepare and activation"):
        asyncio.run(
            startup.start_platform_runtime(
                manager,
                dsh.DeepSeekHarnessEngineAdapter(),
                session_id="session-1",
                assignment_id="assignment-1",
                template=template,
                workspace_plan=_plan(),
                user_id=None,
                permission_mode=None,
                progress_callback=None,
                callback_url=None,
            )
        )
    assert len(discards) == 1 and "between prepare and activation" in discards[0]
    assert refills == ["agent-1"]
