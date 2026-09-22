#!/usr/bin/env python3
"""Every error code AstraBox raises must have a row in the registry.

A code with no row is answered by `error_spec`'s fallback — `category=
"unregistered"`, `owner="unknown"` — and both reach the client in the error
envelope. The fallback is honest, but it says nothing: a caller cannot tell
whether to fix its request, wait, or call an administrator.

This gate stops new codes from arriving without a row. The codes that predate it
are listed in the baseline beside this script, which may only shrink.

Run it directly to see the arrears:

    .venv/bin/python scripts/check_error_registry.py

The design, including why the collector works the way it does and what the
burn-down order is, is in `docs/maintainers/error-code-registry-gate.md`.
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "astrabox"
BASELINE_PATH = PACKAGE_ROOT / "common" / "utils" / "error_registry_baseline.txt"

sys.path.insert(0, str(REPO_ROOT))

from astrabox.common.utils.errors import _ERROR_SPECS  # noqa: E402

# Four constructions reach the error envelope. A checker that knows only the
# first two reports REQUEST_CANCELLED as an unraised row: route handlers that
# return rather than raise build the envelope through `error_response`.
CODE_KEYWORDS = ("code", "error_code")
POSITIONAL_CODE_ARG = {
    "APIError": 0,
    "make_api_error": 0,
    "error_response": 0,
    "_ensure_command_success": 1,
}

# Codes that reach an envelope without appearing as a literal at a construction
# site. The in-box sidecar sends the first two; runtime-binding resolution
# forwards the third through ``APIError(code=resolution.reason_code)``. Declared
# because the static collector cannot distinguish them from an unused row.
NON_LITERAL_CODES = frozenset(
    {
        "SIDECAR_ATTACH_IDENTITY_INVALID",
        "SIDECAR_ATTACH_IDENTITY_MISMATCH",
        "ASSISTANT_WORKSPACE_NOT_READY",
    }
)


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Upper-case module-level names bound to a string literal."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.target, ast.Name)
        ):
            names = [node.target.id]
        else:
            continue
        if not isinstance(node.value.value, str):
            continue
        for name in names:
            if name.isupper():
                constants[name] = node.value.value
    return constants


def _imported_constants(
    tree: ast.Module, path: Path, all_constants: dict[Path, dict[str, str]]
) -> dict[str, str]:
    """Upper-case names this module imports, resolved one hop to their source.

    One hop and no further. A checker that chased re-exports would need a real
    import graph, and every module that defines a code defines it as a literal.
    """
    resolved: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        wanted = [a.name for a in node.names if a.asname is None and a.name.isupper()]
        if not wanted:
            continue
        source = _module_path(node, path)
        if source is None:
            continue
        for name in wanted:
            value = all_constants.get(source, {}).get(name)
            if value is not None:
                resolved[name] = value
    return resolved


def _module_path(node: ast.ImportFrom, importer: Path) -> Path | None:
    if node.level:
        base = importer.parent
        for _ in range(node.level - 1):
            base = base.parent
        parts = (node.module or "").split(".") if node.module else []
    elif (node.module or "").startswith("astrabox."):
        base = REPO_ROOT
        parts = (node.module or "").split(".")
    else:
        return None
    candidate = base.joinpath(*parts) if parts else base
    for guess in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if guess.is_file():
            return guess
    return None


def _code_arguments(call: ast.Call) -> list[ast.expr]:
    """The expressions that could name an error code in one call."""
    found = [kw.value for kw in call.keywords if kw.arg in CODE_KEYWORDS]
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    index = POSITIONAL_CODE_ARG.get(name or "")
    if index is not None and len(call.args) > index:
        found.append(call.args[index])
    return found


def collect(root: Path = PACKAGE_ROOT) -> tuple[dict[str, set[str]], list[str]]:
    """Return the codes raised under `root`, and the sites that forward one."""
    sources = sorted(p for p in root.rglob("*.py"))
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in sources}
    all_constants = {p: _module_constants(t) for p, t in trees.items()}

    raised: dict[str, set[str]] = defaultdict(set)
    forwarded: list[str] = []
    for path, tree in trees.items():
        if path.name == "errors.py" and path.parent.name == "utils":
            continue  # the registry itself; its literals are rows, not raises
        names = {**all_constants[path], **_imported_constants(tree, path, all_constants)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for argument in _code_arguments(node):
                where = f"{path.name}:{node.lineno}"
                if path.is_relative_to(REPO_ROOT):
                    where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    raised[argument.value].add(where)
                elif isinstance(argument, ast.Name) and argument.id in names:
                    raised[names[argument.id]].add(where)
                else:
                    forwarded.append(where)
    return raised, forwarded


def read_baseline() -> set[str]:
    if not BASELINE_PATH.is_file():
        return set()
    lines = BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def evaluate(
    raised: dict[str, set[str]], registered: set[str], baseline: set[str]
) -> list[tuple[str, list[str]]]:
    """The four rules that make the baseline a ratchet rather than an allowlist.

    Registering a code is only half of clearing it; the second, third and fourth
    rules are what stop the file from recording the arrears of the day it was
    written for ever.
    """
    raised_codes = set(raised)
    failures: list[tuple[str, list[str]]] = []

    unregistered = sorted(raised_codes - registered - baseline)
    if unregistered:
        failures.append(
            (
                "raised with no registry row. Add a row to "
                "astrabox/common/utils/errors.py giving (status, category, "
                "retryable, owner), read off the raise site",
                [f"{code}  ({sorted(raised[code])[0]})" for code in unregistered],
            )
        )

    settled = sorted(raised_codes & registered & baseline)
    if settled:
        failures.append(
            ("registered and still listed as arrears. Delete these baseline lines", settled)
        )

    stale = sorted(baseline - raised_codes)
    if stale:
        failures.append(
            (
                "listed as arrears but raised nowhere. Delete these baseline lines",
                stale,
            )
        )

    dead = sorted(registered - raised_codes - NON_LITERAL_CODES)
    if dead:
        failures.append(
            (
                "has a registry row and no raise site. Delete the row, or add the "
                "code to RECEIVED_CODES in this script if it arrives from outside",
                dead,
            )
        )
    return failures


def main() -> int:
    raised, forwarded = collect()
    registered = set(_ERROR_SPECS)
    baseline = read_baseline()
    raised_codes = set(raised)
    failures = evaluate(raised, registered, baseline)

    for reason, codes in failures:
        print(f"\n{len(codes)} code(s) {reason}:")
        for code in codes:
            print(f"  {code}")

    if failures:
        print(
            f"\nerror registry FAILED — {len(registered)} registered, "
            f"{len(baseline)} in baseline, {len(raised_codes)} raised"
        )
        return 1

    print(
        f"error registry OK — {len(registered)} registered, "
        f"{len(baseline)} unregistered in baseline (arrears), "
        f"{len(raised_codes)} raised, {len(forwarded)} forwarded site(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
