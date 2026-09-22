"""Shared helpers for the sandbox e2e capability suite.

Leading-underscore module → never collected as a test. Centralizes the
``{code,message,data}`` envelope unwrap, the session create/READY poll, the
AI-SDK Data-Stream-Protocol SSE reader, the file API, and the interaction
respond / settle helpers so the per-capability test files stay readable. Mirrors
the wiring of the existing ``test_live_turn.py`` / ``test_files.py`` exactly (same
endpoints, same envelope, same READY contract) — no divergent paths.
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

# UI states (data_stream contract) that are terminal-before-ready failures.
TERMINAL_STATES = {"TERMINATED", "RECOVERY_REQUIRED", "DELETED"}


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
        and item.get("environment_name") == profile["environment_name"]
        and item.get("model") == profile["model"]
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected the configured {profile['engine_kind']!r} Investment Agent "
            f"exactly once, found {len(matches)}"
        )
    environments = data(client.get("/api/v1/admin/environments"))
    assert isinstance(environments, list), (
        f"Environment list is not a list: {environments!r}"
    )
    environment_matches = [
        item
        for item in environments
        if item.get("name") == profile["environment_name"]
        and item.get("engine_kind") == profile["engine_kind"]
        and item.get("runtime_template_name") == profile["image"]
    ]
    if len(environment_matches) != 1:
        raise AssertionError(
            "the selected Agent's live Environment no longer matches its release "
            f"evidence: engine={profile['engine_kind']!r} "
            f"environment={profile['environment_name']!r} image={profile['image']!r} "
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
    """Name the deployment's Environment with this sandbox tenancy, for this engine.

    Some contracts only exist under one tenancy — a box that belongs to one
    conversation dies with it, a box that belongs to an Agent does not — so a
    test proving one of them has to say which it needs rather than inherit
    whatever the campaign's matrix Agent happens to use.
    """
    profile = current_profile()
    environments = data(client.get("/api/v1/admin/environments"))
    assert isinstance(environments, list), (
        f"Environment list is not a list: {environments!r}"
    )
    matches = [
        str(item.get("name") or "")
        for item in environments
        if isinstance(item, dict)
        and item.get("sandbox_tenancy") == tenancy
        and item.get("engine_kind") == profile["engine_kind"]
        and item.get("runtime_template_name") == profile["image"]
        and item.get("enabled") is True
    ]
    assert matches, (
        f"the deployment has no enabled {profile['engine_kind']} Environment with "
        f"sandbox_tenancy={tenancy!r} on image {profile['image']!r}; "
        f"available={[(i.get('name'), i.get('sandbox_tenancy')) for i in environments]}"
    )
    return sorted(matches)[0]


def create_agent_variant_session(
    client: httpx.Client,
    *,
    name_suffix: str,
    engine_options: dict[str, object],
    permission_mode: str | None = None,
    environment_name: str | None = None,
) -> dict:
    """Start a conversation from an isolated variant of the matrix Agent.

    ``environment_name`` defaults to the matrix Agent's own. Pass one only when
    the test needs a property of that Environment rather than of the Agent.
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
        "environment_name": environment_name or profile["environment_name"],
        "engine_options": {**canonical_options, **engine_options},
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
    deadline = time.monotonic() + timeout
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
        f"session {sid} stayed CREATING for {timeout:.0f}s; never became settable"
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

    The Pod is the one place the answer is not derived. The endpoint the
    platform hands out is that Pod's IP, so it addresses the box directly and
    works the same for a cold-created box and a borrowed one.

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
    host = urlparse(endpoint).hostname or ""
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
    return seen.get(host, "")
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
) -> list[str]:
    """Poll any Session until READY and optionally prove its sandbox image."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
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
                        client.get(
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
    pytest.fail(f"session {sid} did not reach READY within {timeout:.0f}s; states={seen}")


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
    """The tenancy this matrix row's Environment is configured for.

    Read from the Environment rather than assumed, because it is a per-deployment
    choice and not a property of a product line: the same Agent runs one box per
    conversation or one box per Agent depending on this field, and what ending a
    conversation destroys differs accordingly.
    """

    name = str(current_profile()["environment_name"])
    rows = data(client.get("/api/v1/admin/environments"))
    matches = [item for item in rows if item.get("name") == name]
    assert len(matches) == 1, f"expected one Environment named {name!r}, found {len(matches)}"
    tenancy = str(matches[0].get("sandbox_tenancy") or "").strip()
    assert tenancy, f"Environment {name!r} declares no sandbox_tenancy: {matches[0]}"
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if name in file_names(client, sid):
            dl = download(client, sid, abspath)
            if dl.status_code == 200:
                return dl.content
        time.sleep(0.3)
    return None


# ── AI-SDK Data-Stream-Protocol SSE reader ─────────────────────────────────────


@dataclass
class StreamResult:
    """Parsed result of one ``ai-stream`` POST.

    ``interactions`` are the ``data-interaction`` payloads (each carries an
    ``interaction_id`` + ``tool_name`` + ``kind``). ``finish_reason`` is the
    ``finish`` frame's reason (``tool-calls`` when the turn paused on an
    interaction, ``stop`` when it completed). ``error`` is the in-band error
    frame text, if any.
    """

    n_text_delta: int = 0
    text: str = ""
    interactions: list[dict] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    finish_reason: str | None = None
    error: str | None = None
    saw_ui_header: bool = False
    content_type: str = ""

    @property
    def last_interaction_id(self) -> str | None:
        return self.interactions[-1].get("interaction_id") if self.interactions else None


def stream_turn(
    client: httpx.Client,
    sid: str,
    *,
    content: str | None = None,
    interaction_response: dict | None = None,
    permission_mode: str | None = None,
    timeout: float = STREAM_TIMEOUT_S,
    client_message_id: str | None = None,
) -> StreamResult:
    """POST ai-stream and parse the SSE to a :class:`StreamResult` (fails loud on non-SSE).

    ``client_message_id`` is the caller's when it needs one: a retry that mints
    a fresh id is a SECOND input, not the same one again, and the engine then
    consumes them out of order (`Hermes input consumption is not the FIFO
    head`). A caller retrying one message passes the id it used the first time.
    """
    body: dict = {}
    if content is not None:
        body["content"] = content
        body["client_message_id"] = client_message_id or str(uuid.uuid4())
    if interaction_response is not None:
        body["interaction_response"] = interaction_response
    if permission_mode is not None:
        body["permission_mode"] = permission_mode

    res = StreamResult()
    parts: list[str] = []
    with client.stream(
        "POST",
        f"/api/v1/sessions/{sid}/ai-stream",
        json=body,
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(timeout, connect=30.0),
    ) as resp:
        res.content_type = resp.headers.get("content-type", "")
        assert "text/event-stream" in res.content_type, (
            f"ai-stream did not return SSE (content-type={res.content_type!r}); "
            f"body={resp.read()[:400]!r}"
        )
        res.saw_ui_header = resp.headers.get("x-vercel-ai-ui-message-stream") == "v1"
        for raw in resp.iter_lines():
            line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            etype = ev.get("type")
            if etype == "text-delta":
                res.n_text_delta += 1
                delta = ev.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
            elif etype == "data-interaction":
                d = ev.get("data")
                if isinstance(d, dict):
                    res.interactions.append(d)
            elif etype == "tool-input-available":
                name = ev.get("toolName")
                if name:
                    res.tool_names.append(str(name))
            elif etype == "finish":
                res.finish_reason = str(ev.get("finishReason") or "")
            elif etype == "error":
                res.error = str(ev.get("errorText") or "unknown error")
    res.text = "".join(parts)
    return res


# ── interaction respond / settle ──────────────────────────────────────────────


def respond_interaction(client: httpx.Client, sid: str, interaction_id: str, answer: dict) -> dict:
    """The FE's primary approve/deny path: ``POST /interaction-respond``."""
    return data(
        client.post(
            f"/api/v1/sessions/{sid}/interaction-respond",
            json={"interaction_id": interaction_id, "answer": answer},
        )
    )


def pending_interaction(client: httpx.Client, sid: str) -> dict | None:
    pi = get_session(client, sid).get("pending_interaction")
    return pi if isinstance(pi, dict) and pi.get("interaction_id") else None


def wait_for_pending_interaction(
    client: httpx.Client, sid: str, *, exclude_id: str | None = None, timeout: float = 20.0
) -> dict | None:
    """Poll session detail for a pending interaction (optionally one whose id != ``exclude_id``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pi = pending_interaction(client, sid)
        if pi and (exclude_id is None or str(pi.get("interaction_id")) != exclude_id):
            return pi
        time.sleep(0.5)
    return None


def wait_until_settled(client: httpx.Client, sid: str, timeout: float = SETTLE_TIMEOUT_S) -> dict:
    """Poll until the turn settles: state READY with no pending interaction.

    Returns the final detail. Fails loud on a terminal state or timeout.
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = get_session(client, sid)
        state = str(last.get("state") or "")
        if state in TERMINAL_STATES:
            pytest.fail(f"session {sid} went terminal ({state}) while settling: {last.get('last_error')!r}")
        if state == "READY" and not last.get("pending_interaction"):
            return last
        time.sleep(0.3)
    pytest.fail(
        f"session {sid} did not settle (READY, no pending) within {timeout:.0f}s; "
        f"state={last.get('state')!r} pending={bool(last.get('pending_interaction'))}"
    )


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
    """Interrupt a busy turn (so DELETE isn't 409), then delete. Best effort.

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
    deadline = time.monotonic() + READY_TIMEOUT_S
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
