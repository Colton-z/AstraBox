#!/usr/bin/env python3
"""Ask Playwright how many tests each e2e lane holds, and compare to the contract.

A lane round refuses at collection when the frozen inventory disagrees with what
Playwright finds, which is the right refusal in the wrong place: it costs a
deploy and a lane start to learn a number. Playwright can answer without a
deployment, a browser, or a sandbox — `--list` only loads the spec files — so
the same disagreement is available here in seconds.

The count has to come from Playwright rather than from counting `test(` lines.
A spec that declares tests inside a loop emits one per iteration, and no static
count of the source gets that right; the number is the producer's to state.

It also holds a second property, by running with none of the deployment's
environment set: listing must not need any. Playwright loads every file in a
lane to enumerate its tests, so one spec that reads a required variable at
module scope throws before any test exists, and the whole lane collects
nothing — the round then reports a count mismatch instead of the missing
variable. Read required environment inside the test or its fixture.

Needs the e2e-ui dependency tree (`npm --prefix tests/e2e-ui ci`), which is why
this runs inside `make typecheck-e2e` rather than as a bare static check.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "tests/e2e-contract/suite-contract.json"
E2E_UI = REPO_ROOT / "tests/e2e-ui"
NODE_TOOLCHAIN = REPO_ROOT / "scripts/node-toolchain.py"
TOTAL = re.compile(r"^Total:\s+(\d+)\s+tests?\s+in\s+(\d+)\s+files?", re.M)


def collect(label: str, selection: list[str]) -> tuple[int, int]:
    """What Playwright collects for a set of spec paths, as (tests, files).

    The files are enumerated by the caller and passed one by one, the way
    run-playwright.sh selects them. A positional argument to `playwright test`
    is a filter matched against the file path, not a shell glob, so handing it
    `specs/*.{lane}.spec.ts` selects nothing at all.
    """
    if not selection:
        raise SystemExit(f"nothing to collect for {label}")

    completed = subprocess.run(
        [
            sys.executable,
            str(NODE_TOOLCHAIN),
            "npx",
            "playwright",
            "test",
            "--list",
            *selection,
        ],
        cwd=E2E_UI,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ASTRABOX_E2E_BASE_URL": "http://collection.invalid"},
    )
    match = TOTAL.search(completed.stdout)
    if match is None:
        raise SystemExit(
            f"playwright could not list {label} (exit {completed.returncode}):\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
    tests, files = int(match.group(1)), int(match.group(2))
    # `Total: 0 tests in 0 files` parses as cleanly as any other count, so a
    # broken invocation would otherwise be reported as inventory drift and send
    # the reader to edit the contract. A lane that collects nothing is the
    # invocation being wrong, never the contract.
    if tests == 0 or files == 0:
        raise SystemExit(
            f"playwright collected nothing for {label} from "
            f"{len(selection)} files. The invocation is wrong, not the "
            f"contract (exit {completed.returncode}):\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
    return tests, files


def lane_specs(lane: str) -> list[str]:
    selection = sorted(
        str(path.relative_to(E2E_UI))
        for path in (E2E_UI / "specs").glob(f"*.{lane}.spec.ts")
    )
    if not selection:
        raise SystemExit(f"no spec files carry the {lane} suffix")
    return selection


def main() -> int:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    lanes = sorted(
        suffix
        for suffix, suite in contract["suites"].items()
        if suite["role"] == "e2e"
    )
    if not lanes:
        raise SystemExit("the suite contract registers no e2e lane")

    drift: list[str] = []
    for lane in lanes:
        specs = lane_specs(lane)
        tests, files = collect(f"the {lane} lane", specs)
        frozen = contract["playwright"][lane]
        print(f"{lane}: {tests} tests in {files} files")
        if tests != frozen["tests"] or files != frozen["files"]:
            drift.append(
                f"{lane}: playwright collects {tests} tests in {files} files, "
                f"the contract freezes {frozen['tests']} in {frozen['files']}"
            )

        # A lane that runs part of itself under one worker freezes that split
        # too, and the runner refuses when the halves do not sum. Checking only
        # the total would leave the round to discover a split that moved.
        serial = frozen.get("serial_files")
        if not serial:
            continue
        serial_tests, serial_files = collect(f"the {lane} serial half", list(serial))
        print(f"{lane} serial: {serial_tests} tests in {serial_files} files")
        if serial_tests != frozen["serial_tests"]:
            drift.append(
                f"{lane} serial half: playwright collects {serial_tests} tests, "
                f"the contract freezes {frozen['serial_tests']}"
            )
        if tests - serial_tests != frozen["parallel_tests"]:
            drift.append(
                f"{lane} parallel half: {tests - serial_tests} tests remain, "
                f"the contract freezes {frozen['parallel_tests']}"
            )
        if files - serial_files != frozen["parallel_files"]:
            drift.append(
                f"{lane} parallel half: {files - serial_files} files remain, "
                f"the contract freezes {frozen['parallel_files']}"
            )
    if drift:
        print(
            "\nE2E COLLECTION DRIFT — a lane round would refuse on this:",
            file=sys.stderr,
        )
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nUpdate playwright.<lane> in "
            f"{CONTRACT.relative_to(REPO_ROOT)} to the counts above, together with "
            "any test that restates those counts.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
