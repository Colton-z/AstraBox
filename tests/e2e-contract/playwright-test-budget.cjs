const fs = require('node:fs');
const path = require('node:path');

const contract = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'suite-contract.json'), 'utf8'),
);
if (!Number.isInteger(contract.max_test_seconds) || contract.max_test_seconds !== 180) {
  throw new Error('the live E2E contract must set max_test_seconds to exactly 180');
}
const maxTestMs = contract.max_test_seconds * 1_000;
if (!Number.isInteger(contract.watchdog_grace_seconds) || contract.watchdog_grace_seconds < 5) {
  throw new Error('the live E2E contract must set watchdog_grace_seconds to at least 5');
}
// Playwright fails a test of its own accord at `timeout: MAX_E2E_TEST_MS`, and
// that failure costs one test. This watchdog kills the whole process group, and
// that costs every test the lane had left — so it exists only for the hang
// Playwright cannot interrupt, and it has to fire AFTER Playwright has had its
// chance. Armed at the same instant, the two deadlines race, and the expensive
// one wins often enough that a single slow test ends the round with one red
// reported out of a lane of eighty-eight.
const watchdogMs = maxTestMs + contract.watchdog_grace_seconds * 1_000;

/** Kill a live Playwright process group on a timeout or non-result such as skip. */
class TestBudgetReporter {
  constructor() {
    this.timers = new Map();
    this.settled = new Set();
  }

  record(test, result, override = {}) {
    if (this.settled.has(result)) return;
    const ledger = process.env.ASTRABOX_E2E_SETTLED_RESULTS;
    if (ledger) {
      const error = override.error || (result.error && result.error.message) || '';
      const record = {
        annotations: (test.annotations || []).map(({ type, description }) => ({
          description,
          type,
        })),
        duration_ms: override.duration_ms ?? result.duration ?? 0,
        error,
        expected_status: test.expectedStatus || 'passed',
        file: test.location.file,
        retry: result.retry || 0,
        status: override.status || result.status,
        title: test.titlePath(),
      };
      fs.appendFileSync(ledger, `${JSON.stringify(record)}\n`, 'utf8');
    }
    this.settled.add(result);
  }

  onTestBegin(test, result) {
    const timer = setTimeout(() => {
      const message = JSON.stringify({
        file: test.location.file,
        limit_ms: maxTestMs,
        state: 'TIMEOUT',
        title: test.titlePath(),
        watchdog_ms: watchdogMs,
      });
      const sentinel = process.env.ASTRABOX_E2E_TIMEOUT_SENTINEL;
      if (sentinel) {
        const temporary = `${sentinel}.tmp.${process.pid}`;
        fs.writeFileSync(temporary, `${message}\n`, { encoding: 'utf8', mode: 0o600 });
        fs.renameSync(temporary, sentinel);
      }
      this.record(test, result, {
        duration_ms: maxTestMs,
        error: `test exceeded the fixed ${maxTestMs}ms budget`,
        status: 'timedOut',
      });
      process.stderr.write(`E2E TEST TIMEOUT ${message}\n`);
      const processGroup = Number(process.env.ASTRABOX_E2E_PROCESS_GROUP_ID || '');
      if (Number.isSafeInteger(processGroup) && processGroup > 1) {
        process.kill(-processGroup, 'SIGTERM');
      } else {
        process.kill(process.pid, 'SIGTERM');
      }
    }, watchdogMs);
    this.timers.set(result, timer);
  }

  onTestEnd(test, result) {
    const timer = this.timers.get(result);
    if (timer) clearTimeout(timer);
    this.timers.delete(result);
    if (result.status === 'skipped') {
      const message = JSON.stringify({
        annotations: (test.annotations || []).map(({ type, description }) => ({
          description,
          type,
        })),
        file: test.location.file,
        state: 'SKIPPED',
        title: test.titlePath(),
      });
      const sentinel = process.env.ASTRABOX_E2E_SKIP_SENTINEL;
      if (sentinel) {
        const temporary = `${sentinel}.tmp.${process.pid}`;
        fs.writeFileSync(temporary, `${message}\n`, { encoding: 'utf8', mode: 0o600 });
        fs.renameSync(temporary, sentinel);
      }
      this.record(test, result);
      process.stderr.write(`E2E TEST SKIPPED ${message}\n`);
      const processGroup = Number(process.env.ASTRABOX_E2E_PROCESS_GROUP_ID || '');
      if (Number.isSafeInteger(processGroup) && processGroup > 1) {
        process.kill(-processGroup, 'SIGTERM');
      } else {
        process.kill(process.pid, 'SIGTERM');
      }
      return;
    }

    this.record(test, result);
  }

  onEnd() {
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
    this.settled.clear();
  }
}

module.exports = TestBudgetReporter;
