/**
 * Opt-in acceptance of the Google Calendar and Google Drive connectors against
 * a real Google account.
 *
 * The deployment runs the connector hub with the Google overlay. Each connector
 * takes a consent as the hub's administrator, unless the hub kept one, and one
 * as the suite's own user (fixtures/googleConnectors.ts).
 *
 * The expected answers come from Google, not from AstraBox: the hub's servers,
 * asked directly as the suite user, put an event on that person's calendar and
 * a file in their Drive, each named with this run's tag, and the person's
 * Assistant must report what they hold, in a conversation and from a job it
 * schedules, whose run reaches Calendar from Hermes' scheduler with no
 * conversation open. The event the Assistant is asked to add
 * must then be on the calendar when the hub's server reads it. Then the run
 * removes its events and file from Google and disconnects both apps, and the
 * Assistant, asked for the calendar again, shows its owner a card to connect
 * it. If a case fails first, the run's Google records are removed anyway and
 * the AstraBox records are kept for diagnosis.
 *
 * Fixture: ASTRABOX_E2E_GOOGLE_CLIENT_FILE, as fixtures/googleConnectors.ts says.
 */
import { randomUUID } from 'node:crypto';

import { expect, test } from '@playwright/test';

import { AstraApi, messageText, visibleMessages, type MessageRecord } from '../fixtures/astraApi';
import { closePageAfterAssertions, openSessionView } from '../fixtures/sessionPage';
import {
  callHubTool, connectorRow, connectPerson, setUpGoogleConnector, type HubAnswer,
} from '../fixtures/googleConnectors';

test.use({ locale: 'en-US' });

const CALENDAR = 'google-calendar';
const DRIVE = 'google-drive';
// The hub names each tool after its connector; engines then render the dashes
// their own way (Hermes turns them into underscores).
const HUB_TOOL = {
  getEvents: 'google-calendar-get-events',
  manageEvent: 'google-calendar-manage-event',
  createFile: 'google-drive-create-drive-file',
  searchFiles: 'google-drive-search-drive-files',
  updateFile: 'google-drive-update-drive-file',
};
const GET_EVENTS = /get[-_]events/;
const MANAGE_EVENT = /manage[-_]event/;
const FILE_CONTENT = /get[-_]drive[-_]file[-_]content/;

const tag = `astrabox-e2e-${randomUUID().slice(0, 8)}`;
// Three days ahead, at hours no one keeps, so the run's events stand apart.
const day = new Date(Date.now() + 3 * 86_400_000).toISOString().slice(0, 10);
const nextDay = new Date(Date.parse(`${day}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10);

const state = {
  userId: '',
  assistantId: '',
  seededTitle: `${tag} standup`,
  addedTitle: `${tag} review`,
  fileName: `${tag}.txt`,
  code: randomUUID().slice(0, 8).toUpperCase(),
  calendarWritten: false,
  fileId: '',
  googleRecordsRemoved: false,
};

function asPerson(connectorId: string, tool: string, args: Record<string, unknown>): HubAnswer {
  const answer = callHubTool(state.userId, connectorId, tool, args);
  expect(answer.is_error, `${tool}: ${answer.text.slice(0, 300)}`).toBe(false);
  return answer;
}

/** The run's events on the person's calendar, as Google lists them. */
function runEvents(): { title: string; starts: string; id: string }[] {
  const { text } = asPerson(CALENDAR, HUB_TOOL.getEvents, {
    time_min: `${day}T00:00:00Z`, time_max: `${nextDay}T00:00:00Z`, query: tag, max_results: 25,
  });
  // A start reads "2026-10-10T01:00:00-05:00 [America/Chicago; weekday: ...]".
  return [...text.matchAll(/- "([^"]*)" \(Starts: (\S+)[^)]*?, Ends: .*?ID: (\S+) \|/g)]
    .map(([, title, starts, id]) => ({ title, starts, id }));
}

/** Remove what this run put in Google, once: its events and its file. */
function removeGoogleRecords(): void {
  if (state.googleRecordsRemoved) return;
  if (state.calendarWritten) {
    for (const event of runEvents()) {
      asPerson(CALENDAR, HUB_TOOL.manageEvent, { action: 'delete', event_id: event.id });
    }
  }
  if (state.fileId) asPerson(DRIVE, HUB_TOOL.updateFile, { file_id: state.fileId, trashed: true });
  state.googleRecordsRemoved = true;
}

async function ask(api: AstraApi, sessionId: string, prompt: string) {
  const turn = await api.sendTurn(sessionId, prompt, 150_000);
  expect(turn.errorText, 'the turn must finish without a stream error').toBeNull();
  const history = visibleMessages(await api.getMessages(sessionId, 200));
  const blocks = history.flatMap((row: MessageRecord) => row.blocks || []);
  const lastAssistant = [...history].reverse().find((row) => row.role === 'assistant');
  return {
    calls: (pattern: RegExp) => blocks.filter(
      (block) => block.type === 'tool_use' && pattern.test(String(block.name || '')),
    ),
    resultsOf: (pattern: RegExp) => {
      const ids = new Set(blocks
        .filter((block) => block.type === 'tool_use' && pattern.test(String(block.name || '')))
        .map((block) => block.id));
      return blocks.filter((block) => block.type === 'tool_result' && ids.has(block.tool_use_id));
    },
    reply: lastAssistant ? messageText(lastAssistant) : '',
  };
}

test.describe.serial('Google Calendar and Drive connectors with a real Google account', () => {
  test.afterAll(() => {
    removeGoogleRecords();
  });

  // One consent per case: each may wait for the tester up to the consent timeout.
  for (const connectorId of [CALENDAR, DRIVE]) {
    test(`an administrator sets ${connectorId} up`, async ({ request }) => {
      await setUpGoogleConnector(new AstraApi(request), request, connectorId);
    });
  }

  for (const connectorId of [CALENDAR, DRIVE]) {
    test(`a person connects ${connectorId}`, async ({ request }) => {
      const api = new AstraApi(request);
      state.userId = String((await api.data<{ user_id: string }>('GET', '/user/current')).user_id);
      await connectPerson(api, connectorId);
    });
  }

  test('Google holds an event and a file for the person', () => {
    expect(state.userId, 'the person must be connected').not.toBe('');
    state.calendarWritten = true;
    asPerson(CALENDAR, HUB_TOOL.manageEvent, {
      action: 'create', summary: state.seededTitle,
      start_time: `${day}T05:00:00Z`, end_time: `${day}T05:30:00Z`,
    });
    expect(runEvents().map((event) => event.title)).toEqual([state.seededTitle]);

    const created = asPerson(DRIVE, HUB_TOOL.createFile, {
      file_name: state.fileName, content: `The code is ${state.code}.`, mime_type: 'text/plain',
    });
    state.fileId = /\(ID: ([\w-]+)\)/.exec(created.text)?.[1] ?? '';
    expect(state.fileId, created.text.slice(0, 300)).not.toBe('');
  });

  test("the person's Assistant offers both apps", async ({ request }) => {
    const api = new AstraApi(request);
    expect(state.fileId, 'the person must hold the run\'s file').not.toBe('');
    const environment = await api.assistantEnvironmentName();
    const assistant = await api.createAssistant({
      display_name: `__e2e_calendar_drive_${randomUUID().slice(0, 8)}`,
      environment_name: environment,
      model_config_override: { model_name: await api.assistantModelName(environment) },
    });
    state.assistantId = assistant.assistant_id;
    await api.data('PUT', `/assistants/${state.assistantId}/connectors/${CALENDAR}`);
    expect((await api.data<{ enabled: string[] }>(
      'PUT', `/assistants/${state.assistantId}/connectors/${DRIVE}`,
    )).enabled.sort()).toEqual([CALENDAR, DRIVE]);
    await api.waitForWorkspaceReady(state.assistantId, 150_000);
  });

  test('the Assistant reads the event Google Calendar holds', async ({ request }) => {
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    await api.waitForSessionReady(sessionId, 120_000);
    const { calls, resultsOf, reply } = await ask(api, sessionId,
      `Use the Google Calendar tool get_events on my primary calendar with time_min ${day}T00:00:00Z `
      + `and time_max ${nextDay}T00:00:00Z. Reply with only the titles of the events that contain "${tag}".`);
    expect(calls(GET_EVENTS), 'the Assistant must read the calendar').not.toHaveLength(0);
    expect(resultsOf(GET_EVENTS).some(
      (result) => result.is_error !== true && JSON.stringify(result.content).includes(state.seededTitle),
    ), 'the calendar result must carry the event Google holds').toBe(true);
    expect(reply).toContain(state.seededTitle);
  });

  test("a scheduled briefing reads the owner's calendar on its own", async ({ request }) => {
    // The run happens in Hermes' scheduler process, with no conversation
    // open, so its Calendar call reaches the hub only through the platform's
    // relay as the Assistant's owner.
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    await api.waitForSessionReady(sessionId, 120_000);
    const marker = `BRIEFING_${tag}`;
    const dueAt = new Date(Date.now() + 20_000).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const jobPrompt = `Use the Google Calendar tool get_events on my primary calendar with time_min ${day}T00:00:00Z `
      + `and time_max ${nextDay}T00:00:00Z. Reply with ${marker} followed by the titles of the events `
      + `that contain "${tag}".`;
    const { reply } = await ask(api, sessionId, [
      `Use the cronjob_manage tool once, with action="create", schedule=${JSON.stringify(dueAt)}`,
      `and this exact prompt: ${JSON.stringify(jobPrompt)}`,
      'After it succeeds, reply SCHEDULED and finish your response.',
      `Do not run the job now, do not use action="run", and never write ${marker} yourself.`,
    ].join('\n'));
    expect(reply).toContain('SCHEDULED');
    const assistants = visibleMessages(await api.getMessages(sessionId, 200)).filter((row) => row.role === 'assistant');
    const report = await api.waitForAssistantMessageMatching(
      sessionId, assistants.length, (row) => messageText(row).includes(marker), 150_000,
    );
    expect(messageText(report), "the run reports the event Google holds for the owner").toContain(state.seededTitle);
  });

  test('the Assistant reads the file Google Drive holds', async ({ request }) => {
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    await api.waitForSessionReady(sessionId, 120_000);
    const { calls, reply } = await ask(api, sessionId,
      `Find the file named "${state.fileName}" in my Google Drive and read it with the Google Drive tools. `
      + 'Reply with only the code written in it.');
    expect(calls(FILE_CONTENT), 'the Assistant must read the file').not.toHaveLength(0);
    expect(reply).toContain(state.code);
  });

  test('an event the Assistant adds is on the Google Calendar', async ({ request }) => {
    const api = new AstraApi(request);
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    await api.waitForSessionReady(sessionId, 120_000);
    state.calendarWritten = true;
    const { calls } = await ask(api, sessionId,
      `Add an event titled "${state.addedTitle}" to my primary Google Calendar from ${day}T06:00:00Z `
      + `to ${day}T06:30:00Z, with no attendees. Reply with "done" once it is created.`);
    expect(calls(MANAGE_EVENT), 'the Assistant must write the calendar').not.toHaveLength(0);
    const added = runEvents().find((event) => event.title === state.addedTitle);
    expect(added, 'Google must hold the event the Assistant added').toBeTruthy();
    expect(Date.parse(added!.starts), `starts at ${added!.starts}`).toBe(Date.parse(`${day}T06:00:00Z`));
  });

  test('the run leaves nothing in Google, and the person disconnects both apps', async ({ request }) => {
    const api = new AstraApi(request);
    removeGoogleRecords();
    expect(runEvents()).toEqual([]);
    const search = asPerson(DRIVE, HUB_TOOL.searchFiles, { query: `name = '${state.fileName}'` });
    expect(search.text).not.toContain(state.fileId);

    for (const connectorId of [CALENDAR, DRIVE]) {
      await api.data('DELETE', `/connectors/${connectorId}/connection`);
      expect((await connectorRow(api, '/connectors', connectorId)).status).toBe('disconnected');
    }
  });

  test('the Assistant asks its owner to connect Google Calendar with a card they can open', async ({ request, page }) => {
    const api = new AstraApi(request);
    expect((await connectorRow(api, '/connectors', CALENDAR)).status).toBe('disconnected');
    // An Assistant's calls reach the platform through one address for all
    // its conversations; the card must still show in the one that asked.
    const { session_id: sessionId } = await api.startAssistantConversation(state.assistantId);
    await api.waitForSessionReady(sessionId, 120_000);
    const turn = await api.sendTurn(
      sessionId,
      `What is on my Google Calendar on ${day}? If Google Calendar is not connected for me, ask me to connect it.`,
      150_000,
    );
    expect(turn.errorText, 'the turn must finish without a stream error').toBeNull();
    const parts = visibleMessages(await api.getMessages(sessionId, 100))
      .flatMap((row: MessageRecord) => row.blocks || [])
      .filter((block) => block.type === 'ui_data')
      .map((block) => block.part as { type?: string; data?: Record<string, unknown> })
      .filter((part) => part.type === 'data-tool-card');
    expect(parts.map((part) => part.data), 'the card is kept in the conversation').toEqual([
      expect.objectContaining({ kind: 'connection_request', connectorId: CALENDAR, name: 'Google Calendar' }),
    ]);

    await openSessionView(page, sessionId);
    const card = page.getByTestId('connection-request-card');
    await expect(card, 'the owner sees the card after the turn').toBeVisible({ timeout: 30_000 });
    const [signIn] = await Promise.all([
      page.waitForEvent('popup'),
      card.getByRole('button', { name: 'Connect', exact: true }).click(),
    ]);
    await expect.poll(() => signIn.url(), { timeout: 30_000 }).toMatch(/^https:\/\/accounts\.google\.com\//);
    await signIn.close();
    await closePageAfterAssertions(page);
    await api.deleteAssistant(state.assistantId);
  });
});
