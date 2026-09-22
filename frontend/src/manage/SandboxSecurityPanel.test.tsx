// @vitest-environment jsdom
/**
 * Show the network and credential protection reported by each sandbox.
 *
 * An unavailable report must remain visible with its reason. Otherwise an
 * operator could mistake an empty panel for confirmed network filtering.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';
import type { AdminSandboxSecurity } from '@/types';

// A plain async function rather than a spy returning a rejected promise: a spy
// records and inspects that promise, which turns a refusal — a case this file
// exists to cover — into an unhandled rejection before the component sees it.
let respond: () => Promise<AdminSandboxSecurity> = async () => contained();

vi.mock('@/api', async () => ({
  // The real ApiError: the panel tells a server refusal from a failed request
  // with `instanceof`, so a stand-in would test a different predicate than the
  // one that ships.
  ApiError: (await vi.importActual<typeof import('@/api')>('@/api')).ApiError,
  adminReadSandboxSecurity: () => respond(),
}));

const { SandboxSecurityPanel } = await import('./SandboxSecurityPanel');
const { ApiError } = await import('@/api');

function contained(): AdminSandboxSecurity {
  return {
    sandbox_id: 'sb-1',
    available: true,
    default_action: 'deny',
    egress_rules: [{ action: 'allow', target: 'api.deepseek.com' }],
    credential_names: ['astrabox-model-gateway'],
    binding_names: ['astrabox-model-gateway'],
    detail: null,
  };
}

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

beforeEach(() => {
  respond = async () => contained();
});

afterEach(cleanup);

describe('SandboxSecurityPanel', () => {
  it('keeps the busy refresh label at full contrast', () => {
    respond = () => new Promise<AdminSandboxSecurity>(() => {});
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    const refresh = screen.getByRole('button', { name: 'Refresh' });
    expect(refresh.hasAttribute('disabled')).toBe(true);
    expect(refresh.className).toContain('disabled:opacity-100');
  });

  it('shows the policy and the vault names a contained box reports', async () => {
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    await waitFor(() => expect(screen.getByText(/deny/)).toBeTruthy());
    expect(screen.getByText('api.deepseek.com')).toBeTruthy();
    expect(screen.getAllByText('astrabox-model-gateway').length).toBeGreaterThan(0);
  });

  it('shows the reason when security settings cannot be verified', async () => {
    respond = async () => ({
      ...contained(),
      available: false,
      default_action: null,
      egress_rules: [],
      credential_names: [],
      binding_names: [],
      detail: 'no egress sidecar answered for this sandbox',
    });
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    await waitFor(() => expect(screen.getByText(
      "AstraBox cannot verify this sandbox's security settings",
    )).toBeTruthy());
    expect(screen.getByText(/no egress sidecar answered/)).toBeTruthy();
  });

  it('warns when the default action is allow, because the rules then add nothing', async () => {
    respond = async () => ({ ...contained(), default_action: 'allow' });
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    await waitFor(() => expect(screen.getByText(/not a complete allowlist/i)).toBeTruthy());
  });

  it('reports a refusal with its status rather than rendering nothing', async () => {
    respond = async () => {
      throw new ApiError('AGENT_RUNTIME_ERROR', 'sandbox unreachable', 502);
    };
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    await waitFor(() => expect(screen.getByText(/502/)).toBeTruthy());
  });

  it('reports a transport failure too', async () => {
    respond = async () => {
      throw new Error('network down');
    };
    render(<SandboxSecurityPanel sandboxId="sb-1" />);

    await waitFor(() => expect(screen.getByText(/network down/)).toBeTruthy());
  });
});
