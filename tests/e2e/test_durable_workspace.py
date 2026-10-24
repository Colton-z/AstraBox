"""Native conversation recovery and optional file persistence after compute loss.

The Assistant case checks native SQLite custody in the platform database,
then resumes the same conversation on a volume-free replacement. It reads the
engine's original message rows, not a planted file or the browser projection.
The separate Agent file case checks the optional persistent workspace volume.
Fault injection removes physical compute without an orderly hibernation save;
recovery is driven through the existing wake and conversation APIs.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
import uuid

import httpx
import pytest

from tests.e2e import _docker_compute

from tests.e2e._sandbox_helpers import (
    assert_database_is_the_live_stack,
    conversation_workspace_source,
    create_agent_variant_session,
    data,
    destroy_sandbox_pod,
    destroy_box,
    durable_mounts_of_sandbox,
    get_admin_session_detail,
    history,
    kube_access,
    poll_until_ready,
    release_assistant,
    release_session,
    run_terminal,
    pod_uid,
    read_through_the_medium,
    send_and_follow,
    user_messages,
    wait_until_settled,
)
from tests.e2e.test_conversation_end import _assistant_runtime, _wake_assistant
from tests.e2e._service_containers import SERVER_CONTAINER_HANDLE, require_service_container

pytestmark = pytest.mark.e2e

_READ_NATIVE_SQLITE = """
import json, sqlite3
def native_history(path, native_session_id):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        assert [row[0] for row in con.execute('PRAGMA integrity_check')] == ['ok']
        tables = [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        assert 'sessions' in tables and 'messages' in tables, tables
        session = con.execute('SELECT id FROM sessions WHERE id=?', (native_session_id,)).fetchone()
        # Hermes stores display_identity as a BLOB. Keep every column and
        # every byte in the JSON probe so recovery still compares full rows.
        messages = [{key: {'sqlite_blob_hex': value.hex()} if isinstance(value, bytes) else value
                     for key, value in dict(row).items()} for row in con.execute(
            'SELECT * FROM messages WHERE session_id=? ORDER BY id', (native_session_id,)
        )]
        return {'session_exists': session is not None, 'messages': messages, 'tables': tables}
"""

_READ_ASSISTANT_DATABASE = _READ_NATIVE_SQLITE + """
import asyncio, sys, tempfile
from dataclasses import asdict
from pathlib import Path
from astrabox.deploy.onebox import ensure_database_wiring, needs_sandbox_server, _export_backend_wiring
ensure_database_wiring()
if needs_sandbox_server():
    _export_backend_wiring()
from astrabox.bootstrap import bootstrap
bootstrap()
from astrabox.persistence.repository.session_repository import SessionRepository
from astrabox.persistence.repository.assistant_workspace_repository import AssistantWorkspaceRepository
from astrabox.persistence.repository.runtime_state_snapshot_repository import (
    RuntimeStateOwner, RuntimeStateSnapshotRepository,
)
async def main():
    session = await SessionRepository().get_session(sys.argv[1])
    assert session and session['session_id'] == sys.argv[1], 'Session not in server database'
    ref = session['workspace_ref']
    assert ref['kind'] == 'assistant' and ref['assistant_id'] == sys.argv[2], ref
    owner = RuntimeStateOwner(ref['user_id'], ref['kind'], ref['assistant_id'], ref['engine_kind'])
    assert owner.user_id == session['user_id'] and owner.engine_kind == 'assistant'
    workspace = await AssistantWorkspaceRepository().get_workspace(owner.subject_id)
    assert workspace and workspace['assistant_id'] == owner.subject_id
    assert workspace['engine_kind'] == owner.engine_kind
    native_session_id = session['engine_session_key']
    assert native_session_id
    snapshot = await RuntimeStateSnapshotRepository().load(owner)
    result = {'owner': asdict(owner), 'native_session_id': native_session_id, 'saved': snapshot is not None}
    result['current_sandbox_id'] = workspace['current_sandbox_id']
    result['config_dir'] = workspace['runtime_identity']['config_dir']
    if snapshot is not None:
        assert snapshot.payload.startswith(b'SQLite format 3\\x00'), 'Snapshot is not native state.db'
        with tempfile.TemporaryDirectory(prefix='astrabox-e2e-native-') as temporary:
            path = Path(temporary) / 'state.db'
            path.write_bytes(snapshot.payload)
            result.update(native_history(path, native_session_id))
        result.update(snapshot_id=snapshot.snapshot_id, sha256=snapshot.sha256, size=snapshot.size)
    print('ASTRABOX_NATIVE_STATE=' + json.dumps(result, sort_keys=True))
asyncio.run(main())
"""


def _native_result(output: str) -> dict:
    matches = re.findall(r"^ASTRABOX_NATIVE_STATE=(.+)$", output, flags=re.MULTILINE)
    assert len(matches) == 1, f"native state probe returned no unique result: {output[-600:]!r}"
    return json.loads(matches[0])


def _assistant_snapshot(
    session_id: str, assistant_id: str, *, prompt: str, deadline: float
) -> dict:
    """Read actual repository bytes until the completed native turn reaches custody."""
    server = require_service_container(SERVER_CONTAINER_HANDLE)
    last: dict = {}
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", server, "python", "-c", _READ_ASSISTANT_DATABASE,
             session_id, assistant_id],
            capture_output=True, text=True, timeout=min(30.0, max(0.1, deadline - time.monotonic())),
            check=True,
        )
        last = _native_result(result.stdout)
        messages = last.get("messages") or []
        users = [index for index, row in enumerate(messages)
                 if row.get("role") == "user" and row.get("content") == prompt]
        if users:
            assert len(users) == 1, "the same explicit input was recorded twice in native history"
            if any(row.get("role") == "assistant" and row.get("content")
                   for row in messages[users[0] + 1:]):
                assert last.get("saved") and last.get("session_exists"), last
                return last
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    pytest.fail(f"complete native Assistant turn never reached platform DB custody: {last}")


def _destroy_assistant_box(
    *, pod: str, sandbox_id: str, kubeconfig: str, namespace: str, deadline: float
) -> str:
    """Check the owned Pod, then inject the shared abrupt whole-sandbox loss."""
    if _docker_compute.selected():
        return _docker_compute.kill(sandbox_id, workload=pod)
    command = ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace]

    def kubectl(*args: str) -> str:
        return subprocess.run(
            [*command, *args], capture_output=True, text=True, check=True,
            timeout=min(30.0, max(0.1, deadline - time.monotonic())),
        ).stdout

    observed = json.loads(kubectl("get", "pod", pod, "-o", "json"))
    metadata = observed["metadata"]
    owners = [owner for owner in metadata.get("ownerReferences", [])
              if owner.get("kind") == "BatchSandbox" and owner.get("controller") is True]
    assert len(owners) == 1 and owners[0]["name"] == sandbox_id, metadata
    owner = owners[0]
    assert owner["apiVersion"].split("/")[0] == "sandbox.opensandbox.io", owner
    resource = "batchsandboxes.sandbox.opensandbox.io"
    batch = json.loads(kubectl("get", resource, sandbox_id, "-o", "json"))
    assert batch["metadata"]["uid"] == owner["uid"], "sandbox owner identity changed"
    destroy_box(sandbox_id)
    return str(metadata["uid"])


def _probe(client: httpx.Client, session_id: str, script: str) -> tuple[str, str]:
    """Run `script` in whatever box serves this conversation; return (pod, output).

    The Pod name and the reading come back from ONE terminal call, because
    apart they are a window. Resolved separately, a read can land on the box
    that is still dying while the name came from the one that replaced it — or
    the reverse — and the case that matters most is exactly the one that would
    pass: the marker is still on the disk that is about to disappear.
    """

    stdout, stderr, exit_code, _events = run_terminal(
        client, session_id, f'echo "ASTRABOX_POD=$HOSTNAME"; {script}'
    )
    found = re.search(r"ASTRABOX_POD=(\S+)", stdout)
    pod = found.group(1) if found else ""
    assert pod, (
        f"the box would not name itself: exit={exit_code} "
        f"stdout={stdout[:300]!r} stderr={stderr[:300]!r}"
    )
    return pod, stdout


def _assert_the_box_is_this_conversation_alone(
    client: httpx.Client, session_id: str
) -> None:
    """Refuse to destroy a box that is carrying somebody else's conversation.

    Under ``agent`` tenancy one box holds every conversation of one Agent, and
    the kill below is a node failure, not a session end: it takes every
    conversation in that box down with it. Sharing the matrix Agent with the
    rest of the lane therefore made this test inject box loss into whatever
    else was mid-response — the failure surfaces in the victim, as a response whose
    engine link died, so nothing points back here. Each case now runs on an
    Agent of its own; this asserts the isolation instead of assuming it, so a
    future change that re-shares the box fails here rather than in a stranger.

    Counted from inside the box because that is where the fact lives: placing
    a conversation creates its user, so the ``conv_`` accounts ARE the box's
    occupants. Asking the control plane would answer about the placements it
    knows of, which is the thing under test.
    """

    _pod, listing = _probe(
        client, session_id, "getent passwd | grep -c '^conv_' || true"
    )
    counted = re.findall(r"^\s*(\d+)\s*$", listing, flags=re.MULTILINE)
    assert counted, f"the box would not count its conversations: {listing[:300]!r}"
    occupants = int(counted[-1])
    assert occupants == 1, (
        f"this box carries {occupants} conversations, and destroying it would "
        "take the others down mid-response; the test must own the box it kills"
    )


def _live_box(client: httpx.Client, session_id: str) -> str:
    """The Pod serving this conversation, after proving it can answer a message.

    READY is not enough to ask the box anything. Measured twice on a settled
    pool: a session that had just reached READY answered the terminal with
    `SANDBOX_GONE ... state='Pending'`, before anything had been destroyed.
    What READY carries is that a message may be sent, so an answered message
    is what establishes the box before its name is asked for — the same
    ordering the product gives a user, who talks before they open a shell.
    """

    answered = send_and_follow(client, session_id, "Reply with just OK.", until="end")
    assert answered.error is None and answered.finish_reason == "stop", (
        f"a READY conversation could not answer a message: finish={answered.finish_reason!r} "
        f"error={answered.error!r}"
    )
    wait_until_settled(client, session_id)
    pod, _output = _probe(client, session_id, "true")
    return pod


def _read_after_recovery(
    client: httpx.Client,
    session_id: str,
    *,
    destroyed_uid: str,
    script: str,
) -> str:
    """Talk the conversation back onto a box and read `script` there.

    A message is what brings it back. Nothing recovers a lost box on a timer —
    not this platform, not OpenSandbox, whose Pool keeps a buffer count and no
    record of which member a sandbox borrowed, and not the counterparts
    (AgentScope Runtime re-creates lazily on `connect()`; Open Managed Agents
    recovers orphaned fibers when a request or alarm wakes the object). The
    platform holds the message until a runtime can take it and gives the
    conversation a new box to resume on, so ONE message must be answered:
    a refusal, a failed response or a hang is the defect.

    ``destroyed_uid`` is the Pod object the answer must NOT come from. Compared
    by uid rather than by name, because a controller-owned box is recreated
    under the same name and still is a different box.
    """

    resumed = send_and_follow(client, session_id, "Reply with just OK.", until="end")
    assert resumed.error is None and resumed.finish_reason == "stop", (
        f"the conversation did not answer after its box was destroyed: "
        f"finish={resumed.finish_reason!r} error={resumed.error!r} outcome={resumed.outcome!r}"
    )
    settled = wait_until_settled(client, session_id)
    assert settled["last_response"]["response_id"] == resumed.response_id, settled
    assert settled["last_response"]["outcome"] == "completed", settled
    pod, output = _probe(client, session_id, script)
    kubeconfig, namespace = kube_access()
    assert pod_uid(pod=pod, kubeconfig=kubeconfig, namespace=namespace) != destroyed_uid, (
        f"the conversation answered from the destroyed Pod {pod!r}; nothing moved"
    )
    return output


@pytest.mark.assistant_live
def test_an_assistant_remembers_its_conversations_after_its_box_is_destroyed(
    e2e_client: httpx.Client,
    live_test_deadline: float,
) -> None:
    """A volume-free replacement resumes the original native history held by the DB."""

    assistant_id = ""
    session_id = ""
    token = f"DURABLE-{uuid.uuid4().hex[:6].upper()}"
    first_prompt = f"My project reference is {token}. Remember it for this conversation. Acknowledge briefly without tools."
    next_prompt = "What project reference did I give you earlier? Reply with that reference only, without tools."
    try:
        (
            environment,
            image,
            _digest,
            model,
            kubeconfig,
            namespace,
        ) = _assistant_runtime(e2e_client)
        assistant = data(
            e2e_client.post(
                "/api/v1/assistants",
                json={
                    "display_name": f"__e2e_durable_{uuid.uuid4().hex[:8]}",
                    "runtime_profile_name": environment,
                    "model_config_override": {"model_name": model},
                },
            )
        )
        assistant_id = str(assistant.get("assistant_id") or "")
        assert assistant_id
        workspace = _wake_assistant(
            e2e_client, assistant_id, timeout=max(0.1, live_test_deadline - time.monotonic())
        )
        old_sandbox = str(workspace.get("current_sandbox_id") or "")
        assert old_sandbox

        conversation = data(
            e2e_client.post(f"/api/v1/assistants/{assistant_id}/conversations", json={})
        )
        session_id = str(conversation.get("session_id") or "")
        assert session_id
        poll_until_ready(e2e_client, session_id, expected_image=image)

        planted = send_and_follow(e2e_client, session_id, first_prompt, until="end")
        assert planted.error is None, f"the planting response failed: {planted.error}"
        assert planted.n_text_delta > 0 and planted.text.strip(), "the first response produced no reply"
        settled = wait_until_settled(e2e_client, session_id)
        assert settled["last_response"]["response_id"] == planted.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        # The conversation as its owner reads it, before the box is lost: the
        # loss and the recovery must leave every message of it as it was.
        history_before = history(e2e_client, session_id)
        assert [m["id"] for m in user_messages(history_before)] == [planted.client_message_id], (
            history_before
        )
        detail = get_admin_session_detail(e2e_client, session_id)
        native_session_id = str(detail.get("engine_session_key") or "")
        assert native_session_id and detail.get("sandbox_id") == old_sandbox, detail
        pod, _output = _probe(e2e_client, session_id, "true")
        assert not durable_mounts_of_sandbox(
            pod=pod, kubeconfig=kubeconfig, namespace=namespace
        ), "Assistant core recovery must start without a PVC"

        assert_database_is_the_live_stack(session_id)
        before = _assistant_snapshot(
            session_id, assistant_id, prompt=first_prompt, deadline=live_test_deadline
        )
        assert before["native_session_id"] == native_session_id, before
        assert before["current_sandbox_id"] == old_sandbox, before
        original_messages = before["messages"]
        assert all(row["session_id"] == native_session_id for row in original_messages)

        destroyed_uid = _destroy_assistant_box(
            pod=pod, sandbox_id=old_sandbox,
            kubeconfig=kubeconfig,
            namespace=namespace,
            deadline=live_test_deadline,
        )
        replacement = _wake_assistant(
            e2e_client, assistant_id, timeout=max(0.1, live_test_deadline - time.monotonic())
        )
        new_sandbox = str(replacement.get("current_sandbox_id") or "")
        assert new_sandbox and new_sandbox != old_sandbox, replacement
        assert history(e2e_client, session_id) == history_before, (
            "losing the box changed the conversation's history"
        )

        resumed = send_and_follow(e2e_client, session_id, next_prompt, until="end")
        assert resumed.error is None, f"same-conversation native resume failed: {resumed.error}"
        assert resumed.n_text_delta > 0 and token in resumed.text, (
            f"replacement answered without the original conversation context: {resumed.text!r}"
        )
        settled = wait_until_settled(e2e_client, session_id)
        assert settled["last_response"]["response_id"] == resumed.response_id, settled
        assert settled["last_response"]["outcome"] == "completed", settled
        history_after = history(e2e_client, session_id)
        assert history_after[: len(history_before)] == history_before, (
            "the resumed conversation changed or dropped its earlier messages"
        )
        assert [m["id"] for m in user_messages(history_after)] == [
            planted.client_message_id,
            resumed.client_message_id,
        ], [m["id"] for m in user_messages(history_after)]
        recovered = get_admin_session_detail(e2e_client, session_id)
        assert recovered.get("sandbox_id") == new_sandbox, recovered
        assert recovered.get("engine_session_key") == native_session_id, recovered
        after = _assistant_snapshot(
            session_id, assistant_id, prompt=next_prompt, deadline=live_test_deadline
        )
        assert after["owner"] == before["owner"] and after["native_session_id"] == native_session_id
        assert after["current_sandbox_id"] == new_sandbox, after
        config_dir = str(after["config_dir"])
        assert config_dir.startswith("/home/conversations/") and config_dir.endswith("/.hermes"), after
        read_native = _READ_NATIVE_SQLITE + """
import sys
from pathlib import Path
home = Path(sys.argv[1])
receipt = json.loads((home / 'astrabox-runtime-state-restored.json').read_text())
result = native_history(home / 'state.db', sys.argv[2])
result['restored'] = receipt
print('ASTRABOX_NATIVE_STATE=' + json.dumps(result, sort_keys=True))
"""
        new_pod, output = _probe(
            e2e_client, session_id,
            f"/opt/hermes/.venv/bin/python -c {shlex.quote(read_native)} "
            f"{shlex.quote(config_dir)} {shlex.quote(native_session_id)}",
        )
        assert pod_uid(pod=new_pod, kubeconfig=kubeconfig, namespace=namespace) != destroyed_uid
        assert not durable_mounts_of_sandbox(
            pod=new_pod, kubeconfig=kubeconfig, namespace=namespace
        ), "replacement Assistant must not recover through a PVC"
        native = _native_result(output)
        assert native["session_exists"]
        assert native["restored"]["owner"] == before["owner"], native["restored"]
        assert native["restored"]["sandbox_id"] == new_sandbox, native["restored"]
        assert native["restored"]["snapshot_id"], "Hermes started without a saved native snapshot"
        assert native["messages"][:len(original_messages)] == original_messages, (
            "replacement changed or dropped original native message rows"
        )
        assert [row["content"] for row in native["messages"] if row["role"] == "user"] == [
            row["content"] for row in original_messages if row["role"] == "user"
        ] + [next_prompt], "native resume duplicated or lost an explicit input"
        assert after["messages"] == native["messages"], "resumed native history did not reach the DB"
    finally:
        release_session(session_id)
        release_assistant(assistant_id)


def test_an_agent_conversation_keeps_its_files_after_its_box_is_destroyed(
    e2e_client: httpx.Client,
    live_test_deadline: float,
) -> None:
    """The Agent half of the promise, and the half it deliberately excludes.

    An Agent's conversation is durable through the transcript mirror, so what
    the medium has to keep is the user's workspace — and its engine config
    deliberately stays on the box, which this asserts so the two products'
    different answers stay different on purpose.
    """

    session_id = ""
    marker = f"DURABLE-{uuid.uuid4().hex[:6].upper()}"
    try:
        kubeconfig, namespace = kube_access()
        # An Agent of this test's own: the kill below is destructive to every
        # conversation in the box, and the matrix Agent's box is shared with
        # every other test running beside this one.
        session = create_agent_variant_session(
            e2e_client, name_suffix="durable-box", engine_options={}
        )
        session_id = str(session.get("session_id") or session.get("id") or "")
        assert session_id
        poll_until_ready(e2e_client, session_id)

        pod = _live_box(e2e_client, session_id)
        mounts = durable_mounts_of_sandbox(
            pod=pod, kubeconfig=kubeconfig, namespace=namespace
        )
        assert mounts, "this box carries no durable workspace volume"
        assert not [
            m for m in mounts if str(m.get("mountPath", "")).endswith(".claude")
        ], "an Agent conversation must not put its engine config on the medium"

        # Written and read back through the product's own terminal rather than
        # kubectl: what has to survive is a file the USER can still see, and
        # the path they see it through is this one. kubectl is kept for the two
        # things only it can do — reading the Pod's mounts, and killing it.
        # This conversation's own directory, not `mounts[0]`. A box packs
        # several conversations of the same Agent, so it carries several
        # workspace mounts and the first one belongs to whoever got there
        # first — the read below then finds a sibling's marker and reports it
        # as this conversation's file surviving.
        workspace = conversation_workspace_source(e2e_client, session_id)
        # Under a mount, not equal to one. A pool member mounts the Agent's
        # whole conversations root as a single volume and every conversation
        # gets a directory beneath it — which is also what lets a replacement
        # box read this conversation's files without the conversation itself
        # coming back.
        assert any(
            workspace == str(m.get("mountPath", "")).rstrip("/")
            or workspace.startswith(str(m.get("mountPath", "")).rstrip("/") + "/")
            for m in mounts
        ), (
            f"this conversation's workspace {workspace!r} is not under any of "
            f"the box's durable mounts: {[m.get('mountPath') for m in mounts]}"
        )
        _probe(
            e2e_client,
            session_id,
            f"printf '%s' {marker!r} > {workspace}/durable-probe.txt",
        )

        # Read BEFORE the kill: afterwards the object is gone and there is
        # nothing left to tell the replacement from the original.
        destroyed_uid = pod_uid(pod=pod, kubeconfig=kubeconfig, namespace=namespace)
        _assert_the_box_is_this_conversation_alone(e2e_client, session_id)
        destroy_sandbox_pod(pod=pod, kubeconfig=kubeconfig, namespace=namespace)
        # A pool member mounts the Agent's whole conversations root, so the
        # replacement can read this conversation's directory without the
        # conversation itself coming back.
        found = read_through_the_medium(
            kubeconfig=kubeconfig,
            namespace=namespace,
            script=f"cat {workspace}/durable-probe.txt 2>&1 || true",
            name_hint=pod.rsplit("-", 1)[0],
            not_uid=destroyed_uid,
            deadline=live_test_deadline,
        )
        assert marker in found, (
            f"the replacement box lost the conversation's files: {found[:200]!r}"
        )
    finally:
        release_session(session_id)


def test_a_conversation_comes_back_after_its_box_is_destroyed(
    e2e_client: httpx.Client,
) -> None:
    """The precondition both cases rest on, asserted once.

    If a destroyed box never carried its conversation back, both cases above
    would fail for a reason that has nothing to do with storage. Kept separate
    so that reason is readable when it happens — and asserted on an answer
    rather than on a Pod, because answering is what a user calls coming back.
    """

    session_id = ""
    try:
        kubeconfig, namespace = kube_access()
        # An Agent of this test's own, for the same reason as the case above.
        session = create_agent_variant_session(
            e2e_client, name_suffix="durable-recovery", engine_options={}
        )
        session_id = str(session.get("session_id") or session.get("id") or "")
        assert session_id
        poll_until_ready(e2e_client, session_id)
        pod = _live_box(e2e_client, session_id)

        _assert_the_box_is_this_conversation_alone(e2e_client, session_id)
        destroyed_uid = pod_uid(pod=pod, kubeconfig=kubeconfig, namespace=namespace)
        destroy_sandbox_pod(pod=pod, kubeconfig=kubeconfig, namespace=namespace)
        # Returning at all is the assertion: `_read_after_recovery` refuses an
        # answer from the destroyed Pod, so reaching here means the
        # conversation was carried onto another box and could speak from it.
        _read_after_recovery(
            e2e_client, session_id, destroyed_uid=destroyed_uid, script="echo RECOVERED"
        )
    finally:
        release_session(session_id)
