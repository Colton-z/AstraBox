"""OpenSandbox sandbox-provider unit tests (no live server).

Everything runs against an ``httpx.MockTransport`` injected through the
provider constructors' test-only ``transport`` parameter (the signature
injection point ``_config.sdk_connection_config`` documents) — never a
monkeypatch of the SDK. The fake speaks the exact lifecycle/execd wire the SDK
speaks: lifecycle JSON under ``/v1``, execd SSE for command runs, and the
``/files/download`` byte face.

Pinned here:

* a box recorded with other network wiring is refused at connect;
* browser endpoints never hand out the service's internal relay or the sandbox
  control port;
* an assignment owned by another session is refused, and a privileged or
  non-isolating create is refused or destroyed;
* a created box publishes no ports, and Secure Access reaches the create;
* key hygiene: the API key and secrets never appear in exception or
  diagnostics text.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

import astrabox.providers.open_sandbox.sandbox as sandbox_module
from astrabox.common.utils.errors import APIError
from astrabox.providers.open_sandbox.sandbox import (
    OpenSandboxSandboxProvider,
)
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_FAILED,
    sandbox_installation_id,
)

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


# ── connect / handle / dataplane / endpoint ──────────────────────────────────


async def test_connect_refuses_a_box_recorded_with_other_network_wiring() -> None:
    # A box whose recorded DNS upstream, deny list or callback base is not the
    # deployment's cannot reach the model or the platform; it is gone to every
    # caller, which is what arms the same-send re-borrow with history.
    from astrabox.common.utils.settings import load_astrabox_settings
    from astrabox.providers.open_sandbox.networking import (
        SANDBOX_NETWORK_WIRING_METADATA_KEY,
        sandbox_network_wiring,
    )

    fake = _FakeLifecycle()
    fake.add_sandbox(
        "sb-stale", metadata={SANDBOX_NETWORK_WIRING_METADATA_KEY: "0" * 32}
    )
    fake.add_sandbox(
        "sb-current",
        metadata={
            SANDBOX_NETWORK_WIRING_METADATA_KEY: sandbox_network_wiring(
                load_astrabox_settings()
            )
        },
    )
    provider = _provider(fake)
    with pytest.raises(APIError) as err:
        await provider.connect("sb-stale")
    assert err.value.code == "SANDBOX_GONE"
    assert "network wiring" in err.value.message
    assert [p for _, p in fake.requests if "/endpoints/" in p] == []
    handle = await provider.connect("sb-current")
    await handle.close()


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


async def test_browser_endpoint_refuses_the_sandbox_control_port() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox("sb-1")

    with pytest.raises(APIError) as caught:
        await _provider(fake).resolve_browser_endpoint("sb-1", 8080)

    assert caught.value.code == "EXPOSE_PORT_RESERVED"
    assert caught.value.status_code == 400
    assert fake.endpoint_queries == []


# ── create_sandbox (the SandboxProvider provisioning seam) ───────────────────
#
# This is the platform's provider provisioning seam. The platform composes an
# engine declaration into ``SandboxCreateSpec`` and calls
# ``backend_adapter.create_sandbox(spec)``. Everything below runs the real SDK
# against the fake wire, so the assertions are about bytes on the lifecycle
# face, not about a mock's call record.


def _create_spec(**overrides: Any) -> Any:
    from astrabox.seams.sandbox import SandboxCreateSpec

    defaults: dict[str, Any] = {
        "execution_target": "astrabox",
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


async def test_correlated_create_refuses_an_assignment_owned_by_another_session() -> None:
    fake = _FakeLifecycle()
    fake.add_sandbox(
        "sb-other-session",
        metadata={
            "astrabox.session-id": "sess-other",
            "astrabox.managed-by": "astrabox",
            "astrabox.assignment-id": "assignment-assistant-1",
            "astrabox.installation": await sandbox_installation_id(),
        },
    )

    with pytest.raises(APIError) as caught:
        await _provider(fake).create_sandbox(_create_spec(cwd=None))

    assert caught.value.code == "SANDBOX_ASSIGNMENT_CONFLICT"
    assert fake.create_bodies == []


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


async def test_secure_access_setting_reaches_the_opensandbox_create_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrabox.providers.open_sandbox.executor import create_open_sandbox_box

    monkeypatch.setenv("ASTRABOX_SANDBOX_SECURE_ACCESS", "true")
    fake = _FakeLifecycle()
    handle = await create_open_sandbox_box(
        execution_target="astrabox",
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
