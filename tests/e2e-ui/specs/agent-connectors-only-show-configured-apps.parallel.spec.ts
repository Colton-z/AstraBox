import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

interface AdminConnector {
  connector_id: string;
  name: string;
  configured: boolean;
  published: boolean;
}

let agentId = '';
let assistantId = '';
let runtimeProfileName = '';

onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  if (agentId) await api.deleteAgent(agentId);
  if (assistantId) await api.deleteAssistant(assistantId);
  if (runtimeProfileName) await api.data('DELETE', `/admin/runtime-profiles/${runtimeProfileName}`);
});

test.use({ locale: 'en-US' });

test('Agent and Assistant settings show only configured apps and hide an empty Connectors module', async ({
  page,
  request,
}, testInfo) => {
  const api = new AstraApi(request);
  const catalog = await api.data<AdminConnector[]>('GET', '/admin/connectors');
  expect(catalog.length, 'the administrator still needs the app catalog to configure connectors').toBeGreaterThan(0);
  const available = catalog.filter((connector) => connector.configured && connector.published);
  await testInfo.attach('administrator-connector-configuration', {
    body: JSON.stringify(catalog),
    contentType: 'application/json',
  });

  const seeded = await api.defaultAgent();
  const agent = await api.createAgent({
    name: `__e2e_connector_visibility_${randomUUID().slice(0, 8)}`,
    model: seeded.model,
    runtime_profile_name: seeded.runtime_profile_name,
    prepared_capacity: 0,
  });
  agentId = agent.agent_id;

  const runtime_profiles = await api.data<Array<{ engine_kind: string; enabled: boolean; supported_session_kinds: string[] }>>(
    'GET', '/admin/runtime-profiles',
  );
  const assistantEnvironment = runtime_profiles.find((environment) =>
    environment.enabled && environment.supported_session_kinds.includes('assistant_chat'),
  );
  expect(assistantEnvironment, 'the deployment needs an enabled Assistant environment').toBeDefined();
  runtimeProfileName = `__e2e_connector_visibility_${randomUUID().slice(0, 8)}`;
  await api.data('PUT', `/admin/runtime-profiles/${runtimeProfileName}`, {
    name: runtimeProfileName, enabled: true, engine_kind: assistantEnvironment!.engine_kind, sandbox_backend: 'open_sandbox',
  });
  const assistant = await api.createAssistant({
    display_name: runtimeProfileName,
    runtime_profile_name: runtimeProfileName,
  });
  assistantId = assistant.assistant_id;

  const visit = async (kind: 'agent' | 'assistant', id: string) => {
    const loaded = Promise.all([
      page.waitForResponse((response) =>
        new URL(response.url()).pathname === '/api/v1/connectors' && response.ok(),
      ),
      page.waitForResponse((response) =>
        new URL(response.url()).pathname === `/api/v1/${kind}s/${id}/connectors` && response.ok(),
      ),
    ]);
    await page.goto(appPath(`/manage/${kind}s/${id}`));
    await loaded;
    await page.evaluate(() => new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    ));
    await expect(page.getByRole('alert')).toHaveCount(0);
    const list = page.getByTestId(`${kind}-connectors`);
    await expect(page.getByRole('heading', { name: 'Connectors', exact: true }))
      .toHaveCount(available.length > 0 ? 1 : 0);
    if (available.length === 0) {
      await expect(list).toHaveCount(0);
      await expect(page.getByText(
        kind === 'agent'
          ? 'Work apps this Agent offers. Each person who starts a conversation uses their own sign-in, connected under Connections.'
          : 'Work apps this Assistant can use, each with your own sign-in at the vendor. An app you connect is turned on here unless you turn it off.',
        { exact: true },
      )).toHaveCount(0);
      await expect(page.getByText(
        kind === 'agent'
          ? "Conversations a channel, schedule or webhook starts use no one's connections. Changing the set prepares the Agent's runtime again."
          : "Your sign-in stays on the server: the Assistant’s sandbox never sees the token. Turning a connector on or off restarts the Assistant’s backend once it is idle.",
        { exact: true },
      )).toHaveCount(0);
    } else {
      await expect(list.locator('[data-connector-id]')).toHaveCount(available.length);
      for (const connector of available) {
        const row = list.locator(`[data-connector-id="${connector.connector_id}"]`);
        await expect(row).toContainText(connector.name);
        if (kind === 'agent') {
          await expect(row.getByRole('switch', { name: `Offer ${connector.name} in this Agent` })).toBeEnabled();
        }
        await expect(row.getByRole('button')).toBeEnabled();
      }
    }
    for (const connector of catalog.filter((row) => !row.configured || !row.published)) {
      await expect(page.locator(`[data-connector-id="${connector.connector_id}"]`)).toHaveCount(0);
    }
    await expect(page.getByText('An administrator must set this app up under Connectors first.', { exact: true }))
      .toHaveCount(0);
  };

  await visit('agent', agentId);
  if (available.length > 0) {
    const toggle = page.getByRole('switch', { name: `Offer ${available[0].name} in this Agent` });
    await toggle.click();
    await expect(toggle).toBeChecked();
    await expect(toggle).toBeEnabled();
    await visit('agent', agentId);
    await expect(toggle).toBeChecked();
    await toggle.click();
    await expect(toggle).not.toBeChecked();
    await expect(toggle).toBeEnabled();
  }
  await visit('agent', agentId);
  await visit('assistant', assistantId);
  await visit('assistant', assistantId);
});
