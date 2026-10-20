/** Controlled stream boundaries must preserve the reader's choices and layout. */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { AstraApi } from '../fixtures/astraApi';
import { apiPath } from '../fixtures/env';
import { trackSessions } from '../fixtures/sessionCleanup';
import { openSessionView } from '../fixtures/sessionPage';
import { requireServiceContainer, SERVER_CONTAINER_HANDLE } from '../fixtures/serviceContainer';

const sessions = trackSessions();
type Chunk = Record<string, unknown>;
type StreamWindow = Window & { processStream?: { send: (chunks: Chunk[]) => void } };

async function openProcess(page: Page, request: APIRequestContext) {
  const api = new AstraApi(request);
  const agent = await api.defaultAgent();
  const user = await api.data<{ user_id: string }>('GET', '/user/current');
  const sid = randomUUID();
  sessions.push(sid);
  execFileSync('docker', ['exec', '-i', requireServiceContainer(SERVER_CONTAINER_HANDLE),
    'python', '-', JSON.stringify({ session_id: sid, user_id: user.user_id, agent_id: agent.agent_id })], {
    input: `import asyncio, json, sys
from astrabox.deploy.onebox import ensure_database_wiring
ensure_database_wiring()
from astrabox.persistence.provider import get_persistence
from astrabox.persistence.repository.session_repository import SessionRepository
async def main():
    scene = json.loads(sys.argv[1])
    try:
        await SessionRepository().create_session({**scene, 'state': 'TERMINATED',
            'session_kind': 'agent_chat', 'engine_kind': 'claude_code', 'title': 'Stream presentation fixture',
            'workspace_ref': {'kind': 'agent', 'agent_id': scene['agent_id']}})
    finally:
        await get_persistence().dispose()
asyncio.run(main())
`, encoding: 'utf8', timeout: 15_000,
  });
  // Only this document's stream is synthetic; navigation, assets and history use the deployment.
  await page.addInitScript(({ streamPath }) => {
    localStorage.setItem('astrabox-lang', 'en');
    const fetchOriginal = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input), location.href);
      if (url.pathname !== streamPath) return fetchOriginal(input, init);
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({ start(controller) {
        (window as StreamWindow).processStream = { send(chunks) {
          for (const chunk of chunks) controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
        } };
      } });
      return new Response(body, { headers: {
        'content-type': 'text/event-stream', 'x-vercel-ai-ui-message-stream': 'v1',
      } });
    };
  }, { streamPath: apiPath(`/sessions/${sid}/stream`) });
  // The synthetic response has no model-owned summary to generate.
  await page.route((url) => url.pathname.includes(`/sessions/${sid}/messages/`)
    && url.pathname.endsWith('/process-summary'), (route) => route.fulfill({
    contentType: 'application/json', body: JSON.stringify({ code: 0, data: { status: 'completed', summary: 'Inspection' } }),
  }));
  await openSessionView(page, sid);
  await page.waitForFunction(() => Boolean((window as StreamWindow).processStream));
  const send = (chunks: Chunk[]) => page.evaluate((parts) => (window as StreamWindow).processStream!.send(parts), chunks);
  await send([{ type: 'start', messageId: `live-${sid}` }, tool('read', 'Read'), output('read')]);
  const group = page.getByTestId('assistant-process').last();
  const trigger = group.getByTestId('assistant-process-trigger');
  const activity = group.getByTestId('assistant-process-activity');
  await expect(activity).toBeVisible();
  return { send, group, trigger, activity };
}

function tool(id: string, name = 'Bash'): Chunk {
  return { type: 'tool-input-available', toolCallId: id, toolName: name, dynamic: true,
    input: { command: 'echo process', file_path: '/workspace/example.txt' } };
}
function output(id: string): Chunk { return { type: 'tool-output-available', toolCallId: id, output: 'done' }; }
function thought(id: string): Chunk[] {
  return [{ type: 'reasoning-start', id }, { type: 'reasoning-delta', id, delta: `Visible thought ${id}` }];
}

test('continuous reasoning keeps earlier details open and respects manual choices', async ({ page, request }) => {
  const { send, group, trigger, activity } = await openProcess(page, request);
  await send(thought('0'));
  await trigger.click();
  const cards = group.getByTestId('reasoning-part');
  await expect(group.getByText('Visible thought 0', { exact: true })).toBeVisible();
  for (let index = 1; index <= 8; index += 1) {
    await send([{ type: 'reasoning-end', id: String(index - 1) }, ...thought(String(index))]);
    await expect(cards).toHaveCount(index + 1);
    await expect(activity).toHaveText('Thinking');
  }
  // Pass the former one-second close deadline, including a gap before any next tool.
  await page.waitForTimeout(1_200);
  for (let index = 0; index <= 8; index += 1) {
    await expect(group.getByText(`Visible thought ${index}`, { exact: true })).toBeVisible();
  }
  const last = cards.last().getByRole('button').first();
  await last.click();
  await send([{ type: 'reasoning-delta', id: '8', delta: ' continued' }]);
  await expect(last).toHaveAttribute('aria-expanded', 'false');
  await last.click();
  await send([{ type: 'reasoning-end', id: '8' }]);
  await page.waitForTimeout(1_200);
  await expect(last).toHaveAttribute('aria-expanded', 'true');
  await expect(activity).toHaveText('Thinking');
  await send([tool('next')]);
  await expect(activity).toContainText('echo process');
  await expect(group.getByText('Visible thought 0', { exact: true })).toBeVisible();
  await cards.first().getByRole('button').first().click();
  await send([output('next')]);
  await expect(activity).toBeEmpty();
  await expect(cards.first().getByRole('button').first()).toHaveAttribute('aria-expanded', 'false');
  await send([{ type: 'finish', finishReason: 'stop' }]);
  await expect(page.getByTestId('assistant-process-activity')).toHaveCount(0);
  await expect(group.getByTestId('assistant-process-trigger')).toHaveAttribute('aria-expanded', 'true');
  await cards.first().getByRole('button').first().click();
  await expect(group.getByText('Visible thought 0', { exact: true })).toBeVisible();
});

test('an empty live activity row preserves height with the process closed or open', async ({ page, request }) => {
  const { send, group, trigger, activity } = await openProcess(page, request);
  const heights: unknown[] = [];
  for (const open of [false, true]) {
    if (open) await trigger.click();
    const id = `command-${open}`;
    await send([tool(id)]);
    await expect(activity).toContainText('echo process');
    const before = { group: (await group.boundingBox())!.height, activity: (await activity.boundingBox())!.height };
    await send([output(id)]);
    await expect(activity).toBeEmpty();
    await expect(trigger).toHaveAttribute('aria-expanded', String(open));
    const after = { group: (await group.boundingBox())!.height, activity: (await activity.boundingBox())!.height };
    heights.push({ open, before, after });
    expect(after.activity).toBeCloseTo(before.activity, 2);
    expect(after.activity).toBeCloseTo(32, 2);
    expect(after.group).toBeCloseTo(before.group, 2);
  }
  await send(thought('boundary'));
  await expect(activity).toHaveText('Thinking');
  const thinkingHeight = (await activity.boundingBox())!.height;
  await send([{ type: 'reasoning-end', id: 'boundary' }, tool('boundary')]);
  await expect(activity).toContainText('echo process');
  expect((await activity.boundingBox())!.height).toBeCloseTo(thinkingHeight, 2);
  await send([output('boundary'), { type: 'text-start', id: 'answer' },
    { type: 'text-delta', id: 'answer', delta: 'Next stage is visible' }]);
  await expect(page.getByTestId('assistant-text')).toHaveText('Next stage is visible');
  await expect(page.getByTestId('assistant-process-activity')).toHaveCount(0);
  await send([{ type: 'text-end', id: 'answer' }, { type: 'finish', finishReason: 'stop' }]);
  await expect(page.getByTestId('assistant-turn-process-trigger')).toBeVisible();
  await expect(page.getByTestId('assistant-text')).toHaveText('Next stage is visible');
  await test.info().attach('activity-geometry', { body: JSON.stringify(heights), contentType: 'application/json' });
});
