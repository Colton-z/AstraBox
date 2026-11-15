import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { setAssistantOwnerSecret } from '@/assistant/api';
import type { AssistantOwnerSecretStatus } from '@/assistant/types';
import { Button } from '@/components/ui/button';

import {
  ConsoleCard,
  ConsoleFieldRow,
  ConsoleTextField,
  StatusPill,
  formatDateTime,
} from './console';

/**
 * The credentials an Assistant's runtime asks its owner for, one write-only
 * field each.
 *
 * The runtime declares them (`runtime.owner_secrets`); a label and help are its
 * author's words. The server keeps each value in its secret store and reports
 * only whether it is set and when, so a field is never filled in: what it
 * holds is the next value to send, and sending it replaces the stored one. The
 * runtime does not start until every declared credential is set, so each is
 * marked required.
 */
export function AssistantOwnerSecretsCard({
  assistantId,
  secrets,
  onSaved,
}: {
  assistantId: string;
  secrets: AssistantOwnerSecretStatus[];
  /** Re-read the Assistant: the PUT answers with its row, without the workspace state. */
  onSaved: () => Promise<unknown>;
}) {
  const { t } = useTranslation();
  return (
    <ConsoleCard
      title={t('manage:assistants.owner_secrets_title')}
      intro={t('manage:assistants.owner_secrets_intro')}
    >
      {secrets.map((secret) => (
        <OwnerSecretField key={secret.key} assistantId={assistantId} secret={secret} onSaved={onSaved} />
      ))}
    </ConsoleCard>
  );
}

function OwnerSecretField({
  assistantId,
  secret,
  onSaved,
}: {
  assistantId: string;
  secret: AssistantOwnerSecretStatus;
  onSaved: () => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const id = `assistant-owner-secret-${secret.key}`;

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      await setAssistantOwnerSecret(assistantId, secret.key, value);
      setValue('');
      await onSaved();
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleFieldRow
      label={secret.label}
      htmlFor={id}
      required={secret.required && !secret.deployment_default}
      help={secret.help || undefined}
      error={error || undefined}
    >
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={secret.configured || secret.deployment_default ? 'done' : secret.required ? 'approval' : 'idle'}>
            {secret.configured
              ? t('manage:assistants.owner_secret_configured')
              : secret.deployment_default
                ? t('manage:assistants.owner_secret_deployment_default')
                : secret.required
                  ? t('manage:assistants.owner_secret_missing')
                  : t('manage:assistants.owner_secret_optional_unset')}
          </StatusPill>
          {secret.configured && secret.updated_at && (
            <span className="t-copy-sm text-muted-foreground">
              {t('manage:assistants.owner_secret_updated', { when: formatDateTime(secret.updated_at) })}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <ConsoleTextField
            id={id}
            type="password"
            value={value}
            onChange={setValue}
            disabled={saving}
            placeholder={secret.configured
              ? t('manage:assistants.owner_secret_placeholder_replace')
              : t('manage:assistants.owner_secret_placeholder_set')}
          />
          <Button className="shrink-0" disabled={saving || !value.trim()} onClick={() => void save()}>
            {saving
              ? t('manage:assistants.owner_secret_saving')
              : secret.configured
                ? t('manage:assistants.owner_secret_replace')
                : t('manage:assistants.owner_secret_set')}
          </Button>
        </div>
      </div>
    </ConsoleFieldRow>
  );
}
