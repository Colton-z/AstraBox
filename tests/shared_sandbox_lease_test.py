"""Handing out isolated sessions in one agent-owned box.

Two conversations of one agent that start at the same instant must end up in
the SAME box under DIFFERENT owners — two boxes would split the workspace the
shared mode exists to share, one owner would remove the isolation it exists to
provide — and the loser of the claim race must not leave its box behind. Each
session has a private ``/tmp``, a restored session must carry this
conversation's identity, and a burst of starts is not admitted from one memory
reading.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    BOX_ADMISSIONS,
    SharedSandboxBinding,
    SharedSandboxLease,
)
from astrabox.seams.sandbox import (
    SandboxIsolatedSession,
    SandboxIsolationCapability,
    SandboxMemoryHeadroom,
)
from astrabox.providers import register_builtin_providers

# start_runner resolves the launch line from the engine registry, so the
# adapters must exist before any test asks for one.
register_builtin_providers()


class _FakeAgentRepo:
    """The Agent's shared-box ledger, kept in memory for the lease's own decisions.

    A write applies only while the admissions revision it names is current, the
    way the store applies it. The races between real writers are run against
    the store itself in the tests that take the ``database`` fixture.
    """

    def __init__(self, agent: dict[str, Any] | None = None) -> None:
        self.agent = agent if agent is not None else {"agent_id": "a1"}
        self.admissions_revision = 0
        #: When set, the NEXT box claim loses and this box wins instead.
        self.rival_box: str | None = None

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if self.agent else None

    async def read_ledger(self, agent_id: str) -> dict[str, Any] | None:
        if not self.agent or self.agent.get("agent_id") != agent_id:
            return None
        return {**copy.deepcopy(self.agent), "_admissions_revision": self.admissions_revision}

    async def allocate_conversation_uid(
        self, agent_id: str, *, first: int, last: int
    ) -> int | None:
        if not self.agent or self.agent.get("agent_id") != agent_id:
            return None
        current = self.agent.get("conversation_uid_cursor")
        allocated = max((current if isinstance(current, int) else first - 1) + 1, first)
        if allocated > last:
            return None
        self.agent["conversation_uid_cursor"] = allocated
        return allocated

    def _admit(self, sandbox_id: str, session_id: str, young_since: Any) -> None:
        kept = [
            entry
            for entry in self.agent.get(BOX_ADMISSIONS) or []
            if entry["at"] >= young_since.timestamp()
            and (entry["sandbox_id"], entry.get("session_id")) != (sandbox_id, session_id)
        ]
        kept.append({"sandbox_id": sandbox_id, "session_id": session_id, "at": time.time()})
        self.agent[BOX_ADMISSIONS] = kept
        self.admissions_revision += 1

    async def admit_to_box(
        self, agent_id: str, *, sandbox_id: str, session_id: str,
        admissions_revision: int, young_since: Any,
    ) -> bool:
        if not self.agent or admissions_revision != self.admissions_revision:
            return False
        self._admit(sandbox_id, session_id, young_since)
        return True

    async def claim_resident_box(
        self, agent_id: str, *, observed: dict[str, Any], admissions_revision: int,
        sandbox_id: str, sandbox_backend: str, resident_generation: str | None,
        session_id: str, young_since: Any,
    ) -> bool:
        if self.rival_box is not None:
            self.agent.update(sandbox_id=self.rival_box, sandbox_backend="open_sandbox")
            self.rival_box = None
            self.admissions_revision += 1
            return False
        if not self.agent or admissions_revision != self.admissions_revision:
            return False
        stored = {
            "sandbox_id": "sandbox_id",
            "sandbox_backend": "sandbox_backend",
            "runtime_generation": "_runtime_generation",
            "sandbox_generation": "_sandbox_generation",
        }
        if any(self.agent.get(stored[key]) != (value or None) for key, value in observed.items()):
            return False
        self.agent.update(sandbox_id=sandbox_id, sandbox_backend=sandbox_backend)
        if resident_generation:
            self.agent["_resident_sandbox_generation"] = resident_generation
        self._admit(sandbox_id, session_id, young_since)
        return True


class _FakeProvider:
    name = "open_sandbox"

    def __init__(self, *, hosting: dict[str, bool] | None = None) -> None:
        self.hosting = hosting if hosting is not None else {}
        self.opened: list[dict[str, Any]] = []
        self.closed: list[tuple[str, str]] = []
        self.killed: list[str] = []
        self.prepared: list[dict[str, Any]] = []
        self.probe_error: Exception | None = None
        self.prepare_error: Exception | None = None
        self.ran: list[str] = []
        #: Exit code the workspace write-probe comes back with.
        self.probe_exit = 0
        #: How many sockets the launch check reports on the runner's port.
        self.runner_listening = 1
        #: What the box answers about its own memory, as (limit, current) bytes.
        #: The default leaves room for another conversation; a test that wants a
        #: full box says so.
        self.memory = (3 * 1024**3, 512 * 1024**2)
        #: TCP ports the box says are already bound. Empty is a fresh box.
        self.listening: set[int] = set()
        self._next = 0

    async def read_memory_headroom(self, sandbox_id: str) -> SandboxMemoryHeadroom:
        return SandboxMemoryHeadroom(*self.memory)

    async def read_listening_ports(self, sandbox_id: str) -> set[int]:
        return set(self.listening)

    async def prepare_isolated_workspace(
        self, sandbox_id: str, *, workspace_dir: str, uid: int, gid: int
    ) -> None:
        if self.prepare_error:
            raise self.prepare_error
        self.prepared.append(
            {"sandbox_id": sandbox_id, "path": workspace_dir, "uid": uid, "gid": gid}
        )

    async def run_in_isolated_session(
        self, sandbox_id: str, session_id: str, *, code: str,
        timeout_s: float | None = 60.0,
    ) -> tuple[int, str, str]:
        self.ran.append(code)
        if "ASTRABOX_RUNNER_PORT" in code:
            # The launch's last line is the listening count.
            return (0, f"{self.runner_listening}\n", "")
        return (self.probe_exit, "", "Permission denied" if self.probe_exit else "")

    async def read_isolation_capability(
        self, sandbox_id: str
    ) -> SandboxIsolationCapability:
        if self.probe_error:
            raise self.probe_error
        return SandboxIsolationCapability(
            sandbox_id=sandbox_id,
            available=self.hosting.get(sandbox_id, True),
            detail=None if self.hosting.get(sandbox_id, True) else "no namespaces here",
        )

    async def open_isolated_session(
        self,
        sandbox_id: str,
        *,
        workspace_dir: str,
        workspace_source_dir: str,
        uid: int | None = None,
        gid: int | None = None,
        share_net: bool = True,
        extra_writable: list[str] | None = None,
        extra_binds: list[tuple[str, str]] | None = None,
    ) -> SandboxIsolatedSession:
        self._next += 1
        self.opened.append(
            {
                "sandbox_id": sandbox_id,
                "workspace_dir": workspace_dir,
                "workspace_source_dir": workspace_source_dir,
                "uid": uid,
                "gid": gid,
                "share_net": share_net,
                "extra_writable": list(extra_writable or []),
                "extra_binds": list(extra_binds or []),
            }
        )
        return SandboxIsolatedSession(
            sandbox_id=sandbox_id,
            session_id=f"iso-{self._next}",
            uid=uid,
            gid=gid,
            workspace_dir=workspace_dir,
            workspace_source_dir=workspace_source_dir,
        )

    async def close_isolated_session(self, sandbox_id: str, session_id: str) -> None:
        self.closed.append((sandbox_id, session_id))

    async def kill(self, sandbox_id: str) -> bool:
        self.killed.append(sandbox_id)
        return True


def _acquire(
    lease: SharedSandboxLease,
    candidate: str | None,
    *,
    expected_runtime_generation: str | None = None,
    expected_sandbox_generation: str | None = None,
    uid: int | None = None,
) -> SharedSandboxBinding | None:
    """Ask where a new conversation goes, offering ``candidate`` as a box that
    could become the agent's. Nothing is provisioned by the lease."""
    return asyncio.run(_place(lease, candidate, expected_runtime_generation=expected_runtime_generation,
                              expected_sandbox_generation=expected_sandbox_generation, uid=uid))


async def _place(
    lease: SharedSandboxLease,
    candidate: str | None,
    *,
    expected_runtime_generation: str | None = None,
    expected_sandbox_generation: str | None = None,
    uid: int | None = None,
) -> SharedSandboxBinding | None:
    return await lease.place_in_agent_box(
        agent_id="a1",
        session_id=f"session-{uuid.uuid4().hex[:8]}",
        home_dir="/home/conversations/conv1",
        workspace_dir="/workspace",
        workspace_source_dir="/home/conversations/conv1/workspace",
        candidate=candidate,
        expected_runtime_generation=expected_runtime_generation,
        expected_sandbox_generation=expected_sandbox_generation,
        requested_uid=uid,
    )


@pytest.mark.usefixtures("database")
async def test_first_conversations_starting_together_share_one_box_under_different_owners() -> None:
    """The claim race run against the store: one box, two owners, nothing destroyed."""
    from tests._agent_ledger import create_agent

    repo = await create_agent("a1")
    provider = _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    placed = await asyncio.gather(_place(lease, "box-a"), _place(lease, "box-b"))

    assert all(binding is not None for binding in placed)
    boxes = {binding.sandbox_id for binding in placed if binding is not None}
    assert len(boxes) == 1, f"two conversations split across {boxes}"
    stored = await repo.get_agent("a1")
    assert stored is not None and {stored["sandbox_id"]} == boxes
    assert len({binding.uid for binding in placed if binding is not None}) == 2
    assert provider.killed == [], "the loser's candidate is its caller's to keep"


def test_restart_refuses_a_persisted_session_with_the_wrong_identity() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.probe_exit = 1
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    with pytest.raises(APIError) as caught:
        asyncio.run(
            lease.restore_existing(
                sandbox_id="box-1",
                isolated_session_id="iso-other-user",
                home_dir="/home/conversations/conv1",
                workspace_dir="/workspace",
                workspace_source_dir="/home/conversations/conv1/workspace",
                uid=2042,
                gid=2042,
            )
        )

    assert caught.value.status_code == 409
    assert "does not match this conversation" in caught.value.message
    assert provider.opened == []


# ── capacity, which is not capability ────────────────────────────────────
#
# A box that CAN isolate still kills every conversation in it when the next one
# exhausts the container's memory: the kernel takes the container, not the
# newcomer, so the conversation that broke the limit is not the one that pays.
# The numbers are in-box measurements: an idle box holds 385 MB, each
# conversation adds ~306 MB, and against a 1 GiB limit the third one OOM-kills
# the container while it reports itself Ready throughout.


@pytest.mark.usefixtures("database")
async def test_a_burst_of_conversations_is_not_all_admitted_from_one_reading() -> None:
    """The failure this rule exists for, reproduced.

    A 2 GiB box reads ~385 MB idle, so it has room for about five
    conversations. Admitted together they all read the same number — none has
    started using anything yet — and a rule that only measures answers "room
    for one more" to every one of them. Nine in one box exhausts it, and the
    container dies with every conversation in it.

    The box's reading never moves here, which is exactly the real case: the
    conversations are still preparing. They arrive at once, against the store.
    """
    from tests._agent_ledger import create_agent, set_runtime

    repo = await create_agent("a1")
    await set_runtime("a1", sandbox_id="box-1", sandbox_backend="open_sandbox")
    provider = _FakeProvider()
    # 2 GiB with an idle box in it. Room for five conversations at the floor,
    # and the reading stays put no matter how many are admitted.
    provider.memory = (2 * 1024**3, 385 * 1024**2)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    admitted = await asyncio.gather(*(_place(lease, "box-1") for _ in range(9)))
    accepted = [placement for placement in admitted if placement is not None]

    assert len(accepted) <= 5, (
        f"{len(accepted)} conversations were admitted from one unchanged "
        "reading; the box has room for about five"
    )
    assert accepted, "the first conversations must still be admitted"


def test_every_session_binds_the_conversation_private_tmp() -> None:
    """Both sessions, because a sibling must not differ from its own kin."""

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    for opened in provider.opened:
        assert opened["extra_binds"] == [("/home/conversations/conv1/tmp", "/tmp")]
