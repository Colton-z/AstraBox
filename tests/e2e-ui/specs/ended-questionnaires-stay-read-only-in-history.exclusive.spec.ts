/** Closed forms keep their native structure, recorded state and answers after refresh. */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { postgresRows, sqlLiteral } from '../fixtures/dbOracle';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();

test('expired, withdrawn and answered native forms remain read-only after reload', async ({ page, request }) => {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const ids = { expired: randomUUID(), withdrawn: randomUUID(), answered: randomUUID() };
  sessions.push(...Object.values(ids));
  const records = JSON.parse(execFileSync('docker', [
    'exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE), 'python', '-',
    JSON.stringify({ user_id: user.user_id, agent_id: agent.agent_id, sessions: ids }),
  ], {
    input: readFileSync(resolve(__dirname, '../fixtures/ended_questionnaires.py'), 'utf8'),
    encoding: 'utf8', timeout: 30_000,
  })) as Array<{ session_id: string; interaction_id: string; status: keyof typeof ids; tool_name: string }>;
  expect(records).toHaveLength(3);
  await page.addInitScript(() => window.localStorage.setItem('astrabox-lang', 'en'));
  let answerPosts = 0;
  page.on('request', (outgoing) => {
    if (outgoing.method() === 'POST' && /\/interactions\/[^/]+\/answer$/.test(new URL(outgoing.url()).pathname)) {
      answerPosts += 1;
    }
  });

  for (const record of records) {
    const sid = record.session_id;
    const rows = postgresRows<{ status: string; answer: unknown }>(
      `SELECT status, answer FROM session_interactions WHERE session_id=${sqlLiteral(sid)} `
      + `AND interaction_id=${sqlLiteral(record.interaction_id)}`,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe(record.status);
    if (record.status === 'answered') {
      expect(rows[0].answer).toEqual({ answers: [
        { question_id: 'single', option_label: 'Red' },
        { question_id: 'multiple', option_labels: ['Fast', 'Quiet'] },
      ] });
    }
    const before = await api.getSession(sid);
    expect(before.state).toBe('TERMINATED');
    expect(before.sandbox_id).toBeFalsy();
    expect(before.pending_interaction).toBeFalsy();
    const history = await api.getHistoryBlocks(sid);
    const interaction = history.messages.flatMap((message) => message.parts)
      .find((part) => part.type === 'data-interaction')?.data as Record<string, unknown>;
    expect(interaction).toMatchObject({ presentation: 'form', status: record.status, tool_name: record.tool_name });
    if (record.status === 'withdrawn') expect(record.tool_name).not.toBe('AskUserQuestion');

    await openSessionView(page, sid);
    const card = page.locator(`[data-interaction-id="${record.interaction_id}"]`);
    const counts = record.status === 'withdrawn' ? { radios: 4, checks: 0 } : { radios: 2, checks: 2 };
    for (let visit = 0; visit < 2; visit += 1) {
      if (visit) await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(card).toHaveAttribute('data-interaction-status', record.status);
      await expect(card).toContainText('This questionnaire is closed and can no longer be submitted.');
      await expect(card).toContainText('Choose one colour');
      await expect(card).toContainText('Choose features');
      await expect(card.getByRole('radio')).toHaveCount(counts.radios);
      await expect(card.getByRole('checkbox')).toHaveCount(counts.checks);
      for (const control of await card.locator('input, textarea').all()) await expect(control).toBeDisabled();
      await expect(card.getByRole('button')).toHaveCount(0);
      await expect(page.getByTestId('pending-interaction-panel')).toHaveCount(0);
      if (record.status === 'answered') {
        await expect(card.getByRole('radio', { name: 'Red', exact: true })).toBeChecked();
        await expect(card.getByRole('radio', { name: 'Blue', exact: true })).not.toBeChecked();
        await expect(card.getByRole('checkbox', { name: 'Fast', exact: true })).toBeChecked();
        await expect(card.getByRole('checkbox', { name: 'Quiet', exact: true })).toBeChecked();
      }
    }
    const refused = await request.post(apiPath(`/sessions/${sid}/interactions/${encodeURIComponent(record.interaction_id)}/answer`), {
      data: { decline: true },
    });
    expect(refused.status()).toBe(409);
    expect((await refused.json()).code).toBe('INTERACTION_NOT_OPEN');
  }
  expect(answerPosts, 'viewing and refreshing read-only forms must submit nothing').toBe(0);
});
