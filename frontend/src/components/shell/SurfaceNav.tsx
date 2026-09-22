import { Link, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Home, LayoutTemplate } from 'lucide-react';

import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from '@/components/ui/sidebar';
import { RAIL_ROW_ACTIVE } from './rail';

/**
 * Peer navigation between the application and administration console.
 *
 * Switching surfaces is separate from moving through a breadcrumb hierarchy,
 * so both destinations are rail items and the active row identifies the
 * current surface.
 *
 * The shared component keeps both shells' destinations identical. Kit menu
 * primitives preserve the same interaction and accessibility behavior as the
 * rest of the rail.
 */
export function SurfaceNav() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const inConsole = pathname.startsWith('/manage');

  return (
    <SidebarMenu className="gap-0.5">
      <SidebarMenuItem>
        <SidebarMenuButton
          render={<Link to="/" />}
          isActive={!inConsole}
          tooltip={t('shell:nav_home')}
          className={RAIL_ROW_ACTIVE}
        >
          <Home />
          <span>{t('shell:nav_home')}</span>
        </SidebarMenuButton>
      </SidebarMenuItem>
      <SidebarMenuItem>
        <SidebarMenuButton
          render={<Link to="/manage/agents" />}
          tooltip={t('shell:nav_console')}
          isActive={inConsole}
          className={RAIL_ROW_ACTIVE}
        >
          <LayoutTemplate />
          <span>{t('shell:nav_console')}</span>
        </SidebarMenuButton>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}
