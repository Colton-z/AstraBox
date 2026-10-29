import { ArtifactGallery, ArtifactPage } from './artifacts/ArtifactPages';
import { ConnectionsPage } from './connections/ConnectionsPage';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, Navigate, Route, Routes, useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import i18n from '@/i18n';
import { useTheme } from 'next-themes';
import type { TFunction } from 'i18next';
import useSWR from 'swr';
import useSWRInfinite from 'swr/infinite';
import { keepsLastRead } from './hooks/useKeepCurrent';
import { Archive, Bell, Bot, Check, Hammer, LoaderCircle, MoreHorizontal, Pencil } from 'lucide-react';
import { toast } from 'sonner';

import {
  archiveSession,
  getCurrentUser,
  listSessionsPage,
  renameSession,
} from './api';
import type {
  SessionListPage,
  SessionRecord,
  UserInfo,
} from './types';

import {
  sessionOperationalStatusLabel,
  isTransparentlyRecoverableAgentSession,
} from './utils/format';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
} from '@/components/ui/sidebar';
import { AppShell, AppTopbar, BrandBlock, ErrorNote, PageShell, RAIL_ROW_ACTIVE, SurfaceNav, TruncatingRow } from '@/components/shell';
import { AgentHome } from './components/AgentHome';
import { AgentPage } from './components/AgentPage';
import { StatusPill, toneForSession } from './components/AstraConsole';
import { SessionPage } from './session/SessionPage';
import { SessionTitleInput } from './session/components/SessionTitleInput';
import { AssistantRail } from './assistant/AssistantRail';
import { AssistantMainRoute } from './assistant/AssistantMainRoute';
import { UserMenu } from './components/UserMenu';
import { cn } from '@/lib/utils';
import { isUnread, useNotificationPermission, useReplyNotifications } from '@/lib/replyNotifications';

type ArchivePhase = 'pending' | 'success';

const ARCHIVE_EXIT_ANIMATION_MS = 260;
const RAIL_REFRESH_INTERVAL_MS = 10_000;

function waitForArchiveExitAnimation(): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ARCHIVE_EXIT_ANIMATION_MS);
  });
}

function sourceIcon(s: SessionRecord) {
  if (s.source_type === 'agent' || s.agent_id) return Bot;
  return Hammer;
}

// An untitled conversation is named the way its own header names it.
function sessionDisplayName(s: SessionRecord): string {
  return s.title || i18n.t('chat:page.session_title_fallback', { id: s.session_id.slice(0, 8) });
}

function sessionStatusLabel(s: SessionRecord, t: TFunction): string {
  // Keep the conversation list consistent with the detail header: a per-session
  // agent_chat whose sandbox is gone but re-borrows on the next message reads ready,
  // not hibernating / recovery required (see isTransparentlyRecoverableAgentSession).
  if (isTransparentlyRecoverableAgentSession(s)) return t('shell:status_ready');
  return sessionOperationalStatusLabel(s);
}

function formatShortTime(iso: string | null | undefined, t: TFunction): string {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return t('shell:time_just_now');
    if (diffMin < 60) return t('shell:time_minutes_ago', { count: diffMin });
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return t('shell:time_hours_ago', { count: diffHr });
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return t('shell:time_days_ago', { count: diffDay });
    return d.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' });
  } catch {
    return '';
  }
}

export default function App() {
  const { t } = useTranslation();
  const { theme, setTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const loadMoreSessionsRef = useRef<HTMLDivElement | null>(null);

  const {
    data: sessionPages = [],
    error: sessionsError,
    isLoading: sessionsLoading,
    isValidating: sessionsValidating,
    mutate: mutateSessions,
    size: sessionPageSize,
    setSize: setSessionPageSize,
  } = useSWRInfinite<SessionListPage>(
    (_pageIndex, previousPage) => {
      if (previousPage && !previousPage.has_more) return null;
      const cursor = previousPage?.next_cursor ?? null;
      return ['sessions', cursor] as const;
    },
    ([, cursor]) => listSessionsPage(cursor as string | null),
    // Conversations are created, run and archived by channels, schedules and
    // other devices as well as by this tab, so the rail follows them. It
    // follows them in a hidden tab too: that is when a reply the owner did
    // not wait for has to reach them as a notification.
    { refreshInterval: RAIL_REFRESH_INTERVAL_MS, refreshWhenHidden: true },
  );
  const { data: userInfo, error: userInfoError } = useSWR<UserInfo>('user:current', getCurrentUser);

  const routeLoading = false;
  const [archivePhases, setArchivePhases] = useState<Record<string, ArchivePhase>>({});
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  // Choosing Rename closes the row's menu, which would hand focus back to its
  // trigger and take it from the title field that replaces the row.
  const renameChosen = useRef(false);
  const [actionError, setActionError] = useState('');
  const sessions = useMemo(
    () => sessionPages.flatMap((page) => page.sessions),
    [sessionPages],
  );
  // An Assistant's conversations belong to its row in the Assistants
  // section: the main one is the row itself and the earlier ones open under
  // it, so this list holds Agent conversations only.
  const railSessions = useMemo(
    () => sessions.filter((s) => s.session_kind !== 'assistant_chat'),
    [sessions],
  );
  const hasMoreSessions = Boolean(sessionPages[sessionPages.length - 1]?.has_more);
  const activeSessionId = location.pathname.startsWith('/sessions/')
    ? decodeURIComponent(location.pathname.slice('/sessions/'.length).split('/')[0] ?? '')
    : null;
  const notifications = useNotificationPermission();
  const openSession = useCallback((sessionId: string) => navigate(`/sessions/${sessionId}`), [navigate]);
  useReplyNotifications(sessions, activeSessionId, {
    title: sessionDisplayName,
    body: t('shell:new_reply'),
    open: openSession,
  });
  const loadingMoreSessions = sessionsValidating && sessionPageSize > 0;
  const error = useMemo(() => {
    if (actionError) return actionError;
    const railError = keepsLastRead(sessionsError, { background: true }, sessionPages.length > 0)
      ? null : sessionsError;
    const identityError = keepsLastRead(userInfoError, { background: true }, userInfo !== undefined)
      ? null : userInfoError;
    const readError = railError ?? identityError;
    return readError ? (readError as Error).message : '';
  }, [actionError, sessionPages, sessionsError, userInfo, userInfoError]);


  useEffect(() => {
    const node = loadMoreSessionsRef.current;
    if (!node || !hasMoreSessions || sessionsLoading || sessionsValidating) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      void setSessionPageSize((size) => size + 1);
    }, { rootMargin: '120px' });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMoreSessions, sessionsLoading, sessionsValidating, setSessionPageSize]);

  const refreshOverview = useCallback(async () => {
    await Promise.allSettled([mutateSessions()]);
  }, [mutateSessions]);

  const onAgentConversationCreated = (sessionId: string) => {
    void refreshOverview();
    navigate(`/sessions/${sessionId}`);
  };

  const stopRenaming = () => {
    renameChosen.current = false;
    setRenamingSessionId(null);
  };

  // The row shows the new title at once; the list's next read is the server's
  // answer, so a refused title goes back to the stored one.
  const rename = async (sessionId: string, title: string) => {
    stopRenaming();
    await mutateSessions((currentPages) => currentPages?.map((page) => ({
      ...page,
      sessions: page.sessions.map((item) => (item.session_id === sessionId ? { ...item, title } : item)),
    })), { revalidate: false });
    try {
      await renameSession(sessionId, title);
    } catch (err) {
      toast.error(t('shell:session_rename_failed'), { description: (err as Error).message });
    }
    void mutateSessions();
  };

  const archive = async (session: SessionRecord) => {
    const sessionId = session.session_id;
    if (archivePhases[sessionId]) return;
    setArchivePhases((current) => ({ ...current, [sessionId]: 'pending' }));
    setActionError('');
    try {
      await archiveSession(sessionId);
      setArchivePhases((current) => ({ ...current, [sessionId]: 'success' }));
      if (location.pathname === `/sessions/${sessionId}`) {
        navigate('/');
      }
      await waitForArchiveExitAnimation();
      await mutateSessions((currentPages) => {
        if (!currentPages) return currentPages;
        return currentPages.map((page) => ({
          ...page,
          sessions: page.sessions.filter((item) => item.session_id !== sessionId),
        }));
      }, { revalidate: false });
      void mutateSessions();
    } catch (err) {
      setActionError((err as Error).message);
    } finally {
      setArchivePhases((current) => {
        const next = { ...current };
        delete next[sessionId];
        return next;
      });
    }
  };

  // The session view carries its own header, so it takes no top bar. Keeping
  // that decision here rather than inside the bar is what lets AppShell treat
  // the bar as a slot instead of a special case.
  const isSession = location.pathname.startsWith('/sessions/');
  const crumb = t('shell:tab_agents');

  return (
    <AppShell
      sidebarLabel={t('shell:primary_navigation')}
      skipLabel={t('shell:skip_to_content')}
      sidebarHeader={
        <>
          <BrandBlock />
          <SurfaceNav />
        </>
      }
      sidebarContent={
        <>
        <AssistantRail sessions={sessions} activeSessionId={activeSessionId} />
        <SidebarGroup data-testid="sessions-page">
            <SidebarGroupLabel className="t-eyebrow h-7 px-2.5">
              {t('shell:sessions_group')}
              {/* No count until one is known. A deep link into a conversation
                  renders its transcript while this list is still in flight, and
                  a badge reading 0 beside a loading row asserts an answer the
                  page does not have — next to a session the reader is looking
                  at. */}
              {!sessionsLoading && (
                <SidebarMenuBadge className="tabular-nums">{railSessions.length}</SidebarMenuBadge>
              )}
              {notifications.permission === 'default' && (
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <button
                        type="button"
                        className="ml-auto mr-6 rounded p-0.5 text-sidebar-foreground/55 hover:text-sidebar-foreground"
                        aria-label={t('shell:notifications_enable')}
                        onClick={notifications.request}
                      />
                    }
                  >
                    <Bell className="size-3.5" />
                  </TooltipTrigger>
                  <TooltipContent>{t('shell:notifications_enable')}</TooltipContent>
                </Tooltip>
              )}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              {/* A rounded fill needs room to read as its own object: at gap-0
                  each row's bottom edge is the next row's top edge, so two
                  filled rows merge into one shape with pinched corners. */}
              <SidebarMenu className="gap-0.5" data-testid="sessions-table">
                {railSessions.map((s) => {
                  const Icon = sourceIcon(s);
                  const isActive = location.pathname === `/sessions/${s.session_id}`;
                  const unread = !isActive && isUnread(s);
                  const archivePhase = archivePhases[s.session_id];
                  const ArchiveStateIcon = archivePhase === 'pending' ? LoaderCircle : Check;
                  const actionLabel = archivePhase === 'pending'
                    ? t('shell:session_archiving')
                    : archivePhase === 'success'
                      ? t('shell:session_archived')
                      : t('shell:session_more_actions');
                  const renaming = renamingSessionId === s.session_id;
                  return (
                    <SidebarMenuItem
                      key={s.session_id}
                      data-testid="session-row"
                      data-session-id={s.session_id}
                      data-unread={unread || undefined}
                      className={cn(
                        'max-h-16 overflow-hidden transition-[max-height,opacity,transform] duration-200 ease-out',
                        archivePhase === 'success' && 'max-h-0 -translate-x-1 opacity-0',
                      )}
                    >
                      {/* The field takes the link's place: inside a link, every
                          click on the text being edited would open the
                          conversation. */}
                      {renaming ? (
                        <div className="flex items-center gap-2 p-1.5">
                          <Icon className="size-4 shrink-0 text-sidebar-foreground/55" />
                          <SessionTitleInput
                            title={sessionDisplayName(s)}
                            onSubmit={(title) => { void rename(s.session_id, title); }}
                            onCancel={stopRenaming}
                          />
                        </div>
                      ) : (
                      /* Selected reads the same here as in every other rail —
                         RAIL_ROW_ACTIVE owns that, and owns why. */
                      <SidebarMenuButton
                        render={<Link to={`/sessions/${s.session_id}`} />}
                        isActive={isActive}
                        tooltip={sessionDisplayName(s)}
                        aria-label={sessionDisplayName(s)}
                        className={cn(
                          // Two lines and a top-aligned icon is the only thing
                          // this row needs that a one-line rail row does not.
                          // Both insets come from the kit — the right one from
                          // `group-has-data-[sidebar=menu-action]` — because a
                          // hand-set `pl-3` puts this rail's rows 4px off the
                          // console's.
                          'h-auto items-start py-2 transition-colors duration-150 group-data-[collapsible=icon]:items-center',
                          RAIL_ROW_ACTIVE,
                        )}
                      >
                        <Icon className="mt-0.5 size-4 shrink-0 text-sidebar-foreground/55 group-data-[collapsible=icon]:mt-0" />
                        {/*
                          The explicit track is load-bearing. Without it the
                          grid's implicit track takes its minimum from its
                          content, and the meta line below — whose badge does
                          not wrap — pushes it to 227px inside a 159px button.
                          The title then solves its truncation against 227px
                          and paints the ellipsis 68px outside the visible
                          area, so a long status silently cuts the title while a
                          short one truncates it properly.
                        */}
                        <span className="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)] gap-1 text-left leading-tight group-data-[collapsible=icon]:hidden">
                          <span className="flex min-w-0 items-center gap-1.5">
                            {unread && (
                              <span
                                className="size-1.5 shrink-0 rounded-full bg-primary"
                                role="img"
                                aria-label={t('shell:unread')}
                              />
                            )}
                            <span className={cn('truncate text-13', unread ? 'font-semibold' : 'font-medium')}>
                              {sessionDisplayName(s)}
                            </span>
                          </span>
                          {/*
                            The status is the only part of this line without a
                            bound — the id is always 8 characters and the
                            relative time is short — so it is the one that
                            gives way.
                          */}
                          {/* The id is mono because it is what a reader
                              pastes into a log query. The time beside it is
                              not: mono on a relative time is the "this is
                              data" reading docs/frontend-design.md §6 rules
                              out, and tabular-nums is what lines digits up. */}
                          {/* On the selected row the id and time sit on the
                              astra tint, where the muted grey measures 4.46:1;
                              they take the row's astra text step, as every
                              label on that tint does (styles.css, --astra-fg). */}
                          <TruncatingRow
                            className="gap-1.5"
                            trail={
                              <span className="flex items-center gap-1.5 text-muted-foreground in-data-active:text-astra-fg">
                                <span className="t-mono text-10">{s.session_id.slice(0, 8)}</span>
                                <span className="text-10 tabular-nums">{formatShortTime(s.updated_at || s.created_at, t)}</span>
                              </span>
                            }
                          >
                            <StatusPill tone={toneForSession(s)} state={s.state} truncate>{sessionStatusLabel(s, t)}</StatusPill>
                          </TruncatingRow>
                        </span>
                      </SidebarMenuButton>
                      )}
                      {/* Kept mounted while the field is open, so the menu that
                          closed to open it still has the trigger it belongs to. */}
                      <DropdownMenu>
                        <DropdownMenuTrigger
                          disabled={Boolean(archivePhase)}
                          render={
                            <SidebarMenuAction
                              showOnHover
                              aria-label={actionLabel}
                              className={cn(
                                'right-1.5 top-1/2 size-6 -translate-y-1/2 rounded-md bg-transparent text-sidebar-foreground/45 opacity-0 shadow-none transition-[opacity,transform,background-color,color] duration-150 ease-out hover:bg-sidebar-accent hover:text-sidebar-accent-foreground hover:shadow-none disabled:pointer-events-none disabled:opacity-100 peer-data-[size=default]/menu-button:top-1/2 peer-data-[size=lg]/menu-button:top-1/2 peer-data-[size=sm]/menu-button:top-1/2',
                                archivePhase === 'pending' && 'opacity-100 text-muted-foreground',
                                archivePhase === 'success' && 'bg-transparent text-sidebar-accent-foreground opacity-100',
                                renaming && 'hidden',
                              )}
                            />
                          }
                        >
                          {archivePhase
                            ? <ArchiveStateIcon className={cn('size-3.5', archivePhase === 'pending' && 'animate-spin')} />
                            : <MoreHorizontal className="size-3.5" />}
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" finalFocus={() => !renameChosen.current}>
                          <DropdownMenuItem
                            onClick={() => {
                              renameChosen.current = true;
                              setRenamingSessionId(s.session_id);
                            }}
                          >
                            <Pencil />
                            {t('shell:session_rename')}
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => { void archive(s); }}>
                            <Archive />
                            {t('shell:session_archive')}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </SidebarMenuItem>
                  );
                })}
                {hasMoreSessions && (
                  <div ref={loadMoreSessionsRef} className="px-2 py-2">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="w-full text-xs"
                      disabled={loadingMoreSessions}
                      onClick={() => { void setSessionPageSize((size) => size + 1); }}
                    >
                      {loadingMoreSessions ? t('common:loading') : t('shell:session_load_more')}
                    </Button>
                  </div>
                )}
                {railSessions.length === 0 && !sessionsError && (
                  <div data-testid="sessions-empty-hero" className="px-2 py-4 text-center text-xs text-muted-foreground group-data-[collapsible=icon]:hidden">
                    {sessionsLoading ? t('common:loading') : t('shell:session_empty')}
                  </div>
                )}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </>
      }
      sidebarFooter={<UserMenu userInfo={userInfo} theme={theme} setTheme={setTheme} />}
      topbar={isSession || location.pathname.startsWith('/agents/') || location.pathname.startsWith('/artifacts') || location.pathname.startsWith('/connections') ? undefined : <AppTopbar crumbs={[{ label: crumb }]} trailLabel={t('shell:breadcrumb_navigation')} />}
      banner={
        error ? (
          /* The margin is this strip's own because `SidebarInset` stacks the
             bar, the banner and the content well without gapping them: `m-4`
             is what holds the strip off the bar and inside the well's edges,
             and `mb-0` keeps it flush with the well, which pads itself. */
          <ErrorNote title={t('shell:request_failed')} className="m-4 mb-0">
            {error}
          </ErrorNote>
        ) : undefined
      }
    >
          <Routes>
            {/* Agents is the user-facing entry: pick a configured Agent from
                /api/v1/agents → start a Session.
                A sandbox is provisioned per session and is ephemeral, so it is not a
                user-facing tab. */}
            <Route path="/" element={<Navigate to="/agents" replace />} />
            <Route
              path="/agents"
              element={
                routeLoading
                  ? <div className="flex h-full items-center justify-center text-muted-foreground text-sm">{t('common:loading')}</div>
                  : <HomePage onAgentConversationCreated={onAgentConversationCreated} />
              }
            />
            {/* `/sandbox` remains an entry alias for existing links. */}
            <Route path="/sandbox" element={<Navigate to="/agents" replace />} />
            <Route path="/assistants" element={<AssistantMainRoute onOpened={refreshOverview} />} />
            <Route path="/artifacts" element={<ArtifactGallery />} />
            <Route path="/connections" element={<ConnectionsPage />} />
            <Route path="/artifacts/:id" element={<ArtifactPage />} />
            <Route path="/sessions/:sessionId" element={<SessionPage onSessionChanged={refreshOverview} />} />
            <Route path="/v1/sessions/:sessionId" element={<Navigate to="/" replace />} />
            <Route path="/agents/:agentId" element={<AgentPage onConversationCreated={onAgentConversationCreated} />} />
            <Route path="/assistants/:assistantId" element={<AssistantMainRoute onOpened={refreshOverview} />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
    </AppShell>
  );
}

function HomePage({
  onAgentConversationCreated,
}: {
  onAgentConversationCreated: (sessionId: string) => void;
}) {
  // `wide`, not `narrow`. The narrow measure is a line length — it exists so
  // prose does not run to 130 characters. This surface is a grid of cards to
  // scan, and a reading width leaves 512px of the window unused at 1920 and
  // half of it at 2560, while the cards stay 352px wide either way.
  return (
    <PageShell measure="wide">
      <AgentHome onConversationCreated={onAgentConversationCreated} />
    </PageShell>
  );
}
