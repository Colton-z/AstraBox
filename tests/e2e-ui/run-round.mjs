#!/usr/bin/env node
/**
 * One e2e-ui round, ordered by what is still unproven.
 *
 * The suite stops at its first failure, so the ledger orders specs by the
 * strength and age of their recorded outcome instead of filename.
 *
 * A round therefore runs in phases against a ledger of per-spec outcomes:
 *
 *   1. unproven — no outcome on record; the only place a NEW defect can appear
 *   2. failed   — the red under repair
 *   3. skipped  — it ran and proved nothing (probe skips, opt-in gates)
 *   4. passed   — stale proof refresh, oldest proof first
 *
 * A phase that fails ends the round, leaving its report and its sandboxes for
 * diagnosis. The ledger records only what a run actually demonstrated: a spec
 * cut short by the stop-on-first-failure, or never started, stays unproven and
 * leads the next round.
 *
 * The ledger lives in the output directory (`.e2e/`, which deploys do not
 * sync), so it accumulates on the machine that runs the suite. Any JSON report
 * left there contributes outcomes, including a one-spec Playwright run.
 *
 * Usage:  node run-round.mjs [--plan] [extra playwright args]
 */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { runE2ePreflight } from './preflight.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '../..');
const outputRoot = path.resolve(
  process.env.ASTRABOX_E2E_OUTPUT_DIR || path.join(repoRoot, '.e2e'),
);
const ledgerPath = path.join(outputRoot, 'spec-ledger.json');
const resultsPath = path.join(outputRoot, 'results.json');
const specsDir = path.join(here, 'specs');

const PHASES = [
  ['unproven', 'no outcome on record'],
  ['failed', 'red under repair'],
  ['skipped', 'ran, proved nothing'],
  ['passed', 'regression sweep'],
];
const RANK = { failed: 3, passed: 2, skipped: 1 };

const readJson = (file, fallback) => {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return fallback;
  }
};

const mtime = (file) => {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return 0;
  }
};

/** What a single test result demonstrates, or null when it demonstrates nothing. */
function outcomeOfTest(test) {
  const statuses = (test.results || []).map((r) => r.status);
  if (!statuses.length) return null; // never started — cut by --max-failures
  if (statuses.includes('interrupted')) return null; // killed mid-flight
  if (statuses.some((s) => s === 'failed' || s === 'timedOut')) return 'failed';
  if (statuses.includes('passed')) return 'passed';
  if (statuses.includes('skipped')) return 'skipped';
  return null;
}

function* eachSpec(suite, inheritedFile) {
  const file = suite.file || inheritedFile;
  for (const spec of suite.specs || []) yield [spec.file || file, spec];
  for (const child of suite.suites || []) yield* eachSpec(child, file);
}

/** Fold a Playwright JSON report into the ledger. Silence never overwrites evidence. */
function merge(ledger, report, round) {
  const seen = new Map();
  for (const [file, spec] of eachSpec({ suites: report.suites || [] })) {
    if (!file) continue;
    for (const test of spec.tests || []) {
      const outcome = outcomeOfTest(test);
      if (!outcome) continue;
      const name = path.basename(file);
      const prior = seen.get(name);
      if (!prior || RANK[outcome] > RANK[prior]) seen.set(name, outcome);
    }
  }
  for (const [name, outcome] of seen) {
    ledger.specs[name] = { outcome, round, at: new Date().toISOString() };
  }
  return seen.size;
}

function mergeIfFresh(ledger) {
  if (mtime(resultsPath) <= mtime(ledgerPath)) return;
  const folded = merge(ledger, readJson(resultsPath, {}), ledger.round);
  if (folded) console.log(`ledger: folded in ${folded} spec outcome(s) from the last report`);
}

function bucketise(ledger) {
  const buckets = { unproven: [], failed: [], skipped: [], passed: [] };
  for (const name of fs.readdirSync(specsDir).filter((f) => f.endsWith('.spec.ts')).sort()) {
    const entry = ledger.specs[name];
    const bucket = entry && buckets[entry.outcome] ? entry.outcome : 'unproven';
    buckets[bucket].push({ name, round: entry?.round ?? 0 });
  }
  buckets.passed.sort((a, b) => a.round - b.round); // stalest proof first
  return buckets;
}

async function main() {
  const argv = process.argv.slice(2);
  const planOnly = argv.includes('--plan');
  const passthrough = argv.filter((a) => a !== '--plan');
  const replacesBudgetConfig = passthrough.some(
    (argument) => argument === '--reporter'
      || argument.startsWith('--reporter=')
      || argument === '--config'
      || argument === '-c'
      || argument.startsWith('--config='),
  );
  if (replacesBudgetConfig) {
    console.error('run-round fixes the Playwright config so the 180s per-test watchdog cannot be replaced');
    return 64;
  }

  fs.mkdirSync(outputRoot, { recursive: true });
  const ledger = readJson(ledgerPath, { round: 0, specs: {} });
  ledger.specs ||= {};
  mergeIfFresh(ledger);

  const round = (ledger.round || 0) + 1;
  const buckets = bucketise(ledger);
  const total = Object.values(buckets).reduce((n, b) => n + b.length, 0);

  console.log(`\ne2e-ui round ${round} — ${total} specs, ordered by what is unproven`);
  for (const [phase, why] of PHASES) {
    console.log(`  ${phase.padEnd(9)} ${String(buckets[phase].length).padStart(3)}  (${why})`);
  }
  if (planOnly) {
    for (const [phase] of PHASES) {
      for (const { name } of buckets[phase]) console.log(`  ${phase.padEnd(9)} ${name}`);
    }
    return 0;
  }

  // Preflight substrate dependencies before the ledger can classify their
  // absence as a product result.
  try {
    await runE2ePreflight();
  } catch (error) {
    console.error(`\n${String(error.message || error)}`);
    return 78;
  }

  ledger.round = round;
  fs.writeFileSync(ledgerPath, JSON.stringify(ledger, null, 2));

  for (const [phase, why] of PHASES) {
    const files = buckets[phase];
    if (!files.length) continue;
    console.log(`\n── phase ${phase}: ${files.length} spec(s) — ${why}`);
    // A server restart invalidates concurrent specs that share that server, so
    // restarting specs run in their own single-worker group after the rest of
    // the phase.
    const [restarters, rest] = [
      files.filter(({ name }) => restartsTheServer(name)),
      files.filter(({ name }) => !restartsTheServer(name)),
    ];
    for (const [group, extra] of [[rest, []], [restarters, ['--workers=1']]]) {
      if (!group.length) continue;
      if (extra.length) console.log(`   ↳ serial pass: ${group.length} spec(s) restart the server`);
      const status = runGroup(group, extra, passthrough, resultsPath);
      merge(ledger, readJson(resultsPath, {}), round);
      fs.writeFileSync(ledgerPath, JSON.stringify(ledger, null, 2));
      if (status !== 0) {
        console.log(`\nround ${round} stopped in phase ${phase}; ledger at ${ledgerPath}`);
        return status;
      }
    }
  }
  console.log(`\nround ${round} clean across all phases`);
  return 0;
}

/** True when the spec's own source restarts the shared server container. */
function restartsTheServer(name) {
  try {
    return fs.readFileSync(path.join(here, 'specs', name), 'utf8').includes('restartServerContainer');
  } catch {
    return false;
  }
}

function runGroup(files, extraArgs, passthrough, resultsPath) {
    const run = spawnSync(
      'npx',
      [
        'playwright',
        'test',
        ...files.map(({ name }) => path.join('specs', name)),
        ...extraArgs,
        '--max-failures=1',
        // Do not pass --reporter here. A CLI reporter replaces the configured
        // list, including both the JSON ledger input and the 180s watchdog.
        ...passthrough,
      ],
      {
        cwd: here,
        stdio: 'inherit',
        env: { ...process.env, PLAYWRIGHT_JSON_OUTPUT_NAME: resultsPath },
      },
    );
  return run.status ?? 1;
}

process.exit(await main());
