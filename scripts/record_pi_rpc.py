#!/usr/bin/env python3
"""Record a real ``pi --mode rpc`` stdout stream into a test fixture.

The fixtures under ``tests/data/pi/`` must come from pi itself: the adapter's
translator is a reader of pi's wire format, and a hand-written imitation of
that format proves only that the reader parses what its author imagined. The
first recording taken for this adapter disagreed with the vendor's own
documentation on two counts — see ``tests/data/pi/README.md``.

The model is faked, pi is not. A local HTTP server answers pi's
OpenAI-compatible requests with a scripted stream, so the assistant's words are
scripted while every RPC record on stdout is pi's.

Usage (from the repo root, through the pinned Node toolchain so the right pi
version resolves):

    python3 scripts/node-toolchain.py python3 scripts/record_pi_rpc.py \\
        --pi /path/to/node_modules/.bin/pi --scenario text --out tests/data/pi

Scenarios:
  text  one prompt answered with streamed text (one pi turn)
  tool  one prompt answered by calling bash, then text (two pi turns)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_SCENARIOS = ("text", "tool")


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse(chunks: list[dict]) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


def _make_handler(scenario: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            self.rfile.read(int(self.headers.get("content-length") or 0))
            Handler.calls += 1
            if scenario == "tool" and Handler.calls == 1:
                chunks = [
                    _chunk({"role": "assistant"}),
                    _chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "bash", "arguments": ""},
                                }
                            ]
                        }
                    ),
                    _chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"command":"echo hi"}'},
                                }
                            ]
                        }
                    ),
                    _chunk({}, "tool_calls"),
                ]
            else:
                chunks = [
                    _chunk({"role": "assistant", "content": ""}),
                    _chunk({"content": "Hello"}),
                    _chunk({"content": " there"}),
                    _chunk({}, "stop"),
                ]
            payload = _sse(chunks)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def record(pi_bin: pathlib.Path, scenario: str, out_dir: pathlib.Path) -> int:
    server = HTTPServer(("127.0.0.1", 0), _make_handler(scenario))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    with tempfile.TemporaryDirectory(prefix="pi-record-") as tmp:
        work = pathlib.Path(tmp)
        agent_dir = work / "agent"
        session_dir = work / "sessions"
        workspace = work / "workspace"
        for path in (agent_dir, session_dir, workspace):
            path.mkdir(parents=True, exist_ok=True)
        (agent_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fake": {
                            "baseUrl": f"http://127.0.0.1:{port}/v1",
                            "api": "openai-completions",
                            "apiKey": "test-key",
                            "compat": {
                                "supportsDeveloperRole": False,
                                "supportsReasoningEffort": False,
                            },
                            "models": [{"id": "fake-model"}],
                        }
                    }
                },
                indent=2,
            )
        )

        argv = [
            str(pi_bin),
            "--mode",
            "rpc",
            "--provider",
            "fake",
            "--model",
            "fake-model",
            "--session-dir",
            str(session_dir),
            # A recording must not carry the recorder's machine in it.
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ]
        if scenario != "tool":
            argv.append("--no-tools")

        proc = subprocess.Popen(
            argv,
            cwd=str(workspace),
            env={
                **os.environ,
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_OFFLINE": "1",
                "PI_SKIP_VERSION_CHECK": "1",
                "PI_TELEMETRY": "0",
                "NO_COLOR": "1",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        records: list[dict] = []
        settled = threading.Event()

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    record_obj = json.loads(line)
                except json.JSONDecodeError:
                    print(f"non-JSON stdout: {line[:200]}", file=sys.stderr)
                    continue
                records.append(record_obj)
                if record_obj.get("type") == "agent_settled":
                    settled.set()

        threading.Thread(target=reader, daemon=True).start()

        assert proc.stdin is not None
        proc.stdin.write(
            json.dumps({"id": "cmd-1", "type": "prompt", "message": "say hi"}) + "\n"
        )
        proc.stdin.flush()

        settled_in_time = settled.wait(timeout=60)
        time.sleep(0.5)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

        if not settled_in_time:
            stderr = proc.stderr.read() if proc.stderr else ""
            print(
                "pi never emitted agent_settled. The most likely cause is an "
                "installed pi older than 0.84.x — that line does not have the "
                "event at all. See tests/data/pi/README.md.\n"
                f"stderr: {stderr[:2000]}",
                file=sys.stderr,
            )
            return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    name = {"text": "text-reply", "tool": "tool-call"}[scenario]
    target = out_dir / f"{name}.jsonl"
    target.write_text("".join(json.dumps(r) + "\n" for r in records))
    print(f"recorded {len(records)} records -> {target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pi", required=True, type=pathlib.Path, help="pi executable")
    parser.add_argument("--scenario", choices=_SCENARIOS, default="text")
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("tests/data/pi"),
        help="directory the fixture is written to",
    )
    args = parser.parse_args()
    if not args.pi.exists():
        print(f"pi executable not found: {args.pi}", file=sys.stderr)
        return 2
    return record(args.pi, args.scenario, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
