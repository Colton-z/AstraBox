"""NAS mount orchestration for Assistant workspaces.

Idempotent mount of the unified network-storage root (``/mnt/nas``) plus a per-subject
subdirectory bind + chown + verify. Owns the ``MountNASRequest`` None sentinel — a
backend needing it supplies the real class.
"""

from __future__ import annotations

import contextlib
import shlex
from typing import Any

# The sandbox SDK is an optional dependency. The network-mount machinery in this
# module is only used by a backend that talks to a remote sandbox service:
# a backend that binds the workspace as a local volume never calls the network
# mount paths below. This stays a None sentinel so the
# module imports clean; the functions that reference it fail loud at call time
# (None(...) -> TypeError) if a backend that needs it doesn't supply the real class.
MountNASRequest = None
from opensandbox.models.filesystem import WriteEntry

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.runtime.storage._command_result import (
    _ensure_command_success,
    _extract_command_log_text,
    _extract_command_output_text,
)
from astrabox.core.service.orchestrator.runtime.storage._scope import (
    NAS_ROOT_MOUNT,
    _safe_nas_segment,
)

logger = get_logger(__name__)


_ASSISTANT_WORKSPACE_STORAGE_SCRIPT_PATH = (
    "/usr/local/bin/astrabox-assistant-workspace-storage"
)


async def ensure_nas_root_mounted(
    underlying: Any,
    *,
    endpoint: str,
    root_mount: str = NAS_ROOT_MOUNT,
) -> None:
    """Idempotently mount the unified network-storage root (``:/``) once per sandbox.

    The whole storage tree is reachable under this single mount, so per-conversation
    storage is just a subdirectory of it plus a cheap local bind — never a second
    mount, and never the mount-root / mkdir / umount / remount-subdir dance.

    Idempotent via the mountpoint probe, so it is safe to call on a sandbox that
    already has the root mounted.
    """
    probe = await underlying.commands.run(
        f"mountpoint -q -- {shlex.quote(root_mount)} && echo MOUNTED || echo NOT"
    )
    probe_text = _extract_command_log_text(probe) or str(getattr(probe, "output", "") or "")
    if "MOUNTED" in probe_text:
        return
    mkdir_result = await underlying.commands.run(f"mkdir -p {shlex.quote(root_mount)}")
    _ensure_command_success(
        mkdir_result, "NAS_MOUNT_FAILED", f"NAS root mountpoint mkdir failed path={root_mount}"
    )
    await underlying.mount(
        MountNASRequest(mount_point=root_mount, nas_path="/", endpoint=endpoint)
    )


async def mount_assistant_workspace_storage(
    sandbox: Any,
    *,
    user_id: str,
    assistant_id: str,
    engine_kind: str,
    settings: Any,
    get_underlying_sandbox_fn: Any,
) -> None:
    """Mount the shared persistent home root for an assistant workspace.

    Layout for the shared-sandbox Assistant design:
    ``<nas_base>/assistant`` is mounted once at ``/home/conversations``.
    Per-user assistant homes then live under
    ``/home/conversations/<user_id>/<assistant_id>`` and are owned by their
    workload Linux user. Mounting a single assistant subtree would hide other
    users in a shared sandbox, so this helper deliberately mounts the common
    assistant root.
    """
    if not settings.nas_enabled or not settings.nas_endpoint:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                "assistant workspace storage requires NAS configuration "
                f"user={user_id} assistant={assistant_id}"
            ),
            status_code=502,
        )
    from astrabox.core.service.orchestrator.engine.capabilities import (
        engine_allowed_for_session_kind,
    )

    if not engine_allowed_for_session_kind(engine_kind, "assistant_chat"):
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                f"engine_kind={engine_kind!r} does not support assistant workspaces "
                f"user={user_id} assistant={assistant_id}"
            ),
            status_code=500,
        )

    underlying = get_underlying_sandbox_fn(sandbox)
    if underlying is None:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                "assistant workspace mount requires underlying sandbox "
                f"user={user_id} assistant={assistant_id}"
            ),
            status_code=502,
        )

    # Keyed by assistant_id only (no user_id in path; user↔assistant link is DB-only).
    _ = user_id
    base = settings.nas_base_path.rstrip("/")
    endpoint = settings.nas_endpoint
    safe_assistant_id = _safe_nas_segment(assistant_id, label="assistant_id")
    nas_root = f"{base}/assistants"
    assistant_profile_root = f"{nas_root}/{safe_assistant_id}"
    local_root = "/home/conversations"
    # Lazy import: this module loads during runtime.storage bootstrap, before
    # the engine package's heavy adapter modules — a top-level import would
    # cycle (runtime.storage -> engine -> claude_code_runtime -> workspace ->
    # runtime.storage).
    from astrabox.core.service.orchestrator.engine.capabilities import (
        capabilities_for_engine_kind,
    )

    # The engine's configuration directory is independent of sandbox tenancy.
    engine_config_dir = capabilities_for_engine_kind(engine_kind).workload.config_dir_name
    profile_local_root = f"{local_root}/{safe_assistant_id}"

    already_mounted = False
    with contextlib.suppress(Exception):
        probe = await _run_assistant_workspace_storage_script(
            underlying,
            "probe",
            {
                "ASTRABOX_STORAGE_LOCAL_ROOT": local_root,
            },
        )
        probe_text = _extract_command_log_text(probe) or str(getattr(probe, "output", "") or "")
        already_mounted = any(
            line.startswith("ASSISTANT_WORKSPACE_STORAGE_MOUNTED ")
            for line in probe_text.splitlines()
        )

    try:
        if not already_mounted:
            await underlying.mount(MountNASRequest(
                mount_point="/mnt/nas_init",
                nas_path="/",
                endpoint=endpoint,
            ))
            init_paths = [
                f"/mnt/nas_init{nas_root}",
                f"/mnt/nas_init{assistant_profile_root}",
                f"/mnt/nas_init{assistant_profile_root}/workspace",
            ]
            if engine_config_dir:
                init_paths.append(
                    f"/mnt/nas_init{assistant_profile_root}/{engine_config_dir}"
                )
            await _create_sandbox_directories_via_files(
                underlying,
                init_paths,
                error_message=(
                    "assistant workspace NAS init-dir mkdir failed "
                    f"user={user_id} assistant={assistant_id}"
                ),
            )
            await underlying.umount("/mnt/nas_init")
    except Exception as exc:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=(
                f"assistant workspace NAS init-dir setup failed "
                f"user={user_id} assistant={assistant_id}: {exc}"
            ),
            status_code=502,
        ) from exc

    if not already_mounted:
        await _create_sandbox_directories_via_files(
            underlying,
            [local_root],
            error_message=(
                f"assistant workspace local mount mkdir failed path={local_root} "
                f"user={user_id} assistant={assistant_id}"
            ),
        )
        await underlying.mount(MountNASRequest(mount_point=local_root, nas_path=nas_root, endpoint=endpoint))

    profile_paths = [profile_local_root, f"{profile_local_root}/workspace"]
    if engine_config_dir:
        profile_paths.append(f"{profile_local_root}/{engine_config_dir}")
    await _create_sandbox_directories_via_files(
        underlying,
        profile_paths,
        error_message=(
            f"assistant workspace profile mkdir failed path={profile_local_root} "
            f"user={user_id} assistant={assistant_id}"
        ),
    )
    verify_result = await _run_assistant_workspace_storage_script(
        underlying,
        "verify",
        {
            "ASTRABOX_STORAGE_LOCAL_ROOT": local_root,
            "ASTRABOX_STORAGE_PROFILE_ROOT": profile_local_root,
            "ASTRABOX_STORAGE_ENGINE_CONFIG_DIR": engine_config_dir or "",
        },
    )
    _ensure_command_success(
        verify_result,
        "NAS_MOUNT_FAILED",
        (
            f"assistant workspace NAS root verification failed path={local_root} "
            f"user={user_id} assistant={assistant_id}"
        ),
    )
    logger.info(
        "assistant workspace NAS mounted: %s -> %s user=%s assistant=%s engine=%s",
        nas_root,
        local_root,
        user_id,
        assistant_id,
        engine_kind,
    )


async def _run_assistant_workspace_storage_script(
    sandbox: Any,
    operation: str,
    env: dict[str, str],
) -> Any:
    commands = getattr(sandbox, "commands", None)
    run_fn = getattr(commands, "run", None) if commands is not None else None
    if not callable(run_fn):
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message="assistant workspace storage requires sandbox command runner",
            status_code=502,
        )
    env_args = " ".join(
        shlex.quote(f"{key}={value}")
        for key, value in sorted(env.items())
    )
    command = (
        f"env {env_args} "
        f"bash {shlex.quote(_ASSISTANT_WORKSPACE_STORAGE_SCRIPT_PATH)} "
        f"{shlex.quote(operation)}"
    )
    return await run_fn(command)


async def _create_sandbox_directories_via_files(
    sandbox: Any,
    paths: list[str],
    *,
    error_message: str,
) -> None:
    if not paths:
        return
    files_api = _sandbox_files_api(sandbox)
    create_directories = getattr(files_api, "create_directories", None)
    if not callable(create_directories):
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=f"{error_message}: sandbox files create_directories API is unavailable",
            status_code=502,
        )
    entries = [
        WriteEntry(
            path=str(path),
            data=None,
            mode=755,
            owner=None,
            group=None,
            encoding="utf-8",
        )
        for path in paths
    ]
    try:
        await create_directories(entries)
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            code="NAS_MOUNT_FAILED",
            message=f"{error_message}: {exc}",
            status_code=502,
        ) from exc


def _sandbox_files_api(sandbox: Any) -> Any:
    candidates = []
    if sandbox is not None:
        candidates.append(sandbox)
        underlying = getattr(sandbox, "sandbox", None)
        if underlying is not None and underlying is not sandbox:
            candidates.insert(0, underlying)
    for candidate in candidates:
        files_api = getattr(candidate, "files", None)
        if files_api is not None:
            return files_api
    return None
