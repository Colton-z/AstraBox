"""A to-do service for the dsh-superagent custom Source e2e.

    python3 todo_service.py --host HOST --port PORT --admin-key KEY --token TOKEN [--package FILE]

The agent's side (``/api/...``) takes ``Authorization: Bearer <token>`` and
refuses any other value, so a request that arrives with the sandbox's
placeholder instead of the Vault's value is visible as a refusal. The test's
side (``/admin/...``) takes ``X-Admin-Key``: it files items and comments as a
person, reads the replies the agent left, swaps the token the service accepts, and counts the
refused agent requests. ``--package`` also serves a file at
``/packages/<its name>``, as a registry serves a tarball an owner installs.
Every request is printed as one JSON line.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ME = "agent"


class State:
    def __init__(self, token: str) -> None:
        self.lock = threading.Lock()
        self.token = token
        self.items: dict[str, dict] = {}
        self.comments: dict[str, list[dict]] = {}
        self.events: list[dict] = []
        self.refused = 0
        self.keys: dict[str, str] = {}

    def event(self, item: str, event_id: str, author: str, text: str) -> None:
        self.events.append({
            "seq": len(self.events) + 1, "item": item, "id": event_id, "author": author, "text": text,
            "title": self.items[item]["title"], "at": datetime.now(timezone.utc).isoformat(),
            "url": f"todo://items/{item}",
        })


def handler(state: State, admin_key: str, package: Path | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            print(json.dumps({"method": self.command, "path": self.path, "status": status}), flush=True)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _agent(self) -> bool:
            with state.lock:
                if self.headers.get("Authorization") == f"Bearer {state.token}":
                    return True
                state.refused += 1
            self._send(401, {"error": "unauthorized"})
            return False

        def _admin(self) -> bool:
            if self.headers.get("X-Admin-Key") == admin_key:
                return True
            self._send(403, {"error": "forbidden"})
            return False

        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            url = urlsplit(self.path)
            comments = re.fullmatch(r"/admin/items/([\w-]+)/comments", url.path)
            if package is not None and url.path == f"/packages/{package.name}":
                data = package.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                print(json.dumps({"method": "GET", "path": self.path, "status": 200}), flush=True)
            elif url.path == "/api/events":
                if not self._agent():
                    return
                after = int((parse_qs(url.query).get("after") or ["0"])[0])
                with state.lock:
                    events = [event for event in state.events if event["seq"] > after]
                    last = state.events[-1]["seq"] if state.events else 0
                self._send(200, {"me": ME, "events": events, "next": last})
            elif comments:
                if not self._admin():
                    return
                with state.lock:
                    listed = list(state.comments.get(comments.group(1), []))
                self._send(200, listed)
            elif url.path == "/admin/refused":
                if not self._admin():
                    return
                with state.lock:
                    refused = state.refused
                self._send(200, {"refused": refused})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 — http.server's name
            url = urlsplit(self.path)
            reply = re.fullmatch(r"/api/items/([\w-]+)/comments", url.path)
            person = re.fullmatch(r"/admin/items/([\w-]+)/comments", url.path)
            if reply:
                if not self._agent():
                    return
                self._reply(reply.group(1), self._body())
            elif person:
                if not self._admin():
                    return
                body = self._body()
                with state.lock:
                    if person.group(1) not in state.items:
                        self._send(404, {"error": "no such item"})
                        return
                    comment_id = f"c-{sum(len(c) for c in state.comments.values()) + 1}"
                    state.comments.setdefault(person.group(1), []).append(
                        {"id": comment_id, "author": str(body["author"]), "text": str(body.get("text") or ""), "key": ""})
                    state.event(person.group(1), comment_id, str(body["author"]), str(body.get("text") or ""))
                self._send(201, {"id": comment_id})
            elif url.path == "/admin/items":
                if not self._admin():
                    return
                body = self._body()
                with state.lock:
                    item = f"item-{len(state.items) + 1}"
                    state.items[item] = {"title": str(body.get("title") or item)}
                    state.event(item, item, str(body["author"]), str(body.get("text") or ""))
                self._send(201, {"id": item, "url": f"todo://items/{item}"})
            elif url.path == "/admin/token":
                if not self._admin():
                    return
                body = self._body()
                with state.lock:
                    state.token = str(body["token"])
                self._send(200, {"token_changed": True})
            else:
                self._send(404, {"error": "not found"})

        def _reply(self, item: str, body: dict) -> None:
            text = str(body.get("text") or "")
            key = str(body.get("key") or "")
            with state.lock:
                if item not in state.items:
                    status, answer = 404, {"error": "no such item"}
                elif key and key in state.keys:
                    status, answer = 200, {"id": state.keys[key], "duplicate": True}
                else:
                    comment_id = f"c-{sum(len(c) for c in state.comments.values()) + 1}"
                    state.comments.setdefault(item, []).append(
                        {"id": comment_id, "author": ME, "text": text, "key": key})
                    if key:
                        state.keys[key] = comment_id
                    state.event(item, comment_id, ME, text)
                    status, answer = 201, {"id": comment_id}
            self._send(status, answer)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--package", type=Path)
    args = parser.parse_args()
    state = State(args.token)
    server = ThreadingHTTPServer((args.host, args.port), handler(state, args.admin_key, args.package))
    print(json.dumps({"listening": args.port}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
