"""Image launcher credential custody and authenticated readiness failures."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "containers/sandbox-deepseek-harness/astrabox-dsh-web"


def test_vendor_url_is_private_and_never_copied_to_logs(tmp_path: Path) -> None:
    url = "http://127.0.0.1:44781/?token=native-issued-secret"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "serve", sys.executable, "-c",
         f"print('dsh web: {url}'); print('ordinary diagnostic')"],
        env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True,
        check=True, timeout=10,
    )
    path = tmp_path / ".deepseek-harness/web-url"
    assert path.read_text().strip() == url
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "native-issued-secret" not in result.stdout + result.stderr
    assert "ordinary diagnostic" in result.stdout


@pytest.mark.parametrize("issue_cookie,rpc_ok,http_status", [
    (True, True, 200), (False, True, 200), (True, False, 200), (True, True, 404),
])
def test_readiness_requires_cookie_exchange_and_successful_native_rpc(
    tmp_path: Path, issue_cookie: bool, rpc_ok: bool, http_status: int,
) -> None:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path == "/?token=vendor-token":
                self.send_response(303)
                if issue_cookie:
                    self.send_header("Set-Cookie", "vendor-session=signed; HttpOnly; Path=/")
                self.send_header("Location", "/")
            else:
                self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": payload, "cookie": self.headers.get("Cookie")})
            self.send_response(http_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "type": "server-response", "rpcId": payload["rpcId"],
                "result": {"ok": rpc_ok, "value": []},
            }).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    path = tmp_path / ".deepseek-harness/web-url"
    path.parent.mkdir()
    path.write_text(f"http://127.0.0.1:{port}/?token=vendor-token\n")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "probe", str(port)],
            env={**os.environ, "HOME": str(tmp_path)}, capture_output=True, text=True,
            timeout=10,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert (result.returncode == 0) is (issue_cookie and rpc_ok and http_status == 200)
    assert "vendor-token" not in result.stdout + result.stderr
    if http_status != 200:
        assert f"session/list: HTTP {http_status}" in result.stderr
    elif not issue_cookie:
        assert "token exchange" in result.stderr
    elif not rpc_ok:
        assert "session/list" in result.stderr
    if issue_cookie:
        assert requests == [{
            "path": "/api/session/list", "cookie": "vendor-session=signed",
            "body": {"type": "client-request", "rpcId": "astrabox-readiness",
                     "method": "session/list", "payload": {"args": {"_request": {}}}},
        }]
    else:
        assert requests == []
