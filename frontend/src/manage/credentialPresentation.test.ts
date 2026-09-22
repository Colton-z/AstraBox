import { beforeEach, describe, expect, it } from 'vitest';

import i18n from '@/i18n';

import { credentialDeliveryLabel, credentialTypeLabel } from './credentialPresentation';

describe('credential presentation', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en');
  });

  it('renders delivery modes as user-facing explanations', () => {
    const delivery = {
      deployment_mode: 'team' as const,
      model_credentials: 'egress_placeholder' as const,
      mcp_credentials: 'egress_injection' as const,
      environment_credentials: 'unavailable' as const,
    };

    expect(credentialDeliveryLabel(delivery, 'model_credentials', i18n.t))
      .not.toContain('egress_placeholder');
    expect(credentialDeliveryLabel(delivery, 'mcp_credentials', i18n.t))
      .not.toContain('egress_injection');
  });

  it('renders credential types as service concepts', async () => {
    expect(credentialTypeLabel('mcp_static_header', i18n.t)).toBe(
      'API key header for an MCP service',
    );
    expect(credentialTypeLabel('http_basic', i18n.t)).toBe('HTTP Basic (Git HTTPS)');
    await i18n.changeLanguage('zh');
    expect(credentialTypeLabel('http_basic', i18n.t)).toBe('HTTP Basic（Git HTTPS）');
  });
});
