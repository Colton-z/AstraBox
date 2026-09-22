import { cn } from '@/lib/utils';

/**
 * The page container: one column that owns the gutter, the line length, the
 * vertical scroll and the full height of the well it sits in.
 *
 * Three properties keep page chrome and content on the same axis:
 *
 *   1. The scroll lives on `page-scroll`, the cap on `page-measure` inside it.
 *      Capping the scroller centres the scrollbar along with the text and
 *      splits the page's own elements onto two different left edges.
 *   2. `min-h-full` on the measure column: without it a short page cannot fill
 *      the well, leaving a band of the well's background exposed below it —
 *      350–434px of it at 1440x900 under an empty state.
 *   3. The page scrolls, the shell does not. AppShell's content well is
 *      `overflow-hidden` and hands the whole axis to whatever it mounts.
 *
 * The measure column sets no gap, because its two children are not peers: a
 * page is a header and a body. So a page passes a header carrying its own
 * separation — `ConsolePageHeader`'s `mb-6` — and then ONE body element,
 * `<div className="flex flex-1 flex-col gap-4">`, which is where every other
 * gap on the page comes from. `flex-1` is part of the contract, not decoration:
 * `ConsoleTable` grows when it has no rows and the `Empty`-based states centre
 * themselves in what is left, and both read that height through this column
 * (property 2 above is the band they would otherwise expose). A body whose last
 * element scrolls on its own — a log tail — adds `min-h-0` so the cap reaches
 * it; a body of cards does not, or the page stops growing and scrolls inside
 * itself instead.
 *
 * Spacing therefore belongs to the body column, never to margins its children
 * carry (`docs/frontend-design.md` §0): a `gap` disappears with a child that
 * hides itself, and two of them cannot silently add up.
 */
export function PageShell({
  children,
  detail,
  measure = 'wide',
  className,
}: {
  children: React.ReactNode;
  /**
   * The open record's detail, laid out beside the page rather than over it.
   *
   * A detail panel must not cover content the reader still needs
   * (`docs/frontend-design.md` §3), so wide layouts reserve a sibling column.
   *
   * Below `xl` there is no room for both, so the detail takes the whole column
   * and the list steps aside — §3's other branch, reading one record on its own
   * surface. Either way nothing is hidden underneath something else.
   */
  detail?: React.ReactNode;
  /**
   * The line length this page is willing to grow to. `wide` (88rem) suits dense
   * tables; `narrow` (72rem) suits cards and prose.
   */
  measure?: 'wide' | 'narrow';
  /** Applied to the measure column, so a caller's `gap`/`space-y` acts on content. */
  className?: string;
}) {
  const page = (
    <div
      data-slot="page-measure"
      className={cn(
        'mx-auto flex min-h-full w-full flex-col px-gutter py-6',
        measure === 'narrow' ? 'max-w-measure-narrow' : 'max-w-measure',
        // With a detail beside it the column stops centring on the viewport —
        // it centres on what is left, and the cap applies there.
        detail && 'mx-0',
        className,
      )}
    >
      {children}
    </div>
  );

  if (!detail) {
    return (
      <div data-slot="page-shell" className="flex h-full min-h-0 flex-col">
        <div data-slot="page-scroll" className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden">
          {page}
        </div>
      </div>
    );
  }

  return (
    <div data-slot="page-shell" className="flex h-full min-h-0">
      {/* The list keeps its own scroll; the panel keeps its own. Neither
          pushes the other, and closing the panel gives the width straight back. */}
      <div
        data-slot="page-scroll"
        className="hidden min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden xl:block"
      >
        {page}
      </div>
      <div
        data-slot="page-detail"
        className="flex min-h-0 w-full shrink-0 flex-col border-l xl:w-[30rem] 2xl:w-[34rem]"
      >
        {detail}
      </div>
    </div>
  );
}
