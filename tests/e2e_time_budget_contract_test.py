"""One hard three-minute budget for every live E2E test."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import conftest as live_conftest


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = REPO_ROOT / "tests/e2e-contract/suite-contract.json"
PLAYWRIGHT_SPEC_ROOTS = (
    REPO_ROOT / "tests/e2e-ui/specs",
    REPO_ROOT / "e2e/specs",
)
PYTHON_LIVE = REPO_ROOT / "tests/e2e"
# A declared budget, with the milliseconds it declares where the call names a
# literal. `test.slow` names none: it triples whatever the config set, which is
# above the wall by construction.
PLAYWRIGHT_BUDGET_DECLARATIONS = (
    re.compile(r"\btest\.setTimeout\s*\(\s*([^)]*)\)"),
    re.compile(r"\b(?:testInfo|test\.info\(\))\.setTimeout\s*\(\s*([^)]*)\)"),
    re.compile(r"\btest\.describe\.configure\s*\([^)]*\btimeout\s*:\s*([^,}]*)", re.DOTALL),
    re.compile(r"\btest\.slow\s*\(([^)]*)\)"),
)
LITERAL_MS = re.compile(r"^[0-9_]+$")


def test_live_e2e_budget_is_exactly_three_minutes() -> None:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert contract["max_test_seconds"] == 180
    # The watchdog's whole purpose is to trail the per-test timeout. A zero
    # grace puts it back on the same instant, where it races.
    assert contract["watchdog_grace_seconds"] >= 5


def budget_overrun(source: str, limit_ms: int) -> tuple[int, str] | None:
    """The first declared budget in `source` that reaches past `limit_ms`.

    The wall is absolute: the budget reporter kills the process group at
    `max_test_seconds` whatever a spec declares, so a spec that names a longer
    budget is stating something the run will not honour. A spec that names a
    SHORTER one is not bypassing anything — it is failing sooner than it is
    allowed to, which is the direction this contract wants. The distinction
    matters because the two look identical to a rule that just greps for the
    call, and a gate that cannot tell them apart gets worked around rather
    than obeyed.

    Whether a lane spec may name a budget at all is a separate, stricter rule
    that belongs to the lanes — see e2e_suite_membership_contract_test.py.
    """
    for pattern in PLAYWRIGHT_BUDGET_DECLARATIONS:
        for match in pattern.finditer(source):
            declared = match.group(1).strip()
            line = source.count("\n", 0, match.start()) + 1
            if not LITERAL_MS.match(declared):
                return line, f"declares a budget this gate cannot read: {match.group(0)!r}"
            if int(declared.replace("_", "")) > limit_ms:
                return line, f"declares {declared}ms, past the {limit_ms}ms wall"
    return None


def test_playwright_specs_cannot_declare_a_budget_past_the_wall() -> None:
    limit_ms = json.loads(CONTRACT.read_text(encoding="utf-8"))["max_test_seconds"] * 1000
    offenders: list[str] = []
    for root in PLAYWRIGHT_SPEC_ROOTS:
        paths = sorted(root.rglob("*.ts"))
        assert paths, f"no Playwright specs found under {root}"
        for path in paths:
            found = budget_overrun(path.read_text(encoding="utf-8"), limit_ms)
            if found is not None:
                line, why = found
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{line} {why}")
    assert offenders == [], f"Playwright budgets past the 180s contract: {offenders}"


def test_the_budget_rule_separates_reaching_past_the_wall_from_tightening() -> None:
    """Fed both directions, because only one of them is the defect.

    A spec that declares 170_000 or 120_000 is asking to fail sooner than the
    contract allows, which is the direction the contract wants. A rule that
    greps for the call reports those as bypasses, and reports the real ones —
    `test.slow()`, a computed budget, anything past the wall — identically.
    """
    assert budget_overrun("test.setTimeout(170_000);", 180_000) is None
    assert budget_overrun("test.setTimeout(120_000);", 180_000) is None
    assert budget_overrun("test.setTimeout(180_000);", 180_000) is None

    over = budget_overrun("test.setTimeout(240_000);", 180_000)
    assert over is not None and "past the 180000ms wall" in over[1]

    slow = budget_overrun("test.slow();", 180_000)
    assert slow is not None and "cannot read" in slow[1]

    computed = budget_overrun("test.setTimeout(BUDGET * 2);", 180_000)
    assert computed is not None and "cannot read" in computed[1]

    configured = budget_overrun("test.describe.configure({ timeout: 300_000 });", 180_000)
    assert configured is not None and "past the" in configured[1]


def test_background_sendability_releases_work_after_the_foreground_dispatch() -> None:
    source = (
        REPO_ROOT
        / "tests/e2e-ui/specs/"
        "background-agent-keeps-page-sendable-while-background-visible.exclusive.spec.ts"
    ).read_text(encoding="utf-8")

    prompt_gate = source.index("while not release.exists():")
    foreground_dispatch = source.index(
        "const duringForeground = await waitForUserMessageConsumedWhileBackgroundOpen"
    )
    release = source.index(
        "await api.runTerminalCommand(sessionId, `touch ${releasePath}`"
    )
    foreground_reply = source.index(
        "const [foregroundAssistant] = await Promise.all"
    )
    settled = source.index(
        "const completedChildren = "
        "await waitForBackgroundSettledWithCompletedSubagents"
    )
    bash_proof = source.index(
        "const childTranscript = await api.getChildRunMessages"
    )

    assert prompt_gate < foreground_dispatch < release < foreground_reply < settled < bash_proof
    assert "return `/workspace/.astrabox-e2e-sendable-${runId}.release`;" in source
    assert "the completion marker must come from Bash stdout" in source
    assert "liveDrawer,\n      'the drawer opened" not in source
    assert "sleepSeconds: 90" not in source
    assert "time.sleep(90)" not in source


def test_hibernate_release_marker_outlives_opensandbox_upload_completion() -> None:
    source = (
        REPO_ROOT
        / "tests/e2e-ui/specs/"
        "hibernate-wake-noop-during-live-turn.exclusive.spec.ts"
    ).read_text(encoding="utf-8")

    gate = source.index("while not release.is_file():")
    upload = source.index("await api.uploadFileText(sessionId, '/workspace'")
    completion = source.index("const settled = await api.waitForSessionReady")

    assert gate < upload < completion
    assert "OpenSandbox execd exposes an uploaded file before applying" in source
    assert "release.unlink(missing_ok=True)" not in source


def test_round_runner_cannot_replace_the_budget_reporter() -> None:
    source = (REPO_ROOT / "tests/e2e-ui/run-round.mjs").read_text(encoding="utf-8")
    assert "'--reporter=line,json'" not in source
    assert "argument.startsWith('--reporter=')" in source
    assert "argument.startsWith('--config=')" in source


def test_every_playwright_config_arms_the_independent_budget_reporter() -> None:
    budget_module = (
        REPO_ROOT / "tests/e2e-contract/test-budget.ts"
    ).read_text(encoding="utf-8")
    assert "import.meta" not in budget_module, "Playwright loads this shared module as CommonJS"
    configs = sorted(
        path
        for root in (REPO_ROOT / "tests/e2e-ui", REPO_ROOT / "e2e")
        for path in root.glob("playwright*.config.ts")
    )
    assert configs
    for config in configs:
        source = config.read_text(encoding="utf-8")
        assert "timeout: MAX_E2E_TEST_MS" in source, config
        assert "[PLAYWRIGHT_BUDGET_REPORTER]" in source, config


def test_playwright_budget_reporter_waits_for_playwright_before_killing_the_group(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    """The watchdog arms after the per-test timeout, not with it.

    Playwright fails a slow test itself at `max_test_seconds`, and that costs
    one test. This reporter kills the process group, which costs every test the
    lane had left. Armed at the same instant the two deadlines race, and the
    expensive one wins: one exclusive round reported a single red out of
    eighty-eight because one spec sat at the wall. The grace is what makes the
    kill a hang watchdog rather than a second, harsher timeout.
    """
    reporter = REPO_ROOT / "tests/e2e-contract/playwright-test-budget.cjs"
    sentinel = tmp_path / "test-timeout.json"
    ledger = tmp_path / "settled-results.jsonl"
    probe = r"""
const Reporter = require(process.argv[1]);
const sentinel = process.argv[2];
const ledger = process.argv[3];
let timerDelay = 0;
let killed = null;
global.setTimeout = (callback, delay) => {
  timerDelay = delay;
  callback();
  return { probe: true };
};
global.clearTimeout = () => {};
process.kill = (pid, signal) => { killed = { pid, signal }; };
process.env.ASTRABOX_E2E_TIMEOUT_SENTINEL = sentinel;
process.env.ASTRABOX_E2E_SETTLED_RESULTS = ledger;
process.env.ASTRABOX_E2E_PROCESS_GROUP_ID = '4321';
const result = {};
new Reporter().onTestBegin(
  { location: { file: 'spec.ts' }, titlePath: () => ['suite', 'test'] },
  result,
);
process.stdout.write(JSON.stringify({ killed, timerDelay }));
"""
    completed = subprocess.run(
        ["node", "-e", probe, str(reporter), str(sentinel), str(ledger)],
        capture_output=True,
        text=True,
        check=False,
        env=node_toolchain_env,
    )

    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    grace_ms = contract["watchdog_grace_seconds"] * 1000
    assert grace_ms > 0, "a watchdog armed with the timeout it backs up races it"
    watchdog_ms = contract["max_test_seconds"] * 1000 + grace_ms

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "killed": {"pid": -4321, "signal": "SIGTERM"},
        "timerDelay": watchdog_ms,
    }
    assert json.loads(sentinel.read_text(encoding="utf-8")) == {
        "file": "spec.ts",
        "limit_ms": 180_000,
        "state": "TIMEOUT",
        "title": ["suite", "test"],
        "watchdog_ms": watchdog_ms,
    }
    assert json.loads(ledger.read_text(encoding="utf-8")) == {
        "annotations": [],
        "duration_ms": 180_000,
        "error": "test exceeded the fixed 180000ms budget",
        "expected_status": "passed",
        "file": "spec.ts",
        "retry": 0,
        "status": "timedOut",
        "title": ["suite", "test"],
    }


def test_playwright_budget_reporter_terminates_the_runner_on_skip(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    reporter = REPO_ROOT / "tests/e2e-contract/playwright-test-budget.cjs"
    sentinel = tmp_path / "test-skip.json"
    probe = r"""
const Reporter = require(process.argv[1]);
const sentinel = process.argv[2];
let killed = null;
process.kill = (pid, signal) => { killed = { pid, signal }; };
process.env.ASTRABOX_E2E_SKIP_SENTINEL = sentinel;
process.env.ASTRABOX_E2E_PROCESS_GROUP_ID = '4321';
new Reporter().onTestEnd(
  {
    annotations: [{ type: 'skip', description: 'Casdoor is unavailable' }],
    location: { file: 'postgresql-runtime-services.spec.ts' },
    titlePath: () => ['suite', 'database isolation'],
  },
  { status: 'skipped' },
);
process.stdout.write(JSON.stringify({ killed }));
"""
    completed = subprocess.run(
        ["node", "-e", probe, str(reporter), str(sentinel)],
        capture_output=True,
        text=True,
        check=False,
        env=node_toolchain_env,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "killed": {"pid": -4321, "signal": "SIGTERM"},
    }
    assert json.loads(sentinel.read_text(encoding="utf-8")) == {
        "annotations": [
            {"description": "Casdoor is unavailable", "type": "skip"},
        ],
        "file": "postgresql-runtime-services.spec.ts",
        "state": "SKIPPED",
        "title": ["suite", "database isolation"],
    }


def test_playwright_budget_reporter_appends_each_settled_result(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    reporter = REPO_ROOT / "tests/e2e-contract/playwright-test-budget.cjs"
    ledger = tmp_path / "settled-results.jsonl"
    probe = r"""
const Reporter = require(process.argv[1]);
process.env.ASTRABOX_E2E_SETTLED_RESULTS = process.argv[2];
const reporter = new Reporter();
reporter.onTestEnd(
  {
    annotations: [],
    expectedStatus: 'passed',
    location: { file: 'specs/example.parallel.spec.ts' },
    titlePath: () => ['', 'chromium', 'example.parallel.spec.ts', 'settles once'],
  },
  { duration: 321, retry: 0, status: 'passed' },
);
"""

    completed = subprocess.run(
        ["node", "-e", probe, str(reporter), str(ledger)],
        capture_output=True,
        text=True,
        check=False,
        env=node_toolchain_env,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(ledger.read_text(encoding="utf-8")) == {
        "annotations": [],
        "duration_ms": 321,
        "error": "",
        "expected_status": "passed",
        "file": "specs/example.parallel.spec.ts",
        "retry": 0,
        "status": "passed",
        "title": ["", "chromium", "example.parallel.spec.ts", "settles once"],
    }


def test_python_live_specs_cannot_override_the_collection_budget() -> None:
    offenders: list[str] = []
    paths = sorted(PYTHON_LIVE.rglob("test_*.py"))
    assert paths, f"no Python live specs found under {PYTHON_LIVE}"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "timeout"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], f"Python live timeout markers bypass the 180s contract: {offenders}"


class _Item:
    def __init__(self, *, timeout_marker: object | None = None) -> None:
        self.nodeid = "tests/e2e/test_example.py::test_live"
        self.timeout_marker = timeout_marker
        self.added: list[object] = []

    def get_closest_marker(self, name: str) -> object | None:
        if name == "e2e":
            return SimpleNamespace(name="e2e")
        if name == "timeout":
            return self.timeout_marker
        return None

    def add_marker(self, marker: object) -> None:
        self.added.append(marker)


def test_python_collection_applies_180_seconds_and_rejects_an_override() -> None:
    item = _Item()
    live_conftest.pytest_collection_modifyitems([item])  # type: ignore[list-item]
    assert len(item.added) == 1
    assert item.added[0].args == (180,)  # type: ignore[attr-defined]

    overridden = _Item(timeout_marker=SimpleNamespace(name="timeout"))
    with pytest.raises(pytest.UsageError, match="overrides the fixed 180s"):
        live_conftest.pytest_collection_modifyitems([overridden])  # type: ignore[list-item]
