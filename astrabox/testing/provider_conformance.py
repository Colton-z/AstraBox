"""Reusable conformance suites for the cloud provider seams.

A downstream writing a cloud SandboxProvider / StorageProvider /
ChannelProvider binds these exactly like the collection- and
engine-adapter contract suites::

    # your_plugin/tests/conformance_test.py
    from astrabox.testing.provider_conformance import SandboxProviderContractSuite

    class TestMyCloudSandbox(SandboxProviderContractSuite):
        def make_provider(self):
            return MyCloudSandboxProvider()

The in-tree bindings (``tests/provider_conformance_test.py``) run these over
the built-in OpenSandbox providers, so the contract is exercised by a real
implementation, not only documented.

SCOPE — these pin the STRUCTURAL + capability contract that holds WITHOUT live
cloud infrastructure: the required abstract methods are actually overridden
(not left as the seam's fail-loud default), the name is a stable non-empty
key, the capability flags are the declared types, and registration
round-trips through the seam registry. The live LIFECYCLE (create/connect/
kill/mount against a real backend) needs infra and is the province of the
backend's own integration tests + the live e2e; a downstream should still
write those, but they are out of scope for a unit-lane conformance bind.
"""

from __future__ import annotations

import inspect
from typing import Any


def _is_overridden(instance: Any, base: type, method_name: str) -> bool:
    """True if ``method_name`` is overridden below ``base`` on ``instance``."""
    own = getattr(type(instance), method_name, None)
    inherited = getattr(base, method_name, None)
    return own is not None and own is not inherited


# ── SandboxProvider ──────────────────────────────────────────────────────────


class SandboxProviderContractSuite:
    """Subclass and implement :meth:`make_provider`."""

    def make_provider(self) -> Any:
        raise NotImplementedError("bind the suite: override make_provider()")

    def test_name_is_stable_and_nonempty(self) -> None:
        provider = self.make_provider()
        name = provider.name
        assert isinstance(name, str) and name.strip(), "provider.name must be non-empty"
        assert name == provider.name, "provider.name must be stable"

    def test_required_lifecycle_methods_are_overridden(self) -> None:
        from astrabox.seams.sandbox import SandboxProvider

        provider = self.make_provider()
        for method in ("connect", "kill", "connection_config", "secret_material", "build_dataplane"):
            assert _is_overridden(provider, SandboxProvider, method), (
                f"SandboxProvider.{method} must be implemented (it is abstract "
                "on the seam)"
            )

    def test_capability_flags_are_booleans(self) -> None:
        provider = self.make_provider()
        for flag in (
            "requires_sandbox_object_for_ws",
            "uses_create_oss_mounts",
            "endpoint_is_authoritative",
            "supports_egress_credential_injection",
            "sandbox_is_profile_exclusive",
            "supports_turn_preparation",
        ):
            assert isinstance(getattr(provider, flag, False), bool), (
                f"capability flag {flag} must be a bool"
            )

    def test_turn_preparation_capability_is_safe_and_implemented(self) -> None:
        from astrabox.seams.sandbox import SandboxProvider

        provider = self.make_provider()
        if not bool(getattr(provider, "supports_turn_preparation", False)):
            return
        from astrabox.seams.sandbox import TURN_PREPARATION_CONTRACT_VERSION

        assert (
            getattr(provider, "turn_preparation_contract_version", None)
            == TURN_PREPARATION_CONTRACT_VERSION
        ), "turn preparation contract version must match the host"
        assert getattr(provider, "sandbox_is_profile_exclusive", False) is True, (
            "turn preparation currently requires a profile-exclusive sandbox"
        )
        assert _is_overridden(provider, SandboxProvider, "owns_sandbox"), (
            "turn-preparing providers must validate live-handle ownership"
        )
        assert _is_overridden(provider, SandboxProvider, "prepare_turn"), (
            "providers enabling supports_turn_preparation must override prepare_turn"
        )
        prepare_turn = getattr(provider, "prepare_turn")
        assert inspect.iscoroutinefunction(prepare_turn), "prepare_turn must be async"
        validity = getattr(provider, "turn_preparation_validity_seconds", None)
        if validity is not None:
            assert (
                isinstance(validity, (int, float))
                and not isinstance(validity, bool)
                and validity > 0
            ), "turn_preparation_validity_seconds must be a positive number"

        parameters = list(inspect.signature(prepare_turn).parameters.values())
        assert [parameter.name for parameter in parameters] == ["context"]
        assert parameters[0].kind is inspect.Parameter.KEYWORD_ONLY

    def test_permission_levels_are_a_known_nonempty_tuple(self) -> None:
        from astrabox.seams.sandbox import SANDBOX_PERMISSION_LEVELS

        provider = self.make_provider()
        levels = provider.supported_permission_levels
        assert isinstance(levels, tuple) and levels
        assert len(levels) == len(set(levels))
        assert all(type(level) is str and level in SANDBOX_PERMISSION_LEVELS for level in levels)

    def test_validity_declaration_requires_the_capability(self) -> None:
        provider = self.make_provider()
        if getattr(provider, "turn_preparation_validity_seconds", None) is not None:
            assert bool(getattr(provider, "supports_turn_preparation", False)), (
                "a validity window is meaningless without supports_turn_preparation"
            )

    def test_registration_round_trips(self) -> None:
        from astrabox.seams.sandbox import register_sandbox, sandbox_if_registered

        provider = self.make_provider()
        register_sandbox(provider)
        assert sandbox_if_registered(provider.name) is provider


# ── StorageProvider ──────────────────────────────────────────────────────────


class StorageProviderContractSuite:
    """Subclass and implement :meth:`make_provider`."""

    def make_provider(self) -> Any:
        raise NotImplementedError("bind the suite: override make_provider()")

    def test_registration_round_trips(self) -> None:
        from astrabox.seams.storage import (
            _PROVIDERS,
            register_storage,
            storage_provider,
        )

        provider = self.make_provider()
        name = "conf_probe_storage"
        previous = _PROVIDERS.get(name)
        try:
            register_storage(name, provider)
            assert storage_provider(name) is provider
        finally:
            # A reusable conformance test must not leave a probe provider in the
            # process registry, where it would be indistinguishable from a real
            # installed plugin.
            if previous is None:
                _PROVIDERS.pop(name, None)
            else:
                _PROVIDERS[name] = previous


# ── ChannelProvider ──────────────────────────────────────────────────────────


class ChannelProviderContractSuite:
    """Subclass and implement :meth:`make_provider`.

    Pins the channel seam contract (docs/channel-spine.md): a stable name,
    the synchronous ``verify_and_resolve`` auth+mapping hook, boolean
    capability flags, and complete-or-absent capability shapes. Registration
    itself enforces the shape pairing (``register_channel`` fails loud on a
    partial opt-in), so the suite also asserts the provider actually
    registers.
    """

    def make_provider(self) -> Any:
        raise NotImplementedError("bind the suite: override make_provider()")

    def test_name_is_stable_and_nonempty(self) -> None:
        provider = self.make_provider()
        first = str(getattr(provider, "name", "") or "")
        assert first.strip(), "channel provider must expose a non-empty name"
        assert str(self.make_provider().name) == first, "name must be stable"

    def test_verify_and_resolve_is_overridden_and_synchronous(self) -> None:
        import inspect

        from astrabox.seams.channel import ChannelProvider

        provider = self.make_provider()
        assert _is_overridden(provider, ChannelProvider, "verify_and_resolve"), (
            "verify_and_resolve must be implemented (abstract on the seam)"
        )
        assert not inspect.iscoroutinefunction(
            type(provider).verify_and_resolve
        ), "verify_and_resolve is called synchronously inside the trigger"

    def test_capability_flags_are_booleans(self) -> None:
        provider = self.make_provider()
        for flag in ("supports_streaming_delivery", "supports_source"):
            assert isinstance(getattr(provider, flag, False), bool), (
                f"{flag} must be a bool"
            )

    def test_streaming_capability_is_complete_or_absent(self) -> None:
        import inspect

        from astrabox.seams.channel import ChannelProvider

        provider = self.make_provider()
        overridden = _is_overridden(provider, ChannelProvider, "open_delivery")
        if provider.supports_streaming_delivery:
            assert overridden, "supports_streaming_delivery requires open_delivery"
            assert inspect.iscoroutinefunction(type(provider).open_delivery), (
                "open_delivery must be async"
            )
        else:
            assert not overridden, (
                "an opt-out provider must not expose a partial streaming contract"
            )

    def test_source_capability_is_complete_or_absent(self) -> None:
        import inspect

        from astrabox.seams.channel import ChannelProvider

        provider = self.make_provider()
        overridden = _is_overridden(provider, ChannelProvider, "open_source")
        if provider.supports_source:
            assert overridden, "supports_source requires open_source"
            assert "binding" in inspect.signature(type(provider).open_source).parameters, (
                "open_source must be scoped to one binding"
            )
        else:
            assert not overridden, (
                "an opt-out provider must not expose a partial source contract"
            )

    def test_delivery_contract_is_async_and_binding_scoped(self) -> None:
        import inspect

        provider = self.make_provider()
        delivery = type(provider).deliver_outbound
        assert inspect.iscoroutinefunction(delivery), "deliver_outbound must be async"
        assert "binding" in inspect.signature(delivery).parameters, (
            "deliver_outbound must receive a freshly hydrated binding"
        )
        if provider.supports_streaming_delivery:
            streaming = type(provider).open_delivery
            assert "binding" in inspect.signature(streaming).parameters, (
                "open_delivery must receive a freshly hydrated binding"
            )

    def test_registration_round_trips(self) -> None:
        from astrabox.seams.channel import get_channel, register_channel

        provider = self.make_provider()
        register_channel(provider)  # fails loud on any partial capability shape
        assert get_channel(str(provider.name)) is provider

    def test_provider_module_imports_no_spine_internals(self) -> None:
        """Require provider modules to depend only on public integration seams.

        Loading the provider module must not import ``astrabox.core.*`` or
        ``astrabox.persistence.*``
        — a provider integrates through the public seams
        (``astrabox.seams.*``, ``astrabox.common.*``, ``astrabox.testing``)
        only. Runs in a fresh interpreter so this test's own imports cannot
        mask a leak; the module is loaded by FILE so the check also covers
        providers defined in non-installed modules (e.g. a test double).
        """
        import inspect
        import subprocess
        import sys

        provider_type = type(self.make_provider())
        source = inspect.getsourcefile(provider_type)
        assert source, f"cannot locate source for {provider_type!r}"
        probe = (
            "import importlib.util\n"
            "import sys\n"
            f"spec = importlib.util.spec_from_file_location('provider_probe', {source!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            # importlib requires registration BEFORE exec_module: dataclasses
            # (among others) resolve string annotations through
            # sys.modules[cls.__module__], which is otherwise None.
            "sys.modules[spec.name] = module\n"
            "try:\n"
            "    spec.loader.exec_module(module)\n"
            "finally:\n"
            "    sys.modules.pop(spec.name, None)\n"
            "leaked = sorted(\n"
            "    name for name in sys.modules\n"
            "    if name.startswith(('astrabox.core', 'astrabox.persistence'))\n"
            ")\n"
            "assert not leaked, 'loaded spine internals: ' + ', '.join(leaked)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"provider module {source!r} must not import spine internals\n"
            f"{result.stderr.strip()}"
        )
