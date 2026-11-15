import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { KeyRound, Settings } from 'lucide-react';

import { AppTopbar, EmptyState, ErrorNote, PageShell } from '@/components/shell';
import { StatusPill } from '@/components/AstraConsole';
import { Button, buttonVariants } from '@/components/ui/button';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import { resolveLanguage } from '@/i18n';
import {
  assistantDisplayName,
  assistantMissingSecrets,
  assistantStateIsLive,
  assistantStateLabel,
  assistantStateTone,
} from '@/manage/assistantConfig';
import { ApiError, MODULE_BASE } from '../api';

import { getAssistant, getAssistantPanel, wakeAssistantWorkspace } from './api';
import type { AssistantPanel, AssistantRecord } from './types';

/** How long before `expires_at` the page asks for the next address. */
const RENEW_BEFORE_EXPIRY_MS = 60_000;
/**
 * The shortest wait between two renewals. `expires_at` is read against this
 * machine's clock, and a clock far enough off would otherwise put every
 * renewal in the past and reload the panel without pause.
 */
const MIN_RENEW_DELAY_MS = 15_000;
/** Past a signed 32-bit millisecond count, `setTimeout` fires at once. */
const MAX_TIMER_MS = 2_147_483_647;

function renewDelay(expiresAt: number): number {
  const remaining = expiresAt - Date.now();
  // An address shorter-lived than twice the margin is renewed halfway instead.
  const delay = remaining > 2 * RENEW_BEFORE_EXPIRY_MS ? remaining - RENEW_BEFORE_EXPIRY_MS : remaining / 2;
  return Math.min(Math.max(delay, MIN_RENEW_DELAY_MS), MAX_TIMER_MS);
}

/** How long a runtime may take to restart on its box before the page reports it. */
const PANEL_RESTART_WAIT_MS = 120_000;
const PANEL_NOT_READY_RETRY_MS = 3_000;

function errorCode(error: unknown): string {
  return error instanceof ApiError ? error.code : '';
}

/** Whether sending the same request again can succeed, as the refusal says. */
function retryable(error: unknown): boolean {
  return !(error instanceof ApiError) || error.retryable !== false;
}

/**
 * The runtime panel's signed address for one sandbox, renewed before it expires.
 *
 * Every address is a new hostname whose query logs the runtime in again, so a
 * renewal reloads the panel; it is fetched by hand rather than through SWR,
 * whose focus revalidation would reload it on every return to the tab. A timer
 * does not run while the machine sleeps, so a page that becomes visible after
 * its renewal time renews at once. A failed renewal keeps the address it has:
 * it routes until it expires.
 *
 * A runtime restarting on its box (after its configuration changed) answers
 * that it is not ready yet; that is asked again every few seconds and reported
 * only once it has lasted longer than a restart takes.
 */
function usePanelAddress(assistantId: string, sandboxId: string, retryKey: number) {
  const [opened, setOpened] = useState<{ sandboxId: string; panel: AssistantPanel } | null>(null);
  const [failure, setFailure] = useState<{ sandboxId: string; error: unknown } | null>(null);

  useEffect(() => {
    if (!sandboxId) return undefined;
    let cancelled = false;
    let timer: number | undefined;
    let renewAt = Number.POSITIVE_INFINITY;
    let notReadySince = 0;
    // One request at a time: a return to the tab while a renewal is in flight
    // would otherwise start a second one, and with it a second renewal timer.
    let inFlight = false;
    const open = async () => {
      if (inFlight) return;
      inFlight = true;
      window.clearTimeout(timer);
      try {
        const panel = await getAssistantPanel(assistantId);
        if (cancelled) return;
        setOpened({ sandboxId, panel });
        setFailure(null);
        notReadySince = 0;
        renewAt = Number.POSITIVE_INFINITY;
        if (panel.expires_at) {
          const delay = renewDelay(Date.parse(panel.expires_at));
          renewAt = Date.now() + delay;
          timer = window.setTimeout(() => { void open(); }, delay);
        }
      } catch (error) {
        if (cancelled) return;
        if (errorCode(error) === 'ASSISTANT_WORKSPACE_NOT_READY') {
          notReadySince = notReadySince || Date.now();
          if (Date.now() - notReadySince < PANEL_RESTART_WAIT_MS) {
            timer = window.setTimeout(() => { void open(); }, PANEL_NOT_READY_RETRY_MS);
            return;
          }
        }
        setFailure({ sandboxId, error });
      } finally {
        inFlight = false;
      }
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible' && Date.now() >= renewAt) void open();
    };
    document.addEventListener('visibilitychange', onVisible);
    void open();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [assistantId, sandboxId, retryKey]);

  return {
    panel: opened?.sandboxId === sandboxId ? opened.panel : null,
    error: failure?.sandboxId === sandboxId ? failure.error : null,
  };
}

/**
 * Keep the panel in the console's language.
 *
 * The runtime keeps its own language setting, so the console tells the panel's
 * page which one to show: when that page announces it is ready (on every load,
 * a renewal included) and whenever the console's language changes. Messages go
 * to, and are accepted from, the panel's own origin only.
 */
function usePanelLanguage(frame: React.RefObject<HTMLIFrameElement | null>, url: string | undefined, language: string) {
  useEffect(() => {
    if (!url) return undefined;
    const origin = new URL(url).origin;
    const send = () => {
      frame.current?.contentWindow?.postMessage({ type: 'astrabox.locale', locale: language }, origin);
    };
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin) return;
      if ((event.data as { type?: unknown } | null)?.type === 'astrabox.panel-ready') send();
    };
    window.addEventListener('message', onMessage);
    send();
    return () => window.removeEventListener('message', onMessage);
  }, [frame, url, language]);
}

/**
 * Open, in the console, the conversation the panel names.
 *
 * The bundle's panel shows the Agent conversations that work its Tickets and
 * asks for one with `dsh-superagent.open-conversation` and the conversation's
 * page, a path of this console. A message is taken only from the panel's own
 * frame and origin, and only for a conversation's page.
 */
function usePanelConversations(frame: React.RefObject<HTMLIFrameElement | null>, url: string | undefined) {
  const navigate = useNavigate();
  useEffect(() => {
    if (!url) return undefined;
    const origin = new URL(url).origin;
    const onMessage = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow || event.origin !== origin) return;
      const data = event.data as { type?: unknown; page?: unknown } | null;
      if (data?.type !== 'dsh-superagent.open-conversation' || typeof data.page !== 'string') return;
      const page = data.page.startsWith(`${MODULE_BASE}/`) ? data.page.slice(MODULE_BASE.length) : '';
      if (/^\/sessions\/[A-Za-z0-9-]+$/.test(page)) navigate(page);
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [frame, url, navigate]);
}

type Attempt =
  | { status: 'starting' }
  | { status: 'woken' }
  | { status: 'refused'; error: unknown };

/**
 * An Assistant whose runtime hosts no conversations, opened on the runtime's
 * own page.
 *
 * Opening it wakes its workspace — the server answers at once and builds in the
 * background — then follows the Assistant's read while the workspace is
 * MATERIALIZING, showing the state as the server names it. On READY it asks for
 * the panel's signed address and shows it full height. A start that cannot
 * succeed is not offered: while a credential the runtime declares is missing
 * the page sends the owner to the Assistant's settings, where it is set. A
 * workspace that leaves MATERIALIZING for anything but READY did not start;
 * the read does not say why, so the page says that much and offers the start
 * again rather than retrying on its own.
 */
export function AssistantPanelView({ initial }: { initial: AssistantRecord }) {
  const { t, i18n } = useTranslation();
  const assistantId = initial.assistant_id;
  const [record, setRecord] = useState(initial);
  const [readError, setReadError] = useState('');
  const [attempt, setAttempt] = useState<Attempt>({ status: 'starting' });
  const [attemptKey, setAttemptKey] = useState(0);
  const [panelKey, setPanelKey] = useState(0);

  // Reads overlap — the read after a wake, the follow cadence, a return to the
  // tab — so an answer older than one already shown is dropped: arriving last,
  // it would put a workspace that is already starting back to its state before
  // the wake, where nothing follows it.
  const issued = useRef(0);
  const shown = useRef(0);
  /**
   * Read the Assistant and show the answer. Throws on failure, unless a newer
   * answer is already shown, which makes both this answer and its failure moot.
   */
  const read = useCallback(async () => {
    const ticket = ++issued.current;
    let next: AssistantRecord;
    try {
      next = await getAssistant(assistantId);
    } catch (cause) {
      if (ticket < shown.current) return;
      throw cause;
    }
    if (ticket < shown.current) return;
    shown.current = ticket;
    setRecord(next);
    setReadError('');
  }, [assistantId]);
  const load = useCallback(async (context?: ReloadContext) => {
    try {
      await read();
    } catch (cause) {
      if (keepsLastRead(cause, context, true)) return;
      setReadError((cause as Error).message);
    }
  }, [read]);

  useEffect(() => {
    void load();
  }, [load]);

  const missing = assistantMissingSecrets(record);
  const canStart = missing.length === 0;
  useEffect(() => {
    if (!canStart) return undefined;
    let cancelled = false;
    setAttempt({ status: 'starting' });
    void (async () => {
      try {
        await wakeAssistantWorkspace(assistantId);
        // What the wake did is judged from the read after it: the state before
        // it would call a workspace that is starting one that did not start.
        await read();
      } catch (error) {
        if (!cancelled) setAttempt({ status: 'refused', error });
        return;
      }
      if (!cancelled) setAttempt({ status: 'woken' });
    })();
    return () => { cancelled = true; };
  }, [assistantId, attemptKey, canStart, read]);

  const state = String(record.workspace_state || 'NOT_MATERIALIZED');
  const woken = attempt.status === 'woken';
  useKeepCurrent(load, { follow: woken && state === 'MATERIALIZING' });

  const sandboxId = woken && state === 'READY' ? String(record.current_sandbox_id || '') : '';
  const { panel, error: panelError } = usePanelAddress(assistantId, sandboxId, panelKey);
  const frame = useRef<HTMLIFrameElement>(null);
  usePanelLanguage(frame, panel?.url, resolveLanguage(i18n.resolvedLanguage));
  usePanelConversations(frame, panel?.url);
  // The address names the sandbox the read last called READY; a refusal
  // saying the workspace is not ready means the read is out of date.
  const panelNotReady = errorCode(panelError) === 'ASSISTANT_WORKSPACE_NOT_READY';
  useEffect(() => {
    if (panelNotReady) void load();
  }, [panelNotReady, load]);

  const name = assistantDisplayName(record);
  const settingsPath = `/manage/assistants/${encodeURIComponent(assistantId)}`;
  const startAgain = () => setAttemptKey((key) => key + 1);
  // Not ready even after a fresh read: start it again rather than only asking
  // for another address for the same sandbox.
  const retryPanel = panelNotReady ? startAgain : () => setPanelKey((key) => key + 1);
  const retryButton = (onClick: () => void) => (
    <Button variant="outline" size="sm" onClick={onClick}>{t('common:retry')}</Button>
  );

  const secretNotice = () => {
    // A refusal can name a credential the read still reports as set (the
    // stored value is gone); every declared one is listed then.
    const declared = record.runtime?.owner_secrets ?? [];
    const labels = (missing.length > 0 ? missing : declared).map((secret) => secret.label);
    return (
      <EmptyState
        icon={<KeyRound className="size-5" />}
        title={t('shell:assistant_panel.secret_required', { name, count: Math.max(labels.length, 1) })}
        hint={t('shell:assistant_panel.secret_required_hint', {
          labels: new Intl.ListFormat(i18n.resolvedLanguage, { type: 'conjunction' }).format(labels),
        })}
        action={
          <Link to={settingsPath} className={buttonVariants({ size: 'sm' })}>
            {t('shell:assistant_panel.open_settings')}
          </Link>
        }
      />
    );
  };

  const refusal = (title: string, error: unknown, retry: () => void) => {
    return (
      <ErrorNote title={title}>
        <p>{(error as Error).message}</p>
        {retryable(error) && retryButton(retry)}
      </ErrorNote>
    );
  };

  const waiting = (
    <div role="status" className="flex flex-col items-start gap-3">
      <StatusPill tone={assistantStateTone(state)} live={assistantStateIsLive(state)} state={state}>
        {assistantStateLabel(state)}
      </StatusPill>
      <p className="t-copy text-muted-foreground">{t('shell:assistant_panel.opening', { name })}</p>
    </div>
  );

  let notice: React.ReactNode = null;
  if (!canStart) {
    notice = secretNotice();
  } else if (attempt.status === 'refused') {
    notice = refusal(t('shell:assistant_panel.start_failed', { name }), attempt.error, startAgain);
  } else if (!woken || state === 'MATERIALIZING') {
    notice = waiting;
  } else if (state !== 'READY') {
    // The server says why the last start failed: an action only the owner can
    // take (a credential to set or replace), or the error it ended with.
    const action = record.workspace_action;
    const ownerSecret = action?.status === 'owner_secret_required' || action?.status === 'owner_secret_invalid';
    notice = (
      <ErrorNote title={t('shell:assistant_panel.stopped', { name })}>
        <p>{action?.message || record.workspace_error || t('shell:assistant_panel.stopped_hint')}</p>
        {ownerSecret ? (
          <Link to={settingsPath} className={buttonVariants({ size: 'sm' })}>
            {t('shell:assistant_panel.open_settings')}
          </Link>
        ) : (
          retryButton(startAgain)
        )}
      </ErrorNote>
    );
  } else if (!panel && panelError) {
    const code = errorCode(panelError);
    const title = code === 'ASSISTANT_PANEL_ROUTE_UNSUPPORTED'
      ? t('shell:assistant_panel.route_unsupported', { name })
      : code === 'ASSISTANT_PANEL_UNSUPPORTED'
        ? t('shell:assistant_panel.panel_unsupported', { name })
        : t('shell:assistant_panel.open_failed', { name });
    notice = refusal(title, panelError, retryPanel);
  } else if (!panel) {
    notice = waiting;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <AppTopbar
        crumbs={[{ label: name }]}
        trailLabel={t('shell:breadcrumb_navigation')}
        actions={
          <Link to={settingsPath} className={buttonVariants({ variant: 'outline', size: 'sm' })}>
            <Settings />
            {t('shell:settings')}
          </Link>
        }
      />
      <h1 className="sr-only">{name}</h1>
      {notice !== null || !panel ? (
        <PageShell measure="narrow">
          <div className="flex flex-1 flex-col gap-4">
            {readError && (
              <ErrorNote title={t('shell:assistant_panel.read_failed', { name })}>{readError}</ErrorNote>
            )}
            {notice}
          </div>
        </PageShell>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          {(panelError || readError) && (
            <div className="flex flex-col gap-2 border-b px-gutter py-3">
              {panelError ? (
                <ErrorNote title={t('shell:assistant_panel.renew_failed', { name })}>
                  <p>{(panelError as Error).message}</p>
                  {retryable(panelError) && retryButton(retryPanel)}
                </ErrorNote>
              ) : (
                <ErrorNote title={t('shell:assistant_panel.read_failed', { name })}>{readError}</ErrorNote>
              )}
            </div>
          )}
          <iframe
            ref={frame}
            title={name}
            src={panel.url}
            className="min-h-0 w-full flex-1 border-0 bg-background"
          />
        </div>
      )}
    </div>
  );
}
