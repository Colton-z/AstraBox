// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  isInaccessible,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminSessionDetail, AdminSessionTrace } from '@/types';

/**
 * The export control on a session's record page.
 *
 * `buildAdminSessionTranscriptUrl` is deliberately not mocked. Reading the real
 * address from the rendered anchor verifies that the page is wired to the
 * production URL builder; mocking it would check only the test double.
 */
let detail: () => Promise<AdminSessionDetail> = async () => sessionDetail();
let trace: (turnId?: string) => Promise<AdminSessionTrace> = async () => ({
  turns: [],
  messages: [],
  frames: [],
});
/** Every trace read, so a turn selection can be checked for what it asked for. */
let traceCalls: { sessionId: string; turnId?: string }[] = [];

vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    adminGetSessionDetail: async () => detail(),
    adminGetSessionTrace: async (sessionId: string, opts?: { turnId?: string }) => {
      traceCalls.push({ sessionId, turnId: opts?.turnId });
      return trace(opts?.turnId);
    },
    adminKillSession: async () => ({}),
  };
});

const { default: SessionDetailPage } = await import('./SessionDetailPage');
const { buildAdminSessionTranscriptUrl } = await import('@/api');

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
  // Radix's Select drives a real pointer interaction; jsdom implements none of
  // these. Shimmed rather than avoided, because the behaviour worth testing is
  // "picking a turn asks the server for that turn", and a test that never
  // opened the list could not see it.
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});
beforeEach(() => {
  detail = async () => sessionDetail();
  trace = async () => ({ turns: [], messages: [], frames: [] });
  traceCalls = [];
});

function sessionDetail(overrides: Partial<AdminSessionDetail> = {}): AdminSessionDetail {
  return {
    session_id: 'sess-1',
    user_id: 'u-1',
    display_name: 'Alice',
    state: 'READY',
    sandbox_id: 'sbx-1',
    agent_id: 'agent-1',
    template_name: 'claude-code',
    created_at: '2026-08-01T09:00:00+00:00',
    updated_at: '2026-08-01T09:30:00+00:00',
    ...overrides,
  };
}

function renderPage(sessionId = 'sess-1') {
  return render(
    <MemoryRouter initialEntries={[`/manage/sessions/${sessionId}`]}>
      <Routes>
        <Route path="/manage/sessions/:sessionId" element={<SessionDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('SessionDetailPage export', () => {
  it('points the export action at this session own export endpoint', async () => {
    renderPage('sess-1');

    const link = await screen.findByRole('link', { name: /export/i });
    expect(link.getAttribute('href')).toBe(buildAdminSessionTranscriptUrl('sess-1'));
    // The address carries the record, not a constant: a builder called with the
    // wrong argument still renders a link and still downloads something.
    expect(link.getAttribute('href')).toContain('sess-1');
  });

  it('carries the session id through the URL encoder', async () => {
    // A session id is an opaque key; nothing guarantees it is URL-clean. The
    // page must not hand a raw one to the address bar.
    detail = async () => sessionDetail({ session_id: 'sess/with space' });
    renderPage('sess%2Fwith%20space');

    const link = await screen.findByRole('link', { name: /export/i });
    expect(link.getAttribute('href')).toBe(buildAdminSessionTranscriptUrl('sess/with space'));
    expect(link.getAttribute('href')).not.toContain(' ');
  });

  it('asks the browser to download rather than navigate', async () => {
    // Without `download` the JSON renders in the tab on any response the server
    // does not mark as an attachment.
    renderPage();

    const link = await screen.findByRole('link', { name: /export/i });
    expect(link.hasAttribute('download')).toBe(true);
  });

  it('does not offer export beside an armed destructive confirm', async () => {
    // An export sitting next to "End Session" while that confirm is armed is a
    // second target under a pointer that came to press the first one. The
    // confirm is a modal dialog, so the export stays mounted and the dialog is
    // what holds it out of reach.
    //
    // Which is why the assertion is unreachable rather than absent: absence
    // fails on a correct page, and — were the anchor ever rendered into a
    // portal of its own — would pass over a dialog that left the page live
    // behind it, which is the defect this case is here for.
    const { container } = renderPage();
    const link = await screen.findByRole('link', { name: /export/i });

    screen.getByRole('button', { name: /^kill$/i }).click();

    const dialog = await screen.findByRole('alertdialog');
    // The armed step lives in the dialog with the question, not in the action
    // row beside the export.
    expect(within(dialog).getByRole('button', { name: /end session/i })).toBeTruthy();
    expect(container.querySelector('a[download]')).toBe(link);

    // Out of the accessibility tree — no role query reaches it, and neither
    // does a screen reader — and out of the tab order, because focus is held
    // inside the dialog until it is answered.
    expect(screen.queryByRole('link', { name: /export/i })).toBeNull();
    expect(isInaccessible(link)).toBe(true);
    await waitFor(() => {
      expect(dialog.contains(document.activeElement)).toBe(true);
    });
  });
});

describe('SessionDetailPage last_error', () => {
  it('shows the session own failure text on its page', async () => {
    // /manage/errors hands a row off to this page, so arriving by clicking an
    // error and not finding the error here is the defect.
    detail = async () =>
      sessionDetail({ state: 'FAILED', last_error: 'sandbox 5f2a exited with status 137' });
    renderPage();

    expect(await screen.findByText(/sandbox 5f2a exited with status 137/)).toBeTruthy();
  });

  it('marks the failure text as the deployment own words', async () => {
    // Without data-slot="verbatim" the uppercase check in `make audit-ui` reads
    // a shouting backend message as shouting chrome. One error string in this
    // deployment is the single word READY, and no pattern tells those apart.
    detail = async () => sessionDetail({ state: 'FAILED', last_error: 'READY' });
    const { container } = renderPage();

    await screen.findByText('READY');
    const quoted = container.querySelector('[data-slot="verbatim"]');
    expect(quoted).not.toBeNull();
    expect(quoted?.textContent).toBe('READY');
  });

  it('renders no failure row when the session has not failed', async () => {
    // §5: a permanently empty "Last error —" row spends a line on saying normal.
    detail = async () => sessionDetail({ last_error: undefined });
    const { container } = renderPage();

    await screen.findByRole('link', { name: /export/i });
    expect(screen.queryByText(/last error/i)).toBeNull();
    expect(container.querySelector('[data-slot="verbatim"]')).toBeNull();
  });
});

describe('SessionDetailPage MCP configuration', () => {
  it('shows the MCP configuration as the Agent program received it', async () => {
    // The names row says a filesystem server exists; only the config says what
    // it was pointed at. That difference is what this case checks.
    detail = async () =>
      sessionDetail({
        template_mcp_servers: ['filesystem'],
        template_mcp_config: {
          filesystem: { command: 'npx', args: ['-y', '@mcp/server-filesystem', '/srv/data'] },
        },
      });
    renderPage();

    expect(await screen.findByText(/@mcp\/server-filesystem/)).toBeTruthy();
    expect(screen.getByText(/\/srv\/data/)).toBeTruthy();
  });

  it('renders no config card when the Agent carries none', async () => {
    detail = async () => sessionDetail({ template_mcp_config: null });
    renderPage();

    await screen.findByRole('link', { name: /export/i });
    expect(screen.queryByText(/mcp configuration/i)).toBeNull();
  });

  it('treats an empty object as no config rather than as a config', async () => {
    // A card holding `{}` claims this session was configured with nothing,
    // which is a different statement from having no configuration to show.
    detail = async () => sessionDetail({ template_mcp_config: {} });
    renderPage();

    await screen.findByRole('link', { name: /export/i });
    expect(screen.queryByText(/mcp configuration/i)).toBeNull();
    expect(screen.queryByText('{}')).toBeNull();
  });
});

/** A trace with two turns; the frames differ per turn so a fetch is observable. */
function tracedTurns(turnId?: string): AdminSessionTrace {
  const which = turnId || 'turn-2';
  return {
    current_turn_id: 'turn-2',
    selected_turn_id: which,
    turns: [
      { turn_id: 'turn-1', latest_created_at: '2026-08-01T09:00:00+00:00', message_count: 2 },
      { turn_id: 'turn-2', latest_created_at: '2026-08-01T09:30:00+00:00', message_count: 4 },
    ],
    messages: [],
    frames:
      which === 'turn-1'
        ? [{ seq: 1, type: 'assistant_text', text_preview: 'first turn frame', turn: 'turn-1' }]
        : [{ seq: 9, type: 'tool_use', text_preview: 'second turn frame', turn: 'turn-2' }],
  };
}

describe('SessionDetailPage turn frames', () => {
  it('lists the frames of a turn with their type and sequence', async () => {
    trace = async (turnId) => tracedTurns(turnId);
    renderPage();

    expect(await screen.findByText('second turn frame')).toBeTruthy();
    expect(screen.getByText('tool_use')).toBeTruthy();
    expect(screen.getByText('9')).toBeTruthy();
  });

  it('opens a frame to the payload it carried', async () => {
    // The preview is a summary; the reason anyone opens a frame is that the
    // summary was not enough. Collapsed, the payload must not already be there.
    trace = async (turnId) => tracedTurns(turnId);
    renderPage();

    await screen.findByText('second turn frame');
    expect(screen.queryByText(/"seq": 9/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /second turn frame/i }));
    expect(await screen.findByText(/"seq": 9/)).toBeTruthy();
  });

  it('asks the server for the turn that was picked', async () => {
    trace = async (turnId) => tracedTurns(turnId);
    renderPage();
    await screen.findByText('second turn frame');
    traceCalls = [];

    fireEvent.click(screen.getByRole('combobox', { name: /select a turn/i }));
    const option = await screen.findByRole('option', { name: /2 messages/ });
    // The select commits a choice on the pointer sequence, not on a bare
    // click, so a click alone opens the list and picks nothing.
    fireEvent.pointerDown(option);
    fireEvent.pointerUp(option);
    fireEvent.click(option);

    await waitFor(() => {
      expect(traceCalls.map((c) => c.turnId)).toContain('turn-1');
    });
    // And the frames on screen are that turn's, not the one it replaced.
    expect(await screen.findByText('first turn frame')).toBeTruthy();
  });

  it('offers no turn selector when there is only one turn', async () => {
    // §5: a select that cannot change the frames below it is a control lying
    // about being one.
    trace = async () => ({
      current_turn_id: 'turn-1',
      selected_turn_id: 'turn-1',
      turns: [{ turn_id: 'turn-1', latest_created_at: '2026-08-01T09:00:00+00:00', message_count: 1 }],
      messages: [],
      frames: [{ seq: 1, type: 'assistant_text', text_preview: 'only frame' }],
    });
    renderPage();

    await screen.findByText('only frame');
    expect(screen.queryByRole('combobox')).toBeNull();
  });

  it('says so when the server truncated the turn', async () => {
    // A bounded window presented as the whole turn is the one reading of this
    // list that would be wrong.
    trace = async () => ({
      current_turn_id: 'turn-1',
      turns: [{ turn_id: 'turn-1', message_count: 900 }],
      messages: [],
      frames: [{ seq: 900, type: 'tool_result', text_preview: 'last frame' }],
      truncated_frame_count: 412,
    });
    renderPage();

    expect(await screen.findByText(/412 earlier ones are not included/)).toBeTruthy();
  });
});

describe('SessionTraceDigest raw messages', () => {
  it('opens a message to the document it was summarised from', async () => {
    trace = async () => ({
      turns: [{ turn_id: 'turn-1', message_count: 1 }],
      messages: [
        {
          role: 'assistant',
          turn_id: 'turn-1',
          created_at: '2026-08-01T09:30:00+00:00',
          content_preview: 'checking the config',
          raw: { role: 'assistant', content: [{ type: 'text', text: 'checking the config' }] },
        },
      ],
      frames: [],
    });
    renderPage();

    const row = await screen.findByRole('button', { name: /checking the config/i });
    expect(screen.queryByText(/"type": "text"/)).toBeNull();

    fireEvent.click(row);
    expect(await screen.findByText(/"type": "text"/)).toBeTruthy();
  });

  it('leaves a message with no raw as a plain row', async () => {
    // §5 again: a disclosure that reveals nothing is an affordance that never
    // earned its place.
    trace = async () => ({
      turns: [],
      messages: [
        {
          role: 'user',
          turn_id: 'turn-1',
          created_at: '2026-08-01T09:29:00+00:00',
          content_preview: 'no raw for this one',
        },
      ],
      frames: [],
    });
    renderPage();

    await screen.findByText('no raw for this one');
    expect(screen.queryByRole('button', { name: /no raw for this one/i })).toBeNull();
  });

  it('names a raw-message disclosure when the preview is empty', async () => {
    trace = async () => ({
      turns: [],
      messages: [
        {
          role: 'assistant',
          turn_id: 'turn-1',
          created_at: '2026-08-01T09:29:00+00:00',
          raw: { role: 'assistant', content: [] },
        },
      ],
      frames: [],
    });
    renderPage();

    expect(await screen.findByRole('button', { name: /open raw assistant message/i })).toBeTruthy();
  });
});
