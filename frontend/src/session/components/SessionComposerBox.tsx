import type React from 'react';
import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Popover as PopoverPrimitive } from '@base-ui/react/popover';
import { PromptInput, PromptInputTextarea, PromptInputFooter, PromptInputButton, PromptInputSubmit, usePromptInputAttachments } from '@/components/ai-elements/prompt-input';
import {
  QueueItemAction,
  QueueItemAttachment,
  QueueItemFile,
  QueueItemImage,
} from '@/components/ai-elements/queue';
import { ReadingColumn } from '@/components/shell';
import { XIcon } from 'lucide-react';
import type { PromptInputMessage } from '@/components/ai-elements/prompt-input';
import {
  COMPOSER_IMAGE_MEDIA_TYPES,
  readPastedImageFiles,
  turnInputImages,
  type TurnInputImage,
} from '../composerAttachments';
import { ComposerQueue } from '../../components/Composer';
import type { PermissionMode } from '../../types';
import type { QueuedMessageItem } from '../../utils/messages';
import type { DisplaySlashCommand } from '../slashCommands';
import { PermissionModeStripInline } from './PermissionModeStrip';
import { StopGenerationButton } from './StopGenerationButton';

// The composer's own paste rule and attachment strip.
//
// Both need the attachment store, which `PromptInput` puts in context, so they
// are components rendered inside it rather than markup in the box below.
function ComposerTextarea({
  acceptsImages,
  ...props
}: React.ComponentProps<typeof PromptInputTextarea> & { acceptsImages: boolean }) {
  const attachments = usePromptInputAttachments();
  return (
    <PromptInputTextarea
      {...props}
      onPaste={(event) => {
        if (!acceptsImages) return;
        const images = readPastedImageFiles(event.clipboardData);
        if (images.length === 0) return;
        // Copying a slide puts its words on the clipboard beside its picture.
        // Taking the picture must not cancel the paste that carries the
        // words: the textarea is controlled, so the default insertion is what
        // puts them in the draft. Pre-empt it only when it has nothing to
        // insert.
        if (!event.clipboardData.getData('text/plain')) {
          event.preventDefault();
        }
        attachments.add(images);
      }}
    />
  );
}

// The queue element already draws an attachment strip — the same thumbnail,
// filename chip and hover action the queued-message rows use — so the composer
// shows a pending attachment the way the queue shows a sent one.
function ComposerAttachments({ acceptsImages }: { acceptsImages: boolean }) {
  const { t } = useTranslation();
  const attachments = usePromptInputAttachments();
  useEffect(() => {
    if (!acceptsImages && attachments.files.length > 0) {
      attachments.clear();
    }
  }, [acceptsImages, attachments]);
  if (!acceptsImages || attachments.files.length === 0) return null;
  return (
    <QueueItemAttachment className="group px-2 pt-2">
      {attachments.files.map((file) => (
        <span key={file.id} className="flex items-center gap-1">
          <QueueItemImage
            data-testid="composer-attachment"
            data-filename={file.filename}
            src={file.url}
            alt={file.filename || file.mediaType}
          />
          <QueueItemFile>{file.filename || file.mediaType}</QueueItemFile>
          <QueueItemAction
            aria-label={t('chat:composer.remove_attachment')}
            onClick={() => attachments.remove(file.id)}
          >
            <XIcon className="size-3" />
          </QueueItemAction>
        </span>
      ))}
    </QueueItemAttachment>
  );
}

function ComposerSubmit({
  hasText,
  acceptsImages,
  blocked,
  ...props
}: React.ComponentProps<typeof PromptInputSubmit> & {
  hasText: boolean;
  acceptsImages: boolean;
  blocked: boolean;
}) {
  const attachments = usePromptInputAttachments();
  // An image on its own is a message; requiring text would make the picture
  // unsendable without a caption.
  const hasSomethingToSend = hasText || (acceptsImages && attachments.files.length > 0);
  return <PromptInputSubmit {...props} disabled={!hasSomethingToSend || blocked} />;
}

// Composer footer used when no pending interaction is active. It contains the
// queued-message strip, slash-command popup, and prompt input.
export function SessionComposerBox({
  displayedQueue,
  removeQueueItem,
  retryQueueItem,
  canRetryQueued,
  hasSlashCommands,
  showSlashPopup,
  slashPopupRef,
  filteredSlashCommands,
  slashPopupIndex,
  acceptSlashCommand,
  submitText,
  acceptsImages,
  submitLabel,
  draft,
  setDraft,
  handleKeyDown,
  canSend,
  isAgentRuntimeDeleted,
  isTerminated,
  lifecycleState,
  isStreaming,
  isSubmitted,
  hasPendingInteraction,
  permissionMode,
  permissionModes,
  showPermissionMode,
  canChangePermissionMode,
  modeSwitching,
  cyclePermissionMode,
  selectPermissionMode,
  isInterruptSettling,
  wrappedStopGeneration,
}: {
  displayedQueue: QueuedMessageItem[];
  removeQueueItem: (id: string) => void;
  retryQueueItem: (id: string) => void;
  canRetryQueued: boolean;
  hasSlashCommands: boolean;
  showSlashPopup: boolean;
  slashPopupRef: React.MutableRefObject<HTMLDivElement | null>;
  filteredSlashCommands: DisplaySlashCommand[];
  slashPopupIndex: number;
  acceptSlashCommand: (cmd: string) => void;
  submitText: (rawText: string, images?: TurnInputImage[]) => void;
  acceptsImages: boolean;
  submitLabel: string;
  draft: string;
  setDraft: (draft: string) => void;
  handleKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  canSend: boolean;
  isAgentRuntimeDeleted: boolean;
  isTerminated: boolean;
  lifecycleState: string;
  isStreaming: boolean;
  isSubmitted: boolean;
  hasPendingInteraction: boolean;
  permissionMode: PermissionMode;
  permissionModes: readonly PermissionMode[];
  showPermissionMode: boolean;
  canChangePermissionMode: boolean;
  modeSwitching: boolean;
  cyclePermissionMode: () => Promise<void>;
  selectPermissionMode: (mode: string) => Promise<void>;
  isInterruptSettling: boolean;
  wrappedStopGeneration: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const isBusy = isStreaming || isSubmitted;
  // The slash menu hangs off the composer box, and the composer box is not a
  // trigger: making it one would put button semantics around the textarea.
  // Base UI positions a popup against whatever element `Popover.Positioner`'s
  // `anchor` names (`@base-ui/react/popover`), so the wrapper below holds this
  // ref and stays a plain `<div>`. The parts are composed here rather than
  // through `components/ui/popover.tsx` because its `PopoverContent` owns the
  // positioner and forwards no `anchor`; once it does, this collapses back
  // onto the kit component.
  const composerAnchorRef = useRef<HTMLDivElement | null>(null);
  return (
    <ReadingColumn className="pb-4 pt-2">
      <ComposerQueue
        queuedMessages={displayedQueue}
        onRemoveQueuedMessage={removeQueueItem}
        onRetryQueuedMessage={retryQueueItem}
        canRetryQueuedMessages={canRetryQueued}
      />
      <PopoverPrimitive.Root open={showSlashPopup}>
        <div ref={composerAnchorRef} className="relative">
          <PromptInput
            className="rounded-xl border border-border bg-card shadow-none transition-colors duration-150 focus-within:border-primary/60 has-[textarea:focus]:border-primary/60"
            accept={acceptsImages ? COMPOSER_IMAGE_MEDIA_TYPES.join(',') : undefined}
            maxFiles={acceptsImages ? undefined : 0}
            onSubmit={(msg: PromptInputMessage) => {
              submitText(msg.text, acceptsImages ? turnInputImages(msg.files) : []);
            }}
          >
          <ComposerAttachments acceptsImages={acceptsImages} />
          <ComposerTextarea
            acceptsImages={acceptsImages}
            data-testid="composer-prompt"
            className="min-h-[52px] text-sm leading-relaxed placeholder:text-muted-foreground/70"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              canSend ? t('chat:composer.placeholder')
                : isAgentRuntimeDeleted ? t('chat:composer.placeholder_agent_terminated')
                : isTerminated ? t('chat:composer.placeholder_terminated')
                : lifecycleState === 'recovery' ? t('chat:composer.placeholder_recovery')
                : isStreaming ? t('chat:composer.placeholder_streaming') : ''
            }
            disabled={hasPendingInteraction || !canSend}
          />
          <PromptInputFooter className="items-center gap-2 px-2 py-1.5">
            <div className="flex min-w-0 items-center gap-2">
              {showPermissionMode && (
                <PermissionModeStripInline
                  permissionMode={permissionMode}
                  permissionModes={permissionModes}
                  canChange={canChangePermissionMode}
                  modeSwitching={modeSwitching}
                  onSelect={selectPermissionMode}
                />
              )}
              {canSend && hasSlashCommands && (
                <PromptInputButton
                  data-testid="composer-command-menu-trigger"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={(event) => {
                    setDraft('/');
                    const form = event.currentTarget.closest('form');
                    window.requestAnimationFrame(() => form?.querySelector('textarea')?.focus());
                  }}
                >
                  <span aria-hidden="true" className="font-mono text-sm font-semibold">/</span>
                  <span>{t('chat:composer.commands')}</span>
                </PromptInputButton>
              )}
              {canSend && (
                <span className="hidden text-11 text-muted-foreground/70 sm:inline">
                  {t('chat:composer.shortcut_hint')}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <ComposerSubmit
                data-testid="composer-submit"
                aria-label={submitLabel}
                title={submitLabel}
                className="size-8 rounded-lg bg-primary text-primary-foreground hover:bg-astra-2 disabled:bg-secondary disabled:text-muted-foreground"
                hasText={Boolean(draft.trim())}
                acceptsImages={acceptsImages}
                blocked={!canSend || hasPendingInteraction || isInterruptSettling}
                status="ready"
              />
              {isBusy && (
                <StopGenerationButton
                  data-testid="run-composer-stop"
                  className="size-8 rounded-lg"
                  disabled={isInterruptSettling || !isStreaming}
                  status={isSubmitted ? 'submitted' : 'streaming'}
                  isStopping={isInterruptSettling}
                  onStop={() => { void wrappedStopGeneration(); }}
                  variant="secondary"
                />
              )}
            </div>
          </PromptInputFooter>
          </PromptInput>
        </div>
        <PopoverPrimitive.Portal>
          <PopoverPrimitive.Positioner
            anchor={composerAnchorRef}
            side="top"
            align="start"
            sideOffset={8}
            collisionPadding={8}
            className="isolate z-50"
          >
            {/* The reader is typing the command in the textarea, so the menu
                takes focus at neither end: `initialFocus` on open,
                `finalFocus` on close. The positioner above publishes
                `--anchor-width`, which inherits into the popup. */}
            <PopoverPrimitive.Popup
              ref={slashPopupRef}
              data-testid="slash-command-menu"
              initialFocus={false}
              finalFocus={false}
              className="z-50 max-h-[min(34rem,60vh)] w-(--anchor-width) min-w-0 overflow-y-auto overscroll-contain rounded-lg border border-border bg-popover text-popover-foreground shadow-lg outline-hidden ring-1 ring-foreground/10 data-open:animate-in data-open:fade-in-0 data-closed:animate-out data-closed:fade-out-0"
            >
              {filteredSlashCommands.map((cmd, i) => (
                <div
                  key={cmd.name}
                  data-testid="slash-command-option"
                  data-command-name={cmd.name}
                  data-slash-active={i === slashPopupIndex ? 'true' : undefined}
                  title={cmd.description ? `${cmd.name} ${cmd.description}` : cmd.name}
                  className={`flex cursor-pointer flex-col gap-1 px-3 py-2.5 text-sm transition-colors ${i === slashPopupIndex ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/50'}`}
                  onMouseDown={(e) => { e.preventDefault(); acceptSlashCommand(cmd.name); }}
                >
                  <span data-testid="slash-command-name" className="block min-w-0 truncate font-mono text-xs font-semibold text-foreground">{cmd.name}</span>
                  {cmd.description && (
                    <span data-testid="slash-command-description" className="block min-w-0 overflow-hidden text-xs leading-5 text-muted-foreground [display:-webkit-box] [-webkit-box-orient:vertical] [-webkit-line-clamp:3]">
                      {cmd.description}
                    </span>
                  )}
                </div>
              ))}
            </PopoverPrimitive.Popup>
          </PopoverPrimitive.Positioner>
        </PopoverPrimitive.Portal>
      </PopoverPrimitive.Root>
    </ReadingColumn>
  );
}
