"""Keep the Assistant's Hermes gateway off every messaging platform.

Hermes' gateway connects each messaging platform its configuration, its
environment or its credential pool enables (``gateway/config_env.py`` at the
pinned commit). The profile's environment carries the RuntimeProfile's
variables for the agent's tools, so a bot token among them would start that
platform's adapter, which would answer the platform's messages outside
AstraBox's triggers and their allowed senders. An explicit
``platforms.<name>.enabled: false`` in config.yaml wins over the environment.
Before the gateway starts, each platform Hermes' own loader enables, other
than its local surface, is written disabled, and the loader is asked again.
The environment itself is left alone: the tools keep it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gateway
import hermes_yaml as yaml
from gateway.config import Platform, load_gateway_config
from hermes_cli.env_loader import load_hermes_dotenv
from hermes_constants import get_process_hermes_home


def enabled_platforms() -> list[str]:
    """The messaging platforms the gateway would connect, read with its own loader."""

    return sorted(
        platform.value
        for platform, config in load_gateway_config().platforms.items()
        if config.enabled and platform is not Platform.LOCAL
    )


def main() -> int:
    home = get_process_hermes_home()
    # The environment the gateway's entry loads before it reads its
    # configuration (``gateway/run.py``).
    load_hermes_dotenv(hermes_home=home, project_env=Path(gateway.__file__).resolve().parents[1] / ".env")
    enabled = enabled_platforms()
    if not enabled:
        return 0
    path = home / "config.yaml"
    cfg = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    platforms = cfg.setdefault("platforms", {}) if isinstance(cfg, dict) else None
    if not isinstance(platforms, dict):
        sys.stderr.write("ASTRABOX_HERMES_PLATFORMS_UNWRITABLE config.yaml platforms is not a mapping\n")
        return 65
    for name in enabled:
        block = platforms.get(name)
        platforms[name] = {**(block if isinstance(block, dict) else {}), "enabled": False}
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    remaining = enabled_platforms()
    if remaining:
        sys.stderr.write(f"ASTRABOX_HERMES_PLATFORMS_ENABLED {','.join(remaining)}\n")
        return 65
    print(f"astrabox: kept the gateway off messaging platforms {','.join(enabled)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
