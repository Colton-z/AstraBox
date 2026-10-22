#!/usr/bin/env python3
"""Generic Hermes profile config.yaml merger.

Reads up to two env JSON blobs and deep-merges them into
``$HERMES_HOME/config.yaml``:

- ``ASTRABOX_HERMES_CONFIG_OVERWRITE`` - platform-owned, force-overwrite at
  every nested key. Lists are atomic leaves (replaced wholesale). The
  platform's MCP servers are exactly the ones it names: a recorded platform
  server absent from it is removed, and servers the user added stay.
- ``ASTRABOX_HERMES_CONFIG_DEFAULTS``  - user-overridable, setdefault at every
  nested key. Already-present keys are not touched.
- ``ASTRABOX_HERMES_SOUL_B64`` - the Assistant's system prompt as UTF-8
  ``SOUL.md`` content, base64-encoded to preserve multiline markdown through
  shell env plumbing. Present, the platform owns ``SOUL.md``; absent, a
  ``SOUL.md`` the platform wrote is handed back to Hermes' own default.
- ``ASTRABOX_HERMES_SKILLS_CACHE`` - the runtime Skill cache, copied into the
  ``skills/astrabox`` category. Absent, that category is removed.
- ``ASTRABOX_HERMES_PLUGINS`` - JSON ``[{"kind", "key", "source"}]``: each
  cached plugin directory is placed at ``plugins/<key>`` (keys are
  ``astrabox/<directory>``), enabled, and its declared Python dependencies
  prepared in Hermes' per-home dependency environment. The ``astrabox`` category and
  the keys the platform enabled are the platform's; every other plugin and
  Skill in the profile is left alone.

All envs are optional. The script is the single contract between platform and
the Hermes runtime image: adding profile config/materialization fields should
only require platform-side payload changes. It runs before the backend starts
and again while it runs, so every file it writes is replaced atomically.

Exit codes: 0 OK, 65 EX_DATAERR (PyYAML missing, malformed env JSON, etc).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

_ENV_SOURCES: tuple[tuple[str, str], ...] = (
    ("ASTRABOX_HERMES_CONFIG_OVERWRITE", "overwrite"),
    ("ASTRABOX_HERMES_CONFIG_DEFAULTS", "setdefault"),
)
_SOUL_B64_ENV = "ASTRABOX_HERMES_SOUL_B64"
#: Beside SOUL.md: the sha256 of the SOUL.md the platform last wrote. It separates
#: a SOUL.md that still holds the platform's content from one the user wrote
#: afterwards, which must survive.
_MANAGED_SOUL_DIGEST_FILE = "astrabox-managed-soul.sha256"
#: Beside config.yaml: the MCP server names the platform last configured. A
#: recursive merge only adds and overwrites, so without this record a server
#: removed from the Assistant would stay in Hermes' config forever.
_MANAGED_MCP_SERVERS_FILE = "astrabox-managed-mcp-servers.json"
_A2A_PEER_PREFIX = "astrabox/"
_SKILLS_CACHE_ENV = "ASTRABOX_HERMES_SKILLS_CACHE"
_PLUGINS_ENV = "ASTRABOX_HERMES_PLUGINS"
#: The category directory the platform owns under ``skills/`` and ``plugins/``.
_CATEGORY = "astrabox"
#: Inside each platform category: the digest of what was placed there, so an
#: unchanged configuration is not copied again on every start.
_PLACED_DIGEST_FILE = ".astrabox-placed.sha256"
#: Beside config.yaml: the plugin keys the platform last enabled.
_MANAGED_PLUGINS_FILE = "astrabox-managed-plugins.json"
#: Which file identifies a package of each kind (``hermes_cli/plugins_discovery.py``:
#: a directory with ``plugin.yaml`` is a native plugin even beside a ``plugin.json``).
_PLUGIN_MANIFESTS = {"hermes-plugin": ("plugin.yaml", "plugin.yml"), "agent-plugin": ("plugin.json",)}


def deep_merge(target: dict[str, Any], source: dict[str, Any], mode: str) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            deep_merge(existing, value, mode)
            continue
        if mode == "overwrite":
            target[key] = value
        elif mode == "setdefault":
            target.setdefault(key, value)
        else:
            raise ValueError(f"unknown merge mode: {mode!r}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` atomically with owner-only ``data``.

    A running Hermes reads these files while this program rewrites them — the
    config on every session it builds, SOUL.md for that session's prompt — so a
    reader must see the old file or the new one, never a half-written one.
    """

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def materialize_soul(home: Path) -> bool:
    """Make ``SOUL.md`` follow the Assistant's system prompt.

    With ``ASTRABOX_HERMES_SOUL_B64`` set, the platform owns ``SOUL.md``: the
    content is written and its digest recorded. Without it, a ``SOUL.md`` that
    still holds what the platform last wrote is removed, so Hermes seeds its own
    default identity when it next loads the profile; one that changed since is
    the user's and stays. Either way the record goes, because the platform owns
    nothing any more.
    """

    soul_path = home / "SOUL.md"
    managed_path = home / _MANAGED_SOUL_DIGEST_FILE
    raw = os.environ.get(_SOUL_B64_ENV)
    if raw is None:
        if not managed_path.exists():
            return False
        managed = managed_path.read_text(encoding="utf-8").strip()
        if soul_path.exists() and _sha256(soul_path.read_bytes()) == managed:
            soul_path.unlink()
            print("ASTRABOX_HERMES_SOUL_RELEASED")
        managed_path.unlink()
        return True
    try:
        content = base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"{_SOUL_B64_ENV} invalid: {exc}\n")
        raise TypeError(str(exc)) from exc
    encoded = content.encode("utf-8")
    digest = _sha256(encoded)
    current = soul_path.read_bytes() if soul_path.exists() else None
    recorded = (
        managed_path.read_text(encoding="utf-8").strip()
        if managed_path.exists()
        else None
    )
    if current == encoded and recorded == digest:
        return False
    if current != encoded:
        _write_private(soul_path, encoded)
        print("ASTRABOX_HERMES_SOUL_READY")
    if recorded != digest:
        _write_private(managed_path, f"{digest}\n".encode("ascii"))
    return True


def _replace_tree(staging: Path, target: Path) -> None:
    """Put ``staging`` at ``target``, removing what was there."""

    previous = target.with_name(f".{target.name}.{os.getpid()}.old")
    if target.exists():
        os.replace(target, previous)
    os.replace(staging, target)
    shutil.rmtree(previous, ignore_errors=True)


def _place(target: Path, digest: str, fill: Any) -> bool:
    """Make ``target`` hold what ``fill`` copies into a fresh directory.

    Skipped when ``target`` already records ``digest``; returns whether it was
    rewritten.
    """

    recorded = target / _PLACED_DIGEST_FILE
    if recorded.exists() and recorded.read_text(encoding="utf-8").strip() == digest:
        return False
    staging = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        fill(staging)
        (staging / _PLACED_DIGEST_FILE).write_text(f"{digest}\n", encoding="utf-8")
        _replace_tree(staging, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return True


def materialize_skills(home: Path) -> None:
    """Make ``skills/astrabox`` hold exactly the Assistant's Skills."""

    target = home / "skills" / _CATEGORY
    cache = os.environ.get(_SKILLS_CACHE_ENV)
    if not cache:
        if target.exists():
            shutil.rmtree(target)
            print("ASTRABOX_HERMES_SKILLS_RELEASED")
        return
    source = Path(cache)
    digest = (source / ".skill-cache-hash").read_text(encoding="utf-8").strip()

    def fill(staging: Path) -> None:
        for skill in sorted(source.iterdir()):
            if skill.is_dir() and not skill.name.startswith("."):
                shutil.copytree(skill, staging / skill.name, symlinks=True)

    if _place(target, digest, fill):
        print("ASTRABOX_HERMES_SKILLS_READY")


def configured_plugins() -> list[dict[str, str]]:
    raw = (os.environ.get(_PLUGINS_ENV) or "").strip()
    if not raw:
        return []
    plugins = json.loads(raw)
    if not isinstance(plugins, list) or not all(
        isinstance(plugin, dict)
        and plugin.get("kind") in _PLUGIN_MANIFESTS
        and str(plugin.get("key") or "").startswith(f"{_CATEGORY}/")
        and plugin.get("source")
        for plugin in plugins
    ):
        raise TypeError(f"{_PLUGINS_ENV} must list {{kind, key, source}} plugins")
    return plugins


def materialize_plugins(cfg: dict[str, Any], plugins: list[dict[str, str]], home: Path) -> None:
    """Make ``plugins/astrabox`` hold the Assistant's plugins, and enable exactly those.

    Hermes loads a user plugin only when its key is in ``plugins.enabled`` and
    not in ``plugins.disabled``. The enabled list keeps the owner's own keys and
    gains exactly the configured ones; a key the platform recorded but the
    configuration omits is taken out.
    """

    target = home / "plugins" / _CATEGORY
    if not plugins:
        if target.exists():
            shutil.rmtree(target)
            print("ASTRABOX_HERMES_PLUGINS_RELEASED")
    else:
        for plugin in plugins:
            source = Path(plugin["source"])
            manifests = _PLUGIN_MANIFESTS[plugin["kind"]]
            if not any((source / name).is_file() for name in manifests):
                raise TypeError(
                    f"package {plugin['key']} of kind {plugin['kind']} has no {' or '.join(manifests)}"
                )
            if plugin["kind"] == "agent-plugin" and (source / "plugin.yaml").exists():
                raise TypeError(f"package {plugin['key']} has a plugin.yaml: Hermes loads it as a hermes-plugin")

        def fill(staging: Path) -> None:
            for plugin in plugins:
                shutil.copytree(
                    plugin["source"],
                    staging / plugin["key"].split("/", 1)[1],
                    symlinks=True,
                    ignore=shutil.ignore_patterns(".git"),
                )

        digest = _sha256(json.dumps(plugins, sort_keys=True).encode("utf-8"))
        if _place(target, digest, fill):
            print("ASTRABOX_HERMES_PLUGINS_READY")

    managed_path = home / _MANAGED_PLUGINS_FILE
    previous: list[Any] = []
    if managed_path.exists():
        recorded = json.loads(managed_path.read_text(encoding="utf-8"))
        if not isinstance(recorded, list):
            raise TypeError(f"{_MANAGED_PLUGINS_FILE} must hold a JSON array")
        previous = recorded
    keys = [plugin["key"] for plugin in plugins]
    section = cfg.get("plugins")
    if keys or (isinstance(section, dict) and previous):
        if not isinstance(section, dict):
            section = cfg["plugins"] = {}
        enabled = [key for key in section.get("enabled") or [] if key not in previous and key not in keys]
        section["enabled"] = enabled + keys
        disabled = [key for key in section.get("disabled") or [] if key not in keys]
        if disabled or "disabled" in section:
            section["disabled"] = disabled
    if previous != keys:
        _write_private(managed_path, json.dumps(keys).encode("utf-8"))


def install_plugin_dependencies() -> None:
    """Prepare the enabled native plugin set through Hermes' package manager.

    PM owns the configuration discovery, per-home dependency environment,
    currency check and locked publication. A failed preparation stops startup.
    """
    from pm import sync_venv, venv_is_current

    if not venv_is_current():
        sync_venv(explicit=True)
    print("ASTRABOX_HERMES_PLUGIN_DEPENDENCIES_READY")


def release_unconfigured_mcp_servers(
    cfg: dict[str, Any], overwrite: dict[str, Any], home: Path
) -> None:
    """Keep only the platform MCP servers the current blob names.

    Only names the platform recorded as its own are removed, so a server the
    user added to this profile through Hermes stays.
    """

    managed_path = home / _MANAGED_MCP_SERVERS_FILE
    configured = overwrite.get("mcp_servers")
    current = sorted(configured) if isinstance(configured, dict) else []
    previous: list[Any] = []
    if managed_path.exists():
        recorded = json.loads(managed_path.read_text(encoding="utf-8"))
        if not isinstance(recorded, list):
            raise TypeError(f"{_MANAGED_MCP_SERVERS_FILE} must hold a JSON array")
        previous = recorded
    servers = cfg.get("mcp_servers")
    if isinstance(servers, dict):
        for name in set(previous) - set(current):
            servers.pop(name, None)
        if not servers:
            cfg.pop("mcp_servers")
    if previous != current:
        _write_private(managed_path, json.dumps(current).encode("utf-8"))


def replace_platform_a2a_peers(
    cfg: dict[str, Any], overwrite: dict[str, Any],
) -> None:
    """Rebuild owned peers while keeping owner-authored external peers intact."""
    configured = overwrite.get("a2a_agents", {})
    if not isinstance(configured, dict):
        raise TypeError("platform a2a_agents must be a mapping")
    peers = cfg.setdefault("a2a_agents", {})
    if not isinstance(peers, dict):
        raise TypeError("a2a_agents must be a mapping")
    if any(not str(name).startswith(_A2A_PEER_PREFIX) for name in configured):
        raise ValueError("platform A2A peers require the astrabox/ namespace")
    for name in list(peers):
        if str(name).startswith(_A2A_PEER_PREFIX):
            peers.pop(name)
    peers.update(configured)


def enable_assistant_a2a_tools() -> None:
    """Use the pinned vendor's tools-enable implementation, preserving its defaults."""
    from hermes_cli.config import load_config
    from hermes_cli.plugins import discover_plugins
    from hermes_cli.tools_config import _get_platform_tools
    from hermes_cli.tools_config_mcp import _apply_toolset_change

    discover_plugins()
    for platform in ("cli", "cron"):
        config = load_config()
        if "a2a" not in _get_platform_tools(config, platform):
            _apply_toolset_change(config, platform, ["a2a"], "enable")
        if "a2a" not in _get_platform_tools(load_config(), platform):
            raise RuntimeError(f"Hermes a2a toolset remains disabled for {platform}")


def main() -> int:
    try:
        import yaml
    except ImportError as exc:
        sys.stderr.write(f"PyYAML required to write Hermes config: {exc}\n")
        return 65

    home = Path(os.environ.get("HERMES_HOME") or "/root/.hermes")
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    cfg_path = home / "config.yaml"

    if cfg_path.exists():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            sys.stderr.write(f"Hermes config.yaml parse failed: {exc}\n")
            return 65
        cfg: dict[str, Any] = {} if loaded is None else loaded
    else:
        cfg = {}

    if not isinstance(cfg, dict):
        sys.stderr.write("Hermes config.yaml top-level must be a YAML mapping\n")
        return 65

    applied_config = False
    assistant_a2a = False
    for env_name, mode in _ENV_SOURCES:
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        try:
            patch = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"{env_name} not valid JSON: {exc}\n")
            return 65
        if not isinstance(patch, dict):
            sys.stderr.write(f"{env_name} top-level must be a JSON object\n")
            return 65
        if mode == "overwrite":
            try:
                release_unconfigured_mcp_servers(cfg, patch, home)
                if "a2a_agents" in patch or "a2a_agents" in cfg:
                    replace_platform_a2a_peers(cfg, patch)
                assistant_a2a = "a2a_agents" in patch
            except (TypeError, ValueError) as exc:
                sys.stderr.write(f"Hermes managed configuration invalid: {exc}\n")
                return 65
        deep_merge(cfg, patch, mode)
        applied_config = True

    try:
        materialized_soul = materialize_soul(home)
    except TypeError:
        return 65

    try:
        materialize_skills(home)
        plugins = configured_plugins()
        materialize_plugins(cfg, plugins, home)
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"ASTRABOX_HERMES_EXTENSIONS_FAILED {exc}\n")
        return 65
    applied_config = applied_config or bool(plugins) or "plugins" in cfg

    if not applied_config and not cfg_path.exists():
        if materialized_soul:
            return 0
        print("ASTRABOX_HERMES_CONFIG_NOOP")
        return 0

    _write_private(
        cfg_path,
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    # After config.yaml: Hermes resolves a plugin's dependencies together with
    # those of every plugin config.yaml enables.
    try:
        install_plugin_dependencies()
        if assistant_a2a:
            enable_assistant_a2a_tools()
    except (OSError, RuntimeError) as exc:
        sys.stderr.write(f"ASTRABOX_HERMES_EXTENSIONS_FAILED {exc}\n")
        return 65
    print("ASTRABOX_HERMES_CONFIG_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
