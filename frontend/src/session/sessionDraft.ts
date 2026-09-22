const SESSION_DRAFT_KEY_PREFIX = 'astrabox:session-draft:';

function sessionDraftKey(sessionId: string): string {
  return `${SESSION_DRAFT_KEY_PREFIX}${encodeURIComponent(sessionId)}`;
}

export function readSessionDraft(sessionId: string): string {
  return window.sessionStorage.getItem(sessionDraftKey(sessionId)) ?? '';
}

export function writeSessionDraft(sessionId: string, draft: string): void {
  if (!draft) {
    clearSessionDraft(sessionId);
    return;
  }
  window.sessionStorage.setItem(sessionDraftKey(sessionId), draft);
}

export function clearSessionDraft(sessionId: string): void {
  window.sessionStorage.removeItem(sessionDraftKey(sessionId));
}
