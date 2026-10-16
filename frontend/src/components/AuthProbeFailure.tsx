import { useTranslation } from 'react-i18next';

import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';

/** Keep an unknown identity on the entry page until its probe succeeds. */
export function AuthProbeFailure({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <main className="flex min-h-svh items-center justify-center bg-background px-6">
      <div className="flex w-full max-w-sm flex-col gap-4">
        <ErrorNote title={t('shell:signin.probe_failed')}>
          {t('shell:signin.probe_failed_note')}
        </ErrorNote>
        <Button onClick={onRetry}>{t('common:retry')}</Button>
      </div>
    </main>
  );
}
