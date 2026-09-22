// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminSandboxDiagnostics } from '@/types';

// The api module is replaced by a plain async function rather than a spy that
// returns a rejected promise: a spy records the returned promise and inspects
// it, which turns a refusal — the case this file exists to cover — into an
// unhandled rejection before the component ever sees it. Calls are recorded by
// hand instead.
const calls: [string, string][] = [];
let respond: (scope: string) => Promise<AdminSandboxDiagnostics> = async () => report();

vi.mock('@/api', async () => ({
  // ApiError is the real class: the panel tells a server refusal from a failed
  // request with `instanceof`, so a stand-in here would test a different
  // predicate than the one that ships.
  ApiError: (await vi.importActual<typeof import('@/api')>('@/api')).ApiError,
  adminReadSandboxDiagnostics: async (sandboxId: string, scope: string) => {
    calls.push([sandboxId, scope]);
    return respond(scope);
  },
}));

const { ApiError } = await import('@/api');
const { SandboxDiagnosticsPanel } = await import('./SandboxDiagnosticsPanel');

afterEach(cleanup);
beforeAll(async () => {
  await i18n.changeLanguage('en');
});
beforeEach(() => {
  calls.length = 0;
  respond = async () => report();
});

const REPORT_TEXT = 'Pod Name:   sb-1\nNamespace:  astrabox\nPhase:      Running\n';

function report(overrides: Partial<AdminSandboxDiagnostics> = {}): AdminSandboxDiagnostics {
  return {
    sandbox_id: 'sb-1',
    backend: 'open_sandbox',
    scope: 'summary',
    content_type: 'text/plain; charset=utf-8',
    text: REPORT_TEXT,
    truncated: false,
    known_scopes: ['summary', 'inspect', 'events', 'logs'],
    ...overrides,
  };
}

describe('the sandbox diagnostics panel', () => {
  it('keeps the busy refresh label at full contrast', () => {
    respond = () => new Promise<AdminSandboxDiagnostics>(() => {});
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    const refresh = screen.getByRole('button', { name: 'Refresh' });
    expect(refresh.hasAttribute('disabled')).toBe(true);
    expect(refresh.className).toContain('disabled:opacity-100');
  });

  it('shows the report verbatim, as text', async () => {
    const { container } = render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    const block = await waitFor(() => {
      const el = container.querySelector('pre');
      expect(el).toBeTruthy();
      return el!;
    });
    // Byte-for-byte, including the alignment the server rendered: this format
    // carries no schema, so anything that parsed or reflowed it would be
    // presenting a structure nobody agreed to.
    expect(block.textContent).toBe(REPORT_TEXT);
    expect(container.querySelector('table')).toBeNull();
  });

  it('shows a 501 as the backend producing no such report, with the reason', async () => {
    respond = async () => {
      throw new ApiError(
        'SANDBOX_DIAGNOSTICS_NOT_IMPLEMENTED',
        'the OpenSandbox server at http://server.test does not implement the report',
        501,
      );
    };
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    // An empty panel here would read as a sandbox with nothing to say — the
    // opposite of what happened.
    await waitFor(() => expect(screen.getByText(/does not implement/)).toBeTruthy());
    expect(
      screen.getByText(
        i18n.t('manage:sandboxes.diagnostics_unavailable', {
          scope: i18n.t('manage:sandboxes.scope.summary'),
        }),
      ),
    ).toBeTruthy();
  });

  it('does not report a failed request as a report that does not exist', async () => {
    // A transport failure — the api layer throws a plain Error, never an
    // ApiError, when no response came back at all.
    respond = async () => {
      throw new Error('Load failed: connection reset');
    };
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    // "I could not ask" and "the server produces no such report" are different
    // facts about different things: one is a property of the backend, the other
    // is a reason to retry. The panel must not print the first as the second.
    await waitFor(() =>
      expect(
        screen.getByText(
          i18n.t('manage:sandboxes.diagnostics_unreachable', {
            scope: i18n.t('manage:sandboxes.scope.summary'),
          }),
        ),
      ).toBeTruthy(),
    );
    expect(screen.getByText(/connection reset/)).toBeTruthy();
    expect(
      screen.getByText(i18n.t('manage:sandboxes.diagnostics_unreachable_note')),
    ).toBeTruthy();
    expect(
      screen.queryByText(
        i18n.t('manage:sandboxes.diagnostics_unavailable', {
          scope: i18n.t('manage:sandboxes.scope.summary'),
        }),
      ),
    ).toBeNull();
  });

  it('shows a 404 as the server refusing, with its status and reason', async () => {
    respond = async () => {
      throw new ApiError('SANDBOX_NOT_FOUND', 'no sandbox with id sb-1', 404);
    };
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    // The server answered, definitely, and said why. Printing that as "the
    // request itself failed, retry" would throw away the one thing the operator
    // needs — and would invite a retry that answers the same way forever.
    await waitFor(() =>
      expect(
        screen.getByText(
          i18n.t('manage:sandboxes.diagnostics_rejected', {
            scope: i18n.t('manage:sandboxes.scope.summary'),
            status: 404,
          }),
        ),
      ).toBeTruthy(),
    );
    expect(screen.getByText(/no sandbox with id sb-1/)).toBeTruthy();
    expect(
      screen.queryByText(
        i18n.t('manage:sandboxes.diagnostics_unreachable', {
          scope: i18n.t('manage:sandboxes.scope.summary'),
        }),
      ),
    ).toBeNull();
    // Nor is it the 501 sentence: a 404 about the sandbox says nothing about
    // whether this backend produces the report.
    expect(
      screen.queryByText(
        i18n.t('manage:sandboxes.diagnostics_unavailable', {
          scope: i18n.t('manage:sandboxes.scope.summary'),
        }),
      ),
    ).toBeNull();
  });

  it('shows a 502 from the runtime as the server refusing, not as unreachable', async () => {
    respond = async () => {
      throw new ApiError('AGENT_RUNTIME_ERROR', 'sandbox control plane returned 500', 502);
    };
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);

    await waitFor(() =>
      expect(
        screen.getByText(
          i18n.t('manage:sandboxes.diagnostics_rejected', {
            scope: i18n.t('manage:sandboxes.scope.summary'),
            status: 502,
          }),
        ),
      ).toBeTruthy(),
    );
    expect(screen.getByText(/control plane returned 500/)).toBeTruthy();
    expect(
      screen.getByText(i18n.t('manage:sandboxes.diagnostics_rejected_note')),
    ).toBeTruthy();
  });

  it('marks a capped report as capped', async () => {
    respond = async () => report({ truncated: true });
    render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);
    await waitFor(() =>
      expect(screen.getByText(i18n.t('manage:sandboxes.diagnostics_truncated'))).toBeTruthy(),
    );
  });

  it('asks for one report at a time', async () => {
    const { container } = render(<SandboxDiagnosticsPanel sandboxId="sb-1" />);
    await waitFor(() => expect(container.querySelector('pre')).toBeTruthy());
    // A report is a live read against the control plane (a log pull, an event
    // query), so opening the panel must not fetch all four — and must not ask
    // the same question twice.
    expect(calls).toEqual([['sb-1', 'summary']]);
  });
});
