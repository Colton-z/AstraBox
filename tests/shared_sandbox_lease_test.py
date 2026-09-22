"""Handing out isolated sessions in one agent-owned box.

The properties that matter are all about what happens when two conversations of
one agent start at the same instant, and about what is trusted to say a box is
usable. Both fail in ways that look like success:

* they must end up in the SAME box under DIFFERENT owners. Two boxes would
  split the workspace the shared mode exists to share; one owner would remove
  the isolation it exists to provide.
* the loser of the claim race must not leave its box behind.
* whether a box can still host is asked of the BOX. A box the agent still names
  may be gone, or may have come back from a pool whose template does not grant
  the privilege level, and proceeding on an unanswered probe would open sessions
  that isolate nothing.
* release closes the session and NEVER the box — the box is the other
  conversations' too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    CONVERSATION_UID_BASE,
)
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    RUNNER_PORT_BASE,
    RUNNER_PORT_SPAN,
    runner_port_for_uid,
)
from astrabox.core.service.orchestrator.runtime.shared_sandbox_lease import (
    SharedSandboxBinding,
    SharedSandboxLease,
)
from astrabox.seams.sandbox import SandboxIsolatedSession, SandboxIsolationCapability
from astrabox.providers import register_builtin_providers

# start_runner resolves the launch line from the engine registry, so the
# adapters must exist before any test asks for one.
register_builtin_providers()


class _FakeAgentRepo:
    def __init__(self, agent: dict[str, Any] | None = None) -> None:
        self.agent = agent if agent is not None else {"agent_id": "a1"}
        #: When set, the NEXT compare-and-set loses and this box wins instead.
        self.rival_box: str | None = None

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return dict(self.agent) if self.agent else None

    async def update_agent(self, agent_id: str, updates: dict[str, Any]) -> bool:
        if not self.agent or self.agent.get("agent_id") != agent_id:
            return False
        self.agent.update(updates)
        return True

    async def compare_and_update_agent(
        self, agent_id: str, *, expected: dict[str, Any], updates: dict[str, Any]
    ) -> bool:
        if self.rival_box is not None and "sandbox_id" in updates:
            self.agent["sandbox_id"] = self.rival_box
            self.rival_box = None
            return False
        # Matches every expected key, the way the real one does — the box claim
        # and the uid claim both come through here and each must be able to
        # fail on its own field.
        for key, want in expected.items():
            held = self.agent.get(key)
            if isinstance(want, dict):
                if "$exists" in want and bool(want["$exists"]) != (key in self.agent):
                    return False
                # A MISSING field fails $in, exactly as the store does. A
                # fake that matched it would let a claim pass here and fail on a
                # real deployment.
                if "$in" in want and (key not in self.agent or held not in want["$in"]):
                    return False
            elif held != want:
                return False
        self.agent.update(updates)
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

    async def read_memory_headroom(self, sandbox_id: str) -> tuple[int, int]:
        return self.memory

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
    """Ask where this conversation goes, offering ``candidate`` as a box that
    could become the agent's. Nothing is provisioned by the lease."""
    return asyncio.run(
        lease.place_in_agent_box(
            agent_id="a1",
            home_dir="/home/conversations/conv1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv1/workspace",
            candidate=candidate,
            expected_runtime_generation=expected_runtime_generation,
            expected_sandbox_generation=expected_sandbox_generation,
            requested_uid=uid,
        )
    )


def _start_runner(
    lease: SharedSandboxLease,
    binding: SharedSandboxBinding,
    *,
    engine_kind: str = "claude_code",
) -> str:
    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    port = runner_port_for_uid(binding.uid)
    launch = get_engine_adapter(engine_kind).shared_conversation_service_launch(
        home=binding.home_dir,
        workspace=binding.workspace_dir,
        port=port,
    )
    return asyncio.run(
        lease.start_runner(
            binding,
            launch=launch,
            engine_label=engine_kind,
        )
    )


def test_the_first_conversation_provisions_and_claims_the_box() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    assert binding is not None
    assert binding.sandbox_id == "box-1"
    assert binding.uid == CONVERSATION_UID_BASE
    assert binding.gid == binding.uid
    assert repo.agent["sandbox_id"] == "box-1", "the box must land on the agent row"
    assert repo.agent["sandbox_backend"] == "open_sandbox", (
        "by-id cleanup must retain the box's authoritative backend after restart"
    )
    assert provider.opened[0]["share_net"] is True
    assert provider.opened[0]["extra_writable"] == [
        "/home/conversations/conv1"
    ]
    assert provider.opened[0]["workspace_dir"] == "/workspace"
    assert provider.opened[0]["workspace_source_dir"] == (
        "/home/conversations/conv1/workspace"
    )
    assert len(provider.opened) == 2
    assert binding.terminal_isolated_session_id == "iso-2"
    assert binding.terminal_isolated_session_id != binding.isolated_session_id
    assert [item["path"] for item in provider.prepared] == [
        "/home/conversations/conv1",
        "/home/conversations/conv1/workspace",
        "/home/conversations/conv1/tmp",
    ]


def test_the_second_conversation_reuses_the_box_under_a_new_owner() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    first = _acquire(lease, "box-1")


    second = _acquire(lease, None)
    assert second.sandbox_id == first.sandbox_id, "same box"
    assert second.uid != first.uid, "different owner"
    assert second.isolated_session_id != first.isolated_session_id
    assert second.terminal_isolated_session_id != first.terminal_isolated_session_id


def test_a_stale_resident_box_is_not_reused_before_replacement_arrives() -> None:
    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "sandbox_id": "box-old",
            "sandbox_backend": "open_sandbox",
            "_runtime_generation": "generation-new",
            "_sandbox_generation": "sandbox-new",
            "_resident_sandbox_generation": "sandbox-old",
        }
    )
    provider = _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    assert _acquire(
        lease,
        None,
        expected_runtime_generation="generation-new",
        expected_sandbox_generation="sandbox-new",
    ) is None
    assert provider.opened == []


def test_a_cleared_null_pointer_can_claim_the_current_pool_box() -> None:
    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "sandbox_id": None,
            "sandbox_backend": None,
            "_runtime_generation": "generation-new",
        }
    )
    provider = _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(
        lease, "box-new", expected_runtime_generation="generation-new"
    )

    assert binding is not None
    assert repo.agent["sandbox_id"] == "box-new"


def test_an_acquirer_with_an_old_definition_does_not_claim_a_new_agent_pointer() -> None:
    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "_runtime_generation": "generation-new",
        }
    )
    provider = _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    assert _acquire(
        lease, "box-old", expected_runtime_generation="generation-old"
    ) is None
    assert "sandbox_id" not in repo.agent


def test_an_old_box_claim_without_a_backend_is_repaired_before_use() -> None:
    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-old"})
    provider = _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, None)

    assert binding is not None
    assert binding.sandbox_id == "box-old"
    assert repo.agent["sandbox_backend"] == "open_sandbox"


def test_losing_the_claim_race_joins_the_winner_and_destroys_nothing() -> None:
    """The loser's candidate was acquired by its CALLER and is still held there
    — destroying it here would take away a box somebody is using. It simply is
    not the agent's box, and this conversation joins the one that is."""
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    repo.rival_box = "box-winner"
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-mine")
    assert binding is not None
    assert binding.sandbox_id == "box-winner", "must not overwrite the winner"
    assert provider.killed == [], "the caller's box is not this layer's to destroy"
    assert repo.agent["sandbox_id"] == "box-winner"


def test_a_box_that_can_no_longer_host_is_replaced() -> None:
    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-old"})
    provider = _FakeProvider(hosting={"box-old": False, "box-new": True})
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-new")
    assert binding is not None
    assert binding.sandbox_id == "box-new"
    assert repo.agent["sandbox_id"] == "box-new"


def test_a_box_that_cannot_carry_more_than_one_conversation_yields_None() -> None:
    """The packing decision, and the only place it is made. Not an error and
    not a reason to replace the box: the caller keeps it and the conversation
    has it to itself. Nothing above the seam is told which happened, because
    nothing above the seam can tell the difference."""
    repo, provider = _FakeAgentRepo(), _FakeProvider(hosting={"box-plain": False})
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    assert _acquire(lease, "box-plain") is None
    assert provider.opened == [], "no session is opened in a box that cannot host"
    assert provider.killed == [], "and the box is NOT thrown away"
    assert "sandbox_id" not in repo.agent, "nor claimed for the agent"


def test_an_unanswered_probe_counts_as_cannot_host() -> None:
    """Proceeding on a box that did not answer would open sessions that
    isolate nothing — silently, since every call downstream still succeeds. So
    silence means "no sharing", which costs the conversation nothing: it keeps
    the box to itself."""
    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-old"})
    provider = _FakeProvider()
    provider.probe_error = RuntimeError("connection refused")
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    assert _acquire(lease, "box-new") is None
    assert provider.opened == []
    assert provider.killed == [], "an unreachable probe is no reason to destroy a box"


def test_release_closes_the_session_and_leaves_the_box_alone() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    asyncio.run(lease.release(binding))
    assert provider.closed == [
        ("box-1", binding.terminal_isolated_session_id),
        ("box-1", binding.isolated_session_id),
    ]
    assert provider.killed == [], "the box belongs to the other conversations too"
    assert repo.agent["sandbox_id"] == "box-1", "and the agent still holds it"


def test_restart_restores_and_verifies_the_persisted_placement() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = asyncio.run(
        lease.restore_existing(
            sandbox_id="box-1",
            isolated_session_id="iso-durable",
            terminal_isolated_session_id="iso-terminal-durable",
            home_dir="/home/conversations/conv1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv1/workspace",
            uid=2042,
            gid=2042,
        )
    )

    assert binding.isolated_session_id == "iso-durable"
    assert binding.terminal_isolated_session_id == "iso-terminal-durable"
    assert binding.workspace_dir == "/workspace"
    assert binding.workspace_source_dir == "/home/conversations/conv1/workspace"
    probe = provider.ran[-1]
    assert 'test "$(id -u)" = 2042' in probe
    assert "test -w /workspace" in probe


def test_restart_replaces_a_missing_legacy_terminal_session() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = asyncio.run(
        lease.restore_existing(
            sandbox_id="box-1",
            isolated_session_id="iso-durable",
            home_dir="/home/conversations/conv1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv1/workspace",
            uid=2042,
            gid=2042,
        )
    )

    assert binding.isolated_session_id == "iso-durable"
    assert binding.terminal_isolated_session_id == "iso-1"
    assert provider.opened[0]["uid"] == 2042


def test_restart_replaces_a_stale_persisted_terminal_session() -> None:
    class _StaleTerminalProvider(_FakeProvider):
        async def run_in_isolated_session(
            self,
            sandbox_id: str,
            session_id: str,
            *,
            code: str,
            timeout_s: float | None = 60.0,
        ) -> tuple[int, str, str]:
            if session_id == "iso-terminal-stale":
                raise APIError(
                    code="AGENT_RUNTIME_ERROR",
                    message="isolated session is gone",
                    status_code=409,
                )
            return await super().run_in_isolated_session(
                sandbox_id,
                session_id,
                code=code,
                timeout_s=timeout_s,
            )

    repo, provider = _FakeAgentRepo(), _StaleTerminalProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = asyncio.run(
        lease.restore_existing(
            sandbox_id="box-1",
            isolated_session_id="iso-agent-durable",
            terminal_isolated_session_id="iso-terminal-stale",
            home_dir="/home/conversations/conv1",
            workspace_dir="/workspace",
            workspace_source_dir="/home/conversations/conv1/workspace",
            uid=2042,
            gid=2042,
        )
    )

    assert provider.closed == [("box-1", "iso-terminal-stale")]
    assert binding.isolated_session_id == "iso-agent-durable"
    assert binding.terminal_isolated_session_id == "iso-1"


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


def test_the_private_home_and_workspace_are_prepared_before_the_session_opens() -> None:
    """OpenSandbox binds the home, the workspace source and the private /tmp
    when it creates the isolated session, so each must already belong to its
    numeric owner."""
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    assert [p["path"] for p in provider.prepared] == [
        "/home/conversations/conv1",
        "/home/conversations/conv1/workspace",
        "/home/conversations/conv1/tmp",
    ]
    assert all(p["uid"] == binding.uid for p in provider.prepared)
    assert binding.home_dir == "/home/conversations/conv1"


def test_a_session_that_cannot_write_its_workspace_is_refused_and_closed() -> None:
    """Otherwise this surfaces as the agent's first tool call failing, three
    layers from the cause — and the session is left running meanwhile."""
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.probe_exit = 1
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    with pytest.raises(APIError) as caught:
        _acquire(lease, "box-1")
    assert "cannot write its workspace" in str(caught.value.message)
    assert provider.closed == [("box-1", "iso-1")], "the dead session must not leak"


def test_workspace_access_check_preserves_user_files(tmp_path: Path) -> None:
    """Checking access must not create, touch or delete user task files."""
    user_file = tmp_path / ".astrabox-writable"
    user_file.write_text("User-owned task data\n", encoding="utf-8")
    before = user_file.stat()

    class ShellProvider(_FakeProvider):
        async def run_in_isolated_session(
            self, sandbox_id: str, session_id: str, *, code: str, timeout_s: float
        ) -> tuple[int, str, str]:
            result = subprocess.run(
                ["sh", "-c", code], capture_output=True, text=True, timeout=timeout_s
            )
            return result.returncode, result.stdout, result.stderr

    lease = SharedSandboxLease(agent_repo=_FakeAgentRepo(), provider=ShellProvider())
    asyncio.run(lease._confirm_writable("box", "session", str(tmp_path)))
    assert list(tmp_path.iterdir()) == [user_file]
    assert user_file.read_text(encoding="utf-8") == "User-owned task data\n"
    assert user_file.stat().st_mtime_ns == before.st_mtime_ns


def test_a_workspace_that_cannot_be_prepared_never_opens_a_session() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.prepare_error = APIError(
        code="AGENT_RUNTIME_ERROR", message="chown failed", status_code=502
    )
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    with pytest.raises(APIError):
        _acquire(lease, "box-1")
    assert provider.opened == []


def test_asking_before_a_box_exists_answers_None_without_provisioning() -> None:
    """The cheap first question — does the agent already hold a box? — which
    when answered yes means acquiring nothing at all. Asking it with no
    candidate must never create anything."""
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    assert _acquire(lease, None) is None
    assert provider.opened == []
    assert "sandbox_id" not in repo.agent


def test_a_missing_agent_means_no_sharing_not_a_failed_conversation() -> None:
    repo, provider = _FakeAgentRepo({}), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    assert _acquire(lease, "box-1") is None
    assert provider.opened == []


def test_endless_claim_contention_gives_up_on_sharing_not_on_the_session() -> None:
    class _AlwaysLoses(_FakeAgentRepo):
        async def compare_and_update_agent(self, agent_id: str, **kwargs: Any) -> bool:
            return False

    repo, provider = _AlwaysLoses(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    # Not fatal: a conversation with a box of its own is a working
    # conversation, and failing it here would kill a session over a question
    # whose answer is optional.
    assert _acquire(lease, "b1") is None
    assert provider.killed == [], "contention destroys nothing"


def test_the_runner_is_started_inside_the_session_on_its_own_port() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    port = _start_runner(lease, binding)
    assert port == str(runner_port_for_uid(binding.uid))
    launch = [c for c in provider.ran if "ASTRABOX_RUNNER_PORT" in c][0]
    # The line is the ENGINE's declaration now, and it still names the image's
    # own runner at the image's own path — nothing is assembled here that the
    # box did not ship with.
    assert "/opt/astrabox/sandbox_runner.py" in launch
    assert f"ASTRABOX_RUNNER_PORT={port}" in launch
    # Runtime buffering uses the writable private home, not the user's files.
    assert f"ASTRABOX_RUNNER_SPOOL_DIR={binding.home_dir}/.astrabox-spool" in launch
    assert f"TMPDIR={binding.home_dir}/.astrabox-spool" in launch
    assert f"{binding.workspace_dir}/.astrabox-spool" not in launch
    # Detached, or it dies with the run that started it.
    launch_line = [ln for ln in launch.splitlines() if "setsid" in ln][0]
    assert launch_line.rstrip().endswith("&")


def test_a_runner_that_never_listens_is_refused_not_returned() -> None:
    """Otherwise the host dials a port nothing is on, and the failure surfaces
    as a transport error with no hint that the process never started."""
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.runner_listening = 0
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    with pytest.raises(APIError) as caught:
        _start_runner(lease, binding)
    assert "did not come up" in str(caught.value.message)


def test_an_engine_without_a_service_launch_is_refused_before_anything_starts() -> None:
    """box_account integrations reach no half-started state.

    The refusal must arrive before any command runs in the session: a service
    that is half-launched under the wrong assumptions is the silent-downgrade
    shape the placement gate exists to prevent.
    """

    from astrabox.core.service.orchestrator.engine import registry as _registry
    from astrabox.core.service.orchestrator.engine.base import EngineAdapter

    class _LaunchlessAdapter:
        """A future integration that has not declared its launch yet.

        Every shipped engine declares one now, so the gate is pinned through
        a stub rather than by borrowing a real adapter whose graduation
        would silently turn this into a test of nothing. Only the seam
        method under test is real — the base declaration's None default.
        """

        shared_conversation_service_launch = (
            EngineAdapter.shared_conversation_service_launch
        )

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")
    ran_before = list(provider.ran)
    original = _registry.get_engine_adapter
    _registry._REGISTRY["launchless_stub"] = _LaunchlessAdapter()  # type: ignore[attr-defined]
    try:
        with pytest.raises(APIError) as caught:
            asyncio.run(
                lease.start_runner(
                    binding,
                    launch=None,
                    engine_label="launchless_stub",
                )
            )
    finally:
        _registry._REGISTRY.pop("launchless_stub", None)  # type: ignore[attr-defined]
        assert _registry.get_engine_adapter is original
    assert caught.value.code == "UNSUPPORTED_SANDBOX_TENANCY"
    assert "declares no per-conversation service launch" in caught.value.message
    assert provider.ran == ran_before, "nothing may run in the session first"


def test_two_conversations_get_two_ports() -> None:
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    a = _acquire(lease, "box-1")
    b = _acquire(lease, "box-1")
    assert runner_port_for_uid(a.uid) != runner_port_for_uid(b.uid)


def test_the_port_derivation_stays_inside_its_span() -> None:
    """A uid far past the span must still land on a port in the range, not
    somewhere in the ephemeral range or on the image's own runner."""
    for uid in (CONVERSATION_UID_BASE, 2499, 9999, 59999):
        port = runner_port_for_uid(uid)
        assert RUNNER_PORT_BASE <= port < RUNNER_PORT_BASE + RUNNER_PORT_SPAN
        assert port != 8000, "must never collide with the image's own runner"


# ── capacity, which is not capability ────────────────────────────────────
#
# A box that CAN isolate still kills every conversation in it when the next one
# exhausts the container's memory: the kernel takes the container, not the
# newcomer, so the conversation that broke the limit is not the one that pays.
# The numbers are in-box measurements: an idle box holds 385 MB, each
# conversation adds ~306 MB, and against a 1 GiB limit the third one OOM-kills
# the container while it reports itself Ready throughout.


def test_a_box_without_room_is_not_packed_with_another_conversation() -> None:
    """The refusal costs one packing decision, never a box full of work."""

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (1024**3, 1024**3 - 64 * 1024**2)  # 64 MB free of 1 GiB
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    assert _acquire(lease, "box-1") is None, (
        "a box with 64 MB free must not take a conversation that needs 320 MB"
    )
    assert provider.opened == [], "nothing may be opened in a box that has no room"


def test_a_box_with_room_still_takes_the_conversation() -> None:
    """The control: without it the refusal above proves only that it refuses."""

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (3 * 1024**3, 512 * 1024**2)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    placed = _acquire(lease, "box-1")

    assert placed is not None
    assert placed.sandbox_id == "box-1"


def test_a_box_that_will_not_report_its_memory_is_not_packed() -> None:
    """No headroom proven is the safe reading of an unanswered probe.

    The cost of refusing a conversation is one refusal; the cost of admitting
    one too many is every conversation in the box.
    """

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (0, 0)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    assert _acquire(lease, "box-1") is None


def test_an_agent_pointing_at_a_destroyed_box_takes_one_of_its_own() -> None:
    """The pointer heals by being replaced, not by this question succeeding.

    An Agent row outlives the box it names: the last conversation left and the
    box was destroyed, or a node lost it, or a Pool reclaimed it. The next
    conversation must be able to take a box of its own — raising the probe's
    transport failure out of here would fail that conversation instead, for
    every cause of a box's death at once.
    """

    class _GoneProvider(_FakeProvider):
        async def read_memory_headroom(self, sandbox_id: str) -> tuple[int, int]:
            raise APIError(
                code="SANDBOX_GONE",
                message=f"sandbox {sandbox_id!r} does not answer",
                status_code=404,
            )

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    lease = SharedSandboxLease(agent_repo=repo, provider=_GoneProvider())

    assert _acquire(lease, "box-1") is None


def test_a_broken_memory_probe_is_raised_rather_than_read_as_no_room() -> None:
    """A refusal is a normal answer here, which is what hides a broken probe.

    This probe once read an attribute the vendor's result does not carry. It
    answered "no room" for every box, nothing was ever packed into a shared
    one, and the whole shared-tenancy path silently never ran. Swallowing
    anything but "the box did not answer" would restore exactly that.
    """

    class _BrokenProvider(_FakeProvider):
        async def read_memory_headroom(self, sandbox_id: str) -> tuple[int, int]:
            _ = sandbox_id
            raise AttributeError("'OpenSandboxHandle' object has no attribute 'output'")

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    lease = SharedSandboxLease(agent_repo=repo, provider=_BrokenProvider())

    with pytest.raises(AttributeError):
        _acquire(lease, "box-1")


def test_a_burst_of_conversations_is_not_all_admitted_from_one_reading() -> None:
    """The failure this rule exists for, reproduced.

    A 2 GiB box reads ~385 MB idle, so it has room for about five
    conversations. Admitted together they all read the same number — none has
    started using anything yet — and a rule that only measures answers "room
    for one more" to every one of them. Nine in one box exhausts it, and the
    container dies with every conversation in it.

    The box's reading never moves here, which is exactly the real case: the
    conversations are still preparing.
    """

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    # 2 GiB with an idle box in it. Room for five conversations at the floor,
    # and the reading stays put no matter how many are admitted.
    provider.memory = (2 * 1024**3, 385 * 1024**2)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    admitted = [_acquire(lease, "box-1") for _ in range(9)]
    accepted = [placement for placement in admitted if placement is not None]

    assert len(accepted) <= 5, (
        f"{len(accepted)} conversations were admitted from one unchanged "
        "reading; the box has room for about five"
    )
    assert accepted, "the first conversations must still be admitted"


def test_an_admission_stops_counting_once_the_box_can_see_it() -> None:
    """The control: the charge is a stand-in for a reading, not a second limit.

    Without it the rule above could be satisfied by never admitting anyone
    again, which would make every Agent's second conversation take a box of its
    own for ever.
    """

    import astrabox.core.service.orchestrator.runtime.shared_sandbox_lease as module

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (2 * 1024**3, 385 * 1024**2)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    assert _acquire(lease, "box-1") is not None
    # Age every admission past the grace, the way real time would.
    stale = time.time() - module.ADMISSION_GRACE_SECONDS - 1
    for entry in repo.agent.get(module.BOX_ADMISSIONS) or []:
        entry["at"] = stale

    assert module.young_admissions(repo.agent, "box-1") == [], (
        "an admission older than the grace must stop being charged"
    )
    assert _acquire(lease, "box-1") is not None, (
        "a box whose reading now covers its occupants must keep taking work"
    )


def test_a_lost_admission_race_does_not_admit_on_the_stale_reading() -> None:
    """Losing the compare-and-set means asking again, not proceeding anyway.

    The write is what serialises admissions; a loser that carried on would be
    admitting itself on a reading that the winner has already spent.
    """

    import astrabox.core.service.orchestrator.runtime.shared_sandbox_lease as module

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (2 * 1024**3, 385 * 1024**2)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    real_cas = repo.compare_and_update_agent
    refusals = {"left": 1}

    async def losing_cas(agent_id: str, **kwargs: Any) -> bool:
        if refusals["left"] and module.BOX_ADMISSIONS in (kwargs.get("updates") or {}):
            refusals["left"] -= 1
            # The rival's admission lands instead.
            repo.agent[module.BOX_ADMISSIONS] = [
                {"sandbox_id": "box-1", "at": time.time()}
            ]
            return False
        return await real_cas(agent_id, **kwargs)

    repo.compare_and_update_agent = losing_cas  # type: ignore[method-assign]

    placed = _acquire(lease, "box-1")

    assert placed is not None, "the retry must still place the conversation"
    charged = repo.agent.get(module.BOX_ADMISSIONS) or []
    assert len(charged) == 2, (
        f"both the rival and the retry must be charged, got {charged}"
    )


def test_a_two_gib_box_stops_at_the_measured_capacity() -> None:
    """The ceiling that was crossed, expressed in the numbers that crossed it.

    Measured, not estimated: an idle box reads 377 MB and a box carrying one
    conversation reads 951 MB. A 2 GiB box therefore holds two conversations
    and the third is the cliff. At a 320 MB reserve three were admitted, each
    grew past it, and the box reached `-2 MB free of 2048 MB` — the point where
    the kernel takes the container and every conversation in it.
    """

    import astrabox.core.service.orchestrator.runtime.shared_sandbox_lease as module

    idle = 377 * 1024**2
    per_conversation = 951 * 1024**2 - idle
    assert module.CONVERSATION_MEMORY_RESERVE_BYTES >= per_conversation, (
        "the reserve must cover what a conversation grows to, not what it "
        f"starts at: measured {per_conversation // 1024**2} MB, reserved "
        f"{module.CONVERSATION_MEMORY_RESERVE_BYTES // 1024**2} MB"
    )

    repo = _FakeAgentRepo({"agent_id": "a1", "sandbox_id": "box-1"})
    provider = _FakeProvider()
    provider.memory = (2 * 1024**3, idle)
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    accepted = [p for p in (_acquire(lease, "box-1") for _ in range(6)) if p is not None]

    assert 0 < len(accepted) <= 2, (
        f"{len(accepted)} conversations admitted into a 2 GiB box that measured "
        "as holding two"
    )
async def test_a_returning_conversation_keeps_its_uid(monkeypatch) -> None:
    """A re-borrow must own its durable home, so the uid must not change.

    p138's one product-grade failure: a conversation whose box was destroyed
    came back with a fresh uid on every placement (2000 → 2001 → … → 2004)
    and answered EACCES against its own session files — they exist on the
    shared workspace volume, owned by the first uid. The allocator's cursor
    never re-issues a number, so keeping the old uid is safe by
    construction.
    """

    import astrabox.core.service.orchestrator.runtime.shared_sandbox_lease as lease_module

    allocated: list[int] = []

    async def _allocate(agents, agent_id):
        allocated.append(4242)
        return 4242

    monkeypatch.setattr(
        lease_module, "allocate_agent_scoped_uid", _allocate
    )

    class _Provider:
        name = "open_sandbox"

        async def read_listening_ports(self, sandbox_id):
            return set()

        async def open_isolated_session(self, sandbox_id, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(session_id=f"iso-{kwargs.get('uid')}")

        async def run_in_isolated_session(self, *args, **kwargs):
            return 0, "ok", ""

        async def close_isolated_session(self, *args, **kwargs):
            return None

        async def prepare_isolated_workspace(self, *args, **kwargs):
            return None

    lease = lease_module.SharedSandboxLease(
        agent_repo=object(), provider=_Provider()
    )

    kept = await lease.open_unclaimed_slot(
        agent_id="a1",
        sandbox_id="box-1",
        home_dir="/home/conversations/conv_x",
        workspace_dir="/workspace",
        workspace_source_dir="/home/conversations/conv_x/workspace",
        requested_uid=2000,
    )
    assert kept.uid == 2000
    assert allocated == [], "a kept uid must not touch the allocator"

    fresh = await lease.open_unclaimed_slot(
        agent_id="a1",
        sandbox_id="box-1",
        home_dir="/home/conversations/conv_y",
        workspace_dir="/workspace",
        workspace_source_dir="/home/conversations/conv_y/workspace",
    )
    assert fresh.uid == 4242
    assert allocated == [4242]


def test_a_uid_whose_engine_port_is_already_bound_is_skipped() -> None:
    """The port a uid implies has 500 values; the uid space has 58,000.

    Two conversations of one agent whose uids differ by a multiple of the
    span ask for the same port, and on a shared box the second engine cannot
    bind — the placement dies with "did not come up on port N ... not
    listening", which reads like a slow box. Rare early, certain later,
    because the cursor only moves forward.
    """

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.listening = {runner_port_for_uid(CONVERSATION_UID_BASE)}
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    assert binding.uid == CONVERSATION_UID_BASE + 1, (
        "the colliding uid must be skipped, not reused or recycled"
    )
    assert runner_port_for_uid(binding.uid) not in provider.listening


def test_a_free_port_takes_the_first_uid_offered() -> None:
    """No collision means no skipping: the cursor is not to be burned."""

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.listening = {12345}
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    assert binding.uid == CONVERSATION_UID_BASE


def test_a_box_that_cannot_answer_the_probe_still_places() -> None:
    """An unanswered probe degrades to the old behaviour, never to a refusal."""

    repo, provider = _FakeAgentRepo(), _FakeProvider()

    async def _silent(sandbox_id: str) -> set[int]:
        return set()

    provider.read_listening_ports = _silent  # type: ignore[assignment]
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    assert binding.uid == CONVERSATION_UID_BASE


def test_a_box_holding_the_whole_span_is_refused_by_name() -> None:
    """Handing back a colliding uid would produce the unreadable timeout."""

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.listening = {
        runner_port_for_uid(CONVERSATION_UID_BASE + offset) for offset in range(64)
    }
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    with pytest.raises(APIError) as caught:
        _acquire(lease, "box-1")

    assert caught.value.code == "SANDBOX_CAPACITY_UNAVAILABLE"
    assert "engine-port collision" in caught.value.message


def test_every_session_binds_the_conversation_private_tmp() -> None:
    """Both sessions, because a sibling must not differ from its own kin."""

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    for opened in provider.opened:
        assert opened["extra_binds"] == [("/home/conversations/conv1/tmp", "/tmp")]


def test_a_uid_whose_upstream_port_is_bound_is_skipped_too() -> None:
    """A conversation reserves a pair, so half a check passes a doomed uid.

    An engine that fronts its own service derives the loopback upstream from
    the outward port by the same rule, and a uid whose upstream was taken
    failed to bind while the platform reported "did not come up on port
    <outward>" — naming the port that was free (p172).
    """

    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        runner_ports_for_uid,
    )

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    outward, upstream = runner_ports_for_uid(CONVERSATION_UID_BASE)
    assert upstream != outward
    provider.listening = {upstream}
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    binding = _acquire(lease, "box-1")

    assert binding is not None
    assert binding.uid == CONVERSATION_UID_BASE + 1
    assert not provider.listening.intersection(
        runner_ports_for_uid(binding.uid)
    )


def test_a_refusal_reads_the_launch_logs_of_every_engine_shape() -> None:
    """The reason has to reach the refusal, whichever engine wrote it.

    Launch redirects and service logs both live under the private home.
    Reading only one location loses errors from the other processes.
    """

    repo, provider = _FakeAgentRepo(), _FakeProvider()
    provider.runner_listening = 0
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-1")

    with pytest.raises(APIError):
        _start_runner(lease, binding)

    readback = [code for code in provider.ran if "tail -c" in code]
    assert readback, "the refusal must try to read the launch log at all"
    assert "/home/conversations/conv1/.astrabox-runner.log" in readback[-1]
    assert "/home/conversations/conv1/.astrabox-*-launch.log" in readback[-1]
    assert "/home/conversations/conv1/.astrabox-logs/*.log" in readback[-1]


def test_a_box_claimed_moments_ago_is_waited_for_rather_than_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent box that was just claimed is coming up, not gone.

    Both answer the isolation probe with silence, and treating them the same
    splits an Agent's conversations across two boxes -- the one thing shared
    tenancy exists to prevent. With several warm boxes in the pool the Agent's
    second conversation arrives during the only seconds the probe reliably
    says no, instead of waiting out a cold create.
    """
    from astrabox.core.service.orchestrator.runtime import (
        shared_sandbox_lease as lease_module,
    )

    monkeypatch.setattr(lease_module, "_BOX_CLAIM_RETRY_SECONDS", 0.0)
    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "sandbox_id": "box-sibling",
            lease_module.BOX_ADMISSIONS: [
                {"sandbox_id": "box-sibling", "at": time.time(), "session_id": "s1"}
            ],
        }
    )
    provider = _FakeProvider()
    provider.probe_error = RuntimeError("connection refused")

    # The sibling's box answers on the second look, the way a booting box does.
    original = provider.read_isolation_capability
    looks = {"n": 0}

    async def _answers_on_the_second_look(sandbox_id: str):
        looks["n"] += 1
        if looks["n"] >= 2:
            provider.probe_error = None
        return await original(sandbox_id)

    monkeypatch.setattr(
        provider, "read_isolation_capability", _answers_on_the_second_look
    )

    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    binding = _acquire(lease, "box-mine")

    assert binding is not None, "the conversation must land somewhere"
    assert binding.sandbox_id == "box-sibling", (
        "it belongs in the Agent's box, not in the one it happened to be holding "
        "while that box was still coming up"
    )
    assert repo.agent["sandbox_id"] == "box-sibling", (
        "and the Agent's pointer must not have been taken by the newcomer"
    )


def test_a_box_silent_past_the_warm_up_window_is_still_replaced() -> None:
    """The repair path is the reason silence means no in the first place.

    An Agent row can name a box that is gone. Waiting for one of those
    would strand every later conversation, so only a claim young enough to still
    be booting earns the wait.
    """
    from astrabox.core.service.orchestrator.runtime import (
        shared_sandbox_lease as lease_module,
    )

    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "sandbox_id": "box-dead",
            lease_module.BOX_ADMISSIONS: [
                {
                    "sandbox_id": "box-dead",
                    "at": time.time() - (lease_module._HELD_BOX_WARMUP_SECONDS + 5.0),
                    "session_id": "s1",
                }
            ],
        }
    )
    provider = _FakeProvider()
    provider.probe_error = RuntimeError("connection refused")

    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    assert _acquire(lease, "box-new") is None, (
        "an unreachable box past its warm-up is refused, and the conversation "
        "keeps the box it already has"
    )


def test_a_full_box_is_not_asked_again_just_because_it_was_claimed_recently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answered "no room" is a verdict, not a symptom of still booting.

    A box with 55 MB free of 2048 answers no every time it is asked; a retry
    that reads a bool cannot tell "the box answered no" from "the box did not
    answer", and asks four times over twelve seconds for the same answer.
    Only silence earns the wait.
    """
    from astrabox.core.service.orchestrator.runtime import (
        shared_sandbox_lease as lease_module,
    )

    monkeypatch.setattr(lease_module, "_BOX_CLAIM_RETRY_SECONDS", 0.0)
    repo = _FakeAgentRepo(
        {
            "agent_id": "a1",
            "sandbox_id": "box-full",
            lease_module.BOX_ADMISSIONS: [
                {"sandbox_id": "box-full", "at": time.time(), "session_id": "s1"}
            ],
        }
    )
    provider = _FakeProvider()
    # The box answers, and its answer is that one more conversation would take
    # the container down with it.
    provider.memory = (2 * 1024**3, 2 * 1024**3 - 55 * 1024**2)

    looks = {"n": 0}
    original = provider.read_isolation_capability

    async def _counted(sandbox_id: str):
        looks["n"] += 1
        return await original(sandbox_id)

    monkeypatch.setattr(provider, "read_isolation_capability", _counted)

    lease = SharedSandboxLease(agent_repo=repo, provider=provider)
    _acquire(lease, "box-mine")

    assert looks["n"] <= 2, (
        "a box that answered must not be re-probed on the warm-up path; it was "
        f"asked {looks['n']} times"
    )
def test_a_conversation_placed_again_keeps_the_owner_it_already_has() -> None:
    """The reborrow case: same conversation, second placement, one identity.

    Its account already exists on the box from the first placement, and the
    bootstrap validates what it finds against the uid it is given —
    `test "$(id -u "$user")" = "$requested_uid"`, which under `set -e` exits 1.
    Handing out a fresh number would be a different conversation's answer.
    """
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    first = _acquire(lease, "box-1")
    assert first is not None

    again = _acquire(lease, "box-1", uid=first.uid)
    assert again is not None
    assert again.uid == first.uid
    assert again.gid == first.gid


def test_a_conversation_with_no_owner_yet_is_allocated_a_fresh_one() -> None:
    """The control: allocation still happens for a conversation that has none.

    Without it, the case above would pass against a placement that reused some
    other conversation's number, or none at all — and the allocator's whole
    point is that two conversations of one agent never share an owner.
    """
    repo, provider = _FakeAgentRepo(), _FakeProvider()
    lease = SharedSandboxLease(agent_repo=repo, provider=provider)

    first = _acquire(lease, "box-1")
    second = _acquire(lease, "box-1")

    assert first is not None and second is not None
    assert second.uid != first.uid
