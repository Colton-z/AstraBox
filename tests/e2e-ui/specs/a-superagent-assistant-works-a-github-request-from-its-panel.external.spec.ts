/**
 * Opt-in acceptance: a dsh-superagent Assistant takes a real GitHub request and
 * has one of its owner's Agents do the work, and keeps its record of it when
 * its box is lost.
 *
 * The owner has an Agent for changing code in GitHub repositories, which the
 * deployment's bot can push and open pull requests as through a Vault. The
 * owner opens the Assistant and lands on the bundle's own panel, connected to
 * its harness. Through that panel's API the GitHub Source is pointed at the
 * test repository and the asking account is allowed. A second GitHub account
 * then mentions the bot in a new issue. The Assistant takes it as its own
 * Ticket and hands it to that Agent: the work is a conversation of the Agent
 * that A2A started, owned by the Assistant's owner. The proof of the work is on
 * GitHub: the bot answers on the issue and opens that Ticket's pull request,
 * whose file is exactly what was asked for. A follow-up on the issue reaches the
 * same conversation, which the Ticket's page opens in the console. The box is
 * then deleted behind the platform's back; the Assistant comes back on a new box
 * by itself, and its panel shows the same Ticket, still opening that
 * conversation. A last follow-up asks for other work, which the Agent hands
 * back: superagent sends the message to a fresh assessment with the Agent's
 * reason, and the Ticket keeps waiting for review.
 *
 * Needs (ASTRABOX_E2E_EXTERNAL_ACCOUNTS=1):
 * - ASTRABOX_E2E_SUPERAGENT_RUNTIME_PROFILE: an enabled RuntimeProfile on the
 *   dsh_superagent engine, in a deployment whose shared bot token is set.
 * - ASTRABOX_E2E_SUPERAGENT_WORKER_RUNTIME_PROFILE: a RuntimeProfile of an
 *   engine whose image has git and gh, for the Agent that does the work.
 * - ASTRABOX_E2E_SUPERAGENT_REPOSITORY: owner/name the bot was invited to.
 * - ASTRABOX_E2E_SUPERAGENT_AGENT_LOGIN: the bot's GitHub login.
 * - ASTRABOX_E2E_SUPERAGENT_AGENT_TOKEN_FILE: the bot's token, which the test
 *   puts in the working Agent's Vault, as an administrator would.
 * - ASTRABOX_E2E_SUPERAGENT_REQUESTER_TOKEN_FILE: a token of the account that
 *   asks, able to open and close issues and pull requests there.
 * No token is printed; the repository is left with the issue and the pull
 * requests of the run, including one a handed-back request led to, closed and
 * their branches deleted.
 */
import fs from 'node:fs';

import { expect, test, type APIRequestContext, type Frame, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sessionInputRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { a2aConversations, loseTheBoxAndRecover, openOnPanel, panelApi, panelFrame, type Ticket } from '../fixtures/superagentPanel';

function required(name: string): string {
  const value = (process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const profile = required('ASTRABOX_E2E_SUPERAGENT_RUNTIME_PROFILE');
const workerProfile = required('ASTRABOX_E2E_SUPERAGENT_WORKER_RUNTIME_PROFILE');
const repository = required('ASTRABOX_E2E_SUPERAGENT_REPOSITORY');
const agentLogin = required('ASTRABOX_E2E_SUPERAGENT_AGENT_LOGIN');
const agentToken = fs.readFileSync(required('ASTRABOX_E2E_SUPERAGENT_AGENT_TOKEN_FILE'), 'utf8').trim();
const requesterToken = fs.readFileSync(required('ASTRABOX_E2E_SUPERAGENT_REQUESTER_TOKEN_FILE'), 'utf8').trim();

async function github<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${requesterToken}`,
      Accept: 'application/vnd.github+json',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`GitHub ${method} ${path} returned HTTP ${response.status}`);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

async function platform<T>(request: APIRequestContext, method: 'POST' | 'PUT' | 'DELETE', path: string, data?: unknown): Promise<T> {
  const response = await request.fetch(apiPath(path), { method, data });
  const text = await response.text();
  // The bot token is in the request; the response holds no secret, but say nothing more than the status on failure.
  if (!response.ok()) throw new Error(`${method} ${path} returned HTTP ${response.status()}`);
  return (text ? JSON.parse(text).data : undefined) as T;
}

/**
 * The Ticket as its owner finds it: on the Execution page, whose default filter lists every Ticket that has not
 * ended, whether it is working, waiting on a person or waiting for its next Trigger.
 */
async function showsTicket(frame: Frame, title: string): Promise<void> {
  const notice = frame.getByRole('dialog').filter({ has: frame.getByRole('button', { name: /^(Continue|继续)$/ }) });
  await expect(async () => {
    if (await notice.isVisible()) await notice.getByRole('button', { name: /^(Continue|继续)$/ }).click();
    await frame.getByRole('tab', { name: /^(Execution|执行)$/ }).click({ timeout: 2_000 });
  }).toPass({ timeout: 30_000 });
  await expect(frame.getByText(title).first()).toBeVisible();
}

/** The Ticket's page in the panel opens the Agent's conversation in the console. */
async function opensConversation(page: Page, frame: Frame, title: string, sessionId: string): Promise<void> {
  await frame.getByRole('button', { name: title, exact: true }).first().click();
  await frame.getByRole('button', { name: /^(Open conversation|打开对话)$/ }).click();
  await expect(page).toHaveURL(new RegExp(`/sessions/${sessionId}$`));
}

let assistantId = '';
let workerId = '';
let vaultId = '';
let issueNumber = 0;
let pullNumber = 0;
let branch = '';
let head = '';
let started = '';
onPassOnly(async ({ request }) => {
  if (pullNumber) await github('PATCH', `/repos/${repository}/pulls/${pullNumber}`, { state: 'closed' });
  if (branch) await github('DELETE', `/repos/${repository}/git/refs/heads/${branch}`);
  // A handed-back request is routed again and may become a Ticket with a pull request of its own.
  const others = await github<Array<{ number: number; user: { login: string }; head: { ref: string }; created_at: string }>>(
    'GET', `/repos/${repository}/pulls?state=open&per_page=50`);
  for (const other of others.filter((pull) => pull.user.login === agentLogin && started && pull.created_at >= started)) {
    await github('PATCH', `/repos/${repository}/pulls/${other.number}`, { state: 'closed' });
    await github('DELETE', `/repos/${repository}/git/refs/heads/${other.head.ref}`);
  }
  if (issueNumber) await github('PATCH', `/repos/${repository}/issues/${issueNumber}`, { state: 'closed' });
  const api = new AstraApi(request);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (workerId) await api.deleteAgent(workerId);
  if (vaultId) await platform(request, 'POST', `/admin/vaults/${vaultId}/archive`);
});

test('a dsh-superagent Assistant has an owner\'s Agent work a GitHub mention, and keeps its Ticket on a new box', async ({ page, request }) => {
  test.setTimeout(50 * 60_000);
  const api = new AstraApi(request);
  const marker = `superagent-e2e-${Date.now().toString(36)}`;
  started = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  const requester = (await github<{ login: string }>('GET', '/user')).login;

  // The Agent that works the Tickets, with the bot's GitHub access in a Vault: Git over HTTPS and the GitHub API.
  const vault = await platform<{ vault_id: string }>(request, 'POST', '/admin/vaults', { display_name: `__e2e_${marker}` });
  vaultId = vault.vault_id;
  await platform(request, 'POST', `/admin/vaults/${vaultId}/credentials`, { display_name: 'GitHub over HTTPS',
    auth: { type: 'http_basic', url: `https://github.com/${repository}.git`, username: agentLogin, password: agentToken } });
  await platform(request, 'POST', `/admin/vaults/${vaultId}/credentials`, { display_name: 'GitHub API',
    auth: { type: 'environment_variable', secret_name: 'GH_TOKEN', secret_value: agentToken,
      networking: { type: 'limited', allowed_hosts: ['api.github.com'] }, injection_location: { header: true, body: false } } });
  const workerName = `__e2e_coder_${marker}`;
  const worker = await api.createAgent({
    name: workerName,
    description: 'Changes code in GitHub repositories and opens pull requests for the changes.',
    model: String((await api.defaultAgent()).model),
    runtime_profile_name: workerProfile,
    prepared_capacity: 0,
  });
  workerId = String(worker.agent_id);
  const binding = await platform<{ vault_ids: string[] }>(request, 'PUT', `/admin/agents/${workerId}/credential-vaults`,
    { vault_ids: [vaultId] });
  expect(binding.vault_ids).toEqual([vaultId]);

  const assistant = await api.createAssistant({
    display_name: `__e2e_${marker}`,
    runtime_profile_name: profile,
    model_config_override: { model_name: await api.assistantModelName(profile) },
  });
  assistantId = String(assistant.assistant_id || '');
  expect(assistantId).not.toEqual('');
  const owner = String(assistant.owner_id || '');

  const frame = await openOnPanel(page, assistantId);
  // The panel shows the console's language, and follows a switch made in the console.
  await page.getByTestId('user-menu-trigger').click();
  const language = page.getByRole('combobox', { name: /^(Language|语言)$/ });
  for (const code of ['zh', 'en']) {
    await language.selectOption(code);
    await expect.poll(() => frame.evaluate(() => document.documentElement.lang), { timeout: 15_000 })
      .toMatch(new RegExp(`^${code}\\b`, 'i'));
  }
  await page.keyboard.press('Escape');
  const settings = await panelApi<{
    blocks: Record<string, { value: Record<string, unknown>; revision: number }>;
    sources: Array<{ id: string; value: { enabled: boolean; config: Record<string, unknown> }; revision: number }>;
  }>(frame, 'GET', 'settings');
  const source = settings.sources.find((entry) => entry.id === 'github');
  if (!source) throw new Error('the bundle offers no GitHub Source');
  await panelApi(frame, 'PUT', 'settings/source:github', {
    value: { enabled: true, config: { ...source.value.config, repository, cli: '/usr/local/bin/gh-agent' } },
    revision: source.revision,
  });
  const policy = settings.blocks.policy;
  await panelApi(frame, 'PUT', 'settings/policy', {
    value: { ...policy.value, sources: { ...(policy.value.sources as object), github: { allowed: [{ id: requester }], admission: {} } } },
    revision: policy.revision,
  });
  // The prepare command now runs in the working Agent's workspace.
  const executor = settings.blocks.executor;
  await panelApi(frame, 'PUT', 'settings/executor', {
    value: {
      ...executor.value,
      prepare: 'git clone --quiet "https://github.com/$DSH_SUPERAGENT_GITHUB_REPOSITORY.git" "$DSH_SUPERAGENT_DIR" '
        + '&& git -C "$DSH_SUPERAGENT_DIR" checkout -q -b "agent/$DSH_SUPERAGENT_NAME"',
    },
    revision: executor.revision,
  });
  await expect.poll(async () => {
    const state = await panelApi<{ setup: { problem?: unknown; accounts?: { agent?: string; repository?: string } } }>(frame, 'GET', 'state');
    return [state.setup.problem ?? null, state.setup.accounts?.agent, state.setup.accounts?.repository];
  }, { timeout: 120_000, intervals: [3_000] }).toEqual([null, agentLogin, repository]);
  const before = new Set((await panelApi<{ items: Ticket[] }>(frame, 'GET', 'tickets')).items.map((ticket) => ticket.id));

  const file = `${marker}.md`;
  const issue = await github<{ number: number }>('POST', `/repos/${repository}/issues`, {
    title: `Add ${file}`,
    body: `@${agentLogin} please add a file \`${file}\` at the repository root containing exactly one line: `
      + `\`${marker}\`. Open a pull request for it. Have the ${workerName} agent do it.`,
  });
  issueNumber = issue.number;

  // This Assistant took the issue: a Ticket of its own follows it.
  let ticket: Ticket = { id: '', title: '', threads: [] };
  await expect.poll(async () => {
    const items = (await panelApi<{ items: Ticket[] }>(await panelFrame(page), 'GET', 'tickets')).items;
    ticket = items.find((entry) => !before.has(entry.id)
      && entry.threads.some((thread) => thread.url?.endsWith(`/${repository}/issues/${issueNumber}`))) ?? ticket;
    return Boolean(ticket.id && ticket.directory);
  }, { message: 'the Assistant opens a Ticket on the issue', timeout: 10 * 60_000, intervals: [5_000] }).toBe(true);
  // The bundle binds a pull request to a Ticket by its branch: the last path segment is the Ticket's name.
  const ticketName = String(ticket.directory).split('/').at(-1);

  // The work is a conversation of the working Agent, started over A2A and owned by the Assistant's owner.
  await expect.poll(() => a2aConversations(workerId).length,
    { message: 'the Ticket goes to the working Agent', timeout: 5 * 60_000, intervals: [5_000] }).toBe(1);
  const [conversation] = a2aConversations(workerId);
  expect(conversation.user_id).toEqual(owner);
  // The A2A service creates the conversation, then records its message.
  await expect.poll(() => sessionInputRows(conversation.session_id).length,
    { message: 'the Ticket\'s first message reaches the conversation', timeout: 60_000, intervals: [2_000] }).toBe(1);
  const firstInputs = sessionInputRows(conversation.session_id);
  expect(firstInputs[0].content, 'the Agent is told what was asked, and the Ticket\'s name').toContain(marker);
  expect(firstInputs[0].content).toContain(ticketName);

  // GitHub is the evidence: the bot's reply and this Ticket's pull request with exactly the asked-for file.
  await expect.poll(async () => {
    const pulls = await github<Array<{ number: number; user: { login: string }; head: { ref: string; sha: string } }>>(
      'GET', `/repos/${repository}/pulls?state=open&per_page=50`);
    const pull = pulls.find((candidate) => candidate.user.login === agentLogin && candidate.head.ref.split('/').at(-1) === ticketName);
    if (!pull) return [];
    pullNumber = pull.number;
    branch = pull.head.ref;
    head = pull.head.sha;
    return (await github<Array<{ filename: string }>>('GET', `/repos/${repository}/pulls/${pull.number}/files`))
      .map((entry) => entry.filename);
  }, { message: "the Ticket's pull request changes exactly the asked-for file", timeout: 30 * 60_000, intervals: [20_000] })
    .toEqual([file]);
  const content = await github<{ content: string }>('GET', `/repos/${repository}/contents/${file}?ref=${encodeURIComponent(branch)}`);
  expect(Buffer.from(content.content, 'base64').toString('utf8').trim()).toEqual(marker);
  await expect.poll(async () => {
    const comments = await github<Array<{ user: { login: string }; body: string }>>(
      'GET', `/repos/${repository}/issues/${issueNumber}/comments`);
    return comments.some((comment) => comment.user.login === agentLogin && comment.body.includes(`/pull/${pullNumber}`));
  }, { message: 'the bot answers on the issue with its pull request', timeout: 10 * 60_000, intervals: [10_000] }).toBe(true);

  // The Ticket waits for a person's review, and its result names what the Agent pushed, which superagent reads from the
  // task's outcome extension (docs/a2a.md, "Task outcome extension"): the branch, the pull request's head commit and the
  // pull request.
  let reviewed: Ticket | undefined;
  await expect.poll(async () => {
    reviewed = (await panelApi<{ items: Ticket[] }>(await panelFrame(page), 'GET', 'tickets')).items
      .find((entry) => entry.id === ticket.id);
    return [reviewed?.stage, reviewed?.action];
  }, { message: 'the Ticket waits for review', timeout: 5 * 60_000, intervals: [5_000] }).toEqual(['person', 'review']);
  const pushed = (reviewed?.summary ?? '').split('\n').find((line) => line.startsWith('Pushed: ')) ?? '';
  expect(pushed, 'the result names the branch').toContain(`branch ${branch}`);
  expect(pushed, 'and the pull request').toContain(`pull_request https://github.com/${repository}/pull/${pullNumber}`);
  const commit = /\bcommit ([0-9a-f]{7,64})\b/.exec(pushed)?.[1] ?? '';
  expect(head.startsWith(commit) && commit.length > 0, `the result names the pull request's head commit ${head}: ${pushed}`).toBe(true);

  // A follow-up on the issue reaches the same conversation of the same Agent.
  const followUp = `follow-up-${marker}`;
  await github('POST', `/repos/${repository}/issues/${issueNumber}/comments`,
    { body: `@${agentLogin} thanks; reply with the word ${followUp} so I know you saw this.` });
  await expect.poll(() => sessionInputRows(conversation.session_id).findIndex((row) => row.content.includes(followUp)),
    { message: 'the follow-up arrives as the second input of the Ticket\'s conversation', timeout: 10 * 60_000, intervals: [10_000] })
    .toBe(1);
  expect(a2aConversations(workerId), 'a follow-up starts no second conversation').toHaveLength(1);
  // The Agent's answer to the follow-up reaches the person on the issue.
  await expect.poll(async () => {
    const comments = await github<Array<{ user: { login: string }; body: string; created_at: string }>>(
      'GET', `/repos/${repository}/issues/${issueNumber}/comments`);
    return comments.some((comment) => comment.user.login === agentLogin && comment.body.includes(followUp));
  }, { message: 'the bot answers the follow-up on the issue', timeout: 10 * 60_000, intervals: [10_000] }).toBe(true);

  // The panel's address is renewed while the work runs, so the frame is looked up again.
  const shown = await panelFrame(page);
  const worked = (await panelApi<{ items: Ticket[] }>(shown, 'GET', 'tickets')).items.filter((entry) => !before.has(entry.id));
  expect(worked.map((entry) => entry.id)).toEqual([ticket.id]);
  await showsTicket(shown, ticket.title);
  await opensConversation(page, shown, ticket.title, conversation.session_id);

  // Nobody is looking from here until the workspace is back.
  const context = page.context();
  await page.close();
  await loseTheBoxAndRecover(api, assistantId);

  const again = await context.newPage();
  const reopened = await openOnPanel(again, assistantId);
  const kept = (await panelApi<{ items: Ticket[] }>(reopened, 'GET', 'tickets')).items;
  expect(kept.find((entry) => entry.id === ticket.id)?.title).toEqual(ticket.title);
  await showsTicket(reopened, ticket.title);
  await opensConversation(again, reopened, ticket.title, conversation.session_id);

  // A follow-up that asks for other work: the Agent hands it back. Its task ends rejected with the hand-back in the
  // task outcome extension, and superagent hands the message back as a worker does (upstream's Engine.handback): the
  // message goes to a fresh assessment carrying the Agent's reason, and the Ticket keeps its work.
  const elsewhere = `handback-${marker}`;
  // The page shows the Agent's conversation now; the Ticket's page is in the panel again.
  await openOnPanel(again, assistantId);
  await github('POST', `/repos/${repository}/issues/${issueNumber}/comments`, {
    body: `@${agentLogin} ${elsewhere}: a separate request, translate README.md into French. It is not part of this pull `
      + 'request and is another agent\'s work: hand it back for routing instead of doing it.' });
  await expect.poll(async () => {
    const detail = await panelApi<{ inputs: Array<{ trigger?: { preview: string; handback?: { reason: string } } }> }>(
      await panelFrame(again), 'GET', `tickets/${ticket.id}`);
    return detail.inputs.find((input) => input.trigger?.preview.includes(elsewhere))?.trigger?.handback?.reason ?? '';
  }, { message: 'superagent hands the follow-up back with the Agent\'s reason', timeout: 15 * 60_000, intervals: [10_000] })
    .not.toEqual('');
  const outcomes = postgresRows<{ data: { state: string; handback?: boolean; message: string } }>(
    `SELECT data FROM tool_cards WHERE session_id = ${sqlLiteral(conversation.session_id)} AND kind = 'a2a_task_outcome' `
      + 'ORDER BY created_at').map((row) => row.data);
  expect(outcomes.at(-1), 'the Agent reports the follow-up rejected and handed back')
    .toMatchObject({ state: 'TASK_STATE_REJECTED', handback: true });
  const still = (await panelApi<{ items: Ticket[] }>(await panelFrame(again), 'GET', 'tickets')).items
    .find((entry) => entry.id === ticket.id);
  expect([still?.stage, still?.action], 'the Ticket keeps waiting for review of its pull request').toEqual(['person', 'review']);
});
