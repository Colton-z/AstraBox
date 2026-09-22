import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import { adminListSandboxes } from '@/api';
import type { AdminSandboxSummary } from '@/types';

import {
  ConsoleCard,
  ConsoleErrorState,
  ConsoleFact,
  ConsoleFactRail,
  ConsoleFieldRow,
  ConsoleRecordLoading,
  ConsoleRecordPage,
  formatDateTime,
  useRecordCrumb,
} from './console';
import { SandboxDiagnosticsPanel } from './SandboxDiagnosticsPanel';
import { SandboxSecurityPanel } from './SandboxSecurityPanel';
import {
  sandboxImage,
  sandboxStateLabel,
  sandboxStateTone,
  shortId,
} from './sandboxConfig';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

// The list is paged; a record reached by URL has to be found in it. One wide
// page covers every deployment this console is used on, and a sandbox outside
// it is reported as not found rather than silently rendered as missing.
const LOOKUP_PAGE_SIZE = 200;

/**
 * One sandbox, on its own page.
 *
 * Containment and diagnostics are whole panels — a security posture with its
 * own rows, and a set of reports fetched one at a time. Each is a card of its
 * own rather than the `value` of a label/value field, which would render a
 * panel as if it were a single fact, in a column too narrow to read it
 * (docs/frontend-design.md §3).
 *
 * Nothing is editable: a sandbox is the deployment's own object, and the
 * console reports it.
 */
export default function SandboxDetailPage() {
  const { sandboxId = '' } = useParams();
  const { t } = useTranslation();

  const [sandbox, setSandbox] = useState<AdminSandboxSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useRecordCrumb(sandbox ? shortId(sandbox.sandbox_id) : t('common:sandbox'));

  // True after any successful read, a not-found answer included (see keepsLastRead).
  const loaded = useRef(false);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (!background) setLoading(true);
    try {
      const page = await adminListSandboxes({ pageSize: LOOKUP_PAGE_SIZE });
      const found = (page.items ?? []).find((s) => s.sandbox_id === sandboxId) ?? null;
      setSandbox(found);
      setError(found ? '' : t('manage:sandboxes.not_found', { id: sandboxId }));
      loaded.current = true;
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      setError((e as Error).message);
    } finally {
      if (!background) setLoading(false);
    }
  }, [sandboxId, t]);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load);

  if (loading && !sandbox) {
    return (
      <ConsoleRecordPage title={t('common:sandbox')}>
        <ConsoleRecordLoading />
      </ConsoleRecordPage>
    );
  }

  if (!sandbox) {
    return (
      <ConsoleRecordPage title={t('common:sandbox')}>
        <ConsoleErrorState
          title={t('manage:sandboxes.error_title')}
          detail={error}
          onRetry={() => void load()}
        />
      </ConsoleRecordPage>
    );
  }

  const metadata = sandbox.metadata ?? {};
  const entrypoint = sandbox.entrypoint ?? [];

  return (
    <ConsoleRecordPage
      title={shortId(sandbox.sandbox_id)}
      status={{ tone: sandboxStateTone(sandbox.state), label: sandboxStateLabel(sandbox.state) }}
      rail={
        <ConsoleFactRail>
          {/* Mono: the backend's registered name is what goes in
              ASTRABOX_SANDBOX_BACKEND and what a log line says (§6). */}
          <ConsoleFact
            label={t('manage:sandboxes.field_backend')}
            value={sandbox.backend ? <span className="font-mono">{sandbox.backend}</span> : '—'}
          />
          <ConsoleFact
            label={t('manage:sandboxes.col_image')}
            value={<span className="font-mono">{sandboxImage(sandbox)}</span>}
          />
          <ConsoleFact
            label={t('common:created_at')}
            value={formatDateTime(sandbox.created_at) || '—'}
          />
          <ConsoleFact
            label={t('manage:sandboxes.col_expires')}
            value={formatDateTime(sandbox.expires_at) || '—'}
          />
        </ConsoleFactRail>
      }
    >
      <ConsoleCard title={t('manage:sandboxes.section_overview')}>
        <ConsoleFieldRow label={t('manage:sandboxes.field_sandbox_id')}>
          <p className="font-mono select-text">{sandbox.sandbox_id}</p>
        </ConsoleFieldRow>
        <ConsoleFieldRow label={t('manage:sandboxes.col_session')}>
          {/* Attribution is the box's own create metadata or nothing: a box
              without it is unattributed, never joined to a session by id shape
              or create order. Where the box does name one, this is that id — a
              link, not a lookup. */}
          {sandbox.session_id ? (
            <Link
              to={`/manage/sessions/${encodeURIComponent(sandbox.session_id)}`}
              className="font-mono underline underline-offset-2 hover:text-foreground"
            >
              {sandbox.session_id}
            </Link>
          ) : (
            <p className="text-muted-foreground">{t('manage:sandboxes.no_session_claim')}</p>
          )}
        </ConsoleFieldRow>
        <ConsoleFieldRow label={t('manage:sandboxes.field_entrypoint')}>
          <p className="font-mono break-words select-text">
            {entrypoint.length ? entrypoint.join(' ') : '—'}
          </p>
        </ConsoleFieldRow>
        {Object.keys(metadata).length > 0 && (
          <ConsoleFieldRow label={t('manage:sandboxes.field_metadata')} labelsGroup>
            <pre tabIndex={0} className="console-scroll max-h-40 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/40 px-2.5 py-2 font-mono text-11 leading-5">
              {JSON.stringify(metadata, null, 2)}
            </pre>
          </ConsoleFieldRow>
        )}
      </ConsoleCard>

      <ConsoleCard title={t('manage:sandboxes.section_security')}>
        <SandboxSecurityPanel sandboxId={sandbox.sandbox_id} />
      </ConsoleCard>

      <ConsoleCard title={t('manage:sandboxes.section_diagnostics')}>
        <SandboxDiagnosticsPanel sandboxId={sandbox.sandbox_id} />
      </ConsoleCard>
    </ConsoleRecordPage>
  );
}
