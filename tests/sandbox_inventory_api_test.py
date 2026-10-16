"""Contracts for the read-only ``/api/v1/admin/sandboxes/*`` surface.

Every caller string that becomes part of a BACKEND URL is refused unless it is
already one path segment. A control plane addressed by
``/sandboxes/{id}/diagnostics/{scope}`` turns an unchecked name into a
different request against that control plane, whose answer this surface then
hands back — an ops read becomes a proxy. The refusals below assert the backend
was never asked.

The route prefix is also part of the contract because
:mod:`astrabox.web.identity_middleware` applies the admin gate at
``/api/v1/admin``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import astrabox.seams.sandbox as sandbox_seam
from astrabox.api.routes.sandboxes import register_sandbox_routes
from astrabox.api.routes._shared import handle_api_error
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox import (
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxDescriptor,
    SandboxDiagnostics,
    SandboxPage,
    SandboxProvider,
    register_sandbox,
    set_default_sandbox_backend,
)

_CREATED_AT = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)


class _BareProvider(SandboxProvider):
    """The abstract lifecycle pairs stubbed out; this surface never calls them."""

    def connection_config(self, **kwargs: Any) -> Any:
        return None

    def secret_material(self, *, settings: Any) -> str:
        return ""

    def build_dataplane(self, **kwargs: Any) -> Any:
        raise RuntimeError("no dataplane")

    async def connect(self, sandbox_id: str) -> Any:
        raise RuntimeError("no connect")

    async def kill(self, sandbox_id: str) -> bool:
        raise RuntimeError("no kill")


class _InventoryProvider(_BareProvider):
    """A backend that answers the inventory face."""

    name = "fake_inventory"

    def __init__(self) -> None:
        self.list_calls: list[tuple[int, int]] = []
        self.describe_calls: list[str] = []
        self.diagnostics_calls: list[tuple[str, str]] = []

    def _descriptor(self, sandbox_id: str, *, session_id: str | None) -> SandboxDescriptor:
        metadata = (
            {SANDBOX_SESSION_ID_METADATA_KEY: session_id} if session_id else {}
        )
        return SandboxDescriptor(
            sandbox_id=sandbox_id,
            state="Running",
            created_at=_CREATED_AT,
            expires_at=None,
            image="astrabox/agent:1",
            entrypoint=("/opt/gem/run.sh",),
            metadata=metadata,
            session_id=session_id,
        )

    async def list_sandboxes(self, *, page: int = 1, page_size: int = 50) -> SandboxPage:
        self.list_calls.append((page, page_size))
        return SandboxPage(
            items=(
                self._descriptor("sb-ours", session_id="sess-1"),
                self._descriptor("sb-foreign", session_id=None),
            ),
            page=page,
            page_size=page_size,
            total_items=5,
            total_pages=3,
            has_next_page=True,
        )

    async def describe_sandbox(self, sandbox_id: str) -> SandboxDescriptor:
        self.describe_calls.append(sandbox_id)
        if sandbox_id != "sb-ours":
            raise APIError(
                code="SANDBOX_NOT_FOUND",
                message=f"sandbox {sandbox_id!r} not found",
                status_code=404,
            )
        return self._descriptor(sandbox_id, session_id="sess-1")

    async def read_diagnostics(
        self, sandbox_id: str, *, scope: str
    ) -> SandboxDiagnostics:
        self.diagnostics_calls.append((sandbox_id, scope))
        if scope == "summary":
            raise APIError(
                code="SANDBOX_DIAGNOSTICS_NOT_IMPLEMENTED",
                message=(
                    "the OpenSandbox server at http://server.test does not "
                    "implement the 'summary' diagnostic report"
                ),
                status_code=501,
            )
        return SandboxDiagnostics(
            sandbox_id=sandbox_id,
            scope=scope,
            content_type="text/plain; charset=utf-8",
            text="Pod Name: sb-ours\nPhase: Running\n",
            truncated=False,
        )


@pytest.fixture(scope="module")
def app() -> FastAPI:
    # Module-scoped and held: ``register_sandbox_routes`` is idempotent by
    # ``id(app)``, so a per-test app could silently skip registration if a
    # collected one left its address behind.
    application = FastAPI()
    application.add_exception_handler(APIError, handle_api_error)
    register_sandbox_routes(application)
    return application


@pytest.fixture
def backends() -> Iterator[dict[str, SandboxProvider]]:
    """Swap the process-wide backend registry for the duration of one test."""
    saved_backends = dict(sandbox_seam._BACKENDS)
    saved_default = sandbox_seam._DEFAULT_BACKEND
    sandbox_seam._BACKENDS.clear()
    try:
        yield sandbox_seam._BACKENDS
    finally:
        sandbox_seam._BACKENDS.clear()
        sandbox_seam._BACKENDS.update(saved_backends)
        set_default_sandbox_backend(saved_default)


@pytest.fixture
def inventory(backends: dict[str, SandboxProvider]) -> _InventoryProvider:
    provider = _InventoryProvider()
    register_sandbox(provider)
    set_default_sandbox_backend(provider.name)
    return provider


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def test_the_ops_surface_sits_behind_the_admin_prefix_gate(app: FastAPI) -> None:
    from astrabox.web.identity_middleware import _is_admin_path

    paths = [
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/")
    ]
    assert paths, "no sandbox routes were registered"
    # The gate is the PREFIX. A route that drifted to /api/v1/sandboxes would
    # be an ungated ops surface, not a cosmetic difference.
    assert all(_is_admin_path(path) for path in paths), paths
    assert "/api/v1/admin/sandboxes" in paths


# ── caller strings that become backend URLs ─────────────────────────────────
#
# Each name below reaches a backend as a PATH SEGMENT. An HTTP client resolves
# `..` in a path, so a name carrying one fetches a different endpoint of the
# control plane. Admin-only does not shrink that boundary: the control plane is
# reachable from this process and, in the usual deployment, from nowhere else.


def test_a_sandbox_id_cannot_leave_its_url_segment(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    # `%2e%2e` is the spelling that survives: the client sends it encoded, and
    # the id arrives at the route as `..`.
    for encoded in ("%2e%2e", "%2E%2E", "sb%00ours", "sb:ours", "%2Fv1%2Fsandboxes"):
        response = client.get(f"/api/v1/admin/sandboxes/{encoded}")

        # A path this router cannot match at all is refused by the router (404);
        # one it CAN match must be refused by this surface (400). Either way the
        # backend is never asked.
        assert response.status_code in (400, 404), encoded
        if response.status_code == 400:
            assert response.json()["code"] == "SANDBOX_NAME_INVALID", encoded
    assert inventory.describe_calls == []


def test_a_sandbox_id_cannot_leave_its_segment_on_the_diagnostics_route(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    response = client.get("/api/v1/admin/sandboxes/%2e%2e/diagnostics/inspect")

    assert response.status_code == 400
    assert response.json()["code"] == "SANDBOX_NAME_INVALID"
    assert inventory.diagnostics_calls == []
