/**
 * A JSON block names a key it defines twice, and says when an edit changes nothing.
 *
 * Parsed into a value, a second definition of a stored key replaces the first,
 * and an edit that only appends a copy reads back equal to what is stored. Read
 * that way, the edit offers no Save and sends nothing, and the reader cannot
 * tell it from one the form lost. The text is what the reader typed, so the
 * refusal is checked against the text and the record the server holds.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { apiPath, appPath } from '../fixtures/env';
import { onPassOnly } from '../fixtures/sessionCleanup';

let agentId = '';
onPassOnly(async ({ request }) => {
  if (agentId) await new AstraApi(request).deleteAgent(agentId);
  agentId = '';
});

/** One `"name": {...}` line of a block; a block's line 1 is its `{`. */
const entry = (name: string, value: unknown) => `  ${JSON.stringify(name)}: ${JSON.stringify(value)}`;
const block = (...entries: string[]) => `{\n${entries.join(',\n')}\n}`;

test('a key defined twice is refused with both lines, and a new name saves', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.createColdTestAgent(`__e2e_json_duplicate_${Date.now()}`);
  agentId = agent.agent_id;
  const alpha = { type: 'stdio', command: 'true', args: ['--alpha'] };
  const shared = { type: 'stdio', command: 'true', args: ['--shared'], env: { SERVICE_CODE: 'one' } };
  const stored = { alpha_agent: alpha, shared_agent: shared };
  await api.updateAgent(agentId, { ...await api.getAgent(agentId), mcp_servers: stored });
  const before = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
  expect(before.mcp_servers).toEqual(stored);

  const writes: string[] = [];
  page.on('request', (sent) => {
    if (sent.method() === 'PUT' && new URL(sent.url()).pathname === apiPath(`/agents/${agentId}`)) {
      writes.push(sent.postData() ?? '');
    }
  });
  await page.goto(appPath(`/manage/agents/${encodeURIComponent(agentId)}`));
  const mcp = page.locator('#agent-mcp_servers');
  const card = page.locator('[data-slot="card"]').filter({ has: mcp });
  const save = card.getByRole('button', { name: 'Save', exact: true });
  const status = page.locator('#agent-mcp_servers-status');
  /** What the field holds, once it holds anything: it is empty until the record loads. */
  const held = async () => {
    const raw = await mcp.inputValue();
    return raw === '' ? undefined : JSON.parse(raw);
  };
  await expect.poll(held).toEqual(stored);

  /** Type `text` as a reader pastes it, and leave the field the way they do. */
  const type = async (text: string) => {
    await mcp.fill(text);
    await mcp.press('Tab');
    await expect(mcp).not.toBeFocused();
    return text;
  };
  /** A text the field did not commit stays exactly as typed, both definitions included. */
  const kept = async (text: string) => {
    await expect(mcp, 'the buffer keeps exactly what was typed').toHaveValue(text);
  };

  await test.step('a copy of a stored entry, appended, is refused rather than merged', async () => {
    await kept(await type(
      block(entry('alpha_agent', alpha), entry('shared_agent', shared), entry('shared_agent', shared)),
    ));
    await expect(status).toHaveText(
      '"shared_agent" is defined twice (lines 3 and 4), and only one would be saved. '
      + 'Rename one or remove the extra.',
    );
    await expect(mcp).toHaveAttribute('aria-invalid', 'true');
    await expect(mcp).toHaveAttribute('aria-describedby', /agent-mcp_servers-status/);
    await expect(save, 'a refused edit is not a change to save').toHaveCount(0);
  });

  await test.step('a duplicate with different values blocks a change already pending', async () => {
    const changedAlpha = { ...alpha, args: ['--alpha', '--verbose'] };
    await type(block(entry('alpha_agent', changedAlpha), entry('shared_agent', shared)));
    await expect(status).toHaveCount(0);
    await expect(save).toBeEnabled();
    await kept(await type(block(
      entry('alpha_agent', changedAlpha),
      entry('shared_agent', shared),
      entry('shared_agent', { ...shared, env: { SERVICE_CODE: 'two' } }),
    )));
    await expect(status).toContainText('"shared_agent" is defined twice (lines 3 and 4)');
    await expect(save, 'the pending change waits until the duplicate is resolved').toBeDisabled();
  });

  await test.step('a key defined twice inside an entry is named by its path', async () => {
    await kept(await type([
      '{',
      `  "alpha_agent": ${JSON.stringify(alpha)},`,
      '  "shared_agent": {',
      '    "type": "stdio", "command": "true", "args": ["--shared"],',
      '    "env": {',
      '      "SERVICE_CODE": "one",',
      '      "SERVICE_CODE": "two"',
      '    }',
      '  }',
      '}',
    ].join('\n')));
    await expect(status).toContainText('"shared_agent.env.SERVICE_CODE" is defined twice (lines 6 and 7)');
  });

  await test.step('reverting the pending change clears the refusal of the text it replaces', async () => {
    await card.getByRole('button', { name: 'Revert', exact: true }).click();
    await expect.poll(held).toEqual(stored);
    await expect(status).toHaveCount(0);
    await expect(mcp).not.toHaveAttribute('aria-invalid', 'true');
    await expect(save).toHaveCount(0);
  });

  await test.step('an edit that only reorders and reformats is said to change nothing', async () => {
    await kept(await type(block(
      entry('shared_agent', { env: shared.env, args: shared.args, command: shared.command, type: shared.type }),
      entry('alpha_agent', alpha),
    )));
    await expect(status).toHaveText(
      'This edit changes nothing: key order and formatting are not part of what is saved.',
    );
    await expect(mcp).not.toHaveAttribute('aria-invalid', 'true');
    await expect(save).toHaveCount(0);
  });

  const unchanged = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
  expect(writes, 'no refused or unchanged edit reached the server').toEqual([]);
  expect(unchanged.version).toEqual(before.version);
  expect(unchanged.mcp_servers).toEqual(stored);

  await test.step('a changed entry and a server under a new name save', async () => {
    const changedAlpha = { ...alpha, args: ['--alpha', '--verbose'] };
    const added = { type: 'stdio', command: 'true', args: ['--shared'], env: { SERVICE_CODE: 'two' } };
    await type(block(
      entry('alpha_agent', changedAlpha),
      entry('shared_agent', shared),
      entry('shared_agent_two', added),
    ));
    await expect(status).toHaveCount(0);
    const saved = page.waitForResponse((response) => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === apiPath(`/agents/${agentId}`));
    await save.click();
    expect((await saved).status()).toBe(200);
    const record = await api.data<Record<string, unknown>>('GET', `/agents/${agentId}`);
    expect(record.mcp_servers).toEqual({ alpha_agent: changedAlpha, shared_agent: shared, shared_agent_two: added });
  });
});
