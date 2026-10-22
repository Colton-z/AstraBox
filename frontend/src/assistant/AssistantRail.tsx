import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useSWR from 'swr';
import { ChevronRight, Plus } from 'lucide-react';

import { Avatar, AvatarFallback } from '@/components/ui/avatar';
import {
  SidebarGroup,
  SidebarGroupAction,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
} from '@/components/ui/sidebar';
import { ErrorNote, RAIL_ROW_ACTIVE } from '@/components/shell';
import { keepsLastRead } from '@/hooks/useKeepCurrent';
import { isUnread } from '@/lib/replyNotifications';
import { cn } from '@/lib/utils';
import { ApiError } from '../api';
import type { SessionRecord } from '../types';

import { ensurePrimaryAssistant, listAssistants } from './api';
import type { AssistantRecord } from './types';

const RAIL_REFRESH_INTERVAL_MS = 10_000;

/** The caller's IANA timezone, which a new Assistant keeps time in. */
function browserTimezone(): string | null {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

function initial(assistant: AssistantRecord): string {
  const icon = (assistant.icon ?? '').trim();
  if (icon) return icon;
  return Array.from(assistant.display_name.trim())[0]?.toUpperCase() ?? '?';
}

/** An untitled conversation is named by when it started, in the viewer's locale. */
function startedLabel(iso: string | null | undefined): string {
  const started = iso ? new Date(iso) : null;
  if (!started || Number.isNaN(started.getTime())) return '';
  return started.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/**
 * The caller's primary Assistant, created on first use.
 *
 * Every reader of these keys goes through these hooks, with the fetcher: SWR
 * delivers focus, reconnect and error-retry revalidation to the first hook
 * subscribed to a key, and a reader subscribed without a fetcher would take
 * those events and fetch nothing.
 */
export function usePrimaryAssistant() {
  const { t } = useTranslation();
  // This endpoint resolves the owner's single primary record, so repeated
  // reads use the shared transient-error retry policy despite its POST method.
  return useSWR('assistant:primary', () => ensurePrimaryAssistant({
    display_name: t('shell:assistant_default_name'),
    timezone: browserTimezone(),
  }), { revalidateOnFocus: false });
}

/** The caller's Assistants, once the primary one exists. */
export function useRailAssistants() {
  const primary = usePrimaryAssistant();
  const list = useSWR(primary.data ? 'assistants:rail' : null, listAssistants, {
    refreshInterval: RAIL_REFRESH_INTERVAL_MS,
  });
  return { primary, list };
}

/**
 * The caller's Assistants, at the top of the rail like people in a contact list.
 *
 * Each user has one without setting anything up: the first visit creates it
 * from the deployment's Assistant defaults (POST /assistants/primary). A
 * deployment with no RuntimeProfile for Assistants answers
 * ASSISTANT_DEFAULTS_UNAVAILABLE, and the section is absent rather than an
 * entry that cannot be opened. A row opens the Assistant's main conversation,
 * so it carries that conversation's unread mark. Its earlier conversations,
 * the main ones it has replaced and any others it was given, open under the
 * row; the sessions list below holds Agent conversations only.
 */
export function AssistantRail({
  sessions,
  activeSessionId,
}: {
  sessions: SessionRecord[];
  activeSessionId: string | null;
}) {
  const { t } = useTranslation();
  const { primary, list } = useRailAssistants();
  const unavailable = primary.error instanceof ApiError
    && primary.error.code === 'ASSISTANT_DEFAULTS_UNAVAILABLE';
  const assistants = useMemo(() => {
    const rows = list.data ?? (primary.data ? [primary.data] : []);
    return [...rows].sort((a, b) => {
      if (Boolean(a.primary) !== Boolean(b.primary)) return a.primary ? -1 : 1;
      return String(a.created_at ?? '').localeCompare(String(b.created_at ?? ''));
    });
  }, [list.data, primary.data]);
  const byId = useMemo(() => new Map(sessions.map((s) => [s.session_id, s])), [sessions]);
  const earlierById = useMemo(() => {
    const earlier = new Map<string, SessionRecord[]>();
    const mains = new Set(assistants.map((a) => a.main_session_id).filter(Boolean));
    for (const session of sessions) {
      if (session.session_kind !== 'assistant_chat' || !session.assistant_id) continue;
      if (mains.has(session.session_id)) continue;
      earlier.set(session.assistant_id, [...(earlier.get(session.assistant_id) ?? []), session]);
    }
    for (const rows of earlier.values()) {
      rows.sort((a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
    }
    return earlier;
  }, [assistants, sessions]);
  const [opened, setOpened] = useState<ReadonlySet<string>>(new Set());

  if (unavailable) return null;
  // The list follows other devices and schedules in the background; a refresh
  // that fails while the rows are on screen keeps them, as the sessions list
  // does. A first read that fails is reported.
  const listError = keepsLastRead(list.error, { background: true }, list.data !== undefined)
    ? undefined : list.error;
  const primaryError = keepsLastRead(primary.error, { background: true }, primary.data !== undefined)
    ? undefined : primary.error;
  const error = (primaryError ?? listError) as Error | undefined;

  return (
    <SidebarGroup data-testid="assistant-rail">
      <SidebarGroupLabel className="t-eyebrow h-7 px-2.5">{t('shell:assistants_group')}</SidebarGroupLabel>
      <SidebarGroupAction
        render={<Link to="/manage/assistants/new" />}
        aria-label={t('shell:assistant_new')}
        title={t('shell:assistant_new')}
        className="top-2"
      >
        <Plus />
      </SidebarGroupAction>
      <SidebarGroupContent>
        <SidebarMenu className="gap-0.5">
          {assistants.map((assistant) => {
            const main = assistant.main_session_id ?? null;
            const isActive = main !== null && main === activeSessionId;
            const mainSession = main ? byId.get(main) : undefined;
            const unread = !isActive && mainSession !== undefined && isUnread(mainSession);
            const earlier = earlierById.get(assistant.assistant_id) ?? [];
            const expanded = opened.has(assistant.assistant_id)
              || earlier.some((s) => s.session_id === activeSessionId);
            return (
              <SidebarMenuItem
                key={assistant.assistant_id}
                data-testid="assistant-row"
                data-assistant-id={assistant.assistant_id}
                data-unread={unread || undefined}
              >
                <SidebarMenuButton
                  render={<Link to={`/assistants/${encodeURIComponent(assistant.assistant_id)}`} />}
                  isActive={isActive}
                  tooltip={assistant.display_name}
                  aria-label={assistant.display_name}
                  className={cn('transition-colors duration-150', RAIL_ROW_ACTIVE)}
                >
                  <Avatar size="sm" className="size-5">
                    <AvatarFallback className="text-[11px] font-semibold">{initial(assistant)}</AvatarFallback>
                  </Avatar>
                  <span className={cn('truncate text-13', unread ? 'font-semibold' : 'font-medium')}>
                    {assistant.display_name}
                  </span>
                  {unread && (
                    <span
                      className="ml-auto size-1.5 shrink-0 rounded-full bg-primary group-data-[collapsible=icon]:hidden"
                      role="img"
                      aria-label={t('shell:unread')}
                    />
                  )}
                </SidebarMenuButton>
                {earlier.length > 0 && (
                  <SidebarMenuAction
                    data-testid="assistant-earlier-toggle"
                    aria-expanded={expanded}
                    aria-label={t('shell:assistant_earlier_conversations', { name: assistant.display_name })}
                    title={t('shell:assistant_earlier_conversations', { name: assistant.display_name })}
                    onClick={() => setOpened((current) => {
                      const next = new Set(current);
                      if (next.has(assistant.assistant_id)) next.delete(assistant.assistant_id);
                      else next.add(assistant.assistant_id);
                      return next;
                    })}
                  >
                    <ChevronRight className={cn('transition-transform duration-150', expanded && 'rotate-90')} />
                  </SidebarMenuAction>
                )}
                {expanded && earlier.length > 0 && (
                  <SidebarMenuSub data-testid="assistant-earlier-conversations">
                    {earlier.map((session) => {
                      const current = session.session_id === activeSessionId;
                      const label = session.title || startedLabel(session.created_at);
                      return (
                        <SidebarMenuSubItem key={session.session_id} data-session-id={session.session_id}>
                          <SidebarMenuSubButton
                            render={<Link to={`/sessions/${encodeURIComponent(session.session_id)}`} />}
                            isActive={current}
                            className="text-13"
                          >
                            <span className="truncate">{label}</span>
                            {!current && isUnread(session) && (
                              <span
                                className="ml-auto size-1.5 shrink-0 rounded-full bg-primary"
                                role="img"
                                aria-label={t('shell:unread')}
                              />
                            )}
                          </SidebarMenuSubButton>
                        </SidebarMenuSubItem>
                      );
                    })}
                  </SidebarMenuSub>
                )}
              </SidebarMenuItem>
            );
          })}
          {error && !unavailable && (
            <li className="px-1 py-1 group-data-[collapsible=icon]:hidden">
              <ErrorNote className="text-xs">{error.message}</ErrorNote>
            </li>
          )}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  );
}
