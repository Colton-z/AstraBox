/**
 * E2E: a conversation names the Agent it runs, in the list and in its header.
 *
 * A generated title says what was asked, not which Agent answers, so an owner
 * with several Agents cannot tell their conversations apart by title alone.
 * The journey opens a conversation of an Agent whose name exists nowhere else,
 * and checks what the owner sees: the list row and the header name that Agent,
 * a ready row has the width to show the whole name, the header is the way back
 * to the Agent's page, and once the Agent is deleted the row keeps the whole
 * name while it and the header say the conversation takes no further message.
 *
 * WHY THE NAME'S WIDTH IS MEASURED. A truncated name still has its text, so a
 * text assertion passes on a row that reads "Rele…"; only the element's own
 * width says whether the owner can read it.
 *
 * WHY A FRESH AGENT. The default Agent's name is on every other worker's rows;
 * a name made for this run can only have come from this conversation's Agent.
 * The session API is read too, so the name is shown because the conversation
 * carries it, not because the page resolved it from an Agent list it happened
 * to load.
 */
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { appPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
const sessions = trackSessions();

test('a conversation names the Agent it runs, in the list and in its header', async ({ page, request }) => {
  const api = new AstraApi(request);
  const name = `Triage ${Date.now().toString(36).slice(-6)}`;
  const agent = await api.createColdTestAgent(name);
  const agentId = String(agent.agent_id);
  let deleted = false;
  try {
    const started = await api.startConversation(agentId);
    sessions.push(started.session_id);
    const sessionId = started.session_id;
    expect((await api.getSession(sessionId)).agent_name, 'the conversation carries its Agent').toBe(name);

    await openSessionView(page, sessionId);
    const row = page.locator(`[data-testid="session-row"][data-session-id="${sessionId}"]`);
    const named = page.getByTestId('run-view').getByTestId('session-agent');
    await expect(row.getByTestId('session-row-agent'), 'the list row names the Agent').toHaveText(name, {
      timeout: 30_000,
    });
    await expect(named, 'the header names the Agent').toHaveText(name);
    await api.waitForSessionReady(sessionId);
    const rowAgent = row.getByTestId('session-row-agent');
    await expect(row.getByTestId('status-pill'), 'a ready row is not marked').toHaveCount(0, { timeout: 30_000 });
    expect(
      await rowAgent.evaluate((element) => element.scrollWidth <= element.clientWidth),
      'a ready row shows the whole Agent name',
    ).toBe(true);
    await expect(named).toHaveAttribute('href', appPath(`/agents/${agentId}`));

    await named.click();
    await expect(page).toHaveURL((url) => url.pathname === appPath(`/agents/${agentId}`));
    await expect(page.getByRole('heading', { level: 1, name, exact: true })).toBeVisible({ timeout: 30_000 });

    // ── the Agent is deleted: its conversation still says whose it was ────────
    await api.deleteAgent(agentId);
    deleted = true;
    await openSessionView(page, sessionId);
    await expect(named, 'a deleted Agent keeps its name in the header').toHaveText(name, { timeout: 30_000 });
    await expect(
      page.getByTestId('run-view').getByRole('link', { name, exact: true }),
      'a deleted Agent has no page to link to',
    ).toHaveCount(0);
    await expect(rowAgent).toHaveText(name);
    // It takes no further message, so the row says so, as the header does, and
    // the mark leaves the name its width: whose conversation it was is what the
    // row is still read for.
    await expect(row.getByRole('img', { name: 'Terminated', exact: true }), 'the row reads terminated').toBeVisible({
      timeout: 30_000,
    });
    await expect(row.getByTestId('status-pill')).toHaveCount(0);
    expect(
      await rowAgent.evaluate((element) => element.scrollWidth <= element.clientWidth),
      'an ended row shows the whole Agent name',
    ).toBe(true);
  } finally {
    if (!deleted) await api.deleteAgent(agentId);
  }
});
