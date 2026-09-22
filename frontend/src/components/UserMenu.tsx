import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LogOut, Monitor, Moon, Settings2, Sun } from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { SidebarMenuButton } from '@/components/ui/sidebar';
import { LanguageSwitcher, Segmented } from '@/components/LanguageSwitcher';
import { probeAuthSession } from '@/api';
import type { UserInfo } from '@/types';

/**
 * Houses the rarely-changed settings (theme + language) behind a single
 * trigger, so they do not dominate the sidebar footer. Two triggers:
 *  - default: the account row (user surface)
 *  - compact: a settings-cog icon button (admin console, no user identity there)
 * Language persists via i18next's detector; theme is owned by the host shell.
 */
export function UserMenu({
  userInfo,
  theme,
  setTheme,
  compact = false,
}: {
  userInfo?: UserInfo | null;
  /** The stored preference — `light`, `dark`, or `system`. */
  theme: string | undefined;
  setTheme: (t: string) => void;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const name = userInfo ? userInfo.display_name || userInfo.user_id : '';
  // Sign-out exists only where there is a session to end — the same three-way
  // probe the route gate asks, so the menu and the gate can never disagree
  // about whether this deployment authenticates.
  const [hasAuthSession, setHasAuthSession] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void probeAuthSession(controller.signal)
      .then((probe) => setHasAuthSession(probe.mode === 'signed-in'))
      .catch(() => {});
    return () => controller.abort();
  }, []);
  const signOut = async () => {
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST', credentials: 'include' });
    } finally {
      window.sessionStorage.removeItem('astrabox:signin-redirect');
      // Back to the console root, where the gate sends an ended session to the
      // sign-in page rather than leaving the reader on a blank shell.
      window.location.assign('/');
    }
  };

  return (
    <DropdownMenu>
      {/* `render` hands the trigger the `<button>` this surface needs; both are
          real buttons, so `nativeButton` stays at its default. The open state
          is `data-popup-open` on the trigger (@base-ui/react/menu) — the
          classes below key off that, and a selector aimed at anything else
          would style nothing while still compiling. */}
      <DropdownMenuTrigger
        render={
          compact ? (
            <button
              type="button"
              aria-label={t('shell:settings')}
              className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent/60 hover:text-foreground data-popup-open:bg-sidebar-accent/60 data-popup-open:text-foreground"
            />
          ) : (
            <SidebarMenuButton
              type="button"
              size="lg"
              aria-label={name || t('shell:account')}
              className="h-auto gap-2.5 rounded-lg border border-transparent px-1.5 py-1.5 text-left transition-colors hover:border-border hover:bg-card/50 data-popup-open:border-border data-popup-open:bg-card/50 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:gap-0 group-data-[collapsible=icon]:border-0"
            />
          )
        }
      >
        {compact ? (
          <Settings2 className="size-[15px]" strokeWidth={1.7} />
        ) : (
          <>
            <Avatar className="size-8 rounded-md">
              {userInfo?.avatar_url ? <AvatarImage src={userInfo.avatar_url} alt="" /> : null}
              <AvatarFallback className="rounded-md bg-secondary text-xs">{(name || '·').charAt(0)}</AvatarFallback>
            </Avatar>
            <div className="min-w-0 flex-1 leading-tight group-data-[collapsible=icon]:hidden">
              <div className="truncate text-13 font-medium">{name || t('shell:account')}</div>
              {userInfo?.display_name && (
                <div className="t-mono truncate text-10 text-muted-foreground">{userInfo.user_id}</div>
              )}
            </div>
            <Settings2 className="size-3.5 shrink-0 text-muted-foreground/70 group-data-[collapsible=icon]:hidden" strokeWidth={1.7} />
          </>
        )}
      </DropdownMenuTrigger>
      {/* This menu is a panel, not a list of labels: the theme segmented
          control and the language switcher inside it need a width of their
          own, and the trigger is a 32px icon button when the rail is
          collapsed. So the floor is stated here rather than taken from the
          trigger — `!` because styles.css floors every popup at its anchor's
          width from outside the cascade layers, and a plain utility is inside
          them. Without it this menu collapses to 127px. */}
      <DropdownMenuContent
        side="top"
        align={compact ? 'end' : 'start'}
        sideOffset={8}
        className="min-w-56!"
      >
        <div className="px-2 py-2">
          <div className="t-eyebrow mb-1.5 px-0.5">{t('shell:theme')}</div>
          <Segmented
            ariaLabel={t('shell:theme')}
            value={theme ?? 'system'}
            onChange={setTheme}
            options={[
              { value: 'light', label: <><Sun className="size-3.5" /> {t('shell:theme_light')}</> },
              { value: 'dark', label: <><Moon className="size-3.5" /> {t('shell:theme_dark')}</> },
              { value: 'system', label: <><Monitor className="size-3.5" /> {t('shell:theme_system')}</> },
            ]}
          />
        </div>
        <DropdownMenuSeparator />
        <div className="px-2 py-2">
          <div className="t-eyebrow mb-1.5 px-0.5">{t('shell:language')}</div>
          <LanguageSwitcher />
        </div>
        {hasAuthSession && (
          <>
            <DropdownMenuSeparator />
            {/* The menu's one item, declared as one: Base UI's roving focus
                walks its own item collection, so a plain button here would
                leave a `role="menu"` that announces no items and whose arrow
                keys reach nothing. */}
            <DropdownMenuItem onSelect={() => { void signOut(); }}>
              <LogOut className="size-3.5" strokeWidth={1.7} />
              {t('shell:sign_out')}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export default UserMenu;
