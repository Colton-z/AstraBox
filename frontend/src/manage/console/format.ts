// Locale-neutral, mono-friendly timestamp formatting for the whole manage console.
// The en-US `Date.toLocaleString()` ("6/23/2026, 8:09:47 AM") reads wrong in a
// Chinese operator console and isn't tabular; every manage list column + drawer
// renders dates through these two helpers so the surface is uniform:
//   formatDateTime         → YYYY-MM-DD HH:mm     (list columns, drawer timelines)
//   formatDateTimeSeconds  → YYYY-MM-DD HH:mm:ss  (drawers where the second matters)
// Both keep a fixed-width digit shape that lines up under the kit's `.console-val`
// mono face. Dates are rendered in the viewer's local timezone (operators reason
// about wall-clock), just without the locale's separators/AM-PM.

const pad = (n: number) => String(n).padStart(2, '0');

/** Parse to a valid Date, or null. Accepts ISO strings / epoch numbers. */
function toDate(v: string | number | null | undefined): Date | null {
  if (v == null || v === '') return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** `YYYY-MM-DD HH:mm` in local time. Invalid → the raw input; empty → em-dash. */
export function formatDateTime(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  const d = toDate(v);
  if (!d) return String(v);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `YYYY-MM-DD HH:mm:ss` in local time — for drawers where the second matters. */
export function formatDateTimeSeconds(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—';
  const d = toDate(v);
  if (!d) return String(v);
  return `${formatDateTime(v)}:${pad(d.getSeconds())}`;
}
