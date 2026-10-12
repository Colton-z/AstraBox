import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown, ArrowUp, Plus, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import i18n from '@/i18n';
import { ErrorNote, PageShell } from '@/components/shell';
import { Button } from '@/components/ui/button';
import { FieldError } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemTitle,
} from '@/components/ui/item';
import {
  ApiError,
  createAdminAgentCategory,
  deleteAdminAgentCategory,
  listAdminAgentCategories,
  renameAdminAgentCategory,
  reorderAdminAgentCategories,
} from '@/api';
import type { AdminAgentCategory } from '@/types';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';

import {
  ConsoleCard,
  ConsoleDangerButton,
  ConsoleEmptyState,
  ConsoleErrorState,
  ConsoleFieldRow,
  ConsolePageHeader,
  ConsoleRecordLoading,
  ConsoleTextField,
} from './console';

/**
 * What to tell an administrator about a refused category write.
 *
 * The two refusals with a next step get a sentence in the reader's language;
 * anything else is the server's own words, which already name what was wrong.
 */
function refusalText(error: unknown, name: string): string {
  if (error instanceof ApiError && error.code === 'AGENT_CATEGORY_NAME_TAKEN') {
    return i18n.t('manage:agent_categories.name_taken', { name });
  }
  if (error instanceof ApiError && error.code === 'AGENT_CATEGORY_NOT_FOUND') {
    return i18n.t('manage:agent_categories.gone', { name });
  }
  return (error as Error).message;
}

type Renaming = { id: string; name: string; error: string; saving: boolean };

/**
 * The Agent categories, in the order the home page shows them as tabs.
 *
 * Platform administrators define the list here: add, rename, move up or down,
 * delete. Whoever manages an Agent chooses its categories on the Agent's own
 * page, and Agents name a category by id, so a rename reaches every Agent in
 * it without touching them. Deleting takes the category off its Agents first,
 * which is why the confirmation says how many will lose it.
 */
export default function AgentCategoriesPage() {
  const { t } = useTranslation();
  const [rows, setRows] = useState<AdminAgentCategory[] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [actionError, setActionError] = useState('');
  const [newName, setNewName] = useState('');
  const [createError, setCreateError] = useState('');
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Renaming | null>(null);
  const loaded = useRef(false);
  const list = useRef<HTMLDivElement | null>(null);
  // Each order write carries the whole list, so they are sent one after
  // another: two in flight could land in either order and leave the older
  // order standing. A re-read meanwhile would show the order being replaced.
  const orderWrites = useRef<Promise<void>>(Promise.resolve());
  const pendingOrderWrites = useRef(0);
  // The arrow that moved a row. Moving a row moves its element, and a moved
  // element loses focus, so the arrow is focused again once the list renders.
  const refocus = useRef<string | null>(null);

  const load = useCallback(async (context?: ReloadContext) => {
    const background = context?.background === true;
    if (background && pendingOrderWrites.current > 0) return;
    if (!background) setRefreshing(true);
    try {
      setRows(await listAdminAgentCategories());
      loaded.current = true;
      setLoadError('');
    } catch (e) {
      if (keepsLastRead(e, context, loaded.current)) return;
      if (loaded.current) setActionError((e as Error).message);
      else setLoadError((e as Error).message);
    } finally {
      if (!background) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useKeepCurrent(load);

  useLayoutEffect(() => {
    const moved = refocus.current;
    if (!moved) return;
    refocus.current = null;
    list.current?.querySelector<HTMLElement>(`[data-move="${moved}"]`)?.focus();
  }, [rows]);

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    setCreating(true);
    setCreateError('');
    try {
      await createAdminAgentCategory(name);
      setNewName('');
      await load();
    } catch (e) {
      setCreateError(refusalText(e, name));
    } finally {
      setCreating(false);
    }
  };

  const saveRename = async () => {
    if (!renaming || !rows) return;
    const name = renaming.name.trim();
    const current = rows.find((row) => row.category_id === renaming.id);
    if (!current || !name) return;
    if (name === current.name) {
      setRenaming(null);
      return;
    }
    setRenaming({ ...renaming, saving: true, error: '' });
    try {
      const saved = await renameAdminAgentCategory(renaming.id, name);
      setRows((prev) =>
        prev && prev.map((row) => (row.category_id === saved.category_id ? { ...row, name: saved.name } : row)),
      );
      setRenaming(null);
    } catch (e) {
      setRenaming((prev) => prev && { ...prev, saving: false, error: refusalText(e, name) });
      if (e instanceof ApiError && e.code === 'AGENT_CATEGORY_NOT_FOUND') await load();
    }
  };

  const move = (index: number, offset: -1 | 1) => {
    if (!rows) return;
    const next = [...rows];
    const [moved] = next.splice(index, 1);
    next.splice(index + offset, 0, moved);
    refocus.current = `${moved.category_id}:${offset < 0 ? 'up' : 'down'}`;
    setRows(next);
    setActionError('');
    const ids = next.map((row) => row.category_id);
    pendingOrderWrites.current += 1;
    orderWrites.current = orderWrites.current.then(async () => {
      try {
        await reorderAdminAgentCategories(ids);
      } catch (e) {
        setActionError((e as Error).message);
        await load();
      } finally {
        pendingOrderWrites.current -= 1;
      }
    });
  };

  const remove = async (row: AdminAgentCategory) => {
    setActionError('');
    try {
      await deleteAdminAgentCategory(row.category_id);
      setRows((prev) => prev && prev.filter((other) => other.category_id !== row.category_id));
    } catch (e) {
      setActionError(refusalText(e, row.name));
      await load();
    }
  };

  const header = (
    <ConsolePageHeader
      title={t('manage:agent_categories.title')}
      meta={rows ? t('manage:agent_categories.meta', { count: rows.length }) : undefined}
      description={t('manage:agent_categories.description')}
      actions={
        <Button
          variant="outline"
          size="icon"
          onClick={() => void load()}
          disabled={refreshing}
          aria-label={t('common:refresh')}
        >
          <RefreshCw className={refreshing ? 'size-4 animate-spin' : 'size-4'} />
        </Button>
      }
    />
  );

  if (!rows) {
    return (
      <PageShell>
        {header}
        <div className="flex flex-1 flex-col gap-4">
          {loadError ? (
            <ConsoleErrorState
              title={t('manage:agent_categories.error_title')}
              detail={loadError}
              onRetry={() => void load()}
            />
          ) : (
            <ConsoleRecordLoading />
          )}
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell>
      {header}
      <div className="flex flex-1 flex-col gap-4">
        {actionError && <ErrorNote>{actionError}</ErrorNote>}
        <ConsoleCard
          title={t('manage:agent_categories.list_title')}
          intro={t('manage:agent_categories.list_intro')}
        >
          {rows.length === 0 ? (
            <ConsoleEmptyState
              title={t('manage:agent_categories.empty_title')}
              hint={t('manage:agent_categories.empty_hint')}
            />
          ) : (
            <div ref={list}>
              {/* The rule between rows is each row's own bottom border, as on
                  the credential assignments list: `divide-*` writes inside
                  `:where()` and loses to `Item`'s own border. */}
              <ItemGroup className="gap-0">
                {rows.map((row, index) => {
                  const editing = renaming?.id === row.category_id ? renaming : null;
                  const inputId = `agent-category-${row.category_id}-name`;
                  return (
                    /* `role="listitem"` because `ItemGroup` declares a list and
                       `Item` claims no role of its own. */
                    <Item
                      key={row.category_id}
                      role="listitem"
                      data-testid="agent-category-row"
                      className="rounded-none border-0 border-b border-border px-0 first:pt-0 last:border-b-0 last:pb-0"
                    >
                      {editing ? (
                        <form
                          className="flex min-w-0 flex-1 flex-wrap items-start gap-2"
                          onSubmit={(event) => {
                            event.preventDefault();
                            void saveRename();
                          }}
                        >
                          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                            {/* The row's band: as tall as the row's buttons
                                beside it (docs/frontend-design.md §9). */}
                            <Input
                              id={inputId}
                              className="h-7"
                              autoFocus
                              value={editing.name}
                              aria-label={t('manage:agent_categories.rename_label', { name: row.name })}
                              aria-invalid={editing.error ? true : undefined}
                              aria-describedby={editing.error ? `${inputId}-error` : undefined}
                              readOnly={editing.saving}
                              onChange={(event) =>
                                setRenaming({ ...editing, name: event.target.value, error: '' })
                              }
                              onKeyDown={(event) => {
                                if (event.key === 'Escape') setRenaming(null);
                              }}
                            />
                            {editing.error && (
                              <FieldError id={`${inputId}-error`} className="text-11 leading-snug">
                                {editing.error}
                              </FieldError>
                            )}
                          </div>
                          <div className="flex items-center gap-2">
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={editing.saving}
                              onClick={() => setRenaming(null)}
                            >
                              {t('common:cancel')}
                            </Button>
                            <Button
                              type="submit"
                              size="sm"
                              disabled={editing.saving || !editing.name.trim()}
                            >
                              {t('common:save')}
                            </Button>
                          </div>
                        </form>
                      ) : (
                        <>
                          <ItemContent className="min-w-0 gap-0">
                            <ItemTitle className="t-label block truncate text-13">{row.name}</ItemTitle>
                            <ItemDescription className="t-copy-sm text-xs tabular-nums">
                              {t('manage:agent_categories.agents', { count: row.agents })}
                            </ItemDescription>
                          </ItemContent>
                          <ItemActions>
                            {/* Focusable at either end of the list: the arrow
                                that just moved a row there keeps the
                                keyboard's place instead of dropping it. */}
                            <Button
                              size="icon-sm"
                              variant="ghost"
                              focusableWhenDisabled
                              disabled={index === 0}
                              data-move={`${row.category_id}:up`}
                              aria-label={t('manage:agent_categories.move_up', { name: row.name })}
                              className="data-disabled:opacity-50"
                              onClick={() => move(index, -1)}
                            >
                              <ArrowUp />
                            </Button>
                            <Button
                              size="icon-sm"
                              variant="ghost"
                              focusableWhenDisabled
                              disabled={index === rows.length - 1}
                              data-move={`${row.category_id}:down`}
                              aria-label={t('manage:agent_categories.move_down', { name: row.name })}
                              className="data-disabled:opacity-50"
                              onClick={() => move(index, 1)}
                            >
                              <ArrowDown />
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={t('manage:agent_categories.rename_button', { name: row.name })}
                              onClick={() =>
                                setRenaming({ id: row.category_id, name: row.name, error: '', saving: false })
                              }
                            >
                              {t('manage:agent_categories.rename')}
                            </Button>
                            <ConsoleDangerButton
                              size="sm"
                              aria-label={t('manage:agent_categories.delete_button', { name: row.name })}
                              confirm={{
                                title: t('manage:agent_categories.delete_title', { name: row.name }),
                                description:
                                  row.agents > 0
                                    ? t('manage:agent_categories.delete_description', {
                                        count: row.agents,
                                      })
                                    : t('manage:agent_categories.delete_unused'),
                                action: t('manage:agent_categories.delete_action'),
                                onConfirm: () => void remove(row),
                              }}
                            >
                              {t('common:delete')}
                            </ConsoleDangerButton>
                          </ItemActions>
                        </>
                      )}
                    </Item>
                  );
                })}
              </ItemGroup>
            </div>
          )}
        </ConsoleCard>
        <ConsoleCard
          title={t('manage:agent_categories.new_title')}
          intro={t('manage:agent_categories.new_intro')}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void create();
            }}
          >
            <ConsoleFieldRow
              label={t('common:name')}
              htmlFor="agent-category-new"
              error={createError || undefined}
            >
              <div className="flex gap-2">
                <ConsoleTextField
                  id="agent-category-new"
                  mono={false}
                  value={newName}
                  invalid={Boolean(createError)}
                  placeholder={t('manage:agent_categories.new_placeholder')}
                  onChange={(value) => {
                    setNewName(value);
                    setCreateError('');
                  }}
                />
                <Button type="submit" disabled={creating || !newName.trim()}>
                  <Plus className="size-4" />
                  {t('manage:agent_categories.add')}
                </Button>
              </div>
            </ConsoleFieldRow>
          </form>
        </ConsoleCard>
      </div>
    </PageShell>
  );
}
