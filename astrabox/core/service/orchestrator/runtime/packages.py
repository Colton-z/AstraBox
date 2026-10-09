"""An Agent's or Assistant's engine-native packages, as stored and fetched.

A package is one Git source of a kind the Environment's engine declares
(``EngineExtensions.packages``): a Claude Code plugin, a Hermes plugin, and so
on. This module knows the source, not the kind; what a kind means and where it
is installed is the engine's declaration.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any
from urllib.parse import urlsplit

from astrabox.common.utils.errors import APIError

PACKAGES_BASE_DIR = "/opt/astrabox/claude-plugins"
AGENT_RUNTIME_PACKAGE_CACHE_DIR = "/opt/conversation-runtime/claude-plugin-repos-cache"
AGENT_RUNTIME_PACKAGE_CACHE_BASE_DIR = f"{AGENT_RUNTIME_PACKAGE_CACHE_DIR}/repos"


def template_packages(template: Any) -> list[dict[str, Any]]:
    raw = template.get("packages") if isinstance(template, dict) else getattr(template, "packages", None)
    return normalize_packages(raw)


def package_egress_hosts(template: Any) -> list[str]:
    """Return the Git hosts needed while the sandbox prepares packages."""

    hosts: list[str] = []
    for index, repo in enumerate(template_packages(template)):
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
                code="PACKAGE_INVALID",
                message=(
                    f"packages[{index}] has no network host for "
                    f"protocol={protocol!r}: {url!r}"
                ),
                status_code=500,
            )
        normalized = host.lower()
        if normalized not in hosts:
            hosts.append(normalized)
    return hosts


def normalize_packages(raw: Any) -> list[dict[str, Any]]:
    if raw is None or raw == "":
        return []
    if not isinstance(raw, list):
        raise APIError(
            code="PACKAGE_INVALID",
            message=f"packages must be a list, got {type(raw).__name__}",
            status_code=500,
        )

    repos: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{index}] must be an object",
                status_code=500,
            )
        kind = str(item.get("kind") or "").strip()
        if not kind:
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{index}].kind is required: the package kind the engine declares",
                status_code=500,
            )
        url = str(item.get("url") or "").strip()
        if not url:
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{index}].url is required",
                status_code=500,
            )
        protocol = str(item.get("protocol") or "ssh").strip().lower()
        secret_name = str(item.get("deploy_key_secret_name") or "").strip()
        paths = _normalize_paths(item.get("paths"), index)
        repos.append({
            "kind": kind,
            "url": url,
            "protocol": protocol,
            "deploy_key_secret_name": secret_name,
            "branch": str(item.get("branch") or item.get("ref") or "").strip(),
            "depth": item.get("depth"),
            "sha": str(item.get("sha") or item.get("commit") or "").strip(),
            "paths": paths,
        })
    return repos


def build_package_checkout_dir(
    session_id: str,
    index: int,
    repo: dict[str, Any],
    *,
    base_dir: str | None = None,
) -> str:
    session = _safe_segment(session_id, default="session")
    slug = _repo_slug(str(repo.get("url") or "repo"))
    root = str(base_dir or PACKAGES_BASE_DIR).rstrip("/")
    return f"{root}/{session}/{index:02d}-{slug}"


def _normalize_paths(raw: Any, repo_index: int) -> list[str]:
    if raw is None or raw == "":
        return ["."]
    values = raw if isinstance(raw, list) else [raw]
    paths: list[str] = []
    for path_index, value in enumerate(values):
        path = str(value or "").strip()
        if not path:
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{repo_index}].paths[{path_index}] is empty",
                status_code=500,
            )
        if path.startswith("/"):
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{repo_index}].paths[{path_index}] must be relative",
                status_code=500,
            )
        normalized = posixpath.normpath(path)
        if normalized in {"", "."}:
            normalized = "."
        if normalized == ".." or normalized.startswith("../"):
            raise APIError(
                code="PACKAGE_INVALID",
                message=f"packages[{repo_index}].paths[{path_index}] escapes repo root",
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
