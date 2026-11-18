import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { diffLines } from 'diff';
import { useTranslation } from 'react-i18next';

import {
  ApiError,
  getAgentVersionChanges,
  listAgentVersions,
  restoreAgentVersion,
} from '@/api';
import type {
  AgentConfig,
  AgentVersionChange,
  AgentVersionChanges,
  AgentVersionPage,
  AgentVersionRecord,
} from '@/types';
import { ErrorNote } from '@/components/shell';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import { cn } from '@/lib/utils';

import {
  ConsoleCard,
  ConsoleErrorState,
  ConsoleFieldRow,
  ConsoleLoadMore,
  ConsoleSelect,
  ConsoleTable,
  ConsoleTableSkeleton,
  formatDateTime,
  formatDateTimeSeconds,
  type ConsoleColumn,
} from './console';

/** Versions read per page: short enough that a selected version's changes stay near the table. */
const PAGE_SIZE = 10;

/**
 * A string this long, or one with a line break, is compared line by line;
 * anything shorter reads better whole, one value over the other.
 */
const LINE_DIFF_LENGTH = 80;

/** The versions read so far, newest first, and the Agent's version when they were read. */
type History = { current: number; versions: AgentVersionRecord[]; hasMore: boolean };

/** An open restore: the version to restore and the Agent version it was compared against. */
type Restore = { target: number; against: number; conflict: boolean };

/**
 * `page` with the older versions already read kept below it.
 *
 * A stored version never changes, so rows read earlier still hold under a
 * fresh first page — but only when the two overlap. Without an overlap more
 * versions were saved than a page holds, something lies between them, and
 * the fresh page stands alone.
 */
function continued(page: AgentVersionPage, held: History | null): History {
  const fresh = { current: page.current_version, versions: page.versions, hasMore: page.has_more };
  const oldest = page.versions.at(-1)?.version;
  if (!held || oldest === undefined || !held.versions.some((v) => v.version === oldest)) return fresh;
  const older = held.versions.filter((v) => v.version < oldest);
  return older.length > 0
    ? { ...fresh, versions: [...page.versions, ...older], hasMore: held.hasMore }
    : fresh;
}

/**
 * An Agent's saved versions, what any one of them changed, and restoring one.
 *
 * The history is a card on the Agent's own page, like a schedule's runs: it is
 * a record of this Agent, read beside the configuration it explains. Selecting
 * a version opens its changes in a card below the table, so the list stays in
 * view while the reader compares (docs/frontend-design.md §3). A restore is a
 * question asked before it writes: the dialog shows what would change from the
 * current version, and which differences a restore leaves as they are.
 */
export function AgentVersionHistory({
  agentId,
  currentVersion,
  fieldLabels,
  unsavedEdits,
  onRestored,
  onChanged,
}: {
  agentId: string;
  /** The version of the record the page shows; the history is read again when it moves. */
  currentVersion: number;
  /** What the edit form calls each stored field, by dotted path (`agentFieldLabels`). */
  fieldLabels: Map<string, string>;
  /** The page holds edits in its cards that a restore would replace. */
  unsavedEdits: boolean;
  /** Read the restored Agent and seed the page's drafts from it. */
  onRestored: () => Promise<void>;
  /** Read the Agent again after another writer changed it; the page keeps its drafts. */
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [history, setHistory] = useState<History | null>(null);
  // The same read, for callbacks that must extend what is held now rather
  // than what was held when they were created.
  const held = useRef<History | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [older, setOlder] = useState({ loading: false, error: '' });
  const [selected, setSelected] = useState<number | null>(null);
  const [restore, setRestore] = useState<Restore | null>(null);
  const [restoreOpen, setRestoreOpen] = useState(false);
  const changesRef = useRef<HTMLDivElement>(null);

  const reload = useCallback(async (context?: ReloadContext): Promise<History | null> => {
    if (!context?.background) setLoading(true);
    try {
      const next = continued(await listAgentVersions(agentId, { limit: PAGE_SIZE }), held.current);
      held.current = next;
      setHistory(next);
      setError('');
      return next;
    } catch (e) {
      if (!keepsLastRead(e, context, held.current !== null)) setError((e as Error).message);
      return null;
    } finally {
      if (!context?.background) setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void reload();
  }, [reload, currentVersion]);
  useKeepCurrent(reload);

  const loadOlder = async () => {
    const last = held.current?.versions.at(-1);
    if (!last || older.loading) return;
    setOlder({ loading: true, error: '' });
    try {
      const page = await listAgentVersions(agentId, { limit: PAGE_SIZE, before: last.version });
      const now = held.current;
      // A re-read that replaced the rows meanwhile ends somewhere else; this
      // page continues the rows it was asked for, not those.
      if (now && now.versions.at(-1)?.version === last.version) {
        const next = { ...now, versions: [...now.versions, ...page.versions], hasMore: page.has_more };
        held.current = next;
        setHistory(next);
      }
      setOlder({ loading: false, error: '' });
    } catch (e) {
      setOlder({ loading: false, error: (e as Error).message });
    }
  };

  // The newest version either read knows of: the page's record moves on a save
  // before this history is read again, and a re-read here can see a change the
  // page's record has not.
  const latest = Math.max(history?.current ?? 0, currentVersion);

  const select = (version: number) => {
    setSelected(version);
    // The changes open below the table, which can be below the fold.
    requestAnimationFrame(() => changesRef.current?.scrollIntoView?.({ block: 'nearest' }));
  };

  const openRestore = (target: number) => {
    setRestore({ target, against: latest, conflict: false });
    setRestoreOpen(true);
  };

  const restored = async (agent: AgentConfig) => {
    setRestoreOpen(false);
    if (typeof agent.version === 'number') setSelected(agent.version);
    await onRestored();
  };

  // The restore wrote nothing: the Agent moved past the version the comparison
  // was drawn against. The comparison is drawn again against the version the
  // Agent has now, and the reader confirms that one or leaves.
  const conflicted = async () => {
    const fresh = await reload();
    setRestore((open) => open && {
      ...open,
      against: fresh ? fresh.current : open.against,
      conflict: true,
    });
    onChanged();
  };

  const operationLabel = (version: AgentVersionRecord) =>
    version.operation === 'restore'
      ? t('manage:agent_versions.operation_restore', { version: version.restored_from_version })
      : t(`manage:agent_versions.operation_${version.operation}`);

  const columns: ConsoleColumn<AgentVersionRecord>[] = [
    {
      key: 'version',
      intent: 'compact',
      header: t('manage:agent_versions.col_version'),
      cell: (version) => String(version.version),
    },
    {
      key: 'change',
      intent: 'text',
      header: t('manage:agent_versions.col_change'),
      cell: operationLabel,
    },
    {
      key: 'fields',
      intent: 'name',
      header: t('manage:agent_versions.col_fields'),
      cell: (version) => version.changed_fields?.length
        ? version.changed_fields.map((name) => fieldLabels.get(name) ?? name).join(', ')
        : '—',
    },
    {
      key: 'author',
      intent: 'identifier',
      header: t('manage:agent_versions.col_author'),
      cell: (version) => version.author_user_id || '—',
    },
    {
      key: 'time',
      intent: 'timestamp',
      header: t('manage:agent_versions.col_time'),
      title: (version) => formatDateTimeSeconds(version.created_at),
      cell: (version) => (
        <time dateTime={version.created_at}>{formatDateTime(version.created_at)}</time>
      ),
    },
  ];

  const baseline = history?.versions.find((version) => version.operation === 'baseline');

  return (
    <>
      <ConsoleCard
        title={t('manage:agent_versions.title')}
        intro={t('manage:agent_versions.intro')}
        note={t('manage:agent_versions.note')}
      >
        <ConsoleTable
          columns={columns}
          rows={error ? [] : history?.versions ?? []}
          rowKey={(version) => String(version.version)}
          onRowClick={(version) => select(version.version)}
          isRowActive={(version) => version.version === selected}
          empty={
            error ? (
              <ConsoleErrorState
                title={t('manage:agent_versions.error_title')}
                detail={error}
                onRetry={() => void reload()}
              />
            ) : loading && !history ? (
              <ConsoleTableSkeleton columns={columns} rows={3} />
            ) : undefined
          }
          footer={
            baseline && !error ? (
              <p className="t-copy-sm px-5 py-3 text-muted-foreground">
                {t('manage:agent_versions.history_starts', { version: baseline.version })}
              </p>
            ) : undefined
          }
        />
        <ConsoleLoadMore
          hasMore={!error && Boolean(history?.hasMore)}
          loading={older.loading}
          error={older.error}
          onLoadMore={() => void loadOlder()}
        />
      </ConsoleCard>

      {selected !== null && history && (
        <div ref={changesRef}>
          <VersionChanges
            key={selected}
            agentId={agentId}
            version={selected}
            latest={latest}
            choices={history.versions.map((version) => version.version)}
            fieldLabels={fieldLabels}
            onRestore={() => openRestore(selected)}
          />
        </div>
      )}

      {restore && (
        <RestoreDialog
          agentId={agentId}
          restore={restore}
          open={restoreOpen}
          fieldLabels={fieldLabels}
          unsavedEdits={unsavedEdits}
          onCancel={() => setRestoreOpen(false)}
          onRestored={restored}
          onConflict={conflicted}
        />
      )}
    </>
  );
}

/** What one version changed, against the version before it or one the reader picks. */
function VersionChanges({
  agentId,
  version,
  latest,
  choices,
  fieldLabels,
  onRestore,
}: {
  agentId: string;
  version: number;
  /** The Agent's version now; it has nothing to restore. */
  latest: number;
  /** The versions read so far, each a possible base. */
  choices: number[];
  fieldLabels: Map<string, string>;
  onRestore: () => void;
}) {
  const { t } = useTranslation();
  const baseId = useId();
  // Null compares with the version before this one, which is the server's
  // answer to give: it knows which versions were kept.
  const [base, setBase] = useState<number | null>(null);
  const [changes, setChanges] = useState<AgentVersionChanges | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // Only the answer for the comparison on screen may land: a slower answer
    // for the base picked before would otherwise replace it.
    let current = true;
    setChanges(null);
    setError('');
    getAgentVersionChanges(agentId, version, base ?? undefined).then(
      (next) => {
        if (current) setChanges(next);
      },
      (e: unknown) => {
        if (current) setError((e as Error).message);
      },
    );
    return () => {
      current = false;
    };
  }, [agentId, version, base, attempt]);

  const bases = choices.filter((choice) => choice !== version);
  const comparedWith = changes?.base_version ?? null;
  const intro = !changes
    ? undefined
    : comparedWith === null
      ? t('manage:agent_versions.oldest')
      : t('manage:agent_versions.changes_from', { base: comparedWith });

  return (
    <ConsoleCard
      title={t('manage:agent_versions.version_title', { version })}
      intro={intro}
      actions={version !== latest && (
        <Button variant="outline" size="sm" onClick={onRestore}>
          {t('manage:agent_versions.restore')}
        </Button>
      )}
    >
      {/* A choice only when there is one to make: with a single other
          version, it is already the comparison shown (§5). */}
      {bases.length > 1 && (
        <ConsoleFieldRow label={t('manage:agent_versions.compare_with')} htmlFor={baseId}>
          <ConsoleSelect
            id={baseId}
            value={(base ?? comparedWith)?.toString() ?? ''}
            onChange={(value) => setBase(Number(value))}
            options={bases.map((choice) => ({
              value: String(choice),
              label: t('manage:agent_versions.version_option', { version: choice }),
            }))}
          />
        </ConsoleFieldRow>
      )}
      {error ? (
        <ConsoleErrorState
          title={t('manage:agent_versions.changes_error_title')}
          detail={error}
          onRetry={() => setAttempt((n) => n + 1)}
        />
      ) : !changes ? (
        <p className="t-copy text-muted-foreground">{t('common:loading')}</p>
      ) : comparedWith === null ? null : changes.changes.length === 0 ? (
        <p className="t-copy text-muted-foreground">{t('manage:agent_versions.no_differences')}</p>
      ) : (
        <ChangeList
          changes={changes.changes}
          fieldLabels={fieldLabels}
          label={intro}
          sides={{
            before: t('manage:agent_versions.version_option', { version: comparedWith }),
            after: t('manage:agent_versions.version_option', { version }),
          }}
        />
      )}
    </ConsoleCard>
  );
}

/**
 * The restore question: what restoring `restore.target` would change from the
 * Agent's version `restore.against`, and which differences it leaves alone.
 *
 * The comparison is drawn once per opening and kept while the dialog is open.
 * The restore names the version it was drawn against, so a change made since
 * by anyone else is refused by the server rather than overwritten by a
 * comparison the reader never saw.
 */
function RestoreDialog({
  agentId,
  restore,
  open,
  fieldLabels,
  unsavedEdits,
  onCancel,
  onRestored,
  onConflict,
}: {
  agentId: string;
  restore: Restore;
  open: boolean;
  fieldLabels: Map<string, string>;
  unsavedEdits: boolean;
  onCancel: () => void;
  onRestored: (agent: AgentConfig) => Promise<void>;
  onConflict: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const restorableId = useId();
  const keptId = useId();
  const [preview, setPreview] = useState<AgentVersionChanges | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [failure, setFailure] = useState('');
  const [busy, setBusy] = useState(false);
  const { target, against } = restore;

  useEffect(() => {
    let current = true;
    setPreview(null);
    setPreviewError('');
    getAgentVersionChanges(agentId, target, against).then(
      (next) => {
        if (current) setPreview(next);
      },
      (e: unknown) => {
        if (current) setPreviewError((e as Error).message);
      },
    );
    return () => {
      current = false;
    };
  }, [agentId, target, against]);

  useEffect(() => {
    if (open) setFailure('');
  }, [open]);

  const restorable = useMemo(() => preview?.changes.filter((change) => change.restorable) ?? [], [preview]);
  const kept = useMemo(() => preview?.changes.filter((change) => !change.restorable) ?? [], [preview]);

  const confirm = async () => {
    if (!preview || busy) return;
    setBusy(true);
    setFailure('');
    try {
      await onRestored(await restoreAgentVersion(agentId, target, against));
    } catch (e) {
      // The server's own signal that the Agent moved past `against`: the
      // same restore resent can never succeed, so the comparison is redone.
      if (e instanceof ApiError && e.code === 'AGENT_VERSION_CONFLICT') await onConflict();
      else setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !busy) onCancel();
      }}
    >
      <AlertDialogContent className="max-h-[85dvh] grid-rows-[auto_minmax(0,1fr)_auto] data-[size=default]:max-w-[calc(100%-2rem)] data-[size=default]:sm:max-w-2xl">
        <AlertDialogHeader>
          <AlertDialogTitle>{t('manage:agent_versions.restore_title', { version: target })}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('manage:agent_versions.restore_description', { version: target, current: against })}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div tabIndex={0} className="console-scroll flex min-h-0 flex-col gap-4 overflow-y-auto">
          {restore.conflict && <ErrorNote>{t('manage:agent_versions.restore_conflict')}</ErrorNote>}
          {failure && <ErrorNote>{failure}</ErrorNote>}
          {previewError ? (
            <ErrorNote>{previewError}</ErrorNote>
          ) : !preview ? (
            <p className="t-copy text-muted-foreground">{t('common:loading')}</p>
          ) : restorable.length === 0 ? (
            <p className="t-copy">{t('manage:agent_versions.restore_nothing', { version: target })}</p>
          ) : (
            <section className="flex flex-col gap-2">
              <h3 id={restorableId} className="t-label">{t('manage:agent_versions.restore_will')}</h3>
              <ChangeList
                changes={restorable}
                fieldLabels={fieldLabels}
                labelledBy={restorableId}
                sides={{
                  before: t('manage:agent_versions.restore_now'),
                  after: t('manage:agent_versions.restore_after'),
                }}
              />
            </section>
          )}
          {kept.length > 0 && (
            <section className="flex flex-col gap-2">
              <h3 id={keptId} className="t-label">{t('manage:agent_versions.restore_kept')}</h3>
              <p className="t-copy-sm text-muted-foreground">
                {t('manage:agent_versions.restore_kept_help', { version: target })}
              </p>
              <ChangeList changes={kept} fieldLabels={fieldLabels} labelledBy={keptId} />
            </section>
          )}
          {unsavedEdits && restorable.length > 0 && (
            <p className="t-copy-sm text-muted-foreground">{t('manage:agent_versions.restore_unsaved')}</p>
          )}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>{t('common:cancel')}</AlertDialogCancel>
          {restorable.length > 0 && (
            <AlertDialogAction disabled={busy} onClick={() => void confirm()}>
              {busy
                ? t('manage:agent_versions.restoring')
                : t('manage:agent_versions.restore_confirm', { version: target })}
            </AlertDialogAction>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * Changes, each under the name the edit form gives its field. With `sides`
 * each shows its two values; without, only which fields differ.
 */
function ChangeList({
  changes,
  fieldLabels,
  label,
  labelledBy,
  sides,
}: {
  changes: AgentVersionChange[];
  fieldLabels: Map<string, string>;
  label?: string;
  labelledBy?: string;
  /** What the two values are: the base's and the compared version's. */
  sides?: { before: string; after: string };
}) {
  return (
    <ul aria-label={label} aria-labelledby={labelledBy} className="flex flex-col gap-4">
      {changes.map((change) => (
        <li key={JSON.stringify(change.path)} className="flex min-w-0 flex-col gap-1.5">
          <FieldName path={change.path} fieldLabels={fieldLabels} />
          {sides && <ChangeValues before={change.before} after={change.after} sides={sides} />}
        </li>
      ))}
    </ul>
  );
}

/**
 * A change's field: the form's label for the longest leading part of the path
 * that has one, then the rest of the path as stored. A path with no label at
 * all — a binding made outside the form — is shown as stored.
 */
function FieldName({ path, fieldLabels }: { path: string[]; fieldLabels: Map<string, string> }) {
  let named = path.length;
  while (named > 0 && !fieldLabels.has(path.slice(0, named).join('.'))) named -= 1;
  const label = named > 0 ? fieldLabels.get(path.slice(0, named).join('.')) : undefined;
  const rest = path.slice(named).join('.');
  return (
    <span className="t-label min-w-0 break-words">
      {label}
      {label && rest && <span aria-hidden> › </span>}
      {rest && <span className="console-val font-normal text-muted-foreground">{rest}</span>}
    </span>
  );
}

const textOrUnset = (value: unknown): value is string | null | undefined =>
  value == null || typeof value === 'string';
const longText = (value: unknown) =>
  typeof value === 'string' && (value.includes('\n') || value.length >= LINE_DIFF_LENGTH);

/** Two values of one field: long text line by line, anything else whole, one over the other. */
function ChangeValues({
  before,
  after,
  sides,
}: {
  before: unknown;
  after: unknown;
  sides: { before: string; after: string };
}) {
  if (textOrUnset(before) && textOrUnset(after) && (longText(before) || longText(after))) {
    return <LineDiff before={before ?? ''} after={after ?? ''} />;
  }
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1">
      <dt className="console-label">{sides.before}</dt>
      <dd className="min-w-0">
        <del className="no-underline">
          <ShownValue value={before} />
        </del>
      </dd>
      <dt className="console-label">{sides.after}</dt>
      <dd className="min-w-0">
        <ins className="no-underline">
          <ShownValue value={after} />
        </ins>
      </dd>
    </dl>
  );
}

function ShownValue({ value }: { value: unknown }) {
  const { t } = useTranslation();
  if (value == null) {
    return <span className="text-muted-foreground">{t('manage:agent_versions.value_unset')}</span>;
  }
  if (value === '') {
    return <span className="text-muted-foreground">{t('manage:agent_versions.value_empty')}</span>;
  }
  if (typeof value === 'boolean') {
    return <span>{value ? t('manage:console.toggle_on') : t('manage:console.toggle_off')}</span>;
  }
  if (typeof value === 'string' || typeof value === 'number') {
    return <span className="whitespace-pre-wrap break-words">{String(value)}</span>;
  }
  // Mono: a structured value is shown as the JSON its field is edited as.
  return (
    <pre className="console-val text-11 leading-5 whitespace-pre-wrap break-words">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

/** Two texts line by line, as the conversation shows a file edit. */
function LineDiff({ before, after }: { before: string; after: string }) {
  const parts = useMemo(() => diffLines(before, after), [before, after]);
  return (
    <div
      tabIndex={0}
      className="console-scroll console-val max-h-80 overflow-auto rounded-md border bg-muted/40 py-1 text-11 leading-5"
    >
      {parts.map((part, i) => {
        const Part = part.added ? 'ins' : part.removed ? 'del' : 'div';
        return (
          <Part
            key={i}
            className={cn(
              'block no-underline',
              part.added ? 'bg-mint/10 text-mint-fg' : part.removed ? 'bg-crimson/10 text-crimson-fg' : 'text-muted-foreground',
            )}
          >
            {part.value.replace(/\n$/, '').split('\n').map((line, j) => (
              <span key={j} className="flex">
                <span aria-hidden className="w-5 shrink-0 select-none text-center">
                  {part.added ? '+' : part.removed ? '-' : ' '}
                </span>
                <span className="min-w-0 flex-1 whitespace-pre-wrap break-words px-1">{line}</span>
              </span>
            ))}
          </Part>
        );
      })}
    </div>
  );
}
