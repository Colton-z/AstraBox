/** A Hermes Assistant opens its own chat and restores that chat on a new box. */
import { randomUUID } from 'node:crypto';

import { expect, test, type Page } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import { requireSandboxHandle, sandboxExec } from '../fixtures/sandboxOps';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

let assistantId = '';
const platformSessions = trackSessions();
onPassOnly(async ({ request }) => {
  if (assistantId) await new AstraApi(request).deleteAssistant(assistantId);
});

const replies = (page: Page) => page.locator('[data-slot="aui_assistant-message-content"]:visible');

async function openWebapp(console: Page, id: string, name: string): Promise<Page> {
  await console.goto(appPath(`/assistants/${id}`));
  const open = console.getByRole('button', { name: `Open ${name} in a new tab` });
  await expect(open).toBeEnabled({ timeout: 75_000 });
  const [webapp] = await Promise.all([console.context().waitForEvent('page'), open.click()]);
  await expect(webapp.getByRole('button', { name: 'New session', exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  expect(new URL(webapp.url()).protocol).toBe('https:');
  expect(new URL(webapp.url()).origin).not.toBe(new URL(console.url()).origin);
  await expect(console).toHaveURL(new RegExp(`/assistants/${id}$`));
  return webapp;
}

async function turn(page: Page, prompt: string, answer: string): Promise<void> {
  const composer = page.getByRole('textbox', { name: 'Message', exact: true });
  const replyCount = await replies(page).count();
  await composer.fill(prompt);
  await composer.press('Enter');
  await expect(page.locator('[data-role="user"]').getByText(prompt, { exact: true })).toBeVisible();
  await expect(replies(page)).toHaveCount(replyCount + 1, { timeout: 35_000 });
  await expect(replies(page).last()).toContainText(answer, { timeout: 35_000 });
  await expect(page.locator('[data-message-streaming="true"]')).toHaveCount(0);
}

type NativeSession = { id: string; model: string; messages: Array<[string, string]> };

async function readHermesSession(api: AstraApi, box: string, prompt: string): Promise<NativeSession> {
  const sandbox = await requireSandboxHandle(api, box);
  const record = sandboxExec(sandbox, [
    "python3 - <<'PY'",
    'import json, sqlite3',
    'from pathlib import Path',
    "db, = Path('/home/conversations').glob('*/.hermes/state.db')",
    'with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:',
    '    session, = conn.execute("SELECT session_id FROM messages WHERE role = ? AND content = ?",',
    `                            ("user", ${JSON.stringify(prompt)})).fetchall()`,
    '    sid, = session',
    '    model, = conn.execute("SELECT model FROM sessions WHERE id = ?", (sid,)).fetchone()',
    '    rows = conn.execute("SELECT role, content FROM messages WHERE session_id = ? ORDER BY id", (sid,)).fetchall()',
    '    print(json.dumps(dict(id=sid, model=model, messages=rows)))',
    'PY',
  ].join('\n'));
  return JSON.parse(record);
}

test('the Hermes Webapp opens existing history and continues a native chat on a new sandbox', async ({
  page, request, playwright,
}) => {
  const api = new AstraApi(request);
  const profile = await api.assistantEnvironmentName();
  const model = await api.assistantModelName(profile);
  const marker = randomUUID().slice(0, 8);
  const name = `Hermes Webapp ${marker}`;
  const existingAnswer = `EXISTING-${marker}`;
  const existingWord = randomUUID().slice(0, 8);
  const existingPrompt = `${existingAnswer}: remember my verification word ${existingWord} for this conversation. Reply with the identifier at the start of this message, without tools.`;
  const nativeAnswer = `NATIVE-${marker}`;
  const savedWord = randomUUID().slice(0, 8);
  const nativePrompt = `${nativeAnswer}: remember my verification word ${savedWord} for this conversation. Reply with the identifier at the start of this message, without tools.`;
  const followup = 'Reply with only my verification word from earlier in this conversation. Do not use tools.';

  const assistant = await api.createAssistant({
    display_name: name, runtime_profile_name: profile, model_config_override: { model_name: model },
  });
  assistantId = assistant.assistant_id;
  const initialBox = String((await api.waitForWorkspaceReady(assistantId, 85_000)).current_sandbox_id || '');
  expect(initialBox).toBeTruthy();
  const earlier = await api.startAssistantConversation(assistantId);
  platformSessions.push(earlier.session_id);
  await api.waitForSessionReady(earlier.session_id);
  const existingReply = await api.sendTurn(earlier.session_id, existingPrompt, 35_000);
  expect(existingReply.errorText).toBeFalsy();
  expect(existingReply.text).toContain(existingAnswer);

  const panelPath = apiPath(`/assistants/${assistantId}/panel`);
  const visitor = await playwright.request.newContext({
    baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
    storageState: { cookies: [], origins: [] },
  });
  try {
    const issuer = requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER');
    const discovery = await json(await visitor.get(`${issuer}/.well-known/openid-configuration`));
    const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
    const token = await issueToken(visitor, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read');
    const headers = { Authorization: `Bearer ${token.value}` };
    const foreign = await visitor.get(apiPath('/user/current'), { headers });
    expect(foreign.ok()).toBe(true);
    expect((await foreign.json()).data.user_id).not.toBe((await api.data<{ user_id: string }>('GET', '/user/current')).user_id);
    expect((await visitor.get(panelPath, { headers })).status()).toBe(404);
  } finally {
    await visitor.dispose();
  }

  const webapp = await openWebapp(page, assistantId, name);
  const unauthenticated = await webapp.evaluate(async () => (await fetch('/api/sessions', { credentials: 'omit' })).status);
  expect(unauthenticated).toBe(401);
  await webapp.getByRole('button', { name: new RegExp(`^${existingAnswer}`) }).first().click();
  await expect(replies(webapp).last()).toContainText(existingReply.text.trim(), { useInnerText: true });
  await turn(webapp, followup, existingWord);
  const earlierNative = await readHermesSession(api, initialBox, existingPrompt);
  expect(earlierNative.messages.slice(-2)).toEqual([
    ['user', followup], ['assistant', expect.stringContaining(existingWord)],
  ]);
  await webapp.getByRole('button', { name: 'New session', exact: true }).first().click();
  await expect(replies(webapp)).toHaveCount(0);
  await turn(webapp, nativePrompt, nativeAnswer);
  const before = await readHermesSession(api, initialBox, nativePrompt);
  expect(before.model).toBe(model);
  expect(before.id).not.toBe(earlierNative.id);
  const nativeReply = before.messages.at(-1);
  expect(nativeReply).toEqual(['assistant', expect.stringContaining(nativeAnswer)]);
  await webapp.close();

  const hibernated = await api.hibernateWorkspace(assistantId);
  expect(hibernated.released).toBe(true);
  const reopened = await openWebapp(page, assistantId, name);
  const nextBox = String((await api.getAssistant(assistantId)).current_sandbox_id || '');
  expect(nextBox).toBeTruthy();
  expect(nextBox).not.toBe(initialBox);
  expect(await readHermesSession(api, nextBox, nativePrompt)).toEqual(before);
  await reopened.getByRole('button', { name: new RegExp(`^${nativeAnswer}`) }).first().click();
  await expect(reopened.locator('[data-role="user"]').getByText(nativePrompt, { exact: true })).toBeVisible();
  await expect(replies(reopened).last()).toContainText(nativeReply![1], { useInnerText: true });
  await turn(reopened, followup, savedWord);
  const after = await readHermesSession(api, nextBox, nativePrompt);
  expect(after.id).toBe(before.id);
  expect(after.messages).toEqual([
    ...before.messages, ['user', followup], ['assistant', expect.stringContaining(savedWord)],
  ]);
});
