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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import astrabox.core.service.orchestrator.engine.claude_code  # noqa: F401 — registers the runtime profile used below
from astrabox.seams.sandbox_data import SandboxCommandResult
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    _CONVERSATION_BOOTSTRAP_SCRIPT,
    _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS,
    _build_conversation_bootstrap_env,
    _command_output,
    _ready_identity_from_bootstrap_output,
    normalize_runtime_identity,
    plan_conversation_identity,
)
from astrabox.providers import register_builtin_providers
from astrabox.core.service.orchestrator.engine.runner_link import RUNNER_TOKEN_FILE_NAME
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    composed_runtime_profile,
)
from astrabox.core.service.orchestrator.workspace import workspace_from_subject_kind


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
_HERMES_PROFILE_SETUP = _RUNTIME_ASSET_DIR / "hermes-profile-setup"


def _agent_template() -> SimpleNamespace:
    return SimpleNamespace(
        engine_kind="claude_code",
        skills=[],
        mcp_servers={},
        plugins=[],
        model_config={},
    )


def _planned_identity(session_id: str = "sess-abc") -> dict[str, object]:
    workspace = workspace_from_subject_kind(
        "deployment_conversation",
        user_id="u-1",
        agent_id="ag-1",
        engine_kind="claude_code",
    )
    return workspace.plan_runtime_identity(  # type: ignore[attr-defined,no-any-return]
        template=_agent_template(), session_id=session_id, user_id="u-1"
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


def test_bootstrap_stderr_cannot_corrupt_the_ready_line_telemetry() -> None:
    """A command's stdout and stderr arrive apart, and stay apart in its output."""

    result = SandboxCommandResult(
        exit_code=0,
        stdout=(
            "CONVERSATION_BOOTSTRAP_READY user=agent uid=1000 gid=1000 "
            "home=/home/agent workspace=/home/agent/workspace "
            "timings_b64=eyJ0b3RhbCI6NzB9"
        ),
        stderr="useradd: warning: home already exists",
    )

    ready = _ready_identity_from_bootstrap_output(
        _planned_identity(),
        _command_output(result),
        transport="sandbox_command_script",
    )

    evidence = ready["stage_evidence"]
    assert evidence["bootstrap_timings_ms"] == {"total": 70}
    assert "bootstrap_timings_error" not in evidence


def test_hermes_profile_setup_maps_the_visible_workspace_to_its_backing(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "chown").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "runuser").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "[ \"$1\" = -u ]\n"
        "shift 2\n"
        "[ \"$1\" = -- ]\n"
        "shift\n"
        "exec \"$@\"\n",
        encoding="utf-8",
    )
    # The managed scope is root-owned in the box; here it only has to exist.
    (fake_bin / "install").write_text(
        "#!/bin/sh\nfor last; do :; done\nmkdir -p \"$last\"\n", encoding="utf-8"
    )
    for tool in ("chown", "runuser", "install"):
        (fake_bin / tool).chmod(0o755)

    profile_home = tmp_path / "profiles" / "assistant-1"
    workspace_source = profile_home / "workspace"
    visible_workspace = tmp_path / "workspace"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ASTRABOX_HERMES_PROFILE_LINUX_USER": "assistant_user",
        "ASTRABOX_HERMES_PROFILE_HOME": str(profile_home),
        "ASTRABOX_HERMES_WORKSPACE": str(visible_workspace),
        "HERMES_HOME": str(profile_home / ".hermes"),
        "HERMES_MANAGED_DIR": str(tmp_path / "managed"),
    }

    for _ in range(2):
        completed = subprocess.run(
            ["bash", str(_HERMES_PROFILE_SETUP), str(workspace_source)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

    assert visible_workspace.is_symlink()
    assert visible_workspace.readlink() == workspace_source
    (visible_workspace / "proof.txt").write_text("mapped", encoding="utf-8")
    assert (workspace_source / "proof.txt").read_text(encoding="utf-8") == "mapped"
    assert "HERMES_PROFILE_READY" in completed.stdout


def test_hermes_profile_setup_refuses_to_replace_an_existing_visible_root(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profiles" / "assistant-1"
    workspace_source = profile_home / "workspace"
    visible_workspace = tmp_path / "workspace"
    visible_workspace.mkdir()

    completed = subprocess.run(
        ["bash", str(_HERMES_PROFILE_SETUP), str(workspace_source)],
        env={
            "PATH": "/usr/bin:/bin",
            "ASTRABOX_HERMES_PROFILE_LINUX_USER": "assistant_user",
            "ASTRABOX_HERMES_PROFILE_HOME": str(profile_home),
            "ASTRABOX_HERMES_WORKSPACE": str(visible_workspace),
            "HERMES_HOME": str(profile_home / ".hermes"),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 66
    assert "HERMES_PROFILE_WORKSPACE_ALIAS_OCCUPIED" in completed.stderr


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


def test_a_dedicated_sandbox_refuses_a_second_physical_workspace_path() -> None:
    profile = replace(
        composed_runtime_profile("claude_code", "conversation"),
        workspace_source_template="{home}/workspace",
    )

    with pytest.raises(APIError, match="per-Session sandbox"):
        plan_conversation_identity(
            session_id="session-one",
            sandbox_id="box-one",
            agent_id="agent-one",
            runtime_profile=profile,
        )


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


def test_bootstrap_waits_for_the_image_account_between_groupadd_and_useradd(
    tmp_path: Path,
) -> None:
    """A runner-ready race must not manufacture a second workload account."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "passwd-queries"
    useradd_marker = tmp_path / "useradd-called"
    (fake_bin / "getent").write_text(
        """#!/bin/sh
set -eu
kind="$1"
name="$2"
if [ "$kind" = group ] && [ "$name" = agent ]; then
  echo 'agent:x:1000:'
  exit 0
fi
if [ "$kind" = passwd ] && [ "$name" = agent ]; then
  count=0
  [ ! -f "$COUNTER" ] || count="$(cat "$COUNTER")"
  count=$((count + 1))
  printf '%s' "$count" >"$COUNTER"
  if [ "$count" -ge 3 ]; then
    echo 'agent:x:1000:1000::/home/agent:/bin/bash'
    exit 0
  fi
fi
exit 2
""",
        encoding="utf-8",
    )
    (fake_bin / "useradd").write_text(
        """#!/bin/sh
printf called >"$USERADD_MARKER"
exit 99
""",
        encoding="utf-8",
    )
    (fake_bin / "getent").chmod(0o755)
    (fake_bin / "useradd").chmod(0o755)
    harness = (
        "set -euo pipefail\n"
        + _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS
        + "\nuser=agent\nhome=/home/agent\nrequested_uid=\nrequested_gid=\n"
        + "ensure_workload_account\n"
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "COUNTER": str(counter),
            "USERADD_MARKER": str(useradd_marker),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert not useradd_marker.exists()


def test_planning_an_identity_needs_no_storage_configuration() -> None:
    """It is knowable before any box exists, which is what lets a pooled box —
    created with no session on it — satisfy it."""
    identity = _planned_identity()
    assert identity["sandbox_id"] is None


def test_no_numeric_owner_is_allocated_for_a_conversation() -> None:
    """Sessions of one agent get the same account, and are kept apart by having
    their own boxes. A uid told them apart only while they shared a tree — and
    allocating one per session was what a pre-created box could never satisfy."""
    first = _planned_identity("sess-one")
    second = _planned_identity("sess-two")
    assert first["linux_user"] == second["linux_user"]
    assert "uid" not in first and "gid" not in first


# ── the bootstrap owns what it creates, and not what it finds ──


def test_platform_installed_content_stays_out_of_the_workload_s_hands() -> None:
    """The plugin set and deploy key are part of what
    constrains the agent. Root-owned, read and execute only — an account that can
    rewrite its own constraints has none. They are also what will move into a
    read-only image layer, which a workload-owned directory would rule out."""
    body = _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS.split(
        "establish_conversation_identity_root() {", 1
    )[1]
    platform = set(re.findall(r'create_platform_dir "([^"]+)"', body))
    assert platform == {
        "$config/plugins",
        "$home/.ssh",
        "$home/.local/bin",
    }
    helper = _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS.split("create_platform_dir() {", 1)[1]
    helper = helper.split("\n}", 1)[0]
    assert "chown" not in helper
    # 0755, not 0700: the workload has to traverse and read them.
    assert "chmod 755" in helper

    # And the bridge symlinks the platform drops into them are not handed over.
    assert "chown -h" not in _CONVERSATION_BOOTSTRAP_SCRIPT


def test_configless_engine_bootstrap_does_not_invent_a_vendor_directory() -> None:
    harness = (
        "set -euo pipefail\n"
        + _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS
        + r'''
ensure_workload_account() { :; }
getent() {
  test "$1" = passwd
  printf 'agent:x:1000:1000::/home/agent:/bin/bash\n'
}
mkdir() { :; }
create_workload_dir() { printf 'workload:%s\n' "$1"; }
create_platform_dir() { printf 'platform:%s\n' "$1"; }
user=agent
home=/home/agent
workspace=/workspace
config=
cache=/home/agent/.cache
tmpdir=/home/agent/tmp
establish_conversation_identity_root
'''
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "workload:/workspace",
        "workload:/home/agent/.cache",
        "workload:/home/agent/tmp",
        "platform:/home/agent/.ssh",
        "platform:/home/agent/.local/bin",
    ]
    assert 'config="${CONV_CONFIG:-}"' in _CONVERSATION_BOOTSTRAP_SCRIPT


def test_a_shared_identity_delivers_the_allocated_uid_and_gid_to_bootstrap() -> None:
    identity = {
        **_planned_identity(),
        "uid": 20001,
        "gid": 20001,
    }
    env = _build_conversation_bootstrap_env(identity)
    assert env["CONV_UID"] == "20001"
    assert env["CONV_GID"] == "20001"
    assert '--uid "$requested_uid" --gid "$requested_gid"' in (
        _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS
    )


def test_assistant_replacements_keep_the_same_numeric_profile_owner() -> None:
    from astrabox.core.service.orchestrator.runtime.conversation_identity import (
        CONVERSATION_UID_BASE,
        plan_assistant_profile_identity,
    )

    register_builtin_providers()
    first, replacement = (
        plan_assistant_profile_identity(
            engine_kind="assistant",
            user_id="owner-1",
            assistant_id="assistant-1",
            sandbox_id=box,
        )
        for box in ("first-box", "replacement-box")
    )
    assert first["sandbox_id"] != replacement["sandbox_id"]
    assert first["uid"] == replacement["uid"] == CONVERSATION_UID_BASE
    assert first["gid"] == replacement["gid"] == CONVERSATION_UID_BASE
    assert first["home_dir"] == replacement["home_dir"]
    assert _build_conversation_bootstrap_env(replacement)["CONV_UID"] == str(
        CONVERSATION_UID_BASE
    )


async def test_assistant_workspace_requests_ownership_before_bootstrap_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astrabox.core.service.orchestrator.runtime.conversation_identity as identities

    register_builtin_providers()
    seen: list[tuple[dict[str, object], bool]] = []

    async def provision(
        sandbox: object, identity: dict[str, object], *, assistant_profile: bool = False
    ) -> dict[str, object]:
        seen.append((identity, assistant_profile))
        return identity

    monkeypatch.setattr(
        identities, "provision_conversation_identity_with_bootstrap_script", provision
    )
    workspace = workspace_from_subject_kind(
        "assistant_runtime", user_id="owner-1", assistant_id="assistant-1",
        engine_kind="assistant",
    )
    await workspace.mount_and_provision(
        SimpleNamespace(), SimpleNamespace(sandbox_id="new-box"),
        template=SimpleNamespace(), session_id="bootstrap-1", user_id="owner-1",
    )
    assert len(seen) == 1
    identity, assistant_profile = seen[0]
    assert assistant_profile is True
    assert identity["uid"] == identity["gid"] == identities.CONVERSATION_UID_BASE


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
