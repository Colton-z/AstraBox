"""The host's links into a box carry a credential only the deployment can make.

Two in-box listeners answer any peer that can route to the box: the Claude
runner and the Codex forwarder. On Kubernetes that is any pod, because a
sandbox Pod has no ingress policy; under the shared tenancy it is every sibling
conversation. Each now demands a credential derived from the deployment secret
and the seat's identity, and compares it with a file the platform writes into
the seat's home. These tests hold four things: the value every opening
presents is recomputed rather than stored; it is keyed to the secret, so
knowing every platform id is not enough; the file is written before the first
prepare wherever a seat is set up, and nowhere else; and the Codex forwarder
relays nothing until the credential matches.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.engine import claude_code_runtime, codex, runner_link
from astrabox.core.service.orchestrator.engine.base import (
    EnginePreparationContext,
    EngineStartupContext,
)
from astrabox.core.service.orchestrator.engine.codex_link import (
    CODEX_FORWARD_TOKEN_FILE_NAME,
    CODEX_FORWARD_TOKEN_HEADER,
)
from astrabox.core.service.orchestrator.engine.provisioning import in_box_service_token

_REPO = Path(__file__).resolve().parents[1]
_IDENTITY = {
    "linux_user": "agent",
    "home_dir": "/home/agent",
    "workspace_dir": "/workspace",
    "config_dir": "/home/agent/.claude",
    "sandbox_tenancy": "conversation",
}


# ── the derivation ────────────────────────────────────────────────────────


def test_the_token_is_keyed_to_the_deployment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-one")
    first = runner_link.runner_activation_token("box-1", _IDENTITY)
    assert first == runner_link.runner_activation_token("box-1", dict(_IDENTITY))

    monkeypatch.setenv("ASTRABOX_TRANSCRIPT_SIGNING_KEY", "deployment-two")
    assert runner_link.runner_activation_token("box-1", _IDENTITY) != first, (
        "a peer that knows the box and the account must still need the secret"
    )


def test_each_seat_and_each_service_gets_its_own_token() -> None:
    runner = runner_link.runner_activation_token("box-1", _IDENTITY)
    assert runner_link.runner_activation_token("box-2", _IDENTITY) != runner
    assert (
        runner_link.runner_activation_token(
            "box-1", {**_IDENTITY, "linux_user": "conv_b", "home_dir": "/home/conv_b"}
        )
        != runner
    )
    assert codex._forward_token("box-1", _IDENTITY) != runner


def test_a_seat_without_its_identity_gets_no_token() -> None:
    with pytest.raises(RuntimeError, match="account and home"):
        in_box_service_token(purpose="claude-runner", sandbox_id="box-1", runtime_identity={})


# ── the Claude runner: prepare, claim and reconnect present one value ───────


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agent-1",
        name="Claude Code",
        engine_options=None,
        system=None,
        plugin_repos=[],
        mcp_servers={},
        skills=[],
    )


def _model_access() -> SimpleNamespace:
    return SimpleNamespace(
        credential="model-key",
        credential_kind="api_key",
        base_url="http://gateway.internal",
        model_name="deepseek-flash",
        endpoint_provider="",
    )


def _settings() -> SimpleNamespace:
    from astrabox.common.utils.settings import DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES

    return SimpleNamespace(
        remote_agent_include_partial_messages=True,
        remote_agent_max_buffer_size=DEFAULT_REMOTE_AGENT_MAX_BUFFER_SIZE_BYTES,
        mcp_proxy_base_url="",
    )


def _startup(**overrides: Any) -> EngineStartupContext:
    values: dict[str, Any] = dict(
        session_id="session-1",
        template=_template(),
        workspace_plan=None,
        sandbox=object(),
        sandbox_id="box-1",
        cwd="/workspace",
        runtime_identity=dict(_IDENTITY),
        model_access=_model_access(),
        model_credential="model-key",
        resume_session_key=None,
        runner_uri="ws://runner.internal",
        permission_mode="default",
        deployment_settings=_settings(),
        capability_scope="conversation",
    )
    values.update(overrides)
    return EngineStartupContext(**values)


class _Files:
    def __init__(self, events: list[str] | None = None) -> None:
        self.writes: list[tuple[str, bytes, dict[str, Any]]] = []
        self._events = events

    async def write_file(self, path: str, body: bytes, **kwargs: Any) -> None:
        self.writes.append((path, body, kwargs))
        if self._events is not None:
            self._events.append(f"write:{path}")


class _Client:
    engine_session_key = None

    def start_resident_observation(self) -> None:
        pass

    async def close(self) -> None:
        pass


async def test_prepare_claim_cold_start_and_reattach_present_the_same_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One derived value, delivered only where a seat is set up, before its prepare.

    The runner refuses every prepare until the platform's file exists, so the
    file must be written before the first prepare on both paths that set a
    seat up: preparation, and a start that claimed nothing (whose configure
    prepares). A claim and a reattach present the value without writing.
    """
    presented: dict[str, str] = {}
    events: list[str] = []

    class _Link:
        def __init__(self, _uri: str, *, activation_token: str, **_kwargs: Any) -> None:
            presented["prepare"] = activation_token

        async def __aenter__(self) -> _Link:
            return self

        async def prepare(self, _slot_id: str, *, options: dict[str, Any]) -> None:
            events.append("prepare")

        async def close(self) -> None:
            pass

    def _capture(label: str, result: Any) -> Any:
        async def _seam(_runner_uri: str, **kwargs: Any) -> Any:
            presented[label] = kwargs["activation_token"]
            events.append(label)
            return result

        return _seam

    files = _Files(events)
    sandbox = SimpleNamespace(files=files)

    async def _initialize(*_args: Any, **_kwargs: Any) -> object:
        return object()

    monkeypatch.setattr(runner_link, "RunnerLink", _Link)
    monkeypatch.setattr(claude_code_runtime, "initialize_engine_client", _initialize)
    monkeypatch.setattr(
        claude_code_runtime, "_activate_runner_engine_client", _capture("claim", _Client())
    )
    monkeypatch.setattr(
        claude_code_runtime, "_connect_runner_engine_client", _capture("cold", _Client())
    )
    monkeypatch.setattr(
        claude_code_runtime,
        "_attach_runner_engine_client",
        _capture("reattach", (_Client(), "attached")),
    )

    await claude_code_runtime.prepare_runtime(
        EnginePreparationContext(
            template=_template(),
            slot_id="slot-1",
            placement="conversation_box",
            sandbox=sandbox,
            sandbox_id="box-1",
            cwd="/workspace",
            runtime_identity=dict(_IDENTITY),
            model_access=_model_access(),
            model_credential="slot-placeholder",
            runtime_env={},
            runner_uri="ws://runner.internal",
            preparation_fingerprint="fingerprint-1",
            deployment_settings=_settings(),
        )
    )
    await claude_code_runtime.activate_runtime(
        _startup(
            sandbox=sandbox,
            prepared_manifest={"slot_id": "slot-1", "activation_mcp_servers": []},
        )
    )
    await claude_code_runtime.activate_runtime(_startup(sandbox=sandbox))
    await claude_code_runtime.activate_runtime(_startup(sandbox=sandbox, attach_mode="reattach"))

    expected = runner_link.runner_activation_token("box-1", _IDENTITY)
    assert presented == {
        "prepare": expected,
        "claim": expected,
        "cold": expected,
        "reattach": expected,
    }
    token_path = f"/home/agent/{runner_link.RUNNER_TOKEN_FILE_NAME}"
    assert events == [
        f"write:{token_path}",
        "prepare",
        "claim",
        f"write:{token_path}",
        "cold",
        "reattach",
    ]
    assert {body for _path, body, _kwargs in files.writes} == {expected.encode("ascii")}
    assert all(
        kwargs == {"mode": 600, "owner": "agent", "group": "agent"}
        for _path, _body, kwargs in files.writes
    )


# ── the Codex forwarder ───────────────────────────────────────────────────


def _load_forwarder() -> Any:
    path = _REPO / "containers" / "sandbox-codex" / "astrabox-codex-forward"
    loader = importlib.machinery.SourceFileLoader("astrabox_codex_forward", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_UPGRADE = (
    b"GET / HTTP/1.1\r\nHost: box\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
    b"Sec-WebSocket-Extensions: permessage-deflate\r\n"
)


@pytest.mark.parametrize(
    ("token_file", "header", "admitted"),
    [
        (None, b"the-seat-token", False),
        (b"the-seat-token\n", None, False),
        (b"the-seat-token\n", b"a-guessed-token", False),
        (b"the-seat-token\n", b"the-seat-token", True),
    ],
    ids=["before-delivery", "no-credential", "wrong-credential", "the-platform"],
)
async def test_the_forwarder_relays_only_an_upgrade_carrying_the_credential(
    monkeypatch: pytest.MonkeyPatch,
    token_file: bytes | None,
    header: bytes | None,
    admitted: bool,
) -> None:
    forwarder = _load_forwarder()
    received: list[bytes] = []
    with tempfile.TemporaryDirectory() as scratch:
        socket_path = os.path.join(scratch, "app-server.sock")
        token_path = os.path.join(scratch, CODEX_FORWARD_TOKEN_FILE_NAME)
        if token_file is not None:
            Path(token_path).write_bytes(token_file)
        monkeypatch.setattr(forwarder, "SOCKET", socket_path)
        monkeypatch.setattr(forwarder, "TOKEN_FILE", token_path)

        async def _app_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n\r\n")
            await writer.drain()
            writer.close()

        upstream = await asyncio.start_unix_server(_app_server, path=socket_path)
        listener = await asyncio.start_server(forwarder._handle, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            head = _UPGRADE
            if header is not None:
                head += CODEX_FORWARD_TOKEN_HEADER.encode("ascii") + b": " + header + b"\r\n"
            writer.write(head + b"\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=5)
            writer.close()
        finally:
            listener.close()
            upstream.close()

    if admitted:
        assert b"101" in status
        assert len(received) == 1
        relayed = received[0].lower()
        assert b"x-astrabox-codex-token" not in relayed, "the server must never see the credential"
        assert b"sec-websocket-extensions" not in relayed
    else:
        assert b"403" in status
        assert received == [], "nothing reaches the app-server before the credential matches"


# ── the Codex adapter delivers and presents it ────────────────────────────


async def test_codex_writes_the_credential_where_its_seat_is_set_up_and_presents_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connects: list[str] = []

    async def _connect(sandbox: Any, *, forward_token: str, port: int = 44790) -> Any:
        connects.append(forward_token)
        return SimpleNamespace(server_info={}, close=_close)

    async def _close() -> None:
        pass

    monkeypatch.setattr(codex.CodexAppServerLink, "connect", staticmethod(_connect))
    files = _Files()
    template = SimpleNamespace(
        agent_id="agent-1",
        engine_kind="codex",
        engine_options=None,
        system=None,
        runtime_template_name="astrabox/sandbox-codex:latest",
    )
    await codex.CodexEngineAdapter().prepare_runtime(
        EnginePreparationContext(
            template=template,
            slot_id="slot-1",
            placement="conversation_box",
            sandbox=SimpleNamespace(files=files),
            sandbox_id="box-1",
            cwd="/workspace",
            runtime_identity=dict(_IDENTITY),
            model_access=SimpleNamespace(base_url="http://gateway.internal"),
            model_credential="slot-placeholder",
            runtime_env={},
            runner_uri=None,
            preparation_fingerprint="fingerprint-1",
            deployment_settings=_settings(),
        )
    )

    token = codex._forward_token("box-1", _IDENTITY)
    assert files.writes == [
        (
            f"/home/agent/{CODEX_FORWARD_TOKEN_FILE_NAME}",
            token.encode("ascii"),
            {"mode": 600, "owner": "agent", "group": "agent"},
        )
    ]
    assert connects == [token], "the probe must pass the same forwarder check a claim does"
