/**
 * E2E: a Claude Code message over a megabyte reaches the conversation, and the next message goes through.
 *
 * A tool result can legitimately exceed a megabyte: Read returns a screenshot's
 * bytes in the result's content and again in `tool_use_result`, the CLI's
 * structured result, and a Write over an existing file carries the original
 * file and the patch that replaced it there. The runner sends each SDK message
 * as one WebSocket message, so the host must accept what the runner link
 * declares, not a client library's default; a message it refused was replayed
 * on every reattach, and later messages waited behind it while the
 * conversation still read Ready.
 *
 * The Agent overwrites a file of about 1.5 MB with one line. The host turns
 * that Write's structured result into the conversation's file change, and the
 * change it stored lists every removed line, over a megabyte: the message
 * under test crossed the link and was read. The turn finishes, and a second
 * message typed in the console is answered by a Bash check that reads the new
 * line back from the file.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi, messageParts } from '../fixtures/astraApi';
import { trackSessions } from '../fixtures/sessionCleanup';
import { expectComposerEnabled, openSessionView, sendPrompt } from '../fixtures/sessionPage';

test.use({ locale: 'en-US' });

const sessions = trackSessions();
const MIB = 1024 * 1024;

test('a Claude Code message over a megabyte reaches the conversation and the next message goes through', async ({ page, request }) => {
  const api = new AstraApi(request);
  const id = randomUUID().slice(0, 8);
  const file = `big-edit-${id}.txt`;
  const before = `TARGET_OLD_${id}`;
  const after = `TARGET_NEW_${id}`;
  const done = `DONE_${id}`;

  const agent = await api.defaultAgent();
  // startConversation gives the conversation `bypassPermissions`, so Bash
  // and Write run instead of waiting on an approval card.
  const sessionId = (await api.startConversation(agent.agent_id)).session_id;
  sessions.push(sessionId);
  await api.waitForSessionReady(sessionId, 60_000);

  // 16,000 lines of 93 bytes and one more: about 1.5 MB.
  const writer = `python3 -c "open('${file}','w').write(''.join('line %06d %s\\n' % (i, 'x' * 80) `
    + `for i in range(16000)) + '${before}\\n')"`;
  const edited = await api.sendTurn(sessionId, [
    `Run this exact command with the Bash tool in the current directory: ${writer}`,
    `Then use the Read tool on ${file} with offset 16000 and limit 5, which shows its last lines.`,
    `Then use the Write tool to overwrite ${file} so that it contains only the single line ${after}.`,
    `When the write has succeeded, reply ${done} and finish your response.`,
  ].join('\n'), 90_000);
  expect(edited.errorText).toBeFalsy();
  expect(edited.toolNames, JSON.stringify(edited.toolNames)).toContain('Write');
  expect(edited.text).toContain(done);

  const changes = (await api.getMessages(sessionId, 30)).messages
    .flatMap((message) => messageParts(message, 'data-file-changes'));
  const removed = changes.find((part) => JSON.stringify(part).includes(before));
  expect(removed, 'the stored file change lists the overwritten lines').toBeTruthy();
  expect(JSON.stringify(removed).length, 'the Write result the host read is over a megabyte')
    .toBeGreaterThan(MIB);

  await openSessionView(page, sessionId);
  await expectComposerEnabled(page);
  const count = `COUNT_${id}`;
  await sendPrompt(
    page,
    sessionId,
    `Run grep -c ${after} ${file} with the Bash tool and reply with exactly ${count}=<the number it printed>.`,
  );
  await expect(
    page.getByTestId('assistant-text').filter({ hasText: `${count}=1` }),
    'the next message is answered from the edited file',
  ).toHaveCount(1, { timeout: 60_000 });
});
