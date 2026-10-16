export function isTransientHistoryLoadError(message: string | null | undefined): boolean {
  const normalized = String(message ?? '').trim().toLowerCase();
  return (
    normalized.includes('persistence_unavailable')
    || normalized.includes('failed to fetch')
    || normalized.includes('load failed')
  );
}
