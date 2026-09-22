from __future__ import annotations

from collections.abc import Callable

import time

import httpx
import pytest

from tests.e2e import _sandbox_helpers as helpers
from tests.e2e._sandbox_helpers import poll_until_ready, workspace_path


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="https://astrabox.test",
        transport=httpx.MockTransport(handler),
    )


def _ok(request: httpx.Request, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        request=request,
        json={"code": "OK", "message": "", "data": payload},
    )


def test_ready_poll_uses_the_owner_session_projection() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _ok(request, {"session_id": "session-1", "state": "READY"})

    with _client(handler) as client:
        assert poll_until_ready(client, "session-1") == ["READY"]

    assert paths == ["/api/v1/sessions/session-1"]


def test_workspace_path_reads_private_identity_from_operator_detail() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _ok(
            request,
            {
                "session_id": "session-1",
                "state": "READY",
                "runtime_identity": {"workspace_dir": "/workspace"},
            },
        )

    with _client(handler) as client:
        assert workspace_path(client, "session-1", "proof.txt") == "/workspace/proof.txt"

    assert paths == ["/api/v1/admin/sessions/session-1/detail"]


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_kubectl(
    *, running: list[str], uids: dict[str, str], reads: dict[str, str]
) -> Callable[..., _FakeCompleted]:
    """Answer the three kubectl shapes `read_through_the_medium` issues."""

    def run(argv: list[str], **_: object) -> _FakeCompleted:
        if "get" in argv and "pods" in argv:
            return _FakeCompleted(stdout="\n".join(running) + "\n")
        if "get" in argv and "pod" in argv:
            pod = argv[argv.index("pod") + 1]
            if pod not in uids:
                return _FakeCompleted(stderr="NotFound", returncode=1)
            return _FakeCompleted(stdout=uids[pod])
        if "exec" in argv:
            pod = argv[argv.index("exec") + 1]
            return _FakeCompleted(stdout=reads[pod])
        raise AssertionError(f"unexpected kubectl call: {argv}")

    return run


def test_the_read_refuses_when_only_the_destroyed_box_is_left(monkeypatch) -> None:
    """The false green this exclusion exists to prevent.

    A deleted Pod keeps reporting Running for a moment, so a reader that matches
    on name alone can read the planted marker back out of the very box that
    wrote it — and pass on a deployment with no durable storage at all.
    """

    monkeypatch.setattr(
        helpers.subprocess,
        "run",
        _fake_kubectl(
            running=["box-1"],
            uids={"box-1": "uid-old"},
            reads={"box-1": "DURABLE-TOKEN"},
        ),
    )
    with pytest.raises(AssertionError) as refusal:
        helpers.read_through_the_medium(
            kubeconfig="kc",
            namespace="ns",
            script="cat marker",
            name_hint="box-",
            not_uid="uid-old",
            deadline=time.monotonic() + 0.2,
        )
    assert "uid-old" in str(refusal.value)


def test_the_read_takes_the_replacement_that_shares_the_name(monkeypatch) -> None:
    """The same name is the ordinary case, not the exception.

    A controller-owned box comes back under the name it had, so the replacement
    is only distinguishable by uid — which is the whole reason the caller reads
    one before the kill.
    """

    monkeypatch.setattr(
        helpers.subprocess,
        "run",
        _fake_kubectl(
            running=["box-1"],
            uids={"box-1": "uid-new"},
            reads={"box-1": "DURABLE-TOKEN"},
        ),
    )
    found = helpers.read_through_the_medium(
        kubeconfig="kc",
        namespace="ns",
        script="cat marker",
        name_hint="box-",
        not_uid="uid-old",
        deadline=time.monotonic() + 5,
    )
    assert found == "DURABLE-TOKEN"


def test_a_failed_pod_listing_is_reported_rather_than_polled_through(
    monkeypatch,
) -> None:
    """An unreadable listing must not read as a cluster with no Pods.

    This is what cost two testbed rounds: kubectl refused the query, stdout was
    empty, and the loop polled out its whole budget against what looked like an
    empty cluster.
    """

    def run(argv: list[str], **_: object) -> _FakeCompleted:
        return _FakeCompleted(stderr="error: error parsing jsonpath", returncode=1)

    monkeypatch.setattr(helpers.subprocess, "run", run)
    with pytest.raises(AssertionError) as refusal:
        helpers.read_through_the_medium(
            kubeconfig="kc",
            namespace="ns",
            script="cat marker",
            name_hint="box-",
            not_uid="uid-old",
            deadline=time.monotonic() + 30,
        )
    assert "parsing jsonpath" in str(refusal.value)


def test_pod_uid_refuses_a_missing_pod_before_the_kill(monkeypatch) -> None:
    """Before the kill, an absent Pod breaks the test's premise.

    `pod_uid_quietly` answers "" for the same Pod during the wait, where absence
    is the expected state — the two callers want opposite things, which is why
    they are two functions.
    """

    monkeypatch.setattr(
        helpers.subprocess, "run", _fake_kubectl(running=[], uids={}, reads={})
    )
    with pytest.raises(AssertionError):
        helpers.pod_uid(pod="box-1", kubeconfig="kc", namespace="ns")
    assert helpers.pod_uid_quietly(pod="box-1", kubeconfig="kc", namespace="ns") == ""
