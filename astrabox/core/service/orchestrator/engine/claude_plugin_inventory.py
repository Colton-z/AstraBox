"""Inspect Claude's installed local plugin manifests without starting the engine."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from astrabox.core.service.orchestrator.engine.claude_code_config import CLAUDE_PLUGIN_KIND
from astrabox.core.service.orchestrator.runtime.packages import template_packages
from astrabox.core.service.orchestrator.runtime.storage._plugin_cache import (
    _build_plugin_repo_cache_checkout_dir,
)
from astrabox.seams.runtime_preparation import ResolvedPackage, SessionMCPServer

# Claude's plugin manifest merges the root .mcp.json and mcpServers entries.
# External bundles, dynamic components and symbolic declarations cannot prove
# what will be loaded. Keep the original plan for those packages.
_INSPECT = """
import json
import subprocess
import sys
from pathlib import Path

def git(root, *args):
    return subprocess.check_output(
        ["git", "-c", "safe.directory=" + str(root), "-C", str(root), *args],
        stderr=subprocess.DEVNULL, text=True, timeout=10,
    ).strip()

def local_path(root, value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("non-local plugin path")
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("symlink in plugin declaration")
    return current

def read_object(path, optional=False):
    if optional and not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("plugin declaration is not a bounded file")
    with path.open("rb") as handle:
        value = json.loads(handle.read(1024 * 1024 + 1))
    if not isinstance(value, dict):
        raise ValueError("plugin declaration is not an object")
    return value

def mcp_map(value):
    if "mcpServers" in value:
        if set(value) != {"mcpServers"}:
            raise ValueError("ambiguous MCP declaration")
        value = value["mcpServers"]
    if not isinstance(value, dict) or any(not isinstance(server, dict) for server in value.values()):
        raise ValueError("invalid MCP declaration")
    return value

def plugin_names(root):
    manifest = read_object(local_path(root, ".claude-plugin/plugin.json"), optional=True)
    if (manifest.get("dependencies") or manifest.get("hooks") or manifest.get("experimental")
            or local_path(root, "hooks/hooks.json").exists()):
        raise ValueError("dynamic plugin component")
    names = set(mcp_map(read_object(local_path(root, ".mcp.json"), optional=True)))
    declared = manifest.get("mcpServers", {})
    for item in declared if isinstance(declared, list) else [declared]:
        if isinstance(item, str) and item.startswith("./") and item.endswith(".json"):
            names.update(mcp_map(read_object(local_path(root, item))))
        elif isinstance(item, dict):
            names.update(item)
        else:
            raise ValueError("unresolved plugin MCP declaration")
    plugin = manifest.get("name")
    if names and not isinstance(plugin, str):
        raise ValueError("MCP plugin has no manifest name")
    return [{"plugin": plugin, "name": name} for name in sorted(names)]

def inspect(item):
    root = Path(item["checkout"])
    if root.is_symlink():
        raise ValueError("checkout is a symlink")
    commit = git(root, "rev-parse", "HEAD")
    if commit != item["sha"] or git(root, "status", "--porcelain",
                                     "--untracked-files=all", "--ignored=matching"):
        raise ValueError("checkout is not the declared immutable commit")
    servers = [server for path in item["paths"] for server in plugin_names(local_path(root, path))]
    return {"index": item["index"], "commit": commit, "mcp_servers": servers}

try:
    print(json.dumps([inspect(item) for item in json.loads(sys.argv[1])]))
except (OSError, ValueError, subprocess.SubprocessError):
    print("null")
"""


async def inspect_prepared_plugins(sandbox: Any, template: Any) -> tuple[ResolvedPackage, ...] | None:
    """Return only immutable package identities and native MCP names."""
    options = getattr(template, "engine_options", None) or {}
    sdk_options = options.get("sdk_options") or {}
    if getattr(template, "default_repo", None) or sdk_options.get("plugins") or sdk_options.get("settings"):
        return None
    extra_args = sdk_options.get("extra_args") or {}
    if "plugin-dir" in extra_args or "plugin-url" in extra_args:
        return None
    declared = []
    for index, package in enumerate(template_packages(template)):
        commit = str(package.get("sha") or "").lower()
        if package["kind"] != CLAUDE_PLUGIN_KIND or not re.fullmatch(r"[0-9a-f]{40}", commit):
            return None
        declared.append({
            "index": index, "sha": commit, "paths": package["paths"],
            "checkout": _build_plugin_repo_cache_checkout_dir(index, package),
        })
    if not declared:
        return None
    result = await sandbox.commands.run(
        f"python3 -c {shlex.quote(_INSPECT)} {shlex.quote(json.dumps(declared))}", timeout=30,
    )
    if result.exit_code != 0:
        return None
    try:
        rows = json.loads(result.stdout)
        if rows is None:
            return None
        packages = tuple(ResolvedPackage(
            index=row["index"], commit=row["commit"],
            mcp_servers=tuple(SessionMCPServer(**ref) for ref in row["mcp_servers"]),
        ) for row in rows)
    except (TypeError, ValueError):
        return None
    names = [ref for package in packages for ref in package.mcp_servers]
    if len(set(names)) != len(names):
        return None
    return packages
