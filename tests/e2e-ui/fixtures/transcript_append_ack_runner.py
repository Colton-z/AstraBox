"""Test-owned response faults around the image's unchanged resident runner.

The production spool owns acceptance, identifiers, retry and deletion. This
wrapper only times SDK acceptance and withholds/injects HTTP responses for the
exact test input. It never reads the runner's capability or fabricates success.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import sys
import threading
import time
import urllib.request
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "astrabox_ack_probe_runner", "/opt/astrabox/sandbox_runner.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)

lock = threading.Lock()
states: dict[str, dict] = {}
real_urlopen = urllib.request.urlopen
real_append = runner.SpoolSessionStore.append


def selected_config(entries: list[dict]) -> dict | None:
    if (ROOT / "disarmed").exists() or not (ROOT / "arm.json").exists():
        return None
    config = json.loads((ROOT / "arm.json").read_text())
    if config["mode"] not in ("ack", "502", "malformed"):
        raise RuntimeError("unknown transcript response fault")
    if any(
        entry.get("type") == "user" and config["marker"] in json.dumps(entry, ensure_ascii=False)
        for entry in entries
    ):
        return config
    return None


def record(config: dict, mutate):
    mode = config["mode"]
    with lock:
        state = states.setdefault(
            mode,
            {
                "mode": mode,
                "session_id": config["session_id"],
                "attempts": [],
                "sdk_accepts": [],
                "errors": [],
            },
        )
        result = mutate(state)
        temporary = ROOT / f"{mode}.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False))
        temporary.replace(ROOT / f"{mode}.json")
        return result


def wait_for_release(config: dict) -> None:
    deadline = time.monotonic() + 60
    while not (ROOT / f"{config['mode']}.release").exists():
        if (ROOT / "disarmed").exists():
            return
        if time.monotonic() >= deadline:
            record(config, lambda state: state["errors"].append("release deadline expired"))
            raise TimeoutError("transcript response fault release deadline expired")
        time.sleep(0.05)


async def observed_append(self, key, entries):
    config = selected_config(entries)
    if config is None:
        return await real_append(self, key, entries)
    started = time.monotonic()
    try:
        # The source pressures SDK acceptance with a one-second caller budget.
        # Here fsync acceptance is separate from the production background flush.
        async with asyncio.timeout(1):
            result = await real_append(self, key, entries)
    except Exception as exc:
        error_type = type(exc).__name__
        record(config, lambda state: state["errors"].append(error_type))
        raise
    record(
        config,
        lambda state: state["sdk_accepts"].append(
            {
                "elapsed_seconds": time.monotonic() - started,
                "payload_sha256": runner.transcript_payload_digest(key, entries),
            }
        ),
    )
    return result


class ObservedResponse:
    def __init__(self, response, config, ordinal):
        self.response = response
        self.config = config
        self.ordinal = ordinal

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)

    def read(self):
        body = self.response.read()
        payload = json.loads(body)
        if self.response.status != 200 or payload.get("code") != "OK":
            record(self.config, lambda state: state["errors"].append("non-OK real append"))
            raise RuntimeError("transcript append did not return real HTTP 200/OK")
        data = payload["data"]
        lost = self.config["mode"] == "ack" and self.ordinal == 0
        malformed = self.config["mode"] == "malformed" and self.ordinal == 0

        def update(state):
            state["attempts"][self.ordinal].update(
                {
                    "status": self.response.status,
                    "response_code": payload["code"],
                    "store_sequence": data["store_sequence"],
                    "injected_timeout_after_commit": lost,
                    "injected_malformed_ack": malformed,
                }
            )
            state["stage"] = (
                "malformed-ack" if malformed else
                "committed-awaiting-release" if lost else "acknowledged"
            )

        record(self.config, update)
        if lost:
            # The host independently reads the committed batch before releasing
            # this read. Only then is the source's timeout substituted for ACK.
            wait_for_release(self.config)
            raise TimeoutError("E2E read timeout after real committed append")
        if malformed:
            return b"<html>transient upstream acknowledgement</html>"
        return body


def observed_urlopen(request, *args, **kwargs):
    if not isinstance(request, urllib.request.Request):
        return real_urlopen(request, *args, **kwargs)
    if not urlsplit(request.full_url).path.endswith("/append"):
        return real_urlopen(request, *args, **kwargs)
    body = bytes(request.data or b"")
    payload = json.loads(body)
    config = selected_config(payload.get("entries", []))
    if config is None:
        return real_urlopen(request, *args, **kwargs)
    # The activation-provided base URL includes /api/v1/sbxcap/{token}.
    # Match the operation suffix, preserving that credential-bearing prefix
    # on the original request and never copying it into test evidence.
    operation = f"/api/v1/transcript/{config['session_id']}/append"
    if not urlsplit(request.full_url).path.endswith(operation):
        record(config, lambda state: state["errors"].append("selected another platform Session"))
        raise RuntimeError("transcript fault selected another platform Session")

    def select(state):
        if "append_id" not in state:
            state.update(
                {
                    "append_id": payload["append_id"],
                    "key": payload["key"],
                    "entries": payload["entries"],
                }
            )
        if state["append_id"] != payload["append_id"]:
            return None
        ordinal = len(state["attempts"])
        state["attempts"].append(
            {
                "append_id": payload["append_id"],
                "request_body_sha256": hashlib.sha256(body).hexdigest(),
                "payload_sha256": runner.transcript_payload_digest(
                    payload["key"], payload["entries"]
                ),
                "declared_payload_sha256": payload["payload_sha256"],
                "injected_transient_http": config["mode"] == "502" and ordinal < 4,
            }
        )
        return ordinal

    ordinal = record(config, select)
    if ordinal is None:
        return real_urlopen(request, *args, **kwargs)
    if config["mode"] == "502" and ordinal < 4:
        record(config, lambda state: state["attempts"][ordinal].update({"status": 502}))
        raise HTTPError(
            request.full_url,
            502,
            "E2E injected transient gateway response",
            Message(),
            io.BytesIO(b"injected transient gateway response"),
        )
    if config["mode"] == "502" and ordinal == 4:
        record(config, lambda state: state.update({"stage": "retry-awaiting-release"}))
        wait_for_release(config)
    if config["mode"] == "malformed" and ordinal == 1:
        record(config, lambda state: state.update({"stage": "retry-awaiting-release"}))
        wait_for_release(config)
    return ObservedResponse(real_urlopen(request, *args, **kwargs), config, ordinal)


runner.SpoolSessionStore.append = observed_append
urllib.request.urlopen = observed_urlopen
asyncio.run(runner.main())
