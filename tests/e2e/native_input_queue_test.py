"""Busy input follows Claude's native consumption and response boundaries."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e import _sandbox_helpers as h
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = [pytest.mark.e2e, pytest.mark.deployment_shape]


def test_native_busy_input_is_consumed_before_result_and_survives_reattach(
    e2e_client: httpx.Client, live_test_deadline: float,
) -> None:
    client = e2e_client
    assert h.engine_kind() == "claude_code"
    created = h.create_session(client, permission_mode=h.permission_mode("unattended"))
    sid = created["session_id"]
    nonce = uuid.uuid4().hex
    paths = [f"native_queue_{nonce}_{suffix}" for suffix in ("a", "b")]
    markers = [f"NATIVE_{nonce}_{suffix}" for suffix in ("A", "B")]

    def wait(read, description):
        while time.monotonic() < live_test_deadline:
            value = read()
            if value:
                return value
            time.sleep(0.25)
        raise AssertionError(description)

    def release(name):
        out = h.data(client.post(
            f"/api/v1/sessions/{sid}/files/upload", data={"path": ""},
            files={"files": (name + ".release", b"release", "text/plain")},
        ))
        assert out["uploaded_count"] == 1

    def native_blocks():
        return [block for row in h.transcript_entries(sid) if not row["subpath"]
                for block in (row["entry"].get("message") or {}).get("content", [])
                if isinstance(block, dict)]

    try:
        h.poll_until_agent_ready(client, sid)
        sandbox = h.get_session(client, sid)["sandbox_id"]
        kubeconfig, namespace = h.kube_access()
        pods = json.loads(subprocess.check_output([
            "kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
            "get", "pods", "-o", "json",
        ], text=True))["items"]
        owned = [p for p in pods if any(o.get("kind") == "BatchSandbox" and o.get("name") == sandbox
                                       for o in p["metadata"].get("ownerReferences", []))]
        assert len(owned) == 1
        assert not h.durable_mounts_of_sandbox(
            pod=owned[0]["metadata"]["name"], kubeconfig=kubeconfig, namespace=namespace,
        ), "native input custody must not depend on a persistent workspace volume"
        commands = []
        for name, marker in zip(paths, markers, strict=True):
            started = h.workspace_path(client, sid, name + ".started")
            released = h.workspace_path(client, sid, name + ".release")
            commands.append("sh -c " + shlex.quote(
                f"printf started > {shlex.quote(started)}; "
                f"while [ ! -f {shlex.quote(released)} ]; do sleep 0.1; done; "
                f"printf {shlex.quote(marker)}"
            ))
        first_prompt = (
            "Run exactly this Bash command in the foreground with timeout 180000. "
            "The operator will release it. Do not create release files or use background tasks. "
            f"After it returns, follow any newer user instruction: {commands[0]}"
        )
        first = h.send_and_follow(client, sid, first_prompt, until="output")
        assert h.wait_for_file(client, sid, h.workspace_path(client, sid, paths[0] + ".started")) == b"started"
        second_prompt = (
            "After the current command returns, run exactly this Bash command in the foreground "
            "with timeout 180000. The operator will release it; do not create release files or "
            f"use background tasks. Then reply with its output and finish: {commands[1]}"
        )
        second = h.send_input(client, sid, second_prompt)
        wait(lambda: h.recorded_input(sid, second["input_id"])["delivered_at"], "busy input was not delivered")
        before = h.recorded_input(sid, second["input_id"])
        assert not any(p["type"] == "data-result" for p in h.recorded_output(sid, first.response_id))
        print(json.dumps({"stage": "before_reattach", "second_input_state": before["state"]}), flush=True)

        server = require_service_container(SERVER_CONTAINER_HANDLE)
        subprocess.run(["docker", "restart", "--time", "30", server], check=True, capture_output=True, timeout=120)
        def healthy():
            try:
                return client.get("/healthz", timeout=2).status_code == 200
            except httpx.TransportError:
                return False
        wait(healthy, "platform did not restart")
        assert h.get_session(client, sid)["sandbox_id"] == sandbox
        release(paths[0])
        assert h.wait_for_file(client, sid, h.workspace_path(client, sid, paths[1] + ".started")) == b"started"
        h.follow_stream(client, first.stream,
            lambda stream: any(p.get("type") == "tool-input-available" and p.get("input", {}).get("command") == commands[1]
                               for response in stream.responses.values() for p in response.parts),
            describe="the native queued input's running tool")
        active = h.recorded_response(sid, first.response_id)
        assert set(active["input_ids"]) == {first.input_id, second["input_id"]}, active
        assert active["status"] == "running", active
        assert not any(p["type"] == "data-result" for p in h.recorded_output(sid, first.response_id)), (
            "a post-Result second turn does not prove native busy-input consumption"
        )
        assert any(set(p.get("messageMetadata", {}).get("input_ids", [])) == set(active["input_ids"])
                   for p in first.response.parts if p.get("type") == "message-metadata"), (
            "the live response must announce every native-consumed input it answers"
        )
        cold_active = h.history(client, sid)
        assert {m["id"]: h.message_text(m) for m in h.user_messages(cold_active)} == {
            first.client_message_id: first_prompt, second["client_message_id"]: second_prompt,
        }
        calls = wait(lambda: found if len(found := [b for b in native_blocks() if b.get("type") == "tool_use"]) == 2 else None,
                     "native SessionStore did not preserve both tool calls")
        assert len(calls) == 2 and {b["input"]["command"] for b in calls} == set(commands), calls
        for call in calls:
            shown = [p for m in cold_active for p in m["parts"] if p.get("toolCallId") == call["id"]]
            assert len(shown) == 1 and shown[0]["input"] == call["input"], shown
            expected = "output-available" if call["input"]["command"] == commands[0] else "input-available"
            assert shown[0]["state"] == expected, shown
        print(json.dumps({"stage": "pre_result", "native_tools": 2, "consumed_inputs": 2, "results": 0}), flush=True)

        release(paths[1])
        h.follow_reply(client, first, until="end")
        h.wait_until_settled(client, sid)
        assert first.error is None and markers[1] in first.text, first.stream.summary()
        native = native_blocks()
        cold = h.history(client, sid)
        assert len({m["id"] for m in cold}) == len(cold)
        assert len(h.user_messages(cold)) == 2
        assert {b["id"] for b in native if b.get("type") == "tool_use"} == {b["id"] for b in calls}
        for kind, field, ui_kind in (("text", "text", "text"), ("thinking", "thinking", "reasoning")):
            visible = "".join(p.get("text", "") for m in h.assistant_messages(cold)
                              for p in m["parts"] if p.get("type") == ui_kind)
            for block in native:
                if block.get("type") == kind:
                    assert block[field] in visible, (kind, block)
        for call in calls:
            results = [b for b in native if b.get("type") == "tool_result" and b.get("tool_use_id") == call["id"]]
            assert len(results) == 1 and not results[0].get("is_error"), results
            marker = markers[commands.index(call["input"]["command"])]
            assert marker in json.dumps(results[0])
            live = [p for p in first.response.parts if p.get("type") == "tool-output-available" and p.get("toolCallId") == call["id"]]
            shown = [p for m in cold for p in m["parts"] if p.get("toolCallId") == call["id"]]
            assert len(live) == len(shown) == 1
            assert marker in json.dumps(live[0]) and marker in json.dumps(shown[0])
            assert shown[0]["state"] == "output-available"
        for input_id in (first.input_id, second["input_id"]):
            assert h.recorded_input(sid, input_id)["state"] == "admitted"
            assert h.answer_in_history(cold, input_id)["id"] == first.response_id
        final = h.recorded_response(sid, first.response_id)
        assert final["outcome"] == "completed" and set(final["input_ids"]) == {first.input_id, second["input_id"]}, final
        print(json.dumps({"passed": 1, "selected": 1, "native_pre_result_inputs": 2,
                          "native_live_cold_tool_results": 2, "same_sandbox_after_reattach": True,
                          "persistent_workspace_volume": False,
                          "second_input_state_at_restart": before["state"]}), flush=True)
    finally:
        h.release_session(sid)
