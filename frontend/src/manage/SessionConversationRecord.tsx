import { useCallback, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';

import {
  adminGetHistoryBlockDetails,
  adminGetHistoryBlocks,
  adminGetSessionChildRunMessages,
  adminListSessionChildRuns,
  type SessionChildRun,
} from '@/api';
import { ArtifactNavigationProvider } from '@/artifacts/navigation';
import { ErrorNote } from '@/components/shell';
import { Button } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { AgentRow } from '@/session/components/TasksPanel';
import { MessageList } from '@/session/components/MessageList';
import { MarkdownContent } from '@/session/components/MessageParts';
import { SubagentTranscriptDrawer } from '@/session/components/SubagentTranscriptDrawer';
import { SubagentNavigationProvider } from '@/session/components/subagentNavigation';
import { TranscriptAccessContext, type TranscriptAccess } from '@/session/TranscriptAccess';
import { useSessionHistory } from '@/session/hooks/useSessionHistory';
import { useSubagentRegistry } from '@/session/hooks/useSubagentRegistry';

import { ConsoleCard, StatusPill } from './console';
import { sessionStateLabel, sessionStateTone } from './sessionConfig';

const adminTranscriptAccess: TranscriptAccess = {
  readHistory: adminGetHistoryBlocks,
  readDetails: adminGetHistoryBlockDetails,
};

const EMPTY_CHILD_RUNS: SessionChildRun[] = [];

export function SessionConversationRecord({ sessionId, conversationState }: {
  sessionId: string;
  conversationState: string;
}) {
  return (
    <TranscriptAccessContext.Provider value={adminTranscriptAccess}>
      <ArtifactNavigationProvider readOnlyTranscript>
        <Conversation sessionId={sessionId} conversationState={conversationState} />
      </ArtifactNavigationProvider>
    </TranscriptAccessContext.Provider>
  );
}

function Conversation({ sessionId, conversationState }: {
  sessionId: string;
  conversationState: string;
}) {
  const { t } = useTranslation();
  const history = useSessionHistory(sessionId);
  const pending = history.window?.pendingInteraction;
  const working = conversationState === 'PROCESSING';
  const waiting = conversationState === 'WAITING_INPUT';
  const {
    data: childRuns,
    error: childError,
    mutate: refreshChildren,
  } = useSWR(['admin-session-child-runs', sessionId], () => adminListSessionChildRuns(sessionId), {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const registry = useSubagentRegistry(childRuns?.child_runs ?? EMPTY_CHILD_RUNS);
  const [selectedChildRunId, setSelectedChildRunId] = useState<string | null>(null);
  const transcriptFrame = useRef<HTMLDivElement>(null);
  // The drawer overlays the transcript frame, and this page scrolls as a
  // whole: the frame can sit partly below the fold, under the child list that
  // opened it. Bring the whole frame into view, so the drawer reads with its
  // own scroll alone. Its height, 62dvh but at least 18rem, fits any viewport
  // taller than 18rem.
  const openChild = useCallback((childRunId: string) => {
    setSelectedChildRunId(childRunId);
    transcriptFrame.current?.scrollIntoView({ block: 'nearest' });
  }, []);

  return (
    <ConsoleCard
      title={t('manage:sessions.conversation_section')}
      actions={(working || waiting) && (
        <StatusPill tone={sessionStateTone(conversationState)}>{sessionStateLabel(conversationState)}</StatusPill>
      )}
    >
      {registry.agents.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger render={<Button variant="outline" size="sm" />}>
            {t('manage:sessions.subagents')}
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2 flex max-h-64 flex-col gap-2 overflow-y-auto">
            {registry.agents.map((entry) => (
              <AgentRow
                key={entry.childRunId}
                entry={entry}
                selected={entry.childRunId === selectedChildRunId}
                onSelect={openChild}
              />
            ))}
          </CollapsibleContent>
        </Collapsible>
      )}
      {history.error ? (
        <ErrorNote title={t('chat:page.messages_load_failed')}>
          {history.error}{' '}
          <Button variant="outline" size="sm" onClick={() => void history.refetch()}>{t('common:retry')}</Button>
        </ErrorNote>
      ) : history.loading ? (
        <p role="status" className="t-copy text-muted-foreground">{t('common:loading')}</p>
      ) : history.messages.length === 0 ? (
        <p className="t-copy text-muted-foreground">{t('misc:share.no_messages')}</p>
      ) : (
        <SubagentNavigationProvider agents={registry.agents} openSubagent={openChild}>
          <div
            ref={transcriptFrame}
            className="relative flex h-[clamp(18rem,62dvh,44rem)] min-w-0 flex-col overflow-hidden rounded-md border bg-background"
          >
            <MessageList
              messages={history.messages}
              sessionId={sessionId}
              responseExpected={working}
              firstItemIndex={100_000 - history.messages.length}
              hasMore={history.hasMore}
              loadingMore={history.loadingMore}
              loadMoreError={history.loadMoreError}
              scrollToBottomKey=""
              onLoadOlder={history.loadOlder}
            />
            <SubagentTranscriptDrawer
              sessionId={sessionId}
              refreshRevision={0}
              childRunId={selectedChildRunId}
              entry={selectedChildRunId ? registry.getAgent(selectedChildRunId) : undefined}
              onClose={() => setSelectedChildRunId(null)}
              readMessages={adminGetSessionChildRunMessages}
            />
          </div>
          {pending?.presentation === 'decision' && pending.body && (
            <div className="rounded-md border p-3">
              {pending.prompt && <p className="mb-2 text-sm font-medium">{pending.prompt}</p>}
              <MarkdownContent text={pending.body} />
            </div>
          )}
        </SubagentNavigationProvider>
      )}
      {childError && (
        <ErrorNote title={t('chat:subagent.load_failed')}>
          {childError.message}{' '}
          <Button variant="outline" size="sm" onClick={() => void refreshChildren()}>{t('common:retry')}</Button>
        </ErrorNote>
      )}
    </ConsoleCard>
  );
}
