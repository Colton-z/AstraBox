import { useTranslation } from 'react-i18next';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { Button } from '@/components/ui/button';
import type { UserAction } from '../../types';
import type { useSessionUserAction } from '../hooks/useSessionUserAction';

export function UserActionNotice({ action }: { action: UserAction }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2" role="status">
      <p>{action.message}</p>
      {action.url && <a className="underline" href={action.url} target="_blank" rel="noopener noreferrer">
        {t('chat:user_action.open')}
      </a>}
    </div>
  );
}

export function SessionUserAction({ gate }: { gate: ReturnType<typeof useSessionUserAction> }) {
  const { t } = useTranslation();
  if (!gate.applicable || gate.ready) return null;
  return (
    <div className="mb-3 space-y-2 rounded-lg border p-3 text-sm" aria-busy={gate.busy}>
      {gate.state?.user_action && <UserActionNotice action={gate.state.user_action} />}
      {gate.busy && <p role="status">{t('chat:user_action.checking')}</p>}
      {gate.error && <ErrorNote>{t('chat:user_action.failed')} {gate.error}</ErrorNote>}
      <div className="flex flex-wrap gap-2">
        {!gate.error && gate.state?.actions.map((action) => (
          <Button key={action.id} size="sm" variant="outline" disabled={gate.busy}
            onClick={() => gate.perform(action.id)}>{action.label}</Button>
        ))}
        <Button size="sm" variant="outline" disabled={gate.busy} onClick={gate.refresh}>
          {t('chat:user_action.refresh')}
        </Button>
      </div>
    </div>
  );
}
