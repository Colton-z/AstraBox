import type { TFunction } from 'i18next';

import type { CredentialDeliveryOverview, VaultCredentialSummary } from '@/types';

type CredentialDeliveryKey = Exclude<keyof CredentialDeliveryOverview, 'deployment_mode'>;

const CREDENTIAL_TYPE_KEYS: Record<VaultCredentialSummary['auth']['type'], string> = {
  static_bearer: 'type_static',
  mcp_oauth: 'type_oauth',
  mcp_static_header: 'type_header',
  environment_variable: 'type_environment',
  http_basic: 'type_http_basic',
};

export function credentialDeliveryLabel(
  delivery: CredentialDeliveryOverview | null,
  key: CredentialDeliveryKey,
  t: TFunction,
): string {
  const mode = String(delivery?.[key] || '').trim();
  if (!mode) return '—';
  return t(`manage:credentials.delivery.${mode}`);
}

export function credentialTypeLabel(
  type: VaultCredentialSummary['auth']['type'],
  t: TFunction,
): string {
  return t(`manage:credentials.${CREDENTIAL_TYPE_KEYS[type]}`);
}
