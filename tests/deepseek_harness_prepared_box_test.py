"""DeepSeek Harness prepared boxes: the fingerprint and the claim.

The harness's runtime is a supervised image service, so its prepared unit is
the booted box and activation is `session.create` on the claiming Session's
own connection. The fingerprint tests pin WHICH facts are box-frozen: the
image, the gateway allowlist, the agent preset and the Agent's instructions all
retire a warm box, because the conversation is composed from the last two at
PREPARE and a claim rejoins it by name; the permission preset rides the claim
and must not.
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
    EnginePreparationContext,
    EngineStartupContext,
)
from astrabox.core.service.orchestrator.engine.deepseek_harness_client import DSH_MODEL_PROVIDER


def _template(**overrides: Any) -> Any:
    base = dict(
        agent_id="agent-1",
        engine_kind="deepseek_harness",
        agent_prewarm_fingerprint="fp-1",
        runtime_generation="fp-1",
        prepared_capacity=1,
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
        user_id="user-1",
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
    monkeypatch.setattr(dsh, "_native_model_base_url", lambda _access, base_url: base_url)


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
        placement=pb.PLACEMENT_CONVERSATION_BOX,
        sandbox=sandbox or SimpleNamespace(sandbox_id="box-9"),
        sandbox_id="box-9",
        cwd="/workspace",
        runtime_identity={
            "workspace_dir": "/workspace", "sandbox_id": "box-9",
            "linux_user": "user", "home_dir": "/home/user",
            "sandbox_tenancy": "conversation",
        },
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


# ── spawn fingerprint ─────────────────────────────────────────────────────


def test_spawn_fingerprint_is_stable_for_the_same_frozen_facts() -> None:
    assert dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    ) == dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    )


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
    base = dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    )
    changed = dsh._slot_spawn_fingerprint(
        _template(**template_overrides), base_url=base_url, native_base_url=base_url
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

    base = dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    )
    changed = dsh._slot_spawn_fingerprint(
        _template(**overrides), base_url="https://gw.test", native_base_url="https://gw.test"
    )
    assert changed != base


def test_claim_borne_facts_do_not_touch_the_fingerprint() -> None:
    """What activation still delivers must not retire a warm box.

    The permission preset is a ``commands/execute`` line every publish
    re-asserts, start and rejoin alike, and the model name never reaches the
    box at all — the harness's provider posts to the gateway, which routes.
    """

    base = dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    )
    changed = dsh._slot_spawn_fingerprint(
        _template(model_config={"model": "another-model"}),
        base_url="https://gw.test",
        native_base_url="https://gw.test",
    )
    assert changed == base


# ── preparation ───────────────────────────────────────────────────────────


class _FakeLink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((method, dict(payload)))
        if method == "session/modelCatalog":
            return {"groups": [{"id": DSH_MODEL_PROVIDER, "models": [{"id": "deepseek-chat"}]}]}
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

    async def _agents_md(
        sandbox: Any, *, cwd: str, instructions: str,
        runtime_identity: dict[str, Any] | None,
    ) -> None:
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
        ("session/create", {"args": {"request": {"cwd": "/workspace", "agentPreset": "code"}}}),
        ("session/modelCatalog", {"args": {}}),
    ]
    assert link.closed is True
    # The id is what the claim rejoins; without it a claim would have no
    # choice but to create a second conversation.
    assert receipt["prepared_native_session"] == "dsh-native-1"
    # The claim selects the model on this route without reading the catalog.
    assert receipt["prepared_model_route"] == {
        "model": "deepseek-chat",
        "route": DSH_MODEL_PROVIDER,
    }
    assert receipt["engine_kind"] == dsh.ENGINE_KIND
    assert receipt["spawn_fingerprint"] == dsh._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", native_base_url="https://gw.test"
    )


# ── claim gates ───────────────────────────────────────────────────────────


def _claimed_manifest(template: Any) -> dict[str, Any]:
    return {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "engine_initialized": True,
        "sandbox_id": "box-77",
        "cwd": "/workspace",
        "spawn_fingerprint": dsh._slot_spawn_fingerprint(
            template, base_url="https://gw.test", native_base_url="https://gw.test"
        ),
        "activation_mcp_servers": [],
        "gateway_substitution": True,
        "prepared_native_session": "dsh-native-1",
        "engine_kind": "deepseek_harness",
        "runtime_generation": "fp-1",
        "runtime_env": {},
        "environment_credential_contract": [],
        "workspace_id": "workspace-1",
        "runtime_identity": {
            "workspace_dir": "/workspace",
            "session_id": "slot-abc",
            "home_dir": "/home/agent",
            "linux_user": "agent",
        },
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
    user_id: str | None = "user-1",
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
        "engine_initialized": True,
        "sandbox_id": "box-77",
        "spawn_fingerprint": "spawn-of-an-older-image",
        "gateway_substitution": True,
        "runtime_identity": {"workspace_dir": "/workspace"},
    }

    discards: list[str] = []

    async def _discard(template: Any, manifest: dict[str, Any], *, reason: str, **_kwargs: Any) -> None:
        discards.append(reason)

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
    with pytest.raises(APIError, match="prepared harness box") as mismatch:
        asyncio.run(
            startup.start_platform_runtime(
                _Manager(),
                dsh.DeepSeekHarnessEngineAdapter(),
                session_id="session-1",
                assignment_id="assignment-1",
                template=_template(),
                workspace_plan=_plan(),
                user_id="user-1",
                permission_mode=None,
                progress_callback=None,
                callback_url=None,
            )
        )
    assert mismatch.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert len(discards) == 1
    assert "AGENT_PREWARM_CONFIG_INVALID" in discards[0]


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

    async def _win(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(claimed)

    async def _adopt(
        template_arg: Any,
        manifest: dict[str, Any],
        *,
        session_id: str,
        assignment_id: str,
    ) -> Any:
        # The native-state write precedes the link; the failure under test is the link's.
        return SimpleNamespace(sandbox_id="box-77", files=SimpleNamespace(write_bytes=_target))

    async def _connect(sandbox: Any, *, port: int = 44780, launch_url_path: str, observe_only: bool = False) -> Any:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="the box went away between prepare and activation",
            status_code=502,
        )

    discards: list[str] = []

    async def _discard(template_arg: Any, manifest: dict[str, Any], *, reason: str, **_kwargs: Any) -> None:
        discards.append(reason)

    monkeypatch.setattr(ps, "claim_prepared_seat", _win)
    monkeypatch.setattr(pb, "adopt_claimed_box", _adopt)
    monkeypatch.setattr(startup, "_discard_claimed_unit", _discard)
    monkeypatch.setattr(dsh.DshApiLink, "connect", staticmethod(_connect))

    async def _mark(*args: Any, **kwargs: Any) -> None:
        return None

    async def _vault(sandbox: Any, **kwargs: Any) -> None:
        return None

    async def _target(*args: Any, **kwargs: Any) -> None:
        return None

    async def _provision(*args: Any, **kwargs: Any) -> Any:
        monkeypatch.setattr(ps, "record_gateway_entry", _mark)
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
                user_id="user-1",
                permission_mode=None,
                progress_callback=None,
                callback_url=None,
            )
        )
    assert len(discards) == 1 and "between prepare and activation" in discards[0]
