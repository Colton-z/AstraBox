#!/usr/bin/env python3
"""Render ``docs/configuration.md`` from the :mod:`astrabox.config.env_registry`.

The registry (:data:`astrabox.config.env_registry.ENV_REGISTRY`) is the
canonical inventory of environment variables AstraBox reads. Run this script
after editing the registry and commit the result —
``scripts/check_env_registry.py`` asserts the committed file is byte-identical
to what :func:`render_markdown` produces from the CURRENT registry, so a
registry change with no regenerate fails the gate instead of silently drifting.

Usage::

    python scripts/gen_config_docs.py

No arguments, no flags: it always (re)writes ``docs/configuration.md`` next to
this script's repo root.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from astrabox.config.env_registry import (  # noqa: E402
    ENV_REGISTRY,
    SETUP_REVIEW,
    Tier,
    by_tier,
    compose_settable,
)

#: Public settings lead the generated reference; operator-only tiers follow.
_TIER_ORDER: tuple[Tier, ...] = ("public", "internal", "injected", "dev", "test")

_TIER_TITLE: dict[Tier, str] = {
    "public": "Deployment settings",
    "internal": "Advanced tuning",
    "injected": "Sandbox runtime variables",
    "dev": "Local development",
    "test": "Automated tests",
}

_TIER_BLURB: dict[Tier, str] = {
    "public": (
        "Settings for credentials, endpoints, feature options, resource limits, "
        "and security. `.env.example` hand-picks common quickstart settings rather "
        "than rendering this complete group."
    ),
    "internal": (
        "Timeouts, retries, and intervals for deployments that need to tune "
        "specific operational behavior."
    ),
    "injected": (
        "Values prepared by the AstraBox service and read by code inside a "
        "sandbox. If one is renamed, update both sides and the matching tests."
    ),
    "dev": (
        "Options reserved for local development and debugging. Production "
        "security settings are listed under Deployment settings."
    ),
    "test": (
        "Fault-injection switches reserved for the automated end-to-end test suite."
    ),
}

_HEADER = """\
<!-- GENERATED FILE — do not hand-edit.
     Generated from: astrabox/config/env_registry.py
     Regenerate with: python scripts/gen_config_docs.py -->

# Complete environment-variable list

This generated page lists every environment variable read by AstraBox. Use it
to look up an exact name, default, or accepted scope. For deployment workflows,
start with [Configure AstraBox](runtime-profiles.md), which groups common settings by
task and explains when to use them.

The complete list is grouped by intended use:

* **Deployment settings** configure a running installation.
* **Advanced tuning** changes timeouts, retries, and other defensive defaults.
* **Sandbox runtime variables** are prepared by the service for sandbox code.
* **Local development** options support debugging on a developer machine.
* **Automated tests** options are reserved for the end-to-end test suite.
"""

_DYNAMIC_HEADER = """
## Dynamic / pattern-based

Entries below have names assembled at runtime. Each naming pattern is listed
once.
"""


def _cell(text: str) -> str:
    """Escape a value for a markdown table cell (defends against a stray `|`)."""
    return text.replace("|", "\\|").replace("\n", " ")


def _default_cell(default: str) -> str:
    if default == "":
        return "*(none)*"
    return f"`{default}`"


def _render_table(rows: list) -> str:
    lines = [
        "| Variable | Default | Description |",
        "| --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| `{name}` | {default} | {description} |".format(
                name=_cell(row.name),
                default=_default_cell(row.default),
                description=_cell(row.description),
            )
        )
    return "\n".join(lines)


def render_markdown() -> str:
    """Render the full ``docs/configuration.md`` body from ``ENV_REGISTRY``."""
    parts = [_HEADER]
    for tier in _TIER_ORDER:
        # Pattern rows are rendered once in their own section below, not
        # duplicated into whichever tier table they also carry.
        rows = [row for row in by_tier(tier) if not row.is_pattern]
        if not rows:
            continue
        parts.append(f"\n## {_TIER_TITLE[tier]}\n")
        parts.append(f"\n{_TIER_BLURB[tier]}\n")
        parts.append(f"\n{_render_table(rows)}\n")

    patterns = sorted(
        (s for s in ENV_REGISTRY if s.is_pattern), key=lambda s: s.name
    )
    if patterns:
        parts.append(_DYNAMIC_HEADER)
        parts.append(f"\n{_render_table(patterns)}\n")

    text = "".join(parts)
    if not text.endswith("\n"):
        text += "\n"
    return text


_REFERENCE_HEADER = """\
# GENERATED FILE: do not edit. Generated from astrabox/config/env_registry.py
# by scripts/gen_config_docs.py.
#
# Every setting an operator of the Compose deployment can put in
# containers/.env, with its default. To change one, copy its line into
# containers/.env without the leading "#", set the value, and run
# `docker compose up -d` in this directory (or run the installer again).
# A setting left out keeps the default shown here.
#
# scripts/install.sh reads the first section: a "#?" line says why the
# setting below it deserves a look before the deployment takes work.
"""


def _comment(text: str) -> str:
    return "\n".join(f"# {line}" for line in textwrap.wrap(text, width=76))


def _reference_default(default: str) -> str:
    # "derived" and "generated" describe where a value comes from; neither is
    # a value an operator could write.
    return "" if default in ("derived", "generated") else default


def render_settings_reference() -> str:
    """Render ``containers/settings.reference.env`` from the registry."""
    settable = {spec.name: spec for spec in compose_settable()}
    parts = [_REFERENCE_HEADER, "\n# >>> review at first install\n"]
    for name, why in SETUP_REVIEW.items():
        spec = settable[name]
        parts.append(f"\n#? {why}\n{_comment(spec.description)}\n#{name}={_reference_default(spec.default)}\n")
    parts.append("\n# <<< review at first install\n\n# Every other setting, by name.\n")
    for name, spec in settable.items():
        if name in SETUP_REVIEW:
            continue
        parts.append(f"\n{_comment(spec.description)}\n#{name}={_reference_default(spec.default)}\n")
    return "".join(parts)


def main() -> int:
    for out_path, text in (
        (_REPO_ROOT / "docs" / "configuration.md", render_markdown()),
        (_REPO_ROOT / "containers" / "settings.reference.env", render_settings_reference()),
    ):
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
