import { Fragment, useEffect, useSyncExternalStore } from 'react';
import { Link } from 'react-router-dom';

import { cn } from '@/lib/utils';
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from '@/components/ui/sidebar';
import { Toaster } from '@/components/ui/sonner';

/** Whichever theme `styles.css` is painting right now. */
function useStylesheetTheme(): 'light' | 'dark' {
  return useSyncExternalStore(subscribeToRootClass, readRootTheme);
}

function subscribeToRootClass(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributeFilter: ['class'] });
  return () => observer.disconnect();
}

function readRootTheme(): 'light' | 'dark' {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

/**
 * Shared application shell for the sidebar, top bar, and content well.
 *
 * `SidebarProvider`, `Sidebar`, and `SidebarInset` own navigation width,
 * collapse behavior, keyboard access, the mobile sheet, and persistence. Those
 * behaviors apply to every surface and therefore are not caller options.
 *
 * Callers own the slot contents. `topbar` is a node rather than a flag so a
 * route that carries its own header can omit the shared bar at the call site.
 */
export function AppShell({
  sidebarHeader,
  sidebarContent,
  sidebarLabel,
  skipLabel,
  sidebarFooter,
  topbar,
  banner,
  children,
  className,
}: {
  sidebarHeader?: React.ReactNode;
  sidebarContent: React.ReactNode;
  sidebarLabel: string;
  /**
   * What the bypass link says. Translated by the caller, the way
   * `sidebarLabel` is, because this file names no landmark itself.
   */
  skipLabel: string;
  sidebarFooter?: React.ReactNode;
  /** The 56px bar. Omit it on routes that render their own header. */
  topbar?: React.ReactNode;
  /** Full-bleed strip under the top bar — request errors and the like. */
  banner?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  const theme = useStylesheetTheme();

  return (
    <>
      {/* First in the tab order and invisible until it holds focus. Without it
          a reader arriving by keyboard crosses the whole rail — measured at 17
          stops before `<main>` on a console page — on every deep link and every
          reload (WCAG 2.4.1). */}
      <a
        href="#app-content"
        // Off-screen at full size rather than hidden and grown: focus moves it,
        // it does not resize it (docs/frontend-design.md §5). `fixed` keeps it
        // out of the layout either way, so nothing shifts when it arrives.
        className="fixed top-3 left-3 z-50 -translate-y-[300%] rounded-md border bg-popover px-3 py-2 text-sm text-popover-foreground focus:translate-y-0"
      >
        {skipLabel}
      </a>
      <SidebarProvider className={cn('!h-screen !min-h-0 overflow-hidden', className)}>
        {/* `icon`, not the kit's default `offcanvas`: this rail IS the
            navigation, and a collapse that takes it to zero width leaves the
            reader with a breadcrumb and a back button. Icon width keeps every
            destination one click away, which is what makes collapsing worth
            offering on a narrow desktop window. */}
        <Sidebar
          collapsible="icon"
          className="border-r group-data-[collapsible=icon]:[&_[data-sidebar=menu-item]]:mx-auto group-data-[collapsible=icon]:[&_[data-sidebar=menu-item]]:w-8"
        >
          <nav aria-label={sidebarLabel} className="flex min-h-0 flex-1 flex-col">
            {sidebarHeader && (
              <SidebarHeader className="gap-4 p-3 group-data-[collapsible=icon]:px-0">
                {sidebarHeader}
              </SidebarHeader>
            )}
            <SidebarContent className="px-1.5 group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:[&_[data-sidebar=group]]:px-0">
              {sidebarContent}
            </SidebarContent>
          </nav>
          {sidebarFooter && (
            <SidebarFooter className="gap-2 p-2 group-data-[collapsible=icon]:items-center group-data-[collapsible=icon]:px-0">
              {sidebarFooter}
            </SidebarFooter>
          )}
        </Sidebar>

        <SidebarInset id="app-content" tabIndex={-1} className="min-h-0 overflow-hidden">
          {topbar}
          {banner}
          {/*
            The well hands its whole height to what it mounts and scrolls nothing
            itself — a page (PageShell) or a route with its own panes (the session
            view) owns the axis. `[&>*]:h-full` is what lets a route be a flex
            column without restating the height at every route element.
          */}
          <div data-slot="app-content" className="min-h-0 flex-1 overflow-hidden [&>*]:h-full">
            {children}
          </div>
        </SidebarInset>
      </SidebarProvider>

      {/*
        One toaster for the whole app, so `toast()` works from any surface
        without each one mounting its own.

        It sits outside `SidebarProvider`, which is `h-screen overflow-hidden`.
        The toast list positions itself `fixed`, and a fixed element is still
        clipped when an ancestor establishes a containing block, so only a
        sibling keeps toasts on screen. It renders nothing until one is queued.

        `theme` is passed as the RESOLVED one. `ui/sonner.tsx` asks
        `next-themes` for the preference, and under `system` that answer is the
        word "system" rather than a colour — sonner would then key its greys off
        the OS while `styles.css` keys the surface off the `dark` class. Reading
        that class is reading the signal the stylesheet actually switched on, so
        the toast cannot disagree with what is under it. The vendored component
        spreads `{...props}` after its own `theme`, so this wins.
      */}
      <Toaster theme={theme} />
    </>
  );
}

/**
 * The 56px bar: sidebar trigger, then a breadcrumb, then whatever the surface
 * wants on the right.
 *
 * `crumbs` is a list because the two surfaces sit at different depths — the app
 * shows one segment, the console shows "Console / Agents". The trail is chrome
 * and stays full-bleed at the shell's gutter rather than joining the page's
 * measure column; a bar that centred itself on a wide display would leave the
 * trigger floating in the middle of the window.
 *
 * The trail carries hierarchy and nothing else. Every segment in it walks up a
 * level within the current surface; none of them leaves the surface. A link
 * that did would be indistinguishable from the ones that do not — same size,
 * same colour, same separators — so crossing between the app and the console
 * belongs to `SurfaceNav` in the rail, where it is a destination rather than an
 * ancestor.
 */
export type Crumb = { label: React.ReactNode; to?: string };

/**
 * Put the page's own name in the document title.
 *
 * A single-page app keeps whatever `index.html` states unless something writes
 * it, so every route reads as one page: the browser tab, the history entry and
 * a bookmark all say the product's name and nothing about where the reader is.
 * The trail's last crumb is that name, already translated.
 */
function useDocumentTitle(crumbs: Crumb[]) {
  const name = [...crumbs].reverse().find((c) => typeof c.label === 'string' && c.label)?.label;
  useEffect(() => {
    document.title = typeof name === 'string' && name ? `${name} · AstraBox` : 'AstraBox';
  }, [name]);
}

export function AppTopbar({
  crumbs,
  trailLabel,
  actions,
  className,
}: {
  /** The trail. A crumb with `to` is the way back to that level. */
  crumbs: Crumb[];
  /**
   * What the trail's landmark is called. It arrives translated, the way
   * `sidebarLabel` does, because this file names no landmark itself — the
   * kit's own default is the English word "breadcrumb".
   */
  trailLabel: string;
  actions?: React.ReactNode;
  className?: string;
}) {
  useDocumentTitle(crumbs);
  return (
    <header
      className={cn(
        'flex h-14 shrink-0 items-center justify-between border-b border-border px-gutter',
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <SidebarTrigger className="-ml-1 text-muted-foreground hover:text-foreground" />
        <div className="h-4 w-px bg-border" />
        {/* The translated name distinguishes this navigation landmark from the
            named rail beside it; the kit default would otherwise be English. */}
        <Breadcrumb aria-label={trailLabel} className="min-w-0">
          <BreadcrumbList className="min-w-0 flex-nowrap gap-2 text-13">
            {crumbs.map((crumb, i) => (
              <Fragment key={i}>
                {/* A separator goes between segments, so the first one has none.
                    Rendered ahead of every segment it would open the trail with a
                    slash hanging off nothing. */}
                {i > 0 && (
                  <BreadcrumbSeparator className="text-muted-foreground/50">/</BreadcrumbSeparator>
                )}
                <BreadcrumbItem className="min-w-0">
                  {i === crumbs.length - 1 ? (
                    // `font-medium` says again what `.t-h2-tight` already sets:
                    // the current-page slot carries `font-normal`, and a utility
                    // beats a rule in `@layer components` whatever the order, so
                    // without it the trail's head would drop to book weight.
                    <BreadcrumbPage className="t-h2-tight truncate text-15 font-medium tracking-[-0.01em]">
                      {crumb.label}
                    </BreadcrumbPage>
                  ) : crumb.to ? (
                    // The trail is the way back, so every ancestor segment is a
                    // link. A plain label would name the level without offering it.
                    <BreadcrumbLink
                      render={<Link to={crumb.to} />}
                      className="console-back truncate text-muted-foreground"
                    >
                      {crumb.label}
                    </BreadcrumbLink>
                  ) : (
                    <span className="truncate text-muted-foreground">{crumb.label}</span>
                  )}
                </BreadcrumbItem>
              </Fragment>
            ))}
          </BreadcrumbList>
        </Breadcrumb>
      </div>
      {actions}
    </header>
  );
}
