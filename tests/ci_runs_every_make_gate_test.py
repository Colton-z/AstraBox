"""Every check `make` runs is also run on a pull request.

CI does not invoke the Makefile; it restates the recipe in YAML. A restatement
drifts, and this one drifts in the direction nobody notices: a check wired into
`make` and missing from the workflow passes locally for whoever remembers to run
it and is absent from every PR. Nothing reports that — the job is green, because
the step is not there to fail.

The failure is silent and permanent, so it is worth a structural check rather
than a reviewer's memory. This compares the two lists by reading both files: the
`scripts/*.py` checks the Makefile's gate targets invoke, against the ones the
workflows mention anywhere. Membership is the whole assertion — WHICH job runs a
check, and in what order, is a matter for whoever tunes CI.

It does not check the reverse direction. A workflow running something `make`
does not is a deliberate choice fairly often (a slow check kept out of the local
loop), and asserting it would turn a judgement call into a failure.
"""

from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github" / "workflows"

#: Digits included on purpose. `check_i18n.py` is the reason: a pattern of
#: `[a-z_]+` silently omits it, and a census that quietly drops a row reads
#: exactly like a clean one.
_SCRIPT = re.compile(r"scripts/[a-z0-9_]+\.py")


def _checks_in(text: str) -> set[str]:
    return {match for match in _SCRIPT.findall(text) if "check_" in match}


def test_every_check_make_runs_is_also_run_in_ci() -> None:
    from_make = _checks_in((_ROOT / "Makefile").read_text(encoding="utf-8"))
    from_ci = set()
    for workflow in sorted(_WORKFLOWS.glob("*.yml")):
        from_ci |= _checks_in(workflow.read_text(encoding="utf-8"))

    assert from_make, "the Makefile must invoke at least one check, or this reads nothing"
    missing = sorted(from_make - from_ci)
    assert missing == [], (
        "these checks run in `make` and in no workflow, so they never run on a pull "
        "request and their absence cannot fail anything:\n  "
        + "\n  ".join(missing)
        + "\nAdd a step for each, or remove it from the Makefile if it is not a gate."
    )


def test_the_comparison_can_still_tell_the_two_apart() -> None:
    """A set difference over two empty sets is also empty.

    Both halves are read from files, so a rename or a moved workflow directory
    would leave this passing over nothing at all. Pinning that each side finds
    the checks it should is what keeps the green above meaningful.
    """
    from_make = _checks_in((_ROOT / "Makefile").read_text(encoding="utf-8"))
    assert "scripts/check_i18n.py" in from_make
    assert "scripts/check_palette.py" in from_make

    ci_text = "".join(
        workflow.read_text(encoding="utf-8") for workflow in sorted(_WORKFLOWS.glob("*.yml"))
    )
    assert "scripts/check_comment_style.py" in _checks_in(ci_text)

    # And a check present in one and not the other is reported rather than
    # absorbed — the case this file exists for.
    assert sorted({"scripts/check_a.py", "scripts/check_b.py"} - {"scripts/check_a.py"}) == [
        "scripts/check_b.py"
    ]
