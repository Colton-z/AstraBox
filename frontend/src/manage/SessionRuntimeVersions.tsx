import { useTranslation } from 'react-i18next';
import type { AdminSessionVersions } from '@/types';
import { formatDateTime } from './agentConfig';

/** The last observation is persisted; displaying it never contacts a runtime. */
export function SessionRuntimeVersions({ versions, compact = false }: {
  versions?: AdminSessionVersions;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const unknown = (reason?: string | null) => t(`manage:runtime_versions.reasons.${reason ?? 'runtime_not_observed'}`, {
    defaultValue: reason ?? t('manage:runtime_versions.reasons.runtime_not_observed'),
  });
  return (
    <div data-testid="runtime-versions" data-runtime-generation={versions?.runtime_generation ?? ''}
      className="min-w-0 space-y-1 text-xs">
      {(versions?.components ?? []).map((item) => (
        <div key={item.component} title={`${item.component}: ${item.version ?? unknown(item.reason)} (${item.source})`}>
          <span className="text-muted-foreground">{item.component}: </span>
          <span className="break-all font-mono">{compact && item.component === 'astrabox-runner'
            ? item.version?.slice(0, 12) ?? unknown(item.reason) : item.version ?? unknown(item.reason)}</span>
          {!compact && <div className="text-muted-foreground">{t('manage:runtime_versions.source')}: {item.source}</div>}
        </div>
      ))}
      {versions?.reason && <p className="text-muted-foreground">{unknown(versions.reason)}</p>}
      {!versions && <p>{unknown()}</p>}
      {!compact && versions?.observed_at && <>
        <p className="text-muted-foreground">{t('manage:runtime_versions.observed')}: {formatDateTime(versions.observed_at)}</p>
        <p className="break-all font-mono">{versions.sandbox_id}</p>
        <p className="break-all font-mono">{versions.runtime_generation}</p>
      </>}
      {versions?.sandbox_id && versions.matches_bound_sandbox === false
        && <p className="text-citrine-fg">{t('manage:runtime_versions.previous_instance')}</p>}
    </div>
  );
}
