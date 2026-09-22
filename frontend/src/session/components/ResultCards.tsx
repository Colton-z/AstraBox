import React from 'react';
import { useTranslation } from 'react-i18next';
import { ErrorNote } from '@/components/shell';

// Maps the SDK stop_reason to an i18n key; '' means "normal stop, show nothing".
const STOP_REASON_LABEL_KEYS: Record<string, string> = {
  end_turn: '',
  tool_use: '',
  max_tokens: 'chat:result.stop_reason.max_tokens',
  stop_sequence: '',
  rate_limit: 'chat:result.stop_reason.rate_limit',
  billing_error: 'chat:result.stop_reason.billing_error',
  authentication_failed: 'chat:result.stop_reason.authentication_failed',
  server_error: 'chat:result.stop_reason.server_error',
};

export function ResultPartCard({ data }: { data: Record<string, unknown> }) {
  const { t } = useTranslation();
  const parts: string[] = [];
  if (typeof data.duration_ms === 'number') {
    parts.push(`${(data.duration_ms / 1000).toFixed(1)}s`);
  }
  if (typeof data.total_cost_usd === 'number') {
    parts.push(`$${data.total_cost_usd.toFixed(4)}`);
  }
  const usage = data.usage as Record<string, number> | undefined;
  if (usage) {
    const tokens: string[] = [];
    if (usage.input_tokens) tokens.push(t('chat:result.input_tokens', { count: usage.input_tokens }));
    if (usage.output_tokens) tokens.push(t('chat:result.output_tokens', { count: usage.output_tokens }));
    if (tokens.length) parts.push(tokens.join(' / '));
  }
  if (typeof data.num_turns === 'number') {
    parts.push(t('chat:result.turns', { count: data.num_turns }));
  }
  const stopKey = typeof data.stop_reason === 'string' ? (STOP_REASON_LABEL_KEYS[data.stop_reason] ?? '') : '';
  const stopLabel = stopKey ? t(stopKey) : '';
  const isAbnormal = !!stopLabel;

  if (parts.length === 0 && !isAbnormal) return null;

  // A line, not a card. What closes a turn is its meter reading — four values
  // a reader does not act on — and a card is the heaviest container here: it
  // would give the reading the same weight as the tool call above it, in a box
  // that looks like the ones a reader can open but this one cannot be.
  //
  // An abnormal stop is the exception, because then the line is news: it keeps
  // a border so the turn that ended badly does not read like one that did not.
  return (
    <div
      className={
        isAbnormal
          ? 'flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-citrine/50 px-3 py-2 text-xs text-muted-foreground'
          : 'flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground'
      }
    >
      {isAbnormal && <span className="font-medium text-citrine-fg">{stopLabel}</span>}
      {parts.map((p, i) => (
        <span key={i} className="inline-flex items-center gap-1">
          {i > 0 && <span className="text-border">·</span>}
          {p}
        </span>
      ))}
    </div>
  );
}

export function TurnFailureCard({ error }: { error?: string }) {
  const { t } = useTranslation();
  return (
    <ErrorNote className="flex items-start gap-2">
      <span className="shrink-0">❌</span>
      <span>{error ? t('chat:result.turn_failed_with_detail', { error }) : t('chat:result.turn_failed')}</span>
    </ErrorNote>
  );
}

/**
 * The engine's retry ladder, while it is running.
 *
 * A model endpoint that refuses the request does not fail the turn: the CLI
 * retries it ten times with exponential backoff, announcing each attempt as
 * `system/api_retry`, and only then gives up. That ladder takes about three
 * minutes. Without this note, a deployment whose gateway rejects the
 * credential shows a spinner and nothing else for those three minutes, and a
 * refused request is indistinguishable from a slow answer.
 *
 * The status and the error name are printed as the engine spelled them. A
 * deployment points at whatever gateway it likes, so restating `401` /
 * `authentication_failed` in a platform-defined vocabulary would rename the
 * engine's verdict.
 */
export interface ApiRetryData {
  attempt?: number;
  max_retries?: number;
  error_status?: number;
  error?: string;
}

export function ApiRetryNote({ payload }: { payload: ApiRetryData }) {
  const { t } = useTranslation();
  const attempt = payload.attempt ?? null;
  const max = payload.max_retries ?? null;
  const quoted = [
    payload.error_status === undefined ? '' : String(payload.error_status),
    payload.error ?? '',
  ].filter(Boolean).join(' ');

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-citrine/50 px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-citrine-fg">
        {attempt !== null && max !== null
          ? t('chat:result.api_retry_attempt', { attempt, max })
          : t('chat:result.api_retry')}
      </span>
      {quoted && <span data-slot="verbatim" className="font-mono">{quoted}</span>}
    </div>
  );
}
