"""Conversation-scoped runtime identity helpers.

This module intentionally uses plain dictionaries instead of dataclasses to
avoid sensitivity to dataclass module metadata under some import setups.
"""

import base64
import contextlib
import contextvars
import hashlib
import inspect
import json
import posixpath
import re
import shlex
import zlib
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from opensandbox.models.execd import ExecutionHandlers, RunCommandOpts

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.engine.capabilities import (
    EngineRuntimeProfileDeclaration,
)
from astrabox.seams.sandbox import (
    SANDBOX_TENANCIES,
    SANDBOX_TENANCY_AGENT,
    SANDBOX_TENANCY_CONVERSATION,
    sandbox_for_sandbox,
)

logger = get_logger(__name__)

AGENT_RUNTIME_SKILL_CACHE_DIR = "/opt/conversation-runtime/claude-skills-cache"

# Skill-cache prep fetches each Agent-declared skill's content over the network
# (via git clone); several skills can exceed the default 60s exec timeout, so give
# it the same generous budget as the bootstrap.
_SKILL_CACHE_PREP_TIMEOUT_MS = 300_000
AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT = "/usr/local/bin/astrabox-provision-conversation"
AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_API_PATH = "/conversation/bootstrap"
AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_HTTP_TIMEOUT_SECONDS = 120.0
CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP = "sidecar_http"
CONVERSATION_BOOTSTRAP_TRANSPORT_SANDBOX_COMMAND = "sandbox_command_script"

_IDENTITY_BOOTSTRAP_REQUIRED_COMMANDS = (
    "getent",
    "useradd",
    "runuser",
    "id",
    "mkdir",
    "chown",
    "chmod",
    "cut",
    "sleep",
)
_IDENTITY_BOOTSTRAP_REQUIRED_COMMANDS_SCRIPT = (
    "for cmd in " + " ".join(_IDENTITY_BOOTSTRAP_REQUIRED_COMMANDS) + r"""; do
  command -v "$cmd" >/dev/null
done"""
)
_CONVERSATION_BOOTSTRAP_REQUIRED_COMMANDS = _IDENTITY_BOOTSTRAP_REQUIRED_COMMANDS + (
    "ln",
    "readlink",
    "base64",
    "dirname",
    "cat",
    "rm",
)
_CONVERSATION_BOOTSTRAP_REQUIRED_COMMANDS_SCRIPT = (
    "for cmd in " + " ".join(_CONVERSATION_BOOTSTRAP_REQUIRED_COMMANDS) + r"""; do
  command -v "$cmd" >/dev/null
done"""
)
_CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS = r"""ensure_workload_account() {
  # An account that is already here is the IMAGE's: the agent image declares it
  # to the base entrypoint, which creates it when the container starts — so a
  # prewarmed box, created long before this session, has it too. Do not touch it.
  #
  # An absent account is the assistant engine's case, and only that: one box
  # hosts several per-(user, assistant) profiles, which the image cannot name in
  # advance. Create it here and let the kernel pick the uid — nothing outside
  # this box reads that number, because no tree owned by it is ever mounted into
  # a second box.
  if getent passwd "$user" >/dev/null; then
    actual_home="$(getent passwd "$user" | cut -d: -f6)"
    test "$actual_home" = "$home"
    if [ -n "$requested_uid" ]; then
      test "$(id -u "$user")" = "$requested_uid"
      test "$(id -g "$user")" = "$requested_gid"
    fi
    return
  fi
  # The AIO base creates its image-declared workload identity as two commands:
  # groupadd, then useradd. The resident runner can accept a bootstrap between
  # those commands. A group with this exact name and no passwd row is therefore
  # not an invitation to create a second account; it is the image account's
  # observable in-progress state. Wait for its owner to finish, then re-enter
  # the validation above. If it never finishes, fail on the incomplete image
  # contract instead of adopting or rewriting the group.
  if getent group "$user" >/dev/null; then
    account_waits=0
    while [ "$account_waits" -lt 100 ]; do
      if getent passwd "$user" >/dev/null; then
        ensure_workload_account
        return
      fi
      account_waits=$((account_waits + 1))
      sleep 0.1
    done
    printf 'CONVERSATION_BOOTSTRAP_INCOMPLETE_IMAGE_ACCOUNT user=%s group=%s\n' \
      "$user" "$(getent group "$user" | cut -d: -f3)" >&2
    exit 42
  fi
  if [ -n "$requested_uid" ]; then
    command -v groupadd >/dev/null
    if ! getent group "$requested_gid" >/dev/null; then
      groupadd --gid "$requested_gid" "$user"
    fi
    useradd --create-home --home-dir "$home" --shell /bin/bash \
      --uid "$requested_uid" --gid "$requested_gid" "$user"
  else
    useradd --create-home --home-dir "$home" --shell /bin/bash "$user"
  fi
}

# This bootstrap owns what it CREATES, and nothing else. A directory that is
# already there was made by the image or by a MOUNT, and a mount's ownership
# belongs to whoever mounted it — re-owning it would be the box reaching outside
# itself, and against a root-squashed NFS export it does not even fail at the
# mount: it fails the whole session bootstrap. Same rule the in-box server
# applies to the CLI's cwd (sandbox_ws_server._ensure_working_directory).
create_workload_dir() {
  # Handed to the workload because the workload must WRITE it. Each caller below
  # says why.
  target="$1"
  if [ -d "$target" ]; then
    return
  fi
  mkdir -p "$target"
  chown "$user:$user" "$target"
  chmod 700 "$target"
}

create_platform_dir() {
  # Stays root-owned: the workload gets read + execute, never write. What the
  # platform puts in these directories — the plugin set and deploy key — is
  # part of what CONSTRAINS the agent, and the agent runs code
  # the model chose. An account that could write its own constraints has none.
  # 0755, not 0700: the workload has to traverse and read them.
  target="$1"
  if [ -d "$target" ]; then
    return
  fi
  mkdir -p "$target"
  chmod 755 "$target"
}

establish_conversation_identity_root() {
  home_parent="${home%/*}"
  test "$home_parent" != "$home"
  mkdir -p "$home_parent"
  ensure_workload_account
  actual_home="$(getent passwd "$user" | cut -d: -f6)"
  test "$actual_home" = "$home"

  # The agent's files, exposed through the sandbox provider's native file face.
  create_workload_dir "$workspace"
  # An engine may declare no config directory. When it declares one, the engine
  # owns mutable session state below it and the workload therefore needs write
  # access before a turn can start.
  if [ -n "$config" ]; then
    create_workload_dir "$config"
    create_workload_dir "$config/debug"
    create_workload_dir "$config/mcp"
    # Platform-installed plugin links are constraints, not workload state.
    create_platform_dir "$config/plugins"
  fi
  # XDG cache for the CLI and for whatever it shells out to (npm, pip, git).
  create_workload_dir "$cache"
  # The CLI's scratch space for tool I/O, and where the git askpass helper lands.
  create_workload_dir "$tmpdir"

  # Platform-installed, root-owned: plugin symlinks into the shared cache and
  # the deploy key. The key file
  # itself is written by root and handed to the workload at 0600 because ssh
  # demands that; the directory holding it is not the workload's to refill.
  create_platform_dir "$home/.ssh"
  create_platform_dir "$home/.local/bin"
}"""

_CONVERSATION_SKILLS_BOOTSTRAP_FUNCTIONS = r"""replace_skills_target_with_symlink() {
  target="$1"
  source="$2"
  if [ -e "$target" ] || [ -L "$target" ]; then
    if [ -L "$target" ]; then
      rm -f "$target"
    else
      printf 'CONVERSATION_BOOTSTRAP_NON_SYMLINK_SKILLS target=%s\n' "$target" >&2
      exit 43
    fi
  fi
  ln -s "$source" "$target"
  test -L "$target"
  test "$(readlink "$target")" = "$source"
}"""

_CONVERSATION_BOOTSTRAP_SCRIPT = Path(__file__).with_name("provision-conversation").read_text(
    encoding="utf-8"
)

def _clean(value: Any) -> str:
    return str(value or "").strip()


def _compact_bootstrap_text(value: Any, *, limit: int = 2000) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _bootstrap_payload_detail(payload: dict[str, Any], *, output_limit: int = 2000) -> str:
    error_info = payload.get("error_info")
    error_info = error_info if isinstance(error_info, dict) else {}
    code = str(error_info.get("code") or payload.get("code") or "").strip()
    logical_status = error_info.get("status_code")
    rc = payload.get("rc")
    category = str(error_info.get("category") or "").strip()
    retryable = error_info.get("retryable")
    user_message = _compact_bootstrap_text(error_info.get("user_message"), limit=1200)
    debug_message = _compact_bootstrap_text(error_info.get("debug_message"), limit=1200)
    error = _compact_bootstrap_text(payload.get("error"), limit=1200)
    output = _compact_bootstrap_text(payload.get("output"), limit=output_limit)
    parts: list[str] = []
    if code:
        parts.append(f"code={code}")
    if logical_status is not None:
        parts.append(f"logical_status={logical_status}")
    if category:
        parts.append(f"category={category}")
    if retryable is not None:
        parts.append(f"retryable={bool(retryable)}")
    if rc is not None:
        parts.append(f"rc={rc}")
    if user_message and user_message != error:
        parts.append(f"user_message={user_message}")
    if debug_message and debug_message not in {error, user_message}:
        parts.append(f"debug_message={debug_message}")
    if error:
        parts.append(f"error={error}")
    if output:
        parts.append(f"output={output!r}")
    return " ".join(parts) or "empty sidecar failure payload"


def _http_response_error_detail(response: Any) -> str:
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = response.json()
    except Exception:
        body = _compact_bootstrap_text(getattr(response, "text", ""), limit=500)
        return f"status={status} body={body}"
    if isinstance(payload, dict):
        return f"status={status} {_bootstrap_payload_detail(payload, output_limit=1000)}"
    body = _compact_bootstrap_text(getattr(response, "text", ""), limit=500)
    return f"status={status} body={body}"


def _bootstrap_api_error_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    error_info = payload.get("error_info")
    if not isinstance(error_info, dict):
        return {}
    kwargs: dict[str, Any] = {}
    category = str(error_info.get("category") or "").strip()
    if category:
        kwargs["category"] = category
    if isinstance(error_info.get("retryable"), bool):
        kwargs["retryable"] = bool(error_info.get("retryable"))
    debug_message = _compact_bootstrap_text(
        error_info.get("debug_message") or payload.get("error"),
        limit=1200,
    )
    if debug_message:
        kwargs["debug_message"] = debug_message
    evidence = error_info.get("evidence")
    if isinstance(evidence, dict):
        kwargs["evidence"] = evidence
    cause_code = str(error_info.get("code") or payload.get("code") or "").strip().upper()
    if cause_code and cause_code != "CONVERSATION_BOOTSTRAP_FAILED":
        kwargs["cause_code"] = cause_code
    return kwargs


def _safe_session_id(session_id: str) -> str:
    value = _clean(session_id)
    if not value:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="conversation identity requires session_id",
            status_code=500,
        )
    return value


def assistant_workspace_dir(user_id: str, assistant_id: str) -> str:
    """The Assistant's workspace directory, rendered from its own profile.

    One renderer for a path several callers need. Composing it by hand is what
    the HTML preview did, and the copy drifted the moment the profile's
    `home_template` changed shape — silently, because a preview rooted at a
    directory that does not exist looks like an empty workspace rather than a
    wrong path. An administrator configuring a different template must move
    every reader with it, which is only possible while there is one.
    """

    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    profile = composed_runtime_profile(
        "assistant", SANDBOX_TENANCY_AGENT, session_kind="assistant_chat"
    )
    values = {
        "session_hash": _session_hash(f"{user_id}:{assistant_id}"),
        "user_id": user_id,
        "assistant_id": assistant_id,
    }
    username = profile.username_template.format(**values)
    home = profile.home_template.format(**values, username=username)
    return profile.workspace_source_template.format(
        **values, username=username, home=home
    )


def assistant_config_dir(user_id: str, assistant_id: str) -> str:
    """The Assistant's engine config directory, rendered from its profile.

    Optional workspace storage mounts this profile separately from user files.
    The vendor's live SessionDB resides here; its native snapshot is also held
    in the platform database for restoration without a persistent volume.
    """

    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    profile = composed_runtime_profile(
        "assistant", SANDBOX_TENANCY_AGENT, session_kind="assistant_chat"
    )
    name = str(profile.config_dir_name or "").strip()
    if not name:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="the Assistant profile declares no engine config directory",
            status_code=500,
        )
    return f"{assistant_profile_home(user_id, assistant_id)}/{name}"


def assistant_profile_home(user_id: str, assistant_id: str) -> str:
    """The Assistant's profile home, rendered from the same template.

    The file panel serves the workspace; optional persistent storage mounts
    the workspace and engine config as separate children of this home. Both
    paths render from the same profile so they agree on the workload identity.

    Renderable from the two ids alone, which is what makes it usable where the
    runtime identity is not: a mount is decided when the box is created, and
    the identity is not rendered until the box exists and its account is made.
    """

    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    profile = composed_runtime_profile(
        "assistant", SANDBOX_TENANCY_AGENT, session_kind="assistant_chat"
    )
    values = {
        "session_hash": _session_hash(f"{user_id}:{assistant_id}"),
        "user_id": user_id,
        "assistant_id": assistant_id,
    }
    username = profile.username_template.format(**values)
    return profile.home_template.format(**values, username=username)


def _session_hash(session_id: str) -> str:
    digest = hashlib.sha256(_safe_session_id(session_id).encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:16]


#: First POSIX owner an agent's conversations are allocated. Below it sit the
#: image's own accounts, and the ``agent`` user the profile renders by name.
CONVERSATION_UID_BASE = 2000

#: Refuse past this rather than wrap. Wrapping would hand a live conversation an
#: owner some earlier conversation's files are still sitting under, which is the
#: exact collision the allocation exists to prevent — and it would do it
#: silently, which is worse than an agent that stops taking new conversations.
CONVERSATION_UID_CEILING = 60000

#: Concurrent claims on one agent's cursor are resolved by retry, not by a lock.
#: The bound is small because contention is per agent: it takes two of the same
#: agent's conversations starting in the same instant to collide once.
_UID_ALLOCATION_ATTEMPTS = 8


async def allocate_agent_scoped_uid(agent_repo: Any, agent_id: str) -> int:
    """Hand out a POSIX owner no conversation of this agent has held before.

    Monotonic and never recycled. The cursor only moves forward, and a released
    conversation does not give its number back — a uid is safe to reuse only
    once nothing it owns is left on disk, and this layer cannot know that. The
    ceiling is therefore a real end, not a wrap point.

    Scoped to the agent because that is the blast radius: the conversations that
    can meet on one filesystem are the ones sharing a box, and a shared box
    serves one agent's conversations. A deployment that ever lets two agents
    share a box needs a deployment-wide cursor instead, and this is the function
    that would have to change.

    Claimed by compare-and-set on the agent row so two conversations starting
    together cannot read the same cursor and both take it.
    """
    agent_key = str(agent_id or "").strip()
    if not agent_key:
        raise APIError(
            code="AGENT_RUNTIME_ERROR",
            message="cannot allocate a conversation uid without an agent id",
            status_code=500,
        )
    for _ in range(_UID_ALLOCATION_ATTEMPTS):
        agent = await agent_repo.get_agent(agent_key)
        if not isinstance(agent, dict):
            raise APIError(
                code="AGENT_NOT_FOUND",
                message=f"agent {agent_key} not found while allocating a conversation uid",
                status_code=404,
            )
        current = agent.get("conversation_uid_cursor")
        cursor = int(current) if isinstance(current, int) else CONVERSATION_UID_BASE - 1
        allocated = max(cursor + 1, CONVERSATION_UID_BASE)
        if allocated > CONVERSATION_UID_CEILING:
            raise APIError(
                code="AGENT_RUNTIME_ERROR",
                message=(
                    f"agent {agent_key} has exhausted its conversation uid range "
                    f"({CONVERSATION_UID_BASE}-{CONVERSATION_UID_CEILING}); numbers are "
                    "never recycled because files outlive the conversations that wrote them"
                ),
                status_code=500,
            )
        # `expected` carries the cursor this loop just read. A racing claim moves
        # it, this update matches nothing, and the retry re-reads rather than
        # overwriting.
        if await agent_repo.compare_and_update_agent(
            agent_key,
            expected={"conversation_uid_cursor": current}
            if isinstance(current, int)
            else {"conversation_uid_cursor": {"$exists": False}},
            updates={"conversation_uid_cursor": allocated},
        ):
            return allocated
    raise APIError(
        code="AGENT_RUNTIME_ERROR",
        message=(
            f"could not claim a conversation uid for agent {agent_key} after "
            f"{_UID_ALLOCATION_ATTEMPTS} attempts"
        ),
        status_code=503,
    )


def plan_conversation_identity(
    *,
    session_id: str,
    sandbox_id: str | None,
    agent_id: str | None,
    runtime_profile: EngineRuntimeProfileDeclaration,
    identity_values: dict[str, Any] | None = None,
    generation: int = 1,
) -> dict[str, Any]:
    """Render one adapter declaration into a deterministic runtime identity."""
    effective_session_id = _safe_session_id(session_id)
    effective_sandbox_id = _clean(sandbox_id)
    template_values = {
        **dict(identity_values or {}),
        "session_id": effective_session_id,
        "session_hash": _session_hash(effective_session_id),
        "agent_id": _clean(agent_id),
        "sandbox_id": effective_sandbox_id,
    }
    linux_user = _render_username(runtime_profile, template_values)
    template_values["username"] = linux_user
    home_dir = _render_profile_template(
        runtime_profile, "home_template", template_values
    )
    template_values["home"] = home_dir
    workspace_dir = _render_profile_template(
        runtime_profile, "workspace_template", template_values
    )
    template_values["workspace"] = workspace_dir
    workspace_source_dir = _render_profile_template(
        runtime_profile, "workspace_source_template", template_values
    )
    template_values["workspace_source"] = workspace_source_dir
    config_dir = (
        f"{home_dir}/{runtime_profile.config_dir_name}"
        if runtime_profile.config_dir_name
        else ""
    )
    cache_dir = _render_profile_template(
        runtime_profile, "cache_template", template_values
    )
    temp_dir = _render_profile_template(
        runtime_profile, "temp_template", template_values
    )
    file_root_dir = _render_profile_template(
        runtime_profile, "file_root_template", template_values
    )
    file_root_source_dir = _map_path_between_roots(
        file_root_dir,
        source_root=workspace_dir,
        target_root=workspace_source_dir,
    )
    for path_key, path_value in (
        ("cache_template", cache_dir),
        ("temp_template", temp_dir),
    ):
        _require_path_under(path_value, home_dir, key=path_key)
    if runtime_profile.sandbox_tenancy == SANDBOX_TENANCY_AGENT:
        _require_path_under(
            workspace_source_dir,
            home_dir,
            key="workspace_source_template",
        )
    elif workspace_source_dir != workspace_dir:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=(
                "a per-Session sandbox must use its visible workspace as the "
                "physical workspace source"
            ),
            status_code=500,
        )
    if config_dir:
        _require_path_under(config_dir, home_dir, key="config_dir_name")
    return {
        "sandbox_tenancy": runtime_profile.sandbox_tenancy,
        "sandbox_id": effective_sandbox_id or None,
        "agent_id": _clean(agent_id) or None,
        "session_id": effective_session_id,
        "linux_user": linux_user,
        "home_dir": home_dir,
        "workspace_dir": workspace_dir,
        "workspace_source_dir": workspace_source_dir,
        "file_root_dir": file_root_dir,
        "file_root_source_dir": file_root_source_dir,
        "config_dir": config_dir,
        "config_env_var": runtime_profile.config_env_var,
        "cache_dir": cache_dir,
        "temp_dir": temp_dir,
        "generation": int(generation or 1),
        "current_stage": "planned",
        "stage_evidence": {},
        "status": "planned",
    }


def plan_assistant_profile_identity(
    *,
    engine_kind: str,
    user_id: str,
    assistant_id: str,
    sandbox_id: str | None = None,
) -> dict[str, Any]:
    """Render an Assistant profile from its engine's declared runtime shape."""

    normalized_engine = _clean(engine_kind)
    normalized_user = _clean(user_id)
    normalized_assistant = _clean(assistant_id)
    if not normalized_engine or not normalized_user or not normalized_assistant:
        raise ValueError(
            "assistant profile identity requires engine_kind, user_id, and assistant_id"
        )
    from astrabox.core.service.orchestrator.engine.capabilities import (
        engine_allowed_for_session_kind,
    )
    from astrabox.core.service.orchestrator.engine.runtime_profiles import (
        composed_runtime_profile,
    )

    if not engine_allowed_for_session_kind(normalized_engine, "assistant_chat"):
        raise ValueError(
            f"engine_kind={normalized_engine!r} does not support assistant_chat"
        )
    profile = composed_runtime_profile(
        normalized_engine,
        SANDBOX_TENANCY_AGENT,
        session_kind="assistant_chat",
    )
    return plan_conversation_identity(
        session_id=f"{normalized_user}:{normalized_assistant}",
        sandbox_id=sandbox_id,
        agent_id=None,
        runtime_profile=profile,
        identity_values={
            "user_id": normalized_user,
            "assistant_id": normalized_assistant,
        },
    )


def normalize_runtime_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    linux_user = _clean(value.get("linux_user"))
    home_dir = _clean(value.get("home_dir"))
    workspace_dir = _clean(value.get("workspace_dir"))
    if not linux_user or not home_dir or not workspace_dir:
        return None
    normalized = dict(value)
    normalized["linux_user"] = linux_user
    normalized["home_dir"] = home_dir
    normalized["workspace_dir"] = workspace_dir
    sandbox_tenancy = _clean(value.get("sandbox_tenancy"))
    if sandbox_tenancy not in SANDBOX_TENANCIES:
        return None
    workspace_source_dir = _clean(value.get("workspace_source_dir"))
    if not workspace_source_dir:
        normalized_home = posixpath.normpath(home_dir)
        normalized_workspace = posixpath.normpath(workspace_dir)
        workspace_is_under_home = normalized_workspace == normalized_home or (
            normalized_workspace.startswith(f"{normalized_home.rstrip('/')}/")
        )
        if sandbox_tenancy != SANDBOX_TENANCY_CONVERSATION and not workspace_is_under_home:
            return None
        workspace_source_dir = workspace_dir
    normalized["workspace_source_dir"] = workspace_source_dir
    normalized["file_root_dir"] = _clean(value.get("file_root_dir")) or workspace_dir
    normalized["file_root_source_dir"] = (
        _clean(value.get("file_root_source_dir"))
        or _map_path_between_roots(
            normalized["file_root_dir"],
            source_root=workspace_dir,
            target_root=workspace_source_dir,
        )
    )
    normalized["config_dir"] = _clean(value.get("config_dir"))
    config_env_var = _clean(value.get("config_env_var"))
    if config_env_var and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config_env_var) is None:
        return None
    if config_env_var and not normalized["config_dir"]:
        return None
    normalized["config_env_var"] = config_env_var or None
    normalized["cache_dir"] = _clean(value.get("cache_dir")) or f"{home_dir}/.cache"
    normalized["temp_dir"] = _clean(value.get("temp_dir")) or f"{home_dir}/tmp"
    normalized["sandbox_tenancy"] = sandbox_tenancy
    normalized["generation"] = int(value.get("generation") or 1)
    for int_key in ("uid", "gid"):
        if int_key in value and value.get(int_key) is not None:
            try:
                normalized[int_key] = int(value.get(int_key))
            except (TypeError, ValueError):
                normalized.pop(int_key, None)
    return normalized


def mark_runtime_identity_failed(
    identity: dict[str, Any] | None,
    error: BaseException,
    *,
    stage: str,
) -> dict[str, Any] | None:
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return None
    failed = dict(normalized)
    failed["status"] = "failed"
    failed["current_stage"] = stage
    evidence = failed.get("stage_evidence") if isinstance(failed.get("stage_evidence"), dict) else {}
    failed["stage_evidence"] = {
        **evidence,
        f"{stage}_error": str(error),
    }
    return failed


def _render_username(
    profile: EngineRuntimeProfileDeclaration,
    values: dict[str, Any],
) -> str:
    template = profile.username_template
    try:
        username = template.format(**values)
    except Exception as exc:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"invalid username_template in runtime profile: {exc}",
            status_code=500,
        ) from exc
    username = _clean(username)
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile rendered invalid Linux username: {username!r}",
            status_code=500,
        )
    return username


def _render_profile_template(
    profile: EngineRuntimeProfileDeclaration,
    key: str,
    values: dict[str, Any],
) -> str:
    template = str(getattr(profile, key) or "").strip()
    try:
        rendered = template.format(**values)
    except Exception as exc:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"invalid {key} in runtime profile: {exc}",
            status_code=500,
        ) from exc
    path = _clean(rendered).rstrip("/")
    if not path.startswith("/"):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} must render an absolute path: {path!r}",
            status_code=500,
        )
    path = _normalize_identity_path(path, key=key)
    if path == "/root" or path.startswith("/root/"):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} must not render a root-owned path: {path}",
            status_code=500,
        )
    return path


def _normalize_identity_path(path: str, *, key: str) -> str:
    raw = str(path or "").strip()
    if any(part == ".." for part in raw.split("/")):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} must not contain parent traversal: {raw}",
            status_code=500,
        )
    normalized = posixpath.normpath(raw)
    if not normalized.startswith("/"):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} must normalize to an absolute path: {raw!r}",
            status_code=500,
        )
    if not re.fullmatch(r"/[A-Za-z0-9._/@:=+-]*(?:/[A-Za-z0-9._@:=+-]+)*", normalized):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} contains unsupported path characters: {raw}",
            status_code=500,
        )
    return normalized


def _require_path_under(path: str, root: str, *, key: str) -> None:
    value = _normalize_identity_path(path, key=key).rstrip("/")
    base = _normalize_identity_path(root, key="home_template").rstrip("/")
    if value != base and not value.startswith(f"{base}/"):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message=f"runtime profile {key} must stay under the conversation home: {value}",
            status_code=500,
        )


def _map_path_between_roots(
    path: str,
    *,
    source_root: str,
    target_root: str,
) -> str:
    """Map ``path`` from one equivalent workspace root to the other."""

    value = posixpath.normpath(str(path or "").strip())
    source = posixpath.normpath(str(source_root or "").strip())
    target = posixpath.normpath(str(target_root or "").strip())
    if value != source and not value.startswith(f"{source.rstrip('/')}/"):
        return value
    relative = posixpath.relpath(value, source)
    return target if relative == "." else posixpath.normpath(posixpath.join(target, relative))


def identity_workspace_dir(identity: dict[str, Any] | None) -> str | None:
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return None
    return _clean(normalized.get("workspace_dir")) or None


def identity_workspace_source_dir(identity: dict[str, Any] | None) -> str | None:
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return None
    return _clean(normalized.get("workspace_source_dir")) or None


def identity_file_root_dir(identity: dict[str, Any] | None) -> str | None:
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return None
    return _clean(normalized.get("file_root_dir")) or _clean(normalized.get("workspace_dir")) or None


def identity_file_root_source_dir(identity: dict[str, Any] | None) -> str | None:
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return None
    return _clean(normalized.get("file_root_source_dir")) or None


def identity_path_to_source(
    identity: dict[str, Any] | None,
    path: str,
) -> str:
    """Translate a visible workspace path for box-level filesystem access."""

    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return str(path or "")
    return _map_path_between_roots(
        str(path or ""),
        source_root=normalized["workspace_dir"],
        target_root=normalized["workspace_source_dir"],
    )


def identity_path_from_source(
    identity: dict[str, Any] | None,
    path: str,
) -> str:
    """Translate a box-level workspace path back to its visible path."""

    normalized = normalize_runtime_identity(identity)
    if not normalized:
        return str(path or "")
    return _map_path_between_roots(
        str(path or ""),
        source_root=normalized["workspace_source_dir"],
        target_root=normalized["workspace_dir"],
    )


def command_for_identity(command: str, identity: dict[str, Any] | None) -> str:
    """Wrap a shell command so it executes as the conversation Linux user."""
    normalized = normalize_runtime_identity(identity)
    raw_command = str(command or "")
    if not normalized:
        return raw_command
    linux_user = normalized["linux_user"]
    home_dir = normalized["home_dir"]
    workspace_dir = normalized["workspace_source_dir"]
    default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    local_bin = f"{home_dir.rstrip('/')}/.local/bin"
    script = (
        "set -e; "
        f"cd -- {shlex.quote(workspace_dir)}; "
        f"export HOME={shlex.quote(home_dir)} USER={shlex.quote(linux_user)} LOGNAME={shlex.quote(linux_user)} "
        f"PWD={shlex.quote(workspace_dir)} PATH={shlex.quote(local_bin)}:${{PATH:-{default_path}}}; "
        f"exec bash -c {shlex.quote(raw_command)}"
    )
    return f"runuser -u {shlex.quote(linux_user)} -- bash -lc {shlex.quote(script)}"


def _build_conversation_bootstrap_env(
    normalized: dict[str, Any],
    *,
    skills: list[str] | tuple[str, ...] | None = None,
    default_repo: dict[str, Any] | None = None,
    plugin_links: list[dict[str, Any]] | None = None,
    plugin_cache_hash: str | None = None,
    plugin_cache_dir: str | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "CONV_USER": normalized["linux_user"],
        "CONV_HOME": normalized["home_dir"],
        "CONV_WORKSPACE": normalized["workspace_source_dir"],
        "CONV_CONFIG": normalized["config_dir"],
        "CONV_CACHE": normalized["cache_dir"],
        "CONV_TMP": normalized["temp_dir"],
    }
    uid = normalized.get("uid")
    gid = normalized.get("gid")
    if (uid is None) != (gid is None):
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="runtime identity must carry both uid and gid or neither",
            status_code=500,
        )
    if uid is not None and gid is not None:
        env["CONV_UID"] = str(int(uid))
        env["CONV_GID"] = str(int(gid))
    skill_items = [str(item).strip() for item in (skills or []) if str(item).strip()]
    if skill_items:
        manifest = _skill_manifest(skill_items)
        env["CONV_SKILL_MANIFEST_B64"] = base64.b64encode(manifest.encode("utf-8")).decode("ascii")
        env["CONV_SKILL_CACHE_DIR"] = AGENT_RUNTIME_SKILL_CACHE_DIR
    repo = dict(default_repo or {}) if isinstance(default_repo, dict) else {}
    if repo:
        url = _clean(repo.get("url"))
        target = _clean(repo.get("target"))
        key_b64 = _clean(repo.get("key_b64"))
        https_token = _clean(repo.get("https_token"))
        if not url or not target or (not key_b64 and not https_token):
            raise APIError(
                code="DEFAULT_REPO_INVALID",
                message="conversation default_repo bootstrap payload is incomplete",
                status_code=500,
            )
        env["CONV_DEFAULT_REPO_URL"] = url
        env["CONV_DEFAULT_REPO_TARGET"] = identity_path_to_source(normalized, target)
        if key_b64:
            env["CONV_DEFAULT_REPO_KEY_B64"] = key_b64
        branch = _clean(repo.get("branch"))
        depth = _clean(repo.get("depth"))
        if branch:
            env["CONV_DEFAULT_REPO_BRANCH"] = branch
        if depth:
            env["CONV_DEFAULT_REPO_DEPTH"] = depth
        if https_token:
            env["CONV_DEFAULT_REPO_HTTPS_TOKEN"] = https_token
    links = [dict(item) for item in (plugin_links or []) if isinstance(item, dict)]
    if links:
        missing = [
            name
            for name, value in (
                ("plugin_cache_hash", plugin_cache_hash),
                ("plugin_cache_dir", plugin_cache_dir),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise APIError(
                code="AGENT_RUNTIME_PLUGIN_CACHE_MISSING",
                message=f"conversation plugin bootstrap missing fields: {', '.join(missing)}",
                status_code=500,
            )
        lines: list[str] = []
        for item in links:
            source = _clean(item.get("source"))
            dest = _clean(item.get("dest"))
            plugin_dirs = [_clean(path) for path in (item.get("plugin_dirs") or []) if _clean(path)]
            if not source or not dest or not plugin_dirs:
                raise APIError(
                    code="AGENT_RUNTIME_PLUGIN_CACHE_MISSING",
                    message="conversation plugin bootstrap link is incomplete",
                    status_code=500,
                )
            lines.append(source + "\t" + dest + "\t" + "\x1f".join(plugin_dirs))
        env["CONV_PLUGIN_LINKS_B64"] = base64.b64encode(("\n".join(lines) + "\n").encode("utf-8")).decode("ascii")
        env["CONV_PLUGIN_CACHE_HASH"] = str(plugin_cache_hash or "").strip()
        env["CONV_PLUGIN_CACHE_DIR"] = str(plugin_cache_dir or "").strip()
    return env


def _ready_identity_from_bootstrap_output(
    normalized: dict[str, Any],
    output: str,
    *,
    transport: str,
) -> dict[str, Any]:
    parsed = _parse_bootstrap_ready_line(output)
    timings, timings_error = _parse_bootstrap_timings(parsed.get("timings_b64"))
    ready = dict(normalized)
    ready["status"] = "ready"
    ready["current_stage"] = "ready"
    stage_evidence = {
        **(ready.get("stage_evidence") if isinstance(ready.get("stage_evidence"), dict) else {}),
        "bootstrap_transport": transport,
        "bootstrap_output": output[:1000],
    }
    if timings:
        stage_evidence["bootstrap_timings_ms"] = timings
    if timings_error:
        stage_evidence["bootstrap_timings_error"] = timings_error
    ready["stage_evidence"] = stage_evidence
    ready.update({key: value for key, value in parsed.items() if key in {"uid", "gid"}})
    return ready


async def _run_installed_conversation_bootstrap_script_via_command(
    sandbox: Any,
    env: dict[str, str],
    *,
    error_code: str,
    error_message: str,
) -> str:
    command_runner = getattr(sandbox, "commands", None)
    run_fn = getattr(command_runner, "run", None) if command_runner is not None else None
    if not callable(run_fn):
        raise APIError(
            code=error_code,
            message=f"{error_message}: sandbox command runner is unavailable",
            status_code=502,
        )

    # `2>&1` because the script's readiness marker goes to stdout and its
    # failure line goes to stderr, and those are the two answers this call has.
    # Keeping them on one channel means a failure arrives with the line that
    # names it rather than with the exit status alone.
    command = f"bash {shlex.quote(AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT)} 2>&1"
    bootstrap_timeout_ms = 300_000
    streamed: list[str] = []

    async def _keep(message: Any) -> None:
        text = getattr(message, "text", None)
        streamed.append(str(text if text is not None else message))

    result = await run_sandbox_command(
        run_fn,
        command,
        envs=env,
        timeout_in_millis=bootstrap_timeout_ms,
        handlers=ExecutionHandlers(on_stdout=_keep, on_stderr=_keep),
    )
    error = getattr(result, "error", None)
    output = _command_output(result, streamed=streamed)
    if error or "CONVERSATION_BOOTSTRAP_READY" not in output:
        raise APIError(
            code=error_code,
            message=(
                f"{error_message}: "
                f"{error or 'missing readiness marker'}; "
                f"command={command!r}; dispatch={_last_dispatch_branch()}; "
                f"env={_env_shape(env)}; output={output[:2000]!r}"
            ),
            status_code=502,
        )
    return output


async def run_conversation_bootstrap_script(
    sandbox: Any,
    identity: dict[str, Any],
    *,
    sidecar_endpoint: str | None = None,
    bootstrap_transport: str = CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP,
    skills: list[str] | tuple[str, ...] | None = None,
    default_repo: dict[str, Any] | None = None,
    plugin_links: list[dict[str, Any]] | None = None,
    plugin_cache_hash: str | None = None,
    plugin_cache_dir: str | None = None,
) -> dict[str, Any]:
    """Run the pre-installed bootstrap script using the backend-selected transport."""
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="runtime_identity is missing required fields",
            status_code=500,
        )

    env = _build_conversation_bootstrap_env(
        normalized,
        skills=skills,
        default_repo=default_repo,
        plugin_links=plugin_links,
        plugin_cache_hash=plugin_cache_hash,
        plugin_cache_dir=plugin_cache_dir,
    )

    transport = str(bootstrap_transport or CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP).strip()
    if transport == CONVERSATION_BOOTSTRAP_TRANSPORT_SANDBOX_COMMAND:
        if sandbox is None:
            raise APIError(
                code="CONVERSATION_BOOTSTRAP_FAILED",
                message="conversation bootstrap command transport requires a live sandbox handle",
                status_code=502,
            )
        output = await _run_installed_conversation_bootstrap_script_via_command(
            sandbox,
            env,
            error_code="CONVERSATION_BOOTSTRAP_FAILED",
            error_message="failed to bootstrap conversation runtime from agent cache via sandbox command",
        )
        ready = _ready_identity_from_bootstrap_output(
            normalized,
            output,
            transport=transport,
        )
        logger.info(
            "conversation bootstrap completed: session=%s sandbox=%s user=%s workspace=%s",
            ready.get("session_id"),
            ready.get("sandbox_id"),
            ready.get("linux_user"),
            ready.get("workspace_dir"),
        )
        return ready
    if transport != CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP:
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_TRANSPORT_UNSUPPORTED",
            message=f"unsupported conversation bootstrap transport: {transport!r}",
            status_code=500,
        )

    endpoint = str(sidecar_endpoint or "").strip() or await _sidecar_endpoint(sandbox)
    dataplane_sandbox = _dataplane_sandbox_candidate(sandbox)
    payload = {"env": env}
    encoded_payload = _encode_sidecar_payload(payload)
    try:
        response = await sandbox_for_sandbox(dataplane_sandbox).build_dataplane(
            sandbox=dataplane_sandbox,
            endpoint=endpoint,
            port=8000,
        ).request(
            "GET",
            f"{AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_API_PATH}/{encoded_payload}",
            timeout=AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_HTTP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_FAILED",
            message=f"conversation bootstrap sidecar request failed: {exc}",
            status_code=502,
        ) from exc
    if response.status_code != 200:
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_FAILED",
            message=(
                "conversation bootstrap sidecar returned "
                f"{_http_response_error_detail(response)}"
            ),
            status_code=502,
        )
    try:
        response_payload = response.json()
    except Exception as exc:
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_FAILED",
            message=f"conversation bootstrap sidecar returned invalid JSON: {exc}",
            status_code=502,
        ) from exc
    if not isinstance(response_payload, dict):
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_FAILED",
            message="conversation bootstrap sidecar returned non-object JSON",
            status_code=502,
        )
    output = str(response_payload.get("output") or "")
    if int(getattr(response, "status_code", 0) or 0) != 200 or not response_payload.get("ok") or "CONVERSATION_BOOTSTRAP_READY" not in output:
        detail = _bootstrap_payload_detail(response_payload)
        raise APIError(
            code="CONVERSATION_BOOTSTRAP_FAILED",
            message=(
                "failed to bootstrap conversation runtime from agent cache via sidecar API: "
                f"status={getattr(response, 'status_code', 0)} {detail}"
            ),
            status_code=502,
            **_bootstrap_api_error_kwargs(response_payload),
        )
    ready = _ready_identity_from_bootstrap_output(
        normalized,
        output,
        transport=transport,
    )
    logger.info(
        "conversation bootstrap completed: session=%s sandbox=%s user=%s workspace=%s",
        ready.get("session_id"),
        ready.get("sandbox_id"),
        ready.get("linux_user"),
        ready.get("workspace_dir"),
    )
    return ready


async def provision_conversation_identity_with_bootstrap_script(
    sandbox: Any,
    identity: dict[str, Any],
) -> dict[str, Any]:
    """Provision identity via the installed bootstrap script, not inline shell.

    The backend inspects /command traffic before it reaches the sandbox. Keep the
    request body to a stable executable path plus environment values; the
    privileged user/group/chown logic lives in the installed script file.
    """
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="runtime_identity is missing required fields",
            status_code=500,
        )
    env = _build_conversation_bootstrap_env(normalized)
    output = await _run_installed_conversation_bootstrap_script_via_command(
        sandbox,
        env,
        error_code="CONVERSATION_IDENTITY_UNSUPPORTED",
        error_message="failed to provision conversation identity via installed bootstrap script",
    )
    ready = dict(normalized)
    ready["status"] = "ready"
    ready["current_stage"] = "ready"
    stage_evidence = ready.get("stage_evidence") if isinstance(ready.get("stage_evidence"), dict) else {}
    ready["stage_evidence"] = {
        **stage_evidence,
        "bootstrap_script": AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT,
        "bootstrap_transport": "sandbox_command_script",
        "bootstrap_output": output[:1000],
    }
    ready.update({key: value for key, value in _parse_bootstrap_ready_line(output).items() if key in {"uid", "gid"}})
    logger.info(
        "conversation identity provisioned via installed bootstrap: session=%s sandbox=%s user=%s workspace=%s",
        ready.get("session_id"),
        ready.get("sandbox_id"),
        ready.get("linux_user"),
        ready.get("workspace_dir"),
    )
    return ready


async def provision_conversation_identity(sandbox: Any, identity: dict[str, Any]) -> dict[str, Any]:
    """Create and verify the Linux user/home/workspace for a conversation."""
    normalized = normalize_runtime_identity(identity)
    if not normalized:
        raise APIError(
            code="CONVERSATION_IDENTITY_INVALID",
            message="runtime_identity is missing required fields",
            status_code=500,
        )
    command_runner = getattr(sandbox, "commands", None)
    run_fn = getattr(command_runner, "run", None) if command_runner is not None else None
    if not callable(run_fn):
        raise APIError(
            code="CONVERSATION_IDENTITY_UNSUPPORTED",
            message="sandbox command runner is required to provision conversation identity",
            status_code=502,
        )

    user = normalized["linux_user"]
    home = normalized["home_dir"]
    workspace = normalized["workspace_source_dir"]
    config = normalized["config_dir"]
    cache = normalized["cache_dir"]
    temp = normalized["temp_dir"]
    script = f"""
set -euo pipefail
user={shlex.quote(user)}
home={shlex.quote(home)}
workspace={shlex.quote(workspace)}
config={shlex.quote(config)}
cache={shlex.quote(cache)}
tmpdir={shlex.quote(temp)}
trap 'rc=$?; printf "CONVERSATION_IDENTITY_PROVISION_FAILED line=%s rc=%s\\n" "$LINENO" "$rc" >&2; exit "$rc"' ERR
{_IDENTITY_BOOTSTRAP_REQUIRED_COMMANDS_SCRIPT}
identity_error_prefix=CONVERSATION_IDENTITY

{_CONVERSATION_IDENTITY_BOOTSTRAP_FUNCTIONS}

establish_conversation_identity_root
	actual_user=$(runuser -u "$user" -- id -un)
	test "$actual_user" = "$user"
	actual_home=$(runuser -u "$user" -- bash -lc 'printf "%s" "$HOME"')
	test "$actual_home" = "$home"
	actual_pwd=$(runuser -u "$user" -- bash -lc 'cd "$1" && pwd -P' _ "$workspace")
	test "$actual_pwd" = "$workspace"
	uid=$(id -u "$user")
	gid=$(id -g "$user")
printf 'CONVERSATION_IDENTITY_READY user=%s uid=%s gid=%s home=%s workspace=%s\\n' "$user" "$uid" "$gid" "$home" "$workspace"
"""
    result = await run_fn(f"bash -lc {shlex.quote(script)}")
    error = getattr(result, "error", None)
    output = _command_output(result)
    if error or "CONVERSATION_IDENTITY_READY" not in output:
        raise APIError(
            code="CONVERSATION_IDENTITY_UNSUPPORTED",
            message=f"failed to provision conversation identity: {error or 'missing readiness marker'}; output={output[:2000]!r}",
            status_code=502,
        )
    ready = dict(normalized)
    ready["status"] = "ready"
    ready["current_stage"] = "ready"
    ready["stage_evidence"] = {
        **(ready.get("stage_evidence") if isinstance(ready.get("stage_evidence"), dict) else {}),
        "provision_output": output[:1000],
    }
    uid_gid = _parse_identity_ready_line(output)
    ready.update(uid_gid)
    logger.info(
        "conversation identity provisioned: session=%s sandbox=%s user=%s workspace=%s",
        ready.get("session_id"),
        ready.get("sandbox_id"),
        ready.get("linux_user"),
        ready.get("workspace_dir"),
    )
    return ready


def _parse_skill_descriptor(descriptor: str) -> tuple[str, str, str, str]:
    """Parse a git skill descriptor ``<repo>@<ref>#<path>`` into
    ``(repo, ref, path, name)``.

    ``<repo>`` is required; ``@<ref>`` (branch/tag/commit) and ``#<path>`` (skill
    dir inside the repo) are optional. The ref split is authority-aware so an SSH
    URL (``git@host:org/repo``) or an embedded-credential URL
    (``https://x:tok@host/org/repo``) is not mistaken for a ref delimiter. Fails
    loud on a malformed descriptor so a bad Agent entry never reaches a sandbox
    exec.
    """
    raw = descriptor.strip()
    if not raw:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message="skill descriptor must not be empty",
            status_code=500,
        )
    repo_ref, _, path = raw.partition("#")
    path = path.strip().strip("/")
    scheme_match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", repo_ref)
    if scheme_match:
        authority_start = scheme_match.end()
        authority_end = repo_ref.find("/", authority_start)
        if authority_end == -1:
            authority_end = len(repo_ref)
        userinfo_at = repo_ref.rfind("@", authority_start, authority_end)
        search_from = userinfo_at + 1 if userinfo_at != -1 else authority_start
    else:
        colon = repo_ref.find(":")
        first_at = repo_ref.find("@")
        if first_at != -1 and (colon == -1 or first_at < colon):
            search_from = first_at + 1
        else:
            search_from = 0
    ref_at = repo_ref.find("@", search_from)
    if ref_at != -1:
        repo = repo_ref[:ref_at].strip()
        ref = repo_ref[ref_at + 1:].strip()
    else:
        repo = repo_ref.strip()
        ref = ""
    if not repo:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor is missing a repo: {descriptor!r}",
            status_code=500,
        )
    if "@" in ref or "#" in ref:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor has a malformed ref: {descriptor!r}",
            status_code=500,
        )
    if path:
        name = posixpath.basename(path)
    else:
        repo_trimmed = repo[:-4] if repo.endswith(".git") else repo
        name = re.split(r"[/:]", repo_trimmed)[-1]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill descriptor derived an invalid name {name!r} from {descriptor!r}",
            status_code=500,
        )
    return repo, ref, path, name


def skill_repo_egress_hosts(skills: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return the Git origins required by the existing Skills installer."""
    hosts: list[str] = []
    for item in skills or ():
        descriptor = str(item).strip()
        if not descriptor:
            continue
        repo, _, _, _ = _parse_skill_descriptor(descriptor)
        if "://" in repo:
            parsed = urlsplit(repo)
            host = "" if parsed.scheme == "file" else parsed.hostname or ""
        else:
            match = re.fullmatch(r"(?:[^@\s/:]+@)?([^/:\s]+):.+", repo)
            host = match.group(1) if match else ""
        if host and host.lower() not in hosts:
            hosts.append(host.lower())
    return hosts


def _skill_descriptor_table(skill_items: list[str]) -> str:
    """Render skill descriptors into a base64 table for the in-sandbox git loop
    (one row per skill, rows newline-joined; the five columns repo, ref, path,
    name, is_sha are joined by the 0x1f unit separator — not a tab, which is
    IFS-whitespace and would collapse an empty ref/path field on the shell side).

    Each descriptor is parsed here so a malformed entry fails loud in Python
    before any sandbox exec; ``is_sha`` marks a full/short commit SHA ref so the
    loop clones then detaches instead of a shallow branch clone.
    """
    rows: list[str] = []
    for descriptor in skill_items:
        repo, ref, path, name = _parse_skill_descriptor(descriptor)
        is_sha = "1" if re.fullmatch(r"[0-9a-fA-F]{7,40}", ref) else "0"
        rows.append("\x1f".join((repo, ref, path, name, is_sha)))
    return base64.b64encode(("\n".join(rows) + "\n").encode("utf-8")).decode("ascii")


async def prepare_agent_runtime_skill_cache(
    sandbox: Any,
    skills: list[str] | tuple[str, ...] | None,
) -> None:
    skill_items = [str(item).strip() for item in (skills or []) if str(item).strip()]
    if not skill_items:
        return
    command_runner = getattr(sandbox, "commands", None)
    run_fn = getattr(command_runner, "run", None) if command_runner is not None else None
    if not callable(run_fn):
        raise APIError(
            code="CONVERSATION_IDENTITY_UNSUPPORTED",
            message="sandbox command runner is required to prepare agent runtime skill cache",
            status_code=502,
        )

    manifest = _skill_manifest(skill_items)
    manifest_b64 = base64.b64encode(manifest.encode("utf-8")).decode("ascii")
    manifest_hash = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    skills_table_b64 = _skill_descriptor_table(skill_items)
    cache_dir = AGENT_RUNTIME_SKILL_CACHE_DIR
    script = f"""
set -euo pipefail
cache_dir={shlex.quote(cache_dir)}
lock_dir="$cache_dir.lock"
desired_hash={shlex.quote(manifest_hash)}
desired_manifest=$(printf %s {shlex.quote(manifest_b64)} | base64 -d)
if test -f "$cache_dir/.skill-cache-hash" && test "$(cat "$cache_dir/.skill-cache-hash")" = "$desired_hash"; then
  printf 'AGENT_RUNTIME_SKILL_CACHE_READY hash=%s cached=1\\n' "$desired_hash"
  exit 0
fi
mkdir -p "$(dirname "$cache_dir")"
locked=0
for attempt in $(seq 1 180); do
  if mkdir "$lock_dir" 2>/dev/null; then
    locked=1
    break
  fi
  if test -f "$cache_dir/.skill-cache-hash" && test "$(cat "$cache_dir/.skill-cache-hash")" = "$desired_hash"; then
    printf 'AGENT_RUNTIME_SKILL_CACHE_READY hash=%s cached=1\\n' "$desired_hash"
    exit 0
  fi
  sleep 1
done
test "$locked" = "1"
# Stage the clone in a LOCAL dir, never under cache_dir: when cache_dir is bind-mounted
# onto a shared network cache, writing many small skill files directly onto it is slow
# (~100ms+ each, minutes total). git clone into a local staging dir (fast), then bulk-`mv` the
# finished tree onto cache_dir (one cross-fs copy). Per-PID so concurrent sandboxes don't clash.
tmp_dir="/tmp/awt-skill-cache-stage.$$"
cleanup() {{
  rm -rf "$tmp_dir"
  rmdir "$lock_dir" 2>/dev/null || true
}}
trap cleanup EXIT
if test -f "$cache_dir/.skill-cache-hash" && test "$(cat "$cache_dir/.skill-cache-hash")" = "$desired_hash"; then
  printf 'AGENT_RUNTIME_SKILL_CACHE_READY hash=%s cached=1\\n' "$desired_hash"
  exit 0
fi
# Each declared skill is a git descriptor (<repo>@<ref>#<path>). Clone each into a
# dot-prefixed staging subdir (so it is NOT itself picked up as a skill dir), copy the
# declared path (or the whole repo) to "$tmp_dir/<name>", then drop the clone (.git and
# the rest of the repo). Under `set -euo pipefail` any clone/test failure exits loud.
command -v git >/dev/null
rm -rf "$tmp_dir"
mkdir -p "$tmp_dir"
skills_table="$(printf %s {shlex.quote(skills_table_b64)} | base64 -d)"
while IFS="$(printf '\\037')" read -r skill_repo skill_ref skill_path skill_name skill_is_sha; do
  [ -n "$skill_repo" ] || continue
  clone_dir="$tmp_dir/.clone-$skill_name"
  rm -rf "$clone_dir"
  if [ -z "$skill_ref" ]; then
    git clone --depth 1 -- "$skill_repo" "$clone_dir"
  elif [ "$skill_is_sha" = "1" ]; then
    git clone -- "$skill_repo" "$clone_dir"
    git -C "$clone_dir" checkout --detach "$skill_ref"
  else
    git clone --depth 1 --branch "$skill_ref" -- "$skill_repo" "$clone_dir"
  fi
  if [ -n "$skill_path" ]; then
    skill_src="$clone_dir/$skill_path"
  else
    skill_src="$clone_dir"
  fi
  test -d "$skill_src"
  rm -rf "$tmp_dir/$skill_name"
  cp -a "$skill_src" "$tmp_dir/$skill_name"
  rm -rf "$clone_dir"
done <<SKILLS_EOF
$skills_table
SKILLS_EOF
printf '%s' "$desired_manifest" > "$tmp_dir/.skill-manifest"
printf '%s\\n' "$desired_hash" > "$tmp_dir/.skill-cache-hash"
chmod -R a+rX,go-w "$tmp_dir"
rm -rf "$cache_dir"
mv "$tmp_dir" "$cache_dir"
printf 'AGENT_RUNTIME_SKILL_CACHE_READY hash=%s cached=0\\n' "$desired_hash"
"""
    # No command env: each skill's git credential (if any) rides in its clone URL.
    result = await run_sandbox_command(
        run_fn,
        f"bash -lc {shlex.quote(script)}",
        envs=None,
        timeout_in_millis=_SKILL_CACHE_PREP_TIMEOUT_MS,
    )
    error = getattr(result, "error", None)
    output = _command_output(result)
    if error or "AGENT_RUNTIME_SKILL_CACHE_READY" not in output:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=(
                "failed to prepare agent runtime skill cache: "
                f"error={error or 'missing readiness marker'}; output={output[:2000]!r}"
            ),
            status_code=502,
        )


def _env_shape(env: dict[str, str] | None) -> str:
    """The environment as names and lengths — never values.

    A bootstrap that fails without saying anything leaves only its inputs to
    look at, and the interesting question about each is whether it is present,
    empty, or improbably large. The values themselves include a repository key.
    """
    items = sorted((env or {}).items())
    return "{" + ", ".join(f"{name}:{len(str(value))}" for name, value in items) + "}"


#: Which branch of :func:`run_sandbox_command` served the current task's most
#: recent call. The three differ in what they can deliver — env, timeout,
#: handlers — so a failure that does not name the branch leaves the reader
#: unable to tell a command that failed from one dispatched without its inputs.
#: Task-local storage matters because concurrent bootstraps must not report one
#: another's dispatch evidence.
_DISPATCH_BRANCH: contextvars.ContextVar[str] = contextvars.ContextVar(
    "conversation_bootstrap_dispatch_branch",
    default="<none recorded>",
)


def _last_dispatch_branch() -> str:
    return _DISPATCH_BRANCH.get()


def _command_output(result: Any, *, streamed: list[str] | None = None) -> str:
    """Everything the command said, and a statement when it said nothing.

    ``streamed`` is what the caller collected from the wire as it arrived; the
    accumulator is read as well so callers without stream handlers retain the
    command output. An empty capture is stated explicitly, keeping a silent
    command distinct from a platform that failed to capture its output.
    Separate captures have line boundaries so stderr cannot extend a structured
    readiness field at the end of stdout. Chunks within one capture stay intact.
    """
    logs = getattr(result, "logs", None)
    streams: list[str] = ["".join(streamed)] if streamed else []
    if logs is not None:
        for stream in ("stdout", "stderr"):
            parts: list[str] = []
            for item in getattr(logs, stream, []) or []:
                text = getattr(item, "text", None)
                parts.append(str(text if text is not None else item))
            if parts:
                streams.append("".join(parts))
    if not streams:
        streams.append("<no stdout or stderr captured from the sandbox command>")
    error = getattr(result, "error", None)
    if error:
        streams.append(str(error))
    return "\n".join(streams)


def _skill_manifest(skill_items: list[str]) -> str:
    return "\n".join(skill_items) + "\n"


def _parse_identity_ready_line(output: str) -> dict[str, int]:
    for line in str(output or "").splitlines():
        if not line.startswith("CONVERSATION_IDENTITY_READY "):
            continue
        parsed: dict[str, int] = {}
        for part in line.split()[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key in {"uid", "gid"}:
                try:
                    parsed[key] = int(value)
                except ValueError:
                    pass
        return parsed
    return {}


def _parse_bootstrap_ready_line(output: str) -> dict[str, Any]:
    for line in str(output or "").splitlines():
        if not line.startswith("CONVERSATION_BOOTSTRAP_READY "):
            continue
        parsed: dict[str, Any] = {}
        for part in line.split()[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key in {"uid", "gid"}:
                try:
                    parsed[key] = int(value)
                except ValueError:
                    pass
            elif key == "timings_b64":
                parsed[key] = value
        return parsed
    return {}


def _decode_bootstrap_json_b64(encoded: Any) -> Any:
    value = str(encoded or "").strip()
    if not value:
        return None
    compact = "".join(value.split())
    missing_padding = (-len(compact)) % 4
    if missing_padding:
        compact += "=" * missing_padding
    return json.loads(base64.b64decode(compact).decode("utf-8"))


def _parse_bootstrap_timings(encoded: Any) -> tuple[dict[str, int], str | None]:
    value = str(encoded or "").strip()
    if not value:
        return {}, None
    try:
        payload = _decode_bootstrap_json_b64(value)
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return {}, f"payload must be an object, got {type(payload).__name__}"
    timings: dict[str, int] = {}
    for key, raw in payload.items():
        try:
            value_int = int(raw)
        except (TypeError, ValueError):
            continue
        if value_int >= 0:
            timings[str(key)] = value_int
    return timings, None


def _encode_sidecar_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = zlib.compress(raw, level=9)
    if len(compressed) + 2 < len(raw):
        return "z:" + base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _dataplane_sandbox_candidate(sandbox: Any) -> Any:
    candidates: list[Any] = []
    if sandbox is not None:
        candidates.append(sandbox)
        underlying = getattr(sandbox, "sandbox", None)
        if underlying is not None and underlying is not sandbox:
            candidates.append(underlying)
    for candidate in candidates:
        # A wrapping sandbox backend exposes its dataplane handle on this attribute;
        # that candidate is the one to hand to build_dataplane. Backends that do not
        # wrap the sandbox object have none, so the top-level sandbox is used.
        if getattr(candidate, "dataplane_async_sandbox", None) is not None:
            return candidate
    return sandbox


async def _sidecar_endpoint(sandbox: Any, port: int = 8000) -> str:
    candidates: list[Any] = []
    if sandbox is not None:
        candidates.append(sandbox)
        underlying = getattr(sandbox, "sandbox", None)
        if underlying is not None and underlying is not sandbox:
            candidates.append(underlying)
    for candidate in candidates:
        get_endpoint = getattr(candidate, "get_endpoint", None)
        if not callable(get_endpoint):
            continue
        # Endpoint is a stable property of (sandbox, port); get_endpoint is a gateway
        # round-trip. Memoize on the candidate object (lifetime == sandbox) so the bootstrap
        # path doesn't re-pay it on every conversation borrow.
        cache = getattr(candidate, "_astrabox_endpoint_cache", None)
        cached = cache.get(int(port)) if isinstance(cache, dict) else None
        if cached:
            return cached
        try:
            endpoint_info = await get_endpoint(port)
        except Exception as exc:
            raise APIError(
                code="CONVERSATION_BOOTSTRAP_API_UNAVAILABLE",
                message=f"conversation bootstrap sidecar endpoint resolution failed: {exc}",
                status_code=502,
            ) from exc
        endpoint = str(getattr(endpoint_info, "endpoint", endpoint_info) or "").strip()
        if endpoint:
            if not isinstance(cache, dict):
                cache = {}
                with contextlib.suppress(Exception):
                    candidate._astrabox_endpoint_cache = cache
            cache[int(port)] = endpoint
            return endpoint
    raise APIError(
        code="CONVERSATION_BOOTSTRAP_API_UNAVAILABLE",
        message="conversation bootstrap requires resident sidecar endpoint",
        status_code=502,
    )


def _sandbox_run_fn(sandbox: Any) -> Any:
    candidates = []
    if sandbox is not None:
        candidates.append(sandbox)
        underlying = getattr(sandbox, "sandbox", None)
        if underlying is not None and underlying is not sandbox:
            candidates.append(underlying)
    for candidate in candidates:
        commands = getattr(candidate, "commands", None)
        run_fn = getattr(commands, "run", None) if commands is not None else None
        if callable(run_fn):
            return run_fn
    return None


async def run_sandbox_command(
    run_fn: Any,
    command: str,
    *,
    envs: dict[str, str] | None = None,
    timeout_in_millis: int | None = None,
    handlers: Any = None,
) -> Any:
    """Single exec chokepoint: deliver ``envs``/``timeout`` to whichever run-command
    convention the resolved sandbox adapter exposes — never silently drop env.

    The two backends disagree on how a command's environment is passed:
      - one backend: ``sandbox.commands.run`` is the sandbox SDK ``CommandsAdapter.run``,
        which accepts only ``opts=RunCommandOpts(envs=, timeout=timedelta)`` and rejects
        ``envs=`` with ``TypeError``.
      - the other backend: a wrapper adapter's ``run`` accepts ``envs=``/``timeout_in_millis=``
        natively and has a ``**_`` that would silently swallow an ``opts=`` kwarg.

    Dispatch on the run callable's parameter names (the same introspection
    ``terminal_service._supports_handlers`` uses), never ``try/except TypeError``:
    the wrapper's ``**_`` makes a wrong-form call succeed while dropping env, so only the name
    check distinguishes "accepts opts" from "swallows opts". When the adapter exposes
    neither convention, deliver env by inlining ``env K=V … command`` — genuine
    delivery on any shell runner, never a silent drop."""
    if not callable(run_fn):
        raise APIError(
            code="AGENT_RUNTIME_SANDBOX_EXEC_UNSUPPORTED",
            message="sandbox command runner is unavailable",
            status_code=502,
        )
    clean_envs = {k: v for k, v in (envs or {}).items() if v is not None} or None
    try:
        params = inspect.signature(run_fn).parameters
    except (TypeError, ValueError):
        params = {}

    _DISPATCH_BRANCH.set(
        "opts"
        if "opts" in params
        else "envs"
        if "envs" in params
        else "inline-env"
        if clean_envs is not None
        else "bare"
    )
    if "opts" in params:  # one backend (SDK CommandsAdapter)
        opts_kwargs: dict[str, Any] = {}
        if clean_envs is not None:
            opts_kwargs["envs"] = clean_envs
        if timeout_in_millis is not None:
            opts_kwargs["timeout"] = timedelta(milliseconds=int(timeout_in_millis))
        opts = RunCommandOpts(**opts_kwargs) if opts_kwargs else None
        # Handlers only where the adapter declares them: a wrapper with `**_`
        # would swallow the kwarg and lose the stream silently, which is the
        # failure this exists to prevent.
        if handlers is not None and "handlers" in params:
            return await run_fn(command, opts=opts, handlers=handlers)
        return await run_fn(command, opts=opts)

    if "envs" in params:  # the other backend (wrapper adapter)
        call_kwargs: dict[str, Any] = {}
        if clean_envs is not None:
            call_kwargs["envs"] = clean_envs
        if timeout_in_millis is not None:
            call_kwargs["timeout_in_millis"] = int(timeout_in_millis)
        return await run_fn(command, **call_kwargs)

    if clean_envs is not None:  # no env kwarg → inline into the command (still delivers env)
        env_args = " ".join(shlex.quote(f"{k}={v}") for k, v in clean_envs.items())
        return await run_fn(f"env {env_args} {command}")
    return await run_fn(command)  # nothing to deliver; call bare regardless of convention


def conversation_identity_from_plan(
    runtime_identity: dict[str, Any] | None, workspace_plan: Any
) -> Any:
    """The conversation's identity, but only when its environment allows sharing.

    Two gates, and both are needed. This one is intent: an operator with a pool
    whose boxes could carry several conversations can still want a box each —
    untrusted tenants are the obvious case — so capability alone must never
    pack them. The environment expresses it by choosing a profile whose tenancy
    is ``shared_agent``; every other profile passes no conversation identity.
    The second gate is the backend's, which asks a box what it can actually do.

    That profile is also where the per-conversation home comes from, and the two
    are one decision rather than two: the per-session profile's home is a fixed
    path, so conversations sharing a box under it would chown the same directory
    out from under each other.

    Both halves or neither: an agent with no home, or a home with no agent,
    could not place a conversation anywhere sensible, and half an identity looks
    like an answer.
    """
    from astrabox.seams.sandbox import ConversationIdentity

    if str((runtime_identity or {}).get("sandbox_tenancy") or "").strip() != "agent":
        return None
    agent_id = str(
        getattr(workspace_plan, "agent_id", "")
        or (runtime_identity or {}).get("agent_id")
        or ""
    ).strip()
    home_dir = str((runtime_identity or {}).get("home_dir") or "").strip()
    workspace_dir = str((runtime_identity or {}).get("workspace_dir") or "").strip()
    workspace_source_dir = str(
        (runtime_identity or {}).get("workspace_source_dir") or ""
    ).strip()
    if not agent_id or not home_dir or not workspace_dir or not workspace_source_dir:
        return None
    return ConversationIdentity(
        agent_id=agent_id,
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        workspace_source_dir=workspace_source_dir,
        linux_user=str((runtime_identity or {}).get("linux_user") or "").strip(),
        config_dir=str((runtime_identity or {}).get("config_dir") or "").strip(),
        cache_dir=str((runtime_identity or {}).get("cache_dir") or "").strip(),
        temp_dir=str((runtime_identity or {}).get("temp_dir") or "").strip(),
        uid=(
            int((runtime_identity or {}).get("uid"))
            if (runtime_identity or {}).get("uid") is not None
            else None
        ),
        gid=(
            int((runtime_identity or {}).get("gid"))
            if (runtime_identity or {}).get("gid") is not None
            else None
        ),
        terminal_isolated_session_id=str(
            (runtime_identity or {}).get("terminal_isolated_session_id") or ""
        ).strip()
        or None,
    )
