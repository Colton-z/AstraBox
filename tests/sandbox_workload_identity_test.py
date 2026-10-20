"""The account the agent CLI runs as is a property of the IMAGE, not of a session.

A sandbox can exist before any session claims it — that is what a prewarm pool
is — so an account that only a session could create is missing on exactly the
boxes a pool lends out, and the spawn, which names the user, dies there with
`getpwnam(): name not found`.

The image does not *create* that account, it *names* it. The base ships no
account at all: its entrypoint makes one from `USER`/`USER_UID`/`USER_GID` on
every container start and runs its browser-facing services as that account.
Creating a second account at build time makes the entrypoint's
`groupadd --gid 1000` collide with the build's own group, and `set -e` takes
the whole container down (`Exited (4)`) before any service starts. Naming the
base's account instead yields one workload identity per box; OpenSandbox
Filesystem and execd then expose the same workspace.

The second half is ownership. One workload identity is not one identity: the
agent runs code the model chose, so the plugin set and deploy key the PLATFORM
installs stay root-owned and read-only to it. An account that can rewrite its
own constraints has none.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 — registers the runtime profile used below
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    _CONVERSATION_BOOTSTRAP_SCRIPT,
    _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS,
    _build_conversation_bootstrap_env,
    normalize_runtime_identity,
    plan_conversation_identity,
)
from astrabox.core.service.orchestrator.engine.runner_link import RUNNER_TOKEN_FILE_NAME
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    composed_runtime_profile,
)


_DOCKERFILE = (
    Path(__file__).resolve().parents[1] / "containers" / "sandbox-claude-code" / "Dockerfile"
)
_RUNNER_LAUNCHER = _DOCKERFILE.with_name("start-runner.sh")
_EXECD_ISOLATION_CONFIG = _DOCKERFILE.with_name("execd-isolation.toml")
_RUNTIME_ASSET_DIR = (
    Path(__file__).resolve().parents[1]
    / "astrabox"
    / "core"
    / "service"
    / "orchestrator"
    / "runtime"
)


def _dockerfile_env(name: str) -> str:
    """The value the image declares for an ENV key, from the ENV block."""
    match = re.search(rf"^\s*{name}=(\S+?)\s*\\?$", _DOCKERFILE.read_text(), re.MULTILINE)
    assert match, f"the agent image no longer declares ENV {name}"
    return match.group(1)


def test_the_image_configures_execd_to_host_conversation_homes() -> None:
    """Shared conversations ask execd to make their private HOME writable.

    Execd rejects every ``extra_writable`` path outside ``allowed_writable``.
    The image therefore has to carry the allowlist before it can be prewarmed;
    setting it while a conversation starts would make cold and pooled boxes
    different again.
    """
    config = tomllib.loads(_EXECD_ISOLATION_CONFIG.read_text())
    assert config["allowed_writable"] == [
        "/workspace",
        "/mnt",
        "/media",
        "/data",
        "/home/conversations",
    ]
    assert "/home" not in config["allowed_writable"], (
        "allow only AstraBox's conversation root, not every user's home"
    )
    dockerfile = _DOCKERFILE.read_text()
    assert (
        "COPY --chmod=u=rwX,go=rX containers/sandbox-claude-code/execd-isolation.toml "
        "/opt/astrabox/opensandbox/isolation.toml"
    ) in dockerfile
    assert _dockerfile_env("EXECD_ISOLATION_CONFIG") == (
        "/opt/astrabox/opensandbox/isolation.toml"
    )


# ── the image and the host name the same account and the same workspace ──


def test_shared_bootstrap_uses_private_backing_for_the_visible_workspace() -> None:
    identity = plan_conversation_identity(
        session_id="session-one",
        sandbox_id="box-one",
        agent_id="agent-one",
        runtime_profile=composed_runtime_profile("claude_code", "agent"),
    )

    env = _build_conversation_bootstrap_env(
        identity,
        default_repo={
            "url": "git@example.test:org/repo.git",
            "target": "/workspace",
            "key_b64": "encoded",
        },
    )

    source = str(identity["workspace_source_dir"])
    assert identity["workspace_dir"] == "/workspace"
    assert env["CONV_WORKSPACE"] == source
    assert env["CONV_DEFAULT_REPO_TARGET"] == source


def test_a_shared_visible_root_requires_its_private_backing_path() -> None:
    identity = {
        "linux_user": "conv_one",
        "home_dir": "/home/conversations/conv_one",
        "workspace_dir": "/workspace",
        "sandbox_tenancy": "agent",
    }

    assert normalize_runtime_identity(identity) is None


def test_runner_waits_for_the_image_account_then_drops_its_whole_process(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "passwd-queries"
    runuser_args = tmp_path / "runuser-args"
    server = tmp_path / "sandbox_runner.py"
    workspace = tmp_path / "workspace"
    server.write_text("# probe\n", encoding="utf-8")
    workspace.mkdir()

    (fake_bin / "getent").write_text(
        """#!/bin/sh
set -eu
count=0
[ ! -f "$COUNTER" ] || count="$(cat "$COUNTER")"
count=$((count + 1))
printf '%s' "$count" >"$COUNTER"
if [ "$count" -ge 3 ]; then
  printf 'agent:x:1000:1000::/home/agent:/bin/bash\n'
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "runuser").write_text(
        """#!/bin/sh
printf '%s\n' "$@" >"$RUNUSER_ARGS"
""",
        encoding="utf-8",
    )
    for name in ("getent", "sleep", "runuser"):
        (fake_bin / name).chmod(0o755)

    completed = subprocess.run(
        ["sh", str(_RUNNER_LAUNCHER)],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "COUNTER": str(counter),
            "RUNUSER_ARGS": str(runuser_args),
            "ASTRABOX_INBOX_SERVER": str(server),
            "ASTRABOX_RUNNER_PORT": "8123",
            "ASTRABOX_WORKLOAD_USER": "agent",
            "WORKSPACE": str(workspace),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert counter.read_text() == "3"
    assert runuser_args.read_text().splitlines() == [
        "-u",
        "agent",
        "--",
        "env",
        "HOME=/home/agent",
        "USER=agent",
        "LOGNAME=agent",
        f"PWD={workspace}",
        "ASTRABOX_RUNNER_PORT=8123",
        # The file the platform delivers the runner's credential into; the
        # runner refuses every prepare until it exists.
        f"ASTRABOX_RUNNER_TOKEN_FILE=/home/agent/{RUNNER_TOKEN_FILE_NAME}",
        "/usr/local/bin/python3.12",
        str(server),
    ]


# ── the bootstrap owns what it creates, and not what it finds ──


@pytest.mark.parametrize(
    ("existing_owner", "expected_code", "expected_changes"),
    [("0:0", 0, 3), ("2000:2000", 0, 0), ("2001:2001", 42, 0)],
)
def test_assistant_ownership_initializes_only_private_roots_and_refuses_a_foreign_owner(
    existing_owner: str, expected_code: int, expected_changes: int,
) -> None:
    # Run the shipped operation with a synthetic stat/chown boundary. A real
    # mounted-volume replay separately proves the resulting workload can write.
    source = (_RUNTIME_ASSET_DIR / "provision-assistant-profile").read_text()
    body = source.split("prepare_assistant_profile_roots() {", 1)[1]
    body = body.split("\n}\n", 1)[0]
    harness = "set -eu\nprepare_assistant_profile_roots() {" + body + "\n}\n" + r'''
user=asst_owned
home=/home/conversations/asst_owned
workspace=$home/workspace
config=$home/.hermes
requested_uid=2000
requested_gid=2000
mkdir() { :; }
chmod() { :; }
stat() {
  case "$2" in %u) printf '%s\n' "${EXISTING_OWNER%:*}" ;;
    %g) printf '%s\n' "${EXISTING_OWNER#*:}" ;; *) exit 91 ;; esac
}
chown() { printf 'OWN %s %s\n' "$1" "$2"; test "$#" = 2; }
prepare_assistant_profile_roots
'''
    result = subprocess.run(
        ["bash", "-c", harness],
        env={"PATH": "/usr/bin:/bin", "EXISTING_OWNER": existing_owner},
        text=True, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode == expected_code, result.stderr
    changes = result.stdout.splitlines()
    assert len(changes) == expected_changes
    if expected_changes:
        assert changes == [
            f"OWN 2000:2000 /home/conversations/asst_owned{suffix}"
            for suffix in ("", "/workspace", "/.hermes")
        ]
    if expected_code:
        assert "ASSISTANT_PROFILE_OWNER_MISMATCH" in result.stderr
