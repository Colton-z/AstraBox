/**
 * Opt-in acceptance against real Slack, Feishu, and DingTalk accounts.
 *
 * The fixture contains only the bot credentials under test; it does not retain
 * a second user's unrelated OAuth session. AstraBox also deliberately ignores
 * bot-authored events to prevent a reply loop. Each case therefore creates the
 * real binding and prints one unique marker. A tester sends that marker from
 * the named platform account.
 * Playwright then proves the inbound event became a durable Agent turn and the
 * adapter returned a real outbound message id. No provider credential is ever
 * printed, persisted in test output, or read back through the product API.
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from '@playwright/test';

import { AstraApi, messageText } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import {
  PlatformApi,
  type ChannelFieldRecord,
  type ChannelProviderRecord,
} from '../fixtures/platformApi';
import {
  requireServiceContainer,
  SERVER_CONTAINER_HANDLE,
} from '../fixtures/serviceContainer';
import { onPassOnly } from '../fixtures/sessionCleanup';

type ExternalProvider = 'slack' | 'feishu' | 'dingtalk';
type ExternalChannelFixture = {
  channel_config: Record<string, unknown>;
  credentials: Record<string, unknown>;
};
type DeliveryEvidence = {
  inbound_state: string | null;
  outbox_state: string | null;
  session_id: string | null;
  delivery_aliases: number;
};

const FIXTURE_ENV: Record<ExternalProvider, string> = {
  slack: 'ASTRABOX_E2E_SLACK_CHANNEL_FILE',
  feishu: 'ASTRABOX_E2E_FEISHU_CHANNEL_FILE',
  dingtalk: 'ASTRABOX_E2E_DINGTALK_CHANNEL_FILE',
};
const DELIVERY_TIMEOUT_MS = 155_000;

let cleanupAgentId = '';
let cleanupDeploymentId = '';
onPassOnly(async ({ request }) => {
  if (cleanupAgentId && cleanupDeploymentId) {
    await new PlatformApi(request).deleteDeployment(cleanupAgentId, cleanupDeploymentId);
  }
  cleanupAgentId = '';
  cleanupDeploymentId = '';
});

const CHANNEL_DELIVERY_PROBE = String.raw`
import asyncio
import json
import sys

from astrabox.persistence.repository.backend import get_async_collection
from astrabox.persistence.repository.channel_repository import (
    INBOUND_COLLECTION,
    OUTBOX_COLLECTION,
)

async def main():
    deployment_id, marker = sys.argv[1:]
    inbound_collection = await get_async_collection(INBOUND_COLLECTION)
    inbound_cursor = inbound_collection.find({"deployment_id": deployment_id}).limit(50)
    matching = []
    async for row in inbound_cursor:
        content = str((row.get("payload") or {}).get("content") or "")
        if marker in content:
            matching.append(row)
    if not matching:
        print(json.dumps({
            "inbound_state": None,
            "outbox_state": None,
            "session_id": None,
            "delivery_aliases": 0,
        }, sort_keys=True))
        return
    inbound = matching[-1]
    outbox_collection = await get_async_collection(OUTBOX_COLLECTION)
    outbox_cursor = outbox_collection.find({
        "deployment_id": deployment_id,
        "work_item_id": inbound.get("_id"),
    }).limit(20)
    outboxes = [row async for row in outbox_cursor]
    outbox = outboxes[-1] if outboxes else {}
    print(json.dumps({
        "inbound_state": inbound.get("state"),
        "outbox_state": outbox.get("state"),
        "session_id": inbound.get("session_id"),
        "delivery_aliases": len(outbox.get("delivery_aliases") or []),
    }, sort_keys=True))

asyncio.run(main())
`;

function readFixture(provider: ExternalProvider): ExternalChannelFixture {
  const envName = FIXTURE_ENV[provider];
  const fixturePath = String(process.env[envName] || '').trim();
  if (!path.isAbsolute(fixturePath)) {
    throw new Error(`${envName} must name an absolute mode-0600 JSON file`);
  }
  const stat = fs.lstatSync(fixturePath);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o600) {
    throw new Error(`${envName} must name a regular, non-symlink mode-0600 JSON file`);
  }
  const parsed = JSON.parse(fs.readFileSync(fixturePath, 'utf8')) as Partial<ExternalChannelFixture>;
  if (
    !parsed.channel_config
    || typeof parsed.channel_config !== 'object'
    || Array.isArray(parsed.channel_config)
    || !parsed.credentials
    || typeof parsed.credentials !== 'object'
    || Array.isArray(parsed.credentials)
  ) {
    throw new Error(`${envName} must contain channel_config and credentials objects`);
  }
  return parsed as ExternalChannelFixture;
}

function fieldValue(
  fixture: ExternalChannelFixture,
  field: ChannelFieldRecord,
): unknown {
  const source = field.secret ? fixture.credentials : fixture.channel_config;
  const value = source[field.key];
  if (field.required && (value === undefined || value === null || value === '')) {
    throw new Error(`the external channel fixture is missing required field ${field.key}`);
  }
  return value;
}

async function fillField(page: Page, field: ChannelFieldRecord, value: unknown): Promise<void> {
  if (value === undefined || value === null || value === '') return;
  const control = page.getByLabel(field.label);
  await expect(control).toBeVisible();
  if (field.secret) await expect(control).toHaveAttribute('type', 'password');
  if (field.kind === 'boolean') {
    if (Boolean(value) !== await control.isChecked()) await control.click();
  } else if (field.kind === 'select') {
    await control.selectOption(String(value));
  } else {
    await control.fill(String(value));
  }
}

function probeDelivery(
  serverContainer: string,
  deploymentId: string,
  marker: string,
): DeliveryEvidence {
  const result = spawnSync(
    'docker',
    [
      'exec',
      serverContainer,
      'python',
      '-c',
      CHANNEL_DELIVERY_PROBE,
      deploymentId,
      marker,
    ],
    { encoding: 'utf8', timeout: 20_000, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  if (result.error || result.status !== 0) {
    throw new Error('the fresh server process could not read channel delivery evidence');
  }
  const line = String(result.stdout || '')
    .split('\n')
    .map((candidate) => candidate.trim())
    .filter(Boolean)
    .reverse()
    .find((candidate) => candidate.startsWith('{'));
  if (!line) throw new Error('the channel delivery probe returned no JSON evidence');
  return JSON.parse(line) as DeliveryEvidence;
}

async function waitForDelivery(
  serverContainer: string,
  deploymentId: string,
  marker: string,
): Promise<DeliveryEvidence> {
  const deadline = Date.now() + DELIVERY_TIMEOUT_MS;
  let latest: DeliveryEvidence = {
    inbound_state: null,
    outbox_state: null,
    session_id: null,
    delivery_aliases: 0,
  };
  while (Date.now() < deadline) {
    latest = probeDelivery(serverContainer, deploymentId, marker);
    if (latest.inbound_state === 'DEAD' || latest.outbox_state === 'DEAD') {
      throw new Error('the real channel turn or outbound delivery reached DEAD');
    }
    if (
      latest.inbound_state === 'SETTLED'
      && latest.outbox_state === 'DELIVERED'
      && latest.session_id
      && latest.delivery_aliases > 0
    ) {
      return latest;
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(
    `no delivered ${deploymentId} channel turn arrived before the ${DELIVERY_TIMEOUT_MS}ms deadline`,
  );
}

function assertSecretsAbsent(payload: unknown, fixture: ExternalChannelFixture, where: string): void {
  const serialized = JSON.stringify(payload ?? null);
  for (const candidate of Object.values(fixture.credentials)) {
    if (typeof candidate === 'string' && candidate.length >= 8 && serialized.includes(candidate)) {
      throw new Error(`${where} exposed a write-only external channel credential`);
    }
  }
}

async function acceptRealChannel(
  providerName: ExternalProvider,
  page: Page,
  request: APIRequestContext,
): Promise<void> {
  const fixture = readFixture(providerName);
  const api = new AstraApi(request);
  const platform = new PlatformApi(request);
  const serverContainer = requireServiceContainer(SERVER_CONTAINER_HANDLE);
  const agent = await api.defaultAgent();
  cleanupAgentId = String(agent.agent_id || '').trim();
  expect(cleanupAgentId, 'the seeded Agent must have an id').not.toEqual('');

  const provider = (await platform.listChannelProviders()).find(
    (candidate) => candidate.name === providerName,
  ) as ChannelProviderRecord | undefined;
  expect(provider, `${providerName} must be installed in the channel catalogue`).toBeTruthy();

  await page.goto(appPath('/manage/deployments/new'));
  await page.getByLabel('Agent', { exact: true }).selectOption(cleanupAgentId);
  await page.getByLabel('Trigger', { exact: true }).selectOption(provider!.scene);
  for (const field of [...provider!.config_fields, ...provider!.credential_fields]) {
    await fillField(page, field, fieldValue(fixture, field));
  }
  await expect(page.getByRole('link', { name: /Open official console/ })).toHaveAttribute(
    'href',
    provider!.setup_url || '',
  );
  await page.getByRole('button', { name: 'Create' }).click();
  await page.waitForURL((url) => !url.pathname.endsWith('/new'));
  cleanupDeploymentId = decodeURIComponent(page.url().split('/').pop() || '');
  expect(cleanupDeploymentId, 'create must navigate to the real channel binding').not.toEqual('');

  const created = (await platform.listDeployments(cleanupAgentId)).find(
    (candidate) => candidate.deployment_id === cleanupDeploymentId,
  );
  expect(created?.scene).toBe(provider!.scene);
  expect(created?.credentials_configured).toBe(true);
  assertSecretsAbsent(created, fixture, 'the Deployment API');
  assertSecretsAbsent(await page.locator('body').innerText(), fixture, 'the Deployment detail page');

  // Source reconciliation runs every five seconds. Announce readiness only
  // after one full interval, so a human message is not sent before the socket
  // connector had a chance to attach.
  await page.waitForTimeout(7_000);
  const marker = `ASTRABOX_${providerName.toUpperCase()}_${Date.now()}`;
  console.log(
    `EXTERNAL_CHANNEL_READY provider=${providerName} marker=${marker} `
      + 'send this marker as a normal human message to the configured bot now',
  );
  const evidence = await waitForDelivery(
    serverContainer,
    cleanupDeploymentId,
    marker,
  );
  expect(evidence).toMatchObject({
    inbound_state: 'SETTLED',
    outbox_state: 'DELIVERED',
  });
  expect(evidence.delivery_aliases).toBeGreaterThan(0);

  const messages = await api.getMessages(String(evidence.session_id), 50);
  expect(
    messages.messages.some(
      (message) => message.role === 'user' && messageText(message).includes(marker),
    ),
    'the real platform message must be visible in the durable Session transcript',
  ).toBe(true);
  expect(
    messages.messages.some(
      (message) => message.role === 'assistant' && messageText(message).trim().length > 0,
    ),
    'the delivered platform reply must come from a persisted assistant message',
  ).toBe(true);
}

test.describe.serial('real messaging account acceptance', () => {
  test('real Slack account carries one Agent turn end to end', async ({ page, request }) => {
    await acceptRealChannel('slack', page, request);
  });

  test('real Feishu account carries one Agent turn end to end', async ({ page, request }) => {
    await acceptRealChannel('feishu', page, request);
  });

  test('real DingTalk account carries one Agent turn end to end', async ({ page, request }) => {
    await acceptRealChannel('dingtalk', page, request);
  });
});
