"""EnginePlatform contract conformance.

Three invariants pinned here:

1. ``RemoteAgentRuntimeManager`` provides every member of the
   :class:`~astrabox.core.service.orchestrator.engine.platform.EnginePlatform`
   Protocol — the public engine-facing surface. If a platform method is
   renamed/removed without updating the Protocol (or vice versa), this fails.

2. The engine package never reaches for manager underscore-privates again —
   a structural guard over the source tree, so the seam cannot silently rot
   back to private-poking (the pre-refactor state).

3. The platform startup coordinator reaches the provider's atomic create seam
   before an engine is activated. Adapters have no ``start_runtime`` workflow
   of their own.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.model.astrabox_models import AgentView
from astrabox.core.service.orchestrator.engine.platform import EnginePlatform
from astrabox.core.service.orchestrator.engine.startup import start_platform_runtime
from astrabox.core.service.orchestrator.engine.registry import (
    get_engine_adapter,
    known_engine_kinds,
)
from astrabox.core.service.orchestrator.session_workspace_plan import (
    RuntimeWorkspacePlan,
)
from astrabox.providers import register_builtin_providers
from astrabox.seams.model import ResolvedModelAccess
from astrabox.seams.sandbox import (
    registered_sandbox_names,
    sandbox_for_name,
    sandbox_for_template,
)

# Ensure the built-in engine (and sandbox) providers are registered before
# ``known_engine_kinds()`` is read below at collection time — importing
# ``runtime_manager`` normally does this as a side effect, but this module
# must not depend on collection order across the test suite for that.
register_builtin_providers()

_ENGINE_PKG = (
    pathlib.Path(__file__).resolve().parents[1]
    / "astrabox"
    / "core"
    / "service"
    / "orchestrator"
    / "engine"
)


def _protocol_members() -> set[str]:
    attrs = getattr(EnginePlatform, "__protocol_attrs__", None)
    if attrs:
        return {name for name in attrs if not name.startswith("_")}
    return {name for name in dir(EnginePlatform) if not name.startswith("_")}


def test_protocol_has_the_expected_surface() -> None:
    """The contract itself: additions/removals must be deliberate."""
    assert _protocol_members() == {
        # tier 1 — engine-independent primitives
        "deployment_settings",
        "resolve_model_access",
        "resolve_sandbox_backend_secret",
        "resolve_session_egress_credentials",
        "resolve_session_mcp_credentials",
        "record_startup_allocation",
        "record_attached_runtime_identity",
        "connect_sandbox_only",
        "clone_default_repo",
        "mount_assistant_workspace_storage",
        "prepare_workspace_storage",
        "resolve_runtime_sandbox_backend",
    }


def test_runtime_manager_conforms_to_engine_platform() -> None:
    from astrabox.core.service.orchestrator.runtime_manager import (
        RemoteAgentRuntimeManager,
    )

    missing = sorted(
        member
        for member in _protocol_members()
        if not hasattr(RemoteAgentRuntimeManager, member)
    )
    assert not missing, (
        "RemoteAgentRuntimeManager is missing EnginePlatform members: "
        f"{missing} — implement them in the public EnginePlatform section"
    )


def test_engine_package_never_touches_manager_privates() -> None:
    pattern = re.compile(r"\b(?:manager|platform)\._[A-Za-z]")
    offenders: list[str] = []
    for py in sorted(_ENGINE_PKG.glob("*.py")):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "engine package must use the EnginePlatform contract, never manager "
        "privates:\n" + "\n".join(offenders)
    )


def test_engine_adapters_never_publish_process_local_runtimes() -> None:
    offenders: list[str] = []
    for py in sorted(_ENGINE_PKG.glob("*.py")):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if ".register_runtime(" in line:
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "the runtime manager publishes adapter attach results; adapters must not "
        "mutate its process-local live map:\n" + "\n".join(offenders)
    )


def test_engine_adapters_do_not_redeclare_platform_emission_envelopes() -> None:
    offenders: list[str] = []
    pattern = re.compile(r"^def _[a-z0-9_]+_emission_category\(")
    for py in sorted(_ENGINE_PKG.glob("*.py")):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "platform envelope classification belongs to emissions.py; adapters only "
        "translate native events into those envelopes:\n" + "\n".join(offenders)
    )


class _ProtocolViolation(AttributeError):
    """Raised by :class:`_SpecPlatform` when engine code reaches for a name
    that is not part of the EnginePlatform contract."""


class _BoundaryReached(Exception):
    """Raised by the mocked sandbox-provisioning seam.

    Proves control reached the provisioning boundary without ``_SpecPlatform``
    ever seeing an out-of-contract attribute access along the way — if it had,
    a :class:`_ProtocolViolation` would have surfaced first instead.
    """


class _SpecPlatform:
    """Minimal ``EnginePlatform`` double.

    Implements every Protocol member with a canned response and raises
    :class:`_ProtocolViolation` for anything else — the runtime-enforcement
    twin of ``test_engine_package_never_touches_manager_privates`` above
    (that test is static/grep-based over the engine package's source; this
    one drives real adapter code and catches any indirect/dynamic reach for a
    non-Protocol name too).
    """

    def __getattr__(self, name: str) -> Any:
        raise _ProtocolViolation(
            f"engine code reached for {name!r}, which is not on EnginePlatform"
        )

    # -- tier 1: engine-independent primitives -----------------------------

    @property
    def deployment_settings(self) -> Any:
        # A real (defaults-only) settings object: the claude_code flow builds
        # its own ``RuntimeConfigResolver(platform.deployment_settings)``
        # locally and reads real settings attributes off it (e.g.
        # ``model_base_url``) well before reaching this test's boundary — a
        # bare ``object()`` can't satisfy those reads. Hermes never touches
        # this property before its own (earlier) boundary, so this is safe
        # for both adapters.
        return load_astrabox_settings()

    def resolve_model_access(self, mc: dict[str, Any]) -> ResolvedModelAccess:
        return ResolvedModelAccess(
            configuration=dict(mc or {}),
            base_url="https://model.example.test",
            model_name="spec-model",
            credential="spec-model-api-key",
            credential_kind="bearer",
            endpoint_provider="litellm",
        )

    def resolve_sandbox_backend_secret(
        self,
        template: Any,
        *,
        backend: str | None = None,
    ) -> str:
        return "spec-sandbox-secret"

    async def resolve_session_egress_credentials(
        self,
        session_id: str,
        *,
        placeholder_context: str | None = None,
    ) -> list[Any]:
        _ = placeholder_context
        return []

    async def resolve_session_mcp_credentials(
        self, session_id: str, server_urls: list[str]
    ) -> Any:
        from astrabox.seams.egress_credentials import MCPOutboundCredentialResolution

        return MCPOutboundCredentialResolution(scope_id="spec-scope")

    async def record_startup_allocation(
        self,
        session_id: str,
        allocation: Any,
        *,
        replaces: Any = None,
    ) -> None:
        _ = (session_id, allocation, replaces)

    async def record_attached_runtime_identity(
        self,
        session_id: str,
        *,
        sandbox_id: str,
        runtime_identity: dict[str, Any],
    ) -> None:
        _ = (session_id, sandbox_id, runtime_identity)

    async def connect_sandbox_only(self, sandbox_id: str) -> Any:
        raise _BoundaryReached(f"connect_sandbox_only reached: {sandbox_id}")

    async def clone_default_repo(
        self,
        sandbox: Any,
        template: Any,
        target_cwd: str,
        session_id: str,
        *,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def mount_assistant_workspace_storage(
        self,
        sandbox: Any,
        *,
        user_id: str,
        assistant_id: str,
        engine_kind: str,
    ) -> None:
        _ = (sandbox, user_id, assistant_id, engine_kind)

    async def prepare_workspace_storage(
        self,
        sandbox: Any,
        *,
        workspace_ref: Any,
        box_path: str,
        owner: str | None = None,
        group: str | None = None,
    ) -> None:
        _ = (sandbox, workspace_ref, box_path, owner, group)

    async def resolve_runtime_sandbox_backend(
        self,
        session_id: str,
        *,
        workspace_plan: Any,
    ) -> str:
        return sandbox_for_template(AgentView(name="spec-backend")).name


def test_spec_platform_implements_every_protocol_member() -> None:
    """Guard the fake's own fidelity: it must implement every pinned member,
    or the boundary test below would be silently weaker than it looks."""
    spec = _SpecPlatform()
    missing = sorted(member for member in _protocol_members() if not hasattr(spec, member))
    assert not missing, f"_SpecPlatform is missing EnginePlatform members: {missing}"


def _spec_workspace_plan(engine_kind: str, session_kind: str) -> RuntimeWorkspacePlan:
    if session_kind == "agent_chat":
        return RuntimeWorkspacePlan(
            subject_kind="deployment_conversation",
            session_kind="agent_chat",
            operation="runtime_start",
            runtime_key="spec-session",
            conversation_session_id="spec-session",
            cwd="/home/agent/workspace",
            resume_engine_session_key=None,
            sandbox_id=None,
            materialize_default_repo=False,
            default_repo_target_cwd=None,
            engine_kind=engine_kind,
            user_id="spec-user",
            assistant_id=None,
            agent_id="spec-agent",
        )
    return RuntimeWorkspacePlan(
        subject_kind="assistant_runtime",
        session_kind="assistant_chat",
        operation="runtime_start",
        runtime_key="spec-runtime-key",
        conversation_session_id=None,
        cwd="/home/conversations/spec-user/spec-asst",
        resume_engine_session_key=None,
        sandbox_id=None,
        materialize_default_repo=False,
        default_repo_target_cwd=None,
        engine_kind=engine_kind,
        user_id="spec-user",
        assistant_id="spec-asst",
        agent_id=None,
    )


@pytest.mark.parametrize("engine_kind", sorted(known_engine_kinds()))
async def test_platform_startup_reaches_only_the_provider_create_capability(
    engine_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the shared platform startup line to the provider create boundary.

    Every engine contributes only its declaration. If an adapter regains a
    create workflow, this test has no adapter entry point through which to call
    it and the structural guard below reports the forbidden method.
    """
    # Any real topology can address the model gateway (in-container it is
    # derived from the bridge address, dev.sh exports it); a defaults-on-bare-host
    # settings object cannot, and the litellm-default endpoint resolution fails
    # loud there by design. Stand in for a real topology, not for the failure.
    monkeypatch.setenv("ASTRABOX_LITELLM_BASE_URL", "http://gateway.conformance.test:4000")
    # Same reasoning for the address a box sends its transcript back to: every
    # real deployment has one, and an adapter that refuses to create a box
    # which could not mirror its session is behaving, not failing.
    monkeypatch.setenv("ASTRABOX_MCP_PROXY_BASE_URL", "http://backend.conformance.test:8000")

    adapter = get_engine_adapter(engine_kind)
    spec = _SpecPlatform()
    template = AgentView(
        name="conformance-spec-template",
        engine_kind=engine_kind,
    )
    session_kind = sorted(adapter.capabilities.supported_session_kinds)[0]
    call_kwargs: dict[str, Any] = dict(
        session_id="spec-session",
        assignment_id="assignment-spec-session",
        template=template,
        workspace_plan=_spec_workspace_plan(engine_kind, session_kind),
        user_id="spec-user",
        permission_mode=None,
        progress_callback=None,
        callback_url=None,
    )

    def _raise_create_sandbox(*_args: Any, **_kwargs: Any) -> Any:
        raise _BoundaryReached("create_sandbox reached")

    looked_up: list[str] = []

    async def _find_unallocated_assignment(assignment_id: str) -> None:
        looked_up.append(assignment_id)

    # Provider creation is the generic boundary for every engine. Patching
    # every registered provider keeps this test open to entry-point engines;
    # adding an adapter must not require another engine_kind branch here.
    for backend_name in registered_sandbox_names():
        provider = sandbox_for_name(backend_name)
        monkeypatch.setattr(provider, "find_sandbox_by_assignment", _find_unallocated_assignment)
        monkeypatch.setattr(provider, "create_sandbox", _raise_create_sandbox)

    with pytest.raises(_BoundaryReached):
        await start_platform_runtime(spec, adapter, **call_kwargs)
    assert looked_up == ["assignment-spec-session"]


def test_engine_adapters_expose_no_platform_workflow_entrypoint() -> None:
    for engine_kind in known_engine_kinds():
        adapter = get_engine_adapter(engine_kind)
        assert not hasattr(adapter, "start_runtime")
        assert not hasattr(adapter, "turn_transport")
