// Read helpers for the management-side Sandbox pages.
//
// The governing rule for this resource: a sandbox row is the backend's report.
// Every value the control plane sends is rendered as it sent it — including a
// state this console has never seen. Nothing here relabels, defaults, or fills
// in a value: an operator on this page is asking what the backend says, and a
// normalized value would answer a different question.
import type { AdminSandboxSummary } from '@/types';
import type { PillTone } from './console';

export { shortId } from './sessionConfig';

/**
 * Backend state → pill tone only. The label is always the backend's own string
 * (see {@link sandboxStateLabel}); this maps the states that have a known
 * place in the lifecycle to a color and leaves everything else neutral
 * rather than guessing that an unrecognized state is healthy or broken.
 *
 * The vocabulary is the OpenSandbox/Kubernetes one its control plane reports
 * (Pending / Running / Succeeded / Failed / Unknown), matched case-insensitively
 * because a state string is not a contract.
 */
export function sandboxStateTone(state?: string): PillTone {
  const s = String(state || '').trim().toUpperCase();
  if (s === 'RUNNING') return 'running';
  if (['PENDING', 'CREATING', 'STARTING'].includes(s)) return 'pending';
  if (['FAILED', 'ERROR', 'EVICTED'].includes(s)) return 'failed';
  if (['SUCCEEDED', 'COMPLETED'].includes(s)) return 'done';
  return 'idle';
}

/** The backend's own state string, never translated. Empty → an em-dash. */
export function sandboxStateLabel(state?: string): string {
  return String(state || '').trim() || '—';
}

/** States that should breathe in the list (a box actively serving a turn). */
export function sandboxStateIsLive(state?: string): boolean {
  return String(state || '').trim().toUpperCase() === 'RUNNING';
}

/**
 * The image the box reports. Some control planes answer `unknown` for a box
 * they did not build from an image reference (a pooled Pod is created from a
 * template) — that string is passed through as-is, because it is what the
 * backend knows.
 */
export function sandboxImage(row: AdminSandboxSummary): string {
  return String(row.image || '').trim() || '—';
}

/** Free-text search over the fields an operator would recognize a box by. */
export function sandboxMatches(row: AdminSandboxSummary, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [row.sandbox_id, row.session_id, row.image, row.state]
    .map((v) => String(v || '').toLowerCase())
    .some((v) => v.includes(q));
}
