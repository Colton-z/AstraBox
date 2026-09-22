"""No astrabox module may import a PRIVATE (underscore) opensandbox module.

Underscore-private third-party API carries no stability contract: an import of
one is a patch release away from breaking the open_sandbox backend, and it
breaks at import time, in production, not here. The permitted surface is the
SDK's public contract only.

What the public surface imports internally is upstream's own business. This
census walks only ``astrabox/**`` and fails loudly on any direct private import.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "astrabox"


def _private_opensandbox_imports(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if module.startswith("opensandbox._") or (
                module == "opensandbox"
                and any(alias.name.startswith("_") for alias in node.names)
            ):
                hits.append(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("opensandbox._"):
                    hits.append(alias.name)
    return hits


def test_no_private_opensandbox_imports_anywhere() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _private_opensandbox_imports(tree)
        if hits:
            offenders[str(path.relative_to(PACKAGE_ROOT.parent))] = hits
    assert not offenders, (
        "private opensandbox modules imported (reach for the public surface "
        "instead — opensandbox.<public module>, and the pool shell's public "
        f"injection points): {offenders!r}"
    )
