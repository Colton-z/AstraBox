"""Every runtime-subject seam call site binds to the coordinator it calls.

A keyword-only seam gives no compile-time tie between a caller and its callee,
and neither ruff nor mypy relates a call in one module to a signature in
another when the receiver is typed loosely. The break this guards is therefore
invisible to every static gate the repository runs, survives a full unit suite,
and appears only when a real Session starts: `publish_runtime_ready()` raising
`TypeError`, the Session settling TERMINATED before READY, and the sandbox that
was already built left with nothing pointing at it.

Binding is checked rather than the argument names being pinned, so a deliberate
change to the contract passes as soon as both sides move together, and only a
half-applied one fails.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from astrabox.core.service.orchestrator.runtime_subject import RuntimeSubjectCoordinator


REPO = Path(__file__).resolve().parents[1]
SESSION_KERNEL = REPO / "astrabox/core/service/orchestrator/session_kernel"
COORDINATOR_ATTRIBUTE = "_runtime_subjects"


def _seam_calls() -> list[tuple[Path, ast.Call, str]]:
    """Every `self._runtime_subjects.<method>(...)` under the session kernel."""

    calls: list[tuple[Path, ast.Call, str]] = []
    for path in sorted(SESSION_KERNEL.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            owner = function.value
            if isinstance(owner, ast.Attribute) and owner.attr == COORDINATOR_ATTRIBUTE:
                calls.append((path, node, function.attr))
    return calls


def test_the_seam_has_call_sites_to_check() -> None:
    """A guard that found nothing to inspect would pass on an empty repository."""

    assert len(_seam_calls()) >= 4


def test_every_seam_call_site_binds_to_the_coordinator_signature() -> None:
    mismatches: list[str] = []
    for path, node, method_name in _seam_calls():
        method = getattr(RuntimeSubjectCoordinator, method_name, None)
        relative = path.relative_to(REPO)
        if method is None:
            mismatches.append(
                f"{relative}:{node.lineno} calls "
                f"RuntimeSubjectCoordinator.{method_name}(), which does not exist"
            )
            continue
        keywords = {keyword.arg: None for keyword in node.keywords if keyword.arg is not None}
        # One extra positional stands in for `self`, which the AST call omits.
        positional = [None] * (len(node.args) + 1)
        try:
            inspect.signature(method).bind_partial(*positional, **keywords)
        except TypeError as exc:
            mismatches.append(f"{relative}:{node.lineno} {method_name}(): {exc}")
    assert not mismatches, "runtime-subject seam call sites drifted:\n  - " + "\n  - ".join(
        mismatches
    )


@pytest.mark.parametrize(
    "method_name",
    ["acquire_startup", "publish_runtime_ready", "converge_startup_failure"],
)
def test_the_coordinator_still_exposes_the_methods_startup_depends_on(method_name: str) -> None:
    """Renaming one is a contract change; the binding check alone would miss it
    if the caller were renamed in the same sweep that broke a different one."""

    assert callable(getattr(RuntimeSubjectCoordinator, method_name, None))
