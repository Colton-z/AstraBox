"""Skill declarations, content acquisition and atomic sandbox-local publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import uuid
from dataclasses import asdict
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.providers.skill_git import _parse_skill_descriptor
from astrabox.seams.sandbox import SandboxHandle
from astrabox.seams.sandbox_data import SandboxDirectorySpec
from astrabox.seams.skills import SkillDescriptor, SkillSource, skill_content_provider

AGENT_RUNTIME_SKILL_CACHE_DIR = "/opt/conversation-runtime/claude-skills-cache"


def parse_skill_source(item: SkillDescriptor) -> SkillSource:
    """Validate a public declaration without reading credentials or the network."""
    if isinstance(item, str):
        repo, version, path, name = _parse_skill_descriptor(item)
        source = SkillSource(provider="git", source=repo, version=version, path=path, name=name)
    elif isinstance(item, dict):
        unknown = set(item) - {"provider", "source", "version", "path", "name", "config"}
        if unknown:
            raise ValueError(f"unknown skill fields: {', '.join(sorted(unknown))}")
        values = {}
        for key in ("provider", "source", "version", "path", "name"):
            value = item.get(key, "")
            if not isinstance(value, str) or "\0" in value:
                raise ValueError(f"skill {key} must be text without NUL")
            values[key] = value.strip()
        if any(not values[key] for key in ("provider", "source", "version", "name")):
            raise ValueError("a structured skill requires provider, source, version and name")
        config = item.get("config", {})
        if not isinstance(config, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or "\0" in v
            for k, v in config.items()
        ):
            raise ValueError("skill config must contain public string values and secret names only")
        source = SkillSource(**values, config=dict(config))
    else:
        raise ValueError("a skill must be a Git descriptor or a source object")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", source.name):
        raise ValueError("a skill name must be a single directory name")
    if source.path.startswith("/") or ".." in source.path.split("/"):
        raise ValueError("a skill path must stay inside its source directory")
    skill_content_provider(source.provider).validate(source)
    return source


def normalize_skills(raw: Any) -> list[SkillDescriptor]:
    """Keep strings and source objects intact, deduplicated by canonical value."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple)):
        raise ValueError("skills must be a list")
    result: list[SkillDescriptor] = []
    seen: set[str] = set()
    names: set[str] = set()
    for item in items:
        if isinstance(item, str):
            item = item.strip()
            if not item:
                continue
        source = parse_skill_source(item)
        normalized = item if isinstance(item, str) else asdict(source)
        key = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        if source.name in names:
            raise ValueError(f"more than one source installs skill {source.name!r}")
        names.add(source.name)
        seen.add(key)
        result.append(normalized)
    return result


def skill_manifest(skills: list[SkillDescriptor]) -> str:
    return "\n".join(
        item if isinstance(item, str) else json.dumps(item, sort_keys=True, separators=(",", ":"))
        for item in skills
    ) + "\n"


def skill_cache_dir(skills: Any) -> str:
    digest = hashlib.sha256(skill_manifest(normalize_skills(skills)).encode()).hexdigest()
    return f"{AGENT_RUNTIME_SKILL_CACHE_DIR}/{digest}"


def skill_repo_egress_hosts(skills: Any) -> list[str]:
    hosts: list[str] = []
    for item in normalize_skills(skills):
        source = parse_skill_source(item)
        for host in skill_content_provider(source.provider).egress_hosts(source):
            if host not in hosts:
                hosts.append(host)
    return hosts


async def _run(sandbox: SandboxHandle, script: str) -> str:
    result = await sandbox.commands.run(f"bash -c {shlex.quote(script)}", timeout=300)
    if result.exit_code != 0:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill content validation or publication failed (exit {result.exit_code})",
            status_code=502,
        )
    return result.stdout


async def prepare_agent_runtime_skill_cache(sandbox: SandboxHandle, skills: Any) -> None:
    """Authorize, resolve, fetch and publish a complete immutable cache tree.

    Concurrent misses may fetch into separate private directories. Only the
    publication holds the cache lock; an already-published identity is reused.
    The declaration link changes atomically after its complete tree exists.
    Cancellation during acquisition never dispatches publication. Once the
    atomic publication begins, a caller losing its connection cannot infer that
    it did not commit; any visible result is nevertheless complete.
    """
    items = normalize_skills(skills)
    if not items:
        return
    operation = f"/tmp/astrabox-skills-{uuid.uuid4().hex}"
    declaration = skill_cache_dir(items)
    manifest = skill_manifest(items)
    sources = [parse_skill_source(item) for item in items]
    await sandbox.files.make_directories([SandboxDirectorySpec(operation, mode=0o700)])
    try:
        resolved = []
        for index, source in enumerate(sources):
            private = f"{operation}/private-{index}"
            await sandbox.files.make_directories([SandboxDirectorySpec(private, mode=0o700)])
            identity = await skill_content_provider(source.provider).resolve(source, sandbox, private)
            if not identity.version or not identity.content_id:
                raise ValueError(f"skill provider {source.provider!r} returned no immutable identity")
            resolved.append(identity)
        receipt = json.dumps([
            {"declaration": asdict(source), **asdict(identity)}
            for source, identity in zip(sources, resolved, strict=True)
        ], sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(receipt.encode()).hexdigest()
        objects = f"{AGENT_RUNTIME_SKILL_CACHE_DIR}/.objects"
        target = f"{objects}/{digest}"
        hit = await _run(sandbox, f"""set -euo pipefail
if test -e {shlex.quote(target)}; then
  test "$(cat {shlex.quote(target + '/.skill-cache-hash')})" = {shlex.quote(digest)}
  printf hit
fi
""")
        if hit.strip() != "hit":
            for index, (source, identity) in enumerate(zip(sources, resolved, strict=True)):
                await skill_content_provider(source.provider).fetch(
                    source, identity, sandbox, f"{operation}/source-{index}",
                    f"{operation}/private-{index}",
                )
            metadata = {
                "sources": [asdict(source) for source in sources],
                "manifest": manifest, "receipt": receipt, "digest": digest,
            }
            await sandbox.files.write_bytes(
                f"{operation}/metadata.json", json.dumps(metadata).encode(), mode=0o600,
            )
            # Python's resolved paths validate symlink targets before copying;
            # only links contained in the selected skill directory survive.
            validation = '''import json, os, pathlib, shutil, stat, sys
root = pathlib.Path(sys.argv[1])
meta = json.loads((root / "metadata.json").read_text())
staging = root / "ready"
staging.mkdir()
for index, source in enumerate(meta["sources"]):
    tree = (root / f"source-{index}").resolve(strict=True)
    if not tree.is_relative_to(root.resolve()):
        raise ValueError("skill source escapes its staging directory")
    selected = (tree / source["path"]).resolve(strict=True)
    if not selected.is_relative_to(tree) or not selected.is_dir():
        raise ValueError("skill directory escapes its source")
    if not (selected / "SKILL.md").is_file():
        raise ValueError("skill directory has no SKILL.md")
    for entry in [selected, *selected.rglob("*")]:
        actual = entry.resolve(strict=True)
        if not actual.is_relative_to(selected):
            raise ValueError("skill symlink escapes its selected directory")
        mode = actual.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError("skill content contains a special file")
    destination = staging / source["name"]
    shutil.copytree(selected, destination, symlinks=False)
for entry in [staging, *staging.rglob("*")]:
    mode = entry.stat().st_mode
    entry.chmod(0o755 if entry.is_dir() or mode & 0o111 else 0o644)
(staging / ".skill-manifest").write_text(meta["manifest"])
(staging / ".skill-sources.json").write_text(meta["receipt"])
(staging / ".skill-cache-hash").write_text(meta["digest"] + "\\n")
'''
            await _run(sandbox, shlex.join(["python3", "-c", validation, operation]))
        # All acquisition and validation are finished before this commit step.
        # flock is also the existing package cache's publication lock.
        publishing = f"{objects}/.staging-{uuid.uuid4().hex}"
        link = f"{AGENT_RUNTIME_SKILL_CACHE_DIR}/.link-{uuid.uuid4().hex}"
        publication = f"""set -euo pipefail
mkdir -p -- {shlex.quote(objects)}
exec 9>{shlex.quote(objects + '/.publish.lock')}
flock -w 180 9
trap 'rm -rf -- {shlex.quote(publishing)} {shlex.quote(link)}' EXIT
if test -e {shlex.quote(target)}; then
  test "$(cat {shlex.quote(target + '/.skill-cache-hash')})" = {shlex.quote(digest)}
else
  cp -a -- {shlex.quote(operation + '/ready')} {shlex.quote(publishing)}
  mv -- {shlex.quote(publishing)} {shlex.quote(target)}
fi
ln -s -- {shlex.quote(target)} {shlex.quote(link)}
mv -Tf -- {shlex.quote(link)} {shlex.quote(declaration)}
"""
        await _run(sandbox, publication)
    except APIError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise APIError(
            code="AGENT_RUNTIME_SKILL_CACHE_FAILED",
            message=f"skill content acquisition failed ({type(exc).__name__})",
            status_code=502,
        ) from None
    finally:
        await asyncio.shield(sandbox.files.remove([operation]))
