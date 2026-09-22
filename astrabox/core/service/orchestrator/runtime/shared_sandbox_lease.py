"""Place Agent conversations in provider-isolated sessions inside one box.

The agent owns the shared box and its UID allocation. Each conversation gets
separate PID, mount, and tmpfs namespaces plus a distinct POSIX owner.

Boundaries:

* Release closes one isolated session, not its sandbox. RuntimeManager checks
  remaining occupants before destroying the shared sandbox.
* Parking affects the whole sandbox, so one idle conversation cannot pause it.

Box claims and UID allocation use compare-and-set. Concurrent conversations for
one agent converge on the same box with different owners, and a losing caller
uses the winner's value.
"""

from __future__ import annotations

import dataclasses
import contextlib
import shlex
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    runner_port_for_uid,
    runner_ports_for_uid,
)
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    allocate_agent_scoped_uid,
)

logger = get_logger(__name__)

#: How many uids to try before calling a box's engine-port span exhausted.
#: Small on purpose: a healthy box holds a handful of conversations, so more
#: than a few collisions in a row means the span is genuinely full rather
#: than unlucky.
_UID_PORT_COLLISION_ATTEMPTS = 8

#: How many times a losing box claim re-reads before giving up. Contention is
#: per agent and only at the instant its FIRST conversations start, so this is
#: a backstop against a pathological row, not a retry budget.
_BOX_CLAIM_ATTEMPTS = 4

#: Warm-up window after claim. A failed isolation probe inside this window can
#: mean startup is in progress; after it expires the claim path may replace the
#: Agent's stale sandbox reference.
_HELD_BOX_WARMUP_SECONDS = 30.0

#: Between claim attempts, while an Agent's box is still coming up. Four
#: attempts at this interval cover the boot; the wait only happens to a
#: conversation that found a young, silent box, never to the one creating it.
_BOX_CLAIM_RETRY_SECONDS = 3.0



#: Runner log basename, independent of the runtime's private directory.
RUNNER_LOG_NAME = ".astrabox-runner.log"

#: Memory reserved for each conversation admitted to a shared sandbox. Admission
#: must account for engine growth after startup, not just idle memory. This is
#: not a per-conversation limit: tools can exceed it, and container-level OOM can
#: interrupt other conversations in the same sandbox.
CONVERSATION_MEMORY_RESERVE_BYTES = 640 * 1024 * 1024

#: Row field holding conversations admitted to a box whose memory the box does
#: not report yet.
BOX_ADMISSIONS = "box_admissions"

#: How long an admission is counted against a box before the box's own reading
#: covers it. A conversation is admitted seconds before its engine exists, so
#: `memory.current` answers for a conversation that has not started using
#: anything — and concurrent admissions all read the same low number and all
#: pass unless pending admissions also reserve memory.
#:
#: Generous on purpose. Counting a conversation for longer than it needed costs
#: one refusal, and the conversation keeps a box of its own; counting it for too
#: short a time costs every conversation in the container.
ADMISSION_GRACE_SECONDS = 120.0





def _is_warming_up(admissions: list[dict], *, now: float | None = None) -> bool:
    """Whether the box these admissions name was claimed inside the warm-up window.

    The most recent admission is the claim that matters: an older one belongs to
    a conversation that has already had its chance to bring the box up.
    """

    moment = time.time() if now is None else now
    stamps = [
        float(entry.get("at") or 0.0)
        for entry in admissions
        if isinstance(entry, dict)
    ]
    if not stamps:
        return False
    return moment - max(stamps) < _HELD_BOX_WARMUP_SECONDS


def young_admissions(agent: Any, sandbox_id: str, *, now: float | None = None) -> list[dict]:
    """Admissions to `sandbox_id` the box's own memory reading cannot see yet.

    Entries expire once memory readings cover the admitted work. A confirmed
    departure removes its entry earlier; a finished conversation needs neither
    a memory reservation nor protection from box reclamation.
    """

    moment = time.time() if now is None else now
    target = str(sandbox_id or "").strip()
    raw = (agent or {}).get(BOX_ADMISSIONS)
    if not isinstance(raw, (list, tuple)) or not target:
        return []
    live: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if str(item.get("sandbox_id") or "").strip() != target:
            continue
        try:
            at = float(item.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if moment - at < ADMISSION_GRACE_SECONDS:
            live.append(dict(item))
    return live


def surviving_admissions(agent: Any, *, now: float | None = None) -> list[dict]:
    """Every admission still inside its grace, for any box. Expired ones drop."""

    moment = time.time() if now is None else now
    raw = (agent or {}).get(BOX_ADMISSIONS)
    if not isinstance(raw, (list, tuple)):
        return []
    kept: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            at = float(item.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if moment - at < ADMISSION_GRACE_SECONDS:
            kept.append(dict(item))
    return kept


async def release_box_admission(
    *, agent_repo: Any, agent_id: str, sandbox_id: str, session_id: str
) -> None:
    """Withdraw only the placement whose isolated sessions are confirmed closed."""

    for _attempt in range(_BOX_CLAIM_ATTEMPTS):
        row = await agent_repo.get_agent(agent_id)
        raw = (row or {}).get(BOX_ADMISSIONS)
        if not isinstance(raw, list):
            return
        kept = [
            entry for entry in raw
            if not (
                isinstance(entry, dict)
                and entry.get("sandbox_id") == sandbox_id
                and entry.get("session_id") == session_id
            )
        ]
        if len(kept) == len(raw):
            return
        if await agent_repo.compare_and_update_agent(
            agent_id,
            expected={BOX_ADMISSIONS: raw},
            updates={BOX_ADMISSIONS: kept},
        ):
            return
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"agent {agent_id!r} admissions changed repeatedly while releasing "
            f"Session {session_id!r} from sandbox {sandbox_id!r}"
        ),
        status_code=503,
    )


@dataclass(slots=True)
class SharedSandboxBinding:
    """What one conversation holds under the shared mode.

    Both ids are durable and both are needed: ``sandbox_id`` is what every
    lifecycle operation addresses (connect, probe, renew — the control plane
    knows boxes, not sessions), and ``isolated_session_id`` is what the
    conversation actually runs in and what its teardown closes.
    """

    sandbox_id: str
    isolated_session_id: str
    terminal_isolated_session_id: str
    uid: int
    gid: int
    home_dir: str
    workspace_dir: str
    workspace_source_dir: str
    prepared_slot_id: str | None = None
    activation_token: str | None = dataclasses.field(default=None, repr=False)
    runtime_generation: str | None = None
    runner_port: int | None = None
    claim_manifest_path: str | None = None


class SharedSandboxLease:
    """Hand a conversation an isolated session in its agent's box."""

    def __init__(self, *, agent_repo: Any, provider: Any) -> None:
        self._agents = agent_repo
        self._provider = provider
        self._backend = str(getattr(provider, "name", "") or "").strip().lower()
        if not self._backend:
            raise ValueError("shared sandbox provider must declare its backend name")

    async def place_in_agent_box(
        self,
        *,
        agent_id: str,
        home_dir: str,
        workspace_dir: str,
        workspace_source_dir: str,
        candidate: str | None = None,
        expected_runtime_generation: str | None = None,
        expected_sandbox_generation: str | None = None,
        session_id: str | None = None,
        requested_uid: int | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SharedSandboxBinding | None:
        """Open this conversation's session in the agent's box.

        The runtime profile supplies both workspace paths. Processes inside the
        isolated session see ``workspace_dir``; the shared box stores those
        files in ``workspace_source_dir`` under the conversation's private home.

        The conversation's whole private HOME is prepared and passed through
        The provider seam's ``extra_writable`` field. Claude Code writes its
        config, cache, npm prefix and scratch files outside the workspace; a
        workspace-only session therefore starts successfully and fails on its
        first real turn. The home remains ``0700`` and owned by this
        conversation's unique uid, so a sibling session cannot traverse it.

        NOTHING IS PROVISIONED HERE. The caller acquires a box the ordinary way
        and offers it as ``candidate``; this only ever decides where the
        conversation goes. Provisioning speculatively to find out whether packing
        is possible costs the deployment its capability: the speculative box
        borrows from the pool, loses the claim race, gives the box back, finds
        the pool now empty and creates another box — wasting capacity by asking
        a placement question through provisioning.

        Returns None when there is no box that can carry more than one
        conversation. That is the packing decision, and None is how it is
        expressed: the caller has a perfectly good box either way, and nothing
        above has to be told which happened.
        """
        home = str(home_dir or "").rstrip("/")
        workspace = str(workspace_dir or "").rstrip("/")
        workspace_source = str(workspace_source_dir or "").rstrip("/")
        if (
            not home.startswith("/")
            or not workspace.startswith("/")
            or not workspace_source.startswith("/")
        ):
            raise APIError(
                code="CONVERSATION_IDENTITY_INVALID",
                message="shared placement requires absolute home, workspace, and workspace source",
                status_code=500,
            )
        sandbox_id = await self._claim_box(
            agent_id=agent_id,
            candidate=candidate,
            expected_runtime_generation=expected_runtime_generation,
            expected_sandbox_generation=expected_sandbox_generation,
            session_id=session_id,
        )
        if not sandbox_id:
            return None
        if on_progress is not None:
            await on_progress({"sandbox_id": sandbox_id})
        return await self.open_unclaimed_slot(
            agent_id=agent_id,
            sandbox_id=sandbox_id,
            home_dir=home,
            workspace_dir=workspace,
            workspace_source_dir=workspace_source,
            requested_uid=requested_uid,
            on_progress=on_progress,
        )

    async def _allocate_uid_free_of_port_collisions(
        self, *, agent_id: str, sandbox_id: str
    ) -> int:
        """A uid whose engine port nothing in this box is already using.

        The uid is monotonic across an agent's whole history, but the port it
        implies is ``RUNNER_PORT_BASE + uid % RUNNER_PORT_SPAN`` — 500 values.
        Uids separated by a multiple of the span request the same ports. Check
        both service ports against live listeners before choosing a uid to avoid
        an engine startup failure on a shared sandbox.

        The provider reads listeners from the sandbox instead of maintaining a
        separate port ledger. An unanswered probe yields an empty set, allowing
        placement to proceed without a collision precheck; the service's bind
        remains responsible for rejecting an occupied port.
        """

        taken = await self._provider.read_listening_ports(sandbox_id)
        attempts = 0
        while True:
            uid = await allocate_agent_scoped_uid(self._agents, agent_id)
            attempts += 1
            wanted = runner_ports_for_uid(uid)
            if not taken.intersection(wanted):
                return uid
            logger.info(
                "shared placement: uid=%s wants ports %s, %s already bound in "
                "box %s; taking the next uid",
                uid,
                list(wanted),
                sorted(taken.intersection(wanted)),
                sandbox_id,
            )
            if attempts >= _UID_PORT_COLLISION_ATTEMPTS:
                # Every candidate collided, which means this box is holding
                # (or leaking) most of the span. Refusing names that; handing
                # back a colliding uid would produce the unreadable timeout
                # this method exists to prevent.
                raise APIError(
                    code="SANDBOX_CAPACITY_UNAVAILABLE",
                    message=(
                        f"no conversation uid free of an engine-port collision "
                        f"in sandbox {sandbox_id!r} after {attempts} attempts; "
                        f"{len(taken)} ports are bound in the box"
                    ),
                    status_code=503,
                )

    async def open_unclaimed_slot(
        self,
        *,
        agent_id: str,
        sandbox_id: str,
        home_dir: str,
        workspace_dir: str,
        workspace_source_dir: str,
        requested_uid: int | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> SharedSandboxBinding:
        """Create one isolated placement without binding a platform Session."""

        home = str(home_dir or "").rstrip("/")
        workspace = str(workspace_dir or "").rstrip("/")
        workspace_source = str(workspace_source_dir or "").rstrip("/")
        if (
            not home.startswith("/")
            or not workspace.startswith("/")
            or not workspace_source.startswith("/")
        ):
            raise APIError(
                code="CONVERSATION_IDENTITY_INVALID",
                message=(
                    "isolated slot requires absolute home, workspace, and "
                    "workspace source"
                ),
                status_code=500,
            )
        # A conversation that lived before keeps its uid: its durable home
        # (on the shared workspace volume) is owned by that uid, and a fresh
        # allocation on every placement made a re-borrowed conversation
        # unable to read its own session files — EACCES on a path that
        # plainly exists. The allocator's cursor never re-issues a number,
        # so an old uid is this conversation's alone. An unclaimed slot has no
        # owner yet and takes the next one free of a port collision.
        reused_owner = requested_uid is not None and int(requested_uid) > 0
        if reused_owner:
            uid = int(requested_uid)
        else:
            uid = await self._allocate_uid_free_of_port_collisions(
                agent_id=agent_id, sandbox_id=sandbox_id
            )
        logger.info(
            "shared placement owner: agent=%s sandbox=%s uid=%s reused=%s",
            agent_id,
            sandbox_id,
            uid,
            reused_owner,
        )
        if on_progress is not None:
            await on_progress(
                {
                    "uid": uid,
                    "gid": uid,
                    "home_dir": home,
                    "workspace_dir": workspace,
                    "workspace_source_dir": workspace_source,
                }
            )
        # Before the session, not after: a provider may auto-create a missing
        # workspace as root. Prepare the private home and physical backing
        # directory first, then expose the home through the provider's isolated
        # session contract so runtime config/cache/tmp paths are writable too.
        await self._provider.prepare_isolated_workspace(
            sandbox_id, workspace_dir=home, uid=uid, gid=uid
        )
        await self._provider.prepare_isolated_workspace(
            sandbox_id, workspace_dir=workspace_source, uid=uid, gid=uid
        )
        # Bind a conversation-owned directory over the isolated session's /tmp.
        # Runtime caches and command isolation need a writable temporary path
        # without sharing their files with sibling conversations.
        private_tmp = f"{home}/tmp"
        await self._provider.prepare_isolated_workspace(
            sandbox_id, workspace_dir=private_tmp, uid=uid, gid=uid
        )
        opened = None
        terminal = None
        try:
            opened = await self._provider.open_isolated_session(
                sandbox_id,
                workspace_dir=workspace,
                workspace_source_dir=workspace_source,
                uid=uid,
                gid=uid,
                share_net=True,
                extra_writable=[home],
                extra_binds=[(private_tmp, "/tmp")],
            )
            if on_progress is not None:
                await on_progress({"isolated_session_id": opened.session_id})
            await self._confirm_writable(sandbox_id, opened.session_id, workspace)
            # A sibling isolation session lets terminal interruption delete the
            # terminal shell without killing the resident Agent runner. Both
            # sessions still see this conversation's files under the same uid.
            terminal = await self._provider.open_isolated_session(
                sandbox_id,
                workspace_dir=workspace,
                workspace_source_dir=workspace_source,
                uid=uid,
                gid=uid,
                share_net=True,
                extra_writable=[home],
                extra_binds=[(private_tmp, "/tmp")],
            )
            if on_progress is not None:
                await on_progress(
                    {"terminal_isolated_session_id": terminal.session_id}
                )
            await self._confirm_writable(
                sandbox_id, terminal.session_id, workspace
            )
        except BaseException:
            for child in (terminal, opened):
                if child is None:
                    continue
                try:
                    await self._provider.close_isolated_session(
                        sandbox_id, child.session_id
                    )
                except Exception:
                    pass
            raise
        logger.info(
            "shared sandbox: agent=%s box=%s agent_session=%s "
            "terminal_session=%s uid=%d",
            agent_id,
            sandbox_id,
            opened.session_id,
            terminal.session_id,
            uid,
        )
        return SharedSandboxBinding(
            sandbox_id=sandbox_id,
            isolated_session_id=opened.session_id,
            terminal_isolated_session_id=terminal.session_id,
            uid=uid,
            gid=uid,
            home_dir=home,
            workspace_dir=workspace,
            workspace_source_dir=workspace_source,
        )

    async def start_runner(
        self,
        binding: SharedSandboxBinding,
        *,
        launch: str | None,
        engine_label: str,
    ) -> str:
        """Adopt or start this conversation's engine service; return its port.

        A listening service survives platform client eviction and is reused.
        The engine adapter's subsequent handshake proves protocol readiness;
        the port check only prevents a duplicate launch in this placement.

        The launch LINE is the engine's declaration
        (``shared_conversation_service_launch``): only the adapter knows how
        its own service starts, and the binary it names is the image's,
        already there. What this method owns is placement — the session, the
        derived port, running the line inside the namespace, and the
        listen-wait — which is identical for every engine. That split is the
        same one the tenancy platformization made for identity.

        The port is derived from the conversation's uid rather than tracked,
        because the two are handed out by the same monotonic counter and a box
        holds far fewer conversations than the span. A collision is therefore
        not expected — and if one happens it is LOUD: the second service cannot
        bind, this refuses, and nobody is handed a link to another
        conversation's service.

        Only the network namespace is shared (``share_net``), which is what
        lets the host reach the port at all. The PID, mount and tmpfs
        namespaces are the session's, so the process is the conversation's
        alone and dies when its session is closed.
        """
        port = runner_port_for_uid(binding.uid)
        if not launch:
            raise APIError(
                code="UNSUPPORTED_SANDBOX_TENANCY",
                message=(
                    f"engine {engine_label!r} declares no per-conversation "
                    "service launch; its shared placement cannot be completed"
                ),
                status_code=409,
            )
        exit_code, out, _err = await self._provider.run_in_isolated_session(
            binding.sandbox_id,
            binding.isolated_session_id,
            code=f"(ss -ltn 2>/dev/null || netstat -ltn) | grep -c ':{port} '",
            timeout_s=30.0,
        )
        listening = out.strip().splitlines()[-1].strip() if out.strip() else "0"
        if exit_code == 0 and listening not in ("0", ""):
            return str(port)
        # Poll for the listen socket instead of a fixed sleep. The bind is
        # ~150ms in an idle box, but the measured distribution on a full lane
        # is bimodal — 31 binds in 2-5s and 22 in 10-16s, tracking node load,
        # not the engine — so the old 10s bound sat inside the slow mode and
        # failed honest starts by a second or two. 30s is outside everything
        # observed while still far under the run's own 60s ceiling. On
        # failure the trigger's launch log is read back: the launch line
        # redirects the whole service start into it, so without this the
        # refusal carried literally nothing (the ": 0" of one campaign's
        # forensics was grep's count output).
        exit_code, out, err = await self._provider.run_in_isolated_session(
            binding.sandbox_id,
            binding.isolated_session_id,
            code=(
                f"{launch}\n"
                f"i=0\n"
                f"until (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) "
                f"| grep -q ':{port} '; do\n"
                f"  i=$((i+1)); [ $i -gt 600 ] && break\n"
                f"  sleep 0.05\n"
                f"done\n"
                f"(ss -ltn 2>/dev/null || netstat -ltn) | grep -c ':{port} '"
            ),
            timeout_s=60.0,
        )
        listening = out.strip().splitlines()[-1].strip() if out.strip() else "0"
        if exit_code != 0 or listening == "0":
            launch_log_tail = ""
            with contextlib.suppress(Exception):
                _, log_out, _ = await self._provider.run_in_isolated_session(
                    binding.sandbox_id,
                    binding.isolated_session_id,
                    # Launch redirects and service logs both belong to the
                    # private home, never the user workspace.
                    code=(
                        "tail -c 1200 "
                        f"{shlex.quote(binding.home_dir)}/.astrabox-runner.log "
                        f"{shlex.quote(binding.home_dir)}/"
                        ".astrabox-*-launch.log "
                        f"{shlex.quote(binding.home_dir)}/.astrabox-logs/*.log "
                        "2>/dev/null || true"
                    ),
                    timeout_s=15.0,
                )
                launch_log_tail = " ".join(log_out.split())[:600]
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"the conversation's engine service did not come up on port {port} in "
                    f"session {binding.isolated_session_id!r} of sandbox "
                    f"{binding.sandbox_id!r}: "
                    f"{err.strip() or 'not listening'}"
                    + (
                        f"; launch log tail: {launch_log_tail}"
                        if launch_log_tail
                        else "; launch log unavailable"
                    )
                ),
                status_code=502,
            )
        logger.info(
            "shared sandbox: conversation service up box=%s session=%s port=%d",
            binding.sandbox_id, binding.isolated_session_id, port,
        )
        return str(port)

    async def release(self, binding: SharedSandboxBinding) -> None:
        """Close both conversation sessions. The box stays for the others."""
        failure: Exception | None = None
        for session_id in (
            binding.terminal_isolated_session_id,
            binding.isolated_session_id,
        ):
            if not str(session_id or "").strip():
                continue
            try:
                await self._provider.close_isolated_session(
                    binding.sandbox_id, session_id
                )
            except Exception as exc:
                failure = failure or exc
        logger.info(
            "shared sandbox: released agent_session=%s terminal_session=%s "
            "from box=%s",
            binding.isolated_session_id,
            binding.terminal_isolated_session_id,
            binding.sandbox_id,
        )
        if failure is not None:
            raise failure

    async def restore_existing(
        self,
        *,
        sandbox_id: str,
        isolated_session_id: str,
        terminal_isolated_session_id: str | None = None,
        home_dir: str,
        workspace_dir: str,
        workspace_source_dir: str,
        uid: int,
        gid: int,
    ) -> SharedSandboxBinding:
        """Restore and verify the placement persisted on a conversation row."""
        home = str(home_dir or "").rstrip("/")
        workspace = str(workspace_dir or "").rstrip("/")
        workspace_source = str(workspace_source_dir or "").rstrip("/")
        binding = SharedSandboxBinding(
            sandbox_id=str(sandbox_id),
            isolated_session_id=str(isolated_session_id),
            terminal_isolated_session_id=str(
                terminal_isolated_session_id or ""
            ).strip(),
            uid=int(uid),
            gid=int(gid),
            home_dir=home,
            workspace_dir=workspace,
            workspace_source_dir=workspace_source,
        )
        code = (
            f'test "$(id -u)" = {int(uid)} && '
            f'test "$(id -g)" = {int(gid)} && '
            f"test -d {shlex.quote(home)} && "
            f"test -w {shlex.quote(workspace)}"
        )
        exit_code, out, err = await self._provider.run_in_isolated_session(
            binding.sandbox_id,
            binding.isolated_session_id,
            code=code,
            timeout_s=30.0,
        )
        if exit_code != 0:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    "persisted isolated session does not match this conversation "
                    f"(box={binding.sandbox_id!r}, session="
                    f"{binding.isolated_session_id!r}, uid={binding.uid}): "
                    f"{err.strip() or out.strip() or f'exit {exit_code}'}"
                ),
                status_code=409,
            )
        if binding.terminal_isolated_session_id:
            terminal_usable = True
            try:
                terminal_exit, _terminal_out, _terminal_err = (
                    await self._provider.run_in_isolated_session(
                        binding.sandbox_id,
                        binding.terminal_isolated_session_id,
                        code=code,
                        timeout_s=30.0,
                    )
                )
                terminal_usable = terminal_exit == 0
            except TimeoutError:
                # A host restart can leave a foreground run with no SSE owner.
                # This session is intentionally disposable; delete it instead
                # of making every future terminal command queue behind it.
                terminal_usable = False
            except APIError as exc:
                if int(getattr(exc, "status_code", 0) or 0) not in (404, 409):
                    raise
                terminal_usable = False
            if not terminal_usable:
                await self._provider.close_isolated_session(
                    binding.sandbox_id,
                    binding.terminal_isolated_session_id,
                )
                binding.terminal_isolated_session_id = ""
        if not binding.terminal_isolated_session_id:
            terminal = await self._provider.open_isolated_session(
                binding.sandbox_id,
                workspace_dir=binding.workspace_dir,
                workspace_source_dir=binding.workspace_source_dir,
                uid=binding.uid,
                gid=binding.gid,
                share_net=True,
                extra_writable=[binding.home_dir],
                # The replacement terminal sees the same private /tmp as the
                # session it replaces; a sibling with the root-owned tmpfs
                # would behave differently from its own predecessor.
                extra_binds=[(f"{binding.home_dir.rstrip('/')}/tmp", "/tmp")],
            )
            try:
                await self._confirm_writable(
                    binding.sandbox_id,
                    terminal.session_id,
                    binding.workspace_dir,
                )
            except BaseException:
                await self._provider.close_isolated_session(
                    binding.sandbox_id, terminal.session_id
                )
                raise
            binding.terminal_isolated_session_id = terminal.session_id
        return binding

    async def _confirm_writable(
        self, sandbox_id: str, session_id: str, workspace_dir: str
    ) -> None:
        """Check workspace access as the session account without changing files."""
        workspace = shlex.quote(workspace_dir)
        code = f"test -d {workspace} && test -w {workspace} && test -x {workspace}"
        exit_code, _out, err = await self._provider.run_in_isolated_session(
            sandbox_id, session_id, code=code, timeout_s=30.0
        )
        if exit_code == 0:
            return
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message=(
                f"the isolated session cannot write its workspace {workspace_dir!r} "
                f"in sandbox {sandbox_id!r}: {err.strip() or f'exit {exit_code}'}"
            ),
            status_code=502,
        )

    # -- the box claim ------------------------------------------------------

    async def _claim_box(
        self,
        *,
        agent_id: str,
        candidate: str | None,
        expected_runtime_generation: str | None,
        expected_sandbox_generation: str | None,
        session_id: str | None = None,
    ) -> str:
        """The agent's box if it has a usable one, else the candidate if it can
        host, else "" — meaning nothing here can carry a second conversation."""
        for _ in range(_BOX_CLAIM_ATTEMPTS):
            agent = await self._agents.get_agent(agent_id)
            if not isinstance(agent, dict):
                # Nothing to share with. Not this layer's error to raise: the
                # conversation still has its box.
                logger.info(
                    "shared sandbox: no agent row for %s; no box to share", agent_id
                )
                return ""
            held = str(agent.get("sandbox_id") or "").strip()
            held_backend = str(agent.get("sandbox_backend") or "").strip().lower()
            expected_runtime = str(expected_runtime_generation or "").strip()
            configured_runtime = str(
                agent.get("_runtime_generation") or ""
            ).strip()
            if (
                expected_runtime
                and configured_runtime
                and configured_runtime != expected_runtime
            ):
                logger.info(
                    "shared sandbox: agent=%s changed while this conversation "
                    "was acquiring a box (conversation=%s current=%s); keeping "
                    "the candidate dedicated",
                    agent_id,
                    expected_runtime,
                    configured_runtime,
                )
                return ""
            expected_sandbox = str(expected_sandbox_generation or "").strip()
            held_sandbox_generation = str(
                agent.get("_resident_sandbox_generation") or ""
            ).strip()
            held_is_current = (
                not expected_sandbox or held_sandbox_generation == expected_sandbox
            )
            if held and held_backend in ("", self._backend) and held_is_current:
                pending_entries = young_admissions(agent, held)
                pending = len(pending_entries)
                # One probe, and both decisions are read from it: the probe is a
                # round trip into the box, and asking twice on the ordinary path
                # would pay for it on every conversation that joins.
                answered, can_host = await self._host_verdict(held, pending=pending)
                if not can_host and not answered and _is_warming_up(pending_entries):
                    # Claimed seconds ago and not answering yet: coming up, not
                    # gone. Replacing the pointer here would put this Agent's
                    # conversations in two different boxes.
                    logger.info(
                        "shared sandbox: agent=%s box=%s was claimed moments ago "
                        "and has not answered its isolation probe; waiting for it "
                        "rather than taking the pointer",
                        agent_id,
                        held,
                    )
                    await asyncio.sleep(_BOX_CLAIM_RETRY_SECONDS)
                    continue
                if can_host:
                    if not await self._record_admission(
                        agent_id=agent_id,
                        agent=agent,
                        sandbox_id=held,
                        session_id=session_id,
                    ):
                        # Another conversation was admitted between the reading
                        # and the write. Re-read and ask again with it counted.
                        continue
                    if held_backend:
                        return held
                    # Older rows may predate the paired backend field. Repair it
                    # before handing the box out: after a process restart, this
                    # row is the only authoritative route for renew and cleanup.
                    if await self._record_backend_for_held_box(
                        agent_id=agent_id,
                        agent=agent,
                        sandbox_id=held,
                    ):
                        return held
                    continue
            elif held and held_backend not in ("", self._backend):
                logger.warning(
                    "shared sandbox: agent=%s names box=%s from backend=%s; "
                    "it cannot be joined through backend=%s",
                    agent_id,
                    held,
                    held_backend,
                    self._backend,
                )
            elif held and not held_is_current:
                logger.info(
                    "shared sandbox: agent=%s names stale box=%s "
                    "resident_generation=%s expected_generation=%s; "
                    "a new conversation will replace the pointer",
                    agent_id,
                    held,
                    held_sandbox_generation or "<missing>",
                    expected_sandbox,
                )
            fresh = str(candidate or "").strip()
            if not fresh:
                # No candidate offered yet: the caller asked before acquiring
                # one, which is the cheap first question — does this agent
                # already hold a box the conversation can join?
                return ""
            candidate_pending = len(young_admissions(agent, fresh))
            if not await self._can_host(fresh, pending=candidate_pending):
                # This box cannot carry a second conversation, so there is
                # nothing to claim and nothing to share. NOT an error, and NOT
                # a reason to replace the box: the caller keeps it.
                return ""
            # Remote probes leave time for siblings to join or depart. Preserve
            # their current admissions without reusing a capacity verdict after
            # the candidate gained reservations or its placement context changed.
            current = await self._agents.get_agent(agent_id)
            if not isinstance(current, dict):
                return ""
            placement_fields = (
                "sandbox_id", "sandbox_backend", "_runtime_generation",
                "_sandbox_generation", "_resident_sandbox_generation",
            )
            if any(current.get(key) != agent.get(key) for key in placement_fields):
                continue
            if len(young_admissions(current, fresh)) > candidate_pending:
                continue
            agent = current
            expected: dict[str, Any] = {
                "sandbox_id": (
                    agent.get("sandbox_id")
                    if "sandbox_id" in agent
                    else {"$exists": False}
                )
            }
            expected["sandbox_backend"] = (
                agent.get("sandbox_backend")
                if "sandbox_backend" in agent
                else {"$exists": False}
            )
            if expected_runtime:
                expected["_runtime_generation"] = (
                    agent.get("_runtime_generation")
                    if "_runtime_generation" in agent
                    else {"$exists": False}
                )
            if expected_sandbox:
                expected["_sandbox_generation"] = expected_sandbox
            # The claim charges its own conversation in the same write, so the
            # box is never published as the agent's with nothing recorded
            # against it — the window in which every later asker would read an
            # empty list.
            charged = surviving_admissions(agent)
            fresh_entry: dict[str, Any] = {"sandbox_id": fresh, "at": time.time()}
            if str(session_id or "").strip():
                fresh_entry["session_id"] = str(session_id).strip()
            charged.append(fresh_entry)
            expected[BOX_ADMISSIONS] = (
                agent.get(BOX_ADMISSIONS)
                if BOX_ADMISSIONS in agent else {"$exists": False}
            )
            updates = {
                "sandbox_id": fresh,
                "sandbox_backend": self._backend,
                BOX_ADMISSIONS: charged,
            }
            if expected_sandbox:
                updates["_resident_sandbox_generation"] = expected_sandbox
            claimed = await self._agents.compare_and_update_agent(
                agent_id,
                # Match the exact observed shape. A never-claimed row has no
                # field; a deliberately invalidated resident has a null field.
                # Treating both as the same `$in` predicate fails for a missing
                # Mongo field and makes the first post-upgrade claim exhaust.
                expected=expected,
                updates=updates,
            )
            if claimed:
                return fresh
            # Somebody else's box won the claim. The candidate is NOT
            # discarded — the caller acquired it and is still holding it, and
            # the next turn of this loop re-reads the agent and joins the
            # winner instead.
            logger.info(
                "shared sandbox: lost the box claim for agent=%s (candidate %s)",
                agent_id, fresh,
            )
        # Never fatal. Not settling means "no sharing", and a conversation with
        # a box of its own is a working conversation — failing it here would
        # kill a session over a question whose answer is optional.
        logger.warning(
            "shared sandbox: could not settle a box for agent=%s after %d attempts; "
            "this conversation keeps its own box",
            agent_id, _BOX_CLAIM_ATTEMPTS,
        )
        return ""

    async def _record_admission(
        self,
        *,
        agent_id: str,
        agent: dict,
        sandbox_id: str,
        session_id: str | None = None,
    ) -> bool:
        """Charge this conversation to the box, or report losing the race.

        Compare-and-update on the same row the box claim uses, so two
        conversations admitted at the same instant cannot both write the list
        they each read. The loser re-reads and asks about capacity again with
        the winner counted, which is the whole point: the answer to "is there
        room for one more" has to be given once per asker, not once per
        reading.
        """

        observed = agent.get(BOX_ADMISSIONS)
        kept = surviving_admissions(agent)
        entry: dict[str, Any] = {"sandbox_id": str(sandbox_id), "at": time.time()}
        # Who was admitted, so a disposal asking "is anyone else coming" can
        # tell its own admission from a stranger's. An entry without it (older
        # writers) reads as a stranger — the conservative side.
        if str(session_id or "").strip():
            entry["session_id"] = str(session_id).strip()
        kept.append(entry)
        return bool(
            await self._agents.compare_and_update_agent(
                agent_id,
                expected={
                    BOX_ADMISSIONS: (
                        observed if BOX_ADMISSIONS in agent else {"$exists": False}
                    )
                },
                updates={BOX_ADMISSIONS: kept},
            )
        )

    async def _record_backend_for_held_box(
        self,
        *,
        agent_id: str,
        agent: dict[str, Any],
        sandbox_id: str,
    ) -> bool:
        expected_backend: Any = (
            agent.get("sandbox_backend")
            if "sandbox_backend" in agent
            else {"$exists": False}
        )
        return await self._agents.compare_and_update_agent(
            agent_id,
            expected={
                "sandbox_id": sandbox_id,
                "sandbox_backend": expected_backend,
            },
            updates={"sandbox_backend": self._backend},
        )

    async def _can_host(self, sandbox_id: str, *, pending: int = 0) -> bool:
        """Ask the BOX, never the configuration.

        A box the agent still names may be gone, or may have come back from a
        pool whose template does not grant the privilege level. Either way
        the honest answer comes from the box, and any failure to get one counts
        as no — a shared mode that proceeded on an unanswered probe would open
        sessions that isolate nothing.
        """
        _answered, hosts = await self._host_verdict(sandbox_id, pending=pending)
        return hosts

    async def _host_verdict(
        self, sandbox_id: str, *, pending: int = 0
    ) -> tuple[bool, bool]:
        """``(the box answered, it can host)``.

        Callers that only decide where a conversation goes want the second
        value. The one deciding whether to WAIT wants the first: a box that
        answered "no room" has given a verdict and will give the same one again,
        while a box that did not answer may simply not be up yet. Collapsing
        them made a warm-up retry ask a full box four times over twelve seconds.
        """
        try:
            capability = await self._provider.read_isolation_capability(sandbox_id)
        except Exception as exc:  # noqa: BLE001 — an unusable box is the answer
            logger.info(
                "shared sandbox: box %s did not answer the isolation probe: %s",
                sandbox_id, exc,
            )
            return False, False
        if not capability.available:
            logger.info(
                "shared sandbox: box %s cannot host sessions: %s",
                sandbox_id, capability.detail,
            )
            return True, False
        return True, await self._has_room(sandbox_id, pending=pending)

    async def _has_room(self, sandbox_id: str, *, pending: int = 0) -> bool:
        """Whether this box can carry one more conversation without dying.

        Capability and capacity are different questions and the box answers
        both. A box that CAN isolate still kills every conversation in it when
        the next one exhausts the container's memory — the kernel's OOM killer
        takes the container, not the newcomer, so the conversation that broke
        the limit is not the one that pays.

        Refusing here does not refuse the conversation: the caller keeps the
        box it already acquired and the conversation runs there alone. The
        packing decision is the only thing given up.

        An unreachable sandbox returns False so the caller can retain its own
        acquired sandbox. Repairing the Agent's stale sandbox reference belongs
        to the claim path, not this capacity check.
        """

        try:
            limit, current = await self._provider.read_memory_headroom(sandbox_id)
        except Exception as exc:
            # Only an unreachable sandbox is a normal capacity refusal. Other
            # errors must propagate so a broken memory probe cannot silently
            # disable sharing for every conversation.
            from astrabox.providers.open_sandbox.sandbox import box_is_unreachable

            gone = (
                isinstance(exc, APIError) and str(getattr(exc, "code", "")) == "SANDBOX_GONE"
            ) or box_is_unreachable(exc)
            if not gone:
                raise
            logger.info(
                "shared sandbox: box %s did not answer for its memory (%s); "
                "not packing another conversation into it",
                sandbox_id,
                exc,
            )
            return False
        if limit <= 0:
            logger.info(
                "shared sandbox: box %s reported no memory limit; not packing "
                "another conversation into it",
                sandbox_id,
            )
            return False
        # Each conversation admitted within the grace is charged its floor even
        # though the box does not report it yet. Without this the question is
        # "is there room for one more", asked concurrently, and answered yes to
        # every asker from the same reading.
        promised = CONVERSATION_MEMORY_RESERVE_BYTES * max(int(pending), 0)
        headroom = limit - current - promised
        if headroom < CONVERSATION_MEMORY_RESERVE_BYTES:
            logger.info(
                "shared sandbox: box %s has %d MB free of %d MB, below the %d MB "
                "one more conversation needs; keeping this conversation separate",
                sandbox_id,
                headroom // (1024 * 1024),
                limit // (1024 * 1024),
                CONVERSATION_MEMORY_RESERVE_BYTES // (1024 * 1024),
            )
            return False
        return True
