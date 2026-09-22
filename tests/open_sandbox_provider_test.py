"""OpenSandbox sandbox-provider unit tests (no live server).

Everything runs against an ``httpx.MockTransport`` injected through the
provider constructors' test-only ``transport`` parameter (the signature
injection point ``_config.sdk_connection_config`` documents) — never a
monkeypatch of the SDK. The fake speaks the exact lifecycle/execd wire the SDK
speaks: lifecycle JSON under ``/v1``, execd SSE for command runs, and the
``/files/download`` byte face.

Pinned here:

* the capability flag FREEZE TABLE (exact values, including the
  ``conversation_bootstrap_transport == "sandbox_command_script"`` pin against
  the sidecar-HTTP transport, whose route the in-box server does not serve);
* registration round-trip through the real ``register_builtin_providers``
  (sidecar-pair validation + provisioning composition run for real);
* the READY gate: an ``open_sandbox`` Agent session resolves READY without an
  Assistant workspace binding, a missing storage peer fails CLOSED, and a single-sided
  provisioning shape fails bootstrap composition validation loudly;
* the lifecycle error taxonomy: probe four-state (never raises), idempotent
  kill on 404, monotonic renew, fail-loud expires_at, dead-box fast connect;
* the execd LINE EVENT MODEL → bytes reassembly, every measured shape, with the
  two lossy ones asserting their loss (the file panel's status split depends on
  interior line boundaries surviving);
* key hygiene: the API key never appears in an exception's str/repr;
* the control-plane INVENTORY face: paging at the source, the create-metadata
  round trip that is the only thing tying a live box to a session, the
  plain-text diagnostics passthrough with its truncation rule, and the two
  refusals that must stay refusals — a server's 501 and the seam's own default.
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import astrabox.providers.open_sandbox.sandbox as sandbox_module
from astrabox.common.utils.errors import APIError
from astrabox.config.release_images import release_image
from astrabox.providers.open_sandbox import _config
from astrabox.providers.open_sandbox._metadata import (
    assignment_metadata_value,
    session_metadata_value,
)
from astrabox.providers.open_sandbox.sandbox import (
    EXECD_LINE_EVENT_MODEL,
    EXECD_LINE_EVENT_MODEL_VERSION,
    OpenSandboxDataPlane,
    OpenSandboxEndpoint,
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
    collect_execd_stream,
)
from astrabox.providers.sandbox_image import AGENT_IMAGE_COMPONENT
from astrabox.seams.sandbox import (
    SANDBOX_ASSIGNMENT_ID_METADATA_KEY,
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
    SANDBOX_SESSION_ID_METADATA_KEY,
    SandboxLifecycleProbeResult,
    SandboxProvider,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

_BASE_URL = "http://opensandbox.test:8080"
_EXECD_HOST = "box.execd.test"


@pytest.fixture(autouse=True)
def _lifecycle_base_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The provider resolves deployment settings per op (load_astrabox_settings
    # re-reads the environment), so pointing the env at the fake host is the
    # whole wiring.
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _BASE_URL)
    # The SDK's create path fires a best-effort telemetry POST from its OWN
    # httpx client, which the injected MockTransport does not cover — it would
    # be a real network call from a unit test. Off.
    monkeypatch.setenv("OPENSANDBOX_DISABLE_METRICS", "1")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class _FakeLifecycle:
    """A minimal OpenSandbox server behind ``httpx.MockTransport``.

    Serves the lifecycle JSON API under ``/v1`` on the lifecycle host, and the
    execd command/file faces on the per-sandbox endpoint host, so the SDK's own
    adapters run unmodified against it.
    """

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.sandboxes: dict[str, dict[str, Any]] = {}
        self.created_at = now - timedelta(minutes=5)
        self.endpoint_headers: dict[int, dict[str, str]] = {}
        self.endpoint_queries: list[dict[str, list[str]]] = []
        self.renew_expires_at: datetime | None = None
        self.kill_status_override: int | None = None
        self.error_message = "boom"
        self.raise_transport_errors = False
        #: [(method, path)] across BOTH hosts, for call-shape assertions.
        self.requests: list[tuple[str, str]] = []
        #: plain-text diagnostic bodies keyed by scope; a scope with no entry
        #: answers 501 the way a server without that report does.
        self.diagnostics: dict[str, str] = {}
        #: ``(scope, chunk_generator_factory)`` — a diagnostic served as a
        #: STREAM rather than a fixed body, so a test can observe how much of it
        #: the provider actually pulls.
        self.diagnostics_stream: tuple[str, Any] | None = None
        #: query strings seen on GET /v1/sandboxes, for paging assertions.
        self.list_queries: list[dict[str, list[str]]] = []
        #: page size the fake reports back regardless of what was asked, so a
        #: test can pin that the SERVER's counters ride the page.
        self.list_page_size = 2
        #: execution SSE bodies keyed by literal command string; fallback runs
        #: an empty successful execution.
        self.command_sse: dict[str, str] = {}
        #: execution SSE bodies keyed by a SUBSTRING of the command (checked
        #: after the literal map; first match wins). The readiness probe is a
        #: multi-line interpreter script, so keying it literally is unreadable.
        self.command_sse_by_substring: dict[str, str] = {}
        #: every command string the execd face was asked to run.
        self.commands: list[str] = []
        #: file bytes served by /files/download, keyed by in-box path.
        self.files: dict[str, bytes] = {}
        #: create request bodies seen on POST /v1/sandboxes.
        self.create_bodies: list[dict[str, Any]] = []
        #: Actual execd isolation answer for permission-level create proofs.
        self.isolation_available = True
        self.isolation_message: str | None = None
        #: make-dirs bodies seen on the execd filesystem face.
        self.created_directories: list[dict[str, Any]] = []
        self._created_count = 0

    def add_sandbox(
        self,
        sandbox_id: str,
        *,
        state: str = "Running",
        expires_at: datetime | None = None,
        metadata: dict[str, str] | None = None,
        image: str | None = None,
    ) -> None:
        self.sandboxes[sandbox_id] = {
            "state": state,
            "expires_at": expires_at,
            "metadata": dict(metadata or {}),
            "image": image,
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- wire ----------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.raise_transport_errors:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host == _EXECD_HOST:
            return self._handle_execd(request)
        return self._handle_lifecycle(request)

    def _error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": self.error_message})

    def _sandbox_json(self, sandbox_id: str) -> dict[str, Any]:
        row = self.sandboxes[sandbox_id]
        body: dict[str, Any] = {
            "id": sandbox_id,
            "status": {"state": row["state"]},
            "entrypoint": ["/opt/gem/run.sh"],
            "createdAt": _iso(self.created_at),
        }
        if row["expires_at"] is not None:
            body["expiresAt"] = _iso(row["expires_at"])
        if row.get("metadata"):
            body["metadata"] = dict(row["metadata"])
        if row.get("image"):
            body["image"] = {"uri": row["image"]}
        return body

    def _handle_list(self, request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.url.query.decode())
        self.list_queries.append(query)
        page = int(query.get("page", ["1"])[0])
        page_size = self.list_page_size
        metadata_filter = {
            key: value
            for pair in (query.get("metadata") or [""])[0].split("&")
            if pair and "=" in pair
            for key, value in [pair.split("=", 1)]
        }
        ordered = [
            sandbox_id
            for sandbox_id, row in self.sandboxes.items()
            if all(
                str((row.get("metadata") or {}).get(key) or "") == value
                for key, value in metadata_filter.items()
            )
        ]
        start = (page - 1) * page_size
        window = ordered[start : start + page_size]
        total_pages = max(1, -(-len(ordered) // page_size))
        return httpx.Response(
            200,
            json={
                "items": [self._sandbox_json(sid) for sid in window],
                "pagination": {
                    "page": page,
                    "pageSize": page_size,
                    "totalItems": len(ordered),
                    "totalPages": total_pages,
                    "hasNextPage": page < total_pages,
                },
            },
        )

    def _handle_lifecycle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes" and request.method == "GET":
            return self._handle_list(request)
        if request.url.path == "/v1/sandboxes" and request.method == "POST":
            body = json.loads(request.content)
            self.create_bodies.append(body)
            self._created_count += 1
            sandbox_id = f"sb-created-{self._created_count}"
            self.add_sandbox(sandbox_id, metadata=body.get("metadata"))
            # The lifecycle create answers 202 (the only success status the
            # SDK's generated client parses for this route).
            return httpx.Response(202, json=self._sandbox_json(sandbox_id))
        parts = request.url.path.strip("/").split("/")
        # /v1/sandboxes/{id}[/...]
        if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "sandboxes":
            sandbox_id = parts[2]
            tail = parts[3:]
            if sandbox_id not in self.sandboxes:
                return self._error(404, "NOT_FOUND")
            if not tail and request.method == "GET":
                return httpx.Response(200, json=self._sandbox_json(sandbox_id))
            if not tail and request.method == "DELETE":
                if self.kill_status_override is not None:
                    return self._error(self.kill_status_override, "INTERNAL_ERROR")
                del self.sandboxes[sandbox_id]
                return httpx.Response(204)
            if tail == ["metadata"] and request.method == "PATCH":
                patch = json.loads(request.content)
                metadata = self.sandboxes[sandbox_id]["metadata"]
                for key, value in patch.items():
                    if value is None:
                        metadata.pop(key, None)
                    else:
                        metadata[key] = value
                return httpx.Response(200, json=self._sandbox_json(sandbox_id))
            if tail == ["renew-expiration"] and request.method == "POST":
                assert self.renew_expires_at is not None, "renew not expected"
                self.sandboxes[sandbox_id]["expires_at"] = self.renew_expires_at
                return httpx.Response(200, json={"expiresAt": _iso(self.renew_expires_at)})
            if len(tail) == 2 and tail[0] == "diagnostics" and request.method == "GET":
                if self.diagnostics_stream is not None:
                    scope, factory = self.diagnostics_stream
                    if tail[1] == scope:
                        return httpx.Response(
                            200,
                            content=factory(),
                            headers={"Content-Type": "text/plain; charset=utf-8"},
                        )
                body = self.diagnostics.get(tail[1])
                if body is None:
                    # What a server WITHOUT that report answers; the provider
                    # must relay it rather than invent an empty report.
                    return httpx.Response(
                        501,
                        json={
                            "code": "DIAGNOSTICS_NOT_IMPLEMENTED",
                            "message": "structured diagnostics are not implemented",
                        },
                    )
                return httpx.Response(
                    200,
                    content=body.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                )
            if len(tail) == 2 and tail[0] == "endpoints" and request.method == "GET":
                port = int(tail[1])
                query = parse_qs(request.url.query.decode())
                self.endpoint_queries.append(query)
                expires = (query.get("expires") or [""])[0]
                raw_endpoint = (
                    f"gateway.test/{sandbox_id}/{port}/{expires}/signed"
                    if expires
                    else f"{_EXECD_HOST}:{port}"
                )
                body: dict[str, Any] = {"endpoint": raw_endpoint}
                headers = self.endpoint_headers.get(port)
                if headers:
                    body["headers"] = headers
                return httpx.Response(200, json=body)
        return self._error(404, "NOT_FOUND")

    def _handle_execd(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ping":
            return httpx.Response(200, json={"status": "ok"})
        if (
            request.url.path == "/v1/isolated/capabilities"
            and request.method == "GET"
        ):
            return httpx.Response(
                200,
                json={
                    "available": self.isolation_available,
                    "isolator": "bubblewrap" if self.isolation_available else None,
                    "message": self.isolation_message,
                },
            )
        if request.url.path == "/directories" and request.method == "POST":
            self.created_directories.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if request.url.path == "/command" and request.method == "POST":
            command = str(json.loads(request.content).get("command") or "")
            self.commands.append(command)
            default = 'data: {"type":"execution_complete","timestamp":0,"execution_time":1}\n\n'
            for needle, body in self.command_sse_by_substring.items():
                if needle in command:
                    default = body
                    break
            sse = self.command_sse.get(command, default)
            return httpx.Response(
                200,
                content=sse.encode("utf-8"),
                headers={"Content-Type": "text/event-stream"},
            )
        if request.url.path == "/files/download" and request.method == "GET":
            path = parse_qs(request.url.query.decode())["path"][0]
            data = self.files.get(path)
            if data is None:
                return self._error(404, "NOT_FOUND")
            return httpx.Response(200, content=data)
        return self._error(404, "NOT_FOUND")


def _provider(fake: _FakeLifecycle) -> OpenSandboxSandboxProvider:
    return OpenSandboxSandboxProvider(transport=fake.transport())


# The two conformance suite bindings live in tests/provider_conformance_test.py
# (one binding site for all built-in backends).


# ── flag freeze table ────────────────────────────────────────────────────────

# The activation design's §2 table, frozen EXACTLY. A drift here is a behavior
# change in the engine's branch selection (dispatch gate, mount short-circuits,
# sidecar pairing), not a style edit — treat any diff as a regression.
_FLAG_FREEZE_TABLE: dict[str, object] = {
    "requires_sandbox_object_for_ws": False,
    "uses_create_oss_mounts": True,
    "connection_secret_uses_legacy_sandbox_api_key": False,
    "conversation_bootstrap_transport": "sandbox_command_script",
    "requires_https_git": False,
    "endpoint_is_authoritative": False,
    "assistant_workspace_root_preprovisioned": False,
    "supports_correlated_create": True,
    "supports_egress_credential_injection": True,
    "sandbox_is_profile_exclusive": True,
    "supports_turn_preparation": False,
    "turn_preparation_contract_version": None,
    "turn_preparation_validity_seconds": None,
}


def test_flag_freeze_table_is_exact() -> None:
    provider = OpenSandboxSandboxProvider()
    for flag, expected in _FLAG_FREEZE_TABLE.items():
        actual = getattr(provider, flag)
        if expected is None or isinstance(expected, bool):
            assert actual is expected, f"{flag}: {actual!r} != {expected!r}"
        else:
            assert actual == expected, f"{flag}: {actual!r} != {expected!r}"
    # Pinned explicitly: the in-box server serves NO /conversation/bootstrap
    # route, so a sidecar_http transport would 404 on every bootstrap.
    assert provider.conversation_bootstrap_transport == "sandbox_command_script"


# ── registration + composition + READY gate ─────────────────────────────────


def test_builtin_registration_round_trips() -> None:
    from astrabox.providers import register_builtin_providers
    from astrabox.seams.sandbox import sandbox_for_name
    from astrabox.seams.storage import storage_provider

    # Runs the real sidecar-pair validation, not a hand-rolled subset. Storage
    # is asserted by ITS own name: the two seams are keyed independently, and a
    # storage provider named after this backend is exactly what this cut removed.
    register_builtin_providers()
    provider = sandbox_for_name("open_sandbox")
    assert storage_provider("mounted_volume").name == "mounted_volume"


def test_the_registered_backend_implements_box_creation() -> None:
    # ``create_sandbox`` is soft-abstract on the seam: the base raises
    # NotImplementedError so a lifecycle-only integration stays expressible.
    # The platform calls it unconditionally after it has made the placement,
    # workspace, credential and startup decisions, so the selected backend
    # must provide the atomic supplier capability.
    from astrabox.providers import register_builtin_providers
    from astrabox.seams.sandbox import sandbox_for_name

    register_builtin_providers()
    provider = sandbox_for_name("open_sandbox")
    assert type(provider).create_sandbox is not SandboxProvider.create_sandbox


def test_entry_points_declare_open_sandbox_and_local_storage_targets() -> None:
    # The installed distribution's entry-point metadata only refreshes on
    # reinstall, so the gate checks the pyproject DECLARATION (the source of
    # truth a packaging build consumes), not importlib.metadata.
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    entry_points = pyproject["project"]["entry-points"]
    assert (
        entry_points["astrabox.providers.sandbox"]["open_sandbox"]
        == "astrabox.providers.open_sandbox.sandbox:OpenSandboxSandboxProvider"
    )
    assert (
        entry_points["astrabox.providers.storage"]["mounted_volume"]
        == "astrabox.providers.storage.mounted_volume:MountedVolumeStorage"
    )
    # The storage group is keyed by storage provider name; no row is named after
    # a sandbox backend.
    assert "open_sandbox" not in entry_points["astrabox.providers.storage"]


def test_pytest_defaults_enforce_provider_test_isolation() -> None:
    with open(_REPO_ROOT / "pyproject.toml", "rb") as fh:
        pyproject = tomllib.load(fh)
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_options["addopts"] == (
        "-m 'not e2e and not mongo and not postgresql and not opensandbox' --timeout=120 "
        "--strict-markers --ignore-glob='**/._*'"
    )
    assert any(m.startswith("postgresql:") for m in pytest_options["markers"])
    assert any(m.startswith("opensandbox:") for m in pytest_options["markers"])


def _agent_session(session_id: str = "sess-ready") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "session_kind": "agent_chat",
        "engine_kind": "claude_code",
        "state": "READY",
        "sandbox_id": "sb-ready-1",
        "sandbox_backend": "open_sandbox",
        "user_id": "user-1",
    }


async def test_ready_gate_open_sandbox_without_binding_is_ready() -> None:
    from astrabox.core.service.orchestrator.runtime_binding import (
        reconcile_session_runtime_binding,
    )
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    _, resolution = await reconcile_session_runtime_binding(
        session=_agent_session(), sessions_repo=None
    )
    assert resolution.status == "READY"
    assert resolution.can_dispatch is True
    assert resolution.sandbox_id == "sb-ready-1"


async def test_ready_gate_uninstalled_engine_is_a_named_unavailable_state() -> None:
    from astrabox.core.service.orchestrator.runtime_binding import (
        reconcile_session_runtime_binding,
    )
    from astrabox.providers import register_builtin_providers

    register_builtin_providers()
    session = _agent_session()
    session["engine_kind"] = "uninstalled"
    _, resolution = await reconcile_session_runtime_binding(
        session=session,
        sessions_repo=None,
    )

    assert resolution.status == "UNAVAILABLE"
    assert resolution.reason_code == "SESSION_RUNTIME_IDENTITY_INVALID"
    assert resolution.can_dispatch is False


# ── probe (never raises; four states; override pinned) ──────────────────────


def test_probe_is_overridden_not_the_seam_default() -> None:
    # The runtime's probe wrapper only bounds TimeoutError; the seam default
    # raises NotImplementedError, which would punch through the watcher.
    assert type(OpenSandboxSandboxProvider()).probe is not SandboxProvider.probe


async def test_probe_running_maps_to_ok() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    probe = await _provider(fake).probe("sb-1")
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK
    assert probe.sandbox_state == "running"


async def test_probe_terminated_state_is_in_the_broker_terminal_set() -> None:
    from astrabox.core.service.orchestrator.runtime_manager import (
        _INTERACTION_BROKER_TERMINAL_SANDBOX_STATES,
    )

    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1", state="Terminated")
    probe = await _provider(fake).probe("sb-1")
    # A dead-but-present box converges through its STATE (the broker's terminal
    # vocabulary), not through a probe failure.
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK
    assert probe.sandbox_state == "terminated"
    assert probe.sandbox_state in _INTERACTION_BROKER_TERMINAL_SANDBOX_STATES


async def test_probe_404_maps_to_not_found() -> None:
    fake = _FakeLifecycle()
    probe = await _provider(fake).probe("sb-missing")
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND


async def test_probe_transport_error_is_probe_failed_and_not_terminal() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.raise_transport_errors = True
    probe = await _provider(fake).probe("sb-1")
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_FAILED
    # Transient: carries NO sandbox_state, so it can never satisfy the broker's
    # terminal-state check.
    assert probe.sandbox_state is None
    assert probe.error_text


# ── kill / renew / expires_at ────────────────────────────────────────────────


async def test_kill_destroys_and_returns_true() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    assert await _provider(fake).kill("sb-1") is True
    assert "sb-1" not in fake.sandboxes


async def test_kill_404_is_idempotent_true() -> None:
    fake = _FakeLifecycle()
    assert await _provider(fake).kill("sb-gone") is True


async def test_kill_server_error_raises_agent_runtime_error() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.kill_status_override = 500
    with pytest.raises(APIError) as err:
        await _provider(fake).kill("sb-1")
    assert err.value.code == "AGENT_RUNTIME_ERROR"
    assert err.value.status_code == 502


async def test_renew_short_circuits_when_expiry_already_covers_target() -> None:
    fake = _FakeLifecycle()
    current = datetime.now(timezone.utc) + timedelta(hours=8)
    fake.add_sandbox("sb-1", expires_at=current)
    renewed = await _provider(fake).renew("sb-1", ttl_seconds=3600)
    assert renewed == current
    # Monotonic: never shortens a lease, so the public renew API is not called.
    assert ("POST", "/v1/sandboxes/sb-1/renew-expiration") not in fake.requests


async def test_renew_extends_via_the_public_api_and_returns_its_expiry() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1", expires_at=datetime.now(timezone.utc) + timedelta(seconds=100))
    fake.renew_expires_at = datetime.now(timezone.utc) + timedelta(hours=4)
    renewed = await _provider(fake).renew("sb-1", ttl_seconds=14400)
    assert renewed == fake.renew_expires_at
    assert ("POST", "/v1/sandboxes/sb-1/renew-expiration") in fake.requests


async def test_renew_404_raises_sandbox_not_found() -> None:
    fake = _FakeLifecycle()
    with pytest.raises(APIError) as err:
        await _provider(fake).renew("sb-gone", ttl_seconds=3600)
    assert err.value.code == "SANDBOX_NOT_FOUND"
    assert err.value.status_code == 404


async def test_expires_at_reads_the_control_plane() -> None:
    fake = _FakeLifecycle()
    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    fake.add_sandbox("sb-1", expires_at=expiry)
    assert await _provider(fake).expires_at("sb-1") == expiry


async def test_expires_at_404_raises_instead_of_returning_none() -> None:
    # A swallowed 404 → None would read as "no lease" to the expiration
    # watcher; the seam's None is reserved for "backend has no TTL concept".
    fake = _FakeLifecycle()
    with pytest.raises(APIError) as err:
        await _provider(fake).expires_at("sb-gone")
    assert err.value.code == "SANDBOX_NOT_FOUND"
    assert err.value.status_code == 404


# ── connect / handle / dataplane / endpoint ──────────────────────────────────


async def test_connect_dead_box_fails_fast_before_any_endpoint_fetch() -> None:
    # Fails fast, AND says gone: a per-session box is never restarted in place
    # (identity IS addressing), so "not RUNNING" is the same fact as "removed"
    # and must reach the turn path under the same classification. A generic 502
    # here would read as a transient backend blip, which reattaches the
    # dead instance instead of re-borrowing.
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1", state="Terminated")
    with pytest.raises(APIError) as err:
        await _provider(fake).connect("sb-1")
    assert err.value.code == "SANDBOX_GONE"
    endpoint_fetches = [p for _, p in fake.requests if "/endpoints/" in p]
    assert endpoint_fetches == [], "still no endpoint fetch for a dead box"


async def test_connect_returns_an_owned_handle_without_a_sandbox_attribute() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    provider = _provider(fake)
    handle = await provider.connect("sb-1")
    try:
        assert isinstance(handle, OpenSandboxHandle)
        assert handle.sandbox_id == "sb-1"
        assert handle.id == "sb-1"
        assert provider.owns_sandbox(handle) is True
        assert provider.owns_sandbox(object()) is False
        # get_underlying_sandbox unwraps a `.sandbox` attribute; this handle IS
        # the object consumers hold, so it must not expose one.
        assert getattr(handle, "sandbox", None) is None
    finally:
        await handle.close()


async def test_connect_404_says_the_box_is_GONE_not_merely_not_found() -> None:
    # `is_sandbox_gone_error` classifies on SANDBOX_GONE and nothing else;
    # it arms the same-send re-borrow, so a generic 404 here demotes an
    # out-of-band death to the lapsed-lease backstop — the user's message
    # fails and only the NEXT one gets a box.
    #
    # The typed code is the whole mechanism: nothing downstream re-derives
    # "gone" from the message text, so a provider returning the generic code
    # disables both gates at once and leaves no trace of having done so.
    fake = _FakeLifecycle()
    with pytest.raises(APIError) as err:
        await _provider(fake).connect("sb-gone")
    assert err.value.code == "SANDBOX_GONE"


async def test_a_non_connect_404_stays_the_generic_not_found() -> None:
    # The gone-classification is scoped to CONNECT on purpose: an admin
    # describing a bogus id, or a sweep renewing an already-reaped box, is not
    # a turn discovering that its sandbox died.
    fake = _FakeLifecycle()
    with pytest.raises(APIError) as err:
        await _provider(fake).describe_sandbox("sb-gone")
    assert err.value.code == "SANDBOX_NOT_FOUND"


async def test_handle_get_endpoint_normalizes_and_memoizes() -> None:
    calls: list[int] = []

    class _FakeSdk:
        id = "sb-1"

        async def get_endpoint(self, port: int) -> SimpleNamespace:
            calls.append(port)
            return SimpleNamespace(endpoint=f"{_EXECD_HOST}:{port}", headers={"X-Route": "r1"})

    handle = OpenSandboxHandle(_FakeSdk())  # type: ignore[arg-type]
    first = await handle.get_endpoint(8000)
    second = await handle.get_endpoint(8000)
    assert first is second
    assert calls == [8000]
    # Scheme comes from the deployment's lifecycle base URL (http here).
    assert first.endpoint == f"http://{_EXECD_HOST}:8000"
    assert first.headers == {"X-Route": "r1"}


@pytest.mark.parametrize("origin", [
    "https://isolated.test:9000",
    "http://gateway.test/sandboxes/sb-1/proxy/9000",
])
async def test_conversation_filesystem_uses_sdk_routing_and_access_headers(origin: str) -> None:
    from opensandbox.config import ConnectionConfig

    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"runId":"native-run"}\n')

    transport = httpx.MockTransport(receive)

    class _FakeSdk:
        id = "sb-1"
        connection_config = ConnectionConfig(transport=transport)

        async def get_endpoint(self, port: int) -> SimpleNamespace:
            assert port == 9000
            return SimpleNamespace(endpoint=origin, headers={"X-Route": "conversation"})

    handle = OpenSandboxHandle(_FakeSdk())  # type: ignore[arg-type]
    files = await handle.get_filesystem(9000, headers={"X-EXECD-ACCESS-TOKEN": "test-only"})
    try:
        assert await files.read_file("/workspace/native/status.json") == '{"runId":"native-run"}\n'
        request, = requests
        assert str(request.url).split("?")[0] == origin + "/files/download"
        assert request.url.params["path"] == "/workspace/native/status.json"
        assert request.headers["X-Route"] == "conversation"
        assert request.headers["X-EXECD-ACCESS-TOKEN"] == "test-only"
        assert files.connection_config.transport is transport
    finally:
        await transport.aclose()


def test_build_dataplane_three_branches() -> None:
    provider = OpenSandboxSandboxProvider()

    class _FakeSdk:
        id = "sb-1"

    handle = OpenSandboxHandle(_FakeSdk())  # type: ignore[arg-type]
    handle_plane = provider.build_dataplane(sandbox=handle, port=8000)
    assert isinstance(handle_plane, OpenSandboxDataPlane)

    endpoint_plane = provider.build_dataplane(endpoint="127.0.0.1:8000", port=8000)
    assert isinstance(endpoint_plane, OpenSandboxDataPlane)
    assert endpoint_plane._base_url == "http://127.0.0.1:8000"

    with pytest.raises(RuntimeError, match="cannot build a dataplane"):
        provider.build_dataplane()


async def test_dataplane_resolves_lazily_from_the_handle_and_caches() -> None:
    calls: list[int] = []

    class _FakeSdk:
        id = "sb-1"

        async def get_endpoint(self, port: int) -> SimpleNamespace:
            calls.append(port)
            return SimpleNamespace(endpoint=f"{_EXECD_HOST}:{port}", headers={"X-Route": "r1"})

    plane = OpenSandboxDataPlane(
        handle=OpenSandboxHandle(_FakeSdk()),  # type: ignore[arg-type]
        port=8000,
    )
    base_url, headers = await plane._resolve()
    base_url_again, _ = await plane._resolve()
    assert base_url == base_url_again == f"http://{_EXECD_HOST}:8000"
    assert headers == {"X-Route": "r1"}
    assert calls == [8000]


async def test_resolve_endpoint_returns_a_full_url() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    resolved = await _provider(fake).resolve_endpoint("sb-1", 8000)
    assert resolved == f"http://{_EXECD_HOST}:8000"


async def test_resolve_endpoint_with_required_headers_fails_loud() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.endpoint_headers[8000] = {"X-Sandbox-Route": "abc"}
    with pytest.raises(APIError) as err:
        await _provider(fake).resolve_endpoint("sb-1", 8000)
    # The endpoint-string face cannot carry routing headers; dropping them
    # silently would hand out an address that the server then rejects.
    assert err.value.code == "SANDBOX_ENDPOINT_HEADERS_UNSUPPORTED"


async def test_browser_endpoint_does_not_return_the_services_internal_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_ENDPOINT_VIA_SERVER_PROXY", "true")
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")

    resolved = await _provider(fake).resolve_browser_endpoint("sb-1", 5173)

    assert resolved is not None
    assert resolved.endpoint == f"http://{_EXECD_HOST}:5173"
    assert fake.endpoint_queries[-1]["use_server_proxy"] == ["false"]


async def test_resolve_browser_endpoint_uses_the_sdk_signed_url_operation() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    resolved = await _provider(fake).resolve_browser_endpoint("sb-1", 5173, expires_at=expires_at)

    assert resolved is not None
    assert resolved.signed is True
    assert resolved.expires_at == expires_at
    assert resolved.endpoint == (
        f"http://gateway.test/sb-1/5173/{int(expires_at.timestamp())}/signed"
    )
    assert fake.endpoint_queries[-1]["expires"] == [str(int(expires_at.timestamp()))]
    assert fake.endpoint_queries[-1]["use_server_proxy"] == ["false"]


# ── handle exec/file faces ───────────────────────────────────────────────────


class _FakeCommands:
    def __init__(self, execution: Any) -> None:
        self.execution = execution
        self.commands: list[str] = []

    async def run(self, command: str, **kwargs: Any) -> Any:
        self.commands.append(command)
        return self.execution


class _FakeFiles:
    def __init__(self) -> None:
        self.written: list[tuple[str, bytes]] = []

    async def write_file(self, path: str, data: Any) -> None:
        self.written.append((path, bytes(data)))


def _fake_sdk_sandbox(execution: Any) -> Any:
    return SimpleNamespace(id="sb-1", commands=_FakeCommands(execution), files=_FakeFiles())


def _execution(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    error: tuple[str, str] | None = None,
) -> Any:
    from opensandbox.models.execd import (
        Execution,
        ExecutionError,
        ExecutionLogs,
        OutputMessage,
    )

    logs = ExecutionLogs(
        stdout=[OutputMessage(text=stdout, timestamp=0, is_error=False)] if stdout else [],
        stderr=[OutputMessage(text=stderr, timestamp=0, is_error=True)] if stderr else [],
    )
    execution_error = ExecutionError(name=error[0], value=error[1], timestamp=0) if error else None
    return Execution(exit_code=exit_code, logs=logs, error=execution_error)


async def test_exec_collect_joins_argv_and_collects_streams() -> None:
    sdk = _fake_sdk_sandbox(_execution(exit_code=0, stdout="out", stderr="err"))
    handle = OpenSandboxHandle(sdk)
    code, out, err = await handle.exec_collect(["echo", "a b"])
    assert (code, out, err) == (0, b"out", b"err")
    assert sdk.commands.commands == ["echo 'a b'"]


async def test_exec_collect_infers_exit_code_from_the_error_value() -> None:
    sdk = _fake_sdk_sandbox(_execution(exit_code=None, error=("CommandFailed", "exit status 7")))
    code, _, _ = await OpenSandboxHandle(sdk).exec_collect(["false"])
    assert code == 7


async def test_exec_collect_nonnumeric_error_maps_to_one_with_the_text_on_stderr() -> None:
    sdk = _fake_sdk_sandbox(_execution(exit_code=None, error=("ExecdError", "session vanished")))
    code, _, err = await OpenSandboxHandle(sdk).exec_collect(["true"])
    assert code == 1
    assert b"session vanished" in err


async def test_exec_collect_max_output_keeps_the_tail() -> None:
    sdk = _fake_sdk_sandbox(_execution(exit_code=0, stdout="0123456789"))
    _, out, _ = await OpenSandboxHandle(sdk).exec_collect(["cat"], max_output=4)
    assert out == b"6789"


# ── execd line-event model → byte reassembly ────────────────────────────────
#
# The reassembly rule ``exec_collect`` applies is reverse-engineered from
# measured execd behavior, NOT from a published contract (execd documents no
# event-per-line semantics). These tests exist to (a) pin the five measured
# shapes, faithful and unfaithful alike, and (b) go RED if the upstream
# semantics ever move, instead of letting the file panel silently mis-split a
# response body from the status code appended after it.


def _events_execution(
    *,
    exit_code: int | None = 0,
    stdout: tuple[str, ...] = (),
    stderr: tuple[str, ...] = (),
) -> Any:
    """An ``Execution`` whose logs carry EXACTLY the given per-line events."""
    from opensandbox.models.execd import Execution, ExecutionLogs, OutputMessage

    return Execution(
        exit_code=exit_code,
        logs=ExecutionLogs(
            stdout=[OutputMessage(text=text, timestamp=0, is_error=False) for text in stdout],
            stderr=[OutputMessage(text=text, timestamp=0, is_error=True) for text in stderr],
        ),
        error=None,
    )


def test_the_execd_event_model_assumption_is_written_down_and_assertable() -> None:
    # The whole reassembly hinges on one undocumented judgement: an event whose
    # text is EXACTLY "\n" means "empty line". Both the judgement and the execd
    # version it was measured against are module constants precisely so this
    # test can hold them still.
    assert sandbox_module._EXECD_EMPTY_LINE_EVENT == "\n"
    assert EXECD_LINE_EVENT_MODEL_VERSION == "1.1.0"
    # Every row is (in-box command, events, reassembled bytes).
    for command, events, expected in EXECD_LINE_EVENT_MODEL:
        assert command.startswith("printf ")
        assert isinstance(events, tuple) and events
        assert isinstance(expected, bytes)
    # Pure-function agreement with the table, independent of the handle.
    for _command, events, expected in EXECD_LINE_EVENT_MODEL:
        messages = [SimpleNamespace(text=text) for text in events]
        assert collect_execd_stream(messages) == expected
    assert collect_execd_stream([]) == b""


@pytest.mark.parametrize(
    ("command", "events", "expected"),
    EXECD_LINE_EVENT_MODEL,
    ids=[row[0] for row in EXECD_LINE_EVENT_MODEL],
)
async def test_exec_collect_reassembles_every_measured_execd_shape(
    command: str, events: tuple[str, ...], expected: bytes
) -> None:
    """Each measured shape, through the real handle, on BOTH streams.

    Three rows are faithful. Two are not, and assert the LOSS on purpose:

    * ``printf 'only\\n'`` → ``b'only'``: execd emits no event for a trailing
      newline, so it cannot be recovered;
    * ``printf 'no-newline'`` → ``b'no-newline'``: identical reassembly to the
      row above, i.e. the two inputs are indistinguishable on this face;
    * ``printf 'a\\r\\nb\\n200'`` → ``b'a\\nb\\n200'``: a CR is consumed with the
      LF that follows it.

    These are inherent limits of a line-event output stream — execd's design
    choice, which this adapter adapts to. ``exec_collect`` is therefore for
    line-shaped in-box output only (readiness probes, log tails, stderr reads);
    a caller needing exact bytes uses the ``files`` API (``put_bytes`` /
    ``read_bytes``), and a caller needing an in-box HTTP surface addresses that
    surface's endpoint directly.
    """
    _ = command
    sdk = _fake_sdk_sandbox(_events_execution(stdout=events, stderr=events))
    code, out, err = await OpenSandboxHandle(sdk).exec_collect(["sh", "-c", "x"])
    assert (code, out, err) == (0, expected, expected)


async def test_exec_collect_keeps_every_interior_line_boundary() -> None:
    # Each execd event carries a line boundary. Joining without a separator
    # would produce b'hello200' and break line-oriented readers.
    sdk = _fake_sdk_sandbox(_events_execution(stdout=("hello", "200")))
    _code, out, _err = await OpenSandboxHandle(sdk).exec_collect(["sh", "-c", "x"])
    assert out == b"hello\n200"

    # …and the empty-line event is a boundary of its own rather than a literal
    # "\n" line, so a blank line in the output survives as a blank line.
    sdk = _fake_sdk_sandbox(_events_execution(stdout=("body", "\n", "200")))
    _code, out, _err = await OpenSandboxHandle(sdk).exec_collect(["sh", "-c", "x"])
    assert out == b"body\n\n200"


async def test_exec_collect_max_output_trims_after_reassembly() -> None:
    # The cap is a byte cap on the reassembled stream, so it can land mid-line;
    # it must not be applied per event (which would keep whole lines and blow
    # the bound).
    sdk = _fake_sdk_sandbox(_events_execution(stdout=("abcd", "efgh")))
    _code, out, _err = await OpenSandboxHandle(sdk).exec_collect(["cat"], max_output=6)
    assert out == b"d\nefgh"


async def test_put_bytes_writes_verbatim() -> None:
    sdk = _fake_sdk_sandbox(_execution(exit_code=0))
    await OpenSandboxHandle(sdk).put_bytes("/tmp/blob.bin", b"\x00\x01binary")
    assert sdk.files.written == [("/tmp/blob.bin", b"\x00\x01binary")]


# ── create_sandbox (the SandboxProvider provisioning seam) ───────────────────
#
# This is the platform's provider provisioning seam. The platform composes an
# engine declaration into ``SandboxCreateSpec`` and calls
# ``backend_adapter.create_sandbox(spec)``. Everything below runs the real SDK
# against the fake wire, so the assertions are about bytes on the lifecycle
# face, not about a mock's call record.


def _probe_error_sse(exit_code: int) -> str:
    """An execd error event whose value carries the process exit code.

    The command stream never carries ``exit_code`` itself; ``exec_collect``
    infers a non-zero code from the error payload's trailing integer (the
    behaviour ``test_exec_collect_infers_exit_code_from_the_error_value``
    pins). This is the same shape, produced on the wire.
    """
    return (
        'data: {"type":"error","timestamp":0,'
        f'"error":{{"ename":"exit","evalue":"{int(exit_code)}"}}}}\n\n'
        'data: {"type":"execution_complete","timestamp":0,"execution_time":1}\n\n'
    )


def _create_spec(**overrides: Any) -> Any:
    from astrabox.seams.sandbox import SandboxCreateSpec

    defaults: dict[str, Any] = {
        "session_id": "sess-assistant-1",
        "assignment_id": "assignment-assistant-1",
        "resource_limits": {"cpu": "4", "memory": "4Gi"},
        "resource_requests": {"cpu": "200m", "memory": "768Mi"},
        "image": "astrabox/sandbox-hermes:latest",
        "cwd": "/home/conversations",
        "env": {"ASTRABOX_HERMES_AUTOSTART": "false"},
        "entrypoint": ("/opt/gem/run.sh",),
    }
    defaults.update(overrides)
    return SandboxCreateSpec(**defaults)


async def test_create_sandbox_maps_every_spec_field_onto_the_create_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_SANDBOX_LEASE_SECONDS", "3600")
    fake = _FakeLifecycle()
    handle = await _provider(fake).create_sandbox(_create_spec())

    (body,) = fake.create_bodies
    assert body["image"]["uri"] == "astrabox/sandbox-hermes:latest"
    # The SDK swaps ["tail","-f","/dev/null"] in for ANY falsy entrypoint,
    # which would strip the AIO base image's real init.
    assert body["entrypoint"] == ["/opt/gem/run.sh"]
    # TTL is the deployment lease, never the SDK's 600 s default.
    assert body["timeout"] == 3600
    # env is the spec's, VERBATIM — the provider adds nothing of its own.
    assert body["env"] == {"ASTRABOX_HERMES_AUTOSTART": "false"}
    assert body["resourceLimits"] == {"cpu": "4", "memory": "4Gi"}
    assert body["resourceRequests"] == {"cpu": "200m", "memory": "768Mi"}
    # session_id rides as reverse-lookup metadata.
    assert body["metadata"]["astrabox.session-id"] == "sess-assistant-1"
    assert body["metadata"]["astrabox.assignment-id"] == (
        "assignment-assistant-1"
    )
    # cwd is pre-created inside the box, not passed to create.
    (dirs,) = fake.created_directories
    assert list(dirs) == ["/home/conversations"]
    assert handle.sandbox_id == "sb-created-1"


async def test_replaying_one_create_assignment_reattaches_instead_of_creating_again() -> None:
    fake = _FakeLifecycle()
    provider = _provider(fake)
    spec = _create_spec(cwd=None)

    first = await provider.create_sandbox(spec)
    await first.close()
    replay = await provider.create_sandbox(spec)

    assert first.sandbox_id == replay.sandbox_id == "sb-created-1"
    assert len(fake.create_bodies) == 1
    assert len(fake.list_queries) == 2
    for query in fake.list_queries:
        metadata = (query.get("metadata") or [""])[0]
        assert "astrabox.assignment-id=assignment-assistant-1" in metadata
        assert "astrabox.managed-by=astrabox" in metadata
    await replay.close()


async def test_pool_adoption_with_composite_identities_recovers_the_same_box() -> None:
    session = "agent-runtime:fa9a7c25-2d1a-489f-bd3a-810cb66b9eae"
    assignment = (
        "078925bd-40f0-42b9-8f1e-12b8058d91fa:"
        "a8ea7539-5307-451c-bd7a-a20f9eaa6969"
    )
    fake = _FakeLifecycle()
    provider = _provider(fake)
    pooled = await provider.create_sandbox(
        _create_spec(
            session_id="client-pool-before-claim",
            assignment_id="client-pool-assignment",
            cwd=None,
        )
    )
    await provider.adopt_sandbox_identity(
        pooled,
        session_id=session,
        assignment_id=assignment,
    )
    await pooled.close()
    spec = _create_spec(
        session_id=session,
        assignment_id=assignment,
        cwd=None,
    )

    replay = await provider.create_sandbox(spec)

    adopted_metadata = fake.sandboxes[replay.sandbox_id]["metadata"]
    wire_assignment = adopted_metadata[
        SANDBOX_ASSIGNMENT_ID_METADATA_KEY
    ]
    wire_session = adopted_metadata[
        SANDBOX_SESSION_ID_METADATA_KEY
    ]
    assert wire_assignment == assignment_metadata_value(assignment)
    assert wire_session == session_metadata_value(session)
    assert wire_assignment != assignment
    assert wire_session != session
    for wire_identity in (wire_assignment, wire_session):
        assert len(wire_identity) <= 63
        assert re.fullmatch(
            r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?",
            wire_identity,
        )
    assert pooled.sandbox_id == replay.sandbox_id == "sb-created-1"
    assert len(fake.create_bodies) == 1
    lookup = (fake.list_queries[-1].get("metadata") or [""])[0]
    assert f"{SANDBOX_ASSIGNMENT_ID_METADATA_KEY}={wire_assignment}" in lookup
    claim = await provider.claim_of(
        pooled.sandbox_id,
        expected_session_id=session,
    )
    assert claim.may_destroy is True
    assert claim.session_id == session
    await replay.close()


async def test_correlated_create_refuses_duplicate_assignment_stamps() -> None:
    fake = _FakeLifecycle()
    metadata = {
        "astrabox.session-id": "sess-assistant-1",
        "astrabox.managed-by": "astrabox",
        "astrabox.assignment-id": "assignment-assistant-1",
    }
    fake.add_sandbox("sb-duplicate-1", metadata=metadata)
    fake.add_sandbox("sb-duplicate-2", metadata=metadata)

    with pytest.raises(APIError) as caught:
        await _provider(fake).find_sandbox_by_assignment(
            "assignment-assistant-1"
        )

    assert caught.value.code == "SANDBOX_ASSIGNMENT_AMBIGUOUS"


async def test_correlated_create_refuses_an_assignment_owned_by_another_session() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox(
        "sb-other-session",
        metadata={
            "astrabox.session-id": "sess-other",
            "astrabox.managed-by": "astrabox",
            "astrabox.assignment-id": "assignment-assistant-1",
        },
    )

    with pytest.raises(APIError) as caught:
        await _provider(fake).create_sandbox(_create_spec(cwd=None))

    assert caught.value.code == "SANDBOX_ASSIGNMENT_CONFLICT"
    assert fake.create_bodies == []


async def test_correlated_create_removes_an_unusable_exact_resource_before_failing() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox(
        "sb-stopped-assignment",
        state="Terminated",
        metadata={
            "astrabox.session-id": "sess-assistant-1",
            "astrabox.managed-by": "astrabox",
            "astrabox.assignment-id": "assignment-assistant-1",
        },
    )

    with pytest.raises(APIError) as caught:
        await _provider(fake).create_sandbox(_create_spec(cwd=None))

    assert caught.value.code == "SANDBOX_GONE"
    assert fake.sandboxes == {}
    assert fake.create_bodies == [], (
        "one durable assignment never creates a replacement while its exact "
        "provider resource still exists"
    )


async def test_create_sandbox_falls_back_to_the_shared_image_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ASTRABOX_AGENT_IMAGE", "ASTRABOX_IMAGE_PREFIX", "ASTRABOX_IMAGE_TAG"):
        monkeypatch.delenv(name, raising=False)
    fake = _FakeLifecycle()
    await _provider(fake).create_sandbox(_create_spec(image=None))
    (body,) = fake.create_bodies
    # With nothing configured, the box runs this release's published image.
    assert body["image"]["uri"] == release_image(AGENT_IMAGE_COMPONENT)


async def test_advanced_permission_level_reaches_the_direct_create_and_is_proved() -> None:
    fake = _FakeLifecycle()

    handle = await _provider(fake).create_sandbox(
        _create_spec(permission_level="advanced")
    )

    assert fake.create_bodies[-1]["extensions"] == {
        "bootstrap.execd.isolation": "enable"
    }
    assert ("GET", "/v1/isolated/capabilities") in fake.requests
    await handle.close()


async def test_advanced_create_is_destroyed_when_the_running_box_cannot_isolate() -> None:
    fake = _FakeLifecycle()
    fake.isolation_available = False
    fake.isolation_message = "Creating new namespace failed: Operation not permitted"

    with pytest.raises(APIError) as caught:
        await _provider(fake).create_sandbox(
            _create_spec(permission_level="advanced")
        )

    assert caught.value.code == "SANDBOX_ISOLATION_UNSUPPORTED"
    assert "Operation not permitted" in caught.value.message
    assert fake.sandboxes == {}, "the post-create proof must not leak the rejected box"


async def test_privileged_direct_create_is_refused_before_the_api_call() -> None:
    fake = _FakeLifecycle()

    with pytest.raises(APIError) as caught:
        await _provider(fake).create_sandbox(
            _create_spec(permission_level="privileged")
        )

    assert caught.value.code == "UNSUPPORTED_SANDBOX_PERMISSION_LEVEL"
    assert fake.create_bodies == []


async def test_create_sandbox_can_start_the_credential_proxy_without_writing_a_vault() -> None:
    """A prepared box can create its security boundary before a workload exists.

    No credential may ride on this create; the real vault write occurs only
    after a workload adopts the prepared box.
    """

    fake = _FakeLifecycle()
    await _provider(fake).create_sandbox(
        _create_spec(credential_proxy_enabled=True, vault_write=None)
    )
    (body,) = fake.create_bodies
    assert body["credentialProxy"] == {"enabled": True}


async def test_create_sandbox_publishes_no_ports_and_wires_no_death_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two fields with no OpenSandbox counterpart. `publish_ports` is a
    # local-container concept (endpoints are resolved per port on demand) and
    # the create contract has no callback/webhook field at all, so neither may
    # invent a key on the wire — and neither may fail the create either.
    fake = _FakeLifecycle()
    handle = await _provider(fake).create_sandbox(
        _create_spec(
            publish_ports=(8642, 9119),
            death_callback_url="https://astrabox.test/api/v1/sandbox-callback/x/y/z/t",
        )
    )
    (body,) = fake.create_bodies
    serialized = json.dumps(body)
    assert "8642" not in serialized and "9119" not in serialized
    assert "sandbox-callback" not in serialized
    for absent in ("ports", "publishPorts", "callback", "callbackUrl", "webhook"):
        assert absent not in body
    assert handle.sandbox_id == "sb-created-1"


async def test_create_sandbox_returns_a_handle_that_reconnects_by_id() -> None:
    # The seam's durability requirement: the handle must outlive the provider
    # object that made it, and its id must be re-bindable by a LATER provider.
    fake = _FakeLifecycle()
    handle = await _provider(fake).create_sandbox(_create_spec())
    sandbox_id = handle.sandbox_id
    await handle.close()

    rebound = await _provider(fake).connect(sandbox_id)
    assert rebound.sandbox_id == sandbox_id
    endpoint = await rebound.get_endpoint(8642)
    assert endpoint.endpoint == f"http://{_EXECD_HOST}:8642"
    await rebound.close()


async def test_create_sandbox_without_a_readiness_port_runs_no_probe() -> None:
    # Hermes deliberately omits the gate so the handle is tracked before the
    # (slow, failure-prone) in-box wait; the provider must not add one.
    fake = _FakeLifecycle()
    await _provider(fake).create_sandbox(_create_spec())
    assert fake.commands == []


async def test_create_sandbox_readiness_gate_probes_the_requested_port() -> None:
    fake = _FakeLifecycle()
    handle = await _provider(fake).create_sandbox(
        # Both proofs, asked for separately: one says the platform can run a
        # command in here, the other that the image's own service is serving.
        _create_spec(wait_for_inbox_service_port=8642, requires_command_channel=True)
    )
    assert len(fake.commands) == 2
    assert fake.commands[0] == "sh -lc :"
    # The second execd round trip runs the port retry loop inside the box,
    # addressing the requested port on in-box loopback.
    assert "socket.create_connection" in fake.commands[1]
    assert "8642" in fake.commands[1]
    assert "127.0.0.1" in fake.commands[1]
    assert handle.sandbox_id == "sb-created-1"
    assert "sb-created-1" in fake.sandboxes


async def test_create_sandbox_readiness_timeout_kills_the_box_and_fails_loud() -> None:
    # Nothing else holds this sandbox's id — the caller never receives the
    # handle — so an un-killed box would run out its whole lease unreachable.
    fake = _FakeLifecycle()
    fake.command_sse_by_substring["socket.create_connection"] = _probe_error_sse(7)
    with pytest.raises(APIError) as excinfo:
        await _provider(fake).create_sandbox(_create_spec(wait_for_inbox_service_port=8642))
    assert excinfo.value.code == "AGENT_RUNTIME_ERROR"
    assert "8642" in str(excinfo.value.message)
    assert fake.sandboxes == {}
    assert ("DELETE", "/v1/sandboxes/sb-created-1") in fake.requests


async def test_create_sandbox_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Key hygiene on the create face: a server that echoes the API key back in
    # an error body must not get it into the exception the runtime logs.
    key = "osk-create-secret-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")
    fake = _FakeLifecycle()
    fake.error_message = f"denied for key {key}"

    def _reject_create(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sandboxes" and request.method == "POST":
            return httpx.Response(
                500, json={"code": "INTERNAL_ERROR", "message": fake.error_message}
            )
        return fake.handler(request)

    provider = OpenSandboxSandboxProvider(transport=httpx.MockTransport(_reject_create))
    with pytest.raises(RuntimeError) as excinfo:
        await provider.create_sandbox(_create_spec())
    assert key not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# ── provider boundary ────────────────────────────────────────────────────────


def test_provider_exposes_box_capabilities_not_agent_orchestration() -> None:
    provider = OpenSandboxSandboxProvider()

    assert "create_agent_executor" not in type(provider).__dict__
    assert type(provider).create_sandbox is not SandboxProvider.create_sandbox


def test_runtime_defaults_reflect_the_shared_image_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("ASTRABOX_AGENT_IMAGE", "ASTRABOX_IMAGE_PREFIX", "ASTRABOX_IMAGE_TAG"):
        monkeypatch.delenv(name, raising=False)
    defaults = OpenSandboxSandboxProvider().runtime_defaults()
    assert defaults.runtime_image == release_image(AGENT_IMAGE_COMPONENT)
    assert defaults.agent_command == "claude"


# ── _config ──────────────────────────────────────────────────────────────────


def _settings(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "sandbox_openapi_base_url": _BASE_URL,
        "sandbox_request_timeout_seconds": 15,
        "sandbox_api_key_secret_name": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_lifecycle_base_url_requires_an_explicit_scheme() -> None:
    with pytest.raises(APIError) as err:
        _config.lifecycle_base_url(_settings(sandbox_openapi_base_url="opensandbox.test:8080"))
    assert err.value.code == "SANDBOX_CONFIG_INVALID"
    assert err.value.status_code == 500
    with pytest.raises(APIError):
        _config.lifecycle_base_url(_settings(sandbox_openapi_base_url=""))
    assert (
        _config.lifecycle_base_url(_settings(sandbox_openapi_base_url=f"{_BASE_URL}/")) == _BASE_URL
    )


def test_endpoint_url_takes_the_scheme_from_the_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _config.endpoint_url("https://a.b:1/") == "https://a.b:1"
    assert _config.endpoint_url("h.example:9000") == "http://h.example:9000"
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", "https://opensandbox.test:8443")
    assert _config.endpoint_url("h.example:9000") == "https://h.example:9000"
    with pytest.raises(APIError):
        _config.endpoint_url("")


def test_endpoint_url_can_use_the_ingress_scheme_in_a_split_deployment() -> None:
    settings = _settings(sandbox_endpoint_scheme="https")
    assert (
        _config.endpoint_url("gateway.internal/sb/8080", settings=settings)
        == "https://gateway.internal/sb/8080"
    )


def test_sdk_connection_config_carries_the_registered_settings_faces() -> None:
    config = _config.sdk_connection_config(_settings(sandbox_request_timeout_seconds=7))
    assert config.domain == _BASE_URL
    assert config.api_key is None
    assert config.request_timeout == timedelta(seconds=7)
    assert config.transport is None


def test_sdk_connection_config_accepts_an_explicit_lifecycle_url() -> None:
    config = _config.sdk_connection_config(
        _settings(sandbox_openapi_base_url=""),
        lifecycle_base_url_override="https://opensandbox.test:8443/",
    )

    assert config.domain == "https://opensandbox.test:8443"


def test_sdk_connection_config_accepts_a_longer_timeout_for_a_synchronous_create() -> None:
    config = _config.sdk_connection_config(
        _settings(sandbox_request_timeout_seconds=15),
        request_timeout_seconds=120,
    )
    assert config.request_timeout == timedelta(seconds=120)


def test_create_request_timeout_covers_the_ready_budget() -> None:
    assert (
        _config.create_request_timeout_seconds(
            _settings(
                sandbox_request_timeout_seconds=15,
                sandbox_ready_timeout_seconds=120,
            )
        )
        == 120
    )
    assert (
        _config.create_request_timeout_seconds(
            _settings(
                sandbox_request_timeout_seconds=180,
                sandbox_ready_timeout_seconds=120,
            )
        )
        == 180
    )


# ── how this deployment reaches a sandbox ─────────────────────────────────────


def test_endpoints_are_reached_directly_by_default() -> None:
    # Direct is the default because it is right for the shape a developer runs:
    # AstraBox and the Docker daemon in one network namespace.
    assert _config.use_server_proxy(_settings()) is False
    assert _config.sdk_connection_config(_settings()).use_server_proxy is False


def test_server_proxy_reach_rides_on_the_connection_config() -> None:
    # It must live on the ConnectionConfig, not on a call: the SDK threads it
    # into every endpoint resolution it performs internally — including the
    # execd client behind commands/files — so one setting is what keeps the
    # lifecycle face and the in-box faces agreeing about how to reach a sandbox.
    settings = _settings(sandbox_endpoint_via_server_proxy=True)
    assert _config.use_server_proxy(settings) is True
    assert _config.sdk_connection_config(settings).use_server_proxy is True


def test_browser_endpoint_connection_can_override_the_internal_relay() -> None:
    settings = _settings(sandbox_endpoint_via_server_proxy=True)
    config = _config.sdk_connection_config(
        settings,
        use_server_proxy_override=False,
    )

    assert config.use_server_proxy is False


def test_server_proxy_reach_survives_a_settings_object_without_the_field() -> None:
    # Lifecycle-only callers hand in whatever settings face they hold; a missing
    # field means "direct", never an AttributeError mid-provision.
    assert _config.use_server_proxy(SimpleNamespace()) is False


async def test_a_server_proxied_endpoint_composes_into_both_in_box_faces() -> None:
    """The relay URL carries a PATH, and both faces must keep it.

    A directly-reached endpoint is a bare ``host:port``; a server-proxied one is
    ``server/sandboxes/<id>/proxy/<port>``. Every in-box URL is built by
    appending to that, so a face that assumed "authority only" would silently
    drop the relay path and dial the lifecycle server's own root instead of the
    sandbox.
    """
    relay = "127.0.0.1:8990/sandboxes/sb-1/proxy/8000"

    class _FakeSdk:
        id = "sb-1"

        async def get_endpoint(self, port: int) -> SimpleNamespace:
            return SimpleNamespace(endpoint=relay, headers={})

    # The data plane resolves through the handle and appends request paths to it.
    plane = OpenSandboxDataPlane(
        handle=OpenSandboxHandle(_FakeSdk()),  # type: ignore[arg-type]
        port=8000,
    )
    base_url, _ = await plane._resolve()
    assert base_url == f"http://{relay}"

    # The runner URI swaps the scheme and KEEPS the relay path — that path is
    # exactly what the relay forwards (websocket upgrade included) to the box.
    from astrabox.providers.open_sandbox.executor import _runner_ws_uri

    assert _runner_ws_uri(base_url, session_id="sess-1", sandbox_id="sb-1") == f"ws://{relay}"


def test_secret_material_is_empty_for_an_authless_deployment() -> None:
    assert OpenSandboxSandboxProvider().secret_material(settings=_settings()) == ""


# ── key hygiene ──────────────────────────────────────────────────────────────


async def test_api_key_never_appears_in_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.kill_status_override = 500
    # A misbehaving server echoing the auth header back is the leak vector the
    # scrub exists for: the SDK exception message carries the body verbatim.
    fake.error_message = f"denied for key {key}"
    with pytest.raises(APIError) as err:
        await _provider(fake).kill("sb-1")
    assert key not in str(err.value)
    assert key not in repr(err.value)
    assert "***" in str(err.value)


# ── execd host-side deadline ─────────────────────────────────────────────────


async def test_exec_collect_enforces_a_host_side_deadline() -> None:
    # The SDK's execd streaming client deliberately has no read timeout, so
    # without the host-side bound a non-terminating in-box command (or a
    # half-open host→box connection) would hang the ensure path forever.
    class _HangingCommands:
        async def run(self, command: str, **kwargs: Any) -> Any:
            await asyncio.sleep(3600)

    sdk = SimpleNamespace(id="sb-1", commands=_HangingCommands(), files=None)
    handle = OpenSandboxHandle(sdk)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError, match="did not complete within"):
        await handle.exec_collect(["sleep", "3600"], timeout=0.05)


def test_exec_collect_deadline_defaults_on() -> None:
    import inspect

    default = inspect.signature(OpenSandboxHandle.exec_collect).parameters["timeout"].default
    # The bound must be opt-OUT (timeout=None), never opt-in: every call site
    # that forgets about it still gets a deadline.
    assert isinstance(default, float)
    assert default > 0


# ── key hygiene beyond the provider lifecycle face ───────────────────────────


async def test_probe_generic_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")

    def _echoing_connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"proxy denied key {key}", request=request)

    # A transport-level failure lands in probe's GENERIC except branch (the
    # SDK wraps it in SandboxInternalException, a SIBLING of the API exception
    # type) — the scrub must hold on both branches of the same function.
    provider = OpenSandboxSandboxProvider(transport=httpx.MockTransport(_echoing_connect_error))
    probe = await provider.probe("sb-1")
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_FAILED
    assert probe.error_text is not None
    assert key not in probe.error_text
    assert "***" in probe.error_text


# ── control-plane inventory: list / describe / diagnostics ───────────────────


async def test_list_sandboxes_pages_at_the_source() -> None:
    fake = _FakeLifecycle()
    fake.list_page_size = 2
    for index in range(3):
        fake.add_sandbox(f"sb-{index}")
    provider = _provider(fake)

    first = await provider.list_sandboxes(page=1, page_size=2)

    # The SERVER's counters ride the page — nothing here re-derives them, so a
    # caller learns there is more without having to compare lengths itself.
    assert [item.sandbox_id for item in first.items] == ["sb-0", "sb-1"]
    assert (first.page, first.page_size, first.total_items) == (1, 2, 3)
    assert (first.total_pages, first.has_next_page) == (2, True)

    second = await provider.list_sandboxes(page=2, page_size=2)
    assert [item.sandbox_id for item in second.items] == ["sb-2"]
    assert second.has_next_page is False

    # The page really was asked for on the wire, not sliced host-side after
    # pulling everything.
    assert [q["page"][0] for q in fake.list_queries] == ["1", "2"]
    assert fake.list_queries[0]["pageSize"] == ["2"]


async def test_list_sandboxes_reports_the_session_only_when_the_box_carries_it() -> None:
    from astrabox.seams.sandbox import SANDBOX_SESSION_ID_METADATA_KEY

    fake = _FakeLifecycle()
    fake.list_page_size = 10
    fake.add_sandbox(
        "sb-ours",
        metadata={SANDBOX_SESSION_ID_METADATA_KEY: "sess-42"},
        image="astrabox/agent:1",
    )
    # A box this deployment did not create: present in the inventory (the
    # question is what the BACKEND runs), but with no session to attribute it
    # to. A guess here would be a wrong attribution, which is worse than none.
    fake.add_sandbox("sb-foreign")

    page = await _provider(fake).list_sandboxes(page=1, page_size=10)
    by_id = {item.sandbox_id: item for item in page.items}

    assert by_id["sb-ours"].session_id == "sess-42"
    assert by_id["sb-ours"].image == "astrabox/agent:1"
    assert by_id["sb-ours"].metadata[SANDBOX_SESSION_ID_METADATA_KEY] == "sess-42"
    assert by_id["sb-foreign"].session_id is None
    assert by_id["sb-foreign"].image is None
    assert by_id["sb-foreign"].entrypoint == ("/opt/gem/run.sh",)


async def test_create_writes_the_session_metadata_the_inventory_reads_back() -> None:
    # The write half of the association. If these two ever name different keys
    # the whole inventory silently reports session_id=None, so the round trip
    # is pinned rather than the literal string.
    from astrabox.seams.sandbox import SANDBOX_SESSION_ID_METADATA_KEY
    from astrabox.providers.open_sandbox.executor import create_open_sandbox_box

    fake = _FakeLifecycle()
    handle = await create_open_sandbox_box(
        session_id="sess-round-trip",
        assignment_id="assignment-round-trip",
        image="astrabox/agent:1",
        env={},
        resource_limits={"cpu": "4", "memory": "4Gi"},
        resource_requests={"cpu": "200m", "memory": "768Mi"},
        cwd=None,
        transport=fake.transport(),
    )
    await handle.close()

    metadata = fake.create_bodies[-1]["metadata"]
    assert metadata[SANDBOX_SESSION_ID_METADATA_KEY] == "sess-round-trip"


async def test_secure_access_setting_reaches_the_opensandbox_create_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.providers.open_sandbox.executor import create_open_sandbox_box

    monkeypatch.setenv("ASTRABOX_SANDBOX_SECURE_ACCESS", "true")
    fake = _FakeLifecycle()
    handle = await create_open_sandbox_box(
        session_id="sess-secure",
        assignment_id="assignment-secure",
        image="astrabox/agent:1",
        env={},
        resource_limits={"cpu": "4", "memory": "4Gi"},
        resource_requests={"cpu": "200m", "memory": "768Mi"},
        cwd=None,
        transport=fake.transport(),
    )
    await handle.close()

    assert fake.create_bodies[-1]["secureAccess"] is True


async def test_describe_sandbox_maps_a_missing_box_to_not_found() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    provider = _provider(fake)

    assert (await provider.describe_sandbox("sb-1")).state == "Running"

    with pytest.raises(APIError) as err:
        await provider.describe_sandbox("sb-gone")
    assert err.value.code == "SANDBOX_NOT_FOUND"
    assert err.value.status_code == 404


async def test_diagnostics_passes_plain_text_through_untouched() -> None:
    report = "Pod Name: sb-1\nPhase: Running\n\nEvents:\n  Scheduled\n"
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.diagnostics["inspect"] = report

    result = await _provider(fake).read_diagnostics("sb-1", scope="inspect")

    # Verbatim: no parsing, no field extraction, no re-rendering. The report is
    # prose the server wrote for a human, and nothing here pretends otherwise.
    assert result.text == report
    assert result.truncated is False
    assert result.scope == "inspect"
    assert result.content_type.startswith("text/plain")
    assert ("GET", "/v1/sandboxes/sb-1/diagnostics/inspect") in fake.requests


async def test_diagnostics_relays_a_server_refusal_instead_of_an_empty_report() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    # No entry for "summary" → the server answers 501, as a deployment whose
    # runtime cannot produce that report does.
    with pytest.raises(APIError) as err:
        await _provider(fake).read_diagnostics("sb-1", scope="summary")

    assert err.value.code == "SANDBOX_DIAGNOSTICS_NOT_IMPLEMENTED"
    assert err.value.status_code == 501
    # The reason has to reach the operator: "cannot be had, and here is who
    # said so" is the whole difference between this and returning "".
    assert _BASE_URL in err.value.message
    assert "not implemented" in err.value.message.lower()


async def test_diagnostics_missing_sandbox_is_not_found_not_a_gateway_error() -> None:
    fake = _FakeLifecycle()
    with pytest.raises(APIError) as err:
        await _provider(fake).read_diagnostics("sb-gone", scope="logs")
    assert err.value.code == "SANDBOX_NOT_FOUND"
    assert err.value.status_code == 404


async def test_diagnostics_rejects_an_unknown_scope_before_the_wire() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    with pytest.raises(APIError) as err:
        await _provider(fake).read_diagnostics("sb-1", scope="../../secrets")
    assert err.value.status_code == 400
    assert err.value.code == "SANDBOX_DIAGNOSTIC_SCOPE_INVALID"
    # Nothing reached the server: the scope is a path segment, so an
    # unvalidated one would be a path-traversal handed straight to the URL.
    assert fake.requests == []


async def test_diagnostics_truncation_keeps_the_end_that_matters() -> None:
    cap = sandbox_module._DIAGNOSTICS_MAX_CHARS
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    # A log is append-only, so the NEWEST lines are the ones being looked for.
    fake.diagnostics["logs"] = "H" + ("x" * cap) + "T"
    logs = await _provider(fake).read_diagnostics("sb-1", scope="logs")
    assert logs.truncated is True
    assert len(logs.text) == cap
    assert logs.text.endswith("T")

    # A rendered report reads top-down, so it keeps its head instead.
    fake.diagnostics["summary"] = "H" + ("x" * cap) + "T"
    summary = await _provider(fake).read_diagnostics("sb-1", scope="summary")
    assert summary.truncated is True
    assert len(summary.text) == cap
    assert summary.text.startswith("H")


async def test_diagnostics_stops_reading_a_head_scope_once_the_cap_is_full() -> None:
    """The cap has to bind on the way IN, or it is not a memory bound at all.

    A ``logs`` report is an unbounded container log; the whole reason for a cap
    is that one operator page-load must not be able to pull a multi-gigabyte body
    through the API process. Reading the response and THEN slicing it would have
    already spent exactly what the cap refuses. This drives a streamed body whose
    chunks are counted: a head-kept scope must stop pulling once it has enough.
    """
    cap = sandbox_module._DIAGNOSTICS_MAX_CHARS
    pulled = 0

    async def chunks() -> Any:
        nonlocal pulled
        # Far more body than the cap, in cap-sized pieces.
        for _ in range(50):
            pulled += 1
            yield b"x" * cap

    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.diagnostics_stream = ("inspect", chunks)
    report = await _provider(fake).read_diagnostics("sb-1", scope="inspect")

    assert report.truncated is True
    assert len(report.text) == cap
    assert pulled <= 2, (
        "a head-kept report must stop reading once it is full; pulling the whole "
        "body first is the memory cost the cap exists to refuse"
    )


async def test_diagnostics_keeps_a_tail_line_aligned_so_a_fragment_is_not_prose() -> None:
    """A tail cut mid-line must not hand the tail of a secret to the redactor.

    The redaction pass reads line by line, so a leading fragment sliced out of
    the middle of a redacted value would arrive with its ``name=`` half missing
    and read as ordinary prose. Re-aligning the kept region to its first newline
    is what keeps truncate-then-redact safe.
    """
    cap = sandbox_module._DIAGNOSTICS_MAX_CHARS
    secret = "capability-token-that-must-not-survive"
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")
    fake.diagnostics["logs"] = ("filler line\n" * cap) + f"SOME_CREDENTIAL={secret}\n"
    report = await _provider(fake).read_diagnostics("sb-1", scope="logs")

    assert report.truncated is True
    assert report.text.startswith("filler line\n"), (
        "the partial first line the cut produced is dropped, so what is left "
        "starts on a line boundary"
    )
    assert secret not in report.text, (
        "the assignment survived the cut whole, so the redactor could recognise and mask it"
    )
    assert report.text.endswith("SOME_CREDENTIAL=***\n")


async def test_diagnostics_failure_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "osk-secret-key-1234567890"
    monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", key)
    monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")

    def _echoing(request: httpx.Request) -> httpx.Response:
        # This face carries the API key header, so a server echoing the request
        # back lands the key in the body that gets quoted into the error message.
        return httpx.Response(500, text=f"denied for key {key}")

    provider = OpenSandboxSandboxProvider(transport=httpx.MockTransport(_echoing))
    with pytest.raises(APIError) as err:
        await provider.read_diagnostics("sb-1", scope="events")
    assert key not in str(err.value)
    assert "***" in err.value.message


async def test_the_seam_default_refuses_loud_rather_than_answering_empty() -> None:
    # A backend that never overrides the inventory face must be legible as
    # "cannot answer", never as "nothing is running" — an empty list here would
    # tell an operator their sandboxes are gone.
    class _LifecycleOnly(SandboxProvider):
        name = "lifecycle_only"

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

    provider = _LifecycleOnly()
    for call in (
        provider.list_sandboxes(),
        provider.describe_sandbox("sb-1"),
        provider.read_diagnostics("sb-1", scope="summary"),
    ):
        with pytest.raises(APIError) as err:
            await call
        assert err.value.status_code == 501
        assert "lifecycle_only" in err.value.message


class _SettleProbes:
    """A provider stand-in whose probe replays a scripted state sequence."""

    def __init__(self, states: list[str]) -> None:
        self.states = list(states)
        self.calls = 0

    async def probe(self, sandbox_id: str):
        self.calls += 1
        state = self.states[min(self.calls - 1, len(self.states) - 1)]
        return SandboxLifecycleProbeResult(
            probe_status=SANDBOX_LIFECYCLE_PROBE_OK, sandbox_state=state
        )


async def test_settle_stops_when_the_control_plane_says_the_transition_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FAILED is the control plane's own verdict on this pause, not a step
    # towards PAUSED. Without the guard this keeps polling to the end of the
    # settle budget — in front of an HTTP caller — to return the same answer,
    # so the discriminator is how many probes it took, not the verdict.
    monkeypatch.setattr(sandbox_module, "_PAUSE_SETTLE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(sandbox_module, "_PAUSE_SETTLE_POLL_SECONDS", 0.0)
    probes = _SettleProbes(["PAUSING", "FAILED"])

    settled = await sandbox_module.OpenSandboxSandboxProvider._settles_on(
        probes, "sbx-1", wanted=sandbox_module._PAUSED_STATES, operation="pause"
    )

    assert settled is False
    assert probes.calls == 2, "the verdict must be taken on the probe that reported it"


async def test_settle_keeps_waiting_through_a_state_that_can_still_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The guard must not turn every non-target state into a giving-up verdict:
    # PAUSING is exactly the state a pause passes through.
    monkeypatch.setattr(sandbox_module, "_PAUSE_SETTLE_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(sandbox_module, "_PAUSE_SETTLE_POLL_SECONDS", 0.0)
    probes = _SettleProbes(["PAUSING", "PAUSING", "PAUSED"])

    settled = await sandbox_module.OpenSandboxSandboxProvider._settles_on(
        probes, "sbx-2", wanted=sandbox_module._PAUSED_STATES, operation="pause"
    )

    assert settled is True
    assert probes.calls == 3
