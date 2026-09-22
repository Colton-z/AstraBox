"""Default-repo bootstrap for a resolved Agent's conversation workspace.

A thin wrapper over the git-clone engine; idempotent via a ``.git``-exists probe.
"""

from __future__ import annotations

import shlex
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.secrets import SecretProvider
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    command_for_identity,
    identity_path_to_source,
    normalize_runtime_identity,
)

from astrabox.core.service.orchestrator.runtime.storage._command_result import (
    _extract_command_output_text,
)
from astrabox.core.service.orchestrator.runtime.storage._git_clone import (
    _clone_git_repo_in_sandbox,
    _normalize_deploy_private_key,
    _resolve_git_https_token,
)
from astrabox.core.service.orchestrator.runtime.storage._scope import _template_default_repo

logger = get_logger(__name__)


def _build_default_repo_bootstrap_payload(
    template: Any,
    *,
    target_cwd: str,
    session_id: str,
    use_https_git: bool = False,
) -> dict[str, str] | None:
    import base64

    repo = _template_default_repo(template)
    if not repo:
        return None
    if not isinstance(repo, dict):
        raise APIError(
            code="DEFAULT_REPO_INVALID",
            message=f"default_repo must be a dict, got {type(repo).__name__}",
            status_code=500,
        )

    url = str(repo.get("url") or "").strip()
    if not url:
        return None

    protocol = str(repo.get("protocol") or "ssh").strip().lower()
    if protocol != "ssh":
        raise APIError(
            code="DEFAULT_REPO_UNSUPPORTED_PROTOCOL",
            message=f"default_repo protocol={protocol!r} not supported (v1 supports only 'ssh')",
            status_code=500,
        )
    if not url.startswith("git@"):
        raise APIError(
            code="DEFAULT_REPO_INVALID",
            message=f"default_repo.url={url!r} must be an SSH URL (git@host:group/repo.git)",
            status_code=500,
        )

    target = str(target_cwd or "").strip()
    if not target:
        raise APIError(
            code="DEFAULT_REPO_CLONE_FAILED",
            message="cannot clone default_repo: empty target_cwd",
            status_code=500,
        )

    secret_name = str(repo.get("deploy_key_secret_name") or "").strip()
    if not secret_name and not use_https_git:
        raise APIError(
            code="DEFAULT_REPO_MISSING_KEY",
            message="default_repo.deploy_key_secret_name is required for ssh protocol",
            status_code=500,
        )

    payload: dict[str, str] = {
        "url": url,
        "target": target,
    }
    if secret_name:
        private_key = SecretProvider.get_secret(secret_name)
        if not private_key:
            raise APIError(
                code="DEFAULT_REPO_MISSING_KEY",
                message=f"failed to resolve deploy key from secret_name={secret_name!r}",
                status_code=500,
            )
        private_key = _normalize_deploy_private_key(private_key, secret_name=secret_name)
        payload["key_b64"] = base64.b64encode(private_key.encode("utf-8")).decode("ascii")
    if use_https_git:
        payload["https_token"] = _resolve_git_https_token()
    branch = str(repo.get("branch") or "").strip()
    if branch:
        payload["branch"] = branch
    depth = repo.get("depth")
    if isinstance(depth, int) and depth > 0:
        payload["depth"] = str(depth)
    logger.info(
        "default_repo bootstrap prepared: session=%s url=%s branch=%s depth=%s target=%s",
        session_id,
        url,
        branch or "<default>",
        depth if isinstance(depth, int) and depth > 0 else "<full>",
        target,
    )
    return payload


async def clone_default_repo(
    sandbox: Any,
    template: Any,
    target_cwd: str,
    session_id: str,
    *,
    get_underlying_sandbox_fn: Any,
    runtime_identity: dict[str, Any] | None = None,
) -> None:
    """Clone the resolved Agent's default repository via an SSH deploy key.

    Idempotent: if {target_cwd}/.git exists, skip. Failures raise APIError.
    StrictHostKeyChecking is disabled for this clone.
    """
    repo = _template_default_repo(template)
    if not repo:
        return
    if not isinstance(repo, dict):
        raise APIError(
            code="DEFAULT_REPO_INVALID",
            message=f"default_repo must be a dict, got {type(repo).__name__}",
            status_code=500,
        )

    url = str(repo.get("url") or "").strip()
    if not url:
        return

    protocol = str(repo.get("protocol") or "ssh").strip().lower()
    if protocol != "ssh":
        raise APIError(
            code="DEFAULT_REPO_UNSUPPORTED_PROTOCOL",
            message=f"default_repo protocol={protocol!r} not supported (v1 supports only 'ssh')",
            status_code=500,
        )
    if not url.startswith("git@"):
        raise APIError(
            code="DEFAULT_REPO_INVALID",
            message=f"default_repo.url={url!r} must be an SSH URL (git@host:group/repo.git)",
            status_code=500,
        )

    underlying = get_underlying_sandbox_fn(sandbox)
    if underlying is None:
        raise APIError(
            code="DEFAULT_REPO_CLONE_FAILED",
            message=f"cannot clone default_repo: no underlying sandbox session={session_id}",
            status_code=502,
        )

    target = str(target_cwd or "").strip()
    if not target:
        raise APIError(
            code="DEFAULT_REPO_CLONE_FAILED",
            message="cannot clone default_repo: empty target_cwd",
            status_code=500,
        )

    identity = normalize_runtime_identity(runtime_identity)
    target = identity_path_to_source(identity, target)
    quoted_target = shlex.quote(target)
    probe_cmd = f"test -d {quoted_target}/.git && echo EXISTS || echo MISSING"
    probe = await underlying.commands.run(command_for_identity(probe_cmd, identity))
    probe_output = _extract_command_output_text(probe)
    if "EXISTS" in probe_output:
        logger.info("default_repo already cloned, skip session=%s target=%s", session_id, target)
        return

    branch = str(repo.get("branch") or "").strip()
    depth = repo.get("depth")

    secret_name = str(repo.get("deploy_key_secret_name") or "").strip()
    await _clone_git_repo_in_sandbox(
        underlying,
        ssh_url=url,
        target=target,
        branch=branch,
        depth=depth,
        deploy_key_secret_name=secret_name or None,
        identity=identity,
        error_code="DEFAULT_REPO_CLONE_FAILED",
        label="default_repo",
        # default_repo lands in the conversation cwd, which on some backends is a remote mount.
        # Stage in local /tmp + bulk-parallel-copy so the slow remote checkout writes
        # don't blow the exec timeout (no-op on a local backend path).
        stage_via_tmp=True,
    )
    logger.info(
        "default_repo cloned: session=%s url=%s branch=%s depth=%s target=%s",
        session_id,
        url,
        branch or "<default>",
        depth if isinstance(depth, int) and depth > 0 else "<full>",
        target,
    )
