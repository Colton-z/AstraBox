/**
 * E2E: every user has an Assistant without setting one up, and what the
 * deployment's Assistant defaults provide reaches Assistants that already
 * exist.
 *
 * The primary Assistant leads the rail and uses the deployment's only
 * eligible RuntimeProfile. Its conversation uses the inherited model.
 * Deleting the Assistant deletes its conversation, and the next visit creates
 * a new primary Assistant.
 *
 * The other two change the defaults after an Assistant exists and read the
 * Assistant's next conversation: organization instructions, typed into the
 * console's Assistant defaults page, and a pinned public Skill reach it, and
 * so do an MCP server selected from the extension catalog and the Vault that
 * holds its upstream credential. The evidence is
 * what the program answers and the tool calls its stored conversation holds,
 * not the defaults record. Each case restores the defaults it changed.
 *
 * These journeys provision workspaces and change deployment-wide defaults, so
 * they stay in the suite contract's one-worker serial group.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageToolCalls } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

test.use({ locale: 'en-US' });

const SKILL = 'https://github.com/anthropics/skills.git@683bc88e56f3e09ba94f7055977f3d3aa499f202#skills/brand-guidelines';
const LAYER_FIELDS = [
  'runtime_profile_name', 'model_name', 'instructions', 'skills',
  'mcp_servers', 'packages', 'engine_options', 'credential_vault_ids',
] as const;

type Defaults = Record<(typeof LAYER_FIELDS)[number], unknown> & {
  effective_runtime_profile_name?: string | null;
  assistant_runtime_profile_names?: string[];
};
interface Catalog {
  mcp_servers?: Array<{ id?: string; name?: string }>;
  selected_mcp_server_ids?: string[];
  selected_skill_ids?: string[];
}

const sessions = trackSessions();
const assistantIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const assistantId of assistantIds) await api.deleteAssistant(assistantId);
});

function authored(view: Defaults): Record<string, unknown> {
  return Object.fromEntries(LAYER_FIELDS.map((field) => [field, view[field] ?? null]));
}

/** Run ``body`` with the defaults' authored fields patched, then put them back. */
async function withDefaults(
  api: AstraApi,
  patch: Record<string, unknown>,
  body: () => Promise<void>,
): Promise<void> {
  const before = await api.data<Defaults>('GET', '/admin/assistant-defaults');
  await api.data('PUT', '/admin/assistant-defaults', { ...authored(before), ...patch });
  try {
    await body();
  } finally {
    await api.data('PUT', '/admin/assistant-defaults', authored(before));
  }
}

async function existingAssistant(api: AstraApi, label: string): Promise<string> {
  const runtimeProfileName = await api.assistantEnvironmentName();
  const assistantId = String((await api.createAssistant({
    display_name: `__e2e_${label}_${randomUUID().slice(0, 8)}`,
    runtime_profile_name: runtimeProfileName,
    model_config_override: { model_name: await api.assistantModelName(runtimeProfileName) },
  })).assistant_id || '');
  assistantIds.push(assistantId);
  return assistantId;
}

test("the user's primary Assistant leads the rail and uses the deployment defaults", async ({ page, request }) => {
  const api = new AstraApi(request);
  const runtimeProfileName = await api.assistantEnvironmentName();
  const defaults = await api.data<Defaults>('GET', '/admin/assistant-defaults');
  expect(defaults.assistant_runtime_profile_names, 'the deployment has one RuntimeProfile for Assistants')
    .toEqual([runtimeProfileName]);
  expect(defaults.effective_runtime_profile_name, 'with none named, new Assistants use that one')
    .toBe(runtimeProfileName);

  await withDefaults(api, { model_name: await api.assistantModelName(runtimeProfileName) }, async () => {
    const body = { display_name: 'Assistant', timezone: 'UTC' };
    const first = await api.data<{ assistant_id: string; primary?: boolean }>('POST', '/assistants/primary', body);
    const again = await api.data<{ assistant_id: string }>('POST', '/assistants/primary', body);
    expect(first.primary).toBe(true);
    expect(again.assistant_id, 'asking again answers the same Assistant').toBe(first.assistant_id);
    const owned = await api.data<Array<{ assistant_id: string; primary?: boolean }>>('GET', '/assistants');
    expect(owned.filter((assistant) => assistant.primary).map((assistant) => assistant.assistant_id))
      .toEqual([first.assistant_id]);

    await page.goto(appPath('/agents'));
    const rows = page.getByTestId('assistant-row');
    await expect(rows.first(), 'the primary Assistant leads the rail').toHaveAttribute('data-assistant-id', first.assistant_id);
    const sessionId = (await api.startAssistantConversation(first.assistant_id)).session_id;
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const marker = `answer_${randomUUID().slice(0, 6)}`;
    const answer = await api.sendTurn(sessionId, `Reply with exactly ${marker} in upper case and nothing else.`);
    expect(answer.errorText).toBeFalsy();
    expect(answer.text).toContain(marker.toUpperCase());
    await api.deleteAssistant(first.assistant_id);
    await expect(api.data('GET', `/sessions/${sessionId}`), 'deleting the Assistant deletes its conversation')
      .rejects.toThrow(/-> 404/);
    await page.goto(appPath('/agents'));
    await expect(rows.first(), 'the next visit creates a new primary Assistant')
      .not.toHaveAttribute('data-assistant-id', first.assistant_id);
  });
});

test("the defaults' instructions and Skill reach an Assistant that already exists", async ({ page, request }) => {
  const api = new AstraApi(request);
  const assistantId = await existingAssistant(api, 'defaults_instructions');
  const codeword = `ORGWORD_${randomUUID().slice(0, 6).toUpperCase()}`;

  await withDefaults(api, { skills: [SKILL] }, async () => {
    await page.goto(appPath('/manage/assistant-defaults'));
    const instructions = page.getByRole('textbox', { name: 'Organization instructions', exact: true });
    await instructions.fill(`If anyone asks for the organization codeword, answer ${codeword}.`);
    const saved = page.waitForResponse((response) => (
      response.request().method() === 'PUT'
      && new URL(response.url()).pathname === apiPath('/admin/assistant-defaults')
    ));
    await page.getByRole('button', { name: 'Save', exact: true }).first().click();
    const answer = await saved;
    expect(answer.status(), `the page saves the defaults: ${(await answer.text()).slice(0, 300)}`).toBe(200);
    await page.reload();
    await expect(instructions, 'the page shows the stored instructions').toHaveValue(
      `If anyone asks for the organization codeword, answer ${codeword}.`,
    );

    const inherited = (await api.getAssistant(assistantId) as { inherited?: { instructions?: boolean; skills?: string[] } }).inherited;
    expect(inherited?.instructions).toBe(true);
    expect(inherited?.skills).toEqual([SKILL]);

    const sessionId = String((await api.startAssistantConversation(assistantId)).session_id || '');
    sessions.push(sessionId);
    await api.waitForSessionReady(sessionId);
    const asked = await api.sendTurn(sessionId, 'What is the organization codeword? Reply with it only.', 120_000);
    expect(asked.errorText).toBeFalsy();
    expect(asked.text, 'the organization instructions reached the Assistant').toContain(codeword);
    const listed = await api.sendTurn(sessionId, [
      'Without using any tool, list the names of the Skills available to you, one per line,',
      'exactly as your instructions name them, and nothing else.',
    ].join(' '), 120_000);
    expect(listed.errorText).toBeFalsy();
    expect(listed.text, "the defaults' Skill is in the Assistant's Skill index").toContain('brand-guidelines');
  });
});

test("an MCP server the defaults select from the catalog reaches an Assistant that already exists", async ({ request }) => {
  const managedMcp = String(process.env.ASTRABOX_E2E_MANAGED_MCP_NAME || '').trim();
  const managedVault = String(process.env.ASTRABOX_E2E_MANAGED_MCP_VAULT_ID || '').trim();
  expect(managedMcp, 'the lane names the managed MCP fixture').not.toBe('');
  expect(managedVault, 'the lane names the managed MCP Vault').not.toBe('');
  const api = new AstraApi(request);
  const assistantId = await existingAssistant(api, 'defaults_mcp');

  const catalogBefore = await api.data<Catalog>('GET', '/admin/assistant-defaults/extensions');
  const mcp = (catalogBefore.mcp_servers ?? []).find((item) => item.name === managedMcp);
  expect(mcp?.id, `the catalog offers ${managedMcp}`).toBeTruthy();
  await withDefaults(api, { credential_vault_ids: [managedVault] }, async () => {
    await api.data('PUT', '/admin/assistant-defaults/extensions', {
      mcp_server_ids: [String(mcp!.id)],
      skill_ids: catalogBefore.selected_skill_ids ?? [],
    });
    try {
      const sessionId = String((await api.startAssistantConversation(assistantId)).session_id || '');
      sessions.push(sessionId);
      await api.waitForSessionReady(sessionId);
      const before = (await api.getMessages(sessionId, 100)).messages.length;
      await api.postTurnInput(sessionId, [
        `Call the ${managedMcp} search_engine MCP tool exactly once with query "IBM investor relations"`,
        'and engine google, and return its result unchanged. Do not call any other tool.',
      ].join(' '));
      const answered = new Set<string>();
      const deadline = Date.now() + 150_000;
      for (;;) {
        expect(Date.now() < deadline, 'the turn settles once its gates are answered').toBe(true);
        const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 120_000);
        if (!pending) break;
        if (answered.has(pending.interaction_id)) {
          await new Promise((resolve) => setTimeout(resolve, 1_000));
          continue;
        }
        const ids = ((pending.options ?? []) as Array<Record<string, unknown>>).map((option) => String(option.id ?? ''));
        await api.answerPendingInteraction(sessionId, pending.interaction_id, {
          decision: ['accept', 'approve', 'allow'].find((id) => ids.includes(id)) ?? 'approve',
        });
        answered.add(pending.interaction_id);
        expect(answered.size, 'one tool call raises at most a few gates').toBeLessThan(4);
      }
      const calls = (await api.getMessages(sessionId, 100)).messages.slice(before).flatMap(messageToolCalls)
        .filter((call) => JSON.stringify({ toolName: call.toolName, input: call.input }).includes('search_engine'));
      expect(calls.length, 'the Assistant called the inherited search_engine tool').toBeGreaterThan(0);
      const results = calls.map((call) => JSON.stringify(call.output ?? ''));
      expect(
        results.some((text) => /organic/i.test(text) && /https?:/i.test(text)),
        `the gateway answered with organic results through the inherited Vault: ${results.join(' | ').slice(0, 600)}`,
      ).toBe(true);
    } finally {
      await api.data('PUT', '/admin/assistant-defaults/extensions', {
        mcp_server_ids: catalogBefore.selected_mcp_server_ids ?? [],
        skill_ids: catalogBefore.selected_skill_ids ?? [],
      });
    }
  });
});
