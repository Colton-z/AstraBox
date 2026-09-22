import { ReadingColumn } from '@/components/shell';
import { ComposerQueue, PendingInteractionCard } from '../../components/Composer';
import { PENDING_INTERACTION_PANEL_BODY_CLASS } from '../../components/pendingInteractionLayout';
import type { InteractionResponse, PendingInteraction, PermissionMode } from '../../types';
import type { QueuedMessageItem } from '../../utils/messages';
import { PermissionModeStripInline } from './PermissionModeStrip';
import { StopGenerationButton } from './StopGenerationButton';

// Footer shown in place of the composer while the agent is blocked on a
// pending interaction.
export function SessionPendingInteractionFooter({
  displayedQueue,
  removeQueueItem,
  retryQueueItem,
  canRetryQueued,
  interaction,
  submitting,
  onSubmit,
  isInterruptSettling,
  onStop,
  permissionMode,
  permissionModes,
  showPermissionMode,
  canChangePermissionMode,
  modeSwitching,
  cyclePermissionMode,
  selectPermissionMode,
}: {
  displayedQueue: QueuedMessageItem[];
  removeQueueItem: (id: string) => void;
  retryQueueItem: (id: string) => void;
  canRetryQueued: boolean;
  interaction: PendingInteraction;
  submitting: boolean;
  onSubmit: (response: InteractionResponse) => Promise<void>;
  isInterruptSettling: boolean;
  onStop: () => Promise<void>;
  permissionMode: PermissionMode;
  permissionModes: readonly PermissionMode[];
  showPermissionMode: boolean;
  canChangePermissionMode: boolean;
  modeSwitching: boolean;
  cyclePermissionMode: () => Promise<void>;
  selectPermissionMode: (mode: string) => Promise<void>;
}) {
  return (
    // The border and the ground run the width of the pane; what a reader acts
    // on is capped and centred on the transcript's column, the way the
    // composer this replaces is (ReadingColumn).
    <div data-testid="pending-interaction-panel" className="shrink-0 border-t border-border bg-background">
      <ReadingColumn className={`${PENDING_INTERACTION_PANEL_BODY_CLASS} py-3`}>
        <ComposerQueue
          queuedMessages={displayedQueue}
          onRemoveQueuedMessage={removeQueueItem}
          onRetryQueuedMessage={retryQueueItem}
          canRetryQueuedMessages={canRetryQueued}
        />
        <div className="flex min-h-0 flex-col gap-2">
          <PendingInteractionCard
            interaction={interaction}
            submitting={submitting}
            onSubmit={onSubmit}
            variant="composer"
            stopControl={(
              <StopGenerationButton
                className="shrink-0"
                disabled={isInterruptSettling}
                status="submitted"
                isStopping={isInterruptSettling}
                onStop={() => { void onStop(); }}
              />
            )}
          />
          {showPermissionMode && (
            <div className="flex items-center justify-end gap-2">
              <PermissionModeStripInline
                permissionMode={permissionMode}
                permissionModes={permissionModes}
                canChange={canChangePermissionMode}
                modeSwitching={modeSwitching}
                onSelect={selectPermissionMode}
              />
            </div>
          )}
        </div>
      </ReadingColumn>
    </div>
  );
}
