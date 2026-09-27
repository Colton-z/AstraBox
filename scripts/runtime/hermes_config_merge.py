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
            except (TypeError, json.JSONDecodeError) as exc:
                sys.stderr.write(f"{_MANAGED_MCP_SERVERS_FILE} invalid: {exc}\n")
                return 65
        deep_merge(cfg, patch, mode)
        applied_config = True

    try:
        materialized_soul = materialize_soul(home)
    except TypeError:
        return 65

    if not applied_config and not cfg_path.exists():
        if materialized_soul:
            return 0
        print("ASTRABOX_HERMES_CONFIG_NOOP")
        return 0

    _write_private(
        cfg_path,
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True).encode("utf-8"),
    )
    print("ASTRABOX_HERMES_CONFIG_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
