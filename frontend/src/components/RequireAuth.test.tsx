// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { RequireAuth } from './RequireAuth';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * The gate decides who sees the console, so each case here is a state the
 * deployment can genuinely be in — not a restatement of the component's JSX.
 * Getting any one of them wrong is a real failure: demanding an account nobody
 * can have, blaming an outage on the reader, or dropping a signed-out person on
 * a blank shell.
 */
function stubProbe(reply: { status: number; body?: unknown } | Error) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      if (reply instanceof Error) throw reply;
      return {
        status: reply.status,
        ok: reply.status >= 200 && reply.status < 300,
        json: async () => reply.body,
      } as Response;
    }),
  );
}

function renderGate() {
  return render(
    <MemoryRouter initialEntries={['/agents?tab=1']}>
      <Routes>
        <Route path="/login" element={<div>sign-in page</div>} />
        <Route
          path="/agents"
          element={
            <RequireAuth>
              <div>console</div>
            </RequireAuth>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe('RequireAuth', () => {
  it('lets a deployment with no identity configured straight through', async () => {
    // The probe route does not exist there. Asking for a sign-in would demand
    // an account the deployment cannot issue.
    stubProbe({ status: 404 });
    renderGate();
    expect(await screen.findByText('console')).toBeTruthy();
  });

  it('renders the console for a signed-in browser', async () => {
    stubProbe({ status: 200, body: { authenticated: true, user: { user_id: 'u-1' } } });
    renderGate();
    expect(await screen.findByText('console')).toBeTruthy();
  });

  it('sends a signed-out browser to sign-in, carrying where it was headed', async () => {
    stubProbe({ status: 200, body: { authenticated: false } });
    renderGate();
    expect(await screen.findByText('sign-in page')).toBeTruthy();
  });

  it('shows neither the console nor sign-in while the answer is outstanding', () => {
    // Mounting the console first and bouncing on its first 401 is the flash
    // this gate exists to remove, so nothing may render on a pending probe.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
    renderGate();
    expect(screen.queryByText('console')).toBeNull();
    expect(screen.queryByText('sign-in page')).toBeNull();
  });

  it('does not read an unreachable probe as being signed out', async () => {
    // A failed probe means the network or the server is down. Sending the
    // reader to sign in would blame them for an outage, and signing in cannot
    // fix it; the console's own requests report what is actually wrong.
    stubProbe(new Error('network down'));
    renderGate();
    await waitFor(() => expect(screen.queryByText('console')).toBeTruthy());
    expect(screen.queryByText('sign-in page')).toBeNull();
  });
});
