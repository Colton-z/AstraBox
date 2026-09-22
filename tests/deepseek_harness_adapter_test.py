"""What the deepseek_harness adapter puts in the box before it talks to it.

An AstraBox Agent's instructions reach this engine through ``AGENTS.md`` in
the conversation's workspace — the harness's own project-instructions channel,
verified against a live server (an agent told to identify itself a certain way
did). The file is only useful if it is there before the harness composes the
session, so the ordering is asserted, not just the content: written after the
box exists and before the link opens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine import deepseek_harness
from astrabox.core.service.orchestrator.engine.base import (
    EngineStartupContext,
)
from astrabox.seams.model import ResolvedModelAccess

_CWD = "/home/agent/workspace/sessions/s1"


class _Recorder:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.writes: list[tuple[str, str, int]] = []
        self.request: Any = None


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()

    async def fake_install(
        target: Any,
        *,
        path: str,
        content: str,
        mode: int,
        error_code: str,
        error_message: str,
    ) -> None:
        _ = (target, error_code, error_message)
        recorder.order.append("write-instructions")
        recorder.writes.append((path, content, mode))

    async def fake_connect(target: Any, **kwargs: Any) -> Any:
        _ = (target, kwargs)
        recorder.order.append("connect")
        return SimpleNamespace(close=_close)

    async def fake_publish(**kwargs: Any) -> Any:
        recorder.order.append("publish")
        return SimpleNamespace(**kwargs)

    recorder.publish_kwargs = {}  # type: ignore[attr-defined]

    async def _close() -> None:
        recorder.order.append("close")

    monkeypatch.setattr(deepseek_harness, "install_verified_text_script", fake_install)
    monkeypatch.setattr(
        deepseek_harness.DshApiLink, "connect", staticmethod(fake_connect)
    )
    monkeypatch.setattr(deepseek_harness, "_publish_runtime", fake_publish)
    return recorder


def _manager() -> Any:
    return SimpleNamespace(
        resolve_model_access=lambda raw: ResolvedModelAccess(
            configuration=dict(raw),
            base_url="https://gateway.test/v1",
            model_name="deepseek-model",
            credential="sk-test",
            credential_kind="bearer",
            endpoint_provider="litellm",
        ),
    )


async def _start(system: str | None) -> None:
    model_access = _manager().resolve_model_access({})
    await deepseek_harness.DeepSeekHarnessEngineAdapter().activate_runtime(
        EngineStartupContext(
        session_id="s1",
        template=SimpleNamespace(model_config={}, system=system),
        workspace_plan=SimpleNamespace(resume_engine_session_key=None),
        sandbox=SimpleNamespace(id="sbx-1", sandbox_id="sbx-1"),
        sandbox_id="sbx-1",
        cwd=_CWD,
        runtime_identity={"username": "agent"},
        model_access=model_access,
        model_credential="placeholder",
        resume_session_key=None,
        )
    )


@pytest.mark.asyncio
async def test_agent_instructions_land_where_the_harness_reads_them(
    harness: _Recorder,
) -> None:
    await _start("You are the AstraBox reviewer agent.")

    assert harness.writes, "an Agent with instructions must place AGENTS.md"
    path, content, mode = harness.writes[0]
    assert path == f"{_CWD}/AGENTS.md"
    assert content == "You are the AstraBox reviewer agent.\n"
    assert mode == 0o644


@pytest.mark.asyncio
async def test_the_instructions_are_in_place_before_the_session_can_exist(
    harness: _Recorder,
) -> None:
    """The plugin reads them when the harness composes the session.

    The session is created through the link, so a write that happened after
    the link opened could land after the composition that was supposed to read
    it — and the Agent would answer with the vendor's default persona while
    the file sat on disk looking correct.
    """

    await _start("You are the AstraBox reviewer agent.")

    assert harness.order == ["write-instructions", "connect", "publish"]


@pytest.mark.asyncio
async def test_an_agent_without_instructions_writes_no_file(
    harness: _Recorder,
) -> None:
    await _start("   ")

    assert harness.writes == []
    assert harness.order == ["connect", "publish"]


@pytest.mark.asyncio
async def test_the_box_is_not_ready_until_its_gateway_answers(
    harness: _Recorder,
) -> None:
    """READY must mean "can hold a conversation", not "the container exists".

    The API server starts with the box, so the create blocks on its port. A
    box that reached READY with a dead gateway fails on the first turn, which
    is the failure shape the provisioning contract exists to prevent.
    """

    adapter = deepseek_harness.DeepSeekHarnessEngineAdapter()
    template = SimpleNamespace(model_config={}, system=None)
    request = adapter.sandbox_request(
        template=template,
        model_access=_manager().resolve_model_access(template.model_config),
    )

    assert request.wait_for_inbox_service_port == deepseek_harness.DSH_API_PORT
    assert request.entrypoint == ("/opt/gem/run.sh",)
    assert request.credential_env_var == "DEEPSEEK_API_KEY"
    # The image hands this directory to the account its server runs as.
    # AstraBox chooses the directory, so the adapter declares the variable
    # name and AstraBox fills in the value.
    assert request.cwd_env_var == "ASTRABOX_WORKSPACE"
    assert request.env == {"DEEPSEEK_BASE_URL": "https://gateway.test/v1"}
    assert request.credential.request_paths == ("chat/completions",)


# ── the permission mode has to be verified, not just recorded ────────────
@pytest.mark.asyncio
async def test_a_published_runtime_states_its_mode_was_actually_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform reconciles the mode before every dispatch.

    It skips that round trip only when the runtime says the mode was
    verified. A runtime that carries a mode without the flag reaches READY
    and then fails its first turn with "permission_mode reconciliation was
    not accepted before dispatch" — which is exactly what a real restart
    produced. So the flag and the call that earns it are asserted together.
    """

    applied: list[str] = []

    class _Client:
        engine_session_key = "session-x"

        async def set_permission_mode(self, mode: str) -> None:
            applied.append(mode)

    async def fake_initialize(client: Any, **kwargs: Any) -> Any:
        _ = (client, kwargs)
        return SimpleNamespace(engine_kind="deepseek_harness")

    monkeypatch.setattr(
        deepseek_harness, "DeepSeekHarnessEngineClient", lambda **kw: _Client()
    )
    monkeypatch.setattr(deepseek_harness, "initialize_engine_client", fake_initialize)
    monkeypatch.setattr(deepseek_harness, "extract_sandbox_id", lambda s: "sbx-1")

    runtime = await deepseek_harness._publish_runtime(
        session_id="s1",
        sandbox=SimpleNamespace(),
        link=SimpleNamespace(),
        engine_session_key="session-x",
        terminal_cwd=_CWD,
        permission_mode="danger-full-access",
    )

    assert applied == ["danger-full-access"], "the preset must be applied, not assumed"
    assert runtime.permission_mode == "danger-full-access"
    assert getattr(runtime, "permission_mode_verified", False) is True


@pytest.mark.asyncio
async def test_a_published_runtime_is_moved_to_the_chosen_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine session has no model of its own until one is selected.

    The vendor's `session/create` takes workspace, cwd, session identity and
    the Agent preset — no model — so the conversation runs the deployment's
    own default until this call moves it. Asserted on every publish for the
    same reason the preset is: a model changed on the Agent has to reach the
    conversations that already exist.
    """

    selected: list[str] = []

    class _Client:
        engine_session_key = "session-x"

        async def set_permission_mode(self, mode: str) -> None:
            _ = mode

        async def select_model(self, model: str) -> None:
            selected.append(model)

    async def fake_initialize(client: Any, **kwargs: Any) -> Any:
        _ = (client, kwargs)
        return SimpleNamespace(engine_kind="deepseek_harness")

    monkeypatch.setattr(
        deepseek_harness, "DeepSeekHarnessEngineClient", lambda **kw: _Client()
    )
    monkeypatch.setattr(deepseek_harness, "initialize_engine_client", fake_initialize)
    monkeypatch.setattr(deepseek_harness, "extract_sandbox_id", lambda s: "sbx-1")

    await deepseek_harness._publish_runtime(
        session_id="s1",
        sandbox=SimpleNamespace(),
        link=SimpleNamespace(),
        engine_session_key="session-x",
        terminal_cwd=_CWD,
        permission_mode=None,
        model="gpt-5.6-luna",
    )

    assert selected == ["gpt-5.6-luna"], "the model must be applied, not assumed"


@pytest.mark.asyncio
async def test_a_runtime_without_a_model_selects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Environment that names no model leaves the harness's default alone."""

    class _Client:
        engine_session_key = "session-x"

        async def select_model(self, model: str) -> None:
            raise AssertionError(f"nothing was selected; got {model!r}")

    async def fake_initialize(client: Any, **kwargs: Any) -> Any:
        _ = (client, kwargs)
        return SimpleNamespace(engine_kind="deepseek_harness")

    monkeypatch.setattr(
        deepseek_harness, "DeepSeekHarnessEngineClient", lambda **kw: _Client()
    )
    monkeypatch.setattr(deepseek_harness, "initialize_engine_client", fake_initialize)
    monkeypatch.setattr(deepseek_harness, "extract_sandbox_id", lambda s: "sbx-1")

    runtime = await deepseek_harness._publish_runtime(
        session_id="s1",
        sandbox=SimpleNamespace(),
        link=SimpleNamespace(),
        engine_session_key="session-x",
        terminal_cwd=_CWD,
        permission_mode=None,
        model=None,
    )

    assert runtime.engine_session_key == "session-x"


@pytest.mark.asyncio
async def test_the_model_comes_from_the_environment_not_the_engine_options(
    harness: _Recorder,
) -> None:
    """The Agent's model lives in the Environment's resolved access.

    Nothing in `engine_options.session_create` can carry it — the vendor's
    creation request declares no model field — so a start that did not read
    `model_access.model_name` left every conversation on the image's default
    and the gateway refused a model the user never chose.
    """

    published = await deepseek_harness.DeepSeekHarnessEngineAdapter().activate_runtime(
        EngineStartupContext(
            session_id="s1",
            template=SimpleNamespace(
                model_config={},
                system=None,
                engine_options={"session_create": {"agentPreset": "code"}},
            ),
            workspace_plan=SimpleNamespace(resume_engine_session_key=None),
            sandbox=SimpleNamespace(id="sbx-1", sandbox_id="sbx-1"),
            sandbox_id="sbx-1",
            cwd=_CWD,
            runtime_identity={"username": "agent"},
            model_access=_manager().resolve_model_access({}),
            model_credential="placeholder",
            resume_session_key=None,
        )
    )

    assert published.model == "deepseek-model"


@pytest.mark.asyncio
async def test_a_runtime_without_a_mode_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        engine_session_key = "session-x"

        async def set_permission_mode(self, mode: str) -> None:
            raise AssertionError(f"nothing was requested; got {mode!r}")

    async def fake_initialize(client: Any, **kwargs: Any) -> Any:
        _ = (client, kwargs)
        return SimpleNamespace(engine_kind="deepseek_harness")

    monkeypatch.setattr(
        deepseek_harness, "DeepSeekHarnessEngineClient", lambda **kw: _Client()
    )
    monkeypatch.setattr(deepseek_harness, "initialize_engine_client", fake_initialize)
    monkeypatch.setattr(deepseek_harness, "extract_sandbox_id", lambda s: "sbx-1")

    runtime = await deepseek_harness._publish_runtime(
        session_id="s1",
        sandbox=SimpleNamespace(),
        link=SimpleNamespace(),
        engine_session_key="session-x",
        terminal_cwd=_CWD,
        permission_mode=None,
    )

    assert getattr(runtime, "permission_mode_verified", False) is False


# ── the agent preset roster ──────────────────────────────────────────────
def test_the_agent_form_declares_the_native_creation_node() -> None:
    """A preset AstraBox does not offer is one the operator cannot reach."""

    adapter = deepseek_harness.DeepSeekHarnessEngineAdapter()
    fields = adapter.capabilities.engine_options_schema
    preset_field = next(f for f in fields if f["key"] == "session_create")

    assert preset_field["type"] == "object"
    assert "item_schema" not in preset_field
    assert set(preset_field["protected_keys"]) == {"cwd", "workspaceId", "sessionId"}


def test_the_image_locks_the_deepseek_harness_root_and_resolved_artifacts() -> None:
    """Lock the supplier's graph without forcing subpackages to its root version."""

    image_dir = (
        Path(__file__).resolve().parents[1]
        / "containers"
        / "sandbox-deepseek-harness"
    )
    manifest = json.loads((image_dir / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((image_dir / "package-lock.json").read_text(encoding="utf-8"))
    expected = manifest["dependencies"]["@deepseek-ai/dsh"]

    assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", expected), (
        "the image must pin an exact vendor release, not a semver range"
    )
    assert lock["packages"][""]["dependencies"] == manifest["dependencies"]
    assert lock["packages"]["node_modules/@deepseek-ai/dsh"]["version"] == expected

    release_family = {
        path: package
        for path, package in lock["packages"].items()
        if "node_modules/" in path
        and path.rsplit("node_modules/", 1)[-1].startswith("@deepseek-ai/dsh")
    }
    assert len(release_family) > 1, "the lockfile contains no DSH dependency tree"
    for path, package in release_family.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", package["version"]), path
        assert package["resolved"].startswith("https://registry.npmjs.org/"), path
        assert package["integrity"].startswith("sha512-"), path


def test_the_image_build_uses_the_committed_dependency_graph() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1]
        / "containers"
        / "sandbox-deepseek-harness"
        / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "package-lock.json" in dockerfile
    assert 'npm ci --prefix "${DSH_PREFIX}"' in dockerfile
    assert "npm install" not in dockerfile
    assert "NODE_OPTIONS=--max-old-space-size=8192" in dockerfile


# ── a failed adapter start releases only its own transport ───────────────
@pytest.mark.asyncio
async def test_a_failed_start_closes_its_link_and_rethrows_the_engine_failure(
    harness: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox rollback belongs to the runtime manager, not this adapter."""

    async def boom(**kwargs: Any) -> Any:
        _ = kwargs
        raise RuntimeError("session.create returned HTTP 502")

    monkeypatch.setattr(deepseek_harness, "_publish_runtime", boom)
    with pytest.raises(RuntimeError, match="session.create returned HTTP 502"):
        await _start(None)

    assert harness.order[-2:] == ["connect", "close"]


@pytest.mark.asyncio
async def test_a_link_that_never_opened_propagates_without_adapter_cleanup(
    harness: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform still owns its recorded allocation when connect fails."""

    async def refuse(target: Any, **kwargs: Any) -> Any:
        _ = (target, kwargs)
        raise RuntimeError("the gateway is not listening")

    monkeypatch.setattr(
        deepseek_harness.DshApiLink, "connect", staticmethod(refuse)
    )
    with pytest.raises(RuntimeError, match="gateway is not listening"):
        await _start(None)

    assert "close" not in harness.order
