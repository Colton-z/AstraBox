import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { ErrorNote } from '@/components/shell/ErrorNote';
import { artifactsApi } from './api';
import { ArtifactViewer } from './ArtifactViewer';
export function ArtifactPanel({
  sessionId,
  assistantId,
  selectedId,
  onSelect,
  refreshSignal,
  assistantConversation = false,
}: {
  sessionId: string;
  assistantId?: string | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  refreshSignal: number;
  assistantConversation?: boolean;
}) {
  const { t } = useTranslation();
  const [moreError, setMoreError] = useState('');
  const { data, error, mutate } = useSWR(
    assistantConversation && !assistantId
      ? null
      : ['session-artifacts', sessionId, assistantId],
    () =>
      artifactsApi.list(
        assistantId ? { assistant_id: assistantId } : { session_id: sessionId },
      ),
  );
  useEffect(() => {
    if (refreshSignal) void mutate();
  }, [refreshSignal, mutate]);
  if (assistantConversation && !assistantId)
    return (
      <div className="p-3">
        <ErrorNote>{t('misc:artifacts.assistant_identity_missing')}</ErrorNote>
      </div>
    );
  return (
    <div className="flex h-full min-h-0 flex-col">
      {(error || moreError) && (
        <div className="p-3">
          <ErrorNote>{error?.message || moreError}</ErrorNote>
          <Button variant="outline" size="sm" onClick={() => void mutate()}>
            {t('common:retry')}
          </Button>
        </div>
      )}
      {!data && !error && (
        <div className="p-4 text-sm text-muted-foreground" role="status">
          {t('common:loading')}
        </div>
      )}
      {data && !data.items.length && !selectedId && (
        <p className="p-4 text-sm text-muted-foreground">
          {t('misc:artifacts.empty_conversation')}
        </p>
      )}
      {data && data.items.length > 0 && (
        <div className="flex max-h-40 shrink-0 flex-col gap-1 overflow-auto border-b border-border p-2">
          {data.items.map((artifact) => (
            <Button
              key={artifact.artifact_id}
              variant="ghost"
              size="sm"
              className={`justify-start truncate ${selectedId === artifact.artifact_id ? 'bg-astra-tint text-astra' : ''}`}
              onClick={() => onSelect(artifact.artifact_id)}
            >
              {artifact.title}
            </Button>
          ))}
          {data.has_more && (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                void mutate(
                  async (current) => {
                    const page = await artifactsApi.list({
                      ...(assistantId
                        ? { assistant_id: assistantId }
                        : { session_id: sessionId }),
                      cursor: current!.next_cursor!,
                    });
                    return {
                      ...page,
                      items: [...current!.items, ...page.items],
                    };
                  },
                  { revalidate: false },
                ).catch((failure) => setMoreError((failure as Error).message))
              }
            >
              {t('misc:artifacts.more')}
            </Button>
          )}
        </div>
      )}
      {selectedId && (
        <ArtifactViewer key={selectedId} target={{ id: selectedId }} />
      )}
      {data?.items.length && !selectedId ? (
        <p className="p-4 text-sm text-muted-foreground">
          {t('misc:artifacts.choose')}
        </p>
      ) : null}
    </div>
  );
}
