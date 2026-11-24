import { skillSourceLabel } from '@/types';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ChevronDown, Download, RefreshCw, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';

import { Button, buttonVariants } from '@/components/ui/button';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import {
  adminGetSessionDetail,
  adminGetSessionTrace,
  adminKillSession,
  buildAdminSessionTranscriptUrl,
} from '@/api';
import type { AdminSessionDetail } from '@/types';
import { localizeDisplayText } from '@/utils/format';

import { ErrorNote } from '@/components/shell';
import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleErrorState,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleFieldRow,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  formatDateTimeSeconds,
  useRecordCrumb,
} from './console';
import {
  formatDuration,
  sessionStateLabel,
  sessionStateTone,
  sessionUserDisplay,
  shortId,
} from './sessionConfig';
import { SessionConversationRecord } from './SessionConversationRecord';
import { TraceDigest } from './SessionTraceDigest';
import { ResponseParts } from './SessionResponseParts';
import { SessionRuntimeVersions } from './SessionRuntimeVersions';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

function joinList(values: string[] | null | undefined): string {
  return (values ?? []).filter(Boolean).join(', ');
}

/**
 * The Agent program's MCP config as text, or `''` when there is nothing to show.
 *
 * `template_mcp_config` is `unknown` because its shape belongs to the engine,
 * not to the console. An empty object is not a config — it is the absence of
 * one — and it must not produce a card holding `{}`.
 */
function formatMcpConfig(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'object' && Object.keys(value as object).length === 0) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    // A cycle cannot come off the wire, but a non-serializable value reaching
    // here must not blank the whole page.
    return String(value);
  }
}

// The lifecycle state, READY while a response runs too. These records disclose
// their read time; creation additionally follows through the polling hook.
const RESTING_SESSION_STATES = new Set(['READY', 'TERMINATED', 'DELETED']);

export default function SessionDetailPage() {
  const { sessionId = '' } = useParams();
  return <SessionDetailRecord key={sessionId} sessionId={sessionId} />;
}

function SessionDetailRecord({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [detail, setDetail] = useState<AdminSessionDetail | null>(null);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [conversationRevision, setConversationRevision] = useState(0);
  const [traceOpen, setTraceOpen] = useState(false);
  const {
    data: trace,
    error: traceError,
    isValidating: traceLoading,
    mutate: refreshTrace,
  } = useSWR(traceOpen ? ['admin-session-trace', sessionId] : null, () => adminGetSessionTrace(sessionId), {
    revalidateOnFocus: false,
    shouldRetryOnError: false,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [killing, setKilling] = useState(false);

  useRecordCrumb(detail ? sessionUserDisplay(detail) : t('common:session'));

  // True after any successful read of the detail (see keepsLastRead).
  const loaded = useRef(false);
  const readSequence = useRef(0);

  const load = useCallback(async (context?: ReloadContext) => {
    const sequence = ++readSequence.current;
    const startedAt = new Date().toISOString();
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const record = await adminGetSessionDetail(sessionId);
      if (sequence !== readSequence.current) return;
      setDetail(record);
      setReadAt(startedAt);
      setError('');
      loaded.current = true;
    } catch (failure) {
      if (sequence !== readSequence.current || keepsLastRead(failure, context, loaded.current)) return;
      setDetail(null);
      setError((failure as Error).message);
    } finally {
      if (sequence === readSequence.current) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load, {
    follow: !!detail && !RESTING_SESSION_STATES.has(String(detail.state).toUpperCase()),
  });

  if (loading && !detail) {
    return (
      <ConsoleRecordPage title={t('common:session')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!detail) {
    return (
      <ConsoleRecordPage title={t('common:session')}>
        <ConsoleErrorState
          title={t('manage:sessions.error_title')}
          detail={error}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  const killable = detail.state !== 'TERMINATED' && detail.state !== 'DELETED';
  const skills = joinList(detail.template_skills?.map(skillSourceLabel));
  const mcp = joinList(detail.template_mcp_servers);
  const mcpConfig = formatMcpConfig(detail.template_mcp_config);

  return (
    <ConsoleRecordPage
      title={sessionUserDisplay(detail)}
      lede={readAt ? t('manage:system.captured_at', { at: formatDateTimeSeconds(readAt) }) : undefined}
      status={{ tone: sessionStateTone(detail.state), label: sessionStateLabel(detail.state) }}
      actions={
        <>
          <Button
            variant="outline"
            size="icon"
            onClick={() => {
              void load();
              setConversationRevision((revision) => revision + 1);
              if (traceOpen) void refreshTrace();
            }}
            disabled={loading}
            aria-label={t('common:refresh')}
          >
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
          {/* The Claude Agent SDK transcript is distinct from the AstraBox
              session record: the file lands
              in ~/.claude/projects/ and `claude --resume` continues the
              conversation on the operator's machine. That is what the control
              is for, and why the name it downloads under is the SDK's session
              id rather than this page's.

              An anchor, not a fetch: the server names the file in its
              Content-Disposition and the browser takes the response straight
              to disk. It only borrows the button's shape through
              `buttonVariants` — `Button` (`@base-ui/react/button`) imposes
              button semantics on whatever it renders, and its documentation
              rules that out for an `<a>`, which has its own, including the
              `download` behaviour this one is here for.

              It sits here rather than in the app's conversation header because
              the two are different jobs: a user shares their own conversation
              (session-owner gated), and an agent's owner or an administrator
              exports it — which is the gate this whole page already stands
              behind. */}
          <a
            className={buttonVariants({ variant: 'outline' })}
            href={buildAdminSessionTranscriptUrl(detail.session_id)}
            download
          >
            <Download className="size-4" />
            {t('manage:sessions.export')}
          </a>
          {/* The question is the button's (`confirm`), and it takes the page
              with it: while the dialog is up, the export beside it is behind a
              modal and out of reach, so the row does not have to swap itself
              out to keep a second target from under the pointer. The reasoning
              lives on ConsoleDangerButton; this is the call site. */}
          <ConsoleDangerButton
            disabled={!killable || killing}
            confirm={{
              title: t('manage:sessions.kill_confirm', { id: shortId(detail.session_id) }),
              action: t('manage:sessions.kill_confirm_btn'),
              onConfirm: () => {
                setKilling(true);
                setActionError('');
                void adminKillSession(detail.session_id)
                  .then(() => navigate('/manage/sessions'))
                  .catch((e: unknown) => setActionError((e as Error).message))
                  .finally(() => setKilling(false));
              },
            }}
          >
            <Trash2 className="size-4" />
            {killing ? t('manage:sessions.killing') : t('manage:sessions.kill')}
          </ConsoleDangerButton>
        </>
      }
      rail={
        <ConsoleFactRail>
          <ConsoleFact
            label={t('common:sandbox')}
            value={
              detail.sandbox_id ? <span className="font-mono">{detail.sandbox_id}</span> : '—'
            }
          />
          <ConsoleFact
            label={t('manage:common_fields.duration')}
            value={formatDuration(detail.duration_seconds)}
          />
          <ConsoleFact
            label={t('manage:common_fields.local_runtime')}
            value={detail.has_local_runtime ? t('common:yes') : t('common:no')}
          />
          <ConsoleFact
            label={t('manage:runtime_versions.title')}
            value={<SessionRuntimeVersions versions={detail.runtime_versions} />}
          />
          <ConsoleFact
            label={t('manage:sessions.field_expires')}
            value={formatDateTimeSeconds(detail.expires_at) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      {actionError && <ErrorNote>{actionError}</ErrorNote>}

      <SessionConversationRecord
        key={conversationRevision}
        sessionId={detail.session_id}
        conversationState={detail.conversation_state}
      />

      <ConsoleCard title={t('manage:sessions.section_overview')}>
        <ConsoleFieldRow label={t('manage:common_fields.agent')}>
          <p className="font-mono select-text">{detail.agent_id || '—'}</p>
        </ConsoleFieldRow>
        <ConsoleFieldRow label={t('manage:sessions.field_session_id')}>
          <p className="font-mono select-text">{detail.session_id}</p>
        </ConsoleFieldRow>
        <ConsoleFieldRow label={t('manage:sessions.field_created')}>
          <p className="tabular-nums">{formatDateTimeSeconds(detail.created_at) || '—'}</p>
        </ConsoleFieldRow>
        <ConsoleFieldRow label={t('manage:sessions.field_updated')}>
          <p className="tabular-nums">{formatDateTimeSeconds(detail.updated_at) || '—'}</p>
        </ConsoleFieldRow>
        {skills && (
          <ConsoleFieldRow label={t('manage:common_fields.skills')}>
            <p className="t-copy">{skills}</p>
          </ConsoleFieldRow>
        )}
        {mcp && (
          <ConsoleFieldRow label={t('manage:common_fields.mcp_servers')}>
            <p className="t-copy">{mcp}</p>
          </ConsoleFieldRow>
        )}
        {/* `/manage/errors` links to this record so the operator can inspect its
            failure. Marked as the deployment's own words: `last_error` carries raw
            exception text and wire spellings straight from the API, and the
            console's prose rules do not apply to text it did not write (§8). */}
        {detail.last_error && (
          <ConsoleFieldRow label={t('manage:sessions.field_last_error')}>
            <p data-slot="verbatim" className="t-copy break-words text-crimson-fg select-text">
              {localizeDisplayText(detail.last_error)}
            </p>
          </ConsoleFieldRow>
        )}
      </ConsoleCard>

      {/* Its own card rather than a row in the overview, like the pool counters
          on the environment page: a small report is not a single value, and §3
          gives it a block instead of a line in a list of them.

          Shown verbatim and unparsed. `template_mcp_config` is the resolved
          `mcp_servers` object for this session. Rendering selected fields would
          imply a stable display schema that this engine configuration does not
          have. The row above names the servers; this block shows their actual
          configuration, including targets. */}
      {mcpConfig && (
        <ConsoleCard
          title={t('manage:sessions.mcp_config_section')}
          intro={t('manage:sessions.mcp_config_intro')}
        >
          <pre tabIndex={0} className="console-scroll console-val mt-0.5 max-h-80 select-text overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/40 px-2.5 py-2 text-11 leading-5 text-foreground">
            {mcpConfig}
          </pre>
        </ConsoleCard>
      )}

      <Collapsible open={traceOpen} onOpenChange={setTraceOpen}>
        <CollapsibleTrigger render={<Button variant="outline" />} className="group">
          {t('manage:sessions.trace_section')}
          <ChevronDown className="size-4 transition-transform group-data-open:rotate-180" aria-hidden />
        </CollapsibleTrigger>
        <CollapsibleContent className="mt-4 space-y-4">
          <ConsoleCard title={t('manage:sessions.trace_section')}>
            {trace ? (
              <>
                <ConsoleFieldRow label={t('manage:sessions.field_running_responses')}>
                  <p className="font-mono select-text">{trace.current_response_ids.join(', ') || '—'}</p>
                </ConsoleFieldRow>
                <ConsoleFieldRow label={t('manage:sessions.field_response_count')}>
                  <p className="tabular-nums">{trace.responses.length}</p>
                </ConsoleFieldRow>
                <ConsoleFieldRow label={t('manage:sessions.field_events')}>
                  <p className="tabular-nums">
                    {t('manage:sessions.events_value', {
                      messages: trace.messages.length,
                      parts: trace.parts.length,
                    })}
                  </p>
                </ConsoleFieldRow>
                <ConsoleFieldRow label={t('manage:sessions.recent_events')} labelsGroup>
                  <TraceDigest trace={trace} />
                </ConsoleFieldRow>
              </>
            ) : traceLoading ? (
              <p className="t-copy text-muted-foreground" role="status">{t('common:loading')}</p>
            ) : null}
            {traceError && (
              <ErrorNote>
                {traceError.message}{' '}
                <Button variant="outline" size="sm" onClick={() => void refreshTrace()}>
                  {t('common:retry')}
                </Button>
              </ErrorNote>
            )}
          </ConsoleCard>

          {trace && (trace.parts.length || trace.responses.length) ? (
            <ConsoleCard
              title={t('manage:sessions.parts_section')}
              intro={t('manage:sessions.parts_intro')}
            >
              <ResponseParts sessionId={detail.session_id} trace={trace} />
            </ConsoleCard>
          ) : null}
        </CollapsibleContent>
      </Collapsible>
    </ConsoleRecordPage>
  );
}
