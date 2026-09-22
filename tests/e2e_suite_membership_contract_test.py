"""Validate Playwright suite membership and lane resource constraints.

`run-playwright.sh --lane parallel` collects `specs/*.parallel.spec.ts`.
Every suffix must therefore be registered with a runnable suite; typechecking
alone cannot detect an uncollected spec.

Lane constraints preserve independent test results:

  · Budgets belong to the runner. Its timeout reporter terminates the process
    group, so a spec cannot independently extend its own execution window.
  · Sandbox destruction runs serially because the default Agent uses shared
    tenancy. Destroying its sandbox would also terminate sibling tests.
  · Workspace parking runs serially because committing the filesystem competes
    for host resources and must complete within the lane budget.
  · A sandbox-wide fault or sole-occupancy assertion needs a dedicated Agent.
    Shared conversations have distinct Linux users and nonempty isolation IDs;
    the container-wide workload user does not identify every conversation.
  · Readiness uses the deployment-configurable `waitForSessionReady` budget.
    Per-spec constants would apply different readiness criteria to one service.
  · Page readiness uses visible application state, not `networkidle`. Background
    polling makes network silence depend on poll timing rather than usability.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "tests/e2e-contract/suite-contract.json"
SPECS = REPO_ROOT / "tests/e2e-ui/specs"
MAKEFILE = REPO_ROOT / "Makefile"

SUFFIX = re.compile(r"\.(?P<suffix>[a-z][a-z0-9-]*)\.spec\.ts$")
OWN_BUDGET = re.compile(
    r"\btest\.setTimeout\s*\(|\b(?:testInfo|test\.info\(\))\.setTimeout\s*\("
    r"|\btest\.slow\s*\(|\btest\.describe\.configure\s*\([^)]*\btimeout\s*:",
    re.DOTALL,
)
NETWORK_IDLE = re.compile(r"['\"]networkidle['\"]")
# Two ways a spec says it needs a box of its own. Asserting it is the box's sole
# occupant is one. Reaching into the box as `$ASTRABOX_WORKLOAD_USER` is the
# other: that variable holds ONE value for the whole container, while a shared
# box gives each conversation its own Linux user, so the probe execs as somebody
# else and reads none of this session's processes.
SKIPS = re.compile(r"\btest\.skip\s*\(")
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
LINE_COMMENT = re.compile(r"//[^\n]*")


def executable(source: str) -> str:
    """The source with comments removed.

    A spec that documents at length why it does NOT `test.skip()` was counted as
    a skipper by a regex reading its prose. Two of the twenty-four frozen
    entries were that sentence. A measurement that reads comments as code
    reports a set nobody can act on."""
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", source))
CLAIMS_SOLE_OCCUPANCY = re.compile(
    r"isolated_session_id.{0,240}?\.toEqual\(\s*''\s*\)"
    r"|transcriptStoreProbe"
    r"|ASTRABOX_WORKLOAD_USER"
    r"|setSandboxEgressFault",
    re.DOTALL,
)
# Two routes to conversation tenancy, and a spec may take either: the helper
# that builds a throwaway Agent on the cold Environment, or naming the
# conversation-tenancy Environment itself. What is refused is taking neither and
# then asserting the property only those two produce.
# Only a spec that actually opens a conversation can be in the wrong box. One
# that drives the fixture against a fake API names the same helpers and touches
# no sandbox at all.
OPENS_A_CONVERSATION = re.compile(r"\bstartConversation\s*\(")
CONVERSATION_TENANCY = re.compile(
    r"\bcreateColdTestAgent\s*\(|ASTRABOX_E2E_PREWARM_CONVERSATION_ENVIRONMENT"
)
DESTROYS_A_SANDBOX = re.compile(
    r"\b(?:killSandbox|terminateSandbox|waitForSandboxStopped)\s*\("
)
OWN_READINESS_WAIT = re.compile(
    r"\bwaitForSessionReady\s*\([^)]*,\s*[0-9][0-9_]*\s*\)"
)
# Workspace parking commits the sandbox filesystem through a Kubernetes Job.
# Serial execution prevents other specs from competing for the host resources
# needed to complete that commit within the lane budget.
PARKS_A_WORKSPACE = re.compile(r"\bhibernateWorkspace\s*\(")


CONTRACT_JSON = json.loads(CONTRACT.read_text(encoding="utf-8"))


def registry() -> dict[str, dict[str, object]]:
    return CONTRACT_JSON["suites"]


def lane_violations(name: str, source: str) -> list[str]:
    """What disqualifies `source` from running in an e2e lane under `name`.

    Kept separate from the sweep so the tests below can hand it the exact
    source these rules exclude, not only the tree in front of them: a clean
    tree proves the tree is clean, not that the rule catches anything.
    """
    found: list[str] = []
    if match := OWN_BUDGET.search(source):
        line = source.count("\n", 0, match.start()) + 1
        found.append(f"{name}:{line} names its own test budget")
    if match := NETWORK_IDLE.search(source):
        line = source.count("\n", 0, match.start()) + 1
        found.append(f"{name}:{line} waits for networkidle")
    if match := OWN_READINESS_WAIT.search(source):
        line = source.count("\n", 0, match.start()) + 1
        found.append(f"{name}:{line} names its own session-readiness wait")
    if (
        (match := CLAIMS_SOLE_OCCUPANCY.search(source))
        and OPENS_A_CONVERSATION.search(source)
        and not CONVERSATION_TENANCY.search(source)
    ):
        line = source.count("\n", 0, match.start()) + 1
        found.append(
            f"{name}:{line} claims sole occupancy of its box without "
            "createColdTestAgent"
        )
    return found


def test_every_spec_carries_a_registered_suffix() -> None:
    known = set(registry())
    orphans = [
        path.name
        for path in sorted(SPECS.glob("*.spec.ts"))
        if (match := SUFFIX.search(path.name)) is None
        or match.group("suffix") not in known
    ]
    assert orphans == [], (
        "these specs carry a suffix no suite collects, so nothing runs them: "
        f"{orphans}. Register the suffix in {CONTRACT.name} with the command "
        "that runs it, or rename the file into an existing suite."
    )


def test_every_registered_suite_has_files_and_a_reachable_entry_point() -> None:
    """A registered suite must hold specs, and its entry point must exist.

    Only a lane's inventory is frozen, under `playwright.<lane>.files`, which
    is what the runner checks with --expected-files. The registry deliberately
    repeats no count: adding a console audit is not an inventory change, and a
    number kept in two files is a number that drifts.
    """
    for suffix, entry in sorted(registry().items()):
        found = sorted(path.name for path in SPECS.glob(f"*.{suffix}.spec.ts"))
        assert found, f"the {suffix} suite is registered but holds no specs"
        if entry["role"] == "e2e":
            frozen = CONTRACT_JSON["playwright"][suffix]["files"]
            assert len(found) == frozen, (
                f"the {suffix} lane holds {len(found)} files, the frozen "
                f"inventory says {frozen}: {found}"
            )
        command = str(entry["entry"])
        if command.startswith("make "):
            target = command.split()[1]
            assert re.search(rf"^{re.escape(target)}:", MAKEFILE.read_text(encoding="utf-8"), re.M), (
                f"the {suffix} suite names `{command}`, which the Makefile does not define"
            )
        elif command.startswith("run-playwright.sh --lane "):
            lane = command.split()[-1]
            assert lane == suffix, f"lane {lane} must collect its own suffix {suffix}"


def test_no_lane_spec_names_its_own_budget_or_waits_for_network_silence() -> None:
    lanes = [
        suffix for suffix, entry in registry().items() if entry["role"] == "e2e"
    ]
    assert lanes, "the contract registers no e2e lane"
    offenders: list[str] = []
    for suffix in sorted(lanes):
        for path in sorted(SPECS.glob(f"*.{suffix}.spec.ts")):
            offenders += lane_violations(
                path.name, path.read_text(encoding="utf-8")
            )
    assert offenders == [], (
        "lane specs must fail on their own within the shared budget: "
        f"{offenders}"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The exact two lines the console audits carried into the parallel
        # lane. The first ran to the 180s wall and took the lane's remaining
        # tests with it; the second is the readiness wait that reported four
        # separate reds.
        ("test('x', async ({ page }) => {\n  test.setTimeout(170_000);\n});\n", "budget"),
        ("await page.waitForLoadState('networkidle');\n", "networkidle"),
        ("test.describe.configure({ timeout: 170_000 });\n", "budget"),
        ("test.slow();\n", "budget"),
        ("await api.waitForSessionReady(sessionId, 30_000);\n", "readiness"),
        ("await api.waitForSessionReady(id, 90000);\n", "readiness"),
        (
            "const agent = await api.defaultAgent();\n"
            "const session = await api.startConversation(agent.agent_id);\n"
            "expect(\n"
            "  String(identity?.isolated_session_id || '').trim(),\n"
            "  'this fault must own the whole runner process',\n"
            ").toEqual('');\n",
            "sole occupancy",
        ),
    ],
)
def test_the_lane_rules_refuse_the_source_that_made_them_necessary(
    source: str, expected: str
) -> None:
    found = lane_violations("candidate.parallel.spec.ts", source)
    assert len(found) == 1, found
    assert expected in found[0]


def test_the_lane_rules_pass_a_spec_that_only_uses_the_shared_budget() -> None:
    source = (
        "test('a conversation answers', async ({ page }) => {\n"
        "  await page.goto('/console');\n"
        "  const timer = setTimeout(() => controller.abort(), 5_000);\n"
        "  await expect(page.getByRole('main')).toBeVisible();\n"
        "});\n"
    )
    assert lane_violations("candidate.parallel.spec.ts", source) == []


def test_a_spec_that_destroys_a_sandbox_runs_in_the_serial_group() -> None:
    """A killed box takes its cohabitants with it, so it may not have any.

    The Agent these specs use holds one box for all its conversations. With five
    workers that is five conversations in one box, and a spec that kills it ends
    the other four — reported against whichever of them noticed first, which is
    never the spec that did it.
    """
    exclusive = CONTRACT_JSON["playwright"]["exclusive"]
    serial = set(exclusive["serial_files"])
    shared: list[str] = []
    for path in sorted(SPECS.glob("*.exclusive.spec.ts")):
        if not DESTROYS_A_SANDBOX.search(path.read_text(encoding="utf-8")):
            continue
        entry = f"specs/{path.name}"
        if entry not in serial:
            shared.append(entry)

    assert shared == [], (
        "these specs destroy a sandbox their cohabitants are still using; "
        f"add them to the contract's serial_files: {shared}"
    )


def test_a_spec_that_parks_a_workspace_runs_in_the_serial_group() -> None:
    """A park is an image commit, and it does not fit the wall while sharing.

    The commit is a Job that reads the box's filesystem; the push at the end of
    it is seconds. Sharing the machine with four other lane workers stretched
    the whole park to 3m45s, past a 180s wall the lane pins exactly — so the
    spec never reached its own assertions and, being early in the parallel
    group, took the rest of that group's run with it.
    """
    exclusive = CONTRACT_JSON["playwright"]["exclusive"]
    serial = set(exclusive["serial_files"])
    shared: list[str] = []
    for path in sorted(SPECS.glob("*.exclusive.spec.ts")):
        if not PARKS_A_WORKSPACE.search(path.read_text(encoding="utf-8")):
            continue
        entry = f"specs/{path.name}"
        if entry not in serial:
            shared.append(entry)

    assert shared == [], (
        "these specs commit a workspace image while four other workers compete "
        f"for the same machine; add them to the contract's serial_files: {shared}"
    )


def test_the_serial_group_and_the_parallel_group_add_up() -> None:
    """The split has to be a partition, not two independently edited numbers."""
    exclusive = CONTRACT_JSON["playwright"]["exclusive"]
    assert (
        exclusive["parallel_files"] + len(exclusive["serial_files"])
        == exclusive["files"]
    )
    assert exclusive["parallel_tests"] + exclusive["serial_tests"] == exclusive["tests"]


def test_the_specs_that_can_skip_are_frozen() -> None:
    """The lane's own success criterion is "zero skip", and a third of it can.

    `run-playwright.sh` reports a lane COMPLETE only when every selected test
    "passed exactly once ... with zero skip or retry", so a spec that skips ends
    the round. Twenty-four exclusive specs skip when the model declines to reach
    the state they probe -- a gated Write that the deployment's model answered
    without asking, a background Agent it chose not to spawn. Each one is a
    round decided by the model rather than by the platform.

    Whether the lane should tolerate a declared set of those, or whether they
    should be made deterministic, or moved to a suite whose runner is not a
    release lane, is a decision about the suite. What this pins is the size of
    it: the set may shrink, and a twenty-fifth cannot be added without saying so.
    """
    frozen = set(CONTRACT_JSON["model_conditional_skips"])
    present = {
        f"specs/{path.name}"
        for suffix in sorted(registry())
        if registry()[suffix]["role"] == "e2e"
        for path in SPECS.glob(f"*.{suffix}.spec.ts")
        if SKIPS.search(executable(path.read_text(encoding="utf-8")))
    }

    assert present <= frozen, (
        "these lane specs newly skip, and a skip ends the round: "
        f"{sorted(present - frozen)}"
    )
    assert present == frozen, (
        "these specs no longer skip — remove them from the contract's "
        f"model_conditional_skips: {sorted(frozen - present)}"
    )


WHAT_LINE = re.compile(r"^\s*what:\s*(?P<message>.+?),\s*$", re.MULTILINE)
IDENTIFIER = re.compile(r"\$\{\s*([A-Za-z_$][\w$]*)")
DECLARED = "const {name}", "let {name}", "var {name}", "function {name}", " {name},", " {name} }}"


def test_an_insist_message_names_only_identifiers_that_still_exist() -> None:
    """A message that outlived the variable it reads is a ReferenceError.

    Converting the probe-and-skip sites replaced the span between the probe and
    the skip, and one of those spans held a diagnostic `const` that the skip's
    message interpolated. The message survived the replacement and the variable
    did not, so the spec threw `probed is not defined` at the exact moment it
    was trying to explain why the model had declined — a failure that reads like
    the spec's subject and is not.

    Only `${...}` interpolations are checked: they are what a replaced span
    takes away, and a bare word inside a string is prose.
    """
    dangling: list[str] = []
    for suffix in sorted(registry()):
        if registry()[suffix]["role"] != "e2e":
            continue
        for path in sorted(SPECS.glob(f"*.{suffix}.spec.ts")):
            source = path.read_text(encoding="utf-8")
            if "insist<" not in source:
                continue
            for match in WHAT_LINE.finditer(source):
                for name in IDENTIFIER.findall(match.group("message")):
                    if any(
                        pattern.format(name=name) in source for pattern in DECLARED
                    ):
                        continue
                    line = source.count("\n", 0, match.start()) + 1
                    dangling.append(f"{path.name}:{line} reads `{name}`, which is not declared")

    assert dangling == [], (
        "an insist message reads an identifier no longer in scope: " f"{dangling}"
    )
