import React, { useId } from 'react';
import type { SessionRecord } from '../types';
import {
  isTransparentlyRecoverableAgentSession,
  sessionOperationalStatusLabel,
} from '../utils/format';
import { Ellipsis } from '@/components/shell';

/**
 * Shared AstraBox console design atoms — the mark, the run-state StatusPill, and
 * the mono run-id treatment. These embody the design language (ink surface,
 * astra live-pulse, hairline pills) and are reused across the shell sidebar and
 * the run/chat header so the two surfaces speak the same vocabulary.
 */

export type PillTone = 'running' | 'done' | 'pending' | 'approval' | 'failed' | 'idle';

/**
 * The product mark — the same one the website ships
 * (`website/static/img/astrabox-mark.svg`), not a second drawing of it.
 *
 * A flat `currentColor` tile renders as a black square beside the brand's
 * indigo everywhere else, so the mark carries its own gradient: one mark, one
 * gradient, both surfaces.
 *
 * The star is knocked out of the tile rather than stroked over it — at these
 * sizes a stroked four-point star of this proportion closes up into a plus
 * sign. The tile is opaque, so one drawing serves light and dark.
 *
 * `useId` because the gradient and mask are referenced by id: two marks on one
 * page (sidebar + a header) with hardcoded ids would have the second silently
 * take the first's definitions.
 */
export function AstraMark({ size = 22, className }: { size?: number; className?: string }) {
  const uid = useId();
  const tile = `astra-tile-${uid}`;
  const star = `astra-star-${uid}`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={className}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={tile} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="color-mix(in oklab, var(--astra) 82%, #fff)" />
          <stop offset="1" stopColor="var(--astra-2)" />
        </linearGradient>
        <mask id={star}>
          <rect width="32" height="32" fill="#fff" />
          <path
            d="M16 6.2C16.55 11.4 20.6 15.45 25.8 16C20.6 16.55 16.55 20.6 16 25.8C15.45 20.6 11.4 16.55 6.2 16C11.4 15.45 15.45 11.4 16 6.2Z"
            fill="#000"
          />
        </mask>
      </defs>
      <rect width="32" height="32" rx="7.5" fill={`url(#${tile})`} mask={`url(#${star})`} />
    </svg>
  );
}

const TONE_CLASS: Record<PillTone, string> = {
  running: 'tone-running',
  done: 'tone-done',
  pending: 'tone-pending',
  approval: 'tone-approval',
  failed: 'tone-failed',
  idle: 'tone-idle',
};

// Stable design-token name per tone, exposed as `data-tone` so automated checks
// can assert the rendered run state without depending on localized label text.
const TONE_TOKEN: Record<PillTone, string> = {
  running: 'astra',
  done: 'mint',
  pending: 'citrine',
  approval: 'plasma',
  failed: 'crimson',
  idle: 'idle',
};

/**
 * A run-state chip — tinted bg + state-colored label + an optional dot (the dot
 * breathes with the astra pulse on the `running` tone). Sparingly colored.
 *
 * `state` is the raw run-state string (e.g. `PROCESSING`, `READY`); it is
 * surfaced as `data-state` alongside the token `data-tone` and a `data-pulse`
 * flag so the live/settled transition is observable without reading label text.
 */
export function StatusPill({
  tone,
  children,
  dot = true,
  className = '',
  state,
  live,
  truncate = false,
}: {
  tone: PillTone;
  /** The label. A node rather than a string so a caller can mix in mono fragments. */
  children: React.ReactNode;
  dot?: boolean;
  className?: string;
  state?: string;
  /** Force the breathing halo. Defaults on for `running`. */
  live?: boolean;
  /**
   * Let the label give way when the row is short of width. Off by default, so
   * a pill with room renders its label whole; on where the pill shares a row
   * with something that must not be pushed out of view. The dot and the tone
   * survive truncation, so the state is still legible without the word, and the
   * full text stays available on hover.
   */
  truncate?: boolean;
}) {
  const isLive = live ?? tone === 'running';
  return (
    <span
      data-testid="status-pill"
      data-state={state}
      data-tone={TONE_TOKEN[tone]}
      data-pulse={isLive ? 'true' : 'false'}
      className={`astra-pill ${TONE_CLASS[tone]} ${truncate ? 'min-w-0' : ''} ${className}`}
    >
      {dot && <span className={`astra-pill-dot${isLive ? ' astra-dot--live' : ''}`} aria-hidden />}
      {truncate
        ? (typeof children === 'string'
            ? <Ellipsis>{children}</Ellipsis>
            : <Ellipsis title={null}>{children}</Ellipsis>)
        : children}
    </span>
  );
}

/**
 * The state → tone map. One of these, taking the machine-readable state.
 *
 * Matching English words against the label — the translated string a user
 * sees — breaks in every other language: no pattern matches, so every row
 * falls through to `idle`. Six distinct states — 处理中, 等待回答, 后台运行中,
 * 创建中, 已终止, 待恢复 — then render in the same grey, and `idle` is the one
 * tone that carries no fill, so switching language silently erases the status
 * column while the theme is untouched.
 *
 * `session.state` is available at every call site, so nothing here needs to
 * derive presentation from presentation and make a display string load-bearing.
 */
export function toneForState(state?: string): PillTone {
  const s = String(state || '').toUpperCase();
  // READY is a settled conversation — dispatchable, nothing running. It reads
  // done (calm mint), never the breathing running pulse: the e2e suite asserts
  // `data-pulse=false` here as "no phantom generating tail".
  if (s === 'READY') return 'done';
  if (['BACKGROUND_RUNNING', 'PROCESSING', 'BUSY', 'SENDING', 'ACTIVE', 'RUNNING'].includes(s)) {
    return 'running';
  }
  if (['CREATING', 'PROVISIONING', 'STARTING', 'PENDING', 'WAITING_INPUT', 'INTERRUPTING', 'TERMINATING', 'HIBERNATING', 'IDLE', 'PAUSED'].includes(s)) {
    return 'pending';
  }
  if (['RECOVERY_REQUIRED', 'FAILED', 'ERROR'].includes(s)) return 'failed';
  return 'idle';
}

/** Derive a pill tone for a session row from its record (sidebar list use). */
export function toneForSession(s: SessionRecord): PillTone {
  // A session whose sandbox is gone but re-borrows on the next message reads
  // settled, not failed — the same exception the label makes.
  if (isTransparentlyRecoverableAgentSession(s)) return 'done';
  return toneForState(s.state);
}

/**
 * The mono run-id treatment: `Run · ab12cd34`, tabular, dimmed. When `live`, a
 * leading astra dot breathes. Used in the run header.
 *
 * The id is mono because it is one — it goes into a log search. The word in
 * front of it is not, and it is not shouted: small-caps chrome (`RUN` at
 * 0.14em) on the first line of the product's main surface would be the only
 * uppercase chrome anywhere in it.
 */
export function RunId({
  id,
  live = false,
  className = '',
}: {
  id: string;
  live?: boolean;
  className?: string;
}) {
  const short = id.replace(/-/g, '').slice(0, 8);
  return (
    <span
      className={`t-mono inline-flex items-center gap-2 text-11 text-muted-foreground ${className}`}
    >
      {live && <span className="astra-dot astra-dot--live" />}
      Run · {short}
    </span>
  );
}
