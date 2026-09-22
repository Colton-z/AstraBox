"""The account the agent CLI runs as is a property of the IMAGE, not of a session.

A sandbox can exist before any session claims it — that is what a prewarm pool
is — so an account that only a session could create is missing on exactly the
boxes a pool lends out, and the spawn, which names the user, dies there with
`getpwnam(): name not found`.

The image does not *create* that account, it *names* it. The base ships no
account at all: its entrypoint makes one from `USER`/`USER_UID`/`USER_GID` on
every container start and runs its browser-facing services as that account.
Creating a second account at build time — which is what these tests exist to
prevent — makes the entrypoint's `groupadd --gid 1000` collide with the build's
own group, and `set -e` takes the whole container down (`Exited (4)`) before any
service starts. Naming the base's account instead yields one workload identity
per box; OpenSandbox Filesystem and execd then expose the same workspace.

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
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT,
    _CONVERSATION_BOOTSTRAP_SCRIPT,
    _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS,
    _build_conversation_bootstrap_env,
    _command_output,
    _ready_identity_from_bootstrap_output,
    normalize_runtime_identity,
    plan_conversation_identity,
    provision_conversation_identity_with_bootstrap_script,
)
from astrabox.core.service.orchestrator.engine.registry import (
    registered_engine_adapters,
)
from astrabox.providers import register_builtin_providers
from astrabox.core.service.orchestrator.engine.runtime_profiles import (
    SANDBOX_IMAGE_WORKLOAD_HOME,
    SANDBOX_IMAGE_WORKLOAD_USER,
    SANDBOX_IMAGE_WORKSPACE_DIR,
    composed_runtime_profile,
)
from astrabox.core.service.orchestrator.runtime.runtime_profile import (
    resolve_runtime_profile,
)
from astrabox.core.service.orchestrator.workspace import workspace_from_subject_kind


_DOCKERFILE = (
    Path(__file__).resolve().parents[1] / "containers" / "sandbox-claude-code" / "Dockerfile"
)
_RUNNER_LAUNCHER = _DOCKERFILE.with_name("start-runner.sh")
_HERMES_DOCKERFILE = _DOCKERFILE.parents[1] / "sandbox-hermes" / "Dockerfile"
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
_OPENSANDBOX_DOC = Path(__file__).resolve().parents[1] / "docs/providers/opensandbox.md"


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
        "COPY containers/sandbox-claude-code/execd-isolation.toml "
        "/opt/astrabox/opensandbox/isolation.toml"
    ) in dockerfile
    assert _dockerfile_env("EXECD_ISOLATION_CONFIG") == (
        "/opt/astrabox/opensandbox/isolation.toml"
    )


def test_agent_images_pin_the_official_execd_release() -> None:
    """The image build keeps its inspected supplier daemon artifact pinned."""
    dockerfile = _DOCKERFILE.read_text()
    assert "ARG SANDBOX_EXECD_IMAGE=opensandbox/execd:v1.1.0" in dockerfile
    assert "FROM ${SANDBOX_EXECD_IMAGE} AS execd" in dockerfile
    assert "COPY --from=execd /execd /execd" in dockerfile
    assert "COPY --from=execd /bootstrap.sh /bootstrap.sh" in dockerfile
    assert "ln /execd /opt/astrabox/opensandbox/execd" in dockerfile
    assert "ln /bootstrap.sh /opt/astrabox/opensandbox/bootstrap.sh" in dockerfile
    assert "execd-builder" not in dockerfile
    assert "isolated-cancellation.patch" not in dockerfile


def test_every_agent_image_bakes_the_conversation_bootstrap() -> None:
    """Claiming a prewarmed box may trigger a capability, never install it."""
    expected_copy = (
        "COPY astrabox/core/service/orchestrator/runtime/provision-conversation "
        f"{AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT}"
    )
    assert AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT.startswith("/usr/local/bin/")
    for dockerfile in (_DOCKERFILE, _HERMES_DOCKERFILE):
        body = dockerfile.read_text()
        assert expected_copy in body, dockerfile
        assert f"chmod 0555 {AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT}" in body


async def test_claiming_a_box_only_triggers_the_baked_conversation_bootstrap() -> None:
    """The command transport must not write a fixed platform script at claim time."""
    calls: list[tuple[str, dict[str, str] | None, int | None]] = []

    class Commands:
        async def run(
            self,
            command: str,
            *,
            envs: dict[str, str] | None = None,
            timeout_in_millis: int | None = None,
        ) -> SimpleNamespace:
            calls.append((command, envs, timeout_in_millis))
            logs = SimpleNamespace(
                stdout=[
                    SimpleNamespace(
                        text=(
                            "CONVERSATION_BOOTSTRAP_READY user=agent uid=1000 gid=1000 "
                            "home=/home/agent workspace=/workspace "
                            "timings_b64=\n"
                        )
                    )
                ],
                stderr=[],
            )
            return SimpleNamespace(error=None, logs=logs)

    await provision_conversation_identity_with_bootstrap_script(
        SimpleNamespace(commands=Commands()),
        _planned_identity(),
    )

    assert calls == [
        (
            f"bash {AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT} 2>&1",
            _build_conversation_bootstrap_env(_planned_identity()),
            300_000,
        )
    ]


def test_bootstrap_stderr_cannot_corrupt_the_ready_line_telemetry() -> None:
    """OpenSandbox returns stdout/stderr as separate message collections."""

    result = SimpleNamespace(
        error=None,
        logs=SimpleNamespace(
            stdout=[
                SimpleNamespace(
                    text=(
                        "CONVERSATION_BOOTSTRAP_READY user=agent uid=1000 gid=1000 "
                        "home=/home/agent workspace=/home/agent/workspace "
                        "timings_b64=eyJ0b3RhbCI6NzB9"
                    )
                )
            ],
            stderr=[SimpleNamespace(text="useradd: warning: home already exists")],
        ),
    )

    ready = _ready_identity_from_bootstrap_output(
        _planned_identity(),
        _command_output(result),
        transport="sandbox_command_script",
    )

    evidence = ready["stage_evidence"]
    assert evidence["bootstrap_timings_ms"] == {"total": 70}
    assert "bootstrap_timings_error" not in evidence


def test_hermes_bakes_fixed_profile_tools_and_claim_only_writes_profile_data() -> None:
    dockerfile = _HERMES_DOCKERFILE.read_text()
    copies = {
        "hermes-profile-setup": "/usr/local/bin/astrabox-hermes-profile-setup",
        "hermes-skill-repo-cache": "/usr/local/bin/astrabox-hermes-skill-repo-cache",
    }
    for asset, target in copies.items():
        assert (
            "COPY astrabox/core/service/orchestrator/runtime/"
            f"{asset} {target}"
        ) in dockerfile
        assert f"chmod 0555 {target}" in dockerfile

    hermes_source = (
        Path(__file__).resolve().parents[1]
        / "astrabox/core/service/orchestrator/engine/hermes.py"
    ).read_text()
    for installer in (
        "_install_hermes_config_merge_script",
        "_install_hermes_profile_setup_script",
        "_install_hermes_skill_repo_cache_script",
    ):
        assert installer not in hermes_source
    assert "_install_hermes_profile_env_file" in hermes_source


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
    (fake_bin / "chown").chmod(0o755)
    (fake_bin / "runuser").chmod(0o755)

    profile_home = tmp_path / "profiles" / "assistant-1"
    workspace_source = profile_home / "workspace"
    visible_workspace = tmp_path / "workspace"
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "ASTRABOX_HERMES_PROFILE_LINUX_USER": "assistant_user",
        "ASTRABOX_HERMES_PROFILE_HOME": str(profile_home),
        "ASTRABOX_HERMES_WORKSPACE": str(visible_workspace),
        "HERMES_HOME": str(profile_home / ".hermes"),
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


def test_hermes_profile_declares_the_alias_capabilities_baked_into_its_image() -> None:
    required = set(
        composed_runtime_profile(
            "assistant", "agent", session_kind="assistant_chat"
        ).required_commands
    )

    assert "/usr/local/bin/astrabox-provision-conversation" in required
    assert "/usr/local/bin/astrabox-hermes-profile-setup" in required
    assert {"ln", "readlink"} <= required


def test_every_agent_image_bakes_the_shared_workspace_storage_helper() -> None:
    """A shared box must own its mount capability before any workspace claims it."""
    helper = "/usr/local/bin/astrabox-assistant-workspace-storage"
    expected_copy = (
        "COPY astrabox/core/service/orchestrator/runtime/"
        f"astrabox-assistant-workspace-storage {helper}"
    )
    for dockerfile in (_DOCKERFILE, _HERMES_DOCKERFILE):
        body = dockerfile.read_text()
        assert expected_copy in body, dockerfile
        assert f"chmod 0555 {helper}" in body

    storage_source = (
        _RUNTIME_ASSET_DIR / "storage" / "_nas_mount.py"
    ).read_text()
    assert "_install_assistant_workspace_storage_script" not in storage_source


def test_opensandbox_docs_make_the_remaining_aio_dependency_explicit() -> None:
    """Native provider faces do not make the bundled image base interchangeable."""
    body = _OPENSANDBOX_DOC.read_text()
    prose = " ".join(body.split())
    assert "OpenSandbox-native command, filesystem, and endpoint APIs" in prose
    assert "The bundled images require AIO because" in prose
    assert "`/opt/gem/run.sh`" in prose


# ── the image and the host name the same account and the same workspace ──


def test_the_planned_identity_names_the_account_the_image_declares() -> None:
    identity = _planned_identity()
    assert identity["linux_user"] == SANDBOX_IMAGE_WORKLOAD_USER
    assert identity["home_dir"] == SANDBOX_IMAGE_WORKLOAD_HOME
    assert identity["workspace_dir"] == SANDBOX_IMAGE_WORKSPACE_DIR
    assert identity["workspace_source_dir"] == SANDBOX_IMAGE_WORKSPACE_DIR


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


def test_the_image_declares_exactly_that_account_and_workspace() -> None:
    """The two halves of one contract, in artifacts that cannot import each other.

    The host renders these into every spawn; the image's entrypoint is what makes
    them resolve. If they drift, nothing fails until a real box runs a real turn:
    a wrong name is `getpwnam(): name not found`, a wrong workspace is a file
    panel listing a directory the agent never writes to.
    """
    identity = _planned_identity()
    assert _dockerfile_env("USER") == identity["linux_user"]
    assert _dockerfile_env("ASTRABOX_WORKLOAD_USER") == identity["linux_user"]
    assert _dockerfile_env("WORKSPACE") == identity["workspace_dir"]
    # A fixed uid, because a mount over the workspace has to be made writable by
    # a number the deployment can know in advance.
    assert _dockerfile_env("USER_UID").isdigit()
    assert _dockerfile_env("USER_GID") == _dockerfile_env("USER_UID")


#: Which image implements each engine whose profile names the IMAGE's account.
#:
#: The test below derives the engines from the registry and requires this map to
#: cover exactly them, so an engine added with an image-account profile and no
#: entry here fails rather than going unchecked.
_ENGINE_IMAGE_DIR = {
    "claude_code": "sandbox-claude-code",
    "codex": "sandbox-codex",
    "deepseek_harness": "sandbox-deepseek-harness",
    "pi": "sandbox-pi",
}


def _engines_using_the_image_account() -> set[str]:
    """Engines whose conversation-tenancy identity is the image account.

    The platform composes identity now, and on the conversation tenancy it
    names `SANDBOX_IMAGE_WORKLOAD_USER` for every agent_chat engine — a claim
    ABOUT AN IMAGE, which is what this test checks image by image. Shared
    tenancies render per-conversation accounts no image can bake, so they are
    outside the claim; hermes serves only assistant_chat and composes the
    image account on no tenancy it can reach here.
    """
    # Adapters register when their module is imported. Importing the four by
    # name here would be the same hand-kept list this test exists to replace,
    # so go through the platform's own in-tree installer instead.
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    register_builtin_providers()
    using: set[str] = set()
    for kind, adapter in registered_engine_adapters().items():
        if "agent_chat" not in adapter.capabilities.supported_session_kinds:
            continue
        profile = composed_runtime_profile(kind, "conversation")
        if profile.username_template == SANDBOX_IMAGE_WORKLOAD_USER:
            using.add(kind)
    return using


def test_every_image_account_engine_has_an_image_that_declares_it() -> None:
    """The same contract as above, for every engine that makes the same claim.

    An engine whose profile names `SANDBOX_IMAGE_WORKLOAD_USER` is asserting
    something about its image. The base's entrypoint builds its account from
    `$USER`/`$USER_UID` on every container start, so an image that does not name
    one gets the base's own default and the host's claim is false.

    Nothing else catches that. The build passes, the adapter registers, the
    conformance suite passes, and turns run — the CLI runs perfectly well as
    whoever the box created. Only an operation that uses the NAME rather than
    the process fails: the platform hands the planned account to execd as the
    owner of an uploaded file, execd cannot chown to an account that is not
    there, and `file upload` answers 500 three layers from the cause.

    The engine list comes from the registry, so this holds for the next image
    and not only for the ones present when it was written.
    """
    engines = _engines_using_the_image_account()
    assert engines, "no engine declares the image's workload account any more"
    assert engines == set(_ENGINE_IMAGE_DIR), (
        "every engine whose profile names the image account needs its image "
        f"listed here: registry={sorted(engines)} listed={sorted(_ENGINE_IMAGE_DIR)}"
    )
    for kind in sorted(engines):
        dockerfile = _DOCKERFILE.parents[1] / _ENGINE_IMAGE_DIR[kind] / "Dockerfile"
        text = dockerfile.read_text()
        for key, expected in (
            ("USER", SANDBOX_IMAGE_WORKLOAD_USER),
            ("ASTRABOX_WORKLOAD_USER", SANDBOX_IMAGE_WORKLOAD_USER),
            ("WORKSPACE", SANDBOX_IMAGE_WORKSPACE_DIR),
        ):
            # The key may open the ENV block or continue it; both are the same
            # declaration, and which one it is should not be a contract.
            match = re.search(rf"^\s*(?:ENV\s+)?{key}=(\S+?)\s*\\?$", text, re.MULTILINE)
            assert match and match.group(1) == expected, (
                f"{_ENGINE_IMAGE_DIR[kind]} does not declare ENV {key}={expected}; "
                f"its boxes run as the base image's own account instead, and the "
                f"{kind!r} profile plans {SANDBOX_IMAGE_WORKLOAD_USER!r}"
            )
        # Ownership has to be in the image layer: the runner can answer before
        # the entrypoint's chown would have run.
        assert "install -d -o 1000 -g 1000 -m 0755 /workspace" in text, (
            f"{_ENGINE_IMAGE_DIR[kind]} does not pre-own the workspace"
        )


def test_image_boot_and_recovery_share_one_workload_runner_launcher() -> None:
    dockerfile = _DOCKERFILE.read_text()
    boot = _DOCKERFILE.with_name("boot.sh").read_text()

    assert (
        "COPY containers/sandbox-claude-code/start-runner.sh "
        "/opt/astrabox/start-runner.sh"
    ) in dockerfile
    assert '"$ASTRABOX_RUNNER_LAUNCHER" >>"$ASTRABOX_INBOX_LOG" 2>&1 &' in boot
    assert 'python3 "$ASTRABOX_INBOX_SERVER"' not in boot


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
        "/usr/local/bin/python3.12",
        str(server),
    ]


def test_the_image_preowns_the_workspace_for_the_runtime_created_account() -> None:
    """A turn can arrive as soon as the image-owned runner reports ready.

    The base creates the named account during container startup.  The workspace
    therefore has to carry that account's numeric ownership in the image layer;
    waiting for the base entrypoint to chown it races the resident runner, while
    fixing it when a Session claims the box breaks the prewarm contract.
    """
    dockerfile = _DOCKERFILE.read_text()
    preowned_workspace = "install -d -o 1000 -g 1000 -m 0755 /workspace"
    assert preowned_workspace in dockerfile
    assert dockerfile.index(preowned_workspace) < dockerfile.index(
        "WORKDIR /workspace"
    )


def test_the_image_does_not_build_an_account_beside_the_base_s() -> None:
    """Building an account at the base's uid takes the whole container down, not just the agent.

    The base creates `$USER` with `$USER_UID`/`$USER_GID` at container start. A
    build-time account at that uid makes its `groupadd --gid 1000` fail, `set -e`
    kills the entrypoint, and the box exits 4 with no :8080 and no :8000 — so
    every in-box service, not only the CLI, is gone.
    """
    dockerfile = _DOCKERFILE.read_text()
    # Lines that only ASSERT the base still creates the account are not the image
    # creating one; anything else naming these commands is.
    creating = [
        line
        for line in dockerfile.splitlines()
        if ("useradd" in line or "groupadd" in line)
        and not line.lstrip().startswith(("#", "grep -q"))
    ]
    assert creating == []
    # And the build refuses a base that would collide on its own.
    assert "! getent passwd 1000" in dockerfile
    assert "! getent group 1000" in dockerfile


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


def test_the_image_asserts_the_base_still_honours_the_knobs() -> None:
    """Setting ENV only works while the base reads it. A base that stopped
    templating its services off USER/WORKSPACE would leave them running as some
    other account against some other directory, silently."""
    dockerfile = _DOCKERFILE.read_text()
    assert "ENV_USER" in dockerfile
    assert "ENV_WORKSPACE" in dockerfile


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


def test_the_profile_declares_only_the_image_contract_it_uses() -> None:
    profile = resolve_runtime_profile(_agent_template(), session_kind="agent_chat")
    assert profile.username_template == SANDBOX_IMAGE_WORKLOAD_USER
    assert profile.home_template == SANDBOX_IMAGE_WORKLOAD_HOME
    # Nothing on this path creates a user, so the image is not asked to prove it can.
    assert "useradd" not in profile.required_commands


def test_agent_pool_probe_requires_every_image_owned_claim_capability() -> None:
    profile = resolve_runtime_profile(_agent_template(), session_kind="agent_chat")
    required = set(profile.required_commands)
    assert AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT in required
    assert "/usr/local/bin/astrabox-assistant-workspace-storage" in required


# ── the bootstrap owns what it creates, and not what it finds ──


def test_the_bootstrap_never_re_owns_a_tree_it_did_not_create() -> None:
    """A directory that already exists came from the image or from a MOUNT.

    Walking the home and chowning it reaches mount points: against a root-squashed
    NFS export the chown fails and takes the whole session bootstrap with it — a
    box reaching outside itself, and failing loudly at the wrong boundary.
    """
    assert "find" not in _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS
    assert "-xdev" not in _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS
    assert "chown -R" not in _CONVERSATION_BOOTSTRAP_SCRIPT
    # Both dir helpers return before touching anything that is already there.
    for helper in ("create_workload_dir", "create_platform_dir"):
        body = _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS.split(f"{helper}() {{", 1)[1]
        body = body.split("\n}", 1)[0]
        assert 'if [ -d "$target" ]; then\n    return\n  fi' in body, helper


def test_the_workload_owns_only_what_it_must_write() -> None:
    """Each of these is a directory the CLI writes on a normal turn: its config
    (session state and history, rewritten every turn), its debug logs, the MCP
    server configs, the XDG cache its tools use, its scratch temp, and the
    workspace itself."""
    body = _CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS.split(
        "establish_conversation_identity_root() {", 1
    )[1]
    workload = set(re.findall(r'create_workload_dir "([^"]+)"', body))
    assert workload == {
        "$workspace",
        "$config",
        "$config/debug",
        "$config/mcp",
        "$cache",
        "$tmpdir",
    }


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


def test_an_unwritable_workspace_is_named_instead_of_chowned() -> None:
    """The bootstrap stopped taking ownership of the workspace, so it proves the
    workload can write it. A deployment that mounted a read-only or wrongly-owned
    directory over it learns that here, by path, before a turn starts."""
    assert "CONVERSATION_BOOTSTRAP_WORKSPACE_NOT_WRITABLE" in _CONVERSATION_BOOTSTRAP_SCRIPT
    assert 'runuser -u "$user" -- test -w "$workspace"' in _CONVERSATION_BOOTSTRAP_SCRIPT


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
