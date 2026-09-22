"""Sandbox-lease renewal routes through the provider seam, not an object method.

In-memory sandbox handles carry no ``renew`` method: whether a sandbox has a TTL
at all is a BACKEND property, not a handle one. Renewal therefore goes through
the sandbox provider seam (``sandbox_for_name(backend).renew(sandbox_id, ttl)``),
whose default is a no-op returning ``None`` for a backend with no TTL concept,
and which ``open_sandbox`` implements as a real lease extension. These tests pin
that ``renew_runtime`` resolves the backend and calls the provider — and never
calls ``.renew`` on the in-memory sandbox handle (the mechanism that logged a
warning on every renewal tick).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from astrabox.core.service.orchestrator import runtime_manager as rm
from astrabox.core.service.orchestrator.runtime_manager import RemoteAgentRuntimeManager


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def renew(self, sandbox_id: str, ttl_seconds: int) -> None:
        self.calls.append((sandbox_id, ttl_seconds))
        return None


class _ExplodingSandboxHandle:
    """A sandbox handle that fails if the object-level renew API is used."""

    def __getattr__(self, name: str) -> Any:
        if name == "renew":
            raise AssertionError("renew_runtime must not call sandbox.renew()")
        raise AttributeError(name)


class RenewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def _run_renew(self, provider: _RecordingProvider) -> Any:
        manager = RemoteAgentRuntimeManager()
        manager._runtimes["sess-1"] = SimpleNamespace(
            sandbox_id="container-abc",
            sandbox=_ExplodingSandboxHandle(),
        )

        async def _fake_resolve_backend(sandbox_id: str) -> str:
            return "open_sandbox"

        manager._resolve_sandbox_backend = _fake_resolve_backend  # type: ignore[method-assign]
        with patch.object(rm, "sandbox_for_name", return_value=provider):
            return await manager.renew_runtime("sess-1", 14400.0)

    async def test_renew_routes_through_provider_seam(self) -> None:
        provider = _RecordingProvider()
        result = await self._run_renew(provider)
        self.assertIsNone(result)
        self.assertEqual(provider.calls, [("container-abc", 14400)])

    async def test_renew_missing_runtime_is_noop(self) -> None:
        manager = RemoteAgentRuntimeManager()
        self.assertIsNone(await manager.renew_runtime("absent", 100.0))

    async def test_renew_runtime_without_sandbox_id_is_noop(self) -> None:
        manager = RemoteAgentRuntimeManager()
        manager._runtimes["sess-2"] = SimpleNamespace(sandbox_id="", sandbox=None)
        self.assertIsNone(await manager.renew_runtime("sess-2", 100.0))


if __name__ == "__main__":
    unittest.main()
