#!/usr/bin/env python3
"""Prove every engine a fresh installation offers can start and answer a turn.

    scripts/installer-engine-acceptance.py stub SERVER_IMAGE HOST_ADDRESS PORT
    scripts/installer-engine-acceptance.py turns BASE_URL MODEL [--engine NAME]...
        [--expect-text TEXT]
    scripts/installer-engine-acceptance.py judge SSE_FILE
    scripts/installer-engine-acceptance.py restart BASE_URL MODEL COMPOSE_DIR
        [--engine NAME]... [--cycles N] [--expect-text TEXT]

``turns`` does what a new user does for each engine: create an Environment for
it, then an Agent on it (an Assistant for Hermes, whose workspace is woken
first), and open a conversation. The Session must reach READY, one message must
stream back one complete reply that finishes normally, and the Session must
return to READY. Claude Code uses the Agent the installation seeds. Engines run
one at a time in the order given (all five by default), and every result is
printed before the exit status reports any failure.

``stub`` starts the stub OpenAI-compatible upstream from
``scripts/embedded-gateway-acceptance.py`` on the Docker host, published on
``HOST_ADDRESS:PORT``, and prints the base URL to answer the installer's
``ASTRABOX_INSTALL_MODEL_BASE_URL`` with. Use a host address the installed
server container can reach, such as the Docker bridge gateway. With that
upstream, ``--expect-text STUB-OK`` requires each reply to be the stub's text,
so the reply is known to have come through the engine from the model service.
The container is named ``astrabox-model-stub``; remove it with ``docker rm -f``.

``judge`` prints ``PASS <reply>`` or ``FAIL <reason>`` for a saved
``/ai-stream`` response; ``scripts/installer-acceptance.sh`` judges its turn
with it.

``restart`` opens one conversation per engine, restarts the installation in
COMPOSE_DIR with ``docker compose stop`` and ``start`` CYCLES times (3 by
default), and after each restart asks every conversation a question only its
first turn can answer: a codeword it was given, which the engine can recall
only from the conversation's own history. The first cycle, and every second
one after it, holds the two sandbox edges' bridge addresses with placeholder
containers while the stack is down, so Docker must give the edges new ones: the
case in which sandboxes created before the restart cannot reach the model or
the platform. Each question is sent once and must be answered: a conversation
whose box did not survive the restart is moved to a replacement within that
message, so a refused send fails the check. With ``--expect-text`` (a stub
upstream answers every question with the same text) each reply must carry that
text instead.

Used by ``.github/workflows/installer.yml`` and by maintainers verifying an
installation on a Docker host. Needs python3, and Docker for ``stub``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ENGINES = ("claude_code", "codex", "pi", "deepseek_harness", "hermes")
SEEDED_CLAUDE_CODE_AGENT = "Claude Code"
STUB_CONTAINER = "astrabox-model-stub"
READY_TIMEOUT_SECONDS = 900.0
WAKE_TIMEOUT_SECONDS = 900.0
TURN_TIMEOUT_SECONDS = 600.0
SETTLE_TIMEOUT_SECONDS = 180.0
#: A question a new user could ask, with a short answer. A message that asks
#: nothing leaves the model to reply to whatever else it was shown, such as an
#: engine's own runtime context, and the reply reads like a defect.
MESSAGE = "What is the capital of France? Answer in one sentence."

#: Codex looks its model up by slug in the vendor's models.json; an Agent on a
#: model the catalog lacks runs Codex's unknown-model behaviour, which against
#: a gateway answers one message twice (see the codex engine's model_catalog
#: option). The repository keeps one reviewed entry to copy the slug onto.
CODEX_CATALOG = Path(__file__).resolve().parent.parent / "tests/e2e-contract/codex-model-catalog.json"
CODEX_CATALOG_TEMPLATE_SLUG = "deepseek-flash"

# The Claude Code CLI reports a model or gateway HTTP error as text, not as an
# error frame; such text is a failed turn, never a reply.
MODEL_ERROR_MARKERS = (
    "api error",
    "authentication error",
    "invalid api key",
    "status code: 401",
)


class AcceptanceFailure(RuntimeError):
    """One engine did not get from a new conversation to a completed turn."""


# ── the turn verdict ──────────────────────────────────────────────────────


def _model_error(text: str) -> bool:
    folded = " ".join(text.lower().split())
    return any(marker in folded for marker in MODEL_ERROR_MARKERS)


def judge(stream: str) -> tuple[bool, str]:
    """Judge an ``/ai-stream`` response by its structure, not its wording.

    Returns ``(True, reply)`` when the stream carried at least one complete
    text block, no error, and a ``stop`` finish; otherwise ``(False, reason)``.
    """

    error = ""
    open_blocks: set[str] = set()
    completed: list[str] = []
    text: dict[str, list[str]] = {}
    finish = ""
    for line in stream.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        frame = json.loads(payload)
        kind = frame.get("type")
        if kind == "text-start":
            open_blocks.add(frame["id"])
            text[frame["id"]] = []
        elif kind == "text-delta" and frame.get("id") in open_blocks:
            text[frame["id"]].append(str(frame.get("delta") or ""))
        elif kind == "text-end" and frame.get("id") in open_blocks:
            open_blocks.discard(frame["id"])
            completed.append("".join(text[frame["id"]]))
        elif kind == "error":
            error = error or str(frame.get("errorText") or "error frame")
        elif kind == "data-result" and isinstance(frame.get("data"), dict):
            result = frame["data"]
            if result.get("is_error") or _model_error(str(result.get("result") or "")):
                error = error or str(result.get("result") or "is_error result")
        elif kind == "finish":
            finish = str(frame.get("finishReason") or "")

    reply = " ".join(block.strip() for block in completed if block.strip())
    if not error and _model_error(reply):
        error = reply
    if error:
        return False, "the turn failed: " + " ".join(error.split())[:300]
    if not reply:
        return False, "the turn produced no complete text block"
    if finish != "stop":
        return False, f"the stream finished with {finish or 'no finish frame'}"
    return True, reply


# ── the product API, as the console calls it ─────────────────────────────


class Api:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, path: str, body: Any = None, *, timeout: float = 120.0) -> Any:
        request = urllib.request.Request(
            self.base_url + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"content-type": "application/json", "accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise AcceptanceFailure(
                f"{method} {path} returned HTTP {error.code}: {' '.join(detail.split())[:400]}"
            ) from None
        except OSError as error:
            raise AcceptanceFailure(f"{method} {path} failed: {error}") from None
        return json.loads(raw)["data"] if raw else None

    def stream(self, path: str, body: Any) -> str:
        request = urllib.request.Request(
            self.base_url + path,
            method="POST",
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "accept": "text/event-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TURN_TIMEOUT_SECONDS) as response:
                return response.read().decode(errors="replace")
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise AcceptanceFailure(
                f"POST {path} returned HTTP {error.code}: {' '.join(detail.split())[:400]}"
            ) from None
        except OSError as error:
            raise AcceptanceFailure(f"POST {path} failed: {error}") from None

    def session_state(self, session_id: str) -> dict[str, Any]:
        return self.call("GET", f"/api/v1/sessions/{session_id}")


def _codex_engine_options(model: str) -> dict[str, Any]:
    catalog = json.loads(CODEX_CATALOG.read_text(encoding="utf-8"))
    template = next(m for m in catalog["models"] if m["slug"] == CODEX_CATALOG_TEMPLATE_SLUG)
    return {"model_catalog": {**catalog, "models": [{**template, "slug": model}]}}


def _owner(api: Api, engine: str, model: str, label: str) -> tuple[str, str]:
    """Create what a user creates for ``engine``; return ``(kind, id)``."""

    if engine == "claude_code":
        agents = [a for a in api.call("GET", "/api/v1/agents") if a.get("name") == SEEDED_CLAUDE_CODE_AGENT]
        if len(agents) != 1:
            raise AcceptanceFailure(f"the seeded {SEEDED_CLAUDE_CODE_AGENT} Agent is not listed exactly once")
        return "agent", str(agents[0]["agent_id"])
    environment = f"{label}-{engine.replace('_', '-')}"
    engine_kind = "assistant" if engine == "hermes" else engine
    api.call("PUT", f"/api/v1/admin/environments/{environment}", {"engine_kind": engine_kind, "enabled": True})
    if engine == "hermes":
        assistant = api.call("POST", "/api/v1/assistants", {
            "display_name": f"{label} hermes",
            "environment_name": environment,
            "model_config_override": {"model_name": model},
        })
        return "assistant", str(assistant["assistant_id"])
    # Stated, not left to the deployment's create-time default: each engine's
    # conversation starts its own sandbox, which is the path this check covers.
    body: dict[str, Any] = {
        "name": f"{label} {engine}",
        "environment_name": environment,
        "model": model,
        "prewarm_enabled": False,
    }
    if engine == "codex":
        body["engine_options"] = _codex_engine_options(model)
    return "agent", str(api.call("POST", "/api/v1/agents", body)["agent_id"])


def _wake(api: Api, assistant_id: str) -> None:
    """Wake the Assistant's workspace; waking again is how its state is read."""

    deadline = time.monotonic() + WAKE_TIMEOUT_SECONDS
    workspace: dict[str, Any] = {}
    while time.monotonic() < deadline:
        workspace = api.call("POST", f"/api/v1/assistants/{assistant_id}/workspace/wake", {})
        if workspace.get("state") == "READY" and workspace.get("current_sandbox_id"):
            return
        time.sleep(3)
    # The workspace names its phase; the cause is on the provisioning Session.
    cause = ""
    provisioning = str(workspace.get("provisioning_session_id") or "")
    if provisioning:
        session = api.session_state(provisioning)
        cause = f"; provisioning Session {provisioning} is {session.get('state')}: {session.get('last_error')!r}"
    raise AcceptanceFailure(
        f"the Assistant workspace stayed {workspace.get('state')} for {WAKE_TIMEOUT_SECONDS:.0f}s{cause}"
    )


def _await_state(api: Api, session_id: str, wanted: str, *, timeout: float, allowed: set[str]) -> None:
    deadline = time.monotonic() + timeout
    while True:
        session = api.session_state(session_id)
        state = str(session.get("state") or "")
        if state == wanted:
            return
        if state not in allowed:
            raise AcceptanceFailure(
                f"Session {session_id} entered {state} instead of {wanted}: "
                f"{' '.join(str(session.get('last_error') or '').split())[:400]}"
            )
        if time.monotonic() >= deadline:
            raise AcceptanceFailure(f"Session {session_id} stayed {state} for {timeout:.0f}s")
        time.sleep(3)


def run_engine(api: Api, engine: str, model: str, *, label: str, expect_text: str) -> str:
    """One engine from a new conversation to a settled first turn; returns the reply."""

    kind, owner_id = _owner(api, engine, model, label)
    if kind == "assistant":
        _wake(api, owner_id)
    session_id = str(api.call("POST", f"/api/v1/{kind}s/{owner_id}/conversations", {})["session_id"])
    print(f"  {engine}: Session {session_id} for {kind} {owner_id}", flush=True)
    try:
        _await_state(api, session_id, "READY", timeout=READY_TIMEOUT_SECONDS, allowed={"CREATING"})
        stream = api.stream(
            f"/api/v1/sessions/{session_id}/ai-stream",
            {"content": MESSAGE, "client_message_id": f"installer-engine-{engine}-{uuid.uuid4().hex[:8]}"},
        )
        ok, reply = judge(stream)
        if not ok:
            raise AcceptanceFailure(reply)
        if expect_text and expect_text not in reply:
            raise AcceptanceFailure(f"the reply does not carry {expect_text!r}: {reply[:200]!r}")
        _await_state(
            api, session_id, "READY", timeout=SETTLE_TIMEOUT_SECONDS,
            allowed={"BUSY", "BACKGROUND_RUNNING"},
        )
        return reply
    finally:
        try:
            api.call("DELETE", f"/api/v1/sessions/{session_id}")
        except AcceptanceFailure as error:
            print(f"  {engine}: could not delete Session {session_id}: {error}", flush=True)


def turns(base_url: str, model: str, engines: list[str], expect_text: str) -> int:
    api = Api(base_url)
    label = f"installer-{uuid.uuid4().hex[:6]}"
    failed = []
    for engine in engines:
        started = time.monotonic()
        try:
            reply = run_engine(api, engine, model, label=label, expect_text=expect_text)
        except AcceptanceFailure as error:
            failed.append(engine)
            print(f"FAIL: {engine}: {error} ({time.monotonic() - started:.0f}s)", flush=True)
            continue
        print(f"PASS: {engine} answered and returned to READY ({time.monotonic() - started:.0f}s): "
              f"{' '.join(reply.split())[:160]}", flush=True)
    print(f"{len(engines) - len(failed)}/{len(engines)} engines passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""), flush=True)
    return 1 if failed else 0


# ── restarts, with and without moving the sandbox edges ───────────────────

HOLD_LABEL = "astrabox.acceptance.edge-hold"
EDGE_SERVICES = ("sandbox-edge", "sandbox-dns-edge")
RESTART_READY_TIMEOUT_SECONDS = 600.0
#: Placeholder containers started to occupy the edges' previous addresses. A
#: bridge that hands out that many other addresses first is not the stack this
#: check was written for.
MAX_HOLDERS = 60


def _docker(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["docker", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AcceptanceFailure(f"docker {' '.join(args)} failed: {result.stderr.strip()[:400]}")
    return result.stdout.strip()


def _service_container(compose_dir: Path, service: str) -> str:
    container = _docker("compose", "ps", "-a", "-q", service, cwd=compose_dir)
    if not container or len(container.split()) != 1:
        raise AcceptanceFailure(f"expected one {service} container in {compose_dir}, found {container!r}")
    return container


def _bridge_address(container: str) -> str:
    return _docker("inspect", "-f", "{{.NetworkSettings.Networks.bridge.IPAddress}}", container)


def _hold_addresses(image: str, addresses: set[str]) -> list[str]:
    """Start placeholders on the bridge until every one of ``addresses`` is taken."""

    holders: list[str] = []
    for _ in range(MAX_HOLDERS):
        holder = _docker(
            "run", "-d", "--rm", "--network", "bridge", "--label", f"{HOLD_LABEL}=1",
            "--entrypoint", "sleep", image, "3600",
        )
        holders.append(holder)
        addresses.discard(_bridge_address(holder))
        if not addresses:
            return holders
    raise AcceptanceFailure(f"{MAX_HOLDERS} placeholders did not take {sorted(addresses)}")


def _await_serving(api: Api) -> None:
    deadline = time.monotonic() + RESTART_READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(api.base_url + "/healthz", timeout=5)
            return
        except OSError:
            time.sleep(3)
    raise AcceptanceFailure(f"the server did not answer /healthz {RESTART_READY_TIMEOUT_SECONDS:.0f}s after start")


def _ask(api: Api, session_id: str, message: str, expected: str) -> str:
    stream = api.stream(
        f"/api/v1/sessions/{session_id}/ai-stream",
        {"content": message, "client_message_id": f"installer-restart-{uuid.uuid4().hex[:8]}"},
    )
    ok, reply = judge(stream)
    if not ok:
        raise AcceptanceFailure(reply)
    if expected and expected not in reply:
        raise AcceptanceFailure(f"the reply does not carry {expected!r}: {reply[:200]!r}")
    _await_state(api, session_id, "READY", timeout=SETTLE_TIMEOUT_SECONDS, allowed={"BUSY", "BACKGROUND_RUNNING"})
    return reply


def restart(
    base_url: str, model: str, compose_dir: Path, engines: list[str], cycles: int, expect_text: str
) -> int:
    api = Api(base_url)
    label = f"installer-restart-{uuid.uuid4().hex[:6]}"
    sessions: dict[str, tuple[str, str]] = {}
    failed: list[str] = []
    try:
        for engine in engines:
            codeword = f"{engine.upper().replace('_', '-')}-{uuid.uuid4().hex[:4].upper()}"
            try:
                kind, owner_id = _owner(api, engine, model, label)
                if kind == "assistant":
                    _wake(api, owner_id)
                session_id = str(api.call("POST", f"/api/v1/{kind}s/{owner_id}/conversations", {})["session_id"])
                _await_state(api, session_id, "READY", timeout=READY_TIMEOUT_SECONDS, allowed={"CREATING"})
                first = MESSAGE if expect_text else (
                    f"Remember this codeword for later in this conversation: {codeword}. Reply with OK only."
                )
                _ask(api, session_id, first, expect_text)
            except AcceptanceFailure as error:
                failed.append(f"{engine} first turn")
                print(f"FAIL: {engine} first turn: {error}", flush=True)
                continue
            sessions[engine] = (session_id, expect_text or codeword)
            print(f"  {engine}: Session {session_id} answered its first turn"
                  + ("" if expect_text else f" and holds codeword {codeword}"), flush=True)
        server = _service_container(compose_dir, "server")
        image = _docker("inspect", "-f", "{{.Config.Image}}", server)
        for cycle in range(1, cycles + 1):
            move = cycle % 2 == 1
            before = {service: _bridge_address(_service_container(compose_dir, service)) for service in EDGE_SERVICES}
            _docker("compose", "stop", cwd=compose_dir)
            holders = _hold_addresses(image, set(before.values())) if move else []
            try:
                _docker("compose", "start", cwd=compose_dir)
                _await_serving(api)
            finally:
                if holders:
                    _docker("rm", "-f", *holders)
            after = {service: _bridge_address(_service_container(compose_dir, service)) for service in EDGE_SERVICES}
            moved = before != after
            print(f"  restart {cycle}: edges {before} -> {after}", flush=True)
            if move and not moved:
                raise AcceptanceFailure(f"restart {cycle} was to move the edges, and they kept {before}")
            question = MESSAGE if expect_text else (
                "What is the codeword I gave you at the start of this conversation? Reply with the codeword only."
            )
            for engine, (session_id, expected) in sessions.items():
                started = time.monotonic()
                try:
                    reply = _ask(api, session_id, question, expected)
                except AcceptanceFailure as error:
                    failed.append(f"{engine} after restart {cycle}")
                    print(f"FAIL: {engine} after restart {cycle} (edges moved: {moved}): {error} "
                          f"({time.monotonic() - started:.0f}s)", flush=True)
                    continue
                print(f"PASS: {engine} after restart {cycle} (edges moved: {moved}, "
                      f"{time.monotonic() - started:.0f}s): {' '.join(reply.split())[:120]}", flush=True)
    except AcceptanceFailure as error:
        failed.append("restart")
        print(f"FAIL: {error}", flush=True)
    finally:
        subprocess.run(
            ["sh", "-c", f"docker ps -aq --filter label={HOLD_LABEL}=1 | xargs -r docker rm -f"],
            capture_output=True,
        )
        for session_id, _ in sessions.values():
            try:
                api.call("DELETE", f"/api/v1/sessions/{session_id}")
            except AcceptanceFailure as error:
                print(f"  could not delete Session {session_id}: {error}", flush=True)
    print(("restart acceptance passed" if not failed else f"restart acceptance failed: {', '.join(failed)}"), flush=True)
    return 1 if failed else 0


# ── the stub model service ────────────────────────────────────────────────


def stub(image: str, host_address: str, port: int) -> int:
    gateway_check = Path(__file__).resolve().parent / "embedded-gateway-acceptance.py"
    spec = importlib.util.spec_from_file_location("embedded_gateway_acceptance", gateway_check)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    subprocess.run(["docker", "rm", "-f", STUB_CONTAINER], capture_output=True)
    started = subprocess.run(
        ["docker", "run", "-d", "--name", STUB_CONTAINER, "-p", f"{host_address}:{port}:8080",
         "--entrypoint", "/opt/litellm/bin/python", image, "-c", module.STUB_UPSTREAM],
        capture_output=True, text=True,
    )
    if started.returncode != 0:
        print(f"FAIL: the stub upstream did not start: {started.stderr.strip()}", file=sys.stderr)
        return 1
    base = f"http://{host_address}:{port}/v1"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            # The stub answers POST only; any HTTP answer proves it listens.
            urllib.request.urlopen(base + "/chat/completions", timeout=3)
        except urllib.error.HTTPError:
            print(base, flush=True)
            return 0
        except OSError:
            time.sleep(1)
    logs = subprocess.run(["docker", "logs", STUB_CONTAINER], capture_output=True, text=True)
    print(f"FAIL: the stub upstream did not answer at {base}\n{logs.stdout}{logs.stderr}", file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    turns_parser = commands.add_parser("turns")
    turns_parser.add_argument("base_url")
    turns_parser.add_argument("model", help="the model route the Agents use, e.g. openai-compatible/stub-model")
    turns_parser.add_argument("--engine", action="append", choices=ENGINES, dest="engines")
    turns_parser.add_argument("--expect-text", default="")
    stub_parser = commands.add_parser("stub")
    stub_parser.add_argument("image")
    stub_parser.add_argument("host_address")
    stub_parser.add_argument("port", type=int)
    judge_parser = commands.add_parser("judge")
    judge_parser.add_argument("sse_file")
    restart_parser = commands.add_parser("restart")
    restart_parser.add_argument("base_url")
    restart_parser.add_argument("model")
    restart_parser.add_argument("compose_dir", type=Path, help="the installation's containers/ directory")
    restart_parser.add_argument("--engine", action="append", choices=ENGINES, dest="engines")
    restart_parser.add_argument("--cycles", type=int, default=3)
    restart_parser.add_argument("--expect-text", default="")
    args = parser.parse_args(argv)
    if args.command == "turns":
        return turns(args.base_url, args.model, args.engines or list(ENGINES), args.expect_text)
    if args.command == "restart":
        return restart(
            args.base_url, args.model, args.compose_dir, args.engines or list(ENGINES),
            args.cycles, args.expect_text,
        )
    if args.command == "stub":
        return stub(args.image, args.host_address, args.port)
    ok, detail = judge(Path(args.sse_file).read_text(encoding="utf-8", errors="replace"))
    print(("PASS " if ok else "FAIL ") + (detail[:200] if ok else detail))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
