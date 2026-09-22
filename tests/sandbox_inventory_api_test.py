"""Contracts for the read-only ``/api/v1/admin/sandboxes/*`` surface.

The API provides list, detail, and diagnostics while preserving these
boundaries:

* a backend that CANNOT answer is legible as "cannot answer" — the seam's 501
  reaches the caller with its reason intact, and is never flattened into an
  empty list that would read as "no sandboxes are running";
* the session attribution is the box's own create metadata or nothing. There is
  no fallback that derives a session from the id or the create order, because a
  wrong attribution is worse than a missing one;
* every caller string that becomes part of a BACKEND URL is refused here unless
  it is already one path segment. A control plane addressed by
  ``/sandboxes/{id}/diagnostics/{scope}`` turns an unchecked name into a
  different request against that control plane, whose answer this surface then
  hands back — an ops read becomes a proxy. The refusals below are pinned per
  field, and each asserts the backend was never asked.

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


class _SilentProvider(_BareProvider):
    """A backend that inherits every inventory default (the seam's 501 refusals)."""

    name = "fake_silent"


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


def test_list_returns_the_backends_own_paging_counters(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    body = client.get("/api/v1/admin/sandboxes?page=2&page_size=25").json()

    assert body["code"] == "OK"
    data = body["data"]
    assert data["backend"] == "fake_inventory"
    assert inventory.list_calls == [(2, 25)]
    # Counters come from the backend, so the console can page instead of
    # assuming one response is the whole inventory.
    assert data["pagination"] == {
        "page": 2,
        "page_size": 25,
        "total_items": 5,
        "total_pages": 3,
        "has_next_page": True,
    }


def test_list_shows_the_session_only_where_the_box_carries_it(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    items = client.get("/api/v1/admin/sandboxes").json()["data"]["items"]
    by_id = {item["sandbox_id"]: item for item in items}

    assert by_id["sb-ours"]["session_id"] == "sess-1"
    assert by_id["sb-ours"]["created_at"] == _CREATED_AT.isoformat()
    assert by_id["sb-ours"]["entrypoint"] == ["/opt/gem/run.sh"]
    # A box with no create metadata is still LISTED — the question is what the
    # backend runs — but nothing invents a session for it.
    assert by_id["sb-foreign"]["session_id"] is None
    assert by_id["sb-foreign"]["metadata"] == {}


def test_list_rejects_a_page_size_that_would_pull_the_whole_inventory(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    response = client.get("/api/v1/admin/sandboxes?page_size=100000")
    assert response.status_code == 400
    assert response.json()["code"] == "SANDBOX_QUERY_INVALID"
    assert inventory.list_calls == []


def test_list_rejects_a_non_numeric_page(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    response = client.get("/api/v1/admin/sandboxes?page=first")
    assert response.status_code == 400
    assert inventory.list_calls == []


def test_detail_maps_a_missing_sandbox_to_404(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    assert (
        client.get("/api/v1/admin/sandboxes/sb-ours").json()["data"]["sandbox_id"]
        == "sb-ours"
    )
    response = client.get("/api/v1/admin/sandboxes/sb-gone")
    assert response.status_code == 404
    assert response.json()["code"] == "SANDBOX_NOT_FOUND"


def test_diagnostics_passes_the_report_through_as_text(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    data = client.get(
        "/api/v1/admin/sandboxes/sb-ours/diagnostics/inspect"
    ).json()["data"]

    assert inventory.diagnostics_calls == [("sb-ours", "inspect")]
    assert data["text"] == "Pod Name: sb-ours\nPhase: Running\n"
    assert data["content_type"] == "text/plain; charset=utf-8"
    assert data["truncated"] is False
    assert data["scope"] == "inspect"
    # No parsed body, no promised fields: the payload carries text and nothing
    # that would invite a consumer to scrape it.
    assert set(data) == {
        "sandbox_id",
        "backend",
        "scope",
        "content_type",
        "text",
        "truncated",
        # The scope vocabulary, named as the constant it is: this route accepts
        # these four, and says nothing about which of them THIS sandbox can
        # produce — that is only knowable by asking for each and reading the
        # 501s.
        "known_scopes",
    }


def test_diagnostics_relays_the_servers_refusal_with_its_reason(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    response = client.get("/api/v1/admin/sandboxes/sb-ours/diagnostics/summary")

    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "SANDBOX_DIAGNOSTICS_NOT_IMPLEMENTED"
    # The operator learns WHICH server said so. Swallowing this into an empty
    # report would read as a healthy, silent sandbox.
    assert "http://server.test" in body["message"]


def test_a_backend_that_cannot_enumerate_says_so_instead_of_answering_empty(
    client: TestClient, backends: dict[str, SandboxProvider]
) -> None:
    register_sandbox(_SilentProvider())
    set_default_sandbox_backend("fake_silent")

    response = client.get("/api/v1/admin/sandboxes")

    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "SANDBOX_LISTING_UNSUPPORTED"
    assert "fake_silent" in body["message"]
    # The refusal must not be shaped like a successful empty page.
    assert body.get("data") in (None, {})


def test_an_unknown_backend_name_is_a_404_not_a_silent_default(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    response = client.get("/api/v1/admin/sandboxes?backend=not_installed")
    assert response.status_code == 404
    assert response.json()["code"] == "SANDBOX_BACKEND_UNKNOWN"
    assert inventory.list_calls == []


def test_an_ambiguous_backend_is_refused_rather_than_guessed(
    client: TestClient, backends: dict[str, SandboxProvider]
) -> None:
    register_sandbox(_InventoryProvider())
    register_sandbox(_SilentProvider())
    set_default_sandbox_backend("")

    response = client.get("/api/v1/admin/sandboxes")

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "SANDBOX_BACKEND_UNRESOLVED"
    # Naming the candidates is what makes the error actionable.
    assert "fake_inventory" in body["message"]
    assert "fake_silent" in body["message"]


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


def test_an_unknown_diagnostic_scope_is_refused_before_the_backend(
    client: TestClient, inventory: _InventoryProvider
) -> None:
    # The scope vocabulary is closed, so this is the one field whose check is a
    # membership test — and it belongs here as well as in the backend, because
    # this route is what a deployment exposes.
    for scope in ("%2e%2e", "logs%2Fx", "INSPECT-ish"):
        response = client.get(f"/api/v1/admin/sandboxes/sb-ours/diagnostics/{scope}")
        assert response.status_code in (400, 404), scope
        if response.status_code == 400:
            assert response.json()["code"] == "SANDBOX_DIAGNOSTIC_SCOPE_INVALID", scope
    assert inventory.diagnostics_calls == []


def test_ordinary_backend_id_shapes_still_pass(
    client: TestClient, backends: dict[str, SandboxProvider]
) -> None:
    # The refusal is a segment grammar, not an opinion about how a backend names
    # things: UUIDs, container digests and Kubernetes names all go through.
    provider = _InventoryProvider()
    register_sandbox(provider)
    set_default_sandbox_backend(provider.name)
    for sandbox_id in (
        "3f1a9c2b7d4e",
        "d290f1ee-6c54-4b01-90e6-d701748f0851",
        "sbx-abc.def_1",
    ):
        client.get(f"/api/v1/admin/sandboxes/{sandbox_id}")
    assert provider.describe_calls == [
        "3f1a9c2b7d4e",
        "d290f1ee-6c54-4b01-90e6-d701748f0851",
        "sbx-abc.def_1",
    ]
