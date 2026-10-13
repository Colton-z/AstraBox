import type { UserAction } from '../types';

export function readUserAction(value: unknown): UserAction | null {
  if (!value || typeof value !== 'object') return null;
  const { message, status, url } = value as Record<string, unknown>;
  if (typeof message !== 'string' || !message.trim() || message.length > 500 || /[\x00-\x1f]/.test(message)) return null;
  if (typeof status !== 'string' || !status || status.length > 64 || /[\x00-\x20]/.test(status)) return null;
  if (url != null) {
    if (typeof url !== 'string' || url.length > 2000 || /[\x00-\x20]/.test(url)) return null;
    try {
      if (!['http:', 'https:'].includes(new URL(url).protocol)) return null;
    } catch { return null; }
  }
  return { message, status, url: url == null ? null : url as string };
}
