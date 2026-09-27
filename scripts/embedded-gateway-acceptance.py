#!/usr/bin/env python3
"""Prove a server image's embedded model gateway answers every client protocol.

    scripts/embedded-gateway-acceptance.py <server image>

Every engine reaches its model through the LiteLLM gateway baked into the
server image: Claude Code over Anthropic Messages (``/v1/messages``), Codex over
Responses (``/v1/responses``), and Hermes, the DeepSeek Harness and Pi over
Chat Completions (``/v1/chat/completions``). This check runs that gateway
exactly as the entry point starts it (astrabox/deploy/onebox.py): the image's
own venv, its baked configuration and its pinned dependencies, with no Langfuse
keys, as a fresh installation has. The ``openai-compatible/*`` route points at a
stub OpenAI-compatible upstream, run from the same image, so no model key is
needed.

The first request goes to a gateway process nothing else has reached. LiteLLM
constructs the loggers named under ``success_callback`` and
``failure_callback`` during the first model request of a process, so one that
cannot be constructed fails exactly that request. On an installed stack, other
traffic (a retrying client, a warmed Agent box) can reach the gateway first
and hide it.

Prints one PASS or FAIL line per request and exits non-zero on any failure.
Needs only Docker and python3; the containers and network it creates are
removed on exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPLY = "STUB-OK"
MODEL = "openai-compatible/stub-model"
GATEWAY_KEY = "sk-gateway-acceptance"

# Chat Completions and Responses, streamed and not, each answering REPLY. The
# gateway sends /v1/messages for an `openai/` route to the upstream's Responses
# API, so these two surfaces serve all three client protocols.
STUB_UPSTREAM = r'''
import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLY = "STUB-OK"
USAGE = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def response(model):
    return {"id": "resp_stub", "object": "response", "created_at": int(time.time()), "status": "completed",
            "model": model, "output": [{"type": "message", "id": "msg_stub", "status": "completed",
                                        "role": "assistant",
                                        "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}}


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("upstream " + fmt % args, flush=True)

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream(self, frames):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(frame.encode())
            self.wfile.flush()
        self.close_connection = True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        model, streamed = body.get("model", ""), bool(body.get("stream"))
        if self.path.endswith("/chat/completions"):
            head = {"id": "chatcmpl-stub", "created": int(time.time()), "model": model}
            if not streamed:
                return self.reply({**head, "object": "chat.completion", "usage": USAGE, "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}]})
            chunk = {**head, "object": "chat.completion.chunk"}
            return self.stream([
                "data: " + json.dumps({**chunk, "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": REPLY}, "finish_reason": None}]}) + "\n\n",
                "data: " + json.dumps({**chunk, "usage": USAGE, "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}]}) + "\n\n",
                "data: [DONE]\n\n"])
        if self.path.endswith("/responses"):
            done = response(model)
            if not streamed:
                return self.reply(done)
            events = [("response.created", {"response": {**done, "status": "in_progress", "output": []}}),
                      ("response.output_text.delta", {"item_id": "msg_stub", "output_index": 0,
                                                      "content_index": 0, "delta": REPLY}),
                      ("response.completed", {"response": done})]
            return self.stream([f"event: {name}\ndata: " + json.dumps({"type": name, "sequence_number": n, **data})
                                + "\n\n" for n, (name, data) in enumerate(events)])
        self.send_error(404)


ThreadingHTTPServer(("0.0.0.0", 8080), Upstream).serve_forever()
'''

MESSAGES = [{"role": "user", "content": "Reply with the stub's text."}]
REQUESTS = (
    ("chat", "/v1/chat/completions", {"model": MODEL, "messages": MESSAGES}),
    ("chat stream", "/v1/chat/completions", {"model": MODEL, "messages": MESSAGES, "stream": True}),
    ("messages", "/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": MESSAGES}),
    ("messages stream", "/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": MESSAGES, "stream": True}),
    ("responses", "/v1/responses", {"model": MODEL, "input": MESSAGES[0]["content"]}),
    ("responses stream", "/v1/responses", {"model": MODEL, "input": MESSAGES[0]["content"], "stream": True}),
)


def _docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _events(text: str) -> list[dict]:
    """The JSON payloads of a server-sent event stream, ``[DONE]`` excluded."""

    payloads = []
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            payloads.append(json.loads(line[5:]))
    return payloads


def reply_text(name: str, text: str) -> str:
    """The assistant text a client of this protocol reads from the response."""

    if name == "chat":
        return json.loads(text)["choices"][0]["message"]["content"]
    if name == "messages":
        return "".join(block["text"] for block in json.loads(text)["content"] if block["type"] == "text")
    if name == "responses":
        return "".join(
            part["text"]
            for item in json.loads(text)["output"] if item["type"] == "message"
            for part in item["content"] if part["type"] == "output_text"
        )
    events = _events(text)
    errors = [event for event in events if event.get("type") == "error" or event.get("error")]
    if errors:
        raise ValueError(f"the stream carried an error event: {json.dumps(errors[0])[:300]}")
    if name == "chat stream":
        return "".join(
            choice["delta"].get("content") or "" for event in events for choice in event.get("choices") or []
        )
    if name == "messages stream":
        return "".join(
            event["delta"]["text"]
            for event in events
            if event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta"
        )
    return "".join(event["delta"] for event in events if event.get("type") == "response.output_text.delta")


def _send(base: str, path: str, body: dict) -> tuple[int, str]:
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {GATEWAY_KEY}",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def _wait_live(gateway: str, base: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _docker("inspect", "-f", "{{.State.Running}}", gateway) != "true":
            raise RuntimeError("the gateway exited during startup")
        try:
            with urllib.request.urlopen(base + "/health/liveliness", timeout=5):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise RuntimeError("the gateway did not answer /health/liveliness within 180 s")


def run(image: str) -> int:
    prefix = f"astrabox-gateway-acceptance-{os.getpid()}"
    network, upstream, gateway = prefix, f"{prefix}-upstream", f"{prefix}-gateway"
    passed = 0
    try:
        _docker("network", "create", network)
        # The image's LiteLLM interpreter, so the stub needs no image of its own.
        _docker("run", "-d", "--name", upstream, "--network", network,
                "--entrypoint", "/opt/litellm/bin/python", image, "-c", STUB_UPSTREAM)
        _docker("run", "-d", "--name", gateway, "--network", network, "-p", "127.0.0.1::4000",
                "-e", f"LITELLM_MASTER_KEY={GATEWAY_KEY}",
                "-e", "ASTRABOX_AUTH_SESSION_SECRET=gateway-acceptance",
                "-e", "OPENAI_COMPATIBLE_API_KEY=stub-key",
                "-e", f"OPENAI_COMPATIBLE_BASE_URL=http://{upstream}:8080/v1",
                "--entrypoint", "/opt/litellm/bin/litellm", image,
                "--config", "/opt/astrabox/litellm/config.yaml", "--host", "0.0.0.0", "--port", "4000")
        port = _docker("port", gateway, "4000/tcp").splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        _wait_live(gateway, base)
        print(f"gateway from {image} is live; sending its first model request", flush=True)
        for index, (name, path, body) in enumerate(REQUESTS):
            label = f"{name} ({path}{', first request of the gateway process' if index == 0 else ''})"
            status, text = _send(base, path, body)
            try:
                text_ok = status == 200 and reply_text(name, text).strip() == REPLY
                detail = "" if text_ok else f"HTTP {status}: {' '.join(text.split())[:300]}"
            except (ValueError, KeyError, IndexError, TypeError) as error:
                text_ok, detail = False, f"HTTP {status}, unreadable reply ({error}): {' '.join(text.split())[:300]}"
            print(f"PASS: {label}" if text_ok else f"FAIL: {label}: {detail}", flush=True)
            passed += text_ok
    except RuntimeError as error:
        print(f"FAIL: {error}", flush=True)
    finally:
        if passed != len(REQUESTS):
            log = subprocess.run(["docker", "logs", "--tail", "60", gateway], capture_output=True, text=True)
            print("--- gateway log (last 60 lines)\n" + log.stdout + log.stderr, flush=True)
        subprocess.run(["docker", "rm", "-f", upstream, gateway], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
    print(f"{passed}/{len(REQUESTS)} requests passed", flush=True)
    return 0 if passed == len(REQUESTS) else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(run(sys.argv[1]))
