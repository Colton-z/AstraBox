"""Every background task is held by something that outlives it.

A task nobody holds is not merely untidy. The event loop keeps only a weak
reference, so it can be collected while it waits, and a task created inside a
request inherits that request's context and is cancelled when the request
returns. Both failures are silent and produce the same picture: work that was
started, never finished, and never complained.

`platform_service._spawn_background_task` exists to fix exactly those two
things — it attaches a fresh `contextvars.Context` and keeps the task in a set —
but its existence does not prevent anyone from writing the bare form beside it.
That is the shape this file closes: the facility being there is not the same as
the route around it being shut.

WHAT IS CHECKED, AND WHY IT IS NOT "DO NOT CALL create_task". Most of this
tree's ~35 task creations are correct structured concurrency: a task assigned,
then awaited or gathered in the same scope. Banning the primitive would flag
thirty right answers to catch one wrong one, and a check that guesses is worse
than none — a rule nobody can satisfy gets an allowlist, and an allowlist gets
appended to.

So the rule is about the PROPERTY rather than the call: the task must end up
somewhere. Two shapes are exact enough to enforce and cover discarding it:

  * created as a bare statement, its result dropped on the floor;
  * assigned to a local name that is never read again in that function.

Neither has an exception list, because nothing legitimate needs one —
`_spawn_background_task` itself passes, since it keeps its task and returns it.

WHAT THIS DOES NOT CLOSE: a task that IS held but still inherits a
request-scoped context. Whether a given task outlives its request is not
decidable from the syntax, so that half stays a matter for review. Claiming
otherwise would be the more comfortable sentence and the false one.
"""

from __future__ import annotations

import ast
import pathlib

_SPAWNERS = {"create_task", "ensure_future"}
_ROOT = pathlib.Path(__file__).resolve().parents[1] / "astrabox"


def _spawn_call_name(node: ast.AST) -> str | None:
    """The spawner this call names, whether via `asyncio.` or a bare import."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "asyncio"
        and func.attr in _SPAWNERS
    ):
        return func.attr
    if isinstance(func, ast.Name) and func.id in _SPAWNERS:
        return func.id
    return None


def _modules() -> list[tuple[pathlib.Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(_ROOT.rglob("*.py"))
    ]


def _discarded_results() -> list[str]:
    """Tasks created as a statement — the result goes nowhere at all."""
    found = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            name = _spawn_call_name(node.value)
            if name:
                found.append(f"{path.relative_to(_ROOT.parent)}:{node.lineno} {name}()")
    return found


def _assigned_then_forgotten() -> list[str]:
    """Tasks bound to a local name that the function never reads again.

    Scoped per function rather than per module: a name stored on `self` or in a
    container is held by something outside the call, and only a plain local
    dies at the return.
    """
    found = []
    for path, tree in _modules():
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            read = {
                node.id
                for node in ast.walk(scope)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
            }
            for node in ast.walk(scope):
                if not isinstance(node, ast.Assign):
                    continue
                name = _spawn_call_name(node.value)
                if not name:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in read:
                        found.append(
                            f"{path.relative_to(_ROOT.parent)}:{node.lineno} "
                            f"{target.id} = {name}()  (in {scope.name})"
                        )
    return found


def test_no_background_task_is_created_and_discarded() -> None:
    offenders = _discarded_results()
    assert offenders == [], (
        "a task whose result is discarded is held by nothing: the loop keeps only a "
        "weak reference, so it can be collected mid-wait, and it inherits the "
        "creating request's context, so it is cancelled when that request returns. "
        "Hand it to platform_service._spawn_background_task, or keep it and await "
        "it.\n  " + "\n  ".join(offenders)
    )


def test_no_background_task_is_bound_to_a_name_nobody_reads() -> None:
    offenders = _assigned_then_forgotten()
    assert offenders == [], (
        "binding a task to a local that is never read again keeps it alive only "
        "until the function returns, which is the same fire-and-forget with an "
        "extra line.\n  " + "\n  ".join(offenders)
    )


def test_the_check_catches_the_shape_it_was_written_for() -> None:
    """Exercise the detector independently of the current source tree.

    The discarded-task sample is the positive control for the syntax scan, so
    an empty production scan cannot make a detector that misses this shape pass.
    """
    sample = ast.parse(
        "import asyncio\n"
        "async def send():\n"
        "    asyncio.ensure_future(drain(stream))\n"
        "    return {'state': 'SUBMITTED'}\n"
    )
    statements = [
        node
        for node in ast.walk(sample)
        if isinstance(node, ast.Expr) and _spawn_call_name(node.value)
    ]
    assert len(statements) == 1

    # And the structured form beside it must NOT match, or the rule would flag
    # the thirty places that are right.
    structured = ast.parse(
        "import asyncio\n"
        "async def run():\n"
        "    task = asyncio.create_task(work())\n"
        "    return await task\n"
    )
    assert [
        node
        for node in ast.walk(structured)
        if isinstance(node, ast.Expr) and _spawn_call_name(node.value)
    ] == []
    assert _spawn_call_name(structured.body[1].body[0].value) == "create_task"
