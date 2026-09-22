"""Git-clone strategy engine: SSH-deploy-key vs HTTPS-token cloning, by backend.

Chooses SSH vs HTTPS per backend (``sandbox_for_sandbox(underlying).requires_https_git``),
retries a transient egress 403 with linear backoff, and stages a remote-mount clone through
sandbox-local /tmp when needed. ``_git_clone_with_askpass_command`` has no callers.
``_normalize_deploy_private_key`` is defined here and imported by its
underscore-prefixed name from ``engine/hermes.py`` and ``storage/_default_repo.py``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import uuid
import shlex
from typing import Any, Literal, overload
from urllib.parse import quote, urlsplit, urlunsplit

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.common.utils.secrets import SecretProvider
from astrabox.common.utils.settings import load_astrabox_settings
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    command_for_identity,
    run_sandbox_command,
)
from astrabox.seams.sandbox import sandbox_for_sandbox

from astrabox.core.service.orchestrator.runtime.storage._command_result import (
    _extract_command_stream_text,
)

logger = get_logger(__name__)


_OPENSSH_PRIVATE_KEY_BEGIN = "-----BEGIN OPENSSH PRIVATE KEY-----"
_OPENSSH_PRIVATE_KEY_END = "-----END OPENSSH PRIVATE KEY-----"


# ---- git clone auth strategy (ssh deploy key vs https access token) ----
#
# Repos are configured as SSH (git@host:group/repo.git) + per-repo deploy key.
# That works on SSH-capable backends, but some backends' egress only carries
# http/https on 80/443 — SSH/22 can't leave the box. So on a backend that
# can't do SSH, the clone automatically falls back to the same repo over HTTPS
# using a Git host access token (read_repository). The deploy key is SSH-only
# and cannot authenticate HTTPS, hence a separate token. SSH backends are unchanged.


# Fresh-sandbox egress-registration race (egress-restricted backends): total clone
# attempts and per-retry backoff (multiplied by the attempt index), giving headroom
# before failing loud.
_HTTPS_CLONE_MAX_ATTEMPTS = 5
_HTTPS_CLONE_RETRY_BACKOFF_SECONDS = 2

# Cloning straight into a remote network-mounted workspace is pathologically slow:
# git's checkout does rename+lstat+write ≈ 3 remote writes per file (~310ms each), so
# even a ~200-file repo blows past the 60s exec timeout. Clone into local /tmp instead,
# then bulk-copy the tree to the remote target with high concurrency so the writes
# overlap. This timeout covers that bulk copy.
_REMOTE_BULK_COPY_TIMEOUT_MS = 300_000


def _remote_staging_copy_command(src: str, dst: str) -> str:
    """High-concurrency copy of a local staging tree into a remote-mounted target.

    Mirrors the conversation bootstrap script's default_repo copy: walk ``src``,
    recreate the dirs, then copy files through a 128-way thread pool so the slow
    per-file remote writes overlap.

    Uses ``shutil.copy`` (copyfile + copymode), not ``copyfile``: the staging clone
    is a git checkout that honours each file's committed mode, so plugin/repo scripts
    committed ``100755`` are executable in staging. ``copyfile`` copies bytes only and
    drops to ``0644`` — leaving the non-root conversation runtime with un-runnable,
    un-``chmod``-able (root-owned cache) plugin executables. ``copy`` carries the
    executable bit through; it skips ``copystat``'s utime/xattr (unreliable on some remote filesystems).

    Each file is copied beside its destination and then renamed onto it, because
    the destination is a cache several conversations of one Agent read while one
    of them is writing it. A direct copy is visible while it is still partial,
    and a reader that opened a declaration mid-write got `invalid JSON:
    Expecting ',' delimiter`. `os.replace` is atomic on the same filesystem, so
    a reader sees the whole old file or the whole new one.
    """
    py = (
        "import os,sys,shutil,uuid\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        "s,d=sys.argv[1],sys.argv[2]\n"
        "dirs=[];files=[]\n"
        "for r,_,fs in os.walk(s):\n"
        "    rel=os.path.relpath(r,s); dd=os.path.join(d,rel) if rel!='.' else d\n"
        "    dirs.append(dd)\n"
        "    files.extend((os.path.join(r,f),os.path.join(dd,f)) for f in fs)\n"
        "os.makedirs(d,exist_ok=True)\n"
        "def publish(pair):\n"
        "    src,dst=pair\n"
        "    tmp=dst+'.part-'+uuid.uuid4().hex[:8]\n"
        "    try:\n"
        "        shutil.copy(src,tmp); os.replace(tmp,dst)\n"
        "    finally:\n"
        "        os.path.exists(tmp) and os.remove(tmp)\n"
        "with ThreadPoolExecutor(128) as ex:\n"
        "    list(ex.map(lambda p: os.makedirs(p,exist_ok=True), sorted(dirs)))\n"
        "    list(ex.map(publish, files))\n"
        "print('STAGING_COPY_OK files=%d' % len(files))\n"
    )
    return f"python3 - {shlex.quote(src)} {shlex.quote(dst)} <<'PYCOPY'\n{py}PYCOPY"


_CLONE_STAGE_ROOT = "/tmp/clone-stage"


def _clone_staging_dir(target: str) -> str:
    """A sandbox-local /tmp staging dir private to ONE clone attempt.

    Unique, not derived from the target alone. Several conversations of one
    Agent share a box and share that Agent's cache target, so a name built only
    from the target is the same name for all of them — and the cleanup that
    follows a finished copy runs outside the clone's lock. One conversation
    then deletes the directory another is cloning into, and git fails writing a
    `.git/config` whose parent has just disappeared.

    The target keeps its deterministic name and its lock; only the scratch area
    is made private, which is what a scratch area is.
    """

    slug = re.sub(r"[^A-Za-z0-9]+", "_", target).strip("_") or "repo"
    return f"{_CLONE_STAGE_ROOT}/{slug}-{uuid.uuid4().hex[:12]}"


def _underlying_requires_https_git(underlying: Any) -> bool:
    """True when the box cannot reach git over SSH (backend egress is http/https)."""
    provider = sandbox_for_sandbox(underlying)
    return bool(provider and provider.requires_https_git)


def _clone_lock_path(target: str) -> str:
    """Stable per-target clone lockfile under /tmp (survives cache rebuilds)."""
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    return f"/tmp/awtx-clone-{digest}.lock"


def _clone_then_checkout(
    clone_core: str,
    clone_into: str,
    sha: str | None,
    *,
    depth: Any = None,
    ssh_command: str | None = None,
) -> str:
    """The clone, and the revision pin that belongs with it.

    One command, so both run under the clone's lock and against the same
    directory. Fetch the pin explicitly: a shallow branch clone need not contain
    it. Split apart, the checkout lands on the shared target after the
    tree has been published and rewrites files a reader may already open.
    """

    pinned = str(sha or "").strip()
    if not pinned:
        return clone_core
    fetch_args = ["git", "-C", clone_into, "fetch"]
    if isinstance(depth, int) and depth > 0:
        fetch_args += ["--depth", str(depth)]
    fetch_args += ["origin", pinned]
    fetch = " ".join(shlex.quote(arg) for arg in fetch_args)
    if ssh_command:
        fetch = f"GIT_SSH_COMMAND={shlex.quote(ssh_command)} {fetch}"
    return (
        f"{clone_core} && {fetch} && git -C {shlex.quote(clone_into)} "
        f"checkout --detach {shlex.quote(pinned)}"
    )


def _guarded_clone_cmd(target: str, clone_core: str, *, clean: str | None = None) -> str:
    """Serialize + clean a clone bound for ``target``.

    ``( flock 9; rm -rf clean; <clone> ) 9>lockfile`` — the rm and the clone run under
    one exclusive lock keyed on the TARGET, so a concurrent sibling clone (a
    fault-injection runtime-start retry, or another conversation of the same Agent
    sharing that Agent's cache) waits instead of colliding, and each attempt starts
    from a clean destination. The fd-redirect form avoids nested ``-c`` quoting.

    The lock belongs to the target and not to whatever is being written first,
    because the target is the shared thing: several conversations reach the same
    Agent cache from private staging directories, and only the target can name what
    they are contending for.
    """
    scratch = target if clean is None else clean
    return (
        f"( flock 9 || exit 1; rm -rf -- {shlex.quote(scratch)}; {clone_core} ) "
        f"9>{shlex.quote(_clone_lock_path(target))}"
    )


async def _clone_git_repo_in_sandbox(
    underlying: Any,
    *,
    ssh_url: str,
    protocol: str = "ssh",
    target: str,
    branch: str,
    depth: Any,
    deploy_key_secret_name: str | None,
    identity: dict[str, Any] | None,
    error_code: str,
    label: str,
    ssh_key_dir: str | None = None,
    ssh_key_path: str | None = None,
    stage_via_tmp: bool = False,
    sha: str | None = None,
) -> None:
    """Clone a git repo inside a sandbox, adapting source protocol and backend.

    A declared HTTPS repository is cloned over HTTPS on every backend. Public
    repositories need no credential; when an operator configured the shared Git
    HTTPS token it is added without exposing it in logs. A declared SSH source
    keeps using its per-repository deploy key, except on HTTP-only backends where
    it is translated to HTTPS and therefore requires the configured token.

    ``sha`` pins the checkout, and it is done HERE rather than by the caller
    afterwards. A caller's ``git -C target checkout`` rewrites the working tree
    of a directory other conversations of the same Agent are reading, outside
    any lock and one file at a time — a sibling scanning the cache then opens a
    declaration mid-rewrite. Done here it runs against the private staging tree
    and under the clone's lock, so the shared target only ever receives a tree
    already at the right revision.

    ``stage_via_tmp`` (remote-mount backends only): clone into sandbox-local ``/tmp`` then bulk
    parallel-copy the tree to ``target``. Use it when ``target`` is a remote-mounted
    path — a direct checkout there does ~3 slow writes per file and blows the
    exec timeout; staging makes the checkout local and overlaps the writes.
    """
    # A clone must own a fresh destination and tolerate a concurrent sibling
    # clone. On a reused sandbox the target can already exist from a prior
    # checkout, and a fault-injection runtime-start *retry* can run this clone
    # concurrently with the original attempt — both then rm/clone the same path
    # and `git clone` hard-fails "destination already exists and is not empty",
    # which surfaces as AGENT_RUNTIME_PLUGIN_CACHE_FAILED. The clone command
    # itself therefore rm's the target then clones, both held under a per-target
    # ``flock`` (see _guarded_clone_cmd), so concurrent attempts serialize and
    # each starts from a clean target. The lockfile lives in /tmp (stable across
    # cache rebuilds) keyed by the target path.
    #
    # The per-conversation staging leaf lives under a shared parent
    # (``_CLONE_STAGE_ROOT``). On SSH-capable backends the clone runs as the
    # non-root conversation user (``command_for_identity``); if that parent was
    # created earlier by root or a different conversation user (mode 0755), the
    # runuser cannot create its own leaf there and git fails "could not create
    # work tree dir: Permission denied" (the failure depends on which user
    # happened to create the shared dir first — so it surfaces intermittently
    # across sandboxes). Make the shared parent behave like ``/tmp`` itself —
    # sticky, world-writable (1777) — so every user creates and owns its own
    # unique leaf. Run as root (default identity), idempotent, before either
    # branch.
    if stage_via_tmp:
        await underlying.commands.run(
            f"mkdir -p {shlex.quote(_CLONE_STAGE_ROOT)} "
            f"&& chmod 1777 {shlex.quote(_CLONE_STAGE_ROOT)}"
        )
    declared_protocol = str(protocol or "ssh").strip().lower()
    if declared_protocol not in {"ssh", "https"}:
        raise APIError(
            code=error_code,
            message=f"{label}.protocol={declared_protocol!r} is not supported",
            status_code=500,
        )
    clone_over_https = declared_protocol == "https" or _underlying_requires_https_git(underlying)
    if clone_over_https:
        # An explicitly-HTTPS public repository is valid without a token. The
        # SSH-to-HTTPS fallback is used for repositories configured around a
        # deploy key, so it still requires the operator's HTTPS credential.
        https_token = (
            _resolve_git_https_token(required=False)
            if declared_protocol == "https"
            else _resolve_git_https_token()
        )
        # Token embedded in the URL (https://git:<token>@host/path) + plain clone,
        # run as root (not via command_for_identity's runuser); the tree is handed to
        # the conversation user via chown below.
        #
        # The backend's per-uid egress on a freshly-created sandbox intermittently 403s in
        # the first seconds of its life — an egress-registration race, not a missing
        # allow-list (the same token-in-URL clone succeeds moments later, and a
        # concurrent sibling sandbox cloning the same repo succeeds). Root is more
        # reliable here than a non-root runuser since the token is in the URL
        # (uid-independent), but not immune, so cloning runs as root and retries the
        # identical clone on a transient 403, cleaning the partial checkout between
        # attempts and failing loud after the attempt bound.
        # Token is redacted in logs/errors via _redact_https_clone_url.
        clone_url = (
            _https_git_clone_url(ssh_url, https_token)
            if declared_protocol == "https"
            else _git_https_clone_url(ssh_url, https_token)
        )
        log_url = _redact_https_clone_url(clone_url)
        secret_for_redact: str | None = https_token
        # When target is a remote mount, clone into local /tmp first (fast, reliable),
        # then bulk-parallel-copy to target below. Otherwise clone straight in.
        clone_into = _clone_staging_dir(target) if stage_via_tmp else target
        clone_cmd = _guarded_clone_cmd(
            target,
            _clone_then_checkout(
                _git_clone_command(clone_url, clone_into, branch, depth),
                clone_into,
                sha,
                depth=depth,
            ),
            clean=clone_into,
        )
        clone_result = await underlying.commands.run(clone_cmd)
        for attempt in range(1, _HTTPS_CLONE_MAX_ATTEMPTS):
            clone_err_probe = getattr(clone_result, "error", None)
            if not clone_err_probe:
                break
            stderr_probe = _extract_command_stream_text(clone_result, "stderr")
            if "403" not in (str(clone_err_probe) + stderr_probe):
                break  # non-403 error, not the transient egress race — let it fail loud
            logger.warning(
                "%s clone hit transient egress 403, retrying (attempt %d/%d) target=%s",
                label, attempt, _HTTPS_CLONE_MAX_ATTEMPTS, clone_into,
            )
            await underlying.commands.run(f"rm -rf {shlex.quote(clone_into)}")
            await asyncio.sleep(_HTTPS_CLONE_RETRY_BACKOFF_SECONDS * attempt)
            clone_result = await underlying.commands.run(clone_cmd)
        if stage_via_tmp and not getattr(clone_result, "error", None):
            # Local clone succeeded — bulk-parallel-copy the tree onto the remote target,
            # then drop the staging dir. The copy result becomes clone_result so the
            # shared error check below validates the remote-side write.
            logger.info(
                "%s staged clone done, bulk-copying to remote target=%s", label, target,
            )
            copy_result = await run_sandbox_command(
                underlying.commands.run,
                _remote_staging_copy_command(clone_into, target),
                timeout_in_millis=_REMOTE_BULK_COPY_TIMEOUT_MS,
            )
            await underlying.commands.run(f"rm -rf {shlex.quote(clone_into)}")
            clone_result = copy_result
    else:
        if not deploy_key_secret_name:
            raise APIError(
                code=error_code,
                message=f"{label}.deploy_key_secret_name is required for ssh protocol",
                status_code=500,
            )
        private_key = SecretProvider.get_secret(deploy_key_secret_name)
        if not private_key:
            raise APIError(
                code=error_code,
                message=f"failed to resolve deploy key for {label} from secret_name={deploy_key_secret_name!r}",
                status_code=500,
            )
        private_key = _normalize_deploy_private_key(private_key, secret_name=deploy_key_secret_name)
        encoded_key = base64.b64encode(private_key.encode("utf-8")).decode("ascii")
        _ssh_dir = ssh_key_dir or ("/root/.ssh" if not identity else f"{identity['home_dir'].rstrip('/')}/.ssh")
        _key_path = ssh_key_path or f"{_ssh_dir}/id_ed25519"
        setup_cmd = (
            "set -e; "
            f"mkdir -p {shlex.quote(_ssh_dir)} && chmod 700 {shlex.quote(_ssh_dir)} && "
            f"echo '{encoded_key}' | base64 -d > {shlex.quote(_key_path)} && "
            f"chmod 600 {shlex.quote(_key_path)}"
        )
        if identity:
            setup_cmd += (
                f" && chown -R {shlex.quote(identity['linux_user'])}:{shlex.quote(identity['linux_user'])} "
                f"{shlex.quote(_ssh_dir)}"
            )
        setup_result = await underlying.commands.run(setup_cmd)
        setup_err = getattr(setup_result, "error", None)
        if setup_err:
            setup_stderr = _extract_command_stream_text(setup_result, "stderr").strip()
            raise APIError(
                code=error_code,
                message=f"failed to write deploy key for {label}: {setup_err}; stderr={setup_stderr!r}",
                status_code=502,
            )
        ssh_cmd = (
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o IdentitiesOnly=yes -i {shlex.quote(_key_path)}"
        )
        log_url = ssh_url
        # When the target is a network-bound cache (or remote mount), clone into a sandbox-local
        # staging dir first, then bulk-copy onto the target. git-clone writes thousands of
        # tiny objects, so network-filesystem metadata latency dominates the clone.
        # Local staging plus bulk parallel copy overlaps the slow network writes
        # and matches the HTTPS branch's staging.
        clone_into = _clone_staging_dir(target) if stage_via_tmp else target
        full_cmd = command_for_identity(
            _guarded_clone_cmd(
                target,
                _clone_then_checkout(
                    f"GIT_SSH_COMMAND={shlex.quote(ssh_cmd)} {_git_clone_command(ssh_url, clone_into, branch, depth)}",
                    clone_into,
                    sha,
                    depth=depth,
                    ssh_command=ssh_cmd,
                ),
                clean=clone_into,
            ),
            identity,
        )
        clone_result = await underlying.commands.run(full_cmd)
        if stage_via_tmp and not getattr(clone_result, "error", None):
            copy_cmd = command_for_identity(_remote_staging_copy_command(clone_into, target), identity)
            # Deliver the extended copy timeout via whichever convention the adapter exposes
            # (one backend's opts=RunCommandOpts.timeout, the other's timeout_in_millis);
            # run_sandbox_command must not silently drop it — a dropped timeout leaves large
            # copies on the default (short) timeout and can fail large repos.
            copy_result = await run_sandbox_command(
                underlying.commands.run,
                copy_cmd,
                timeout_in_millis=_REMOTE_BULK_COPY_TIMEOUT_MS,
            )
            await underlying.commands.run(
                command_for_identity(f"rm -rf {shlex.quote(clone_into)}", identity)
            )
            clone_result = copy_result
        secret_for_redact = None

    clone_err = _redact_secret_text(getattr(clone_result, "error", None), secret_for_redact)
    if clone_err:
        clone_stdout = _redact_secret_text(
            _extract_command_stream_text(clone_result, "stdout").strip(), secret_for_redact,
        )
        clone_stderr = _redact_secret_text(
            _extract_command_stream_text(clone_result, "stderr").strip(), secret_for_redact,
        )
        raise APIError(
            code=error_code,
            message=(
                f"git clone failed for {label}: {clone_err}; "
                f"stdout={clone_stdout!r}; stderr={clone_stderr!r}; url={log_url}"
            ),
            status_code=502,
        )

    # The HTTPS clone ran as root (egress reliability). Hand the cloned tree to the
    # conversation user so the sandboxed runtime owns and reads its plugins/workspace
    # exactly as if it had cloned them itself. SSH-backend clones already run as the
    # conv user, so this applies only to the HTTPS-clone path.
    if clone_over_https and identity:
        linux_user = str(identity["linux_user"])
        chown_cmd = (
            f"chown -R {shlex.quote(linux_user)}:{shlex.quote(linux_user)} {shlex.quote(target)}"
        )
        chown_result = await underlying.commands.run(chown_cmd)
        chown_err = getattr(chown_result, "error", None)
        if chown_err:
            chown_stderr = _extract_command_stream_text(chown_result, "stderr").strip()
            raise APIError(
                code=error_code,
                message=(
                    f"failed to chown {label} clone to conversation user {linux_user!r}: "
                    f"{chown_err}; stderr={chown_stderr!r}"
                ),
                status_code=502,
            )


def _git_https_clone_url(ssh_url: str, token: str | None = None) -> str:
    """git@host:group/repo.git -> https://git[:<token>]@host/group/repo.git.

    With ``token`` the credential is embedded (URL-encoded) as the password so git
    authenticates directly, without a GIT_ASKPASS+GIT_HTTPS_TOKEN env handshake. The
    askpass path is unreliable under ``command_for_identity``'s non-root ``runuser``
    layering (the env var does not reliably reach git's askpass subprocess →
    intermittent 403); a token in the URL survives runuser verbatim. The token is
    redacted in logs via :func:`_redact_https_clone_url`.
    """
    if not ssh_url.startswith("git@"):
        raise APIError(
            code="REPO_INVALID",
            message=f"cannot convert non-SSH url to https: {ssh_url!r}",
            status_code=500,
        )
    rest = ssh_url[len("git@"):]
    host, sep, path = rest.partition(":")
    if not sep or not host or not path:
        raise APIError(
            code="REPO_INVALID",
            message=f"cannot convert SSH url to https: {ssh_url!r}",
            status_code=500,
        )
    cred = "git" if not token else f"git:{quote(str(token), safe='')}"
    return f"https://{cred}@{host}/{path}"


def _https_git_clone_url(https_url: str, token: str | None = None) -> str:
    """Validate an HTTPS Git URL and optionally add the configured credential."""
    parts = urlsplit(str(https_url or "").strip())
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or not parts.path
        or parts.query
        or parts.fragment
    ):
        raise APIError(
            code="REPO_INVALID",
            message=(
                "HTTPS repository URL must use https://host/path without "
                "embedded credentials, a query, or a fragment"
            ),
            status_code=500,
        )
    if not token:
        return urlunsplit(("https", parts.netloc, parts.path, "", ""))
    netloc = f"git:{quote(str(token), safe='')}@{parts.netloc}"
    return urlunsplit(("https", netloc, parts.path, "", ""))


def _redact_https_clone_url(url: str) -> str:
    """Hide the token in an https://git:<token>@host/... url for logs/errors."""
    return re.sub(r"(https://[^:]+:)[^@]+(@)", r"\1***\2", url)


def _redact_secret_text(text: Any, secret: str | None) -> str:
    value = str(text or "")
    token = str(secret or "")
    return value.replace(token, "***") if token else value


def _git_clone_command(clone_url: str, target: str, branch: str, depth: Any) -> str:
    git_args = ["git", "clone"]
    if branch:
        git_args += ["-b", branch]
    if isinstance(depth, int) and depth > 0:
        git_args += ["--depth", str(depth)]
    git_args += [clone_url, target]
    return " ".join(shlex.quote(arg) for arg in git_args)


def _git_clone_with_askpass_command(clone_url: str, target: str, branch: str, depth: Any) -> str:
    clone_cmd = _git_clone_command(clone_url, target, branch, depth)
    return (
        "set -e; "
        "askpass=\"$(mktemp /tmp/git-askpass.XXXXXX)\"; "
        "trap 'rm -f \"$askpass\"' EXIT; "
        "cat > \"$askpass\" <<'ASTRABOX_GIT_ASKPASS'\n"
        "#!/usr/bin/env sh\n"
        "case \"$1\" in\n"
        "  *Username*|*username*) printf '%s\\n' git ;;\n"
        "  *) printf '%s\\n' \"${GIT_HTTPS_TOKEN:?}\" ;;\n"
        "esac\n"
        "ASTRABOX_GIT_ASKPASS\n"
        "chmod 700 \"$askpass\"; "
        f"GIT_ASKPASS=\"$askpass\" GIT_TERMINAL_PROMPT=0 {clone_cmd}"
    )


@overload
def _resolve_git_https_token(*, required: Literal[True] = True) -> str: ...


@overload
def _resolve_git_https_token(*, required: Literal[False]) -> str | None: ...


def _resolve_git_https_token(*, required: bool = True) -> str | None:
    settings = load_astrabox_settings()
    name = str(getattr(settings, "git_https_token_secret_name", "") or "").strip()
    if not name:
        if not required:
            return None
        raise APIError(
            code="REPO_MISSING_TOKEN",
            message="git https token secret not configured "
            "(astrabox.git.https_token_secret_name)",
            status_code=500,
        )
    token = SecretProvider.get_secret(name)
    if not token:
        raise APIError(
            code="REPO_MISSING_TOKEN",
            message=f"failed to resolve git https token from secret_name={name!r}",
            status_code=500,
        )
    return str(token).strip()


def _normalize_deploy_private_key(private_key: str, *, secret_name: str) -> str:
    """Normalize deploy key material fetched from the secret store into an SSH-readable file.

    Some secret-store entries cannot preserve raw newlines. For OpenSSH private keys
    this accepts the explicit single-line representation:
    BEGIN marker + base64 body + END marker.
    """
    import base64
    import binascii
    import textwrap

    key = str(private_key or "").strip()
    if not key:
        return ""

    if "\n" not in key and "\\n" in key:
        key = key.replace("\\r\\n", "\n").replace("\\n", "\n").strip()

    if (
        "\n" not in key
        and key.startswith(_OPENSSH_PRIVATE_KEY_BEGIN)
        and key.endswith(_OPENSSH_PRIVATE_KEY_END)
    ):
        payload = key[len(_OPENSSH_PRIVATE_KEY_BEGIN):-len(_OPENSSH_PRIVATE_KEY_END)].strip()
        payload = "".join(payload.split())
        if not payload:
            raise APIError(
                code="DEFAULT_REPO_INVALID_KEY",
                message=f"deploy key secret {secret_name!r} has empty OpenSSH payload",
                status_code=500,
            )
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise APIError(
                code="DEFAULT_REPO_INVALID_KEY",
                message=f"deploy key secret {secret_name!r} has invalid OpenSSH base64 payload",
                status_code=500,
            ) from exc
        if not decoded.startswith(b"openssh-key-v1\x00"):
            raise APIError(
                code="DEFAULT_REPO_INVALID_KEY",
                message=f"deploy key secret {secret_name!r} is not an OpenSSH private key payload",
                status_code=500,
            )
        key = (
            _OPENSSH_PRIVATE_KEY_BEGIN
            + "\n"
            + "\n".join(textwrap.wrap(payload, 70))
            + "\n"
            + _OPENSSH_PRIVATE_KEY_END
        )

    if not key.endswith("\n"):
        key = key + "\n"
    return key
