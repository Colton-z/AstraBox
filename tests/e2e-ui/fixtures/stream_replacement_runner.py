"""A truncated model HTTP response through the image's real Claude SDK and CLI.

Only this cold test box's model endpoint changes. Native SDK messages are observed,
never manufactured; the normal model endpoint handles the final recovery turn.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, StreamEvent, UserMessage

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("replacement_runner", "/opt/astrabox/sandbox_runner.py")
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
original_init = ClaudeSDKClient.__init__
receive = ClaudeSDKClient.receive_messages
upstream = ""
state: dict = {"requests": {}, "native": []}
lock = threading.Lock()


def save() -> None:
    temporary = ROOT / "observation.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(ROOT / "observation.json")


def message(identity: str, content: list, stop: str = "end_turn") -> dict:
    return {"id": identity, "type": "message", "role": "assistant", "model": "fixture",
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}}


def sse(event: dict) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()


class Gate(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def answer(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        data = json.loads(raw)
        users = [item for item in data.get("messages", []) if item.get("role") == "user"]
        prompt = next((str(item.get("content")) for item in reversed(users)
                       if "E2E_REPLACE_" in str(item.get("content"))
                       or "replacement-recovered" in str(item.get("content"))), "")
        mode = "thinking" if "E2E_REPLACE_THINKING" in prompt else "text"
        if not prompt or "/messages" not in self.path or "/count_tokens" in self.path or "replacement-recovered" in prompt:
            request = urllib.request.Request(upstream.rstrip("/") + self.path, data=raw,
                                             headers={k: v for k, v in self.headers.items()
                                                      if k.lower() not in {"host", "content-length"}})
            try:
                response = urllib.request.urlopen(request, timeout=90)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                self.answer(response.read(), response.headers.get("Content-Type", "application/json"), response.status)
            return
        with lock:
            count = state["requests"].get(mode, 0) + 1
            state["requests"][mode] = count
            save()
        identity = f"msg_fixture_{mode}_{count}"
        if not data.get("stream"):
            deadline = time.monotonic() + 90
            while not (ROOT / f"release-{mode}").exists():
                if time.monotonic() > deadline:
                    self.answer(b'{"error":{"type":"api_error","message":"fixture not released"}}',
                                "application/json", 500)
                    return
                time.sleep(0.05)
            self.answer(json.dumps(message(identity, [{"type": "text", "text": f"E2E_REPLACED_{mode}_DONE"}])).encode(),
                        "application/json")
            return
        start = message(identity, [])
        start["stop_reason"] = None
        events = [{"type": "message_start", "message": start}]
        if mode == "thinking" and count == 1:
            blocks = [{"type": "text", "text": "E2E_KEEP_COMPLETED_STEP"},
                      {"type": "tool_use", "id": "tool_fixture_kept", "name": "Bash",
                       "input": {"command": "printf E2E_KEEP_TOOL_RESULT"}}]
            for index, block in enumerate(blocks):
                initial = {**block, **({"text": ""} if block["type"] == "text" else {"input": {}})}
                delta = ({"type": "text_delta", "text": block["text"]} if block["type"] == "text"
                         else {"type": "input_json_delta", "partial_json": json.dumps(block["input"])})
                events += [{"type": "content_block_start", "index": index, "content_block": initial},
                           {"type": "content_block_delta", "index": index, "delta": delta},
                           {"type": "content_block_stop", "index": index}]
            events += [{"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                        "usage": {"output_tokens": 10}}, {"type": "message_stop"}]
        else:
            initial = {"type": "thinking", "thinking": "", "signature": ""} if mode == "thinking" else {"type": "text", "text": ""}
            delta = {"type": "thinking_delta", "thinking": "E2E_ABANDONED_thinking"} if mode == "thinking" else {"type": "text_delta", "text": "E2E_ABANDONED_text"}
            events += [{"type": "content_block_start", "index": 0, "content_block": initial},
                       {"type": "content_block_delta", "index": 0, "delta": delta}]
        body = b"".join(map(sse, events))
        if mode == "thinking" and count == 1:
            self.answer(body, "text/event-stream")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        deadline = time.monotonic() + 45
        while not (ROOT / f"truncate-{mode}").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("preview was not observed before truncation")
            time.sleep(0.05)
        self.close_connection = True


gate = ThreadingHTTPServer(("127.0.0.1", 0), Gate)
threading.Thread(target=gate.serve_forever, daemon=True).start()


def redirected(self, *, options=None, **kwargs):
    global upstream
    assert options is not None and options.env.get("ANTHROPIC_BASE_URL")
    upstream = options.env["ANTHROPIC_BASE_URL"]
    options.env = {**options.env, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{gate.server_port}"}
    original_init(self, options=options, **kwargs)


async def observed(self):
    async for item in receive(self):
        row = None
        if isinstance(item, StreamEvent):
            row = {"kind": "stream", "event": item.event}
        elif isinstance(item, AssistantMessage):
            row = {"kind": "assistant", "message_id": item.message_id,
                   "content": runner._jsonable(item.content)}
        elif isinstance(item, UserMessage):
            row = {"kind": "user", "content": runner._jsonable(item.content)}
        if row is not None:
            with lock:
                state["native"].append(row)
                save()
        yield item


ClaudeSDKClient.__init__ = redirected
ClaudeSDKClient.receive_messages = observed
save()
asyncio.run(runner.main())
