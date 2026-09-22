"""Reusable conformance suite for :class:`EngineAdapter` implementations.

Third-engine authors bind this exactly like the collection contract suite::

    # your_plugin/tests/engine_conformance_test.py
    from astrabox.testing.engine_conformance import EngineAdapterContractSuite

    class TestMyEngineContract(EngineAdapterContractSuite):
        def make_adapter(self):
            return MyEngineAdapter()

The in-tree binding (``tests/engine_adapter_conformance_test.py``) runs the
same suite over both built-in adapters, so the contract these checks encode
is exercised by real implementations, not just documented.

What conformance means for an adapter (the REQUIRED surface):

* ``engine_kind`` — non-empty, stable identity string;
* ``capabilities`` — an explicit :class:`EngineRuntimeCapabilities` declaration
  naming the session products it can drive and its optional config directory;
* ``engine_client_type`` — the concrete class implementing the complete
  conversation-resume, FIFO, stream and stop surface;
* ``sandbox_request`` — declares box requirements without allocating one;
* ``activate_runtime`` — starts or reconnects only the engine in a
  platform-prepared box.
"""

from __future__ import annotations

from typing import Any

from astrabox.core.service.orchestrator.engine.base import (
    EngineAdapter,
    validate_engine_client_type,
)
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeCapabilities,
    validate_engine_options_schema,
)


class EngineAdapterContractSuite:
    """Subclass and implement :meth:`make_adapter` to run the contract."""

    def make_adapter(self) -> EngineAdapter:
        raise NotImplementedError("bind the suite: override make_adapter()")

    # ── identity ─────────────────────────────────────────────────────────

    def test_engine_kind_is_a_stable_nonempty_name(self) -> None:
        adapter = self.make_adapter()
        kind = adapter.engine_kind
        assert isinstance(kind, str) and kind.strip(), "engine_kind must be non-empty"
        assert kind == adapter.engine_kind, "engine_kind must be stable"

    # ── capabilities ─────────────────────────────────────────────────────

    def test_capabilities_shape_and_stamp(self) -> None:
        adapter = self.make_adapter()
        caps = adapter.capabilities
        assert isinstance(caps, EngineRuntimeCapabilities)
        assert caps.engine_kind == adapter.engine_kind, (
            "capabilities.engine_kind must match the adapter (diagnostics "
            "read it off the profile)"
        )
        assert isinstance(caps.supported_session_kinds, frozenset)
        assert caps.supported_session_kinds, (
            "an adapter must declare at least one session product it can drive"
        )
        # The engine declares one set of facts; the platform composes the
        # per-tenancy profiles from them. Conformance walks every composition
        # this adapter's session kinds can reach, so a workload that composes
        # into a malformed profile fails the adapter here rather than a
        # Session later.
        from astrabox.core.service.orchestrator.engine.runtime_profiles import (
            composed_runtime_profile,
        )
        from astrabox.seams.sandbox import SANDBOX_TENANCIES

        assert caps.workload.config_dir_name is None or (
            caps.workload.config_dir_name.startswith(".")
            and "/" not in caps.workload.config_dir_name
        ), "config_dir_name is None or a dot-directory basename"
        for session_kind in sorted(caps.supported_session_kinds):
            for tenancy in SANDBOX_TENANCIES:
                profile = composed_runtime_profile(
                    caps.engine_kind, tenancy, session_kind=session_kind
                )
                assert profile.sandbox_tenancy == tenancy
                assert profile.username_template and profile.home_template
        # Well-formed or absent: the declaration is the write path's whole
        # authority over an Agent's engine_options bag, so a malformed one
        # must fail the adapter here rather than every Agent save later.
        validate_engine_options_schema(caps.engine_kind, caps.engine_options_schema)

    # ── required surface ─────────────────────────────────────────────────

    def test_engine_client_type_is_complete(self) -> None:
        validate_engine_client_type(self.make_adapter().engine_client_type)

    def test_startup_capabilities_are_overridden(self) -> None:
        adapter = self.make_adapter()
        assert (
            type(adapter).sandbox_request is not EngineAdapter.sandbox_request
        ), "sandbox_request must declare the engine's box requirements"
        assert (
            type(adapter).activate_runtime is not EngineAdapter.activate_runtime
        ), "activate_runtime must activate only the engine protocol"

# ── EngineClient ─────────────────────────────────────────────────────────────

class EngineClientContractSuite:
    """Structural conformance for an engine's :class:`EngineClient`.

    Engines are how the platform absorbs agent-tool diversity: every admitted
    tool completes the same conversation-resume, FIFO, stream and stop journey
    through this small surface and speaks one output language (AI SDK frames,
    via the engine's translator). Optional controls are separate capability
    protocols; this suite deliberately does not make an engine invent them. A
    new engine binds this suite next to
    :class:`EngineAdapterContractSuite`::

        class TestMyEngineClient(EngineClientContractSuite):
            def client_type(self):
                return MyEngineClient

    Structural only — driving a real turn needs a live deployment and belongs
    to the engine's integration tests plus the live e2e. The in-tree binding
    runs this over BOTH built-in clients (a resident websocket link and
    per-request HTTP), so the contract is pinned by two shape-divergent
    implementations rather than documented.
    """

    def client_type(self) -> type[Any]:
        raise NotImplementedError("bind the suite: override client_type()")

    def test_every_surface_member_exists_with_the_right_shape(self) -> None:
        validate_engine_client_type(self.client_type())


def _typing_only(_: Any) -> None:  # pragma: no cover
    """Keep Any imported for subclass signatures without a runtime use."""
