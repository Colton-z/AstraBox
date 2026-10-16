#!/usr/bin/env python3
"""Every background task is held by something that outlives it.

A task nobody holds is not merely untidy. The event loop keeps only a weak
reference, so it can be collected while it waits, and a task created inside a
request inherits that request's context and is cancelled when the request
returns. Both failures are silent and produce the same picture: work that was
started, never finished, and never complained.

``platform_service._spawn_background_task`` exists to fix exactly those two
things — it attaches a fresh ``contextvars.Context`` and keeps the task in a set
— but its existence does not prevent anyone from writing the bare form beside
it. This gate closes that shape.

The rule is about the PROPERTY rather than the call: the task must end up
somewhere. Most task creations here are correct structured concurrency — a task
assigned, then awaited or gathered in the same scope — so banning the primitive
would flag the right answers to catch the wrong one. Two shapes are exact enough
to enforce and cover discarding a task:

* created as a bare statement, its result dropped on the floor;
* assigned to a local name that is never read again in that function.

Neither has an exception list, because nothing legitimate needs one.
``_spawn_background_task`` itself passes: it keeps its task and returns it.

What this does not close: a task that IS held but still inherits a
request-scoped context. Whether a task outlives its request is not decidable
from the syntax, so that half stays a matter for review.

Run it directly:

    python scripts/check_background_tasks.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "astrabox"
SPAWNERS = frozenset({"create_task", "ensure_future"})


def _spawner(node: ast.AST) -> str | None:
    """The spawner this call names, through ``asyncio.`` or a bare import."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
        and func.attr in SPAWNERS
    ):
        return func.attr
    if isinstance(func, ast.Name) and func.id in SPAWNERS:
        return func.id
    return None


def offenders() -> tuple[list[str], list[str]]:
    """Tasks whose result is discarded, and tasks bound to a local nobody reads."""
    discarded: list[str] = []
    forgotten: list[str] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and (name := _spawner(node.value)):
                discarded.append(f"{where}:{node.lineno} {name}()")
        # Per function rather than per module: a task stored on ``self`` or in a
        # container is held by something outside the call; only a plain local
        # dies at the return.
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            read = {
                node.id
                for node in ast.walk(scope)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign) or not (name := _spawner(node.value)):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in read:
                        forgotten.append(
                            f"{where}:{node.lineno} {target.id} = {name}()  (in {scope.name})"
                        )
    return discarded, forgotten


def main() -> int:
    discarded, forgotten = offenders()
    if discarded:
        print(
            f"\n{len(discarded)} task(s) created and discarded. The loop keeps only a weak "
            "reference, so the task can be collected mid-wait, and it inherits the creating "
            "request's context, so it is cancelled when that request returns. Hand it to "
            "platform_service._spawn_background_task, or keep it and await it:"
        )
        for item in discarded:
            print(f"  {item}")
    if forgotten:
        print(
            f"\n{len(forgotten)} task(s) bound to a local that is never read again, which "
            "keeps the task alive only until the function returns:"
        )
        for item in forgotten:
            print(f"  {item}")
    if discarded or forgotten:
        print("\nbackground tasks FAILED")
        return 1
    print("background tasks OK — every created task is held or awaited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
