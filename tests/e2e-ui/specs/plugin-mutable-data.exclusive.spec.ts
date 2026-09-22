/**
 * E2E: a conversation prepares the mutable data directory its plugins write.
 *
 * Claude Code resolves `CLAUDE_PLUGIN_DATA` to `<config>/plugins/data/<plugin-id>`,
 * creates it on first reference, and exports it to hook processes and to MCP and
 * LSP server subprocesses — all of which run as the workload account. The plugin
 * code beside it, `<config>/plugins`, is platform-installed and stays root-owned,
 * so the workload account cannot create the missing child underneath it. The
 * conversation bootstrap
 * (`astrabox/core/service/orchestrator/runtime/provision-conversation`) is the
 * one place that distinction is made.
 * https://code.claude.com/docs/en/plugins-reference#persistent-data-directory
 *
 * The Agent carries the plugin repository the deployment already pins for the
 * Investment Research journey, at the same reviewed revision
 * (`ASTRABOX_E2E_PLUGIN_REVISIONS`), and is driven through the console with one
 * tool-free prompt, so startup and the vendor's own initialization are what
 * produce the evidence. The spec then reads
 *
 *   · that the turn settled COMPLETED with its own durable terminal proof and a
 *     non-empty reply, so the probe below runs against a finished turn rather
 *     than a stream still in flight;
 *   · `<config>/plugins/data` — owner, mode, and a real create/write/read/unlink
 *     performed by the workload account itself (its euid is asserted, because a
 *     probe that ran as root would prove nothing about that account);
 *   · `<config>/plugins` — root-owned 0755, so preparing the state directory
 *     does not hand the plugin code over with it;
 *   · the plugin-owned `.mcp.json` under exactly the configured plugin path;
 *   · the durable `SystemMessage(init)` raw event, whose `mcp_servers` must be
 *     exactly the declared servers under their full plugin-qualified names.
 *
 * A second pinned public plugin records the SDK's own SessionStart/Stop hooks
 * in CLAUDE_PLUGIN_DATA. The probe only reads that log and matches its Session
 * identity to native database custody; it never invokes or imitates the hook.
 * See docs/maintainers/plugin-mutable-data.md for the source review.
 * The spec does not claim that an MCP
 * tool ran, that any declared server connected, or that the platform-installed
 * plugin symlinks are immutable, and nothing here establishes what the pinned
 * CLI does when it cannot create the data directory; the bootstrap does not
 * depend on that.
 */
import { test, expect } from '@playwright/test';

import {
  AstraApi,
  messageText,
  visibleMessages,
  type AdminSessionRecord,
} from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import {
  documentsByField,
  oracleDbPath,
  waitForTurnTerminalProof,
} from '../fixtures/dbOracle';
import { appPath, parseTimeoutEnv } from '../fixtures/env';
import {
  requireSandboxHandle,
  sandboxExec,
  type SandboxHandle,
} from '../fixtures/sandboxOps';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');

// The plugin repository the deployment provisions and pins for the Investment
// Research journey. Reusing it is what keeps this spec free of configuration of its own: the lane
// already supplies the reviewed revision, and `financial-analysis` is the
// plugin in that repository with a plugin-owned `.mcp.json`.
const PLUGIN_REPOSITORY = 'https://github.com/anthropics/financial-services.git';
const PLUGIN_PATH = 'plugins/vertical-plugins/financial-analysis';
const STATS_REPOSITORY = 'https://github.com/reidsolon/claude-session-stats-plugin.git';
const STATS_REVISION = '84433f7d3740b3b1c4a238a705f3cfb17d8bd3d8';

const REPLY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 90_000);
const INIT_EVENT_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_INIT_EVENT_TIMEOUT_MS', 30_000);

const PROBE_MARKER = 'ASTRABOX_PLUGIN_DATA_PROBE:';
// Written into the plugin data directory by the workload account and read back
// out of it, so the filename and the content are one run's own.
const PROBE_TOKEN = RUN_ID.replace(/[^A-Za-z0-9-]/g, '');

interface DirectoryFacts {
  path: string;
  owner: string;
  mode: string;
}

interface PluginConfig {
  path: string;
  plugin_name: string;
  server_names: string[];
  checkout_sha: string;
  shallow: string;
}

interface PluginDataProbe {
  plugin_root: DirectoryFacts;
  plugin_data: DirectoryFacts;
  /** Per-plugin directories under `plugins/data`, read before this probe writes. */
  plugin_data_children: string[];
  /** Effective uid of the process that performed the writability checks. */
  workload_euid: number;
  workload_access_w_ok: boolean;
  workload_write_read_back: string;
  plugin_configs: PluginConfig[];
}

interface PluginHookProbe {
  checkout_sha: string;
  shallow: string;
  plugin_name: string;
  logs: Array<{
    path: string;
    uid: number;
    entries: Array<{ event: string; session_id: string; timestamp: string; cwd: string }>;
  }>;
}

/** The reviewed plugin revision the lane runner supplies, as the journey spec reads it. */
function pinnedPluginRevision(): string {
  const raw = String(process.env.ASTRABOX_E2E_PLUGIN_REVISIONS || '').trim();
  expect(
    raw,
    'ASTRABOX_E2E_PLUGIN_REVISIONS carries the reviewed plugin revisions and is supplied by '
      + 'the lane runner (run-playwright.sh --plugin-revisions)',
  ).not.toEqual('');
  const revision = raw.split(',')[0].trim();
  expect(
    /^[0-9a-f]{40}$/.test(revision),
    `ASTRABOX_E2E_PLUGIN_REVISIONS must carry full Git SHAs, got ${JSON.stringify(revision)}`,
  ).toBe(true);
  return revision;
}

/** Runtime identity facts the probe addresses the box by. */
function expectRuntimeIdentity(detail: AdminSessionRecord): {
  linuxUser: string;
  configDir: string;
  uid: number;
  pluginRepo: string;
  statsRepo: string;
} {
  const identity = (detail.runtime_identity || {}) as Record<string, unknown>;
  expect(detail.runtime_identity, 'a READY conversation should expose runtime_identity').toBeTruthy();
  const linuxUser = String(identity.linux_user || '').trim();
  const configDir = String(identity.config_dir || '').replace(/\/+$/, '');
  const uid = Number(identity.uid || 0);
  expect(linuxUser, 'runtime_identity.linux_user names the workload account').not.toEqual('');
  expect(configDir, 'runtime_identity.config_dir names the engine configuration directory').not.toEqual('');
  expect(uid, 'runtime_identity.uid names the workload account numerically').toBeGreaterThan(0);
  const capabilityPlan = (identity.capability_plan || {}) as Record<string, unknown>;
  const plan = Array.isArray(capabilityPlan.plugin_repo_plan) ? capabilityPlan.plugin_repo_plan : [];
  expect(plan, 'both selected plugin repositories must enter the same preparation path').toHaveLength(2);
  const repos = plan as Array<Record<string, unknown>>;
  expect(repos.map((repo) => repo.url).sort()).toEqual([PLUGIN_REPOSITORY, STATS_REPOSITORY].sort());
  const repo = repos.find((repo) => repo.url === PLUGIN_REPOSITORY)!;
  expect(repo.url, 'the runtime checkout must belong to the selected repository').toBe(PLUGIN_REPOSITORY);
  const pluginRepo = String(repo.target_path || '').trim();
  expect(pluginRepo, 'the runtime must name the prepared checkout under its plugin directory')
    .toMatch(/^\/.+/);
  expect(pluginRepo.startsWith(`${configDir}/plugins/`)).toBe(true);
  const statsRepo = String(repos.find((repo) => repo.url === STATS_REPOSITORY)!.target_path || '').trim();
  expect(statsRepo.startsWith(`${configDir}/plugins/`)).toBe(true);
  return { linuxUser, configDir, uid, pluginRepo, statsRepo };
}

/** Independently read files written by the installed plugin, without running its hooks. */
function readPluginHookLog(sandbox: SandboxHandle, configDir: string, checkout: string): PluginHookProbe {
  const script = [
    `python3 - ${JSON.stringify(configDir)} ${JSON.stringify(checkout)} <<'PY'`,
    'import json, pathlib, subprocess, sys',
    'config, checkout = map(pathlib.Path, sys.argv[1:])',
    'manifest = json.loads((checkout / ".claude-plugin/plugin.json").read_text())',
    'def git_value(*args):',
    '    return subprocess.run(["git", "-C", str(checkout), *args], check=True,',
    '                          capture_output=True, text=True, timeout=10).stdout.strip()',
    'logs = []',
    'for path in sorted((config / "plugins/data").glob("*/session-activity.jsonl")):',
    '    logs.append({"path": str(path), "uid": path.stat().st_uid,',
    '                 "entries": [json.loads(line) for line in path.read_text().splitlines() if line]})',
    'print(json.dumps({"checkout_sha": git_value("rev-parse", "HEAD"),',
    '                  "shallow": git_value("rev-parse", "--is-shallow-repository"),',
    '                  "plugin_name": manifest["name"], "logs": logs}))',
    'PY',
  ].join('\n');
  return JSON.parse(sandboxExec(sandbox, script, 30_000).trim()) as PluginHookProbe;
}

/**
 * Read the plugin directories out of the live box.
 *
 * Root reads the ownership and mode, because only root can enter a 0700
 * directory it does not own. The writability half runs through
 * `runuser -u <workload user>`: it reports its own euid and performs a real
 * create/write/read/unlink, so "writable" is the account's answer rather than
 * root's.
 *
 * `plugins/data` is listed before that write, so the reported children are
 * whatever the vendor left there and never this probe's file. The manifests
 * come from the configured plugin path under the checkout named by
 * `runtime_identity.capability_plan.plugin_repo_plan`, not from a walk of the
 * whole clone, which would include plugins the Agent did not select.
 */
function probePluginData(
  sandbox: SandboxHandle,
  configDir: string,
  linuxUser: string,
  pluginRepo: string,
  pluginPaths: string[],
  token: string,
): PluginDataProbe {
  const script = [
    `export ASTRABOX_PROBE_MARKER=${JSON.stringify(PROBE_MARKER)}`,
    `export ASTRABOX_PROBE_CONFIG=${JSON.stringify(configDir)}`,
    `export ASTRABOX_PROBE_ACCOUNT=${JSON.stringify(linuxUser)}`,
    `export ASTRABOX_PROBE_CHECKOUT=${JSON.stringify(pluginRepo)}`,
    `export ASTRABOX_PROBE_PLUGIN_PATHS=${JSON.stringify(JSON.stringify(pluginPaths))}`,
    `export ASTRABOX_PROBE_TOKEN=${JSON.stringify(token)}`,
    "python3 - <<'PY'",
    'import json, os, pwd, stat, subprocess',
    '',
    'config = os.environ["ASTRABOX_PROBE_CONFIG"].rstrip("/")',
    'account = os.environ["ASTRABOX_PROBE_ACCOUNT"]',
    'checkout = os.environ["ASTRABOX_PROBE_CHECKOUT"]',
    'plugin_paths = [',
    '    item.strip("/")',
    '    for item in json.loads(os.environ["ASTRABOX_PROBE_PLUGIN_PATHS"])',
    ']',
    'token = os.environ["ASTRABOX_PROBE_TOKEN"]',
    'plugin_root = config + "/plugins"',
    'plugin_data = plugin_root + "/data"',
    'data_children = sorted(os.listdir(plugin_data))',
    '',
    'def described(path):',
    '    info = os.stat(path)',
    '    return {',
    '        "path": path,',
    '        "owner": pwd.getpwuid(info.st_uid).pw_name,',
    '        "mode": oct(stat.S_IMODE(info.st_mode)),',
    '    }',
    '',
    'WORKLOAD = """',
    'import json, os, sys',
    'target, token = sys.argv[1], sys.argv[2]',
    'probe = os.path.join(target, ".astrabox-e2e-plugin-data-" + token)',
    'access = os.access(target, os.W_OK)',
    'with open(probe, "x", encoding="utf-8") as handle:',
    '    handle.write(token)',
    'try:',
    '    with open(probe, encoding="utf-8") as handle:',
    '        read_back = handle.read()',
    'finally:',
    '    os.unlink(probe)',
    'print(json.dumps({',
    '    "euid": os.geteuid(),',
    '    "access_w_ok": access,',
    '    "read_back": read_back,',
    '}))',
    '"""',
    '',
    'completed = subprocess.run(',
    '    ["runuser", "-u", account, "--", "python3", "-c", WORKLOAD, plugin_data, token],',
    '    capture_output=True, text=True, timeout=60,',
    ')',
    'if completed.returncode != 0:',
    '    raise SystemExit(',
    '        "workload probe failed rc=%s stdout=%r stderr=%r"',
    '        % (completed.returncode, completed.stdout[-800:], completed.stderr[-800:])',
    '    )',
    'workload = json.loads(completed.stdout.strip().splitlines()[-1])',
    '',
    'plugin_configs = []',
    'config_paths = sorted({',
    '    os.path.join(checkout, plugin_path, ".mcp.json")',
    '    for plugin_path in plugin_paths',
    '})',
    'for config_path in config_paths:',
    '    raw = json.loads(open(config_path, encoding="utf-8").read())',
    // The reference documents the `mcpServers` wrapper; some published plugins
    // ship the bare server map. Both are the vendor's, so the probe reports
    // whichever the selected plugin declares.
    '    servers = raw.get("mcpServers") if isinstance(raw, dict) and "mcpServers" in raw else raw',
    '    manifest_path = os.path.join(os.path.dirname(config_path), ".claude-plugin", "plugin.json")',
    '    manifest = json.loads(open(manifest_path, encoding="utf-8").read())',
    '    def git_value(*args):',
    '        return subprocess.run(',
    '            ["git", "-C", os.path.dirname(config_path), *args],',
    '            check=True, capture_output=True, text=True, timeout=10,',
    '        ).stdout.strip()',
    '    plugin_configs.append({',
    '        "path": config_path,',
    '        "plugin_name": str(manifest.get("name") or ""),',
    '        "server_names": sorted(servers) if isinstance(servers, dict) else [],',
    '        "checkout_sha": git_value("rev-parse", "HEAD"),',
    '        "shallow": git_value("rev-parse", "--is-shallow-repository"),',
    '    })',
    '',
    'print(os.environ["ASTRABOX_PROBE_MARKER"] + json.dumps({',
    '    "plugin_root": described(plugin_root),',
    '    "plugin_data": described(plugin_data),',
    '    "plugin_data_children": data_children,',
    '    "workload_euid": workload["euid"],',
    '    "workload_access_w_ok": workload["access_w_ok"],',
    '    "workload_write_read_back": workload["read_back"],',
    '    "plugin_configs": plugin_configs,',
    '}, ensure_ascii=False))',
    'PY',
  ].join('\n');

  const stdout = sandboxExec(sandbox, script, 90_000);
  const at = stdout.indexOf(PROBE_MARKER);
  expect(
    at,
    `plugin data probe emitted no ${PROBE_MARKER} line for sandbox ${sandbox.sandboxId}; `
      + `raw tail=${stdout.slice(-800)}`,
  ).toBeGreaterThanOrEqual(0);
  return JSON.parse(stdout.slice(at + PROBE_MARKER.length).split('\n')[0]) as PluginDataProbe;
}

/** The engine frame a durable `session_events` row stores under `payload`. */
function framePayload(event: Record<string, unknown>): Record<string, unknown> {
  const payload = event.payload;
  return payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {};
}

/**
 * The vendor's `SystemMessage(init)` payloads, newest last.
 *
 * The runner stamps each SDK dataclass with its class name and keeps the
 * vendor's field names, so an init message arrives as
 * `{__sdk_type: "SystemMessage", subtype: "init", data: {...}}`. The bridge
 * stores that private emission as `engine.diagnostic` with the original SDK
 * message at `payload.raw`, outside the browser-frame journal.
 */
function initPayloads(sessionId: string): Array<Record<string, unknown>> {
  const found: Array<Record<string, unknown>> = [];
  for (const event of documentsByField('session_events', '$.session_id', sessionId)) {
    if (event.event_type !== 'engine.diagnostic') continue;
    const payload = framePayload(event);
    if (payload.engine_kind !== 'claude_code' || payload.subtype !== 'init') continue;
    const raw = (payload.raw || {}) as Record<string, unknown>;
    expect(raw.__sdk_type, 'the init diagnostic must retain the native SystemMessage').toBe('SystemMessage');
    expect(raw.data, 'the native init diagnostic must carry its data object').toBeTruthy();
    expect(typeof raw.data).toBe('object');
    found.push(raw.data as Record<string, unknown>);
  }
  return found;
}

/**
 * Durable events that report a permission failure against the plugin data path.
 *
 * The vendor would report an unwritable `CLAUDE_PLUGIN_DATA` from whichever
 * subprocess touched it first. `translate_claude_sdk_message` publishes both
 * `SystemMessage` and `HookEventMessage` as private diagnostics carrying the
 * vendor's message verbatim, so a hook's report and a system message land in
 * the same journal under different subtypes. Reading every durable event of the
 * session covers whichever one carries it, without this spec having to predict
 * which subprocess that is.
 */
function pluginDataPermissionFailures(sessionId: string): string[] {
  const failures: string[] = [];
  for (const event of documentsByField('session_events', '$.session_id', sessionId)) {
    const text = JSON.stringify(event);
    if (!text.includes('plugins/data')) continue;
    if (!/EACCES|permission denied/i.test(text)) continue;
    failures.push(text.slice(0, 600));
  }
  return failures;
}

const sessionIds = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0, agentIds.length)) await api.deleteAgent(agentId);
});

test('a plugin conversation owns its mutable plugin data and registers every declared server', async ({
  page,
  request,
}) => {
  const revision = pinnedPluginRevision();
  const api = new AstraApi(request);

  // Clone the deployment-proven Agent's environment and model so this Agent
  // differs from it only by the plugin under test.
  const base = await api.defaultAgent();
  const environmentName = String(base.environment_name || '').trim();
  expect(environmentName, 'the deployed Agent should expose environment_name to clone').not.toEqual('');
  let model = String(base.model || '').trim();
  if (!model || model.includes('*')) {
    const models = await api.listEnvironmentModels(environmentName);
    model = models.find((candidate) => candidate && !candidate.includes('*')) || '';
  }
  expect(model, 'a concrete model id is required to mint a throwaway Agent').not.toEqual('');

  const agentName = `e2e plugin data ${RUN_ID}`;
  const agent = await api.createAgent({
    name: agentName,
    model,
    environment_name: environmentName,
    plugin_repos: [
      {
        url: PLUGIN_REPOSITORY,
        protocol: 'https',
        branch: 'main',
        depth: 1,
        sha: revision,
        plugin_paths: [PLUGIN_PATH],
      },
      {
        url: STATS_REPOSITORY,
        protocol: 'https',
        branch: 'main',
        depth: 1,
        sha: STATS_REVISION,
        plugin_paths: ['.'],
      },
    ],
  });
  agentIds.push(agent.agent_id);

  // Started from the Agent card, which is how a user reaches a conversation of
  // an Agent that was just arranged for.
  await page.goto(appPath('/agents'));
  const card = page.locator(`[data-testid="agent-option"][data-agent-name="${agentName}"]`);
  await expect(card, `the picker must offer ${agentName}`).toBeVisible({ timeout: 45_000 });
  await card.getByRole('button').click();
  await page.waitForURL((url) => /\/sessions\/[^/]+$/.test(url.pathname), { timeout: 120_000 });
  const sessionId = new URL(page.url()).pathname.split('/').filter(Boolean).pop() || '';
  expect(sessionId, 'the Agent card must open a concrete conversation').not.toEqual('');
  sessionIds.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 60_000 });

  const ready = await api.waitForSessionReady(sessionId);
  const sandboxId = String(ready.sandbox_id || '').trim();
  expect(sandboxId, 'a READY conversation should name its sandbox').not.toEqual('');
  const identity = expectRuntimeIdentity(await api.adminSessionDetail(sessionId));

  // The first real turn, deliberately tool-free: what it must produce is the
  // vendor's own startup and initialization, not a tool call.
  const before = await api.assistantCount(sessionId);
  const prompt = `E2E plugin data ${RUN_ID}: reply with one short sentence and do not use tools.`;
  await page.getByTestId('composer-prompt').fill(prompt);
  await page.getByTestId('composer-submit').click();
  await expect(page.getByTestId('user-message').last()).toContainText(prompt.slice(0, 16), {
    timeout: 30_000,
  });

  // The messages page merges the active-turn overlay, so a new assistant
  // message can be a stream still in flight. The turn's own durable terminal
  // proof is what says it finished, and says COMPLETED rather than any other
  // outcome — a failed turn also renders an assistant message.
  const streaming = await api.waitForAssistantMessageCount(sessionId, before, REPLY_BUDGET_MS);
  const turnId = String(streaming.turn_id || '').trim();
  expect(turnId, 'the reply must belong to a concrete turn').not.toEqual('');
  await waitForTurnTerminalProof(sessionId, turnId, 'COMPLETED', REPLY_BUDGET_MS);

  const settled = visibleMessages(await api.getMessages(sessionId, 50))
    .filter((message) => message.role === 'assistant' && message.turn_id === turnId);
  expect(settled.length, `the settled turn ${turnId} must carry its assistant reply`).toBeGreaterThan(0);
  const replyText = settled.map(messageText).join('\n').trim();
  expect(replyText, `the settled reply must carry text: ${JSON.stringify(settled)}`).not.toEqual('');
  expect(
    replyText,
    'a turn that reports COMPLETED must not have answered with a runtime failure',
  ).not.toMatch(/API Error|AGENT_RUNTIME_ERROR|SANDBOX_GONE|Traceback/i);

  const sandbox = await requireSandboxHandle(api, sandboxId);
  const hookLog = readPluginHookLog(sandbox, identity.configDir, identity.statsRepo);
  test.info().annotations.push({ type: 'e2e_plugin_owned_write', description: JSON.stringify(hookLog) });
  expect(hookLog).toMatchObject({ checkout_sha: STATS_REVISION, shallow: 'true', plugin_name: 'session-stats' });
  expect(hookLog.logs, 'the actual plugin hook must write into its vendor-provided data directory').toHaveLength(1);
  const log = hookLog.logs[0];
  expect(log.uid, 'plugin hook output must belong to the workload account').toBe(identity.uid);
  expect(log.path.startsWith(`${identity.configDir}/plugins/data/`)).toBe(true);
  const nativeRows = documentsByField('transcript_entries', '$.platform_session_id', sessionId)
    .filter((row) => row.subpath == null);
  const nativeSessionIds = [...new Set(nativeRows.map((row) => String(row.session_id || '')))];
  expect(nativeSessionIds).toHaveLength(1);
  expect(nativeSessionIds[0]).not.toEqual('');
  expect(log.entries.length).toBeGreaterThan(0);
  expect([...new Set(log.entries.map((entry) => entry.session_id))]).toEqual(nativeSessionIds);
  expect(log.entries.filter((entry) => entry.event === 'SessionStart').length).toBeGreaterThan(0);
  expect(log.entries.filter((entry) => entry.event === 'Stop').length).toBeGreaterThan(0);
  for (const entry of log.entries) {
    expect(entry.cwd).not.toEqual('');
    expect(Number.isFinite(Date.parse(entry.timestamp))).toBe(true);
  }
  const probe = probePluginData(
    sandbox,
    identity.configDir,
    identity.linuxUser,
    identity.pluginRepo,
    [PLUGIN_PATH],
    PROBE_TOKEN,
  );
  const probeText = JSON.stringify(probe);
  test.info().annotations.push({ type: 'e2e_plugin_data_probe', description: probeText });

  expect(
    probe.workload_euid,
    `the writability probe must run as the workload account, not root: ${probeText}`,
  ).toBe(identity.uid);
  expect(
    probe.plugin_data.owner,
    `the vendor's plugin state directory belongs to the workload account: ${probeText}`,
  ).toBe(identity.linuxUser);
  expect(
    probe.plugin_data.mode,
    `the vendor's plugin state directory is private to the workload account: ${probeText}`,
  ).toBe('0o700');
  expect(
    probe.workload_access_w_ok,
    `the workload account must hold write access to it: ${probeText}`,
  ).toBe(true);
  expect(
    probe.workload_write_read_back,
    `the workload account must actually create, write and read a file in it: ${probeText}`,
  ).toBe(PROBE_TOKEN);
  // Handing the state directory over must not hand over the plugin code above
  // it: what the platform installs there is part of what constrains the agent.
  expect(
    probe.plugin_root.owner,
    `the platform-installed plugin directory stays root-owned: ${probeText}`,
  ).toBe('root');
  expect(
    probe.plugin_root.mode,
    `the platform-installed plugin directory stays read-and-traverse only: ${probeText}`,
  ).toBe('0o755');

  expect(
    probe.plugin_configs.length,
    `the selected plugin must ship a plugin-owned .mcp.json: ${probeText}`,
  ).toBe(1);
  expect(
    probe.plugin_configs.map((config) => config.checkout_sha),
    'the prepared plugin must be the exact reviewed revision, not the moving branch tip',
  ).toEqual([revision]);
  expect(
    probe.plugin_configs.map((config) => config.shallow),
    'pinning the older revision must retain the requested shallow checkout',
  ).toEqual(['true']);
  const declaredServers = probe.plugin_configs.flatMap((config) => (
    config.server_names.map((name) => `plugin:${config.plugin_name}:${name}`)
  )).sort();
  expect(
    declaredServers.length,
    `the selected plugin must declare at least one MCP server: ${probeText}`,
  ).toBeGreaterThan(0);

  // The vendor publishes its initialization once the turn has actually started
  // the runner; the bridge persists its private diagnostic independently of
  // browser delivery, so the rendered reply is not its durability receipt.
  let payloads: Array<Record<string, unknown>> = [];
  await expect
    .poll(() => {
      payloads = initPayloads(sessionId);
      return payloads.length;
    }, { timeout: INIT_EVENT_BUDGET_MS })
    .toBeGreaterThan(0);
  const latest = payloads[payloads.length - 1];
  const servers = latest.mcp_servers;
  expect(
    Array.isArray(servers),
    `Claude's init must report an mcp_servers inventory; read ${oracleDbPath()}: ${JSON.stringify(latest).slice(0, 800)}`,
  ).toBe(true);
  const initNames = (servers as Array<Record<string, unknown>>)
    .map((server) => String(server.name || ''))
    .filter(Boolean)
    .sort();
  test.info().annotations.push({
    type: 'e2e_plugin_mcp_init',
    description: JSON.stringify({ initNames, declaredServers }),
  });
  // Compared whole, plugin qualifier included: this Agent declares no
  // `mcp_servers` of its own and `claude_code_config` sources that option from
  // the Agent alone, so the plugin's declarations are the entire expected
  // inventory, and two plugins declaring the same server name stay distinct.
  expect(
    initNames,
    `Claude's init must register every declared server under its plugin-qualified name: ${probeText}`,
  ).toEqual(declaredServers);

  expect(
    pluginDataPermissionFailures(sessionId),
    'no durable event may report a permission failure against the plugin data path',
  ).toEqual([]);
});
