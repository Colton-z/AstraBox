"""Shared helpers for the live e2e suite.

Leading-underscore module, never collected as a test. It holds the
``{code,message,data}`` envelope unwrap, session creation and the READY poll,
the file and terminal APIs, the conversation as the console drives it (send an
input, follow the session's stream, answer, interrupt, wait until settled, read
history) and reads of the facts the platform records in its database, so the
per-capability test files stay readable and use one path for each.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest

from tests.e2e._service_containers import (
    POSTGRES_CONTAINER_HANDLE,
    require_service_container,
)
from tests.e2e._engine_profile import (
    agent_id as agent_id,
    approval_presentation as approval_presentation,
    child_completion as child_completion,
    contract_supported as contract_supported,
    current_profile,
    decision as decision,
    engine_kind as engine_kind,
    instruction as instruction,
    mirror_entry_types as mirror_entry_types,
    permission_mode as permission_mode,
    tool_name as tool_name,
    tool_names as tool_names,
)

READY_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_READY_TIMEOUT", "180"))
STREAM_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_STREAM_TIMEOUT", "240"))
SETTLE_TIMEOUT_S = float(os.getenv("ASTRABOX_E2E_SETTLE_TIMEOUT", "120"))

#: Session states a conversation does not come back from.
TERMINAL_STATES = {"TERMINATED", "DELETED"}


def data(resp: httpx.Response, *, expect: int = 200) -> dict:
    """Unwrap the ``{"code","message","data"}`` envelope, asserting the status.

    ``expect`` is a parameter rather than "any 2xx" on purpose. Which status a
    route answers is part of what it promises — a creation answering 200 is a
    defect worth failing on — so widening the default here would drop that
    assertion for every route to make room for one. A caller that expects
    something other than 200 says which, and keeps being held to it.
    """
    assert resp.status_code == expect, (
        f"{resp.request.method} {resp.request.url} -> {resp.status_code}: {resp.text[:400]}"
    )
    body = resp.json()
    assert body.get("code") == "OK", f"non-OK envelope: {body}"
    return body.get("data") or {}


def create_session(client: httpx.Client, *, permission_mode: str | None = None) -> dict:
    # The deployment evidence names the exact Agent this engine's matrix run
    # owns. Falling back to the first row would turn a missing fixture into a
    # successful run against another engine.
    profile = current_profile()
    agents = data(client.get("/api/v1/agents"))
    assert isinstance(agents, list), f"Agent list is not a list: {agents!r}"
    matches = [
        item
        for item in agents
        if item.get("agent_id") == profile["agent_id"]
        and item.get("name") == profile["agent_name"]
        and item.get("runtime_profile_name") == profile["runtime_profile_name"]
        and item.get("model") == profile["model"]
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected the configured {profile['engine_kind']!r} Investment Agent "
            f"exactly once, found {len(matches)}"
        )
    runtime_profiles = data(client.get("/api/v1/admin/runtime-profiles"))
    assert isinstance(runtime_profiles, list), (
        f"RuntimeProfile list is not a list: {runtime_profiles!r}"
    )
    environment_matches = [
        item
        for item in runtime_profiles
        if item.get("name") == profile["runtime_profile_name"]
        and item.get("engine_kind") == profile["engine_kind"]
        and item.get("runtime_template_name") == profile["image"]
    ]
    if len(environment_matches) != 1:
        raise AssertionError(
            "the selected Agent's live RuntimeProfile no longer matches its release "
            f"evidence: engine={profile['engine_kind']!r} "
            f"environment={profile['runtime_profile_name']!r} image={profile['image']!r} "
            f"matches={len(environment_matches)}"
        )
    agent_id = str(matches[0]["agent_id"])
    started = data(client.post(f"/api/v1/agents/{agent_id}/conversations", json={}))
    sid = started["session_id"]
    if permission_mode is not None:
        _set_permission_mode_once_settable(client, sid, permission_mode)
    detail = get_session(client, sid)
    assert detail.get("engine_kind") == profile["engine_kind"], (
        f"selected Agent started engine_kind={detail.get('engine_kind')!r}, "
        f"expected {profile['engine_kind']!r}"
    )
    return detail


def environment_with_tenancy(client: httpx.Client, tenancy: str) -> str:
    """Name the deployment's RuntimeProfile with this sandbox tenancy, for this engine.

    Some contracts only exist under one tenancy — a box that belongs to one
    conversation dies with it, a box that belongs to an Agent does not — so a
    test proving one of them has to say which it needs rather than inherit
    whatever the campaign's matrix Agent happens to use.
    """
    profile = current_profile()
    runtime_profiles = data(client.get("/api/v1/admin/runtime-profiles"))
    assert isinstance(runtime_profiles, list), (
        f"RuntimeProfile list is not a list: {runtime_profiles!r}"
    )
    matches = [
        str(item.get("name") or "")
        for item in runtime_profiles
        if isinstance(item, dict)
        and item.get("sandbox_tenancy") == tenancy
        and item.get("engine_kind") == profile["engine_kind"]
        and item.get("runtime_template_name") == profile["image"]
        and item.get("enabled") is True
    ]
    assert matches, (
        f"the deployment has no enabled {profile['engine_kind']} RuntimeProfile with "
        f"sandbox_tenancy={tenancy!r} on image {profile['image']!r}; "
        f"available={[(i.get('name'), i.get('sandbox_tenancy')) for i in runtime_profiles]}"
    )
    return sorted(matches)[0]


def create_agent_variant_session(
    client: httpx.Client,
    *,
    name_suffix: str,
    engine_options: dict[str, object],
    permission_mode: str | None = None,
    runtime_profile_name: str | None = None,
) -> dict:
    """Start a conversation from an isolated variant of the matrix Agent.

    ``runtime_profile_name`` defaults to the matrix Agent's own. Pass one only when
    the test needs a property of that RuntimeProfile rather than of the Agent.
    """

    profile = current_profile()
    canonical_agent_id = str(profile.get("agent_id") or "").strip()
    assert canonical_agent_id, f"matrix profile has no agent_id: {profile!r}"
    canonical = data(client.get(f"/api/v1/agents/{canonical_agent_id}"))
    canonical_options = canonical.get("engine_options") or {}
    assert isinstance(canonical_options, dict), (
        f"matrix Agent engine_options is not an object: {canonical_options!r}"
    )
    payload: dict[str, object] = {
        "name": (
            f"{profile['agent_name']} {name_suffix} "
            f"{uuid.uuid4().hex[:10]}"
        ),
        "model": profile["model"],
        "runtime_profile_name": runtime_profile_name or profile["runtime_profile_name"],
        "engine_options": {**canonical_options, **engine_options},
        # Cold on purpose: these cases exercise a conversation's own start, and
        # the warm path has its own tests. Stated, not left to the deployment's
        # create-time default.
        "prepared_capacity": 0,
    }
    system = canonical.get("system")
    if isinstance(system, str) and system.strip():
        payload["system"] = system
    created = data(client.post("/api/v1/agents", json=payload))
    variant_agent_id = str(created.get("agent_id") or "").strip()
    assert variant_agent_id, f"variant Agent has no agent_id: {created}"
    release_agent(variant_agent_id)

    started = data(
        client.post(f"/api/v1/agents/{variant_agent_id}/conversations", json={})
    )
    sid = str(started.get("session_id") or "").strip()
    assert sid, f"variant Agent conversation has no session_id: {started}"
    release_session(sid)
    if permission_mode is not None:
        _set_permission_mode_once_settable(client, sid, permission_mode)
    detail = get_session(client, sid)
    assert detail.get("engine_kind") == profile["engine_kind"]
    return detail


def _set_permission_mode_once_settable(
    client: httpx.Client, sid: str, permission_mode: str, timeout: float = READY_TIMEOUT_S
) -> None:
    """Set the mode after the session stops being CREATING, not before.

    Starting a conversation returns while provisioning is still in flight, so
    this endpoint answers ``409 SESSION_BUSY`` until the session settles. A
    serial run usually wins that race; five workers contending for the same
    deployment lose it, which is how it surfaced at all.

    The API classifies the refusal ``retryable: true``, and this waits on the
    state the refusal is about rather than retrying blind — a session that never
    settles fails with what it was doing instead of exhausting a retry budget.
    """
    deadline = deadline_after(timeout)
    while time.monotonic() < deadline:
        detail = get_session(client, sid)
        state = str(detail.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} reached terminal state {state} before its permission "
                f"mode could be set (last_error={detail.get('last_error')!r})"
            )
        if state != "CREATING":
            data(
                client.post(
                    f"/api/v1/sessions/{sid}/permission-mode",
                    json={"permission_mode": permission_mode},
                )
            )
            return
        time.sleep(0.5)
    pytest.fail(
        f"session {sid} stayed CREATING until the deadline; never became settable"
    )


def get_session(client: httpx.Client, sid: str) -> dict:
    return data(client.get(f"/api/v1/sessions/{sid}"))


def get_admin_session_detail(client: httpx.Client, sid: str) -> dict:
    """Read operator-only runtime coordinates for fault-injection tests."""
    return data(client.get(f"/api/v1/admin/sessions/{sid}/detail"))


def run_terminal(
    client: httpx.Client, sid: str, command: str
) -> tuple[str, str, int | None, list[dict]]:
    """Run one command in the box through the platform's terminal SSE."""

    stdout: list[str] = []
    stderr: list[str] = []
    exit_code: int | None = None
    events: list[dict] = []
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/terminal/stream",
        json={"command": command},
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(90.0, connect=30.0),
    ) as resp:
        ctype = resp.headers.get("content-type", "")
        assert "text/event-stream" in ctype, (
            f"terminal did not return SSE (content-type={ctype!r}); "
            f"body={resp.read()[:400]!r}"
        )
        for raw in resp.iter_lines():
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            kind = event.get("type")
            if kind == "stdout":
                stdout.append(str(event.get("text") or ""))
            elif kind == "stderr":
                stderr.append(str(event.get("text") or ""))
            elif kind == "exit":
                raw_code = event.get("exit_code")
                exit_code = int(raw_code) if raw_code is not None else None
    return "".join(stdout), "".join(stderr), exit_code, events


def durable_mounts_of_sandbox(
    *, pod: str, kubeconfig: str, namespace: str
) -> list[dict]:
    """The workspace volume mounts the platform asked for on this box.

    Read off the live Pod rather than off the platform's plan: what was
    intended and what the sandbox backend actually mounted are two facts, and
    only the second one keeps a user's files.
    """

    spec = json.loads(
        subprocess.run(
            [
                "kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
                "get", "pod", pod, "-o", "json",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
    )["spec"]
    claims = {
        v["name"]
        for v in spec.get("volumes") or []
        if "persistentVolumeClaim" in v
    }
    for container in spec.get("containers") or []:
        if container.get("name") != "sandbox":
            continue
        return [
            m for m in container.get("volumeMounts") or [] if m.get("name") in claims
        ]
    raise AssertionError(f"Pod {pod} has no sandbox container")


def pod_uid(*, pod: str, kubeconfig: str, namespace: str) -> str:
    """The Pod object's uid, which is what tells a replacement from the original.

    A name cannot: a controller-owned box comes back under the SAME name, and a
    pool member's replacement shares the pool's name prefix. A deleted Pod also
    keeps reporting `Running` for a moment, so a reader that matches on name can
    read the very box it just destroyed and call the medium durable.
    """

    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
         "get", "pod", pod, "-o", "jsonpath={.metadata.uid}"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    uid = result.stdout.strip()
    if result.returncode != 0 or not uid:
        raise AssertionError(
            f"could not read the uid of Pod {pod}, so a later read could not "
            f"prove it came from a different box: rc={result.returncode} "
            f"{result.stderr[-200:]}"
        )
    return uid


def pod_uid_quietly(*, pod: str, kubeconfig: str, namespace: str) -> str:
    """`pod_uid` for a poll loop: a Pod that vanished mid-poll answers ``""``.

    Separate from `pod_uid` because the two want opposite things from a missing
    Pod. Before the kill its absence is a broken assumption; during the wait it
    is the expected outcome, and raising there would end the loop that is
    watching for it.
    """

    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
         "get", "pod", pod, "-o", "jsonpath={.metadata.uid}"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def read_through_the_medium(
    *,
    kubeconfig: str,
    namespace: str,
    script: str,
    name_hint: str,
    not_uid: str,
    deadline: float,
) -> str:
    """Run `script` in a box that mounts the medium, whichever box that is.

    Durability is a property of the MEDIUM, not of the box that wrote to it, so
    reading it back does not require the conversation to come back. That
    distinction is the whole reason this exists: resuming a conversation after
    box loss is a separate promise with its own defect (`engine recovery
    unrecoverable: engine anchor missing or malformed on snapshot`), and a
    storage test that waits for it is failing on someone else's contract.

    A cold-created box is recreated under its own name and mounts its subject's
    directories; a pool member mounts the Agent's whole conversations root and
    can therefore read any conversation under it. Either answers, so this takes
    the first Running Pod whose name contains `name_hint` and asks it.

    `deadline` is absolute and belongs to the caller, because the live E2E
    timeout is a fixed 180s for the whole test (`conftest.MAX_E2E_TEST_SECONDS`)
    and is deliberately not overridable. A duration argument here cannot know
    what the turn and the box kill already spent, and a budget that outlasts the
    test is not a budget: pytest-timeout kills the process mid-poll and the
    refusal below — the only thing that says what was seen — never prints.
    """

    command = ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace]
    started = time.monotonic()
    seen: set[str] = set()
    last = ""
    while time.monotonic() < deadline:
        listing = subprocess.run(
            # Raw, so `\n` reaches kubectl as the two characters its jsonpath
            # parser expects. A plain literal here hands it a real newline
            # inside the quotes, which it rejects — and the refusal is on
            # stderr with an empty stdout, so an unchecked call reads as a
            # cluster with no Pods running at all.
            [*command, "get", "pods", "-o",
             r"jsonpath={range .items[?(@.status.phase=='Running')]}{.metadata.name}{'\n'}{end}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if listing.returncode != 0:
            raise AssertionError(
                "listing Running Pods failed, so this loop has nothing to poll: "
                f"rc={listing.returncode} {listing.stderr[-300:]}"
            )
        running = listing.stdout.split()
        seen.update(running)
        for pod in running:
            if name_hint not in pod:
                continue
            if pod_uid_quietly(pod=pod, kubeconfig=kubeconfig, namespace=namespace) == not_uid:
                # The box under test, still reporting Running on its way out.
                # Reading the planted file from it would pass while proving
                # nothing: that box is the one that wrote it.
                continue
            done = subprocess.run(
                [*command, "exec", pod, "-c", "sandbox", "--", "bash", "-lc", script],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if done.returncode == 0:
                print(f"[medium] read from {pod} after {time.monotonic() - started:.0f}s")
                return done.stdout
            last = f"{pod}: rc={done.returncode} {done.stderr[-200:]}"
        time.sleep(5)
    matched = sorted(name for name in seen if name_hint in name)
    raise AssertionError(
        f"no Running box matching {name_hint!r} other than the destroyed "
        f"{not_uid!r} answered the read within "
        f"{time.monotonic() - started:.0f}s; matching pods seen: {matched or 'none'}; "
        f"all Running pods seen: {sorted(seen)}; last attempt: {last or 'none made'}"
    )



def destroy_sandbox_pod(*, pod: str, kubeconfig: str, namespace: str) -> None:
    """Kill the box the way a node failure would, and return.

    Not `hibernate` and not `terminate`: those are orderly, and an orderly stop
    is the case durable storage is NOT needed for — a park commits the whole
    root filesystem, so the files come back either way. What has to hold is the
    disorderly one: the box disappears and something else still has the files.

    Waiting for a replacement is the caller's, because the replacement is not
    this Pod under another status. A pool-borrowed box is replaced by a
    different pool member with a different name, so "the same Pod came back" is
    a condition only a cold-created box can meet. The condition that holds for
    both is the platform's: the conversation reaches READY again, on whatever
    box it landed on.
    """

    subprocess.run(
        [
            "kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
            "delete", "pod", pod, "--force", "--grace-period=0",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def kube_access() -> tuple[str, str]:
    """The deployment's cluster access: kubeconfig path and sandbox namespace."""
    kubeconfig = os.getenv("ASTRABOX_E2E_KUBECONFIG", "").strip()
    namespace = os.getenv("ASTRABOX_E2E_KUBE_NAMESPACE", "").strip()
    assert kubeconfig and namespace, (
        "destroying a box needs the deployment's cluster access "
        "(ASTRABOX_E2E_KUBECONFIG, ASTRABOX_E2E_KUBE_NAMESPACE)"
    )
    return kubeconfig, namespace


def destroy_box(sandbox_id: str) -> None:
    """Remove a box the way losing its node would: its BatchSandbox and Pod are gone.

    Nothing orderly happens first — no terminate, no hibernate — so the
    platform learns of the loss only from the runtime's connection and the
    control plane. Every process in the box is killed outright before the
    BatchSandbox is deleted: a deleted Pod's containers get its 30 s grace
    period, in which the egress stops serving the box's credentials while the
    engine still runs, and an engine that reports what it saw then (a model
    call refused) is not a box that vanished. Returns once neither the
    BatchSandbox nor a Pod it owned exists, so what the test then observes is
    the platform reacting to a box that is really gone. The Pods are found by
    their owner reference, as :func:`assert_kubernetes_runtime_image` finds them.
    """
    kubeconfig, namespace = kube_access()
    command = ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace]
    resource = "batchsandboxes.sandbox.opensandbox.io"

    def kubectl_json(*args: str) -> dict:
        done = subprocess.run(
            [*command, *args, "-o", "json"], capture_output=True, text=True, timeout=30, check=False
        )
        assert done.returncode == 0, f"kubectl {' '.join(args)} failed: {done.stderr[-400:]}"
        return json.loads(done.stdout)

    batch_uid = str((kubectl_json("get", resource, sandbox_id).get("metadata") or {}).get("uid") or "")
    assert batch_uid, f"BatchSandbox {sandbox_id} has no uid"

    def owned_pods() -> list[str]:
        return [
            str((item.get("metadata") or {}).get("name"))
            for item in kubectl_json("get", "pods").get("items") or []
            if any(
                owner.get("kind") == "BatchSandbox" and owner.get("uid") == batch_uid
                for owner in (item.get("metadata") or {}).get("ownerReferences") or []
            )
        ]

    pods = owned_pods()
    assert pods, f"BatchSandbox {sandbox_id} owns no Pod; this is not the box the session runs in"
    for pod in pods:
        for container in (kubectl_json("get", "pod", pod).get("spec") or {}).get("containers") or []:
            # SIGKILL to every process but the container's init, which then has
            # nothing left to run; the Pod's deletion follows at once.
            killed = subprocess.run(
                [*command, "exec", pod, "-c", str(container["name"]), "--", "sh", "-c", "kill -9 -1"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            # Killing the container's processes may also kill the exec session.
            assert killed.returncode in (0, 137), (
                f"could not kill the processes of {pod}/{container['name']}: {killed.stderr[-400:]}"
            )
    deleted = subprocess.run(
        [*command, "delete", resource, sandbox_id, "--cascade=foreground", "--wait=false"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert deleted.returncode == 0, f"could not delete BatchSandbox {sandbox_id}: {deleted.stderr[-400:]}"
    deadline = deadline_after(90.0)
    remaining: list[str] = []
    while time.monotonic() < deadline:
        batch = subprocess.run(
            [*command, "get", resource, sandbox_id, "--ignore-not-found", "-o", "name"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
        remaining = ([batch] if batch else []) + [pod for pod in owned_pods() if pod in pods]
        if not remaining:
            return
        time.sleep(0.5)
    pytest.fail(f"box {sandbox_id} was not physically removed: {remaining}")


def assert_kubernetes_runtime_image(
    *,
    sandbox_id: str,
    expected_image: str,
    expected_digest: str,
    kubeconfig: str,
    namespace: str,
) -> None:
    """Prove one live BatchSandbox Pod runs the manifest's exact image bytes."""

    assert sandbox_id and expected_image
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest)
    kubeconfig_path = Path(kubeconfig)
    assert kubeconfig_path.is_absolute() and kubeconfig_path.is_file()
    assert not kubeconfig_path.is_symlink()
    assert re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", namespace)

    command = ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace]
    batch_result = subprocess.run(
        [*command, "get", "batchsandbox", sandbox_id, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert batch_result.returncode == 0, (
        f"cannot read BatchSandbox {namespace}/{sandbox_id}: "
        f"{batch_result.stderr[-400:]}"
    )
    batch = json.loads(batch_result.stdout)
    metadata = batch.get("metadata") or {}
    batch_uid = str(metadata.get("uid") or "")
    assert metadata.get("name") == sandbox_id and batch_uid, (
        f"BatchSandbox identity drifted for {namespace}/{sandbox_id}: {metadata}"
    )

    pod_result = subprocess.run(
        [*command, "get", "pods", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert pod_result.returncode == 0, (
        f"cannot list Pods in {namespace}: {pod_result.stderr[-400:]}"
    )
    pod_inventory = json.loads(pod_result.stdout)
    items = pod_inventory.get("items") if isinstance(pod_inventory, dict) else None
    assert isinstance(items, list), f"Pod inventory has no items: {pod_inventory}"
    pods = []
    for item in items:
        if not isinstance(item, dict):
            continue
        owners = (item.get("metadata") or {}).get("ownerReferences") or []
        if any(
            isinstance(owner, dict)
            and owner.get("kind") == "BatchSandbox"
            and owner.get("name") == sandbox_id
            and owner.get("uid") == batch_uid
            for owner in owners
        ):
            pods.append(item)
    assert len(pods) == 1, (
        f"BatchSandbox {namespace}/{sandbox_id} owns {len(pods)} Pods"
    )
    pod = pods[0]
    pod_metadata = pod.get("metadata") or {}
    pod_name = str(pod_metadata.get("name") or "")
    assert pod_name and pod_metadata.get("deletionTimestamp") is None
    assert (pod.get("status") or {}).get("phase") == "Running"
    containers = [
        value
        for value in (pod.get("spec") or {}).get("containers") or []
        if isinstance(value, dict) and value.get("name") == "sandbox"
    ]
    assert len(containers) == 1 and containers[0].get("image") == expected_image, (
        f"Pod {pod_name!r} does not run sandbox image {expected_image!r}"
    )
    statuses = [
        value
        for value in (pod.get("status") or {}).get("containerStatuses") or []
        if isinstance(value, dict) and value.get("name") == "sandbox"
    ]
    assert len(statuses) == 1
    image_id = statuses[0].get("imageID")
    assert isinstance(image_id, str)
    match = re.search(r"(?:^|@|://)(sha256:[0-9a-f]{64})$", image_id)
    assert match and match.group(1) == expected_digest, (
        f"Pod {pod_name!r} sandbox imageID {image_id!r} does not prove "
        f"{expected_digest!r}"
    )


def running_sandbox_image(*, endpoint: str) -> str:
    """The image of the Pod actually serving `endpoint`.

    Asked of the Pod rather than of the control plane, because the control
    plane cannot answer for every box shape. OpenSandbox reads a sandbox's
    image out of its workload's Pod template
    (`opensandbox_server/services/k8s/workload_mapper.py`), and a pool-borrowed
    box has no workload of its own — its Pod belongs to the Pool. The mapper
    then reports the literal ``"unknown"``, which is a true statement about
    what it knows and useless as proof of what is running.

    The Pod is the one place the answer is not derived. A direct endpoint is
    that Pod's IP, so it addresses the box directly and works the same for a
    cold-created box and a borrowed one; an ingress gateway endpoint is
    resolved to the Pod the gateway serves.

    ``""`` when no Pod serves that address. Waiting belongs to the caller,
    which is the only side that can re-ask the platform WHERE the box is: an
    endpoint read once goes stale when the Pool replaces the member, and
    retrying against the stale address just waits out a budget for a Pod that
    is never coming back.
    """

    kubeconfig = os.getenv("ASTRABOX_E2E_KUBECONFIG", "").strip()
    namespace = os.getenv("ASTRABOX_E2E_KUBE_NAMESPACE", "").strip()
    assert kubeconfig and namespace, (
        "proving a sandbox image needs Kubernetes access; set "
        "ASTRABOX_E2E_KUBECONFIG and ASTRABOX_E2E_KUBE_NAMESPACE"
    )
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    assert host, f"sandbox endpoint {endpoint!r} names no host to resolve"

    # `-o json` and pair the two fields inside one Pod object. A multi-field
    # jsonpath emits each field as its own range, so a Pod missing either one
    # shifts the columns and the pairing silently reads one Pod's address
    # against another's image.
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
         "get", "pods", "-o", "json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, (
        f"cannot list Pods to resolve sandbox endpoint {endpoint!r}: "
        f"{result.stderr[-300:]}"
    )
    seen: dict[str, str] = {}
    for item in json.loads(result.stdout).get("items") or []:
        pod_ip = str(((item.get("status") or {}).get("podIP") or "")).strip()
        if not pod_ip:
            continue
        for container in (item.get("spec") or {}).get("containers") or []:
            if container.get("name") == "sandbox":
                seen[pod_ip] = str(container.get("image") or "").strip()
    if host in seen:
        return seen[host]
    # An OpenSandbox ingress gateway endpoint in uri mode is
    # `<gateway>/<sandbox id>/<port>`, so its host is the gateway and no Pod
    # has it. The gateway forwards to the first address the sandbox's
    # BatchSandbox records in `sandbox.opensandbox.io/endpoints`; resolving the
    # same record finds the Pod the gateway serves.
    route = [segment for segment in parsed.path.split("/") if segment]
    if len(route) < 2 or not route[1].isdigit():
        return ""
    return seen.get(_gateway_route_pod_address(route[0], kubeconfig, namespace), "")


def _gateway_route_pod_address(sandbox_id: str, kubeconfig: str, namespace: str) -> str:
    """The Pod address the ingress gateway routes ``sandbox_id`` to, or ``""``."""

    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "--namespace", namespace,
         "get", "batchsandboxes.sandbox.opensandbox.io", sandbox_id, "-o", "json"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode != 0 and "NotFound" in result.stderr:
        return ""
    assert result.returncode == 0, (
        f"cannot read BatchSandbox {sandbox_id!r} to resolve its gateway route: "
        f"{result.stderr[-300:]}"
    )
    annotations = (json.loads(result.stdout).get("metadata") or {}).get("annotations") or {}
    addresses = json.loads(annotations.get("sandbox.opensandbox.io/endpoints") or "[]")
    return str(addresses[0]).strip() if addresses else ""
#: What OpenSandbox reports for a sandbox whose workload carries no pod template
#: of its own. Every box lent from a pool is one, so this is the vendor's way of
#: saying "this sandbox cannot name its image", not an image called "unknown".
_POOLED_IMAGE_UNNAMEABLE = "unknown"


def assert_release_matches_the_box(
    client: httpx.Client, result: dict, *, sandbox_id: str, operation: str
) -> None:
    """Prove a reclaim's own report against the sandbox it names.

    ``killed`` is about the BOX, and a box is not always one conversation's: an
    Agent-shared box outlives the conversations placed in it, so releasing a
    placement reports ``killed=False`` and the box stays. Both answers are
    legitimate; what is not is either of them being untrue. A claimed
    destruction of a box the backend still describes as live, and retention of
    a box absent from the backend, both contradict the platform's report.
    """
    killed = result.get("killed")
    assert isinstance(killed, bool), (
        f"{operation} did not say whether the box itself died: {result}"
    )
    probe = client.get(
        f"/api/v1/admin/sandboxes/{sandbox_id}", params={"backend": "open_sandbox"}
    )
    if killed:
        assert probe.status_code == 404 or (
            probe.status_code == 200 and _sandbox_is_finished(probe)
        ), (
            f"{operation} reported the box destroyed, but the backend did not "
            f"confirm destruction: {probe.status_code} {probe.text[:400]}"
        )
    else:
        assert probe.status_code == 200 and not _sandbox_is_finished(probe), (
            f"{operation} reported the box retained for its owner, but the "
            f"backend does not have it: {probe.status_code} {probe.text[:400]}"
        )


def _sandbox_is_finished(response: httpx.Response) -> bool:
    """Whether the backend describes this sandbox as finished with."""
    if response.status_code != 200:
        return True
    payload = response.json()
    descriptor = payload.get("data") if isinstance(payload, dict) else None
    state = str((descriptor or {}).get("state") or "").strip().lower()
    return state in {"terminated", "failed", "succeed", "succeeded", "deleted"}


def poll_until_ready(
    client: httpx.Client,
    sid: str,
    timeout: float = READY_TIMEOUT_S,
    *,
    expected_image: str | None = None,
    sandbox_admin_client: httpx.Client | None = None,
) -> list[str]:
    """Poll as the Session reader; optionally use an admin for image inventory."""
    seen: list[str] = []
    deadline = deadline_after(timeout)
    while time.monotonic() < deadline:
        detail = get_session(client, sid)
        state = str(detail.get("state") or "")
        if not seen or seen[-1] != state:
            seen.append(state)
        if state == "READY":
            if expected_image is not None:
                sandbox_id = str(detail.get("sandbox_id") or "").strip()
                assert sandbox_id, f"READY session {sid} has no sandbox_id: {detail}"
                # The session's box, re-asked until a Pod answers for it. The
                # Pool replaces a member under a live session, so an endpoint
                # captured once can name a Pod that has already gone — and the
                # session then names a different box than the one being looked
                # for. Re-reading the descriptor follows the move.
                proof_deadline = time.monotonic() + 60.0
                running = ""
                descriptor: dict = {}
                while True:
                    detail = get_session(client, sid)
                    sandbox_id = str(detail.get("sandbox_id") or "").strip()
                    assert sandbox_id, f"READY session {sid} has no sandbox_id: {detail}"
                    descriptor = data(
                        (sandbox_admin_client if sandbox_admin_client is not None else client).get(
                            f"/api/v1/admin/sandboxes/{sandbox_id}",
                            params={"backend": "open_sandbox"},
                        )
                    )
                    endpoint = str(descriptor.get("endpoint") or "").strip()
                    assert endpoint, (
                        f"READY session {sid} has no sandbox endpoint to prove an "
                        f"image against: {descriptor}"
                    )
                    running = running_sandbox_image(endpoint=endpoint)
                    if running:
                        break
                    assert time.monotonic() < proof_deadline, (
                        f"no Pod served session {sid}'s sandbox {sandbox_id} "
                        f"(endpoint {endpoint}) within 60s"
                    )
                    time.sleep(3)
                assert running == expected_image, (
                    "the READY session uses an unexpected sandbox image: "
                    f"session={sid} expected={expected_image!r} "
                    f"actual={running!r} (descriptor said "
                    f"{descriptor.get('image')!r})"
                )
            return seen
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} reached terminal state {state} before READY "
                f"(last_error={detail.get('last_error')!r}); states={seen}"
            )
        time.sleep(0.3)
    pytest.fail(
        f"session {sid} did not reach READY in time; states={seen}; "
        f"{json.dumps(_session_facts(client, sid))[:1500]}"
    )


def box_is_running(*, sandbox_id: str, client: httpx.Client) -> bool:
    """Whether the control plane still reports this box as running.

    Asked of the platform's own inventory rather than Kubernetes, because what
    the retention promise is about is a box the platform will still hand to the
    Agent's other conversations.
    """

    response = client.get(
        f"/api/v1/admin/sandboxes/{sandbox_id}", params={"backend": "open_sandbox"}
    )
    if response.status_code != 200:
        return False
    descriptor = data(response)
    return str(descriptor.get("state") or "").strip().lower() == "running"


def environment_sandbox_tenancy(client: httpx.Client) -> str:
    """The tenancy this matrix row's RuntimeProfile is configured for.

    Read from the RuntimeProfile rather than assumed, because it is a per-deployment
    choice and not a property of a product line: the same Agent runs one box per
    conversation or one box per Agent depending on this field, and what ending a
    conversation destroys differs accordingly.
    """

    name = str(current_profile()["runtime_profile_name"])
    rows = data(client.get("/api/v1/admin/runtime-profiles"))
    matches = [item for item in rows if item.get("name") == name]
    assert len(matches) == 1, f"expected one RuntimeProfile named {name!r}, found {len(matches)}"
    tenancy = str(matches[0].get("sandbox_tenancy") or "").strip()
    assert tenancy, f"RuntimeProfile {name!r} declares no sandbox_tenancy: {matches[0]}"
    return tenancy


def poll_until_agent_ready(
    client: httpx.Client,
    sid: str,
    timeout: float = READY_TIMEOUT_S,
) -> list[str]:
    """Poll a matrix-owned Agent Session and prove its selected runtime image."""

    return poll_until_ready(
        client,
        sid,
        timeout,
        expected_image=str(current_profile()["image"]),
    )


def engine_capabilities(client: httpx.Client, sid: str) -> dict:
    """Return the live adapter manifest, proving it belongs to this matrix row."""

    detail = get_session(client, sid)
    capabilities = detail.get("engine_capabilities")
    assert isinstance(capabilities, dict), (
        f"session {sid} has no verified engine capability manifest: {detail}"
    )
    assert str(capabilities.get("engine_kind") or "") == engine_kind(), (
        "engine capability manifest belongs to a different adapter: "
        f"{capabilities}"
    )
    return capabilities


def assert_permission_mode_unavailable(client: httpx.Client, sid: str) -> None:
    """Prove a modeless engine refuses a permission mode with the named code."""

    assert contract_supported("permission_modes") is False
    capabilities = engine_capabilities(client, sid)
    assert capabilities.get("permission_modes") == [], (
        "the deployed engine advertises permission modes but its matrix contract "
        f"does not: {capabilities}"
    )
    response = client.post(
        f"/api/v1/sessions/{sid}/permission-mode",
        json={"permission_mode": "astrabox-e2e-unsupported-mode"},
    )
    try:
        body = response.json()
    except ValueError:
        body = {}
    assert response.status_code == 400 and body.get("code") == "ENGINE_CAPABILITY_UNAVAILABLE", (
        "a modeless engine must refuse permission-mode control explicitly: "
        f"{response.status_code} {response.text[:400]}"
    )


def assert_child_run_control_unavailable(client: httpx.Client, sid: str) -> None:
    """Prove this runtime does not claim child-run control."""

    assert contract_supported("background_subagent") is False
    capabilities = engine_capabilities(client, sid)
    assert capabilities.get("supports_child_run_control") is False, (
        "the deployed engine advertises child-run control but its matrix contract "
        f"does not: {capabilities}"
    )


# ── file API ─────────────────────────────────────────────────────────────────


def list_files(client: httpx.Client, sid: str, path: str | None = None) -> dict:
    return data(
        client.post(f"/api/v1/sessions/{sid}/files/list", json={"path": path} if path else {})
    )


def file_names(client: httpx.Client, sid: str, path: str | None = None) -> set[str]:
    return {str(e.get("name")) for e in list_files(client, sid, path).get("entries") or []}


def download(client: httpx.Client, sid: str, abspath: str) -> httpx.Response:
    return client.get(f"/api/v1/sessions/{sid}/files/download", params={"path": abspath})


def wait_for_file(client: httpx.Client, sid: str, abspath: str, timeout: float = 60.0) -> bytes | None:
    """Poll the file API until ``abspath`` downloads with 200; return its bytes or None."""
    name = abspath.rsplit("/", 1)[-1]
    deadline = deadline_after(timeout)
    while time.monotonic() < deadline:
        if name in file_names(client, sid):
            dl = download(client, sid, abspath)
            if dl.status_code == 200:
                return dl.content
        time.sleep(0.3)
    return None


# ── the conversation: send, follow, answer, interrupt, settle ─────────────────
#
# The console sends with ``POST /sessions/{id}/inputs`` and hears everything on
# ``GET /sessions/{id}/stream?after_seq=``. One HTTP response of that stream
# ends after the part that finishes a response (``finish`` or ``error``), and
# each burst of parts is followed by a ``data-resume-cursor`` carrying the
# sequence to reconnect from. A response opens with a ``start`` part whose
# ``messageMetadata`` names it (``response_id``), the inputs it answers
# (``input_ids``) and who started it (``origin``); a paused response reopens
# with a bare ``start`` naming the same ``messageId``.

#: Parts that show the runtime produced something for a response: model text
#: or reasoning, or a tool call. ``start`` is the platform's announcement, not
#: output.
OUTPUT_PART_TYPES = frozenset(
    {
        "text-start",
        "text-delta",
        "reasoning-start",
        "reasoning-delta",
        "tool-input-start",
        "tool-input-delta",
        "tool-input-available",
    }
)

#: Longer than the stream's 15s keep-alive, so a quiet stream never times out
#: a read while the server is alive; a dead connection still fails the read.
_STREAM_READ_TIMEOUT_S = 45.0

#: The monotonic time by which the running test's helpers give their verdict.
#: ``conftest.live_test_deadline`` sets it for every live test from the test's
#: own budget: pytest-timeout kills the process where it stands, so a helper
#: that waits up to the hard ceiling dies without saying what it saw.
TEST_DEADLINE: float | None = None


def deadline_after(timeout: float | None) -> float:
    """The earlier of ``timeout`` from now and the running test's own deadline."""
    candidates = [time.monotonic() + (STREAM_TIMEOUT_S if timeout is None else timeout)]
    if TEST_DEADLINE is not None:
        candidates.append(TEST_DEADLINE)
    return min(candidates)


@dataclass
class Response:
    """One response as the stream delivered it: its own parts, in order."""

    response_id: str
    origin: str | None = None
    input_ids: tuple[str, ...] = ()
    #: ``messageMetadata`` of its ``start`` and ``finish`` parts, merged.
    metadata: dict = field(default_factory=dict)
    parts: list[dict] = field(default_factory=list)
    text: str = ""
    n_text_delta: int = 0
    interactions: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    #: Every ``finish`` the response sent: ``tool-calls`` for each pause on an
    #: interaction, then the engine's own reason (or none) for its end.
    finish_reasons: list[str | None] = field(default_factory=list)
    error: str | None = None
    #: ``time.monotonic()`` when its ``start`` arrived and when its first text did.
    started_at: float = field(default_factory=time.monotonic)
    first_text_at: float | None = None
    first_text_wall: float | None = None
    ended_wall: float | None = None

    @property
    def finish_reason(self) -> str | None:
        return self.finish_reasons[-1] if self.finish_reasons else None

    @property
    def paused(self) -> bool:
        return bool(self.finish_reasons) and self.finish_reasons[-1] == "tool-calls"

    @property
    def ended(self) -> bool:
        return bool(self.finish_reasons) and self.finish_reasons[-1] != "tool-calls"

    @property
    def produced_output(self) -> bool:
        return any(part.get("type") in OUTPUT_PART_TYPES for part in self.parts)

    def take(self, part: dict) -> None:
        self.parts.append(part)
        kind = part.get("type")
        if kind == "text-delta":
            if self.first_text_at is None:
                self.first_text_at = time.monotonic()
                self.first_text_wall = time.time()
            self.n_text_delta += 1
            delta = part.get("delta")
            if isinstance(delta, str):
                self.text += delta
        elif kind == "data-interaction":
            record = part.get("data")
            assert isinstance(record, dict), f"an interaction part carries no record: {part}"
            self.interactions.append(record)
        elif kind == "tool-input-available":
            name = part.get("toolName")
            if name:
                self.tool_names.append(str(name))
        elif kind == "error":
            self.error = str(part.get("errorText") or "unknown error")
        elif kind == "finish":
            reason = part.get("finishReason")
            self.finish_reasons.append(str(reason) if reason is not None else None)
            if isinstance(part.get("messageMetadata"), dict):
                self.metadata.update(part["messageMetadata"])
            if reason != "tool-calls":
                self.ended_wall = time.time()

    def summary(self) -> dict:
        return {
            "response_id": self.response_id,
            "origin": self.origin,
            "input_ids": list(self.input_ids),
            "finish_reasons": self.finish_reasons,
            "error": self.error,
            "text": self.text[:200],
            "parts": len(self.parts),
        }


@dataclass
class SessionStream:
    """What the console's stream delivered for one session, response by response.

    ``cursor`` is the sequence to reconnect from; it only moves at a burst
    boundary, so stopping between two connections neither drops nor repeats a
    part. ``open_response`` is the response whose message segment is open at
    the cursor: its later parts arrive without another ``start``.
    """

    session_id: str
    cursor: int = 0
    responses: dict[str, Response] = field(default_factory=dict)
    input_failures: dict[str, dict] = field(default_factory=dict)
    #: Parts that belong to no open message (session-scoped engine output).
    outside: list[dict] = field(default_factory=list)
    open_response: str | None = None
    connections: int = 0
    saw_ui_header: bool = True
    #: An ``error`` outside any open message, held until a ``finish`` names
    #: the response it ended (see :meth:`_take`).
    #: ``data-session-changed`` parts: the session record changed (its title),
    #: which belongs to no response and is never stored with one.
    session_changes: int = 0

    def answering(self, input_id: str) -> Response | None:
        """The response whose ``start`` names this input, once it started."""
        for response in self.responses.values():
            if input_id in response.input_ids:
                return response
        return None

    def from_engine(self) -> list[Response]:
        """Responses the runtime started on its own, in the order they started."""
        return [r for r in self.responses.values() if r.origin == "engine"]

    def summary(self) -> dict:
        return {
            "cursor": self.cursor,
            "connections": self.connections,
            "responses": [r.summary() for r in self.responses.values()],
            "input_failures": self.input_failures,
            "outside_parts": [part.get("type") for part in self.outside],
            "session_changes": self.session_changes,
        }

    def _take(self, part: dict) -> None:
        kind = part.get("type")
        if kind == "start":
            message_id = str(part.get("messageId") or "")
            assert message_id, f"a start part names no message: {part}"
            metadata = part.get("messageMetadata")
            response = self.responses.get(message_id)
            if isinstance(metadata, dict):
                assert response is None, (
                    f"response {message_id} was announced twice on one stream: {part}"
                )
                assert metadata.get("response_id") == message_id, (
                    f"a start part's metadata names another response: {part}"
                )
                input_ids = metadata.get("input_ids")
                assert isinstance(input_ids, list), f"a start part lists no input_ids: {part}"
                response = Response(
                    response_id=message_id,
                    origin=str(metadata.get("origin") or "") or None,
                    input_ids=tuple(str(item) for item in input_ids),
                    metadata=dict(metadata),
                )
                self.responses[message_id] = response
            elif response is None:
                # A response announced before this stream's starting position,
                # reopening after a pause: nothing here says whose it is.
                response = Response(response_id=message_id)
                self.responses[message_id] = response
            response.parts.append(part)
            self.open_response = message_id
            return
        if kind == "data-session-changed":
            self.session_changes += 1
            return
        if kind == "data-input-failed":
            failure = part.get("data")
            assert isinstance(failure, dict) and failure.get("input_id"), (
                f"a refused input names no input: {part}"
            )
            self.input_failures[str(failure["input_id"])] = dict(failure)
            return
        if self.open_response is None:
            # Every response part follows its message's ``start``, including
            # the error and finish that close a response the runtime lost.
            self.outside.append(part)
            return
        self.responses[self.open_response].take(part)
        if kind == "finish":
            self.open_response = None


def _stream_parts(resp: httpx.Response, deadline: float, describe: str):
    """The stream's parts as dicts, failing loudly at the deadline or on a malformed part."""
    for raw in resp.iter_lines():
        if time.monotonic() > deadline:
            raise TimeoutError(describe)
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        part = json.loads(payload)
        assert isinstance(part, dict) and part.get("type"), (
            f"the session stream sent a part that is not a typed object: {payload[:300]}"
        )
        yield part


def follow_stream(
    client: httpx.Client,
    stream: SessionStream,
    until,
    *,
    describe: str,
    timeout: float | None = None,
) -> SessionStream:
    """Follow the session's stream from ``stream.cursor`` until ``until(stream)`` holds.

    ``until`` is checked at every burst boundary and when an HTTP response
    ends; the stream reconnects from the cursor after each response it
    finishes, as the console does. Fails with everything it saw when the
    deadline passes, with ``describe`` naming what it waited for.
    """
    deadline = deadline_after(timeout)
    sid = stream.session_id
    while True:
        if until(stream):
            return stream
        try:
            with client.stream(
                "GET",
                f"/api/v1/sessions/{sid}/stream",
                params={"after_seq": str(stream.cursor)},
                headers={"Accept": "text/event-stream"},
                timeout=httpx.Timeout(_STREAM_READ_TIMEOUT_S, connect=30.0),
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read()[:400]
                    raise AssertionError(
                        f"GET stream after_seq={stream.cursor} -> {resp.status_code}: {body!r}"
                    )
                content_type = resp.headers.get("content-type", "")
                assert "text/event-stream" in content_type, (
                    f"the session stream is not an event stream (content-type={content_type!r})"
                )
                stream.saw_ui_header = stream.saw_ui_header and (
                    resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"
                )
                stream.connections += 1
                for part in _stream_parts(resp, deadline, describe):
                    if part.get("type") == "data-resume-cursor":
                        seq = (part.get("data") or {}).get("seq")
                        assert isinstance(seq, int) and seq >= stream.cursor, (
                            f"a resume cursor went backwards or is not a sequence: {part} "
                            f"(cursor was {stream.cursor})"
                        )
                        stream.cursor = seq
                        if until(stream):
                            return stream
                        continue
                    stream._take(part)
        except TimeoutError:
            pass
        except httpx.TransportError as exc:
            raise AssertionError(
                f"session {sid}: the stream connection failed while waiting for {describe}: "
                f"{exc!r}; it had delivered {json.dumps(stream.summary())[:3000]}"
            ) from exc
        if until(stream):
            return stream
        if time.monotonic() > deadline:
            raise AssertionError(
                f"session {sid}: {describe} did not happen in time. The stream delivered "
                f"{json.dumps(stream.summary())[:3000]}; the session reads "
                f"{json.dumps(_session_facts(client, sid))[:1500]}"
            )


def _session_facts(client: httpx.Client, sid: str) -> dict:
    """What the session detail says about the runtime, for a failure message."""
    try:
        detail = get_session(client, sid)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must not hide the failure it explains
        return {"unreadable": str(exc)}
    return {
        key: detail.get(key)
        for key in (
            "state",
            "runtime_status",
            "current_responses",
            "held_inputs",
            "last_response",
            "pending_interaction",
            "last_error",
            "runtime_unavailable",
            "sandbox_id",
            "output_seq",
        )
    }


def output_position(client: httpx.Client, sid: str) -> int:
    """Where the session's output stands now: a page follows the stream from here."""
    seq = get_session(client, sid).get("output_seq")
    assert isinstance(seq, int) and seq >= 0, f"session {sid} names no output position: {seq!r}"
    return seq


def send_input(
    client: httpx.Client,
    sid: str,
    content: str | list,
    *,
    client_message_id: str | None = None,
    permission_mode: str | None = None,
    duplicate: bool = False,
) -> dict:
    """Send one message as the console does and return the platform's receipt.

    ``duplicate`` is what the receipt must say: a new ``client_message_id`` is
    a new input, the same one again is the earlier input's receipt.
    """
    body: dict = {"content": content, "client_message_id": client_message_id or str(uuid.uuid4())}
    if permission_mode is not None:
        body["permission_mode"] = permission_mode
    receipt = data(client.post(f"/api/v1/sessions/{sid}/inputs", json=body))
    assert receipt.get("session_id") == sid and receipt.get("input_id"), (
        f"the input receipt names no input of this session: {receipt}"
    )
    assert receipt.get("client_message_id") == body["client_message_id"], receipt
    assert receipt.get("duplicate") is duplicate, (
        f"expected duplicate={duplicate} for this send: {receipt}"
    )
    return receipt


@dataclass
class Reply:
    """A message the test sent and what the stream delivered for it."""

    session_id: str
    input_id: str
    client_message_id: str
    stream: SessionStream
    sent_at: float = 0.0
    sent_wall: float = 0.0

    @property
    def response(self) -> Response | None:
        return self.stream.answering(self.input_id)

    @property
    def input_failure(self) -> dict | None:
        return self.stream.input_failures.get(self.input_id)

    @property
    def response_id(self) -> str | None:
        return self.response.response_id if self.response else None

    @property
    def text(self) -> str:
        return self.response.text if self.response else ""

    @property
    def n_text_delta(self) -> int:
        return self.response.n_text_delta if self.response else 0

    @property
    def interactions(self) -> list[dict]:
        return self.response.interactions if self.response else []

    @property
    def tool_names(self) -> list[str]:
        return self.response.tool_names if self.response else []

    @property
    def finish_reason(self) -> str | None:
        return self.response.finish_reason if self.response else None

    @property
    def error(self) -> str | None:
        return self.response.error if self.response else None

    @property
    def paused(self) -> bool:
        return bool(self.response and self.response.paused)

    @property
    def ended(self) -> bool:
        return bool(self.response and self.response.ended)

    @property
    def outcome(self) -> str | None:
        """``failed`` when the platform closed a response the runtime lost; the
        engine's own endings carry their reason in ``finish_reason``."""
        return self.response.metadata.get("outcome") if self.response else None

    @property
    def saw_ui_header(self) -> bool:
        return self.stream.saw_ui_header and self.stream.connections > 0

    @property
    def cursor(self) -> int:
        return self.stream.cursor


def follow_reply(
    client: httpx.Client,
    reply: Reply,
    *,
    until: str = "pause",
    timeout: float | None = None,
    allow_refusal: bool = False,
) -> Reply:
    """Follow the stream until the response to ``reply``'s input next reaches ``until``.

    ``output``: the runtime produced text, reasoning or a tool call for it
    since this call began (or it paused or ended first); ``pause``: it paused
    on an interaction again, or ended; ``end``: it ended. A refused input
    (``data-input-failed``) fails the caller unless ``allow_refusal``: nothing
    will answer it.
    """
    assert until in ("output", "pause", "end"), until
    before = reply.response
    finishes_before = len(before.finish_reasons) if before else 0
    parts_before = len(before.parts) if before else 0

    def reached(stream: SessionStream) -> bool:
        if reply.input_id in stream.input_failures:
            return True
        response = stream.answering(reply.input_id)
        if response is None:
            return False
        if response.ended:
            return True
        paused_again = len(response.finish_reasons) > finishes_before
        if until == "pause":
            return paused_again
        if until == "output":
            return paused_again or any(
                part.get("type") in OUTPUT_PART_TYPES for part in response.parts[parts_before:]
            )
        return False

    follow_stream(
        client,
        reply.stream,
        reached,
        describe=f"the response to input {reply.input_id} reaching {until!r}",
        timeout=timeout,
    )
    if reply.input_failure is not None and not allow_refusal:
        raise AssertionError(
            f"session {reply.session_id} refused input {reply.input_id}: {reply.input_failure}"
        )
    return reply


def send_and_follow(
    client: httpx.Client,
    sid: str,
    content: str | list,
    *,
    until: str = "pause",
    client_message_id: str | None = None,
    permission_mode: str | None = None,
    timeout: float | None = None,
    allow_refusal: bool = False,
) -> Reply:
    """Send one message and follow the stream until its response reaches ``until``.

    The output position is read before sending, so the response to this input
    lies after it; anything else the stream delivers on the way (an earlier
    input's response, the runtime's own) is kept on ``reply.stream`` too.
    """
    stream = SessionStream(sid, cursor=output_position(client, sid))
    sent_at, sent_wall = time.monotonic(), time.time()
    receipt = send_input(
        client, sid, content, client_message_id=client_message_id, permission_mode=permission_mode
    )
    reply = Reply(
        session_id=sid,
        input_id=str(receipt["input_id"]),
        client_message_id=str(receipt["client_message_id"]),
        stream=stream,
        sent_at=sent_at,
        sent_wall=sent_wall,
    )
    return follow_reply(client, reply, until=until, timeout=timeout, allow_refusal=allow_refusal)


def answer_interaction(client: httpx.Client, sid: str, interaction_id: str, answer: dict) -> dict:
    """Answer an open interaction as the console does; the platform must take it."""
    receipt = data(
        client.post(
            f"/api/v1/sessions/{sid}/interactions/{interaction_id}/answer",
            json={"answer": answer},
        )
    )
    assert receipt.get("answered") is True and receipt.get("interaction_id") == interaction_id, (
        f"the answer to {interaction_id} was not taken: {receipt}"
    )
    return receipt


def answer_interaction_refusal(
    client: httpx.Client, sid: str, interaction_id: str, answer: dict
) -> tuple[int, str]:
    """POST an answer that must be refused; ``(status, error code)`` without raising."""
    resp = client.post(
        f"/api/v1/sessions/{sid}/interactions/{interaction_id}/answer",
        json={"answer": answer},
    )
    if resp.status_code < 400:
        return resp.status_code, ""
    try:
        return resp.status_code, str(resp.json().get("code") or "")
    except ValueError:
        return resp.status_code, resp.text[:200]


def interrupt(client: httpx.Client, sid: str, *, expect: str = "accepted") -> dict:
    """Ask the conversation's runtime to stop, and hold the receipt to ``expect``.

    ``accepted``: a reader will pass the stop to the running runtime;
    ``idle``: nothing was running to stop.
    """
    receipt = data(client.post(f"/api/v1/sessions/{sid}/interrupt", timeout=30.0))
    assert receipt == {"session_id": sid, "status": expect}, (
        f"expected the interrupt to be {expect!r}: {receipt}"
    )
    return receipt


def pending_interaction(client: httpx.Client, sid: str) -> dict | None:
    pi = get_session(client, sid).get("pending_interaction")
    return pi if isinstance(pi, dict) and pi.get("interaction_id") else None


def wait_for_pending_interaction(
    client: httpx.Client, sid: str, *, exclude_id: str | None = None, timeout: float = 20.0
) -> dict | None:
    """Poll session detail for a pending interaction (optionally one whose id != ``exclude_id``)."""
    deadline = deadline_after(timeout)
    while time.monotonic() < deadline:
        pi = pending_interaction(client, sid)
        if pi and (exclude_id is None or str(pi.get("interaction_id")) != exclude_id):
            return pi
        time.sleep(0.5)
    return None


def is_settled(detail: dict) -> bool:
    """Whether the detail shows the conversation at rest.

    READY with no response running, no input the platform still holds and
    nothing waiting on the owner. The four are asked separately because the
    state is computed from the others and a disagreement is itself a defect.
    """
    current = detail.get("current_responses")
    held = detail.get("held_inputs")
    assert isinstance(current, list) and isinstance(held, list), (
        f"the session detail does not report its responses and held inputs: {detail}"
    )
    return (
        detail.get("state") == "READY"
        and not current
        and not held
        and not detail.get("pending_interaction")
    )


def wait_until_settled(client: httpx.Client, sid: str, timeout: float = SETTLE_TIMEOUT_S) -> dict:
    """Poll until the conversation is at rest (see :func:`is_settled`); return the detail.

    Fails loudly on a terminal state or at the deadline, with what the session
    was doing.
    """
    deadline = deadline_after(timeout)
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_session(client, sid)
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} went terminal ({state}) while settling: {last.get('last_error')!r}"
            )
        if is_settled(last):
            return last
        time.sleep(0.3)
    pytest.fail(
        f"session {sid} did not settle within its budget; "
        f"{json.dumps(_session_facts(client, sid))[:1500]}"
    )


# ── history ──────────────────────────────────────────────────────────────────


def history(client: httpx.Client, sid: str, *, page_size: int = 50) -> list[dict]:
    """The whole conversation as the console loads it: UI messages, oldest first."""
    pages: list[list[dict]] = []
    before: str | None = None
    while True:
        params: dict = {"limit": str(page_size)}
        if before:
            params["before"] = before
        page = data(client.get(f"/api/v1/sessions/{sid}/messages", params=params))
        messages = page.get("messages")
        assert isinstance(messages, list), f"history page carries no messages list: {page}"
        pages.append(messages)
        if not page.get("has_more"):
            break
        before = page.get("before")
        assert before, f"history says it has more but gives no cursor: {page}"
    return [message for chunk in reversed(pages) for message in chunk]


def message_text(message: dict) -> str:
    """The visible text of one UI message: its text parts, in order."""
    return "".join(
        str(part.get("text") or "")
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("type") == "text"
    )


def user_messages(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") == "user"]


def assistant_messages(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m.get("role") == "assistant"]


def answer_in_history(messages: list[dict], input_id: str) -> dict:
    """The one assistant message that answers ``input_id``; fails unless there is exactly one."""
    found = [
        m
        for m in assistant_messages(messages)
        if input_id in ((m.get("metadata") or {}).get("input_ids") or [])
    ]
    assert len(found) == 1, (
        f"history holds {len(found)} answers to input {input_id}: "
        f"{[(m.get('id'), (m.get('metadata') or {}).get('input_ids')) for m in assistant_messages(messages)]}"
    )
    return found[0]


# ── what the platform recorded (its PostgreSQL tables) ──────────────────────


def _postgres_container() -> str:
    return require_service_container(POSTGRES_CONTAINER_HANDLE)


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def psql(sql: str) -> list[str]:
    """Rows of one query against the deployment's own database, as psql prints them."""
    database = os.getenv("ASTRABOX_E2E_POSTGRES_DB", "astrabox")
    user = os.getenv("ASTRABOX_E2E_POSTGRES_USER", "astrabox")
    output = subprocess.check_output(
        [
            "docker", "exec", _postgres_container(),
            "psql", "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1",
            "-U", user, "-d", database, "-c", sql,
        ],
        text=True,
        timeout=20,
    )
    return [line for line in output.splitlines() if line]


def psql_rows(sql: str, *, order_by: str | None = None) -> list[dict]:
    """Rows of one query as dicts, ordered by the named output columns."""
    ordering = f" ORDER BY {order_by}" if order_by else ""
    return [json.loads(line) for line in psql(f"SELECT row_to_json(q)::text FROM ({sql}) q{ordering}")]


def assert_database_is_the_live_stack(sid: str, timeout: float = 15.0) -> None:
    """Prove the selected PostgreSQL container is the one the server writes."""
    deadline = deadline_after(timeout)
    while time.monotonic() < deadline:
        if psql(f"SELECT 1 FROM sessions WHERE session_id = {sql_literal(sid)}"):
            return
        time.sleep(1.0)
    pytest.fail(
        f"session {sid} is not in PostgreSQL container {_postgres_container()}; "
        "set ASTRABOX_E2E_POSTGRES_CONTAINER to the database the server uses"
    )


def transcript_entries(sid: str) -> list[dict]:
    """The runtime's own transcript the platform keeps for a session, in append order.

    Each row is one entry the runtime wrote (``entry`` decoded from its JSON),
    with the transcript file it belongs to (``subpath`` ``""`` for the main
    one). This is the custody a replacement runtime resumes from.
    """
    rows = psql_rows(
        "SELECT s.subpath, e.seq, e.entry_json FROM transcript_entries e "
        "JOIN transcript_scopes s ON s.scope_id = e.scope_id "
        f"WHERE s.platform_session_id = {sql_literal(sid)}",
        order_by="subpath, seq",
    )
    return [
        {"subpath": row["subpath"], "seq": row["seq"], "entry": json.loads(row["entry_json"])}
        for row in rows
    ]


def recorded_output(sid: str, response_id: str) -> list[dict]:
    """The browser parts the session's reader recorded for one response, in order."""
    return [
        row["part"]
        for row in psql_rows(
            "SELECT seq, part FROM session_output "
            f"WHERE session_id = {sql_literal(sid)} AND response_id = {sql_literal(response_id)}",
            order_by="seq",
        )
    ]


def recorded_input(sid: str, input_id: str) -> dict:
    """The platform's custody record of one input (``state``, ``failure``, times)."""
    rows = psql_rows(
        "SELECT input_id, state, response_id, failure, created_at, delivered_at, admitted_at "
        f"FROM session_inputs WHERE session_id = {sql_literal(sid)} AND input_id = {sql_literal(input_id)}"
    )
    assert len(rows) == 1, f"session {sid} has {len(rows)} records of input {input_id}"
    return rows[0]


def recorded_response(sid: str, response_id: str) -> dict:
    """What the reader recorded about one response, with the inputs it answers."""
    rows = psql_rows(
        "SELECT r.response_id, r.origin, r.status, r.outcome, r.native_reason, r.error, "
        "COALESCE((SELECT array_agg(i.input_id ORDER BY i.input_id) FROM session_response_inputs i "
        "WHERE i.session_id = r.session_id AND i.response_id = r.response_id), '{}') AS input_ids "
        f"FROM session_responses r WHERE r.session_id = {sql_literal(sid)} "
        f"AND r.response_id = {sql_literal(response_id)}"
    )
    assert len(rows) == 1, f"session {sid} has {len(rows)} records of response {response_id}"
    return rows[0]


def reader_lease(sid: str) -> dict | None:
    """Which process holds the session's reader lease, and its generation."""
    rows = psql_rows(
        "SELECT owner, generation, expires_at > now() AS live FROM session_readers "
        f"WHERE session_id = {sql_literal(sid)}"
    )
    return rows[0] if rows else None


#: Sessions this test opened. The reaper fixture in ``conftest`` empties it —
#: deleting them when the test PASSED, and leaving them running when it did not.
#: Per-process by design: under ``-n`` each xdist worker reaps only its own.
OPEN_SESSIONS: list[str] = []
OPEN_ASSISTANTS: list[str] = []
OPEN_AGENTS: list[str] = []


def release_session(sid: str) -> None:
    """Hand this session to the reaper. Deliberately does NOT delete it.

    Called from a test's ``finally``, which runs while the test is still in its
    call phase — pytest does not know yet whether it passed. Deleting here would
    therefore delete the sandbox of a FAILING test, which is the one thing worth
    keeping: a failure's session, its box, and its in-box state are the only
    reproducible scene, and a fresh run rarely lands in the same place.

    So the decision moves to :func:`conftest._reap_or_keep_sandboxes`, which runs
    after the report exists and can tell the two cases apart.
    """
    if sid and sid not in OPEN_SESSIONS:
        OPEN_SESSIONS.append(sid)


def release_assistant(assistant_id: str) -> None:
    """Hand a test-created Assistant to the outcome-aware reaper."""
    if assistant_id and assistant_id not in OPEN_ASSISTANTS:
        OPEN_ASSISTANTS.append(assistant_id)


def release_agent(agent_id: str) -> None:
    """Hand a test-created Agent to the outcome-aware reaper."""
    if agent_id and agent_id not in OPEN_AGENTS:
        OPEN_AGENTS.append(agent_id)


def delete_session(client: httpx.Client, sid: str) -> None:
    """Interrupt a working runtime (so DELETE isn't 409), then delete. Best effort.

    The reaper calls this for a passing test. A test that is ASSERTING deletion
    calls the API itself; this is teardown, not a fixture for that.
    """
    try:
        client.post(f"/api/v1/sessions/{sid}/interrupt", timeout=20.0)
    except Exception:
        pass
    try:
        client.delete(f"/api/v1/sessions/{sid}", timeout=20.0)
    except Exception:
        pass


def conversation_workspace_source(client: httpx.Client, sid: str) -> str:
    """Where THIS conversation's workspace physically lives inside its box.

    Not the same as `workspace_path`. That one is the `/workspace` a process
    inside the conversation's isolated session sees; this is the directory the
    box actually holds and the medium actually mounts, under the conversation's
    private home. The two name the same files only when the conversation owns
    its box outright.

    Needed by anything reading the box from OUTSIDE an isolated session — a
    `kubectl exec` lands in the box, not in the session, so `/workspace` there
    belongs to whichever conversation the box was built around rather than to
    this one. With several conversations packed into a box, reading the visible
    path finds a sibling's file and reports it as this conversation's.
    """

    identity = get_admin_session_detail(client, sid).get("runtime_identity") or {}
    source = str(identity.get("workspace_source_dir") or "").rstrip("/")
    assert source.startswith("/"), (
        f"session {sid} has no physical workspace source in its identity: {identity}"
    )
    return source


def workspace_path(client: httpx.Client, sid: str, filename: str) -> str:
    """An absolute path inside THIS session's workspace.

    Operator detail carries the stable ``/workspace`` contract in the private
    runtime identity. Read it there so the suite proves the deployed image and
    host agree without widening the owner-facing Session DTO.
    """
    deadline = deadline_after(READY_TIMEOUT_S)
    identity: dict = {}
    while time.monotonic() < deadline:
        detail = get_admin_session_detail(client, sid)
        identity = detail.get("runtime_identity") or {}
        root = str(identity.get("workspace_dir") or "").rstrip("/")
        if root.startswith("/"):
            assert root == "/workspace", identity
            return f"{root}/{filename}"
        state = str(detail.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(
                f"session {sid} went terminal ({state}) before it had a workspace "
                f"(last_error={detail.get('last_error')!r})"
            )
        time.sleep(0.5)
    pytest.fail(
        f"admin detail for session {sid} never published runtime_identity.workspace_dir within "
        f"{READY_TIMEOUT_S:.0f}s; identity={identity!r}"
    )
