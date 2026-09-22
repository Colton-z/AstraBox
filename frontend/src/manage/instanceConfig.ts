import i18n from '@/i18n';

export function agentStateLabel(state?: string): string {
  const map: Record<string, string> = {
    ACTIVE: i18n.t('manage:agent_state.active'),
    PROVISIONING: i18n.t('manage:agent_state.provisioning'),
    HIBERNATING: i18n.t('manage:agent_state.hibernating'),
  };
  return map[String(state || '')] ?? String(state || '-');
}
