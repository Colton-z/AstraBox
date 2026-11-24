"""``astrabox assistant`` sends the console's requests and never shows a credential.

Each command runs as the console script in its own process, because its exit
code and output are what a script reads, against a local HTTP server that
records every request and answers as the deployment's Assistant routes do. The
expected requests are the ones the console sends for the same actions
(``frontend/src/assistant/api.ts``; the per-block ``engine_options`` write in
``frontend/src/manage/assistantEditConfig.ts``), so a CLI that drifted to
another field, route or body shape fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ASSISTANT_ID = "asst-0123"
SECRET = "ghp_cli-test-credential-value"

#: The stored record: a dsh-superagent Assistant with a profile_package block
#: the owner set earlier, which changing another block must carry unchanged.
STORED: dict[str, Any] = {
    "assistant_id": ASSISTANT_ID,
    "display_name": "Model evaluation",
    "engine_kind": "dsh_superagent",
    "runtime_profile_name": "dsh-superagent",
    "model_config_override": None,
    "engine_options": {"profile_package": {"dependencies": {"left-pad": "1.3.0"}}},
    "runtime": {
        "conversations": False,
        "panel": True,
        "panel_opens_in_new_tab": False,
        "owner_secrets": [
            {
                "key": "github_token",
                "label": "GitHub agent account token",
                "help": "",
                "configured": False,
                "updated_at": None,
                "deployment_default": True,
                "required": False,
            }
        ],
    },
}


class _Deployment:
    """Records each request and answers it the way the Assistant routes do."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.hibernated = True

    def answer(self, method: str, path: str, body: Any) -> Any:
        base = f"/api/v1/assistants/{ASSISTANT_ID}"
        if (method, path) == ("GET", "/api/v1/assistants"):
            return [{**STORED, "workspace_state": "READY", "current_sandbox_id": "sbx-1"}]
        if (method, path) == ("GET", base):
            return {**STORED, "workspace_state": "READY", "current_sandbox_id": "sbx-1"}
        if (method, path) == ("PATCH", base):
            return {**STORED, **body}
        if (method, path) == ("PUT", f"{base}/owner-secrets/github_token"):
            secret = {**STORED["runtime"]["owner_secrets"][0], "configured": True,
                      "updated_at": "2026-11-24T00:00:00+00:00"}
            return {**STORED, "runtime": {**STORED["runtime"], "owner_secrets": [secret]}}
        if (method, path) == ("POST", f"{base}/workspace/hibernate"):
            return {"assistant_id": ASSISTANT_ID, "hibernated": self.hibernated,
                    "released": self.hibernated, "recovery_required": not self.hibernated,
                    "previous_sandbox_id": "sbx-1",
                    "sandbox_id": None if self.hibernated else "sbx-1"}
        if (method, path) == ("POST", f"{base}/workspace/wake"):
            return {"assistant_id": ASSISTANT_ID, "state": "MATERIALIZING",
                    "provisioning_session_id": "sess-wake"}
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def deployment() -> Iterator[tuple[_Deployment, str]]:
    state = _Deployment()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            state.requests.append((self.command, self.path, body))
            data = state.answer(self.command, self.path, body)
            payload = json.dumps({"code": "OK", "message": "success", "data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_PATCH = do_PUT = do_POST = _serve

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _cli(endpoint: str, cwd: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ASTRABOX_TOKEN", "ASTRABOX_CLIENT_ID", "ASTRABOX_CLIENT_SECRET",
                       "ASTRABOX_TOKEN_URL", "ASTRABOX_ENV_FILE"}
    }
    env["ASTRABOX_ENDPOINT"] = endpoint
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))
    entry = "import sys;from astrabox.cli import main;sys.exit(main(sys.argv[1:]))"
    return subprocess.run(
        [sys.executable, "-c", entry, *args],
        input=stdin, capture_output=True, text=True, timeout=60, env=env, cwd=cwd,
    )


def test_assistant_commands_send_the_consoles_requests_and_never_show_the_credential(
    deployment: tuple[_Deployment, str], tmp_path: Path
) -> None:
    state, endpoint = deployment
    listing = ("GET", "/api/v1/assistants", None)
    base = f"/api/v1/assistants/{ASSISTANT_ID}"
    runs: list[subprocess.CompletedProcess[str]] = []

    def run(*args: str, stdin: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
        state.requests.clear()
        completed = _cli(endpoint, tmp_path, *args, stdin=stdin)
        runs.append(completed)
        assert completed.returncode == code, completed.stderr
        return completed

    # Switching the model and the reasoning effort writes the model override
    # and DSH's own default_model block, and keeps the block the owner set
    # before: dropping it would silently remove the owner's profile package.
    run("assistant", "set", "Model evaluation", "--model", "deepseek-v4-pro",
        "--engine-option", 'default_model={"reasoningEffort": "xhigh"}', "-o", "json")
    assert state.requests == [
        listing,
        ("PATCH", base, {
            "model_config_override": {"model_name": "deepseek-v4-pro"},
            "engine_options": {
                "profile_package": {"dependencies": {"left-pad": "1.3.0"}},
                "default_model": {"reasoningEffort": "xhigh"},
            },
        }),
    ]

    # Reading it back is the detail read, which carries the workspace's state.
    shown = json.loads(run("assistant", "show", ASSISTANT_ID, "-o", "json").stdout)
    assert state.requests == [listing, ("GET", base, None)]
    assert shown["workspace_state"] == "READY"

    put_body = {"value": SECRET}

    # The credential arrives on stdin and reaches only the PUT body; the
    # output reports that it is set.
    listed = json.loads(
        run("assistant", "secret", "set", ASSISTANT_ID, "github_token", "-o", "json",
            stdin=SECRET + "\n").stdout
    )
    assert state.requests == [listing, ("PUT", f"{base}/owner-secrets/github_token", put_body)]
    assert listed["owner_secrets"][0]["configured"] is True

    # From a file, the same request.
    secret_file = tmp_path / "token"
    secret_file.write_text(SECRET + "\n", encoding="utf-8")
    run("assistant", "secret", "set", ASSISTANT_ID, "github_token", "--from-file", str(secret_file))
    assert state.requests == [listing, ("PUT", f"{base}/owner-secrets/github_token", put_body)]

    run("assistant", "hibernate", ASSISTANT_ID)
    assert state.requests == [listing, ("POST", f"{base}/workspace/hibernate", None)]

    run("assistant", "wake", ASSISTANT_ID)
    assert state.requests == [listing, ("POST", f"{base}/workspace/wake", None)]

    # A hibernate the deployment could not confirm answers 200; exiting 0 would
    # let a script wake a workspace that was never paused.
    state.hibernated = False
    failed = json.loads(run("assistant", "hibernate", ASSISTANT_ID, "-o", "json", code=1).stdout)
    assert failed["ok"] is False and failed["recovery_required"] is True

    for completed in runs:
        assert SECRET not in completed.stdout and SECRET not in completed.stderr
