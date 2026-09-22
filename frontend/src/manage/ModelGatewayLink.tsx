import useSWR from 'swr';
import { useTranslation } from 'react-i18next';
import { ArrowUpRight } from 'lucide-react';

import { adminListIntegrations } from '@/api';
import { ErrorNote } from '@/components/shell';
import { keepsLastRead } from '@/hooks/useKeepCurrent';

/** Reuse the deployment's management link without issuing new privileges. */
export function ModelGatewayLink() {
  const { t } = useTranslation();
  const { data, error } = useSWR('/api/v1/admin/integrations', adminListIntegrations);
  if (error && !keepsLastRead(error, { background: true }, data !== undefined)) {
    return <ErrorNote>{t('manage:nav.integrations_unavailable')}</ErrorNote>;
  }
  const gateways = data?.services.filter((service) => service.category === 'model_gateway') ?? [];
  return <>{gateways.map((service) => (
    <a key={service.id} href={service.admin_url} target="_blank" rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline">
      {t('misc:agent_form.fields.model.manage_gateway', { name: service.name })}
      <ArrowUpRight className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{t('manage:nav.opens_new_tab')}</span>
    </a>
  ))}</>;
}
