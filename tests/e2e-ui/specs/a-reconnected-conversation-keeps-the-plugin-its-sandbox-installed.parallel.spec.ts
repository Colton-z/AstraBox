/**
 * E2E: a conversation that reconnects to its sandbox runs the Agent version
 * that sandbox was prepared from.
 *
 * An Agent declares Claude Code Plugin A and a conversation is prepared with
 * it. The Agent is then edited to declare Plugin B instead. The conversation's
 * runtime is evicted from the server while its sandbox keeps running, so its
 * next message reconnects to that sandbox. Claude's options for that
 * reconnect must come from the version that installed A: built from the edited
 * Agent, they name a Plugin the sandbox does not hold, the reconnect is refused
 * with PACKAGE_INVALID, and the message stays pending.
 *
 * The spec reads
 *
 *   · the capability plan the conversation recorded (Plugin A's checkout and
 *     the Agent version it was prepared from), unchanged across the reconnect;
 *   · the message sent after the reconnect: accepted once and answered by one
 *     completed response, with the first exchange still in history;
 *   · Claude's own SystemMessage(init) for each start of the conversation's
 *     engine, whose plugins must be A and never B;
 *   · a new conversation of the edited Agent, whose plan records the current
 *     version and B, and whose init loads B.
 */
import { spawnSync } from 'node:child_process';

import { test, expect } from '@playwright/test';

import {
  AstraApi,
  messageText,
  visibleMessages,
  type AdminSessionRecord,
  type MessageRecord,
} from '../fixtures/astraApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { sessionInputRows, sessionResponseRows } from '../fixtures/dbOracle';
import { parseTimeoutEnv } from '../fixtures/env';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const REPLY_BUDGET_MS = parseTimeoutEnv('ASTRABOX_E2E_TURN_TIMEOUT_MS', 90_000);

// Two public plugins pinned to reviewed revisions, each named by its own
// .claude-plugin/plugin.json.
const PLUGIN_A = {
  kind: 'claude-plugin',
  url: 'https://github.com/reidsolon/claude-session-stats-plugin.git',
  protocol: 'https',
  branch: 'main',
  depth: 1,
  sha: '84433f7d3740b3b1c4a238a705f3cfb17d8bd3d8',
  paths: ['.'],
};
const PLUGIN_A_NAME = 'session-stats';
const PLUGIN_B = {
  kind: 'claude-plugin',
  url: 'https://github.com/anthropics/financial-services.git',
  protocol: 'https',
  branch: 'main',
  depth: 1,
  sha: '3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f',
  paths: ['plugins/vertical-plugins/financial-analysis'],
};
const PLUGIN_B_NAME = 'financial-analysis';
const PLUGIN_NAMES = [PLUGIN_A_NAME, PLUGIN_B_NAME];

interface PreparedPlan {
  agentVersion: unknown;
  plugins: Array<{ url: unknown; target: string }>;
}

/** The capability plan a conversation recorded when its sandbox was prepared. */
function preparedPlan(detail: AdminSessionRecord): PreparedPlan {
  const identity = (detail.runtime_identity || {}) as Record<string, unknown>;
  const plan = (identity.capability_plan || {}) as Record<string, unknown>;
  const repos = Array.isArray(plan.plugin_repo_plan)
    ? (plan.plugin_repo_plan as Array<Record<string, unknown>>)
    : [];
  return {
    agentVersion: plan.agent_version,
    plugins: repos.map((repo) => ({ url: repo.url, target: String(repo.target_path || '') })),
  };
}

/**
 * The value of the first "plugins" field in one diagnostic line.
 *
 * The server cuts a diagnostic line at a size limit, so the array is read out
 * of the text by its brackets instead of parsing a line that may be cut.
 */
function pluginsField(raw: string, sessionId: string): Array<{ name: string; path: string }> {
  const key = '"plugins":';
  const at = raw.indexOf(key);
  if (at < 0) throw new Error(`Claude's init for session ${sessionId} has no plugins field: ${raw.slice(0, 400)}`);
  const start = at + key.length;
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = start; index < raw.length; index += 1) {
    const char = raw[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
    } else if (char === '"') {
      inString = true;
    } else if (char === '[' || char === '{') {
      depth += 1;
    } else if (char === ']' || char === '}') {
      depth -= 1;
      if (depth === 0) return JSON.parse(raw.slice(start, index + 1)) as Array<{ name: string; path: string }>;
    }
  }
  throw new Error(`Claude's init for session ${sessionId} was cut before its plugins field ended`);
}

/**
 * The plugins Claude loaded at each start of a conversation's engine.
 *
 * The engine adapter reports Claude's SystemMessage(init) as a runtime
 * diagnostic, which the session's reader writes to the server log with its raw
 * evidence (runtime diagnostic session=<id> type=<type> raw=<json>).
 */
function initPlugins(sessionId: string): Array<Array<{ name: string; path: string }>> {
  const container = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  // The server logs to stderr; docker logs replays each stream on its own.
  const read = spawnSync('docker', ['logs', container], { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 });
  if (read.status !== 0) throw new Error(`docker logs ${container} failed: ${read.stderr}`);
  const prefix = `runtime diagnostic session=${sessionId} type=`;
  return `${read.stdout}\n${read.stderr}`
    .split('\n')
    .filter((line) => line.includes(prefix))
    .map((line) => line.slice(line.indexOf(prefix)))
    .filter((line) => /^[^ ]*\.init raw=/.test(line.slice(prefix.length)))
    .map((line) => pluginsField(line.slice(line.indexOf(' raw=') + ' raw='.length), sessionId));
}

/** The names of the plugins under test that each engine start loaded. */
function loadedTestPlugins(sessionId: string): string[][] {
  return initPlugins(sessionId).map((plugins) => (
    plugins.map((plugin) => String(plugin.name)).filter((name) => PLUGIN_NAMES.includes(name)).sort()
  ));
}

/** Send one tool-free message and wait for its response to end. */
async function exchange(
  api: AstraApi,
  sessionId: string,
  prompt: string,
): Promise<{ inputId: string; reply: MessageRecord }> {
  const inputId = (await api.postTurnInput(sessionId, prompt)).input_id;
  try {
    return { inputId, reply: await api.waitForReplyTo(sessionId, inputId, { timeoutMs: REPLY_BUDGET_MS }) };
  } catch (error) {
    const session = (await api.getSession(sessionId)) as unknown as Record<string, unknown>;
    throw new Error(
      `${(error as Error).message}; session state=${String(session.state)} `
        + `runtime_unavailable=${String(session.runtime_unavailable)} last_error=${String(session.last_error)}`,
    );
  }
}

/** The response answered the message once, completed, and carries text. */
function expectCompleted(sessionId: string, inputId: string, reply: MessageRecord): void {
  expect(reply.metadata?.outcome, 'the response to the message must complete').toBe('completed');
  expect(messageText(reply).trim(), 'the settled reply must carry text').not.toEqual('');
  expect(
    sessionInputRows(sessionId).filter((row) => row.input_id === inputId),
    'the platform accepted the message once and recorded the response that answered it',
  ).toEqual([expect.objectContaining({ response_id: reply.id })]);
  expect(
    sessionResponseRows(sessionId).filter((row) => row.input_ids.includes(inputId)),
    'exactly one response answers the message, and it completed',
  ).toEqual([expect.objectContaining({ response_id: reply.id, status: 'ended', outcome: 'completed' })]);
}

const sessionIds = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds.splice(0, agentIds.length)) await api.deleteAgent(agentId);
});

test('a reconnected conversation keeps the Plugin its sandbox installed after the Agent changes it', async ({
  request,
}) => {
  const api = new AstraApi(request);
  // The deployment's conversation-tenancy RuntimeProfile: each conversation
  // owns its sandbox, so the reconnect below reaches the sandbox that
  // installed Plugin A and nothing else.
  const runtimeProfileName = String(process.env.ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT || '').trim();
  expect(runtimeProfileName, 'ASTRABOX_E2E_CREDENTIAL_COLD_ENVIRONMENT names the cold RuntimeProfile').not.toEqual('');
  const model = String((await api.defaultAgent()).model || '').trim();
  expect(model, 'the deployment-proven Agent names its model').not.toEqual('');

  const agent = await api.createAgent({
    name: `e2e plugin version ${RUN_ID}`,
    model,
    runtime_profile_name: runtimeProfileName,
    prepared_capacity: 0,
    packages: [PLUGIN_A],
  });
  agentIds.push(agent.agent_id);
  const preparedVersion = Number(agent.version);

  const sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessionIds.push(sessionId);
  test.info().annotations.push({ type: 'e2e_session_id', description: sessionId });
  const sandboxId = String((await api.waitForSessionReady(sessionId)).sandbox_id || '');
  const planA = preparedPlan(await api.adminSessionDetail(sessionId));
  expect(planA.agentVersion, 'the plan records the Agent version it installs').toBe(preparedVersion);
  expect(planA.plugins.map((plugin) => plugin.url), 'the plan installs Plugin A').toEqual([PLUGIN_A.url]);

  const firstPrompt = `E2E plugin version ${RUN_ID} first: reply with one short sentence and do not use tools.`;
  const first = await exchange(api, sessionId, firstPrompt);
  expectCompleted(sessionId, first.inputId, first.reply);

  const current = await api.getAgent(agent.agent_id);
  const edited = await api.updateAgent(agent.agent_id, {
    name: current.name,
    model: current.model,
    runtime_profile_name: current.runtime_profile_name,
    packages: [PLUGIN_B],
    version: current.version,
  });
  expect(Number(edited.version), 'replacing the Plugin makes a new Agent version').toBe(preparedVersion + 1);

  // The edited Agent's new conversation is prepared while the first one
  // reconnects; each is read on its own below.
  const newSessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessionIds.push(newSessionId);
  test.info().annotations.push({ type: 'e2e_new_session_id', description: newSessionId });

  expect((await api.adminEvictRuntime(sessionId)).evicted, 'the server drops the conversation runtime').toBe(sessionId);
  const reconnectPrompt = `E2E plugin version ${RUN_ID} after edit: reply with one short sentence and do not use tools.`;
  const newPrompt = `E2E plugin version ${RUN_ID} new conversation: reply with one short sentence and do not use tools.`;
  const [reconnected, fresh] = await Promise.all([
    exchange(api, sessionId, reconnectPrompt),
    api.waitForSessionReady(newSessionId).then(() => exchange(api, newSessionId, newPrompt)),
  ]);

  expectCompleted(sessionId, reconnected.inputId, reconnected.reply);
  const after = await api.adminSessionDetail(sessionId);
  expect(after.sandbox_id, 'the message was answered in the sandbox the conversation already had').toBe(sandboxId);
  expect(preparedPlan(after), 'the reconnect keeps the plan that installed Plugin A').toEqual(planA);
  const history = visibleMessages(await api.getMessages(sessionId, 50));
  expect(
    history.filter((message) => message.role === 'user').map((message) => messageText(message).trim()),
    'history keeps both messages',
  ).toEqual([firstPrompt, reconnectPrompt]);
  expect(
    history.filter((message) => message.role === 'assistant').map((message) => message.id),
    'history keeps both replies',
  ).toEqual([first.reply.id, reconnected.reply.id]);
  test.info().annotations.push({
    type: 'e2e_reconnected_init_plugins',
    description: JSON.stringify(initPlugins(sessionId)),
  });
  const loaded = loadedTestPlugins(sessionId);
  expect(loaded.length, 'Claude reported its start in this conversation').toBeGreaterThan(0);
  expect(loaded, 'every start of the conversation loaded Plugin A and never Plugin B')
    .toEqual(loaded.map(() => [PLUGIN_A_NAME]));

  expectCompleted(newSessionId, fresh.inputId, fresh.reply);
  const planB = preparedPlan(await api.adminSessionDetail(newSessionId));
  expect(planB.agentVersion, 'a new conversation is prepared from the current version').toBe(preparedVersion + 1);
  expect(planB.plugins.map((plugin) => plugin.url), 'a new conversation installs Plugin B').toEqual([PLUGIN_B.url]);
  test.info().annotations.push({
    type: 'e2e_new_init_plugins',
    description: JSON.stringify(initPlugins(newSessionId)),
  });
  const freshLoaded = loadedTestPlugins(newSessionId);
  expect(freshLoaded.length, 'Claude reported its start in the new conversation').toBeGreaterThan(0);
  expect(freshLoaded, 'the new conversation loaded Plugin B and never Plugin A')
    .toEqual(freshLoaded.map(() => [PLUGIN_B_NAME]));
});
