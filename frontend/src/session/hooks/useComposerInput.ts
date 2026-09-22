import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { UIMessage as SDKUIMessage } from 'ai';
import { normalizeSessionSlashCommands } from '../slashCommands';
import { createClientMessageId } from '../utils/chatHelpers';
import type { TurnInputImage } from '../composerAttachments';
import { readSessionDraft, writeSessionDraft } from '../sessionDraft';

// Composer input state covers the draft, slash-command popup, arrow-key history
// recall, and permission-mode cycle shortcut. `handleKeyDown` coordinates popup
// navigation, history recall, submit-on-Enter, and mode cycling, so the related
// state stays in one hook.
export function useComposerInput({
  sessionId,
  messages,
  slashCommandDetails,
  canSend,
  sendClientMessageNow,
  canChangePermissionMode,
  cyclePermissionMode,
}: {
  sessionId: string;
  messages: SDKUIMessage[];
  slashCommandDetails: unknown;
  canSend: boolean;
  sendClientMessageNow: (clientMessageId: string, text: string, images?: TurnInputImage[]) => void;
  canChangePermissionMode: boolean;
  cyclePermissionMode: () => Promise<void>;
}) {
  // ── Slash commands ───────────────────────────────────────────
  const [draft, setDraftState] = useState(() => readSessionDraft(sessionId));
  const setDraft = useCallback((nextDraft: string) => {
    writeSessionDraft(sessionId, nextDraft);
    setDraftState(nextDraft);
  }, [sessionId]);
  const slashCommands = useMemo(() => {
    return normalizeSessionSlashCommands(slashCommandDetails);
  }, [slashCommandDetails]);
  const [slashPopupIndex, setSlashPopupIndex] = useState(0);
  const slashPopupRef = useRef<HTMLDivElement | null>(null);
  const isSlashMode = draft.startsWith('/') && !draft.includes(' ') && !draft.includes('\n');
  const filteredSlashCommands = isSlashMode ? slashCommands.filter((c) => c.name.toLowerCase().startsWith(draft.toLowerCase())) : [];
  const showSlashPopup = isSlashMode && filteredSlashCommands.length > 0;
  useEffect(() => { setSlashPopupIndex(0); }, [draft]);
  useEffect(() => {
    if (!showSlashPopup) return;
    const activeItem = slashPopupRef.current?.querySelector<HTMLElement>('[data-slash-active="true"]');
    activeItem?.scrollIntoView({ block: 'nearest' });
  }, [showSlashPopup, slashPopupIndex]);

  // ── Input history ────────────────────────────────────────────
  const userMessageTexts = useMemo(
    () => messages.filter((m) => m.role === 'user').map((m) => m.parts.filter((p): p is { type: 'text'; text: string } => p.type === 'text').map((p) => p.text).join('')).filter(Boolean),
    [messages],
  );
  const historyIndexRef = useRef(-1);
  const historyDraftRef = useRef('');

  const submitText = useCallback((rawText: string, images: TurnInputImage[] = []) => {
    const text = rawText.trim();
    // An image is a message on its own, so emptiness is judged on the whole
    // input rather than on its prose.
    if ((!text && images.length === 0) || !canSend) return;
    const clientMessageId = createClientMessageId();
    setDraft('');
    sendClientMessageNow(clientMessageId, text, images);
  }, [canSend, sendClientMessageNow, setDraft]);

  const acceptSlashCommand = useCallback((cmd: string) => {
    const commandText = String(cmd || '').trim();
    if (!commandText) return;
    setDraft(`${commandText} `);
  }, [setDraft]);

  // Enter submits the form rather than the draft. The form is what knows the
  // attachments; sending the text straight from here would post the words
  // and drop the picture beside them, and only when the keyboard was used.
  const handleSend = useCallback((textarea: HTMLTextAreaElement) => {
    textarea.closest('form')?.requestSubmit();
  }, []);

  // ── Keyboard handler ─────────────────────────────────────────
  const isComposingRef = useRef(false);
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.nativeEvent.isComposing || isComposingRef.current) return;
      if (showSlashPopup) {
        if (e.key === 'ArrowDown') { e.preventDefault(); setSlashPopupIndex((v) => (v + 1) % filteredSlashCommands.length); return; }
        if (e.key === 'ArrowUp') { e.preventDefault(); setSlashPopupIndex((v) => (v - 1 + filteredSlashCommands.length) % filteredSlashCommands.length); return; }
        if (e.key === 'Tab' && !e.shiftKey) { e.preventDefault(); acceptSlashCommand(filteredSlashCommands[slashPopupIndex].name); return; }
        if (e.key === 'Escape') { e.preventDefault(); setDraft(''); return; }
      }
      if (e.key === 'Tab' && e.shiftKey && canChangePermissionMode) { e.preventDefault(); e.stopPropagation(); void cyclePermissionMode(); return; }
      if (e.key === 'ArrowUp' && !draft.includes('\n')) {
        const ta = e.currentTarget;
        if (ta.selectionStart === 0) {
          e.preventDefault();
          if (historyIndexRef.current === -1) historyDraftRef.current = draft;
          const nextIdx = Math.min(historyIndexRef.current + 1, userMessageTexts.length - 1);
          if (nextIdx >= 0 && nextIdx < userMessageTexts.length) { historyIndexRef.current = nextIdx; setDraft(userMessageTexts[userMessageTexts.length - 1 - nextIdx]); }
          return;
        }
      }
      if (e.key === 'ArrowDown' && !draft.includes('\n')) {
        const ta = e.currentTarget;
        if (ta.selectionStart === draft.length) {
          e.preventDefault();
          if (historyIndexRef.current > 0) { historyIndexRef.current -= 1; setDraft(userMessageTexts[userMessageTexts.length - 1 - historyIndexRef.current]); }
          else if (historyIndexRef.current === 0) { historyIndexRef.current = -1; setDraft(historyDraftRef.current); }
          return;
        }
      }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (isSlashMode) {
          const selected = showSlashPopup
            ? filteredSlashCommands[slashPopupIndex]
            : slashCommands.find((c) => c.name.toLowerCase() === draft.trim().toLowerCase());
          if (selected) { acceptSlashCommand(selected.name); return; }
        }
        handleSend(e.currentTarget);
        historyIndexRef.current = -1;
      }
    },
    [draft, setDraft, showSlashPopup, filteredSlashCommands, slashPopupIndex, isSlashMode, slashCommands, acceptSlashCommand, canChangePermissionMode, cyclePermissionMode, userMessageTexts, handleSend],
  );

  return {
    draft,
    setDraft,
    hasSlashCommands: slashCommands.length > 0,
    showSlashPopup,
    filteredSlashCommands,
    slashPopupIndex,
    slashPopupRef,
    acceptSlashCommand,
    handleKeyDown,
    submitText,
  };
}
