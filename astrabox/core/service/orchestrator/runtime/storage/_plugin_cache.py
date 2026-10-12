"""The per-agent shared plugin-repository runtime cache.

``prepare_agent_runtime_plugin_cache`` clones once into a shared per-Agent cache
keyed by a sha256 hash of the repository list, and
``bootstrap_conversation_runtime_from_agent_cache`` links one conversation to that
cache. ``AGENT_RUNTIME_CACHE_LOCAL_ROOT`` is unused (zero readers).
"""

from __future__ import annotations

from astrabox.seams.skills import SkillDescriptor

import base64
import hashlib
import json
import shlex
from typing import Any

from astrabox.common.logger.logger_factory import get_logger
from astrabox.common.utils.errors import APIError
from astrabox.seams.sandbox_data import SandboxCommandResult
from astrabox.core.service.orchestrator.runtime.conversation_identity import (
    AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT,
    CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP,
    normalize_runtime_identity,
    run_conversation_bootstrap_script,
)
from astrabox.core.service.orchestrator.engine.extensions import render_extension_target
from astrabox.core.service.orchestrator.runtime.packages import (
    AGENT_RUNTIME_PACKAGE_CACHE_BASE_DIR,
    AGENT_RUNTIME_PACKAGE_CACHE_DIR,
    build_package_checkout_dir,
    template_packages,
)

from astrabox.core.service.orchestrator.runtime.storage._default_repo import (
    _build_default_repo_bootstrap_payload,
)
from astrabox.core.service.orchestrator.runtime.storage._git_clone import (
    _clone_git_repo_in_sandbox,
    _underlying_requires_https_git,
)

logger = get_logger(__name__)


# Sandbox-local runtime cache root. The plugin-repo clones
# (``AGENT_RUNTIME_PACKAGE_CACHE_DIR`` in runtime/packages.py) and the
# Skill cache (``AGENT_RUNTIME_SKILL_CACHE_DIR`` in
# runtime/conversation_identity.py) both live under it. Engine-native MCP
# definitions remain inside the prepared plugin checkout.
#
# The tree stays on the sandbox's local disk rather than on a network-mounted
# cache: claude scans it (plugins/skills) at init with dozens of tiny file
# operations, and per-file network-filesystem metadata latency (~20ms) makes a
# network-backed cache seconds slow.
AGENT_RUNTIME_CACHE_LOCAL_ROOT = "/opt/conversation-runtime"
# The plugin-repo cache is keyed on the declared repository list alone — see
# ``_plugin_repo_cache_hash``, whose digest the box keeps in
# ``.plugin-cache-hash`` — not on agent or session, so every conversation
# declaring the same repositories reuses one checkout inside the box.


_PLUGIN_REPO_CACHE_SESSION = "__agent_runtime_cache__"


def _validate_plugin_repo_source(url: str, protocol: str, label: str) -> None:
    if protocol not in {"ssh", "https"}:
        raise APIError(
            code="PACKAGE_UNSUPPORTED_PROTOCOL",
            message=f"{label}.protocol={protocol!r} is not supported (use 'ssh' or 'https')",
            status_code=500,
        )
    if protocol == "ssh" and not url.startswith("git@"):
        raise APIError(
            code="PACKAGE_INVALID",
            message=f"{label}.url={url!r} must be an SSH URL (git@host:group/repo.git)",
            status_code=500,
        )
    if protocol == "https" and not url.startswith("https://"):
        raise APIError(
            code="PACKAGE_INVALID",
            message=f"{label}.url={url!r} must be an HTTPS URL",
            status_code=500,
        )


def _plugin_repo_cache_manifest(repos: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "version": 2,
            "packages": repos,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _plugin_repo_cache_hash(repos: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_plugin_repo_cache_manifest(repos).encode("utf-8")).hexdigest()


def _plugin_repo_cache_base_dir() -> str:
    return AGENT_RUNTIME_PACKAGE_CACHE_BASE_DIR


def _build_plugin_repo_cache_checkout_dir(index: int, repo: dict[str, Any]) -> str:
    return build_package_checkout_dir(
        _PLUGIN_REPO_CACHE_SESSION,
        index,
        repo,
        base_dir=_plugin_repo_cache_base_dir(),
    )


def cached_package_dirs(template: Any) -> list[tuple[dict[str, Any], str, str]]:
    """Each package directory in the runtime cache: ``(package, path, directory)``.

    One entry per declared path, ``"."`` for the repository root, at the
    checkout :func:`prepare_agent_runtime_plugin_cache` makes.
    """
    dirs: list[tuple[dict[str, Any], str, str]] = []
    for index, package in enumerate(template_packages(template)):
        checkout = _build_plugin_repo_cache_checkout_dir(index, package)
        for path in package.get("paths") or ["."]:
            dirs.append((package, path, checkout if path == "." else f"{checkout}/{path}"))
    return dirs


def _plugin_repo_cache_ready_script(repos: list[dict[str, Any]], cache_hash: str) -> str:
    cache_dir = AGENT_RUNTIME_PACKAGE_CACHE_DIR
    checks = []
    for index, repo in enumerate(repos):
        target = _build_plugin_repo_cache_checkout_dir(index, repo)
        checks.append(f"test -d {shlex.quote(target)}/.git")
        for plugin_path in repo.get("paths") or ["."]:
            plugin_dir = target if plugin_path == "." else f"{target}/{plugin_path}"
            checks.append(f"test -d {shlex.quote(plugin_dir)}")
    checks_script = "\n".join(checks)
    return f"""
set -euo pipefail
cache_dir={shlex.quote(cache_dir)}
desired_hash={shlex.quote(cache_hash)}
if test -f "$cache_dir/.plugin-cache-hash" && test "$(cat "$cache_dir/.plugin-cache-hash")" = "$desired_hash"; then
  {checks_script}
  printf 'AGENT_RUNTIME_PLUGIN_CACHE_READY hash=%s cached=1\\n' "$desired_hash"
else
  printf 'AGENT_RUNTIME_PLUGIN_CACHE_MISSING hash=%s\\n' "$desired_hash"
fi
"""


async def prepare_agent_runtime_plugin_cache(
    sandbox: Any,
    template: Any,
    *,
    get_underlying_sandbox_fn: Any,
) -> None:
    """Clone Agent plugin repositories into its sandbox read-only cache."""
    repos = template_packages(template)
    underlying = get_underlying_sandbox_fn(sandbox)
    if underlying is None:
        raise APIError(
            code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
            message="cannot prepare plugin repo cache: no underlying sandbox",
            status_code=502,
        )
    if not repos:
        return

    commands = underlying.commands
    cache_hash = _plugin_repo_cache_hash(repos)
    ready_result = await commands.run(
        f"bash -lc {shlex.quote(_plugin_repo_cache_ready_script(repos, cache_hash))}"
    )
    if ready_result.exit_code == 0 and "AGENT_RUNTIME_PLUGIN_CACHE_READY" in ready_result.stdout:
        return

    cache_dir = AGENT_RUNTIME_PACKAGE_CACHE_DIR
    # Do NOT `rm -rf "$cache_dir"` here: a concurrent runtime-start (e.g. a
    # fault-injection retry) could wipe the other attempt's in-flight clone mid-write.
    # Each per-repo clone owns its target under a flock + rm (see _guarded_clone_cmd),
    # so freshness is handled per-target without destroying a sibling's work.
    setup_script = f"""
set -euo pipefail
cache_dir={shlex.quote(cache_dir)}
rm -rf "$cache_dir.tmp"
mkdir -p "$(dirname "$cache_dir")"
mkdir -p "$cache_dir/repos" "$cache_dir/.ssh"
chmod 700 "$cache_dir/.ssh"
printf 'AGENT_RUNTIME_PLUGIN_CACHE_BUILDING hash=%s\\n' {shlex.quote(cache_hash)}
"""
    # Clearing a previous build's staging tree is as long as that tree.
    setup_result = await commands.run(f"bash -lc {shlex.quote(setup_script)}", timeout=None)
    if setup_result.exit_code != 0:
        raise APIError(
            code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
            message=(
                "failed to initialize plugin repo cache: "
                f"exit_code={setup_result.exit_code}; output={setup_result.output[:2000]!r}"
            ),
            status_code=502,
        )

    for index, repo in enumerate(repos):
        label = f"cached packages[{index}]"
        url = str(repo.get("url") or "").strip()
        protocol = str(repo.get("protocol") or "ssh").strip().lower()
        _validate_plugin_repo_source(url, protocol, label)

        target = _build_plugin_repo_cache_checkout_dir(index, repo)
        branch = str(repo.get("branch") or "").strip()
        depth = repo.get("depth")
        secret_name = str(repo.get("deploy_key_secret_name") or "").strip()
        await _clone_git_repo_in_sandbox(
            underlying,
            ssh_url=url,
            protocol=protocol,
            target=target,
            branch=branch,
            depth=depth,
            deploy_key_secret_name=secret_name or None,
            identity=None,
            error_code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
            label=label,
            ssh_key_dir=f"{cache_dir.rstrip('/')}/.ssh",
            ssh_key_path=f"{cache_dir.rstrip('/')}/.ssh/id_ed25519_{index}",
            # cache_dir is bind-mounted onto the agent's shared network cache, so clone into a
            # local staging dir then bulk-copy onto it — never git-clone directly onto a network
            # filesystem, where per-file metadata latency dominates startup. When
            # cache_dir is local this adds only an extra local copy.
            stage_via_tmp=True,
            # Pinned inside the clone, against the private staging tree. Run
            # here afterwards it rewrote the shared cache under its readers.
            sha=str(repo.get("sha") or "").strip() or None,
        )

        for plugin_path in repo.get("paths") or ["."]:
            plugin_dir = target if plugin_path == "." else f"{target}/{plugin_path}"
            path_probe = await commands.run(
                f"test -d {shlex.quote(plugin_dir)} && echo EXISTS || echo MISSING"
            )
            if "EXISTS" not in path_probe.stdout:
                raise APIError(
                    code="PACKAGE_INVALID",
                    message=f"{label}.paths entry {plugin_path!r} does not exist in cloned repo",
                    status_code=500,
                )

    manifest = _plugin_repo_cache_manifest(repos)
    manifest_b64 = base64.b64encode(manifest.encode("utf-8")).decode("ascii")
    finalize_script = f"""
set -euo pipefail
cache_dir={shlex.quote(cache_dir)}
printf %s {shlex.quote(manifest_b64)} | base64 -d > "$cache_dir/.plugin-cache-manifest"
printf '%s\\n' {shlex.quote(cache_hash)} > "$cache_dir/.plugin-cache-hash"
rm -rf "$cache_dir/.ssh"
chmod -R a+rX,go-w "$cache_dir"
printf 'AGENT_RUNTIME_PLUGIN_CACHE_READY hash=%s cached=0\\n' {shlex.quote(cache_hash)}
"""
    # The permissions change once per file of the cache.
    finalize_result = await commands.run(f"bash -lc {shlex.quote(finalize_script)}", timeout=None)
    finalize_output = finalize_result.output
    if finalize_result.exit_code != 0 or "AGENT_RUNTIME_PLUGIN_CACHE_READY" not in finalize_output:
        raise APIError(
            code="AGENT_RUNTIME_PLUGIN_CACHE_FAILED",
            message=(
                "failed to finalize plugin repo cache: "
                f"error={_exit_or_missing(finalize_result)}; "
                f"output={finalize_output[:2000]!r}"
            ),
            status_code=502,
        )
    return


def _exit_or_missing(result: SandboxCommandResult) -> str:
    if result.exit_code != 0:
        return f"exit_code={result.exit_code}"
    return "missing readiness marker"


def _engine_extensions(template: Any) -> tuple[Any, Any]:
    """The extension surface and adapter of the template's engine."""

    from astrabox.core.service.orchestrator.engine.capabilities import capabilities_for_engine_kind
    from astrabox.core.service.orchestrator.engine.registry import get_engine_adapter

    engine_kind = str(
        (template.get("engine_kind") if isinstance(template, dict) else getattr(template, "engine_kind", None))
        or ""
    ).strip()
    return capabilities_for_engine_kind(engine_kind).extensions, get_engine_adapter(engine_kind)


def _skills_target(template: Any, identity: dict[str, Any]) -> str:
    """Where this conversation's engine discovers Skills, as its adapter declares."""

    extensions, _adapter = _engine_extensions(template)
    engine_kind = str(
        (template.get("engine_kind") if isinstance(template, dict) else getattr(template, "engine_kind", None))
        or ""
    ).strip()
    declared = extensions.skills_target
    if declared is None:
        raise APIError(
            code="ENGINE_CAPABILITY_UNAVAILABLE",
            message=f"engine {engine_kind!r} declares no Skills target",
            status_code=409,
        )
    return render_extension_target(
        declared, home_dir=identity["home_dir"], config_dir=identity.get("config_dir")
    )


async def bootstrap_conversation_runtime_from_agent_cache(
    sandbox: Any,
    template: Any,
    session_id: str,
    *,
    get_underlying_sandbox_fn: Any,
    runtime_identity: dict[str, Any],
    skills: list[SkillDescriptor] | tuple[SkillDescriptor, ...] | None = None,
    default_repo_target_cwd: str | None = None,
    sidecar_endpoint: str | None = None,
    bootstrap_transport: str = CONVERSATION_BOOTSTRAP_TRANSPORT_SIDECAR_HTTP,
) -> dict[str, Any]:
    """Provision a conversation identity and cached capabilities."""
    identity = normalize_runtime_identity(runtime_identity)
    if not identity:
        raise APIError(
            code="CONVERSATION_IDENTITY_REQUIRED",
            message="runtime_identity is required for conversation bootstrap",
            status_code=500,
        )

    repos = template_packages(template)
    plugin_links: list[dict[str, Any]] = []
    plugin_cache_hash = _plugin_repo_cache_hash(repos) if repos else ""
    extensions, adapter = _engine_extensions(template)
    if repos:
        _ = get_underlying_sandbox_fn(sandbox)
        linked_bases: dict[str, str] = {}
        for index, repo in enumerate(repos):
            kind = extensions.package_kind(str(repo.get("kind") or ""))
            if kind is None or kind.installed_by_engine:
                continue
            base_dir = linked_bases.setdefault(kind.name, render_extension_target(
                kind.target, home_dir=identity["home_dir"], config_dir=identity.get("config_dir"),
            ))
            source = _build_plugin_repo_cache_checkout_dir(index, repo)
            dest = build_package_checkout_dir(session_id, index, repo, base_dir=base_dir)
            plugin_dirs = [
                dest if plugin_path == "." else f"{dest}/{plugin_path}"
                for plugin_path in (repo.get("paths") or ["."])
            ]
            plugin_links.append(
                {
                    "source": source,
                    "dest": dest,
                    "plugin_dirs": plugin_dirs,
                }
            )
    engine_installed = [
        {"kind": str(package.get("kind")), "source": directory}
        for package, _path, directory in (cached_package_dirs(template) if repos else [])
        if (kind := extensions.package_kind(str(package.get("kind") or ""))) is not None
        and kind.installed_by_engine
    ]
    engine_options = (
        template.get("engine_options") if isinstance(template, dict) else getattr(template, "engine_options", None)
    )
    from astrabox.core.service.orchestrator.runtime.mcp_servers import author_mcp_servers

    install_script = adapter.conversation_install_script(
        packages=engine_installed,
        engine_options=dict(engine_options) if isinstance(engine_options, dict) else {},
        mcp_servers=author_mcp_servers(
            template.get("mcp_servers") if isinstance(template, dict) else getattr(template, "mcp_servers", None)
        ),
    )
    # Only on the conversation tenancy is the engine the image's resident
    # service; a shared placement starts its own after this bootstrap.
    restart_script = (
        adapter.resident_restart_script()
        if install_script and str(identity.get("sandbox_tenancy") or "") != "agent"
        else None
    )

    default_repo_payload = None
    if str(default_repo_target_cwd or "").strip():
        underlying = get_underlying_sandbox_fn(sandbox)
        default_repo_payload = _build_default_repo_bootstrap_payload(
            template,
            target_cwd=str(default_repo_target_cwd or "").strip(),
            session_id=session_id,
            use_https_git=_underlying_requires_https_git(underlying),
        )

    ready_identity = await run_conversation_bootstrap_script(
        sandbox,
        identity,
        sidecar_endpoint=sidecar_endpoint,
        skills=skills,
        skills_target=_skills_target(template, identity) if skills else None,
        install_script=install_script,
        restart_script=restart_script,
        default_repo=default_repo_payload,
        plugin_links=plugin_links,
        plugin_cache_hash=plugin_cache_hash,
        plugin_cache_dir=AGENT_RUNTIME_PACKAGE_CACHE_DIR,
        bootstrap_transport=bootstrap_transport,
    )
    stage_evidence = ready_identity.get("stage_evidence") if isinstance(ready_identity.get("stage_evidence"), dict) else {}
    ready_identity["stage_evidence"] = {
        **stage_evidence,
        "bootstrap_script": AGENT_RUNTIME_CONVERSATION_BOOTSTRAP_SCRIPT,
    }
    return ready_identity
