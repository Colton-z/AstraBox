"""Gated LIVE conformance for the ``open_sandbox`` backend (real server).

The structural conformance suites deliberately exclude live lifecycle; this
lane covers exactly that gap against a real opensandbox-server, exercising
the deployment contract ``docs/providers/opensandbox.md`` documents:

* the explicit ``entrypoint`` override survives create (the SDK would swap in
  ``tail -f /dev/null`` otherwise);
* execd's LINE EVENT MODEL, re-measured: the reassembly the file panel's status
  split depends on is reverse-engineered, so the table in
  ``providers/open_sandbox/sandbox.py`` is re-derived here against the real
  server (``-k execd_line_event_model`` runs just that);
* host→box TCP reach: execd round trip, the in-box runner's ``/health`` over
  the dataplane, and the runner wire itself — including the negative pin that
  an invalid opening op is refused loudly (error frame + close 1002): the
  health and protocol checks prove that the listener is the AstraBox runner,
  not an unrelated process absorbing frames;
* renew extends monotonically and ``expires_at`` moves forward;
* kill is immediate and idempotent (no tombstone: probe flips to NOT_FOUND,
  a second kill still returns True);
* the ``astrabox.session-id`` create metadata reverse-lookup filter works.

Deselected by default (``-m 'not … and not opensandbox'``). Point it at a
server with ``ASTRABOX_OPENSANDBOX_LIVE_BASE_URL`` (or the deployment's own
``ASTRABOX_SANDBOX_OPENAPI_BASE_URL``) — unset, every test SKIPS:

    ASTRABOX_OPENSANDBOX_LIVE_BASE_URL=http://127.0.0.1:8080 \
        pytest -m opensandbox    # or: make test-opensandbox

Optional: ``ASTRABOX_OPENSANDBOX_LIVE_API_KEY`` for an authed server. The
agent image (``astrabox/sandbox-claude-code:latest`` or your
``$ASTRABOX_AGENT_IMAGE``) must be pullable by the server.
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from datetime import datetime, timedelta

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.providers.open_sandbox import _config
from astrabox.providers.open_sandbox.sandbox import (
    EXECD_LINE_EVENT_MODEL,
    OpenSandboxHandle,
    OpenSandboxSandboxProvider,
)
from astrabox.providers.sandbox_image import (
    AIO_IMAGE_ENTRYPOINT,
    IN_BOX_SIDECAR_PORT,
    resolve_agent_image,
)
from astrabox.seams.sandbox import (
    SANDBOX_LIFECYCLE_PROBE_NOT_FOUND,
    SANDBOX_LIFECYCLE_PROBE_OK,
)

_LIVE_BASE_URL = (
    os.environ.get("ASTRABOX_OPENSANDBOX_LIVE_BASE_URL")
    or os.environ.get("ASTRABOX_SANDBOX_OPENAPI_BASE_URL")
    or ""
).strip()

pytestmark = [
    pytest.mark.opensandbox,
    pytest.mark.skipif(
        not _LIVE_BASE_URL,
        reason=(
            "live opensandbox-server not configured "
            "(set ASTRABOX_OPENSANDBOX_LIVE_BASE_URL)"
        ),
    ),
]


@pytest.fixture(autouse=True)
def _live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the provider's per-op settings resolution at the live server."""
    monkeypatch.setenv("ASTRABOX_SANDBOX_OPENAPI_BASE_URL", _LIVE_BASE_URL)
    api_key = str(os.environ.get("ASTRABOX_OPENSANDBOX_LIVE_API_KEY") or "").strip()
    if api_key:
        monkeypatch.setenv("ASTRABOX_SANDBOX_API_KEY", api_key)
        monkeypatch.setenv("ASTRABOX_ALLOW_PLAINTEXT_SANDBOX_API_KEY", "1")


# Short create TTL: long enough for the journey, short enough that a crashed
# run's orphan dies quickly; the renew step then extends past it.
_CREATE_TTL_SECONDS = 600
_RENEW_TTL_SECONDS = 3600

_HEALTH_PROBE = (
    "import urllib.request,sys\n"
    "try:\n"
    f"    r=urllib.request.urlopen('http://localhost:{IN_BOX_SIDECAR_PORT}/health',timeout=2)\n"
    "    sys.exit(0 if (getattr(r,'status',r.getcode())==200 and r.read()==b'OK') else 1)\n"
    "except Exception:\n"
    "    sys.exit(1)\n"
)


async def _await_inbox_server_ready(handle: OpenSandboxHandle) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        code, _out, _err = await handle.exec_collect(["python3", "-c", _HEALTH_PROBE])
        if code == 0:
            return
        await asyncio.sleep(0.5)
    raise AssertionError("in-box control server never became healthy")


async def test_live_execd_line_event_model_still_holds() -> None:
    """Re-measure :data:`EXECD_LINE_EVENT_MODEL` against the real execd.

    The reassembly rule ``exec_collect`` applies is reverse-engineered, not a
    published execd contract, and the in-box readers that still ride it depend
    on it: the executor's readiness and log probes and the identity
    provisioner's stderr read all parse line-shaped output. This walks the
    table's five measured shapes — including the two whose loss is inherent to
    a line-event stream — so a change in execd's semantics fails HERE rather
    than silently mis-reading those.

    Run it alone against a live server (see the module docstring for env)::

        ASTRABOX_OPENSANDBOX_LIVE_BASE_URL=http://127.0.0.1:8080 \\
            pytest -m opensandbox -k execd_line_event_model -q
    """
    from opensandbox import Sandbox

    settings = load_astrabox_settings()
    sdk_sandbox = await Sandbox.create(
        image=resolve_agent_image(),
        entrypoint=list(AIO_IMAGE_ENTRYPOINT),
        env={"IS_SANDBOX": "1", "DISABLE_BROWSER": "true"},
        timeout=timedelta(seconds=_CREATE_TTL_SECONDS),
        ready_timeout=timedelta(seconds=int(settings.sandbox_ready_timeout_seconds)),
        metadata={
            "astrabox.session-id": f"live-execd-{uuid.uuid4()}",
            "astrabox.managed-by": "astrabox",
        },
        connection_config=_config.sdk_connection_config(settings),
    )
    sandbox_id = str(sdk_sandbox.id)
    handle = OpenSandboxHandle(sdk_sandbox)
    provider = OpenSandboxSandboxProvider()
    try:
        measured: list[tuple[str, tuple[str, ...], bytes]] = []
        for command, _expected_events, expected_bytes in EXECD_LINE_EVENT_MODEL:
            execution = await handle.commands.run(f"sh -lc {shlex.quote(command)}")
            events = tuple(str(msg.text) for msg in execution.logs.stdout)
            code, out, _err = await handle.exec_collect(["sh", "-lc", command])
            measured.append((command, events, out))
            assert code == 0, f"{command}: exit={code}"
            assert out == expected_bytes, (
                f"{command}: reassembled {out!r}, table says {expected_bytes!r} "
                f"(execd events: {events!r})"
            )
        # Stated as itself: an interior line boundary survives the round trip.
        blocker = dict((row[0], row[2]) for row in measured)
        assert blocker[r"printf 'hello\n200'"] == b"hello\n200"
        # The event shapes the table claims, re-measured on THIS server.
        for command, events, _out in measured:
            table_events = next(
                row[1] for row in EXECD_LINE_EVENT_MODEL if row[0] == command
            )
            assert events == table_events, (
                f"{command}: execd emitted {events!r}, the table recorded "
                f"{table_events!r} — the event model changed; re-derive the "
                "reassembly rule in providers/open_sandbox/sandbox.py before "
                "trusting anything that reads exec_collect output"
            )
    finally:
        await handle.close()
        assert await provider.kill(sandbox_id) is True


async def test_live_full_lifecycle_journey() -> None:
    from opensandbox import Sandbox, SandboxManager
    from opensandbox.models.execd import RunCommandOpts
    from opensandbox.models.sandboxes import SandboxFilter

    provider = OpenSandboxSandboxProvider()
    settings = load_astrabox_settings()
    connection_config = _config.sdk_connection_config(settings)
    session_id = f"live-{uuid.uuid4()}"

    sdk_sandbox = await Sandbox.create(
        image=resolve_agent_image(),
        entrypoint=list(AIO_IMAGE_ENTRYPOINT),
        env={"IS_SANDBOX": "1", "DISABLE_BROWSER": "true"},
        timeout=timedelta(seconds=_CREATE_TTL_SECONDS),
        ready_timeout=timedelta(seconds=int(settings.sandbox_ready_timeout_seconds)),
        metadata={
            "astrabox.session-id": session_id,
            "astrabox.managed-by": "astrabox",
        },
        connection_config=connection_config,
    )
    sandbox_id = str(sdk_sandbox.id)
    await sdk_sandbox.close()
    try:
        # -- create held the explicit entrypoint (the SDK's tail -f default
        #    would have replaced a falsy one) + metadata reverse lookup.
        manager = await SandboxManager.create(connection_config=connection_config)
        try:
            info = await manager.get_sandbox_info(sandbox_id)
            assert list(info.entrypoint or []) == list(AIO_IMAGE_ENTRYPOINT)
            listed = await manager.list_sandbox_infos(
                SandboxFilter(metadata={"astrabox.session-id": session_id})
            )
            assert [row.id for row in listed.sandbox_infos] == [sandbox_id]
        finally:
            await manager.close()

        # -- probe: control-plane state maps to OK("running").
        probe = await provider.probe(sandbox_id)
        assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_OK
        assert probe.sandbox_state == "running"

        # -- by-id connect + execd round trip (the handle's text contract).
        handle = await provider.connect(sandbox_id)
        try:
            code, out, _err = await handle.exec_collect(
                ["sh", "-lc", "echo live-roundtrip"]
            )
            assert code == 0
            assert b"live-roundtrip" in out

            # -- the IMAGE boots the runner (boot.sh entrypoint held above),
            #    so the host launches nothing here: in-box /health…
            await _await_inbox_server_ready(handle)

            # …and host→box TCP: the SAME /health over the dataplane (the
            # documented deployment prerequisite, exercised end to end).
            plane = provider.build_dataplane(sandbox=handle)
            response = await plane.request("GET", "/health")
            assert (response.status_code, response.text) == (200, "OK")

            # -- runner wire: the thing on the port speaks the envelope
            #    protocol. An invalid opening op is refused loudly — error
            #    frame naming the violation, then close 1002 — never absorbed.
            endpoint = await handle.get_endpoint(IN_BOX_SIDECAR_PORT)
            ws_url = endpoint.endpoint.replace("http://", "ws://", 1).replace(
                "https://", "wss://", 1
            )
            async with ws_connect(ws_url, max_size=None) as raw_ws:
                await raw_ws.send(
                    json.dumps({"op": "input", "session_id": session_id})
                )
                reply = json.loads(await raw_ws.recv())
                assert reply.get("op") == "error"
                with pytest.raises(ConnectionClosed):
                    await raw_ws.recv()
                assert raw_ws.close_code == 1002
        finally:
            await handle.close()

        # -- renew extends, expires_at moves forward, monotonically.
        before = await provider.expires_at(sandbox_id)
        assert isinstance(before, datetime)
        renewed = await provider.renew(sandbox_id, ttl_seconds=_RENEW_TTL_SECONDS)
        assert isinstance(renewed, datetime)
        assert renewed > before
        after = await provider.expires_at(sandbox_id)
        assert isinstance(after, datetime)
        assert after > before

        # -- resolve_endpoint: the string face hands out a full URL.
        resolved = await provider.resolve_endpoint(sandbox_id, IN_BOX_SIDECAR_PORT)
        assert isinstance(resolved, str)
        assert resolved.startswith(("http://", "https://"))
    finally:
        killed = await provider.kill(sandbox_id)
        assert killed is True

    # -- no tombstone: gone means 404, and kill is idempotent on it.
    probe = await provider.probe(sandbox_id)
    assert probe.probe_status == SANDBOX_LIFECYCLE_PROBE_NOT_FOUND
    assert await provider.kill(sandbox_id) is True
