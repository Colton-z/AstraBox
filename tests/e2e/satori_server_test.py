"""One live external-backend journey through the fixed-Agent Satori server."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from websockets.sync.client import connect

from tests.e2e import _docker_compute
from tests.e2e._sandbox_helpers import (
    data, decision, permission_mode, psql, psql_rows, sql_literal,
    transcript_entries, wait_for_file,
)

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


class Feed:
    def __init__(self, base, agent, token, sn=0):
        url = base.replace("http://", "ws://").replace("https://", "wss://")
        self.ws = connect(f"{url}/api/satori/agents/{agent}/v1/events", open_timeout=10)
        self.ws.send(json.dumps({"op": 3, "body": {"token": token, "sn": sn}}))
        ready = json.loads(self.ws.recv(timeout=10))
        assert ready["op"] == 4 and ready["body"]["logins"][0]["user"]["id"] == agent, ready
        self.events = []
        self.error = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        ping = time.monotonic()
        try:
            while not self.stopping.is_set():
                if time.monotonic() - ping > 5:
                    self.ws.send(json.dumps({"op": 1}))
                    ping = time.monotonic()
                try:
                    frame = json.loads(self.ws.recv(timeout=0.5))
                except TimeoutError:
                    continue
                if frame["op"] == 0:
                    self.events.append(frame["body"])
        except Exception as exc:
            if not self.stopping.is_set():
                self.error = repr(exc)

    def close(self):
        self.stopping.set()
        self.ws.close()
        self.thread.join(timeout=2)


def test_delegated_satori_chat_uses_owned_native_sessions_and_replays_across_replicas(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    plan = json.loads(Path(os.environ["ASTRABOX_E2E_SATORI_PLAN"]).read_text())
    agent, bases, tokens, principals = (plan[key] for key in ("agent_id", "bases", "tokens", "principals"))
    assert len(bases) == len(tokens) == len(principals) == 2
    feeds = []
    sessions = []
    receipts = []
    markers = ["SATORI_PRIVATE_" + uuid.uuid4().hex for _ in range(4)]

    def call(actor, action, body, *, key=None, replica=0, expect=200, extra=None):
        headers = {"Authorization": "Bearer " + tokens[actor], "Satori-Platform": "astrabox", "Satori-User-ID": agent}
        if key:
            headers["Idempotency-Key"] = key
        if extra:
            headers.update(extra)
        response = httpx.post(f"{bases[replica]}/api/satori/agents/{agent}/v1/{action}",
                              headers=headers, json=body, timeout=max(1, min(20, live_test_deadline-time.monotonic())))
        assert response.status_code == expect, (action, response.status_code, response.text[:500])
        return response.json()

    def wait(predicate, label):
        last = None
        while time.monotonic() < live_test_deadline:
            last = predicate()
            if last:
                return last
            assert all(feed.error is None for feed in feeds), [feed.error for feed in feeds]
            time.sleep(0.2)
        pytest.fail(f"{label} did not settle; last={last!r}")

    def detail(index):
        return call(index//2, "internal/astrabox.conversation.get", {"channel_id": sessions[index]})

    def send(index, text, *, key=None):
        result = call(index//2, "message.create", {"channel_id": sessions[index], "content": text}, key=key or uuid.uuid4().hex)
        assert len(result) == 1 and result[0]["user"]["id"] == principals[index//2], result
        receipts.append(result[0])
        return result[0]

    def completed(index, previous=None):
        def read():
            row = detail(index)
            response = row.get("last_response") or {}
            return response if response.get("status") == "ended" and response.get("response_id") != previous and not row.get("held_inputs") and not row.get("current_responses") else None
        result = wait(read, "native response")
        assert result["outcome"] == "completed", result
        return result["response_id"]

    def full_history(index):
        return call(index//2, "internal/astrabox.history", {"channel_id": sessions[index]})

    try:
        before = psql("SELECT count(*) FROM sessions")[0]
        installation = psql("SELECT installation_id FROM installation")[0]
        compute_query = ["docker", "ps", "-a", "--filter", "label=astrabox.installation=" + installation, "--format", "{{.ID}}"]
        compute_before = subprocess.check_output(compute_query, timeout=10).splitlines()
        assert not call(0, "internal/astrabox.preparation.get", {})["ready"]
        assert call(1, "internal/astrabox.preparation.get", {})["ready"]  # Existing personal grant.
        call(0, "channel.create", {"guild_id": agent, "data": {"type": 0}}, expect=409)
        assert psql("SELECT count(*) FROM sessions")[0] == before, "Preflight created a Session"
        assert call(0, "internal/astrabox.preparation.perform", {"action_id": "authorize"})["ready"]
        assert psql("SELECT count(*) FROM sessions")[0] == before
        assert subprocess.check_output(compute_query, timeout=10).splitlines() == compute_before
        spoofed = call(0, "channel.list", {"guild_id": agent}, extra={"X-Forwarded-User": principals[1]})
        assert spoofed["data"] == []
        call(0, "channel.list", {"guild_id": agent, "user_id": principals[1]}, expect=400)
        for index in range(4):
            key = uuid.uuid4().hex
            body = {"guild_id": agent, "data": {"type": 0, "name": f"Satori conversation {index}"}}
            channel = call(index//2, "channel.create", body, key=key)
            assert call(index//2, "channel.create", body, key=key)["id"] == channel["id"]
            sessions.append(channel["id"])
            def ready():
                view = call(index//2, "internal/astrabox.conversation.get", {"channel_id": channel["id"]})
                assert view["state"] != "TERMINATED", view
                return view if view["state"] == "READY" and view.get("engine_capabilities") else None
            wait(ready, "Channel startup")
            print(f"Satori: owned Channel {index + 1}/4 ready", flush=True)
        assert len(set(sessions)) == 4
        call(0, "channel.get", {"channel_id": sessions[2]}, expect=404)
        call(1, "message.list", {"channel_id": sessions[0]}, expect=404)
        assert {item["id"] for item in call(0, "channel.list", {"guild_id": agent})["data"]} == set(sessions[:2])
        assert {item["id"] for item in call(1, "channel.list", {"guild_id": agent})["data"]} == set(sessions[2:])
        feeds = [Feed(bases[0], agent, token) for token in tokens]
        def first(index):
            key = uuid.uuid4().hex
            prompt = f"Remember this conversation's private marker: {markers[index]}. Reply with only that marker, without tools."
            accepted = send(index, prompt, key=key)
            assert call(index//2, "message.create", {"channel_id": sessions[index], "content": prompt}, key=key)[0]["id"] == accepted["id"]
            return completed(index)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first_responses = list(pool.map(first, range(4)))
        wait(lambda: all(any(event.get("type") == "message-updated" and event.get("message", {}).get("id") == rid
                            for event in feeds[index//2].events) for index, rid in enumerate(first_responses)), "first live messages")
        print("Satori: first four native replies observed live", flush=True)
        cut = max(event["sn"] for event in feeds[0].events)
        first_events = list(feeds[0].events)
        feeds[0].close()
        # An ordinary Agent revision must retain the principal/subject grant.
        current_agent = data(e2e_client.get(f"/api/v1/agents/{agent}"))
        data(e2e_client.put(f"/api/v1/agents/{agent}", json={
            "name": "Satori external chat revised", "model": current_agent["model"],
            "runtime_profile_name": current_agent["runtime_profile_name"],
            "version": current_agent["version"],
        }))
        assert all(call(actor, "internal/astrabox.preparation.get", {})["ready"] for actor in range(2))
        def second(index):
            send(index, "Reply with only the private marker I asked you to remember in this conversation. Do not use tools.")
            return completed(index, first_responses[index])
        with ThreadPoolExecutor(max_workers=4) as pool:
            second_responses = list(pool.map(second, range(4)))
        feeds[0] = Feed(bases[1], agent, tokens[0], sn=cut)
        wait(lambda: all(any(event.get("type") == "message-updated" and event.get("message", {}).get("id") == rid
                            for event in feeds[index//2].events) for index, rid in enumerate(second_responses)), "cross-replica replay")
        print("Satori: second replies replayed across replicas", flush=True)
        for actor, feed in enumerate(feeds):
            assert all(event["channel"]["id"] in sessions[actor*2:actor*2+2] for event in feed.events)
            assert [event["sn"] for event in feed.events] == sorted(set(event["sn"] for event in feed.events))
        assert all(event["sn"] > cut for event in feeds[0].events)
        for index, rid in enumerate(second_responses):
            history = full_history(index)["messages"]
            reply = next(item for item in history if item["id"] == rid)
            text = "".join(part.get("text", "") for part in reply["parts"] if part["type"] == "text")
            assert text.strip() == markers[index], text
            for other in set(markers)-{markers[index]}:
                assert other not in json.dumps(history)
            native = transcript_entries(sessions[index])
            assert sum(markers[index] in json.dumps(entry["entry"]) for entry in native if entry["entry"].get("type") == "user") == 1, "Duplicate native delivery"
            native_texts = [block["text"] for entry in native if entry["entry"].get("type") == "assistant"
                            for block in (entry["entry"].get("message") or {}).get("content", []) if block.get("type") == "text"]
            assert any(markers[index] in text for text in native_texts), native
            assert psql(f"SELECT count(*) FROM session_inputs WHERE session_id={sql_literal(sessions[index])}") == ["2"]
            assert plan["workspace_volume"] == ""
            box = psql(f"SELECT sandbox_id FROM sessions WHERE session_id={sql_literal(sessions[index])}")[0]
            assert _docker_compute.durable_mounts("sandbox-" + box) == []
        # Drive a real supplier approval through the Satori internal answer API.
        owner = httpx.Client(base_url=bases[0], headers={"Authorization": "Bearer " + tokens[0]}, timeout=15)
        data(owner.post(f"/api/v1/sessions/{sessions[0]}/permission-mode", json={"permission_mode": permission_mode("gated")}))
        filename = "satori-approved-" + uuid.uuid4().hex + ".txt"
        send(0, f"Use the Write tool to create {filename} in the workspace with exactly SATORI_APPROVED. Do not use Bash or any other tool.")
        interaction = wait(lambda: detail(0).get("pending_interaction"), "native tool approval")
        assert interaction["presentation"] == "tool_approval", interaction
        call(0, "internal/astrabox.interaction.answer", {"channel_id": sessions[0],
             "interaction_id": interaction["interaction_id"], "answer": {"decision": decision("approve")}})
        completed(0, second_responses[0])
        contents = wait_for_file(owner, sessions[0], filename)
        assert contents is not None and contents.strip() == b"SATORI_APPROVED", contents
        tools = [part for item in full_history(0)["messages"] for part in item["parts"]
                 if part["type"] == "dynamic-tool" or part["type"].startswith("tool-")]
        native_calls = [block for entry in transcript_entries(sessions[0]) if entry["entry"].get("type") == "assistant"
                        for block in (entry["entry"].get("message") or {}).get("content", []) if block.get("type") == "tool_use"]
        assert any(filename in json.dumps(block.get("input")) and any(part.get("toolCallId") == block["id"] and part.get("state") == "output-available" for part in tools) for block in native_calls)
        print("Satori: native approval, file side effect and tool card agree", flush=True)
        # Stop an actual long native command; acceptance must not report completion.
        data(owner.post(f"/api/v1/sessions/{sessions[1]}/permission-mode", json={"permission_mode": permission_mode("unattended")}))
        send(1, "Run Bash with the exact command sleep 60. Do not use any other tool and do not reply before the command finishes.")
        wait(lambda: any(part.get("type") == "tool-input-available" and "sleep 60" in json.dumps(part.get("input"))
                         for event in feeds[0].events if event.get("_type") == "astrabox.output" and event["channel"]["id"] == sessions[1]
                         for part in event["_data"]["parts"]), "native long command")
        call(0, "internal/astrabox.response.stop", {"channel_id": sessions[1]})
        wait(lambda: (detail(1).get("last_response") or {}).get("outcome") == "cancelled", "cancelled outcome")
        data(e2e_client.post(f"/api/v1/admin/satori-fixture/revoke/{principals[0]}/{agent}"))
        assert call(0, "internal/astrabox.preparation.get", {})["ready"] is False
        count = psql(f"SELECT count(*) FROM session_inputs WHERE session_id={sql_literal(sessions[0])}")
        call(0, "message.create", {"channel_id": sessions[0], "content": "Must not execute"}, key=uuid.uuid4().hex, expect=409)
        assert psql(f"SELECT count(*) FROM session_inputs WHERE session_id={sql_literal(sessions[0])}") == count
        data(e2e_client.post(f"/api/v1/admin/satori-fixture/refuse/{principals[1]}/{agent}"))
        refused = send(2, "This accepted input must be refused by the configured preparation provider.")
        wait(lambda: any(part.get("type") == "data-input-failed" for event in feeds[1].events
                         if event.get("_type") == "astrabox.output" for part in event["_data"]["parts"]), "failed input event")
        failed = next(item for item in full_history(2)["messages"] if item["id"] == refused["id"])
        assert failed["metadata"]["state"] == "failed", failed
        turns = [row for row in data(e2e_client.get("/api/v1/admin/satori-fixture"))["turns"]
                 if row["session_id"] in sessions]
        assert all(row["principal_id"] == principals[sessions.index(row["session_id"])//2] and row["granted"] for row in turns)
        assert set(row["session_id"] for row in turns) == set(sessions)
        wait(lambda: any(event.get("_type") == "astrabox.state" and (event["_data"].get("response") or {}).get("outcome") == "cancelled" for event in feeds[0].events), "cancelled event")
        output = Path(os.environ["ASTRABOX_E2E_OUTPUT_DIR"])
        output.mkdir(parents=True, exist_ok=True)
        (output/"satori-evidence.json").write_text(json.dumps({"sessions": sessions, "first_response_ids": first_responses,
            "second_response_ids": second_responses, "first_events": first_events, "replayed": feeds[0].events,
            "other_employee_events": feeds[1].events, "turns": turns, "interaction": interaction}, indent=2))
        owner.close()
    finally:
        for feed in feeds:
            feed.close()


def _solid_png(rgb: tuple[int, int, int], size: int = 16) -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def test_satori_attachments_reach_the_engine_and_read_back_across_replicas(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    """Uploaded files reach the native engine with a message and read back from either replica.

    The image reaches the supplier as an image block and the CSV as a workspace
    file; the reply's own file is a resource too. Neither another user nor
    another endpoint's caller can read or send them.
    """
    import base64
    import re

    plan = json.loads(Path(os.environ["ASTRABOX_E2E_SATORI_PLAN"]).read_text())
    agent, bases, tokens = (plan[key] for key in ("agent_id", "bases", "tokens"))
    other, owner = 0, 1  # The fixture grants principal 1 already; 0 only uploads and reads.
    image, table = _solid_png((255, 0, 0)), b"item,amount\nalpha,2\nbeta,4\n"

    def headers(actor, key=None):
        values = {"Authorization": "Bearer " + tokens[actor], "Satori-Platform": "astrabox", "Satori-User-ID": agent}
        return values | ({"Idempotency-Key": key} if key else {})

    def remaining():
        return max(1, min(20, live_test_deadline - time.monotonic()))

    def call(actor, action, body, *, key=None, replica=0, expect=200):
        response = httpx.post(f"{bases[replica]}/api/satori/agents/{agent}/v1/{action}",
                              headers=headers(actor, key), json=body, timeout=remaining())
        assert response.status_code == expect, (action, response.status_code, response.text[:500])
        return response.json()

    def upload(actor, files, *, replica):
        response = httpx.post(f"{bases[replica]}/api/satori/agents/{agent}/v1/upload.create",
                              headers=headers(actor), files=files, timeout=remaining())
        assert response.status_code == 200, response.text[:500]
        return response.json()

    def proxy(actor, link, *, replica, expect=200):
        response = httpx.get(f"{bases[replica]}/api/satori/agents/{agent}/v1/proxy/{link}",
                             headers={"Authorization": "Bearer " + tokens[actor]}, timeout=remaining())
        assert response.status_code == expect, (link, response.status_code, response.text[:300])
        return response

    def links(content, tag):
        return re.findall(rf'<{tag} src="([^"]+)"(?: title="([^"]*)")?/>', content)

    sid = call(owner, "channel.create", {"guild_id": agent, "data": {"type": 0}}, key=uuid.uuid4().hex)["id"]
    staged = upload(owner, [("picture", ("red.png", image, "image/png")),
                            ("table", ("data.csv", table, "text/csv"))], replica=1)
    assert set(staged) == {"picture", "table"} and all(
        link.startswith(f"internal:astrabox/{agent}/_tmp/") for link in staged.values()), staged
    assert proxy(owner, staged["picture"], replica=0).content == image
    proxy(other, staged["picture"], replica=0, expect=404)
    foreign = upload(other, [("note", ("note.txt", b"not yours", "text/plain"))], replica=0)["note"]
    refused = [
        (f'Read <file src="{foreign}"/>', 409, "INPUT_ATTACHMENT_UNAVAILABLE"),
        (f'Look <img src="{staged["table"]}"/>', 400, "INPUT_ATTACHMENT_UNSUPPORTED"),
        ('Look <img src="https://example.com/red.png"/>', 400, "INPUT_ATTACHMENT_UNSUPPORTED"),
    ]
    for content, status, code in refused:
        result = call(owner, "message.create", {"channel_id": sid, "content": content},
                      key=uuid.uuid4().hex, expect=status)
        assert result["code"] == code, result
    assert not psql_rows(f"SELECT input_id FROM session_inputs WHERE session_id={sql_literal(sid)}")

    key = uuid.uuid4().hex
    content = ("The image is a solid color and the CSV lists amounts. Use your Write tool to create result.txt "
               "containing only the total of the amount column, then reply with the color's name and that total."
               f'<img src="{staged["picture"]}"/><file src="{staged["table"]}" title="data.csv"/>')
    sent = call(owner, "message.create", {"channel_id": sid, "content": content}, key=key, replica=1)
    assert call(owner, "message.create", {"channel_id": sid, "content": content}, key=key)[0]["id"] == sent[0]["id"]
    inputs = psql_rows(f"SELECT input_id FROM session_inputs WHERE session_id={sql_literal(sid)}")
    assert len(inputs) == 1, inputs
    input_id = inputs[0]["input_id"]
    stored = psql_rows("SELECT kind, name, size FROM input_attachments "
                       f"WHERE session_id={sql_literal(sid)} AND input_id={sql_literal(input_id)} ORDER BY ordinal")
    assert [(row["kind"], row["name"], row["size"]) for row in stored] == [
        ("image", "red.png", len(image)), ("file", "data.csv", len(table))], stored

    def ended():
        view = call(owner, "internal/astrabox.conversation.get", {"channel_id": sid})
        assert view["state"] != "TERMINATED", view
        response = view.get("last_response") or {}
        return response if response.get("status") == "ended" and not view.get("held_inputs") else None

    while not (response := ended()):
        assert time.monotonic() < live_test_deadline, "the reply did not end"
        time.sleep(0.5)
    assert response["outcome"] == "completed", response

    history = call(owner, "message.list", {"channel_id": sid}, replica=1)["data"]
    person = next(item for item in history if item["id"] == sent[0]["id"])
    reply = next(item for item in reversed(history) if item["user"]["id"] == agent)
    sent_images, sent_files = links(person["content"], "img"), links(person["content"], "file")
    prefix = f"internal:astrabox/{agent}/{sid}/attachments/"
    assert len(sent_images) == 1 and sent_images[0][0].startswith(prefix), person
    assert len(sent_files) == 1 and sent_files[0] == (sent_files[0][0], "data.csv") and sent_files[0][0].startswith(prefix)
    assert proxy(owner, sent_images[0][0], replica=0).content == image
    assert proxy(owner, sent_files[0][0], replica=1).content == table
    proxy(other, sent_files[0][0], replica=0, expect=404)
    assert "red" in reply["content"].lower() and "6" in reply["content"], reply
    written = [link for link, title in links(reply["content"], "file") if title == "result.txt"]
    assert written and written[0].startswith(f"internal:astrabox/{agent}/{sid}/files/"), reply
    assert proxy(owner, written[0], replica=0).content.strip() == b"6"

    listing = call(owner, "internal/astrabox.files.list", {"channel_id": sid, "path": f"uploads/{input_id}"})
    delivered = {entry["name"]: entry["url"] for entry in listing["entries"] if entry["kind"] == "file"}
    assert set(delivered) == {"red.png", "data.csv"}, listing
    assert proxy(owner, delivered["red.png"], replica=1).content == image
    assert proxy(owner, delivered["data.csv"], replica=0).content == table

    # The supplier itself received the image as an image block, not a path in text.
    encoded = base64.b64encode(image).decode()
    native = [row["entry"] for row in transcript_entries(sid) if row["entry"].get("type") == "user"]
    assert any(isinstance(entry.get("message", {}).get("content"), list) and any(
        block.get("type") == "image" and block.get("source", {}).get("data") == encoded
        for block in entry["message"]["content"]) for entry in native), "no native image block"
    print(json.dumps({"phase": "satori-attachments-accepted", "session_id": sid, "input_id": input_id,
                      "response_id": response["response_id"]}), flush=True)
