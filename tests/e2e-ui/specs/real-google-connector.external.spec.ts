/**
 * Opt-in acceptance of the Gmail connector against a real Google account.
 *
 * The deployment runs the connector hub with the Google overlay. Consent
 * happens twice (fixtures/googleConnectors.ts): once as the hub's
 * administrator, so the hub can list Gmail's tools, and once as the suite's
 * own user, who then owns the connection under test.
 *
 * The expected answer comes from Gmail, not from AstraBox: the case asks the
 * hub's Gmail server directly, with the suite user's hub identity, for the
 * newest inbox message received before a moment a day earlier than the run,
 * and requires the Assistant and the Agent to report that message's id. Only
 * message ids are compared, so no subject or body leaves the mailbox through
 * test output. The moment is fixed when the file loads, so the answer does not
 * change while the cases run; a relative `older_than:1d` moves with the clock,
 * and a message crossing it mid-run changes the answer for later cases.
 *
 * What the cases prove, in order: the administrator's set-up asks Google for
 * offline access and returns to the console's callback; the console the
 * vendor returns to turns Gmail on in the person's existing Assistants except
 * one where they turned it off, and an Assistant created afterwards starts
 * with it; a connected person's
 * Assistant and an Agent that offers Gmail, on each bundled engine, read that
 * person's mail; a
 * person who never connected is told the app is not connected and how to ask
 * them, instead of getting the mail; a channel conversation of the same Agent is offered no Gmail tool;
 * after Disconnect the hub refuses to read the mail for that person; and the
 * Agent, asked by that person for mail, shows them a card to connect Gmail.
 *
 * The Agent is prewarmed, as Agents are by default: its runtime connects MCP
 * servers before anyone talks to it, so a caller's connector works only if the
 * claim reconnects it with that caller's identity.
 *
 * Fixture: ASTRABOX_E2E_GOOGLE_CLIENT_FILE, as fixtures/googleConnectors.ts says.
 */
import { randomUUID } from 'node:crypto';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { AstraApi, messageText, visibleMessages, type MessageRecord } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { channelCallback } from '../fixtures/channelCallback';
import { apiPath, appPath } from '../fixtures/env';
import { deployedApiClient, issueToken, json, requiredEnvironment } from '../fixtures/oidcMachine';
import {
  callHubTool, connectorRow as googleConnectorRow, connectPerson, setUpGoogleConnector, type ConnectorRow,
} from '../fixtures/googleConnectors';
import { PlatformApi } from '../fixtures/platformApi';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';
import { closePageAfterAssertions, openSessionView } from '../fixtures/sessionPage';
import { engineCases, engineProfileFor } from '../fixtures/engineProfile';

test.use({ locale: 'en-US' });

const CONNECTOR = 'gmail';
const SEARCH_TOOL = 'gmail-search-gmail-messages';
// Each engine names a hub tool its own way: Hermes turns the hub's dashes into
// underscores (mcp__gmail__gmail_search_gmail_messages), Claude Code keeps them.
const SEARCH_TOOL_NAME = /gmail[-_]search[-_]gmail[-_]messages/;
// Gmail reads a `before:` given in seconds as that exact instant.
const QUERY = `in:inbox before:${Math.floor(Date.now() / 1000) - 24 * 60 * 60}`;
const PROMPT =
  `Call the Gmail tool ${SEARCH_TOOL} with query "${QUERY}" and page_size 1. `
  + 'Reply with only the Message ID from its result, or with the tool\'s error message if it fails.';

const state = {
  userId: '',
  messageId: '',
  agentId: '',
  assistantId: '',
  deploymentId: '',
  preparedSandboxId: '',
};

/**
 * Delete what the cases made, once the last one passes. The cases share these
 * records in order, so a per-case cleanup would remove what the next one uses,
 * and a failure keeps them for diagnosis.
 */
async function cleanUp(request: APIRequestContext): Promise<void> {
  const api = new AstraApi(request);
  if (state.deploymentId) await new PlatformApi(request).deleteDeployment(state.agentId, state.deploymentId);
  if (state.agentId) await api.deleteAgent(state.agentId);
  if (state.assistantId) await api.deleteAssistant(state.assistantId);
}

/** The hub's Gmail answer for one AstraBox user, read without AstraBox's Agents. */
function searchAsUser(userId: string) {
  return callHubTool(userId, CONNECTOR, SEARCH_TOOL, { query: QUERY, page_size: 1 });
}

function messageIdIn(text: string): string {
  return /Message ID:\s*([0-9a-f]{8,})/.exec(text)?.[1] ?? '';
}

function connectorRow(api: AstraApi, route: string): Promise<ConnectorRow> {
  return googleConnectorRow(api, route, CONNECTOR);
}

/** The Agent's prepared box, once its pool holds a runtime of the current generation. */
async function preparedAgentBox(platform: PlatformApi, agentId: string): Promise<string> {
  let last: Record<string, unknown> = {};
  await expect.poll(async () => {
    last = await platform.preparedRuntime(agentId);
    return last.ready === true && Number(last.prepared_count || 0) > 0 && Boolean(last.sandbox_id);
  }, { timeout: 150_000, intervals: [2_000], message: `the Agent prepared no runtime: ${JSON.stringify(last)}` })
    .toBe(true);
  return String(last.sandbox_id);
}

/** The Gmail search calls and their results in one Session's history. */
async function gmailSearches(api: AstraApi, sessionId: string) {
  const history = visibleMessages(await api.getMessages(sessionId, 100));
  const blocks = history.flatMap((row: MessageRecord) => row.blocks || []);
  // A call names the tool where its engine puts the name: the block's name
  // for most, the `tool` of the Codex `mcpToolCall` item the block carries.
  const calls = blocks.filter((block) => block.type === 'tool_use' && SEARCH_TOOL_NAME.test(JSON.stringify(block)));
  const results = blocks.filter(
    (block) => block.type === 'tool_result' && calls.some((call) => call.id === block.tool_use_id),
  );
  const reply = history.filter((row) => row.role === 'assistant').map(messageText).join('\n');
  return { calls, results, reply };
}

async function askInSession(api: AstraApi, sessionId: string) {
  await api.waitForSessionReady(sessionId, 120_000);
  const turn = await api.sendTurn(sessionId, PROMPT, 120_000);
  expect(turn.errorText, 'the turn must finish without a stream error').toBeNull();
  return gmailSearches(api, sessionId);
}

test.describe.serial('Gmail connector with a real Google account', () => {
  test('an administrator sets Gmail up and the hub asks Google for offline access', async ({ request }) => {
    await setUpGoogleConnector(new AstraApi(request), request, CONNECTOR);
  });

  test('a person connects Gmail, the hub reads their mail, and their Assistants offer it', async ({
    request,
    page,
  }) => {
    const api = new AstraApi(request);
    state.userId = String((await api.data<{ user_id: string }>('GET', '/user/current')).user_id);
    // Two Assistants made while the person has no connection; they turn Gmail
    // off on one.
    if ((await googleConnectorRow(api, '/connectors', CONNECTOR)).status !== 'disconnected') {
      await api.data('DELETE', `/connectors/${CONNECTOR}/connection`);
    }
    const environment = await api.assistantEnvironmentName();
    const made = [];
    for (const name of ['before', 'opted-out']) {
      made.push(String((await api.createAssistant({
        display_name: `__e2e_gmail_${name}_${randomUUID().slice(0, 8)}`,
        environment_name: environment,
      })).assistant_id));
    }
    const [before, optedOut] = made;
    await api.data('DELETE', `/assistants/${optedOut}/connectors/${CONNECTOR}`);
    await connectPerson(api, CONNECTOR);

    const answer = searchAsUser(state.userId);
    expect(answer.is_error, answer.text.slice(0, 300)).toBe(false);
    state.messageId = messageIdIn(answer.text);
    expect(state.messageId, `the inbox must hold a message matching ${QUERY}`).not.toBe('');

    // The vendor returns the browser to Connections, which turns Gmail on in
    // the person's Assistants.
    await page.goto(appPath(`/connections?connected=${CONNECTOR}`));
    await expect(page.getByTestId('connection-adopted')).toBeVisible();
    const enabled = async (id: string) =>
      (await api.data<{ enabled: string[] }>('GET', `/assistants/${id}/connectors`)).enabled;
    expect(await enabled(before), 'an Assistant made before the connection now offers it').toEqual([CONNECTOR]);
    expect(await enabled(optedOut), 'an Assistant where it was turned off keeps it off').toEqual([]);
    for (const id of made) await api.deleteAssistant(id);
  });

  test("the person's Assistant reads their Gmail", async ({ request }) => {
    const api = new AstraApi(request);
    expect(state.messageId, 'the person must be connected').not.toBe('');
    // A connector is relayed by the platform, so the Environment needs no
    // MCP network access for it.
    const environment = await api.assistantEnvironmentName();
    const assistant = await api.createAssistant({
      display_name: `__e2e_gmail_${randomUUID().slice(0, 8)}`,
      environment_name: environment,
      model_config_override: { model_name: await api.assistantModelName(environment) },
    });
    state.assistantId = assistant.assistant_id;
    expect((await api.data<{ enabled: string[] }>(
      'GET', `/assistants/${state.assistantId}/connectors`,
    )).enabled, 'an Assistant starts with the apps its owner connected').toEqual([CONNECTOR]);
    await api.waitForWorkspaceReady(state.assistantId, 150_000);
  });

  test('the Assistant answers with the message Gmail holds', async ({ request }) => {
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    const { calls, results, reply } = await askInSession(api, sessionId);
    expect(calls, 'the Assistant must call the Gmail search tool').not.toHaveLength(0);
    expect(results.some((result) => result.is_error !== true && JSON.stringify(result.content).includes(state.messageId)),
      'the Gmail result must carry the message the hub reads for this person').toBe(true);
    expect(reply).toContain(state.messageId);
  });

  test('an Agent offering Gmail prepares a runtime before anyone talks to it', async ({ request }) => {
    const api = new AstraApi(request);
    const prewarmEnvironment = requiredEnvironment('ASTRABOX_E2E_CREDENTIAL_PREWARM_ENVIRONMENT');
    const base = await api.defaultAgent();
    const agent = await api.createAgent({
      name: `__e2e_gmail_agent_${randomUUID().slice(0, 8)}`,
      model: base.model,
      environment_name: prewarmEnvironment,
      prepared_capacity: 1,
    });
    state.agentId = agent.agent_id;
    // The author offers the app without connecting it for the Agent.
    expect((await api.data<{ enabled: string[] }>(
      'PUT', `/agents/${state.agentId}/connectors/${CONNECTOR}`,
    )).enabled).toEqual([CONNECTOR]);
    // A prepared runtime connects its MCP servers before any Session exists;
    // the person's connector is reconnected when their conversation claims it.
    state.preparedSandboxId = await preparedAgentBox(new PlatformApi(request), state.agentId);
  });

  test('an Agent offering Gmail reads the mail of the person who talks to it', async ({ request, page }) => {
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startConversation(state.agentId);
    const ready = await api.waitForSessionReady(sessionId, 120_000);
    expect(ready.sandbox_id, 'the conversation must claim the prepared runtime').toBe(state.preparedSandboxId);
    expect(String((await api.adminSessionDetail(sessionId)).runtime_identity?.credential_slot_id ?? ''))
      .not.toBe('');
    const { calls, results, reply } = await askInSession(api, sessionId);
    expect(calls, 'the Agent must call the Gmail search tool').not.toHaveLength(0);
    expect(results.some((result) => result.is_error !== true && JSON.stringify(result.content).includes(state.messageId)),
      "the Gmail result must carry the caller's message").toBe(true);
    expect(reply).toContain(state.messageId);

    await openSessionView(page, sessionId);
    const transcript = page.getByTestId('run-view').getByTestId('assistant-message');
    // A settled turn folds its tool calls behind one header once its history
    // loads; wait for that header rather than reveal a transcript not yet there.
    await expect(transcript.getByTestId('assistant-turn-process')).toHaveCount(1, { timeout: 30_000 });
    await revealAssistantProcess(page, { within: transcript });
    const card = transcript.locator('[data-tool-call-id]').filter({ hasText: /gmail.*search/i }).first();
    await expect(card, 'the person sees the Gmail call as a tool card').toBeVisible();
    await card.locator('[data-slot="collapsible-trigger"]').first().click();
    await expect(card.locator('[data-slot="collapsible-content"]')).toContainText(state.messageId);
    await closePageAfterAssertions(page);
  });

  // Connectors are platform MCP servers, which every engine renders from its
  // own MCP route; the Claude Agent above covers Claude Code.
  for (const engine of engineCases().filter((entry) => entry.engine_kind !== 'claude_code')) {
    test(`an Agent on ${engine.engine_kind} offering Gmail reads the mail of the person who talks to it`, async ({
      request,
    }) => {
      const api = new AstraApi(request);
      expect(state.messageId, 'the person must be connected').not.toBe('');
      const profile = engineProfileFor(engine.engine_kind);
      const agent = await api.createAgent({
        name: `__e2e_gmail_${engine.engine_kind}_${randomUUID().slice(0, 8)}`,
        model: profile.model,
        environment_name: profile.environment_name,
        prepared_capacity: 1,
      });
      expect((await api.data<{ enabled: string[] }>(
        'PUT', `/agents/${agent.agent_id}/connectors/${CONNECTOR}`,
      )).enabled).toEqual([CONNECTOR]);
      // Prepared before anyone talks to it, as Agents are by default.
      const preparedBox = await preparedAgentBox(new PlatformApi(request), agent.agent_id);
      const { session_id: sessionId } = await api.startConversation(agent.agent_id);
      const ready = await api.waitForSessionReady(sessionId, 120_000);
      expect(ready.sandbox_id, 'the conversation must claim the prepared runtime').toBe(preparedBox);
      const { calls, reply } = await askInSession(api, sessionId);
      expect(calls, `the ${engine.engine_kind} Agent must call the Gmail search tool`).not.toHaveLength(0);
      expect(reply).toContain(state.messageId);
      await api.deleteAgent(agent.agent_id);
    });
  }

  test('a person who never connected Gmail is told it is not connected, not shown the mail', async ({
    request, playwright,
  }) => {
    const api = new AstraApi(request);
    const issuer = requiredEnvironment('ASTRABOX_E2E_OIDC_ISSUER');
    const unauthenticated = await playwright.request.newContext({ storageState: { cookies: [], origins: [] } });
    let stranger: APIRequestContext | undefined;
    try {
      const { clientId, clientSecret } = deployedApiClient(requireServiceContainer(SERVER_CONTAINER_HANDLE));
      const discovery = await json(await unauthenticated.get(`${issuer}/.well-known/openid-configuration`));
      const token = await issueToken(
        unauthenticated, String(discovery.token_endpoint), clientId, clientSecret, 'astrabox:read astrabox:write',
      );
      stranger = await playwright.request.newContext({
        baseURL: requiredEnvironment('ASTRABOX_E2E_CONSOLE_URL'),
        storageState: { cookies: [], origins: [] },
        extraHTTPHeaders: { Authorization: `Bearer ${token.value}` },
      });
      const strangerApi = new AstraApi(stranger);
      const strangerId = String((await strangerApi.data<{ user_id: string }>('GET', '/user/current')).user_id);
      expect(strangerId).not.toBe(state.userId);
      await api.setAgentAccess(state.agentId, { visibility: 'allowlist', admins: [], allowed_user_ids: [strangerId] });

      const { session_id: sessionId } = await strangerApi.startConversation(state.agentId);
      const { calls, results, reply } = await askInSession(strangerApi, sessionId);
      expect(calls, 'the Gmail tool is offered to every caller').not.toHaveLength(0);
      expect(results.length).toBeGreaterThan(0);
      for (const result of results) {
        expect(result.is_error, 'a caller without a connection gets an error result').toBe(true);
        // What the agent reads names the platform's way to ask, not the
        // hub's authorize path, which neither it nor the person can open.
        const said = JSON.stringify(result.content);
        expect(said).toContain('not connected');
        expect(said).toContain('request_connection');
        expect(said).not.toContain('/oauth/authorize');
      }
      expect(JSON.stringify(results) + reply).not.toContain(state.messageId);
    } finally {
      await stranger?.dispose();
      await unauthenticated.dispose();
    }
  });

  test('a channel conversation with the same Agent is offered no Gmail tool', async ({ request }) => {
    const api = new AstraApi(request);
    const platform = new PlatformApi(request);
    const recipient = await channelCallback();
    try {
      const deployment = await platform.createDeployment(state.agentId, { scene: 'channel:generic_json' });
      state.deploymentId = deployment.deployment_id;
      // The channel runs as the Agent's author, the same person who is
      // connected, so only the channel origin can withhold the connection.
      const response = await request.post(apiPath(`/deployments/${state.deploymentId}/trigger`), {
        headers: { 'x-channel-secret': String(deployment.secret) },
        data: {
          text: PROMPT, message_id: randomUUID(), conversation_id: randomUUID(),
          reply: { callback_url: recipient.url },
        },
      });
      expect(response.ok(), await response.text()).toBe(true);
      const accepted = (await response.json()).data as { status: string; session_id: string };
      expect(accepted.status).toBe('accepted');
      await expect.poll(() => {
        expect(recipient.errors).toEqual([]);
        return recipient.deliveries.length;
      }, { timeout: 120_000 }).toBe(1);
      const { calls, results } = await gmailSearches(api, accepted.session_id);
      expect(calls, 'a channel conversation must not see the connector').toHaveLength(0);
      expect(JSON.stringify(results) + recipient.deliveries[0].text).not.toContain(state.messageId);
    } finally {
      await recipient.close();
    }
  });

  test('after Disconnect the hub no longer reads the mail for that person', async ({ request }) => {
    const api = new AstraApi(request);
    await api.data('DELETE', `/connectors/${CONNECTOR}/connection`);
    expect((await connectorRow(api, '/connectors')).status).toBe('disconnected');
    const answer = searchAsUser(state.userId);
    expect(answer.is_error).toBe(true);
    expect(answer.text).toMatch(/authorize/i);
    expect(answer.text).not.toContain(state.messageId);
  });

  test('the Agent asks the person to connect Gmail with a card they can open', async ({ request, page }) => {
    const api = new AstraApi(request);
    expect((await connectorRow(api, '/connectors')).status).toBe('disconnected');
    const { session_id: sessionId } = await api.startConversation(state.agentId);
    await api.waitForSessionReady(sessionId, 120_000);
    // Whether to ask is the agent's call; it has list_connections to know
    // and request_connection to ask.
    const turn = await api.sendTurn(
      sessionId,
      'Look at the newest email in my Gmail inbox. If Gmail is not connected for me, ask me to connect it.',
      150_000,
    );
    expect(turn.errorText, 'the turn must finish without a stream error').toBeNull();
    const parts = visibleMessages(await api.getMessages(sessionId, 100))
      .flatMap((row: MessageRecord) => row.blocks || [])
      .filter((block) => block.type === 'ui_data')
      .map((block) => block.part as { type?: string; data?: Record<string, unknown> })
      .filter((part) => part.type === 'data-tool-card');
    expect(parts.map((part) => part.data), 'the card is kept in the conversation').toEqual([
      expect.objectContaining({ kind: 'connection_request', connectorId: CONNECTOR, name: 'Gmail' }),
    ]);

    await openSessionView(page, sessionId);
    const card = page.getByTestId('connection-request-card');
    await expect(card, 'the person sees the card after the turn, not only while it streams').toBeVisible({
      timeout: 30_000,
    });
    const [signIn] = await Promise.all([
      page.waitForEvent('popup'),
      card.getByRole('button', { name: 'Connect', exact: true }).click(),
    ]);
    await expect.poll(() => signIn.url(), { timeout: 30_000 }).toMatch(/^https:\/\/accounts\.google\.com\//);
    expect(signIn.url()).toContain('access_type=offline');
    await signIn.close();
    await closePageAfterAssertions(page);
    await cleanUp(request);
  });
});
