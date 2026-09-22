"""Exercise Playwright worker hang reports through Node's real IPC and writer."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_E2E_ROOT = _REPO_ROOT / "tests" / "e2e-ui"
_HANG_REPORT_FIXTURE = _E2E_ROOT / "fixtures" / "hangReport.ts"

_WORKER_SOURCE = """
const { pathToFileURL } = require('node:url');
const fixturePath = process.argv[2];
const reportDir = process.argv[3];
const delayMs = Number(process.argv[4]);
const lingerMs = Number(process.argv[5]);
const exitCode = Number(process.argv[6]);

(async () => {
  const { armWorkerHangReport } = await import(pathToFileURL(fixturePath).href);
  armWorkerHangReport({ delayMs, reportDir });
  if (typeof process.send !== 'function') throw new Error('worker has no IPC channel');
  process.send({ type: 'ready' });
  setTimeout(() => process.exit(exitCode), lingerMs);
})().catch((error) => {
  process.stderr.write(`worker startup error: ${String(error)}\n`);
  process.exit(92);
});
"""

_DRIVER_SOURCE = """
const { fork } = require('node:child_process');

const workerPath = process.argv[2];
const fixturePath = process.argv[3];
const shutdown = process.argv[4];
const workerArgs = process.argv.slice(5);
const child = fork(workerPath, [fixturePath, ...workerArgs], {
  execArgv: ['--experimental-strip-types', '--no-warnings'],
  silent: true,
});

child.stdout.pipe(process.stdout);
child.stderr.pipe(process.stderr);
child.once('message', (message) => {
  if (message?.type !== 'ready') {
    process.stderr.write(`unexpected worker message: ${JSON.stringify(message)}\n`);
    child.kill();
    return;
  }
  if (shutdown === 'stop') child.send({ method: '__stop__' });
  if (shutdown === 'disconnect') child.disconnect();
});
child.once('error', (error) => {
  process.stderr.write(`worker process error: ${String(error)}\n`);
});
child.once('close', (code, signal) => {
  if (signal !== null) {
    process.stderr.write(`worker exited from signal ${signal}\n`);
    process.exit(90);
  }
  process.exit(code ?? 91);
});
"""


def _run_worker(
    tmp_path: Path,
    *,
    shutdown: str,
    report_dir: Path,
    node_toolchain_env: dict[str, str],
    exit_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    worker = tmp_path / "worker.cjs"
    driver = tmp_path / "driver.cjs"
    worker.write_text(textwrap.dedent(_WORKER_SOURCE), encoding="utf-8")
    driver.write_text(textwrap.dedent(_DRIVER_SOURCE), encoding="utf-8")
    return subprocess.run(
        [
            "node",
            str(driver),
            str(worker),
            str(_HANG_REPORT_FIXTURE),
            shutdown,
            str(report_dir),
            "25",
            "350",
            str(exit_code),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env=node_toolchain_env,
    )


def test_a_running_worker_does_not_write_a_hang_report(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    report_dir = tmp_path / "reports"
    result = _run_worker(
        tmp_path,
        shutdown="none",
        report_dir=report_dir,
        node_toolchain_env=node_toolchain_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not report_dir.exists()
    assert "[hangReport]" not in result.stderr


@pytest.mark.parametrize("shutdown", ["stop", "disconnect"])
def test_a_stopping_worker_writes_a_real_report_and_names_it(
    tmp_path: Path,
    shutdown: str,
    node_toolchain_env: dict[str, str],
) -> None:
    report_dir = tmp_path / shutdown
    result = _run_worker(
        tmp_path,
        shutdown=shutdown,
        report_dir=report_dir,
        node_toolchain_env=node_toolchain_env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    reports = list(report_dir.glob("astrabox-worker-hang-*.json"))
    assert len(reports) == 1, result.stdout + result.stderr
    report = reports[0]
    worker_pid = int(report.stem.removeprefix("astrabox-worker-hang-"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["header"]["processId"] == worker_pid
    assert (
        f"[hangReport] worker {worker_pid} still stopping; wrote {report}\n"
        in result.stderr
    )


def test_a_report_failure_preserves_the_worker_result(
    tmp_path: Path,
    node_toolchain_env: dict[str, str],
) -> None:
    blocked_report_dir = tmp_path / "not-a-directory"
    blocked_report_dir.write_text("occupied", encoding="utf-8")
    result = _run_worker(
        tmp_path,
        shutdown="stop",
        report_dir=blocked_report_dir,
        node_toolchain_env=node_toolchain_env,
        exit_code=23,
    )

    assert result.returncode == 23, result.stdout + result.stderr
    assert "[hangReport] worker" in result.stderr
    assert "failed to write" in result.stderr
    assert str(blocked_report_dir) in result.stderr
