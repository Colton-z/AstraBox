"""Tiny authenticated Streamable HTTP MCP server for the AWS Playwright suite."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

EXPECTED_BEARER = str(os.environ.get("EXPECTED_BEARER") or "").strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep the fixture quiet and, in particular, never log Authorization.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/mcp":
            self._json(404, {"error": "not found"})
            return
        if EXPECTED_BEARER and self.headers.get("Authorization") != (
            f"Bearer {EXPECTED_BEARER}"
        ):
            self._json(401, {"error": "missing managed MCP credential"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "invalid JSON"},
                },
            )
            return

        request_id = request.get("id")
        method = str(request.get("method") or "")
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "astrabox-e2e-mcp", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Return the supplied text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            if params.get("name") != "echo":
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "unknown tool"},
                    },
                )
                return
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": f"MCP_E2E_OK:{str(arguments.get('text') or '')}",
                    }
                ]
            }
        elif method == "ping":
            result = {}
        else:
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "unknown method"},
                },
            )
            return

        self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
