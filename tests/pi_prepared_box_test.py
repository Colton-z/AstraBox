"""Pi prepared boxes: the placement, the box barrier, and the claim path.

Three contracts are pinned here, and each one is a decision that reads as
arbitrary until it is broken.

*Which facts the box freezes.* Pi's prepared box holds no started process, so
everything ``pi --mode rpc`` reads at startup — the Agent's ``AGENTS.md``, the
thinking level, the working directory — is applied by the claim and must NOT
retire a warm box, while the gateway and model the image renders into
``models.json`` at boot must.

*That the box can hold a conversation at all.* The image renders that provider
table in the background behind its init, so a box reaching Running proves
nothing; preparation reads it back, and a box that fails is destroyed instead
of published.

*The order of the claim.* The transcript-mirror target has to land before pi
exists, because pi writes its session file from startup and the in-box relay
turns FATAL on a session log it has no target for.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from unittest.mock import AsyncMock

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.agent import prepared_boxes as pb
from astrabox.core.service.orchestrator.agent import prepared_slots as ps
from astrabox.core.service.orchestrator.engine import pi, provisioning, startup
from astrabox.core.service.orchestrator.engine.base import EnginePreparationContext


def _template(**overrides: Any) -> Any:
    base = dict(
        agent_id="agent-1",
        engine_kind="pi",
        agent_prewarm_fingerprint="fp-1",
        runtime_generation="fp-1",
        prewarm_enabled=True,
        model_config={},
        system="Be terse.",
        engine_options={"settings": {"defaultThinkingLevel": "medium"}},
        runtime_template_name="astrabox/sandbox-pi:latest",
        sandbox_backend="open_sandbox",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _access(model: str = "deepseek-chat", base_url: str = "https://gw.test") -> Any:
    return SimpleNamespace(
        base_url=base_url,
        credential="sk-shared",
        credential_kind="bearer",
        model_name=model,
    )


class _Manager:
    def __init__(self, access: Any | None = None) -> None:
        self.allocations: list[tuple[str, Any]] = []
        self._access = access if access is not None else _access()

    def resolve_model_access(self, model_config: Any) -> Any:
        return self._access

    async def record_startup_allocation(self, session_id: str, allocation: Any) -> None:
        self.allocations.append((session_id, allocation))

    async def resolve_session_egress_credentials(
        self, session_id: str, **kwargs: Any
    ) -> list[Any]:
        return []

    @property
    def deployment_settings(self) -> Any:
        return SimpleNamespace(mcp_proxy_base_url="http://backend.pi.test:8000")


def _plan(resume: str | None = None) -> Any:
    return SimpleNamespace(
        resume_engine_session_key=resume,
        cwd="/workspace",
        subject_kind="deployment_conversation",
        agent_id="agent-1",
        assistant_id=None,
        user_id=None,
        engine_kind="pi",
    )


@pytest.fixture(autouse=True)
def _platform_claim_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _mounts(**_kwargs: Any) -> tuple[tuple[str, str], ...]:
        return ()

    async def _ready(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(provisioning, "plan_workspace_mounts", _mounts)
    monkeypatch.setattr(provisioning, "workspace_is_ready", _ready)


def _preparation_context(
    *,
    manager: _Manager | None = None,
    template: Any | None = None,
    sandbox: Any | None = None,
) -> EnginePreparationContext:
    selected_manager = manager or _Manager()
    selected = template or _template()
    return EnginePreparationContext(
        template=selected,
        slot_id="slot-abc",
        activation_token="token",
        placement=pb.PLACEMENT_CONVERSATION_BOX,
        sandbox=sandbox or _box(_models_config("deepseek-chat")),
        sandbox_id="box-77",
        cwd="/workspace",
        runtime_identity={"workspace_dir": "/workspace", "sandbox_id": "box-77"},
        model_access=selected_manager.resolve_model_access(selected.model_config),
        model_credential="placeholder",
        runtime_env={},
        runner_uri=None,
        preparation_fingerprint="fp-1",
        deployment_settings=selected_manager.deployment_settings,
        workspace_id="workspace-1",
        gateway_substitution=True,
    )


def _models_config(*models: str, provider: str = pi.PI_PROVIDER_NAME) -> str:
    return (
        '{"providers": {"'
        + provider
        + '": {"baseUrl": "https://gw.test", "api": "openai-completions", '
        '"apiKey": "$ASTRABOX_PI_API_KEY", "models": ['
        + ", ".join('{"id": "' + model + '"}' for model in models)
        + "]}}}"
    )


class _Files:
    """The in-box filesystem face, answering only what this adapter reads."""

    def __init__(self, content: str | None) -> None:
        self._content = content
        self.reads: list[str] = []
        self.writes: list[tuple[str, bytes]] = []

    async def read_file(self, path: str) -> str:
        self.reads.append(path)
        if self._content is None:
            raise FileNotFoundError(path)
        return self._content

    async def write_file(
        self,
        path: str,
        content: bytes,
        *,
        mode: int | None = None,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        _ = (mode, owner, group)
        self.writes.append((path, content))


class _Commands:
    """The exec face preparation uses to place the instructions file."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, command: str) -> Any:
        self.ran.append(command)
        return SimpleNamespace(error=None, exit_code=0, stdout="", stderr="")


def _box(content: str | None, sandbox_id: str = "box-77") -> Any:
    return SimpleNamespace(
        sandbox_id=sandbox_id, files=_Files(content), commands=_Commands()
    )


# ── placement ─────────────────────────────────────────────────────────────


def test_pi_prepares_a_whole_conversation_box() -> None:
    """The declaration the allocator checks before it builds anything.

    Pi's only runtime profile is conversation tenancy, so the Agent-shared
    slot form is unreachable for it; declaring the whole-box form is what
    makes a prepared unit possible at all rather than a silent no-op.
    """

    from astrabox.core.service.orchestrator.engine.base import EngineAdapter
    from astrabox.seams.sandbox import SANDBOX_TENANCY_CONVERSATION

    adapter = pi.PiEngineAdapter()
    assert adapter.prepares_conversation_box is True
    assert (
        type(adapter).prepare_runtime is not EngineAdapter.prepare_runtime
    )
    # Tenancy is the platform's to compose now; what stays this adapter's is
    # that its integration still places every conversation on the box account,
    # which is why the whole box remains the prepared unit — and why the
    # shared tenancy refuses it until the phase-2 per-conversation service
    # instantiation lands.
    # Per-conversation placement now; the PREPARED unit is still a whole
    # box, and prepare_runtime refuses the agent tenancy loudly.
    assert (
        adapter.capabilities.conversation_placement
        == "per_conversation_account"
    )


def test_pi_keeps_its_transcript_in_a_file_the_platform_must_mirror() -> None:
    """Why a prepared pi box needs the deferred mirror target at all.

    A declared session log is what puts a slot box on the deferred-target
    create; without the declaration the claim's ``bind_mirror_target`` would
    be writing into a box whose relay never reads it.
    """

    from astrabox.core.service.orchestrator.engine.provisioning import (
        _session_log_declaration,
    )

    declaration = _session_log_declaration(_template())
    assert declaration is not None
    assert declaration.root_template == pi.PI_SESSION_DIR_TEMPLATE


# ── which facts the box freezes ───────────────────────────────────────────


def test_spawn_fingerprint_is_stable_for_the_same_frozen_facts() -> None:
    a = pi._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", model="deepseek-chat"
    )
    b = pi._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", model="deepseek-chat"
    )
    assert a == b


@pytest.mark.parametrize(
    ("kwargs", "template_overrides"),
    [
        ({"model": "another-model"}, {}),
        ({"base_url": "https://other-gw.test"}, {}),
        ({}, {"runtime_template_name": "astrabox/sandbox-pi:next"}),
    ],
)
def test_spawn_fingerprint_retires_a_box_when_a_boot_rendered_fact_changes(
    kwargs: dict[str, Any], template_overrides: dict[str, Any]
) -> None:
    """These three are rendered into the box at boot and never re-read.

    The image writes one provider table with one model in it from the create's
    environment, so a claim under a different model or gateway would spawn pi
    against a table that cannot serve it.
    """

    base = pi._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", model="deepseek-chat"
    )
    changed = pi._slot_spawn_fingerprint(
        _template(**template_overrides),
        base_url=kwargs.get("base_url", "https://gw.test"),
        model=kwargs.get("model", "deepseek-chat"),
    )
    assert changed != base


def test_spawn_borne_facts_do_not_touch_the_fingerprint() -> None:
    """The inverse of an engine that parks a child before its claim.

    Pi's prepared box holds no process, so the Agent's instructions and its
    thinking level are read by the spawn the CLAIM performs. Retiring a warm
    box over them would rebuild boxes for a change activation already carries.
    """

    base = pi._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", model="deepseek-chat"
    )
    changed = pi._slot_spawn_fingerprint(
        _template(
            system="Entirely different instructions.",
            engine_options={"settings": {"defaultThinkingLevel": "xhigh"}},
        ),
        base_url="https://gw.test",
        model="deepseek-chat",
    )
    assert changed == base


# ── the box barrier ───────────────────────────────────────────────────────


def test_the_barrier_accepts_a_table_naming_this_model() -> None:
    box = _box(_models_config("deepseek-chat"))
    asyncio.run(pi._await_rendered_models_config(box, model="deepseek-chat"))
    assert box.files.reads == [pi.PI_MODELS_CONFIG_PATH]


def test_the_barrier_refuses_a_table_naming_another_model() -> None:
    """Refused without waiting: the renderer runs once, from create-time env.

    A box whose table names another model cannot be repaired by a claim, and
    polling until the deadline would only delay the discard.
    """

    box = _box(_models_config("some-other-model"))
    with pytest.raises(APIError) as raised:
        asyncio.run(pi._await_rendered_models_config(box, model="deepseek-chat"))
    assert raised.value.code == "AGENT_PREWARM_CONFIG_INVALID"
    assert box.files.reads == [pi.PI_MODELS_CONFIG_PATH]


def test_the_barrier_refuses_a_box_whose_renderer_never_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_POLL_SECONDS", 0.01)
    box = _box(None)
    with pytest.raises(APIError) as raised:
        asyncio.run(pi._await_rendered_models_config(box, model="deepseek-chat"))
    assert raised.value.code == "AGENT_RUNTIME_ERROR"
    assert pi.PI_MODELS_CONFIG_PATH in str(raised.value.message)
    # It kept asking rather than giving up on the first missing read: the
    # renderer is backgrounded behind the image's init.
    assert len(box.files.reads) > 1


def test_the_barrier_waits_out_a_table_that_is_not_pis_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document without this adapter's provider is "not yet", not "wrong"."""

    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_POLL_SECONDS", 0.01)
    box = _box(_models_config("deepseek-chat", provider="someone-else"))
    with pytest.raises(APIError) as raised:
        asyncio.run(pi._await_rendered_models_config(box, model="deepseek-chat"))
    assert raised.value.code == "AGENT_RUNTIME_ERROR"
    assert len(box.files.reads) > 1


def test_preparation_refuses_a_box_that_fails_its_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad provider table never produces engine preparation evidence."""

    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi, "PI_MODELS_CONFIG_POLL_SECONDS", 0.01)

    with pytest.raises(APIError):
        asyncio.run(
            pi.PiEngineAdapter().prepare_runtime(
                _preparation_context(sandbox=_box(None))
            )
        )


def test_preparation_refuses_an_environment_with_no_gateway() -> None:
    """Nothing observes a prepared box's boot log, so the refusal is here.

    On the cold path the image's renderer fails loud and a Session start sees
    it; a prepared box would simply sit unusable until a claim inherited it.
    """

    manager = _Manager(_access(base_url=""))
    with pytest.raises(APIError) as raised:
        asyncio.run(
            pi.PiEngineAdapter().prepare_runtime(
                _preparation_context(manager=manager)
            )
        )
    assert raised.value.code == "ENGINE_CAPABILITY_UNAVAILABLE"


def test_the_receipt_carries_the_digest_the_claim_recomputes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare and claim must derive the same digest from the same Agent.

    Two independent derivations of one digest are how a warm box ends up
    discarded on every claim, or never discarded at all.
    """

    # Parking dials execd for a real pipe; this test is about the receipt's
    # digest, so the pipe is named rather than imitated.
    monkeypatch.setattr(pi, "_park_pi_child", AsyncMock(return_value="pty-parked"))

    receipt = asyncio.run(
        pi.PiEngineAdapter().prepare_runtime(_preparation_context())
    )
    assert receipt["engine_kind"] == "pi"
    assert receipt["spawn_fingerprint"] == pi._slot_spawn_fingerprint(
        _template(), base_url="https://gw.test", model="deepseek-chat"
    )


def test_preparation_places_the_agents_context_before_parking_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the Agent's instructions actually reach the box.

    pi reads its context files once, at startup, and the prepared unit parks a
    child that has already started — so AGENTS.md has to be on disk before the
    parking, and nothing later can put it there. Covered on the preparation step
    rather than on the claim, because the claim deliberately does not rewrite it
    (an in-box exec, 8.3s, to reproduce bytes that are already correct): a test
    anchored there would be asserting a step that must not exist.
    """

    monkeypatch.setattr(pi, "_park_pi_child", AsyncMock(return_value="pty-parked"))
    installed: list[tuple[str, str]] = []

    async def _install(sandbox: Any, *, cwd: str, instructions: str) -> None:
        assert pi._park_pi_child.await_count == 0, (  # type: ignore[attr-defined]
            "a child parked before this has already read the file it needs"
        )
        installed.append((cwd, instructions))

    monkeypatch.setattr(pi, "_install_agent_instructions", _install)

    asyncio.run(
        pi.PiEngineAdapter().prepare_runtime(_preparation_context())
    )

    assert installed == [("/workspace", "Be terse.")]


# ── claim gates ───────────────────────────────────────────────────────────


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
    adapter = pi.PiEngineAdapter()
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


def test_a_stale_spawn_fingerprint_discards_the_claimed_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "spawn_fingerprint": "the-digest-of-a-retired-gateway",
        "gateway_substitution": True,
        "runtime_identity": {"workspace_dir": "/workspace"},
    }

    discards: list[str] = []

    async def _discard(template: Any, manifest: dict[str, Any], *, reason: str) -> None:
        discards.append(reason)

    refills: list[str] = []
    provisioned = provisioning.ProvisionedEngineSandbox(
        sandbox=_box(_models_config("deepseek-chat")),
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
    with pytest.raises(APIError, match="prepared pi box") as caught:
        asyncio.run(
            startup.start_platform_runtime(
                _Manager(),
                pi.PiEngineAdapter(),
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


# ── activation ────────────────────────────────────────────────────────────


def _activation_harness(
    monkeypatch: pytest.MonkeyPatch, template: Any, manager: _Manager
) -> tuple[list[str], list[dict[str, Any]], list[str], list[str]]:
    """Wire every seam the claim touches to an event recorder."""

    events: list[str] = []
    published: list[dict[str, Any]] = []
    cleared: list[str] = []
    refills: list[str] = []

    claimed = {
        "slot_id": "slot-abc",
        "state": "claimed",
        "placement": pb.PLACEMENT_CONVERSATION_BOX,
        "sandbox_id": "box-77",
        "cwd": "/workspace",
        "spawn_fingerprint": pi._slot_spawn_fingerprint(
            template, base_url="https://gw.test", model="deepseek-chat"
        ),
        "activation_mcp_servers": [],
        "gateway_substitution": True,
        "engine_kind": "pi",
        "runtime_generation": "fp-1",
        "runtime_env": {},
        "environment_credential_contract": [],
        "workspace_id": "workspace-1",
        "parked_pty_session_id": "pty-parked",
        "runtime_identity": {"workspace_dir": "/workspace", "session_id": "slot-abc"},
    }
    box_handle = SimpleNamespace(sandbox_id="box-77")

    async def _win(**kwargs: Any) -> dict[str, Any]:
        events.append("claim")
        return dict(claimed)

    async def _clear(*, agent_id: str, slot_id: str) -> None:
        cleared.append(slot_id)

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

    async def _unexpected_instructions(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("claim must not rewrite Agent-level instructions")

    async def _connect(sandbox: Any, **kwargs: Any) -> Any:
        # Resolving the execd endpoint, not starting pi: the client spawns the
        # process lazily inside the publish below, which puts the real spawn
        # one step later still than this event.
        events.append(f"pi-client:{kwargs['command']}")
        assert f"export PI_SUBAGENTS_TEMP_ROOT={kwargs['subagent_temp_root']}" in kwargs["command"]
        return SimpleNamespace(engine_session_key=None)

    async def _publish(**kwargs: Any) -> Any:
        events.append("publish")
        published.append(kwargs)
        return SimpleNamespace(session_id=kwargs["session_id"])

    import astrabox.core.service.orchestrator.engine.pi_client as pi_client
    import astrabox.core.service.orchestrator.engine.transcript_mirror as tm
    import astrabox.seams.model as model_seam
    monkeypatch.setattr(ps, "claim_prepared_slot", _win)
    monkeypatch.setattr(startup, "clear_claimed_slot", _clear)
    monkeypatch.setattr(pb, "adopt_claimed_box", _adopt)
    monkeypatch.setattr(manager, "record_startup_allocation", _record)
    monkeypatch.setattr(model_seam, "model_endpoint_for_name", lambda name: _Endpoint())
    monkeypatch.setattr(ps, "mark_gateway_entry_claimed", _mark)
    monkeypatch.setattr(tm, "bind_mirror_target", _target)
    monkeypatch.setattr(pi, "_install_agent_instructions", _unexpected_instructions)
    monkeypatch.setattr(pi_client, "connect_pi_client", _connect)
    monkeypatch.setattr(pi, "_publish_runtime", _publish)
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
    return events, published, cleared, refills


def test_activation_binds_the_mirror_target_before_parked_pi_is_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim's ordering IS the contract each step's safety rests on.

    Ownership metadata before the allocation record (a half-claimed box must
    stay the reaper's to destroy); the gateway swap and the mirror target
    before the prepared pi pipe is adopted, so the first model call and the
    first line of the session file each already have their destination.
    ``AGENTS.md`` was installed before that child started; a fingerprint
    mismatch retires the box, while claim must not rewrite Agent-level state.
    """

    template = _template()
    manager = _Manager()
    events, published, cleared, refills = _activation_harness(
        monkeypatch, template, manager
    )

    runtime = asyncio.run(
        startup.start_platform_runtime(
            manager,
            pi.PiEngineAdapter(),
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
    assert runtime is not None and runtime.session_id == "session-1"

    from astrabox.seams.egress_credentials import workload_credential_name

    assert [event.split(":")[0] for event in events] == [
        "claim",
        "adopt",
        "allocation",
        "mark",
        "mint",
        "vault",
        "mirror-target",
        "pi-client",
        "publish",
    ]
    assert not [event for event in events if event.startswith("agents-md")], (
        "a claim must not rewrite Agent-level content the preparation placed"
    )
    assert "adopt:session-1" in events
    assert "allocation:box-77:sandbox" in events
    assert f"vault:{workload_credential_name('slot-abc')}" in events
    assert "mirror-target:session-1:/workspace" in events
    assert cleared == ["slot-abc"]
    assert refills == ["agent-1"]


def test_the_claimed_spawn_takes_its_options_from_the_claiming_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing about the spawn was frozen at preparation, so all of it is live.

    ``--session`` is the one flag that must never appear: it would make a
    prepared box absorb a conversation, and the vendor's cwd-scoped session
    discovery answers an unmatched id with an interactive picker on the RPC
    pipe.
    """

    template = _template(engine_options={"settings": {"defaultThinkingLevel": "xhigh"}})
    manager = _Manager()
    events, published, _cleared, _refills = _activation_harness(
        monkeypatch, template, manager
    )

    asyncio.run(
        startup.start_platform_runtime(
            manager,
            pi.PiEngineAdapter(),
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
    spawn = next(event for event in events if event.startswith("pi-client:"))
    # `--session-dir` is always there, so the flag has to be matched with its
    # separator; a check for the bare prefix would pass no matter what.
    assert " --session " not in spawn
    assert " --session-dir " in spawn
    assert '"defaultThinkingLevel": "xhigh"' in spawn
    assert "settings.json" in spawn
    assert "--thinking" not in spawn
    assert "--model deepseek-chat" in spawn
    assert "--provider astrabox" in spawn
    # A fresh conversation: pi mints its own native id at spawn and the client
    # adopts it, so nothing here names one.
    assert published[0]["engine_session_key"] is None
    identity = published[0]["runtime_identity"]
    assert identity["session_id"] == "session-1"
    assert identity["sandbox_id"] == "box-77"


def test_the_resume_flag_the_claimed_spawn_must_never_carry_is_reachable() -> None:
    """The control for the assertion above.

    Without this, ``" --session " not in spawn`` would also hold for a builder
    that had stopped emitting the flag entirely, and the test would be pinning
    nothing.
    """

    resuming = pi.build_pi_rpc_command(
        linux_user="agent",
        home="/home/agent",
        workspace="/workspace",
        model="deepseek-chat",
        resume_session_key="pi-session-9",
    )
    assert " --session pi-session-9" in resuming
def test_the_shared_spawn_sources_its_engine_env_and_the_box_spawn_does_not() -> None:
    """The pipe child runs under box execd, whose environment is the pool
    template's; a shared conversation's credential and gateway live only in
    its engine-env file, so the shared spawn must source it — and a
    box-tenancy spawn must not name a file that tenancy never writes."""

    shared = pi.build_pi_rpc_command(
        linux_user="conv_abc",
        home="/home/conversations/conv_abc",
        workspace="/home/conversations/conv_abc/workspace",
        model="deepseek-chat",
        engine_env_file="/home/conversations/conv_abc/.astrabox-engine-env",
    )
    assert ". /home/conversations/conv_abc/.astrabox-engine-env" in shared

    box = pi.build_pi_rpc_command(
        linux_user="agent",
        home="/home/agent",
        workspace="/workspace",
        model="deepseek-chat",
    )
    assert ".astrabox-engine-env" not in box


def test_the_engine_env_file_follows_the_tenancy() -> None:
    assert (
        pi._shared_engine_env_file(
            {"sandbox_tenancy": "agent", "home_dir": "/home/conversations/c1"}
        )
        == "/home/conversations/c1/.astrabox-engine-env"
    )
    assert pi._shared_engine_env_file({"sandbox_tenancy": "conversation"}) is None
    with pytest.raises(ValueError):
        pi._shared_engine_env_file({"sandbox_tenancy": "agent"})


def test_native_settings_reach_the_launched_process_without_a_key_whitelist(
    tmp_path: Path,
) -> None:
    """Exercise shell quoting and replacement before the supplier starts."""

    native = {
        "defaultThinkingLevel": "high",
        "compaction": {"enabled": False, "keepRecentTokens": 12000},
        "futureVendorSetting": {"literal": "$(exit 91) 'quoted' 中文"},
    }
    executable = tmp_path / "pi"
    executable.write_text(
        '#!/bin/sh\ncat "$PI_CODING_AGENT_DIR/settings.json"\nprintf "%s" "$PI_SUBAGENTS_TEMP_ROOT" >&2\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    command = pi.build_pi_rpc_command(
        linux_user=getpass.getuser(),
        home=str(tmp_path),
        workspace=str(tmp_path),
        model="model-from-platform",
        settings=native,
        isolated=True,
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PI_SUBAGENTS_TEMP_ROOT": "/wrong-parent-root"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == native
    assert result.stderr == str(tmp_path / ".pi/subagents")
    reset = pi.build_pi_rpc_command(
        linux_user=getpass.getuser(),
        home=str(tmp_path),
        workspace=str(tmp_path),
        model="model-from-platform",
        isolated=True,
    )
    result = subprocess.run(
        ["bash", "-c", reset],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {}
    assert result.stderr == str(tmp_path / ".pi/subagents")
