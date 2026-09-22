"""Channel provider conformance and registration gating.

An external provider proves itself against
:class:`astrabox.testing.provider_conformance.ChannelProviderContractSuite`
without importing webhook or session-kernel implementation details;
``register_channel`` rejects partial capability shapes at import time.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrabox.providers.channel_generic import GenericJsonChannelProvider
from astrabox.providers.channel_satori import (
    CHANNEL_PROVIDER_DEFINITIONS,
    GatewayChannelProvider,
)
from astrabox.seams.channel import (
    ChannelDescriptor,
    ChannelDeliveryHandle,
    ChannelDeliveryReceipt,
    ChannelEvent,
    ChannelField,
    ChannelInbound,
    ChannelProvider,
    register_channel,
)
from astrabox.testing.provider_conformance import ChannelProviderContractSuite


class _StreamingReference(ChannelProvider):
    name = "conf_streaming_reference"
    supports_streaming_delivery = True

    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
        return ChannelInbound(content="x")

    async def open_delivery(
        self,
        *,
        reply_context: dict[str, Any],
        prior_aliases: list[str],
        binding: dict[str, Any],
    ) -> ChannelDeliveryHandle:
        _ = binding
        class _Handle(ChannelDeliveryHandle):
            async def emit(self, event: ChannelEvent) -> ChannelDeliveryReceipt | None:
                return None

        return _Handle()


class _SourcingReference(ChannelProvider):
    name = "conf_sourcing_reference"
    supports_source = True

    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
        return ChannelInbound(content="x")

    def open_source(self, *, binding) -> Any:
        _ = binding
        async def _gen():
            if False:  # pragma: no cover - reference shape only
                yield None

        return _gen()


class TestGenericJsonConformance(ChannelProviderContractSuite):
    def make_provider(self) -> Any:
        return GenericJsonChannelProvider()


class TestGatewayChannelConformance(ChannelProviderContractSuite):
    def make_provider(self) -> Any:
        definition = next(
            value
            for value in CHANNEL_PROVIDER_DEFINITIONS
            if value.get("name") == "telegram"
        )
        return GatewayChannelProvider(definition)


class TestStreamingReferenceConformance(ChannelProviderContractSuite):
    def make_provider(self) -> Any:
        return _StreamingReference()


class TestSourcingReferenceConformance(ChannelProviderContractSuite):
    def make_provider(self) -> Any:
        return _SourcingReference()


# ── registration gating (fail-loud on partial shapes) ────────────────────────


def test_registration_rejects_a_flag_without_its_method() -> None:
    class _FlagOnly(ChannelProvider):
        name = "conf_flag_only"
        supports_streaming_delivery = True

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match="declared and implemented together"):
        register_channel(_FlagOnly())


def test_registration_rejects_a_method_without_its_flag() -> None:
    class _MethodOnly(ChannelProvider):
        name = "conf_method_only"

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

        async def open_delivery(self, *, reply_context, prior_aliases, binding):
            raise AssertionError("never called")

    with pytest.raises(RuntimeError, match="declared and implemented together"):
        register_channel(_MethodOnly())


def test_registration_rejects_a_source_flag_without_open_source() -> None:
    class _SourceFlagOnly(ChannelProvider):
        name = "conf_source_flag_only"
        supports_source = True

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match="declared and implemented together"):
        register_channel(_SourceFlagOnly())


def test_registration_rejects_a_source_without_binding_scope() -> None:
    class _UnscopedSource(ChannelProvider):
        name = "conf_unscoped_source"
        supports_source = True

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

        def open_source(self) -> Any:
            raise AssertionError("never called")

    with pytest.raises(RuntimeError, match="must accept a binding"):
        register_channel(_UnscopedSource())


def test_registration_rejects_a_non_bool_flag() -> None:
    class _BadFlag(ChannelProvider):
        name = "conf_bad_flag"
        supports_source = "yes"  # type: ignore[assignment]

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match="must be a bool"):
        register_channel(_BadFlag())


def test_registration_rejects_a_sync_open_delivery() -> None:
    class _SyncDelivery(ChannelProvider):
        name = "conf_sync_delivery"
        supports_streaming_delivery = True

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

        def open_delivery(  # type: ignore[override]
            self, *, reply_context, prior_aliases, binding
        ):
            raise AssertionError("never called")

    with pytest.raises(RuntimeError, match="open_delivery must be async"):
        register_channel(_SyncDelivery())


def test_registration_rejects_delivery_without_binding_scope() -> None:
    class _UnscopedDelivery(ChannelProvider):
        name = "conf_unscoped_delivery"

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

        async def deliver_outbound(self, *, reply_context, text):
            raise AssertionError("never called")

    with pytest.raises(RuntimeError, match="deliver_outbound must accept a binding"):
        register_channel(_UnscopedDelivery())


def test_registration_rejects_secret_in_deployment_config() -> None:
    class _PersistedSecret(ChannelProvider):
        name = "conf_persisted_secret"

        def describe(self) -> ChannelDescriptor:
            return ChannelDescriptor(
                name=self.name,
                label="bad",
                config_fields=(ChannelField(key="token", label="Token", secret=True),),
            )

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match="config fields cannot be secret"):
        register_channel(_PersistedSecret())


def test_registration_rejects_unsafe_setup_link() -> None:
    class _UnsafeLink(ChannelProvider):
        name = "conf_unsafe_link"

        def describe(self) -> ChannelDescriptor:
            return ChannelDescriptor(
                name=self.name,
                label="bad",
                setup_url="javascript:alert(1)",
            )

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    with pytest.raises(RuntimeError, match=r"absolute HTTP\(S\) URL"):
        register_channel(_UnsafeLink())


# ── entry-point loading: class targets register like the other groups ──────


def test_channel_class_entry_point_is_instantiated_and_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astrabox.providers as providers_module
    from astrabox.seams.channel import channel_if_registered

    class _EpChannel(ChannelProvider):
        name = "conf_ep_class_channel"

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    class _FakeEp:
        name = "conf_ep_class_channel"
        value = "pkg:Provider"

        def load(self) -> Any:
            return _EpChannel

    def _fake_select(group: str) -> dict[str, Any]:
        if group == providers_module.CHANNEL_GROUP:
            return {"conf_ep_class_channel": _FakeEp()}
        return {}

    monkeypatch.setattr(providers_module, "_select_entry_points", _fake_select)
    providers_module.load_entry_point_providers()
    registered = channel_if_registered("conf_ep_class_channel")
    assert isinstance(registered, _EpChannel), (
        "a class entry-point target must be instantiated and registered, "
        "matching the sandbox/storage groups"
    )


def test_channel_class_entry_point_with_partial_shape_fails_at_load_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_channel's capability validation now runs at LOAD time for
    class targets — a broken distribution fails on install, not on the first
    callback."""
    import astrabox.providers as providers_module

    class _PartialChannel(ChannelProvider):
        name = "conf_ep_partial_channel"
        supports_streaming_delivery = True  # flag without open_delivery

        def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:
            return ChannelInbound(content="x")

    class _FakeEp:
        name = "conf_ep_partial_channel"
        value = "pkg:Provider"

        def load(self) -> Any:
            return _PartialChannel

    def _fake_select(group: str) -> dict[str, Any]:
        if group == providers_module.CHANNEL_GROUP:
            return {"conf_ep_partial_channel": _FakeEp()}
        return {}

    monkeypatch.setattr(providers_module, "_select_entry_points", _fake_select)
    with pytest.raises(RuntimeError, match="declared and implemented together"):
        providers_module.load_entry_point_providers()


# ── the probe survives provider modules that define dataclasses ─────────────


def test_import_isolation_probe_handles_dataclass_provider_modules(
    tmp_path: Any,
) -> None:
    """A module-level @dataclass with string annotations resolves through
    sys.modules[cls.__module__] during exec — the probe must register the
    module first. Config dataclasses are the NORMAL shape for real
    provider distributions."""
    module_path = tmp_path / "dataclass_provider_module.py"
    module_path.write_text(
        "from __future__ import annotations\n"
        "\n"
        "from dataclasses import dataclass\n"
        "\n"
        "from astrabox.seams.channel import ChannelInbound, ChannelProvider\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class _ProviderConfig:\n"
        "    endpoint: str = 'https://example.invalid'\n"
        "    timeout_seconds: float = 15.0\n"
        "\n"
        "\n"
        "class DataclassConfigProvider(ChannelProvider):\n"
        "    name = 'conf_dataclass_provider'\n"
        "\n"
        "    def __init__(self) -> None:\n"
        "        self.config = _ProviderConfig()\n"
        "\n"
        "    def verify_and_resolve(self, *, headers, raw_body, binding) -> ChannelInbound:\n"
        "        return ChannelInbound(content='x')\n"
    )
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "conf_dataclass_provider_module", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)

        class _Bound(ChannelProviderContractSuite):
            def make_provider(self) -> Any:
                return module.DataclassConfigProvider()

        _Bound().test_provider_module_imports_no_spine_internals()
    finally:
        sys.modules.pop(spec.name, None)
