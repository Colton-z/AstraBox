export function isTransientHistoryLoadError(message: string | null | undefined): boolean {
  const normalized = String(message ?? '').trim().toLowerCase();
  return (
    normalized.includes('mongodb timeout/unavailable')
    || normalized.includes('failed to fetch')
    || normalized.includes('load failed')
  );
}

export function shouldShowHistoryBlockingLoader({
  historyLoading,
  lifecycleState,
  messageCount,
  historyLoadedOnce,
  historyError,
}: {
  historyLoading: boolean;
  lifecycleState: string;
  messageCount: number;
  historyLoadedOnce: boolean;
  historyError?: string | null;
}): boolean {
  if (lifecycleState === 'creating') {
    return false;
  }
  if (historyLoadedOnce) {
    return false;
  }
  if (historyLoading) {
    return messageCount <= 0;
  }
  return messageCount <= 0 && isTransientHistoryLoadError(historyError);
}

export function shouldShowInitialHistoryLoadError({
  historyLoading,
  lifecycleState,
  messageCount,
  historyLoadedOnce,
  historyError,
}: {
  historyLoading: boolean;
  lifecycleState: string;
  messageCount: number;
  historyLoadedOnce: boolean;
  historyError?: string | null;
}): boolean {
  if (lifecycleState === 'creating') {
    return false;
  }
  if (historyLoadedOnce || historyLoading || messageCount > 0) {
    return false;
  }
  return Boolean(String(historyError ?? '').trim()) && !isTransientHistoryLoadError(historyError);
}
