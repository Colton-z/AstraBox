"""HTTPS repository contracts for Agent runtime plugins.

Public plugins are normal Git repositories.  They must not require an SSH
deploy key merely because the sandbox backend can reach port 22, and the
prewarm path must pass the declared protocol through to the shared clone
strategy instead of rejecting it before the clone starts.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrabox.core.service.orchestrator.runtime.storage import _git_clone
from astrabox.core.service.orchestrator.runtime.storage import _plugin_cache
from astrabox.core.service.orchestrator.runtime.plugin_repos import plugin_repo_egress_hosts


class _Commands:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.calls: list[str] = []
        self._results = list(results or [])

    async def run(self, command: str) -> Any:
        self.calls.append(command)
        if self._results:
            return self._results.pop(0)
        return SimpleNamespace(error=None, exit_code=0, stdout="")


def test_https_plugin_repository_host_is_available_to_the_sandbox_policy() -> None:
    assert plugin_repo_egress_hosts(
        {
            "plugin_repos": [
                {
                    "url": "https://GitHub.com/anthropics/financial-services.git",
                    "protocol": "https",
                }
            ]
        }
    ) == ["github.com"]


@pytest.mark.asyncio
async def test_public_https_clone_does_not_require_an_ssh_key(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _Commands()
    underlying = SimpleNamespace(commands=commands)
    monkeypatch.setattr(_git_clone, "_underlying_requires_https_git", lambda _sandbox: False)
    monkeypatch.setattr(
        _git_clone,
        "_resolve_git_https_token",
        lambda *, required=True: None if not required else pytest.fail("public clone required a token"),
    )

    await _git_clone._clone_git_repo_in_sandbox(
        underlying,
        ssh_url="https://github.com/anthropics/claude-plugins-official.git",
        protocol="https",
        target="/opt/plugins/official",
        branch="main",
        depth=1,
        deploy_key_secret_name=None,
        identity=None,
        error_code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
        label="plugin_repos[0]",
    )

    assert len(commands.calls) == 1
    assert "git clone" in commands.calls[0]
    assert "https://github.com/anthropics/claude-plugins-official.git" in commands.calls[0]
    assert "GIT_SSH_COMMAND" not in commands.calls[0]


@pytest.mark.asyncio
async def test_agent_prewarm_accepts_https_plugin_repo_and_forwards_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _Commands(
        [
            SimpleNamespace(error="cache miss", exit_code=1, stdout=""),
            SimpleNamespace(error=None, exit_code=0, stdout=""),
            SimpleNamespace(error=None, exit_code=0, stdout="EXISTS\n"),
            SimpleNamespace(
                error=None,
                exit_code=0,
                stdout="AGENT_RUNTIME_PLUGIN_CACHE_READY hash=test cached=0\n",
            ),
        ]
    )
    underlying = SimpleNamespace(commands=commands)
    cloned: list[dict[str, Any]] = []

    async def _capture_clone(*_args: Any, **kwargs: Any) -> None:
        cloned.append(kwargs)

    monkeypatch.setattr(_plugin_cache, "_clone_git_repo_in_sandbox", _capture_clone)

    template = {
        "plugin_repos": [
            {
                "url": "https://github.com/anthropics/claude-plugins-official.git",
                "protocol": "https",
                "branch": "main",
                "depth": 1,
                "plugin_paths": ["plugins/frontend-design"],
            }
        ]
    }
    result = await _plugin_cache.prepare_agent_runtime_plugin_cache(
        underlying,
        template,
        get_underlying_sandbox_fn=lambda sandbox: sandbox,
    )

    assert result is None
    assert len(cloned) == 1
    assert cloned[0]["protocol"] == "https"
    assert cloned[0]["ssh_url"] == "https://github.com/anthropics/claude-plugins-official.git"


_AGENT_CACHE = "/opt/conversation-runtime/claude/plugin-repos-cache/repos/agent-runtime-cache/00-x"


def test_two_clones_of_one_target_get_their_own_staging_directories() -> None:
    """Conversations of one Agent share a box and share that Agent's cache.

    A staging name derived from the target alone is the same name for all of
    them, and the cleanup that follows a finished copy runs outside the clone's
    lock — so one conversation deletes the directory another is cloning into,
    and git fails writing a `.git/config` whose parent has just gone.
    """

    first = _git_clone._clone_staging_dir(_AGENT_CACHE)
    second = _git_clone._clone_staging_dir(_AGENT_CACHE)

    assert first != second, "two clones of one target must not share scratch space"
    assert first.startswith("/tmp/clone-stage/"), first
    assert second.startswith("/tmp/clone-stage/"), second


def test_the_clone_lock_still_names_the_shared_target() -> None:
    """The control for the test above, and the reason it is not enough alone.

    Making the scratch private removes the collision between staging
    directories; it must not also remove the serialisation of writes to the
    TARGET, which is the thing several conversations actually contend for. The
    lock is keyed on the target so private scratch and shared destination can
    both be true.
    """

    staging = _git_clone._clone_staging_dir(_AGENT_CACHE)
    command = _git_clone._guarded_clone_cmd(
        _AGENT_CACHE, "git clone --quiet URL " + staging, clean=staging
    )

    assert _git_clone._clone_lock_path(_AGENT_CACHE) in command, (
        f"the lock must name the shared target, got: {command}"
    )
    assert _git_clone._clone_lock_path(staging) not in command, (
        "a lock keyed on private scratch serialises nothing"
    )
    # The clean is the scratch it is about to clone into, not the target it
    # will copy onto — removing the target here would delete a sibling's cache.
    assert f"rm -rf -- {staging}" in command, command
    assert f"rm -rf -- {_AGENT_CACHE}" not in command, command


def test_the_staging_copy_publishes_each_file_whole(tmp_path: Path) -> None:
    """A reader during the copy must see a whole file or no file.

    The cache is read by an Agent's other conversations while one of them
    writes it, and a reader that opened a plugin declaration mid-write failed
    the runtime start with `invalid JSON: Expecting ',' delimiter`.

    Watched WHILE the copy runs, not after it. A finished direct copy also
    leaves a whole file, so an outcome check passes either way and proves
    nothing about the thing under test — this one caught a direct copy in the
    act, which is the only observation that separates them.
    """

    source = tmp_path / "staging"
    destination = tmp_path / "cache"
    source.mkdir()
    # Big enough that a direct copy is observably partial for a while.
    payload = json.dumps({"mcpServers": {f"s{n}": {"url": "x" * 2000} for n in range(20000)}})
    (source / ".mcp.json").write_text(payload)

    command = _git_clone._remote_staging_copy_command(str(source), str(destination))
    landed = destination / ".mcp.json"
    torn: list[int] = []

    def watch() -> None:
        # `stat`, not a read: an observation must be cheaper than the thing it
        # is watching. Reading the whole file each round takes as long as the
        # copy does, so the loop steps over the window it exists to see.
        while not stop.is_set():
            try:
                seen = landed.stat().st_size
            except (FileNotFoundError, NotADirectoryError):
                continue
            if seen and seen != len(payload):
                torn.append(seen)

    stop = threading.Event()
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        completed = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    finally:
        stop.set()
        watcher.join(timeout=5)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(landed.read_text())
    assert torn == [], (
        f"a reader saw {len(torn)} partial version(s) of the declaration, "
        f"sizes {sorted(set(torn))[:5]} against {len(payload)}"
    )
    assert list(destination.rglob("*.part-*")) == [], (
        "a partial file was left behind for a reader to find"
    )


def test_the_staging_copy_keeps_the_executable_bit(tmp_path: Path) -> None:
    """Why this uses `shutil.copy` and not `copyfile`.

    A staging clone is a git checkout honouring committed modes. Losing them
    leaves the non-root conversation runtime with plugin scripts it can neither
    run nor chmod, because the cache is root-owned.
    """

    source = tmp_path / "staging"
    destination = tmp_path / "cache"
    source.mkdir()
    script = source / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)

    completed = subprocess.run(
        ["bash", "-c", _git_clone._remote_staging_copy_command(str(source), str(destination))],
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (destination / "run.sh").stat().st_mode & 0o111, "lost the executable bit"


def test_the_revision_pin_runs_in_staging_under_the_clone_lock() -> None:
    """A checkout after publication rewrites the cache under its readers.

    The cache is a directory an Agent's other conversations read. `git checkout`
    rewrites its working tree one file at a time, so a sibling scanning it
    opened a plugin declaration mid-rewrite and the runtime start failed on
    `invalid JSON`. Pinning the revision inside the clone keeps every write on
    the private staging tree, and the shared target only ever receives a tree
    already at the right revision.
    """

    staging = "/tmp/clone-stage/x-abc"
    target = "/opt/conversation-runtime/cache/repos/__agent_runtime_cache__/00-x"
    command = _git_clone._guarded_clone_cmd(
        target,
        _git_clone._clone_then_checkout(f"git clone URL {staging}", staging, "deadbeef"),
        clean=staging,
    )

    assert f"git -C {staging} checkout --detach deadbeef" in command, command
    assert f"git -C {target} checkout" not in command, (
        "the pin must never run against the shared cache"
    )
    # Inside the lock, which is what makes it invisible to a concurrent reader.
    assert command.index("flock") < command.index("checkout --detach"), command
    assert _git_clone._clone_lock_path(target) in command, command


def test_no_revision_pin_leaves_the_clone_command_alone() -> None:
    """The control: repos without a pinned sha must not grow a checkout."""

    command = _git_clone._clone_then_checkout("git clone URL /tmp/x", "/tmp/x", None)

    assert command == "git clone URL /tmp/x"
    assert "checkout" not in command


def test_the_documented_medium_requirements_match_the_code_that_needs_them() -> None:
    """A medium is ruled out by what the workspace path actually does.

    The list in `docs/deploy.md` is what a deployment reads before choosing a
    storage class, and it is only worth reading while it matches. Each entry is
    checked against the call that creates the requirement, so removing one from
    the code without removing it from the table — or the reverse — is caught
    here rather than by a deployment that mounts a bucket and finds out.
    """

    root = Path(__file__).resolve().parents[1]
    deploy_doc = (root / "docs/deploy.md").read_text(encoding="utf-8")
    clone = (root / "astrabox/core/service/orchestrator/runtime/storage/_git_clone.py").read_text(
        encoding="utf-8"
    )
    mounted = (root / "astrabox/providers/storage/mounted_volume.py").read_text(
        encoding="utf-8"
    )

    for requirement, source, evidence in (
        ("rename", clone, "os.replace(tmp,dst)"),
        ("chmod", clone, "shutil.copy(src,tmp)"),
        ("flock", clone, "flock 9"),
        ("symbolic links", mounted, "readlink -f"),
        ("ReadWriteMany", deploy_doc, "ReadWriteMany"),
    ):
        assert evidence in source, (
            f"the workspace path no longer does {evidence!r}; if that requirement "
            f"is gone, take {requirement!r} out of docs/deploy.md too"
        )
        assert requirement in deploy_doc, (
            f"{requirement!r} is required by the code but absent from the medium "
            "table a deployment reads"
        )

    assert "Use only after proving every operation above" in deploy_doc, (
        "the table must require filesystem semantics, not just a FUSE mount"
    )
    assert "Mountpoint for Amazon S3 does not qualify" in deploy_doc, (
        "the table must rule out Mountpoint's incompatible filesystem semantics"
    )
