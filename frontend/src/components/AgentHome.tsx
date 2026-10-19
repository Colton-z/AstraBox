import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Star, X } from 'lucide-react';
import { toast } from 'sonner';
import useSWR from 'swr';

import { Ellipsis, EmptyState, ErrorNote, TruncatingRow } from '@/components/shell';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Toggle } from '@/components/ui/toggle';
import { cn } from '@/lib/utils';
import { ConsoleSearch, ConsoleToolbar } from '@/manage/console/ConsoleControls';
import { agentDisplayName, agentModel } from '@/manage/agentConfig';

import {
  MODULE_BASE,
  getCurrentUser,
  listAgentCategories,
  listAgents,
  setAgentFavorite,
  startAgentConversation,
} from '../api';
import type { AgentCategory, AgentConfig, UserInfo } from '../types';
import { keepsLastRead, useKeepCurrent, type ReloadContext } from '@/hooks/useKeepCurrent';
import {
  ALL_TAB,
  FAVORITES_TAB,
  agentSearchFields,
  agentTags,
  agentsInTab,
  categoriesWithAgents,
  categoryTab,
  initialHomeTab,
  rankAgents,
  readRememberedTab,
  rememberTab,
} from './agentCatalog';

function agentStateLabel(state: string, t: TFunction): string {
  return ({
    ACTIVE: t('agents:state_active'),
    PROVISIONING: t('agents:state_provisioning'),
    HIBERNATING: t('agents:state_hibernating'),
  } as Record<string, string>)[state] ?? state;
}

/**
 * The user-facing agent picker: the configured agents, and one click to open a
 * conversation on one.
 *
 * There is no "run a bare harness" entry here on purpose — every conversation
 * belongs to an agent (docs/domain-model.md), so an empty list is a genuine
 * "nothing has been configured yet" and points at the management console rather
 * than offering a nameless session.
 *
 * The Agents are browsed by tab: the reader's favorites, all of them, and one
 * tab per administrator-defined category that holds an Agent they can see. A
 * search and a tag picked off a card narrow the open tab together. The page
 * opens on the tab this reader last chose, and otherwise on their favorites
 * when they have any.
 */
export function AgentHome({
  onConversationCreated,
}: {
  onConversationCreated: (sessionId: string) => void;
}) {
  const { t } = useTranslation();
  const { data: user, error: userError } = useSWR<UserInfo>('user:current', getCurrentUser);
  const userId = user?.user_id;
  const [agents, setAgents] = useState<AgentConfig[] | null>(null);
  const [categories, setCategories] = useState<AgentCategory[] | null>(null);
  const [error, setError] = useState('');
  const [startingId, setStartingId] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const loadedOnce = useRef(false);
  // A favorite toggle answers before its write does, so a second press on the
  // same star waits for the first: two writes in flight could land in either
  // order, and the rollback of one would undo the other.
  const favoriteWrites = useRef(new Set<string>());

  const refresh = useCallback(async (context?: ReloadContext) => {
    try {
      const [nextAgents, nextCategories] = await Promise.all([
        listAgents(),
        listAgentCategories(),
      ]);
      setAgents(nextAgents);
      setCategories(nextCategories);
      loadedOnce.current = true;
      setError('');
    } catch (e) {
      if (keepsLastRead(e, context, loadedOnce.current)) return;
      setError((e as Error).message);
      setAgents([]);
      setCategories([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useKeepCurrent(refresh);

  const shownCategories = useMemo(
    () => categoriesWithAgents(categories ?? [], agents ?? []),
    [categories, agents],
  );
  const tabs = useMemo(
    () => [FAVORITES_TAB, ALL_TAB, ...shownCategories.map((c) => categoryTab(c.category_id))],
    [shownCategories],
  );
  const categoryNames = useMemo(
    () => new Map((categories ?? []).map((c) => [c.category_id, c.name])),
    [categories],
  );

  const ready = agents !== null && categories !== null && (user !== undefined || userError !== undefined);
  // Chosen once, from everything it depends on, so a star pressed later does
  // not move the reader to another tab. Set during render rather than in an
  // effect, so the page never paints a tab it is about to leave.
  if (ready && tab === null) {
    setTab(initialHomeTab({
      remembered: readRememberedTab(userId),
      tabs,
      favorites: agents.filter((agent) => agent.favorited === true).length,
    }));
  }

  const startConversationOn = async (agentId: string) => {
    setStartingId(agentId);
    setError('');
    try {
      const created = await startAgentConversation(agentId);
      onConversationCreated(created.session_id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setStartingId(null);
    }
  };

  const toggleFavorite = async (agent: AgentConfig) => {
    const id = agent.agent_id;
    if (favoriteWrites.current.has(id)) return;
    favoriteWrites.current.add(id);
    const next = agent.favorited !== true;
    const show = (favorited: boolean) =>
      setAgents((current) =>
        current && current.map((a) => (a.agent_id === id ? { ...a, favorited } : a)),
      );
    show(next);
    try {
      show((await setAgentFavorite(id, next)).favorited);
    } catch (e) {
      show(!next);
      toast.error(
        t(next ? 'agents:favorite_add_failed' : 'agents:favorite_remove_failed', {
          name: agentDisplayName(agent),
        }),
        { description: (e as Error).message },
      );
    } finally {
      favoriteWrites.current.delete(id);
    }
  };

  if (!ready || tab === null) {
    return <p className="text-sm text-muted-foreground">{t('agents:home_loading')}</p>;
  }

  // A chosen category that has since lost its tab (deleted, or left without an
  // Agent this reader can see) leaves the reader on every Agent.
  const activeTab = tabs.includes(tab) ? tab : ALL_TAB;
  const fieldsOf = (agent: AgentConfig) => agentSearchFields(agent, categoryNames);

  const tabBody = (value: string) => {
    const inTab = agentsInTab(agents, value);
    // Only the favorites can be an empty tab: a category without an Agent has
    // no tab, and every Agent is in All.
    if (inTab.length === 0) {
      return (
        <TabEmpty
          icon={<Star className="size-5" />}
          title={t('agents:favorites_empty')}
          hint={t('agents:favorites_empty_hint')}
        />
      );
    }
    const tagged = tagFilter ? inTab.filter((agent) => agentTags(agent).includes(tagFilter)) : inTab;
    const shown = rankAgents(tagged, query, fieldsOf);
    if (shown.length === 0) {
      return <TabEmpty title={t('agents:no_match')} hint={t('agents:no_match_hint')} />;
    }
    return (
      // Columns follow the space rather than a breakpoint ladder that stops at
      // three: `auto-fill` adds a column whenever one fits. Under
      // `xl:grid-cols-3` a 2560px window shows the same three cards as a
      // 1440px one.
      //
      // The 288px floor is measured, not picked. At 1280 the page column is
      // 976px inside its gutter, and a 320px floor fits only two columns there
      // — narrower than the three a breakpoint ladder gives. 288 keeps those
      // three and still yields four at 1920.
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(288px,1fr))]">
        {shown.map((agent) => (
          <AgentCard
            key={agent.agent_id}
            agent={agent}
            starting={startingId === agent.agent_id}
            onStart={() => void startConversationOn(agent.agent_id)}
            onToggleFavorite={() => void toggleFavorite(agent)}
            onPickTag={setTagFilter}
          />
        ))}
      </div>
    );
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col space-y-6">
      <div className="space-y-1.5">
        <h1 className="t-h1-tight text-2xl">{t('agents:home_title')}</h1>
        <p className="text-sm text-muted-foreground">{t('agents:home_subtitle')}</p>
      </div>
      {error && <ErrorNote title={t('agents:action_failed')}>{error}</ErrorNote>}
      {/*
        An empty list is a state of the page, not a card in a grid of one.
        Inside the grid it keeps the grid's height — the message ends around
        y=200 with ~590px of bare background under it — and takes a single
        column, so it reads as one lonely card rather than as "there is nothing
        here". Nor does it get tabs or a search: there is nothing to narrow.
      */}
      {agents.length === 0 ? (
        // The frame and the centring stay on the wrapper: the shared empty
        // state carries the parts, not the surface they stand on, and the
        // dashed border it does carry has no width to draw with.
        <div data-testid="agents-empty" className="flex flex-1 items-center justify-center rounded-xl bg-card ring-1 ring-foreground/10">
          <EmptyState
            title={t('agents:home_empty')}
            hint={t('agents:home_empty_hint')}
            action={
              /* The console is a destination, so this borrows the button's
                 shape through `buttonVariants` instead of being a `Button`:
                 `@base-ui/react/button` imposes button semantics on whatever it
                 renders — `role="button"` on a non-native element — which its
                 own documentation rules out for an `<a>`. */
              <a className={buttonVariants({ variant: 'outline' })} href={`${MODULE_BASE}/manage/agents`}>
                {t('agents:home_empty_cta')}
              </a>
            }
          />
        </div>
      ) : (
        <Tabs
          value={activeTab}
          onValueChange={(value, details) => {
            // Only the reader's own choice is remembered, never a fallback.
            if (details.reason !== 'none' || typeof value !== 'string') return;
            setTab(value);
            rememberTab(userId, value);
          }}
          className="flex min-h-0 flex-1 flex-col gap-4"
        >
          {/* Content-sized tabs on one scrolling line: the categories are the
              administrator's, so their number and the length of their names
              are not this page's to know, and an equal split of the strip
              would spill a long name over its neighbour. */}
          <TabsList
            variant="line"
            aria-label={t('agents:tabs_label')}
            className="w-full shrink-0 justify-start gap-0 overflow-x-auto border-b border-border bg-transparent px-0"
          >
            <TabsTrigger value={FAVORITES_TAB} className="flex-none px-3">
              {t('agents:tab_favorites')}
            </TabsTrigger>
            <TabsTrigger value={ALL_TAB} className="flex-none px-3">
              {t('common:all')}
            </TabsTrigger>
            {shownCategories.map((category) => (
              <TabsTrigger
                key={category.category_id}
                value={categoryTab(category.category_id)}
                className="flex-none px-3"
              >
                {category.name}
              </TabsTrigger>
            ))}
          </TabsList>
          <ConsoleToolbar>
            <ConsoleSearch
              value={query}
              onChange={setQuery}
              total={agents.length}
              placeholder={t('agents:search_placeholder')}
            />
            {tagFilter && (
              <Badge
                variant="secondary"
                className="h-6 cursor-pointer gap-1 pr-1.5 hover:bg-muted"
                render={
                  <button
                    type="button"
                    aria-label={t('agents:tag_filter_clear', { tag: tagFilter })}
                    onClick={() => setTagFilter(null)}
                  />
                }
              >
                #{tagFilter}
                <X aria-hidden />
              </Badge>
            )}
          </ConsoleToolbar>
          {/* One panel per tab, inside the same root as its tab, so each
              tab's `aria-controls` names a panel that exists; only the
              selected one is mounted. */}
          {tabs.map((value) => (
            <TabsContent key={value} value={value} className="flex flex-col">
              {tabBody(value)}
            </TabsContent>
          ))}
        </Tabs>
      )}
    </div>
  );
}

/** A tab with nothing to show, framed like the page's own empty state. */
function TabEmpty({
  icon,
  title,
  hint,
}: {
  icon?: React.ReactNode;
  title: React.ReactNode;
  hint: React.ReactNode;
}) {
  return (
    <div className="flex flex-1 items-center justify-center rounded-xl bg-card ring-1 ring-foreground/10">
      <EmptyState icon={icon} title={title} hint={hint} />
    </div>
  );
}

function AgentCard({
  agent,
  starting,
  onStart,
  onToggleFavorite,
  onPickTag,
}: {
  agent: AgentConfig;
  starting: boolean;
  onStart: () => void;
  onToggleFavorite: () => void;
  onPickTag: (tag: string) => void;
}) {
  const { t } = useTranslation();
  const name = agentDisplayName(agent);
  const tags = agentTags(agent);
  const favorited = agent.favorited === true;
  const unavailable = agent.can_start_conversation === false;
  return (
    <Card
      data-testid="agent-option"
      data-agent-name={agent.name}
      className="group flex h-full flex-col gap-0 overflow-hidden py-0 shadow-none transition-colors duration-150 hover:border-primary/40"
    >
      <CardHeader className="border-b bg-muted/30 py-4">
        {/*
          The badge and the star keep their size and the text gives way.
          Written as a plain flex row the text block does not shrink below its
          own content — it pushes the row 366px past the card's edge, where the
          card's `overflow-hidden` cuts the name mid-letter and takes the badge
          with it.
        */}
        <TruncatingRow
          className="items-start gap-3"
          trail={
            <div className="flex items-center gap-1.5">
              <Badge
                variant={!unavailable && agent.state === 'ACTIVE' ? 'secondary' : 'outline'}
                className="t-mono text-10 font-normal"
              >
                {unavailable
                  ? t('agents:home_unavailable') : agentStateLabel(String(agent.state || ''), t)}
              </Badge>
              {/* The fill carries the state, not a hue: a filled star and an
                  outline one stay distinct in greyscale
                  (docs/frontend-design.md §7). */}
              <Toggle
                size="sm"
                pressed={favorited}
                onPressedChange={onToggleFavorite}
                aria-label={t('agents:favorite_label', { name })}
                className="px-0 text-muted-foreground aria-pressed:text-foreground"
              >
                <Star className={cn(favorited && 'fill-current')} />
              </Toggle>
            </div>
          }
        >
          <div className="space-y-1">
            <CardTitle className="tracking-[-0.01em]">
              <Ellipsis>{name}</Ellipsis>
            </CardTitle>
            {/* The model is the agent's defining property, so it is what the
                card names underneath — not an internal id. */}
            <CardDescription className="t-mono text-11">
              <Ellipsis>{agentModel(agent)}</Ellipsis>
            </CardDescription>
          </div>
        </TruncatingRow>
      </CardHeader>
      {/* The content column grows and the action sits at its foot, so
          every card in a row ends on the same line. Descriptions run
          one to three lines, so with the button following the text
          directly no two buttons in a row line up. */}
      <CardContent className="flex flex-1 flex-col gap-4 py-4">
        {/* Descriptions distinguish agents in the picker. An empty
            description stays empty because repeating the adjacent action
            label would not help readers choose between cards. The clamp
            prevents one long description from setting the row height;
            flex space keeps the action buttons aligned. */}
        <p className="t-copy line-clamp-3 flex-1 text-muted-foreground">
          {agent.description?.trim() || ''}
        </p>
        {tags.length > 0 && (
          <ul className="flex flex-wrap gap-1.5" aria-label={t('agents:tags_label')}>
            {tags.map((tag) => (
              <li key={tag}>
                <Badge
                  variant="outline"
                  className="cursor-pointer font-normal hover:bg-muted"
                  render={
                    <button
                      type="button"
                      aria-label={t('agents:filter_by_tag', { tag })}
                      onClick={() => onPickTag(tag)}
                    />
                  }
                >
                  #{tag}
                </Badge>
              </li>
            ))}
          </ul>
        )}
        {/* Quiet, not accent. A picker has as many of these as it has
            agents, and five accent buttons on one screen say that five
            things are the primary action — which is the same as saying
            none is (docs/frontend-design.md §7). The card's own hover
            border is what marks the one under the pointer. */}
        <Button
          data-testid="agent-start"
          variant="outline"
          disabled={starting || unavailable}
          onClick={onStart}
          className="w-full justify-center"
        >
          {starting ? t('agents:home_starting') : t('agents:home_start')}
        </Button>
      </CardContent>
    </Card>
  );
}
