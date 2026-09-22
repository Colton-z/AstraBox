"""Graceful shutdown releases loop-owned provider control-plane resources."""

from __future__ import annotations

import pytest

import astrabox.core.service.orchestrator.service_registry as registry
import astrabox.seams.sandbox as sandbox_seam


async def test_lifecycle_shutdown_awaits_provider_cleanup_after_quiescing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def close_services(*, reason: str) -> None:
        events.append(f"services:{reason}")

    async def close_providers() -> None:
        events.append("providers")

    monkeypatch.setattr(registry, "close_services_for_lifecycle", close_services)
    monkeypatch.setattr(
        sandbox_seam,
        "shutdown_sandbox_providers_for_current_loop",
        close_providers,
        raising=False,
    )

    await registry.run_lifecycle_shutdown(reason="test-shutdown")

    assert events == ["services:test-shutdown", "providers"]
