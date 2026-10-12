#!/usr/bin/env python3
"""Platform code reaches sandboxes only through the sandbox seam.

`astrabox/seams/sandbox.py` and `astrabox/seams/sandbox_data.py` declare every
face of a box the platform uses: lifecycle, endpoints, files, commands and
long-lived processes. A provider implements them; the built-in OpenSandbox
provider does so with the OpenSandbox SDK and the execd in every image. When
platform code imports that SDK, that provider's package or the built-in
sandbox server's launcher itself, the seam stops being the contract: the code
works against the one backend whose objects happen to have that shape and
fails on any other, and nothing says so until a second backend is installed.

This gate asserts the property rather than banning one call: no module under
`astrabox/core`, `astrabox/api` or `astrabox/persistence` imports
`opensandbox`, `astrabox.providers.open_sandbox` or
`astrabox.deploy.sandbox_server` — at module level, inside a function, or by
name through `importlib.import_module`. The neutral execd implementation in
`astrabox.providers.execd` is the image's contract, not a provider's, and
stays importable. Run it directly to see any violation:

    .venv/bin/python scripts/check_sandbox_seam.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "astrabox"

#: Where the platform lives.
PLATFORM_ROOTS = ("core", "api", "persistence")

#: One backend's SDK, that backend's provider package, and the launcher of the
#: sandbox server the built-in distribution runs.
FORBIDDEN = (
    "opensandbox",
    "astrabox.providers.open_sandbox",
    "astrabox.deploy.sandbox_server",
)


def forbidden(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN)


def imported_module(node: ast.ImportFrom, path: Path) -> str:
    """Resolve `from . import x` against the file, so relative hops are seen."""
    if not node.level:
        return node.module or ""
    base = path.relative_to(REPO_ROOT).parent
    for _ in range(node.level - 1):
        base = base.parent
    prefix = ".".join(base.parts)
    return f"{prefix}.{node.module}" if node.module else prefix


def imported_by_name(node: ast.Call) -> str | None:
    """The literal module an `import_module(...)` or `__import__(...)` call names."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in {"import_module", "__import__"} or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def violations_in(path: Path) -> list[str]:
    """`<file>:<line>::<module>` for every forbidden import in one platform file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    rel = path.relative_to(REPO_ROOT).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = imported_module(node, path)
            modules = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call):
            named = imported_by_name(node)
            modules = [named] if named else []
        hit = next((module for module in modules if forbidden(module)), None)
        if hit is not None:
            found.append(f"{rel}:{node.lineno}::{hit}")
    return found


def collect(package_root: Path = PACKAGE_ROOT) -> list[str]:
    found: list[str] = []
    for root in PLATFORM_ROOTS:
        for path in sorted((package_root / root).rglob("*.py")):
            found.extend(violations_in(path))
    return found


def main() -> int:
    found = collect()
    if not found:
        print(f"sandbox seam: clean ({', '.join(PLATFORM_ROOTS)} import no backend)")
        return 0
    print(
        "platform code importing a sandbox backend's SDK, provider package or "
        "server launcher. Reach the box through astrabox/seams/sandbox.py and "
        "astrabox/seams/sandbox_data.py instead:"
    )
    for entry in found:
        print(f"  {entry}")
    print(f"\n{len(found)} problem(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
