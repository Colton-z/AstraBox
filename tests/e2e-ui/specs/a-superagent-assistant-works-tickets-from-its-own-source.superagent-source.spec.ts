/**
 * A dsh-superagent Assistant works from an organization's own ticket service,
 * with no GitHub, and reaches that service with a credential from its Vault.
 *
 * The Source is a plugin in its own package (fixtures/superagent-todo-source),
 * added the two ways the Assistants guide gives: built into an image on
 * AstraBox's, or installed by the owner on the Assistant as a plugin Bundle.
 * The to-do service (fixtures/todo_service.py) runs here and accepts only the
 * token the Vault holds; the box holds a placeholder that its outbound proxy
 * replaces. The owner has an Agent that runs commands, and each item's work is a
 * conversation of that Agent the Assistant started over A2A, owned by the
 * Assistant's owner. The proof is the service's own record: the agent's replies
 * on the items people filed, with what this test computes itself, and the
 * requests it refused; and the Agent's conversation that was given each item.
 *
 * The image case: the owner configures the Source; an administrator assigns
 * the Vault to the running Assistant; a person's item is answered and the panel
 * shows its Ticket; on a new box after the old one is lost the Ticket stays and
 * items are answered; a rotated credential reaches the service; an archived
 * one is not delivered.
 *
 * The Bundle case, on the plain image: the owner installs the Source with
 * Profile package.json, configures it once it appears, and it answers before
 * and after the box is lost, on a box that installs it again.
 *
 * The outcome case: the Agent declines one item, which its A2A task reports as
 * rejected; that Ticket ends dropped with the Agent's reason and the person is
 * told it. It does another item, whose report reaches the Ticket's result ahead
 * of its reply, and the person's answer.
 *
 * Needs ASTRABOX_E2E_SUPERAGENT_SOURCE_RUNTIME_PROFILE (a dsh_superagent
 * RuntimeProfile on that image), ASTRABOX_E2E_SUPERAGENT_RUNTIME_PROFILE (one
 * on the plain image), ..._WORKER_RUNTIME_PROFILE (the working Agent's),
 * ..._SOURCE_SERVICE_URL (the service as boxes reach it, on port 80) and
 * ..._SOURCE_LISTEN (host:port this runner serves it on).
 */
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { PlatformApi } from '../fixtures/platformApi';
import { postgresRows, sessionInputRows, sqlLiteral } from '../fixtures/dbOracle';
import { onPassOnly } from '../fixtures/sessionCleanup';
import { a2aConversations, loseTheBoxAndRecover, openOnPanel, panelApi, type Ticket } from '../fixtures/superagentPanel';

function required(name: string): string {
  const value = (process.env[name] || '').trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

const profile = required('ASTRABOX_E2E_SUPERAGENT_SOURCE_RUNTIME_PROFILE');
const plainProfile = required('ASTRABOX_E2E_SUPERAGENT_RUNTIME_PROFILE');
const workerProfile = required('ASTRABOX_E2E_SUPERAGENT_WORKER_RUNTIME_PROFILE');
const serviceUrl = required('ASTRABOX_E2E_SUPERAGENT_SOURCE_SERVICE_URL').replace(/\/$/, '');
const [listenHost, listenPort] = required('ASTRABOX_E2E_SUPERAGENT_SOURCE_LISTEN').split(':');
const adminKey = randomUUID();
const REQUESTER = 'requester';

type Comment = { id: string; author: string; text: string };
/** The owner's Agent that works the items, and the owner the Assistant works for. */
type Work = { agentId: string; agentName: string; owner: string };

async function service<T>(method: string, route: string, body?: unknown): Promise<T> {
  const response = await fetch(`http://${listenHost}:${listenPort}${route}`, {
    method,
    headers: { 'X-Admin-Key': adminKey, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`to-do service ${method} ${route} returned HTTP ${response.status}`);
  return (await response.json()) as T;
}

function startService(token: string, tarball?: string): Promise<ChildProcess> {
  const child = spawn('python3', [path.join(__dirname, '..', 'fixtures', 'todo_service.py'),
    '--host', listenHost, '--port', listenPort, '--admin-key', adminKey, '--token', token,
    ...(tarball ? ['--package', tarball] : [])], { stdio: ['ignore', 'pipe', 'inherit'] });
  return new Promise((resolve, reject) => {
    child.once('exit', (code) => reject(new Error(`the to-do service exited with ${code}`)));
    child.stdout!.on('data', (chunk: Buffer) => { if (chunk.toString().includes('"listening"')) resolve(child); });
  });
}

/** What a person asks on an item: work only a command answers, checked here independently. */
function digestRequest(marker: string, agentName: string): { text: string; answer: string } {
  return {
    text: `@agent please run a command to compute the SHA-256 of the exact text "${marker}" (no trailing newline) `
      + `and reply on this item with the hex digest. Have the ${agentName} agent do it.`,
    answer: createHash('sha256').update(marker).digest('hex'),
  };
}

/**
 * File an item as a person and wait for the agent's answer on it; the work was one conversation of the owner's Agent,
 * told what the item asked.
 */
async function fileAndAwaitAnswer(work: Work, marker: string, timeout: number): Promise<string> {
  const { text, answer } = digestRequest(marker, work.agentName);
  const item = await service<{ id: string }>('POST', '/admin/items', { title: `Digest ${marker}`, author: REQUESTER, text });
  await expect.poll(async () => (await service<Comment[]>('GET', `/admin/items/${item.id}/comments`))
    .some((comment) => comment.author === 'agent' && comment.text.toLowerCase().includes(answer)),
  { message: `the agent answers item ${item.id} with the digest of ${marker}`, timeout, intervals: [10_000] }).toBe(true);
  const told = a2aConversations(work.agentId)
    .filter((conversation) => sessionInputRows(conversation.session_id).some((row) => row.content.includes(marker)));
  expect(told.map((conversation) => conversation.user_id), `one conversation of the owner's Agent works ${marker}`)
    .toEqual([work.owner]);
  return item.id;
}

/** A text whose SHA-256 hex digest begins with a digit, or with a letter. */
function digestStarting(base: string, withDigit: boolean): { text: string; digest: string } {
  for (let index = 0; ; index += 1) {
    const text = `${base}-${index}`;
    const digest = createHash('sha256').update(text).digest('hex');
    if (/^[0-9]/.test(digest) === withDigit) return { text, digest };
  }
}

/** The outcomes the Agent reported with report_outcome in the conversations A2A started, oldest first. */
function outcomeReports(agentId: string): Array<{ state: string; message: string }> {
  return postgresRows<{ data: { state: string; message: string } }>(
    'SELECT c.data FROM tool_cards c JOIN sessions s ON s.session_id = c.session_id '
      + `WHERE s.agent_id = ${sqlLiteral(agentId)} AND s.source_type = 'a2a' AND c.kind = 'a2a_task_outcome' ORDER BY c.created_at`,
  ).map((row) => row.data);
}

/** Wait for the agent's comment on an item that names `text`. */
async function answered(item: string, text: string, timeout: number): Promise<void> {
  await expect.poll(async () => (await service<Comment[]>('GET', `/admin/items/${item}/comments`))
    .some((comment) => comment.author === 'agent' && comment.text.toLowerCase().includes(text)),
  { message: `the agent's comment on item ${item} names ${text}`, timeout, intervals: [10_000] }).toBe(true);
}

/** The Ticket that follows the item, open or ended (the panel lists them apart). */
async function ticketFollowing(page: Page, assistantId: string, item: string): Promise<Ticket | undefined> {
  const frame = await openOnPanel(page, assistantId);
  let found: Ticket | undefined;
  await expect.poll(async () => {
    const tickets = [
      ...(await panelApi<{ items: Ticket[] }>(frame, 'GET', 'tickets')).items,
      ...(await panelApi<{ items: Ticket[] }>(frame, 'GET', 'tickets?state=ended')).items,
    ];
    found = tickets.find((ticket) => ticket.threads.some((thread) => thread.url === `todo://items/${item}`));
    return Boolean(found);
  }, { message: `the panel shows the Ticket following ${item}`, timeout: 120_000, intervals: [5_000] }).toBe(true);
  return found;
}

/** The owner points the to-do Source at the service and names who may ask; GitHub stays unset. */
async function configureSource(page: Page, assistantId: string, waitForInstall: number): Promise<void> {
  type Settings = {
    blocks: Record<string, { value: Record<string, unknown>; revision: number }>;
    sources: Array<{ id: string; value: { enabled: boolean; config: Record<string, unknown> }; revision: number }>;
  };
  let settings: Settings | undefined;
  let frame = await openOnPanel(page, assistantId);
  // An installed Bundle reaches the harness when it restarts, which reloads the panel.
  await expect.poll(async () => {
    try {
      settings = await panelApi<Settings>(frame, 'GET', 'settings');
    } catch {
      frame = await openOnPanel(page, assistantId);
      return false;
    }
    return settings.sources.some((entry) => entry.id === 'todo');
  }, { message: 'the panel offers the to-do Source', timeout: waitForInstall, intervals: [15_000] }).toBe(true);
  const source = settings!.sources.find((entry) => entry.id === 'todo')!;
  await panelApi(frame, 'PUT', 'settings/source:todo', {
    value: { enabled: true, config: { baseUrl: serviceUrl, pollSeconds: 5 } }, revision: source.revision,
  });
  const policy = settings!.blocks.policy;
  await panelApi(frame, 'PUT', 'settings/policy', {
    value: { ...policy.value, sources: { ...(policy.value.sources as object), todo: { allowed: [{ id: REQUESTER }], admission: {} } } },
    revision: policy.revision,
  });
  const state = await panelApi<{ setup: { problem?: unknown; accounts?: { agent?: string | null } | null } }>(frame, 'GET', 'state');
  expect([state.setup.problem ?? null, state.setup.accounts?.agent ?? null], 'setup completes with no GitHub account').toEqual([null, null]);
}

/** An administrator assigns the running Assistant a Vault holding the service's token. */
async function assignVault(api: AstraApi, platform: PlatformApi, assistantId: string, name: string, token: string) {
  const vault = await platform.createVault(name);
  vaultIds.push(vault.vault_id);
  const credential = await platform.createCredential(vault.vault_id, {
    type: 'environment_variable', secret_name: 'TODO_TOKEN', secret_value: token,
    networking: { type: 'limited', allowed_hosts: [`${new URL(serviceUrl).hostname}:80`] },
    injection_location: { header: true, body: false }, allow_insecure_http: true,
  });
  await api.data('PUT', `/admin/assistants/${assistantId}/credential-vaults`, { vault_ids: [vault.vault_id] });
  return { vaultId: vault.vault_id, credentialId: credential.credential_id };
}

/**
 * The owner's Agent that runs commands, then the Assistant: the Agents an Assistant can hand work to are the ones its
 * owner can use when its runtime starts.
 */
async function createAssistant(api: AstraApi, runtimeProfile: string, marker: string): Promise<{ assistantId: string; work: Work }> {
  const agentName = `__e2e_worker_${marker}`;
  const agent = await api.createAgent({
    name: agentName,
    description: 'Runs shell commands and reports what they print.',
    model: String((await api.defaultAgent()).model),
    runtime_profile_name: workerProfile,
    prepared_capacity: 0,
  });
  const agentId = String(agent.agent_id || '');
  expect(agentId).not.toEqual('');
  agentIds.push(agentId);
  const assistant = await api.createAssistant({
    display_name: `__e2e_${marker}`,
    runtime_profile_name: runtimeProfile,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfile) },
  });
  const assistantId = String(assistant.assistant_id || '');
  expect(assistantId).not.toEqual('');
  assistantIds.push(assistantId);
  return { assistantId, work: { agentId, agentName, owner: String(assistant.owner_id || '') } };
}

const assistantIds: string[] = [];
const agentIds: string[] = [];
const vaultIds: string[] = [];
let child: ChildProcess | undefined;
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of assistantIds.splice(0)) await api.deleteAssistant(id);
  for (const id of agentIds.splice(0)) await api.deleteAgent(id);
  for (const id of vaultIds.splice(0)) await new PlatformApi(request).deleteVault(id);
});
test.afterEach(() => { child?.kill(); child = undefined; });

test('a dsh-superagent Assistant works tickets from its own Source with a Vault credential and no GitHub', async ({ page, request }) => {
  test.setTimeout(75 * 60_000);
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const marker = `todo-e2e-${Date.now().toString(36)}`;
  const first = randomUUID();
  child = await startService(first);

  const { assistantId, work } = await createAssistant(api, profile, marker);
  await configureSource(page, assistantId, 60_000);
  const { vaultId, credentialId } = await assignVault(api, platform, assistantId, `__e2e_${marker}`, first);

  const item = await fileAndAwaitAnswer(work, `${marker}-a`, 20 * 60_000);
  const ticket = await ticketFollowing(page, assistantId, item);
  expect(ticket, 'the panel shows the Ticket following the item').toBeDefined();

  // Nobody is looking while the box is replaced.
  const context = page.context();
  await page.close();
  await loseTheBoxAndRecover(api, assistantId);
  const reopened = await context.newPage();
  expect((await ticketFollowing(reopened, assistantId, item))?.id, 'the new box keeps the Ticket').toEqual(ticket!.id);
  await fileAndAwaitAnswer(work, `${marker}-b`, 15 * 60_000);

  // A rotated value reaches the running box.
  const second = randomUUID();
  await platform.updateCredential(vaultId, credentialId, { auth: { secret_value: second } });
  await service('POST', '/admin/token', { token: second });
  await fileAndAwaitAnswer(work, `${marker}-c`, 15 * 60_000);

  // An archived credential stops being delivered: the service sees the placeholder.
  const before = (await service<{ refused: number }>('GET', '/admin/refused')).refused;
  await platform.archiveCredential(vaultId, credentialId);
  await expect.poll(async () => (await service<{ refused: number }>('GET', '/admin/refused')).refused - before,
    { message: 'the service refuses the agent once the credential is archived', timeout: 15 * 60_000, intervals: [10_000] })
    .toBeGreaterThanOrEqual(3);
  const unanswered = await service<{ id: string }>('POST', '/admin/items', {
    title: `Digest ${marker}-d`, author: REQUESTER, text: digestRequest(`${marker}-d`, work.agentName).text,
  });
  await reopened.waitForTimeout(90_000);
  expect((await service<Comment[]>('GET', `/admin/items/${unanswered.id}/comments`)).filter((comment) => comment.author === 'agent'),
    'nothing reaches the service without the credential').toEqual([]);
});

test('an owner adds the Source to a dsh-superagent Assistant as a plugin Bundle', async ({ page, request }, testInfo) => {
  test.setTimeout(60 * 60_000);
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const marker = `todo-bundle-${Date.now().toString(36)}`;
  // The Bundle as a registry would serve it: an npm tarball of the package directory.
  const tarball = testInfo.outputPath('astrabox-e2e-todo-source-0.0.0.tgz');
  execFileSync('tar', ['-czf', tarball, '--transform', 's,^,package/,', '-C', path.join(__dirname, '..', 'fixtures', 'superagent-todo-source'),
    'package.json', 'lib', 'cordis.patch.yml']);
  const token = randomUUID();
  child = await startService(token, tarball);

  // The plain image: the Source is the owner's own plugin, installed on the Assistant.
  const { assistantId, work } = await createAssistant(api, plainProfile, marker);
  await new PlatformApi(request).patchAssistant(assistantId, { engine_options: { profile_package: {
    dependencies: { 'astrabox-e2e-todo-source': `${serviceUrl}/packages/${path.basename(tarball)}` },
  } } });
  await assignVault(api, platform, assistantId, `__e2e_${marker}`, token);
  await configureSource(page, assistantId, 20 * 60_000);

  const item = await fileAndAwaitAnswer(work, `${marker}-a`, 20 * 60_000);
  expect(await ticketFollowing(page, assistantId, item), 'the panel shows the Ticket following the item').toBeDefined();

  // A new box installs the owner's plugin again from the Assistant's configuration.
  const context = page.context();
  await page.close();
  await loseTheBoxAndRecover(api, assistantId);
  await context.newPage();
  await fileAndAwaitAnswer(work, `${marker}-b`, 15 * 60_000);
});

test('a person hears why the Agent declined an item, and the Agent\'s report reaches a done item\'s answer', async ({ page, request }) => {
  test.setTimeout(45 * 60_000);
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const marker = `todo-outcome-${Date.now().toString(36)}`;
  const token = randomUUID();
  child = await startService(token);
  const { assistantId, work } = await createAssistant(api, profile, marker);
  await configureSource(page, assistantId, 60_000);
  await assignVault(api, platform, assistantId, `__e2e_${marker}`, token);

  // Whether the work is done turns on a digest only a command finds, so the Agent alone decides it, and the person's
  // reply names a value no acknowledgement can.
  const declined = digestStarting(`${marker}-no`, true);
  const done = digestStarting(`${marker}-yes`, false);
  const ask = (text: string) => `@agent please have the ${work.agentName} agent compute, with a command, the SHA-256 of the exact `
    + `text "${text}" (no trailing newline). If the hex digest begins with a digit, the agent must not do anything more: it declines `
    + 'this request, with the digest as its reason. Otherwise it writes the digest to digest.txt in its workspace and reports the '
    + 'request done, with the digest in its report. Either way, reply on this item with the digest and whether the work was done.';
  const no = await service<{ id: string }>('POST', '/admin/items', { title: `Outcome ${declined.text}`, author: REQUESTER, text: ask(declined.text) });
  const yes = await service<{ id: string }>('POST', '/admin/items', { title: `Outcome ${done.text}`, author: REQUESTER, text: ask(done.text) });

  await answered(no.id, declined.digest, 25 * 60_000);
  const rejected = outcomeReports(work.agentId).find((report) => report.message.toLowerCase().includes(declined.digest));
  expect(rejected?.state, 'the Agent reports the declined task as rejected').toEqual('TASK_STATE_REJECTED');
  const closed = await ticketFollowing(page, assistantId, no.id);
  expect(closed?.ended?.outcome, 'the declined item\'s Ticket ends dropped, not done').toEqual('dropped');
  expect((closed?.summary ?? '').startsWith('The agent declined this request.'), 'its report says the Agent declined it').toBe(true);
  expect((closed?.summary ?? '').toLowerCase(), 'its report carries the Agent\'s reason').toContain(declined.digest);

  await answered(yes.id, done.digest, 20 * 60_000);
  const completed = outcomeReports(work.agentId).find((report) => report.message.toLowerCase().includes(done.digest));
  expect(completed?.state, 'the Agent reports the done task as completed').toEqual('TASK_STATE_COMPLETED');
  const finished = await ticketFollowing(page, assistantId, yes.id);
  expect(finished?.ended?.outcome, 'the done item\'s Ticket completes').toEqual('completed');
  expect((finished?.summary ?? '').startsWith(completed!.message), 'the Agent\'s report leads the Ticket\'s result').toBe(true);
});
