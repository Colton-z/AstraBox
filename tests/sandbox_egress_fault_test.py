"""The gated per-sandbox egress fault surface."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import astrabox.seams.sandbox as sandbox_seam
from astrabox.api import app as app_module
from astrabox.api.routes._shared import handle_api_error
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import (
    SandboxProvider,
    SandboxSecurityPosture,
    register_sandbox,
    set_default_sandbox_backend,
)


class _EgressProvider(SandboxProvider):
    name = "egress_test"

    def __init__(self, rules: tuple[tuple[str, str], ...] = ()) -> None:
        self.rules = dict((target, action) for action, target in rules)
        self.mutations: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("unused")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("unused")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("unused")

    async def read_security_posture(self, sandbox_id: str) -> SandboxSecurityPosture:
        return SandboxSecurityPosture(
            sandbox_id=sandbox_id,
            available=True,
            default_action="allow",
            egress_rules=tuple((action, target) for target, action in self.rules.items()),
        )

    async def patch_egress_rules(
        self,
        sandbox_id: str,
        *,
        rules: tuple[tuple[str, str], ...],
    ) -> None:
        self.mutations.append(("patch", sandbox_id, rules))
        for action, target in rules:
            self.rules[target] = action

    async def delete_egress_rules(
        self,
        sandbox_id: str,
        *,
        targets: tuple[str, ...],
    ) -> None:
        self.mutations.append(
            ("delete", sandbox_id, tuple(("", target) for target in targets))
        )
        for target in targets:
            self.rules.pop(target, None)


@pytest.fixture
def provider() -> Iterator[_EgressProvider]:
    saved_backends = dict(sandbox_seam._BACKENDS)
    saved_default = sandbox_seam._DEFAULT_BACKEND
    sandbox_seam._BACKENDS.clear()
    current = _EgressProvider((('allow', 'platform.internal'),))
    register_sandbox(current)
    set_default_sandbox_backend(current.name)
    try:
        yield current
    finally:
        sandbox_seam._BACKENDS.clear()
        sandbox_seam._BACKENDS.update(saved_backends)
        set_default_sandbox_backend(saved_default)


def _fault_app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, handle_api_error)
    app_module._register_e2e_fault_routes_if_armed(app)
    return app


def test_disabled_gate_registers_no_route_and_cannot_touch_a_provider(
    provider: _EgressProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ASTRABOX_E2E_FAULTS", raising=False)
    app = _fault_app()

    response = TestClient(app).post(
        "/api/v1/admin/e2e/sandboxes/box-1/egress",
        json={"operation": "patch", "action": "deny", "target": "platform.internal"},
    )

    assert response.status_code == 404
    assert all(
        getattr(route, "name", "") != "mutate_e2e_sandbox_egress"
        for route in app.routes
    )
    assert provider.mutations == [], (
        "without ASTRABOX_E2E_FAULTS there is no request path to live policy mutation"
    )
    assert provider.rules == {"platform.internal": "allow"}
