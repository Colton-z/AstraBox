#!/usr/bin/env python3
"""Credential injection is a seam, not a provider's vocabulary.

`astrabox/seams/egress_credentials.py` states the substitution contract
normatively so that every backend implements the same semantics. When platform
or engine code instead reaches into one provider's credential module — minting
that provider's placeholder strings, composing that vendor's binding objects —
the contract stops being the seam's and becomes that provider's, and a second
sandbox backend cannot be added without editing all of them.

That is not a style point. The vocabulary leaked to eleven modules, including a
second provider, and the rule that every writer of the shared substitution
table must carry every live identity lived only in a docstring. One writer did
not, a running conversation's placeholder reached the model gateway verbatim,
and the turn died on `401 Virtual Key expected` — silently, because the vault's
substitution list is write-only and nothing can read back what was lost.

This gate asserts the property instead of banning a call: credential machinery
does not cross the seam. Two ways it can cross, both checked:

* platform (`astrabox/core/`, `astrabox/seams/`) or a DIFFERENT provider
  importing a provider's credential module;
* platform importing a vendor SDK's credential models directly.

The violations that predate the gate are listed in the baseline beside this
script, which may only shrink. Run it directly to see the arrears:

    .venv/bin/python scripts/check_credential_seam.py

Scope note: platform code also imports vendor FILESYSTEM models
(`opensandbox.models.filesystem`) in six places. Same shape, different domain,
and not this gate's business — it is named here so the omission is a decision
rather than an oversight.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "astrabox"
BASELINE_PATH = PACKAGE_ROOT / "seams" / "credential_seam_baseline.txt"

#: Where the platform lives. Code here states WHAT identity a workload spends;
#: how that is realised belongs to whichever provider is selected.
PLATFORM_ROOTS = ("core", "seams", "common", "api", "persistence", "config")

#: The vendor's credential model. Composing these in platform code hard-codes
#: one backend's idea of what a credential IS — a named entry plus a binding
#: whose substitution list is replaced whole. Another backend may inject
#: per-process and have neither.
VENDOR_CREDENTIAL_MODULES = {"opensandbox.models.sandboxes"}
VENDOR_CREDENTIAL_NAMES = {
    "Credential",
    "CredentialAuth",
    "CredentialBinding",
    "CredentialMatch",
    "CredentialProxyConfig",
    "CredentialSubstitution",
    "InlineCredentialSource",
}


def provider_package(module: str) -> str | None:
    """The provider a credential module belongs to, or None if it is not one.

    Matched on the module path rather than a list of known providers: a
    backend added tomorrow gets the same rule without editing this file.
    """
    parts = module.split(".")
    if len(parts) < 3 or parts[0] != "astrabox" or parts[1] != "providers":
        return None
    if not any(part.startswith("credential") for part in parts[2:]):
        return None
    return parts[2]


def owning_provider(path: Path) -> str | None:
    """The provider package a file belongs to, if any."""
    parts = path.relative_to(PACKAGE_ROOT).parts
    if len(parts) < 2 or parts[0] != "providers":
        return None
    return parts[1]


def is_platform(path: Path) -> bool:
    parts = path.relative_to(PACKAGE_ROOT).parts
    return bool(parts) and parts[0] in PLATFORM_ROOTS


def imported_module(node: ast.ImportFrom, path: Path) -> str:
    """Resolve `from . import x` against the file, so relative hops are seen."""
    if not node.level:
        return node.module or ""
    base = path.relative_to(REPO_ROOT).parent
    for _ in range(node.level - 1):
        base = base.parent
    prefix = ".".join(base.parts)
    return f"{prefix}.{node.module}" if node.module else prefix


def violations_in(path: Path) -> list[str]:
    """`<file>::<symbol>` for every credential import that crosses the seam.

    The whole tree is walked, not just module level: every leaked slot
    placeholder in this repository is a lazy import inside a function.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    rel = path.relative_to(REPO_ROOT).as_posix()
    home = owning_provider(path)
    platform = is_platform(path)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = imported_module(node, path)
        provider = provider_package(module)
        if provider is not None and provider != home:
            # A provider may use its own credential module; nobody else may.
            found.extend(f"{rel}::{alias.name}" for alias in node.names)
            continue
        if module in VENDOR_CREDENTIAL_MODULES and platform:
            found.extend(
                f"{rel}::{alias.name}"
                for alias in node.names
                if alias.name in VENDOR_CREDENTIAL_NAMES
            )
    return found


def read_baseline() -> set[str]:
    if not BASELINE_PATH.is_file():
        return set()
    return {
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def collect() -> set[str]:
    found: set[str] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        found.update(violations_in(path))
    return found


def main() -> int:
    found = collect()
    baseline = read_baseline()
    problems: list[tuple[str, list[str]]] = []

    arrivals = sorted(found - baseline)
    if arrivals:
        problems.append(
            (
                "credential machinery crossing the seam. State WHAT identity a "
                "workload spends through astrabox/seams/egress_credentials.py; "
                "leave HOW to the selected provider",
                arrivals,
            )
        )
    stale = sorted(baseline - found)
    if stale:
        problems.append(
            (
                "listed as arrears but no longer imported. Delete these "
                "baseline lines",
                stale,
            )
        )

    if not problems:
        print(f"credential seam: clean ({len(baseline)} arrears remaining)")
        return 0
    for heading, entries in problems:
        print(f"\n{heading}:")
        for entry in entries:
            print(f"  {entry}")
    print(f"\n{sum(len(entries) for _, entries in problems)} problem(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
