import { useEffect, useRef, useState } from 'react';
import { Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import {
  Activity,
  Bot,
  Box,
  Boxes,
  Container,
  ExternalLink,
  Gauge,
  KeyRound,
  Network,
  Plug,
  ScrollText,
  ShieldCheck,
  TriangleAlert,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useTheme } from 'next-themes';
import useSWR from 'swr';

import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
} from '@/components/ui/sidebar';
import {
  AppShell,
  AppTopbar,
  BrandBlock,
  ErrorNote,
  RAIL_ROW_ACTIVE,
  SurfaceNav,
} from '@/components/shell';
import { UserMenu } from '@/components/UserMenu';
import { adminListIntegrations, adminNavigationSummary } from '@/api';
import type { AdminIntegratedService, AdminNavigationSummary } from '@/types';

import AgentsListPage from './AgentsListPage';
import AgentCreatePage from './AgentCreatePage';
import DeploymentCreatePage from './DeploymentCreatePage';
import AssistantCreatePage from './AssistantCreatePage';
import EnvironmentCreatePage from './EnvironmentCreatePage';
import AgentDetailPage from './AgentDetailPage';
import DeploymentsListPage from './DeploymentsListPage';
import McpTokensPage from './McpTokensPage';
import DeploymentDetailPage from './DeploymentDetailPage';
import AssistantsListPage from './AssistantsListPage';
import AssistantDetailPage from './AssistantDetailPage';
import SessionsListPage from './SessionsListPage';
import SessionDetailPage from './SessionDetailPage';
import AgentSessionsPage from './AgentSessionsPage';
import EnvironmentsListPage from './EnvironmentsListPage';
import EnvironmentDetailPage from './EnvironmentDetailPage';
import CredentialsListPage from './CredentialsListPage';
import CredentialVaultPage from './CredentialVaultPage';
import SandboxesListPage from './SandboxesListPage';
import SandboxDetailPage from './SandboxDetailPage';
import SystemPage from './SystemPage';
import ErrorsListPage from './ErrorsListPage';
import LogsPage from './LogsPage';
import { RecordCrumbProvider } from './console';
import {
  MANAGE_NAV_COUNT_KEYS,
  MANAGE_NAV_SUMMARY_KEY,
  type ManageNavCount,
} from './navCounts';

type NavCounts = { agents?: number; sessions?: number; environments?: number };

/**
 * The rail, grouped by what the reader came to do.
 *
 * Ten items under one heading is a list, not a structure: an operator hunting
 * "why did that run fail" must scan the same ten as one authoring an agent.
 * Three core groups follow the order work actually moves — set it up, watch it
 * run, check the deployment itself. A fourth group appears only when the
 * backend says an integrated service has a browser management surface.
 *
 * The last group reads the running process rather than the database, which is
 * why it carries no counts: there is nothing stored to count. Sandboxes carry
 * none either — the backend pages its inventory, and one page's length is not
 * the total, so a number here would be a fabricated one.
 */
type NavItem = {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  match: string;
  /** Opens a capability owned by an integrated service rather than a React route. */
  external?: boolean;
  /** Provider name supplied by the deployment capability contract. */
  integrationName?: string;
  /** Which quiet count to show, for the resources that have one to show. */
  count?: keyof NavCounts;
};

const NAV_GROUPS: { titleKey: string; items: NavItem[] }[] = [
  {
    titleKey: 'manage:nav.group_configure',
    items: [
      { to: '/manage/agents', labelKey: 'manage:nav.agents', icon: Bot, match: '/manage/agents', count: 'agents' as const },
      { to: '/manage/assistants', labelKey: 'manage:nav.assistants', icon: Users, match: '/manage/assistants' },
      { to: '/manage/environments', labelKey: 'manage:nav.environments', icon: Box, match: '/manage/environments', count: 'environments' as const },
      { to: '/manage/credentials', labelKey: 'manage:nav.credentials', icon: KeyRound, match: '/manage/credentials' },
      { to: '/manage/deployments', labelKey: 'manage:nav.deployments', icon: Boxes, match: '/manage/deployments' },
      { to: '/manage/mcp-tokens', labelKey: 'manage:nav.mcp_tokens', icon: Plug, match: '/manage/mcp-tokens' },
    ],
  },
  {
    titleKey: 'manage:nav.group_operate',
    items: [
      { to: '/manage/sessions', labelKey: 'manage:nav.sessions', icon: Activity, match: '/manage/sessions', count: 'sessions' as const },
      { to: '/manage/sandboxes', labelKey: 'manage:nav.sandboxes', icon: Container, match: '/manage/sandboxes' },
    ],
  },
  {
    titleKey: 'manage:nav.group_system',
    items: [
      { to: '/manage/system', labelKey: 'manage:nav.system', icon: Gauge, match: '/manage/system' },
      { to: '/manage/errors', labelKey: 'manage:nav.errors', icon: TriangleAlert, match: '/manage/errors' },
      { to: '/manage/logs', labelKey: 'manage:nav.logs', icon: ScrollText, match: '/manage/logs' },
    ],
  },
];

const INTEGRATION_NAV: Record<AdminIntegratedService['category'], Pick<NavItem, 'labelKey' | 'icon'>> = {
  identity: { labelKey: 'manage:nav.integration_identity', icon: ShieldCheck },
  api_access: { labelKey: 'manage:nav.integration_api_access', icon: KeyRound },
  model_gateway: { labelKey: 'manage:nav.integration_model_gateway', icon: Network },
};

export function ManageSidebar() {
  const { pathname } = useLocation();
  const { t } = useTranslation();
  const [integrations, setIntegrations] = useState<AdminIntegratedService[]>([]);
  const [integrationsError, setIntegrationsError] = useState(false);

  const summary = useSWR<AdminNavigationSummary>(
    MANAGE_NAV_SUMMARY_KEY,
    adminNavigationSummary,
  );
  // Moving between pages is the reader's own gesture, and the counts they
  // land next to should be as current as the page they land on: a record
  // created on the page just left is otherwise not counted until reload.
  const revalidateSummary = summary.mutate;
  const countedAt = useRef(pathname);
  useEffect(() => {
    if (countedAt.current === pathname) return;
    countedAt.current = pathname;
    void revalidateSummary();
  }, [pathname, revalidateSummary]);
  // An open list publishes the total from the same response that rendered its
  // rows. Inactive collections use one count-only summary instead of loading
  // three catalogs (and one otherwise unused Session row) on every shell mount.
  const agents = useSWR<ManageNavCount>(MANAGE_NAV_COUNT_KEYS.agents, null);
  const environments = useSWR<ManageNavCount>(MANAGE_NAV_COUNT_KEYS.environments, null);
  const sessions = useSWR<ManageNavCount>(MANAGE_NAV_COUNT_KEYS.sessions, null);
  // SWR retains the last totals during failed background revalidation.
  const inactive = summary.data;
  const counts: NavCounts = {
    agents: pathname === '/manage/agents'
      ? agents.error ? undefined : agents.data ?? undefined
      : inactive?.agents,
    environments: pathname === '/manage/environments'
      ? environments.error ? undefined : environments.data ?? undefined
      : inactive?.environments,
    sessions: pathname === '/manage/sessions'
      ? sessions.error ? undefined : sessions.data ?? undefined
      : inactive?.sessions,
  };

  // Integrated-service links have no collection page that can share their
  // response, so they remain one independent, failure-visible read.
  useEffect(() => {
    let alive = true;
    void adminListIntegrations()
      .then((result) => {
        if (!alive) return;
        setIntegrations(result.services);
        setIntegrationsError(false);
      })
      .catch(() => {
        if (alive) setIntegrationsError(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  const integrationItems: NavItem[] = integrations.map((integration) => ({
    to: integration.admin_url,
    match: '',
    external: true,
    integrationName: integration.name,
    ...INTEGRATION_NAV[integration.category],
  }));
  const groups = integrationItems.length
    ? [
        ...NAV_GROUPS.slice(0, -1),
        { titleKey: 'manage:nav.group_integrations', items: integrationItems },
        NAV_GROUPS[NAV_GROUPS.length - 1],
      ]
    : NAV_GROUPS;

  return (
    <>
      {/* The shared sidebar group primitives keep section alignment,
          typography and row spacing consistent with the application rail.
          Reimplementing that chrome here would create a second styling source. */}
      {groups.map((group) => (
        <SidebarGroup key={group.titleKey}>
          {/* `console-nav-section` is a stable selector for the three section
              labels. SidebarGroupLabel and `t-eyebrow` own their visual style;
              no private CSS rule should target this selector. */}
          <SidebarGroupLabel className="console-nav-section t-eyebrow h-7 px-2.5">
            {t(group.titleKey)}
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu className="gap-0.5">
              {group.items.map((item) => {
                const Icon = item.icon;
                const count = item.count ? counts[item.count] : undefined;
                const label = item.integrationName
                  ? `${item.integrationName} ${t(item.labelKey)}`
                  : t(item.labelKey);
                const content = (
                  <>
                    <Icon />
                    <span>{label}</span>
                    {item.external ? (
                      <>
                        <ExternalLink className="ml-auto size-3 text-muted-foreground" />
                        <span className="sr-only">{t('manage:nav.opens_new_tab')}</span>
                      </>
                    ) : null}
                  </>
                );
                return (
                  <SidebarMenuItem key={item.to}>
                    <SidebarMenuButton
                      render={item.external
                        ? <a href={item.to} target="_blank" rel="noreferrer" />
                        : <Link to={item.to} />}
                      isActive={!item.external && pathname.startsWith(item.match)}
                      tooltip={label}
                      className={RAIL_ROW_ACTIVE}
                    >
                      {content}
                    </SidebarMenuButton>
                    {count != null && (
                      <SidebarMenuBadge className="tabular-nums">{count}</SidebarMenuBadge>
                    )}
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      ))}
      {integrationsError ? (
        <ErrorNote className="mx-2.5 mb-2">
          {t('manage:nav.integrations_unavailable')}
        </ErrorNote>
      ) : null}
    </>
  );
}

/** The user menu, in the shell's footer slot. */
function ManageSidebarFooter() {
  const { theme, setTheme } = useTheme();

  return (
    // SurfaceNav owns application/console switching at the top of both shells.
    // The footer is reserved for UserMenu so peer navigation does not
    // masquerade as a breadcrumb or appear only at the bottom of the rail.
    <div className="flex items-center justify-end gap-2">
      <UserMenu compact theme={theme} setTheme={setTheme} />
    </div>
  );
}

export default function ManageApp() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  // The console sits one level deeper than the app, so its trail carries two
  // segments. Derived from the same table the rail renders, so a new section
  // cannot appear in one and be missing from the other.
  const current = NAV_GROUPS.flatMap((g) => g.items).find((item) =>
    pathname.startsWith(item.match),
  );
  // A record's own page adds itself to the trail, from its first render (see
  // RecordCrumbProvider). The URL segment is the last resort, not the answer:
  // it is readable for a record keyed by name and a bare UUID for one keyed by
  // id, and a page that reaches this fallback is not naming its segment.
  const recordId = current
    ? decodeURIComponent(pathname.slice(current.match.length).replace(/^\//, '').split('/')[0] || '')
    : '';

  return (
    <RecordCrumbProvider>
      {(recordLabel) => (
    <AppShell
      sidebarLabel={t('shell:primary_navigation')}
      skipLabel={t('shell:skip_to_content')}
      sidebarHeader={
        <>
          <BrandBlock />
          <SurfaceNav />
        </>
      }
      sidebarContent={<ManageSidebar />}
      sidebarFooter={<ManageSidebarFooter />}
      topbar={
        <AppTopbar
          trailLabel={t('shell:breadcrumb_navigation')}
          crumbs={[
            { label: t('manage:nav.console_eyebrow'), to: '/manage' },
            {
              label: current ? t(current.labelKey) : '',
              to: recordId && current ? current.to : undefined,
            },
            ...(recordId ? [{ label: recordLabel ?? recordId }] : []),
          ]}
        />
      }
    >
        <Routes>
          <Route index element={<Navigate to="/manage/agents" replace />} />
          <Route path="agents" element={<AgentsListPage />} />
          <Route path="agents/new" element={<AgentCreatePage />} />
          <Route path="agents/:agentId/sessions" element={<AgentSessionsPage />} />
          <Route path="agents/:agentId" element={<AgentDetailPage />} />
          <Route path="deployments" element={<DeploymentsListPage />} />
          <Route path="mcp-tokens" element={<McpTokensPage />} />
          <Route path="deployments/new" element={<DeploymentCreatePage />} />
          <Route path="deployments/:deploymentId" element={<DeploymentDetailPage />} />
          <Route path="assistants" element={<AssistantsListPage />} />
          <Route path="assistants/new" element={<AssistantCreatePage />} />
          <Route path="assistants/:assistantId" element={<AssistantDetailPage />} />
          <Route path="sessions" element={<SessionsListPage />} />
          <Route path="sessions/:sessionId" element={<SessionDetailPage />} />
          <Route path="sandboxes" element={<SandboxesListPage />} />
          <Route path="sandboxes/:sandboxId" element={<SandboxDetailPage />} />
          <Route path="environments" element={<EnvironmentsListPage />} />
          <Route path="environments/new" element={<EnvironmentCreatePage />} />
          <Route path="environments/:name" element={<EnvironmentDetailPage />} />
          <Route path="credentials" element={<CredentialsListPage />} />
          <Route path="credentials/:vaultId" element={<CredentialVaultPage />} />
          <Route path="system" element={<SystemPage />} />
          <Route path="errors" element={<ErrorsListPage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="*" element={<Navigate to="/manage/agents" replace />} />
        </Routes>
    </AppShell>
      )}
    </RecordCrumbProvider>
  );
}
