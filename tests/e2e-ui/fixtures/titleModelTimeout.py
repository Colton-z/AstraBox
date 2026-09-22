"""Exercise the deployed title client against delayed real HTTP responses."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    SessionTitleGenerationError,
    TitleModelRequestConfig,
)


async def request_title(*, delay_seconds: float) -> str:
    requests: list[str] = []

    class DelayedResponse(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(delay_seconds)
            payload = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "should_generate": True,
                                        "title": "标题超时验收",
                                    }
                                )
                            }
                        }
                    ],
                }
            ).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                # The short-timeout case deliberately closes this connection.
                if delay_seconds != 0.2:
                    raise

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), DelayedResponse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        decision = await ConversationTitleModel().decide_title_generation(
            user_text="请介绍数据库索引",
            assistant_text="索引可以加速查询。",
            turn_index=1,
            request_config=TitleModelRequestConfig(
                base_url=f"http://127.0.0.1:{server.server_port}",
                api_key="fixture-only",
                model_name="delayed-http-fixture",
            ),
        )
        assert decision.should_generate is True
        return decision.title
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert requests == ["/chat/completions"], requests


async def main() -> None:
    configured = load_astrabox_settings().title_model_request_timeout_seconds
    # More than the old hard-coded 8s: reading the setting alone cannot pass.
    title = await request_title(delay_seconds=9)
    assert title == "标题超时验收", title

    # This exec process is isolated from the running AstraBox application.
    os.environ["ASTRABOX_TITLE_MODEL_REQUEST_TIMEOUT_SECONDS"] = "0.05"
    try:
        await request_title(delay_seconds=0.2)
    except SessionTitleGenerationError as exc:
        assert isinstance(exc.__cause__, httpx.ReadTimeout), repr(exc.__cause__)
        assert "ReadTimeout" in str(exc), str(exc)
        assert "request_timeout_seconds=0.05" in str(exc), str(exc)
    else:
        raise AssertionError("the deployment override did not shorten the HTTP wait")

    print(
        json.dumps(
            {
                "configured_timeout_seconds": configured,
                "slow_response_seconds": 9,
                "short_override": "ReadTimeout",
                "requests_per_attempt": 1,
            }
        )
    )


asyncio.run(main())
