"""Agent plugin-repository normalization helpers."""

from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import urlsplit

from astrabox.common.utils.errors import APIError

PLUGIN_REPOS_BASE_DIR = "/opt/astrabox/claude-plugins"
AGENT_RUNTIME_PLUGIN_REPO_CACHE_DIR = "/opt/conversation-runtime/claude-plugin-repos-cache"
AGENT_RUNTIME_PLUGIN_REPO_CACHE_BASE_DIR = f"{AGENT_RUNTIME_PLUGIN_REPO_CACHE_DIR}/repos"


def get_template_plugin_repos(template: Any) -> list[dict[str, Any]]:
    raw = template.get("plugin_repos") if isinstance(template, dict) else getattr(template, "plugin_repos", None)
    return normalize_plugin_repos(raw)


def plugin_repo_egress_hosts(template: Any) -> list[str]:
    """Return the Git hosts needed while the sandbox prepares Plugins."""

    hosts: list[str] = []
    for index, repo in enumerate(get_template_plugin_repos(template)):
        protocol = str(repo.get("protocol") or "ssh").strip().lower()
        url = str(repo.get("url") or "").strip()
        if protocol == "https":
            host = urlsplit(url).hostname or ""
        elif protocol == "ssh":
            match = re.fullmatch(r"[^@\s]+@([^:\s]+):.+", url)
            host = match.group(1) if match else ""
        else:
            host = ""
        if not host:
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=(
                    f"plugin_repos[{index}] has no network host for "
                    f"protocol={protocol!r}: {url!r}"
                ),
                status_code=500,
            )
        normalized = host.lower()
        if normalized not in hosts:
            hosts.append(normalized)
    return hosts


def normalize_plugin_repos(raw: Any) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise APIError(
            code="PLUGIN_REPO_INVALID",
            message=f"plugin_repos must be a list, got {type(raw).__name__}",
            status_code=500,
        )

    repos: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=f"plugin_repos[{index}] must be an object",
                status_code=500,
            )
        url = str(item.get("url") or "").strip()
        if not url:
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=f"plugin_repos[{index}].url is required",
                status_code=500,
            )
        protocol = str(item.get("protocol") or "ssh").strip().lower()
        secret_name = str(item.get("deploy_key_secret_name") or "").strip()
        paths = _normalize_plugin_paths(item.get("plugin_paths"), index)
        repos.append({
            "url": url,
            "protocol": protocol,
            "deploy_key_secret_name": secret_name,
            "branch": str(item.get("branch") or item.get("ref") or "").strip(),
            "depth": item.get("depth"),
            "sha": str(item.get("sha") or item.get("commit") or "").strip(),
            "plugin_paths": paths,
        })
    return repos


def build_plugin_repo_checkout_dir(
    session_id: str,
    index: int,
    repo: dict[str, Any],
    *,
    base_dir: str | None = None,
) -> str:
    session = _safe_segment(session_id, default="session")
    slug = _repo_slug(str(repo.get("url") or "repo"))
    root = str(base_dir or PLUGIN_REPOS_BASE_DIR).rstrip("/")
    return f"{root}/{session}/{index:02d}-{slug}"


def build_plugin_repo_plugin_options(
    session_id: str,
    repos: list[dict[str, Any]],
    *,
    base_dir: str | None = None,
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for index, repo in enumerate(repos):
        checkout_dir = build_plugin_repo_checkout_dir(session_id, index, repo, base_dir=base_dir)
        for plugin_path in repo.get("plugin_paths") or ["."]:
            if plugin_path == ".":
                path = checkout_dir
            else:
                path = f"{checkout_dir}/{plugin_path}"
            options.append({"type": "local", "path": path})
    return options


def merge_plugin_options(
    existing: Any,
    generated: list[dict[str, str]],
    *,
    template_name: str,
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    if existing is not None:
        if not isinstance(existing, list):
            raise APIError(
                code="CLAUDE_OPTIONS_INVALID",
                message=f"template {template_name!r} engine_options.plugins must be a list",
                status_code=500,
            )
        for idx, plugin in enumerate(existing):
            if not isinstance(plugin, dict):
                raise APIError(
                    code="CLAUDE_OPTIONS_INVALID",
                    message=f"template {template_name!r} engine_options.plugins[{idx}] must be an object",
                    status_code=500,
                )
            plugin_type = str(plugin.get("type") or "").strip()
            path = str(plugin.get("path") or "").strip()
            if plugin_type != "local" or not path:
                raise APIError(
                    code="CLAUDE_OPTIONS_INVALID",
                    message=(
                        f"template {template_name!r} engine_options.plugins[{idx}] "
                        "must be a local plugin with path"
                    ),
                    status_code=500,
                )
            merged.append({"type": "local", "path": path})
    merged.extend(generated)
    return merged


def _normalize_plugin_paths(raw: Any, repo_index: int) -> list[str]:
    if raw is None or raw == "":
        return ["."]
    values = raw if isinstance(raw, list) else [raw]
    paths: list[str] = []
    for path_index, value in enumerate(values):
        path = str(value or "").strip()
        if not path:
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=f"plugin_repos[{repo_index}].plugin_paths[{path_index}] is empty",
                status_code=500,
            )
        if path.startswith("/"):
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=f"plugin_repos[{repo_index}].plugin_paths[{path_index}] must be relative",
                status_code=500,
            )
        normalized = posixpath.normpath(path)
        if normalized in {"", "."}:
            normalized = "."
        if normalized == ".." or normalized.startswith("../"):
            raise APIError(
                code="PLUGIN_REPO_INVALID",
                message=f"plugin_repos[{repo_index}].plugin_paths[{path_index}] escapes repo root",
                status_code=500,
            )
        paths.append(normalized)
    return paths


def _repo_slug(url: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    return _safe_segment(tail, default="repo")


def _safe_segment(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
    return cleaned or default
