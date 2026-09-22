import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { Button } from '@/components/ui/button';
import { PageShell } from '@/components/shell';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { listAgentSessions, listAgents } from '@/api';
import type { AdminSessionSummary } from '@/types';

import {
  ConsolePageHeader,
  ConsoleTable,
  ConsoleToolbar,
  ConsoleSearch,
  ConsoleEmptyState,
  ConsoleTableNote,
  ConsoleTableSkeleton,
  ConsoleErrorState,
  StatusPill,
  NameCell,
  useRecordCrumb,
  type ConsoleColumn,
} from './console';
import { formatDateTime } from './agentConfig';
import {
  formatDuration,
  sessionStateIsLive,
  sessionStateLabel,
  sessionStateTone,
  sessionUserDisplay,
  shortId,
} from './sessionConfig';

const ALL = '__all__';

// Per-agent conversation console, backed by the agent_id-keyed listing route —
// the limit is this agent's own, so other agents' traffic cannot page these
// conversations out of reach.
export default function AgentSessionsPage() {
  const { t } = useTranslation();
  const { agentId = '' } = useParams();
  const navigate = useNavigate();
  const [docs, setDocs] = useState<AdminSessionSummary[]>([]);
  const [agentName, setAgentName] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [search, setSearch] = useState('');
  const [state, setState] = useState<string>(ALL);

  // The heading is the fixed word "Sessions", so the trail is what tells the
  // reader whose sessions these are. Without a name here its last segment
  // falls back to the URL's agent id (see RecordCrumbProvider).
  useRecordCrumb(agentName || t('common:agent'));

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sessions, agents] = await Promise.all([listAgentSessions(agentId), listAgents()]);
      setDocs(sessions);
      // The trail and the description name the agent, so a stale/unknown id
      // shows the id rather than leaving both blank as though no agent were
      // asked for.
      setAgentName(agents.find((a) => a.agent_id === agentId)?.name ?? agentId);
      setError('');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const states = useMemo(
    () => [...new Set(docs.map((s) => s.state).filter(Boolean))].sort(),
    [docs],
  );

  // The filter's vocabulary, written once and read twice: the options in the
  // popup, and the label the closed trigger shows. `<SelectValue>` renders the
  // raw value unless the root carries the same list as its `items`
  // (@base-ui/react/select), so without this the trigger would read `__all__` —
  // the sentinel — or a wire spelling like `PROVISIONING`, on screen (§6).
  const stateOptions = useMemo(
    () => [
      { value: ALL, label: t('manage:filter.all_status') },
      ...states.map((s) => ({ value: s, label: sessionStateLabel(s) })),
    ],
    [states, t],
  );

  const liveCount = useMemo(() => docs.filter((s) => sessionStateIsLive(s.state)).length, [docs]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return docs.filter((s) => {
      if (state !== ALL && s.state !== state) return false;
      if (!q) return true;
      return (
        String(s.session_id || '').toLowerCase().includes(q) ||
        String(s.sandbox_id || '').toLowerCase().includes(q) ||
        sessionUserDisplay(s).toLowerCase().includes(q)
      );
    });
  }, [docs, search, state]);

  const columns: ConsoleColumn<AdminSessionSummary>[] = [
    {
      key: 'user',
      intent: 'name',
      header: t('manage:common_fields.user'),
      cell: (s) => <NameCell name={sessionUserDisplay(s)} sub={shortId(s.session_id)} />,
    },
    {
      key: 'state',
      intent: 'status',
      header: t('common:status'),
      cell: (s) => (
        <StatusPill tone={sessionStateTone(s.state)} live={sessionStateIsLive(s.state)}>
          {sessionStateLabel(s.state)}
        </StatusPill>
      ),
    },
    {
      key: 'sandbox',
      intent: 'identifier',
      header: t('common:sandbox'),
      cell: (s) => <span className="text-muted-foreground">{shortId(s.sandbox_id)}</span>,
    },
    {
      key: 'created',
      intent: 'timestamp',
      header: t('common:created_at'),
      cell: (s) => <span className="text-muted-foreground">{formatDateTime(s.created_at)}</span>,
    },
    {
      key: 'duration',
      intent: 'compact',
      header: t('manage:common_fields.duration'),
      cell: (s) => <span className="text-muted-foreground">{formatDuration(s.duration_seconds)}</span>,
    },
    {
      key: 'runtime',
      intent: 'compact',
      header: t('manage:common_fields.local_runtime'),
      cell: (s) =>
        s.has_local_runtime ? (
          <span className="text-mint-fg">{t('common:yes')}</span>
        ) : (
          <span className="text-muted-foreground">{t('common:no')}</span>
        ),
    },
  ];

  return (
    <PageShell>
      <ConsolePageHeader
        title={t('manage:agent_sessions.title')}
        meta={t('manage:agent_sessions.meta', { count: docs.length, live: liveCount })}
        description={t('manage:agent_sessions.description', { name: agentName })}
        actions={
          <Button variant="outline" size="icon" onClick={() => void load()} disabled={loading} aria-label={t('common:refresh')}>
            <RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'} />
          </Button>
        }
      />

      <div className="flex flex-1 flex-col gap-4">
        {/* Not over the error card. A failed refresh keeps the conversations it
            last read but empties the table, and a state filter left standing
            there would offer states that are not on screen and narrow a list the
            reader cannot see (docs/frontend-design.md §5). The search field owns
            its own threshold, so this condition says only what it cannot know. */}
        {!error && (
          <ConsoleToolbar>
            <ConsoleSearch
              value={search}
              onChange={setSearch}
              total={docs.length}
              placeholder={t('manage:agent_sessions.search_placeholder')}
            />
            {/* The options are the states this agent's sessions happen to be in —
                open-ended, so they belong in a select rather than a chip row (see
                FilterChips). It renders only when the data offers a real choice,
                or while it is actively narrowing, so the reader can see the filter
                and undo it (docs/frontend-design.md §5). */}
            {(states.length > 1 || state !== ALL) && (
              <Select
                items={stateOptions}
                value={state}
                // Base UI reports a cleared select as `null`, which this filter
                // has no way to reach: every option carries a value, `__all__`
                // included. Narrowing to nothing is not a state it can hold.
                onValueChange={(value) => { if (value !== null) setState(value); }}
              >
                <SelectTrigger className="w-40">
                  <SelectValue />
                </SelectTrigger>
                {/* The menu hangs off the trigger's box rather than sitting on top
                    of it. Base UI's default `alignItemWithTrigger` is the macOS
                    native-select behaviour, which places the popup so the text of
                    the selected item lands on the trigger's text: the text lines
                    up and the boxes do not. `align="start"` keeps the left edges
                    together, which is how every filter in this console is
                    anchored. */}
                <SelectContent align="start" alignItemWithTrigger={false}>
                  {stateOptions.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </ConsoleToolbar>
        )}

        <ConsoleTable
          columns={columns}
          rows={loading || error ? [] : filtered}
          rowKey={(s) => s.session_id}
          onRowClick={(s) => navigate(`/manage/sessions/${encodeURIComponent(s.session_id)}`)}
          empty={
            loading ? (
              <ConsoleTableSkeleton columns={columns} />
            ) : error ? (
              <ConsoleErrorState title={t('manage:agent_sessions.error_title')} detail={error} onRetry={() => void load()} />
            ) : docs.length === 0 ? (
              <ConsoleEmptyState
                title={t('manage:agent_sessions.empty_title')}
                hint={t('manage:agent_sessions.empty_hint')}
              />
            ) : (
              <ConsoleTableNote>{t('manage:agent_sessions.no_match', { query: search })}</ConsoleTableNote>
            )
          }
        />
      </div>
    </PageShell>
  );
}
