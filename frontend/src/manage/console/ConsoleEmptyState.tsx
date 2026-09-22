import { AlertTriangle, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { columnsTemplate, type ConsoleColumn } from './ConsoleTable';
import { AstraMark } from '@/components/AstraConsole';
import { Button } from '@/components/ui/button';
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';

/**
 * Designed empty state — not bland centered gray text. A faint star-field
 * backdrop, the AstraBox mark in the kit's media tile, and a short hint, so an
 * empty table still reads intentional. Drops into a ConsoleTable's `empty` slot
 * (sits flush inside the table frame).
 *
 * The shape is the kit's `Empty`; the mark and the star-field are the console's
 * skin over it. Padding, measure, type and the gaps between the parts belong to
 * the kit, so an empty state here and an empty panel in a conversation cannot
 * drift apart in any of them.
 *
 * There is no eyebrow: one would render "NO AGENTS" directly above "No agents
 * yet" (§1). What an empty state owes the reader is what will appear here and
 * what produces it — `title`, `hint` and `action` carry that (§5).
 */
export function ConsoleEmptyState({
  title,
  hint,
  action,
  className,
}: {
  title: React.ReactNode;
  hint?: React.ReactNode;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <Empty className={cn('relative overflow-hidden', className)}>
      <div className="bg-starfield pointer-events-none absolute inset-0 opacity-50" aria-hidden />
      {/* `relative` on the content, not only on the frame: the star-field is
          positioned, and a positioned element paints over its static siblings
          whatever the source order. Without this the backdrop covers the words. */}
      <EmptyHeader className="relative">
        <EmptyMedia variant="icon">
          {/* Sized in the kit's own vocabulary, which is what makes the mark
              keep this size: the media tile sets any svg that does not carry a
              `size-` class to 16px, and a CSS rule beats the width and height
              attributes an svg is drawn with. The mark is a filled tile rather
              than a line glyph, so it is drawn to the tile, not to a glyph. */}
          <AstraMark size={20} className="size-5" />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        {hint != null && hint !== '' && <EmptyDescription>{hint}</EmptyDescription>}
      </EmptyHeader>
      {action && <EmptyContent className="relative">{action}</EmptyContent>}
    </Empty>
  );
}

/**
 * Quiet centered status line inside a table frame — loading / "no matches".
 *
 * The same `Empty` as the state above, with nothing but its description: "no
 * row matched" and "nothing here yet" are one shape at two levels of detail,
 * and writing the quiet one as its own box would give the two different
 * padding and a different type size inside the same table frame.
 */
export function ConsoleTableNote({ children }: { children: React.ReactNode }) {
  return (
    <Empty>
      <EmptyDescription>{children}</EmptyDescription>
    </Empty>
  );
}

/**
 * Hairline loading skeleton — the kit's `Skeleton` bars laid out on the table's
 * own grid, so a loading list reads as the table filling in (not a spinner
 * blocking the frame). Drops into a ConsoleTable's `empty` slot while
 * `loading`. `template` should be the same grid-template-columns the columns
 * produce; pass `rows` to taste.
 */
export function ConsoleTableSkeleton<Row>({
  columns,
  rows = 6,
}: {
  // Takes the columns rather than a track list so the skeleton cannot drift
  // from the table it stands in for: a hand-maintained second copy of the
  // template drifts out of sync as each page evolves independently. Generic
  // because a column is invariant in its row type: its `cell` takes the row,
  // so a ConsoleColumn<Agent>[] is not a ConsoleColumn<unknown>[].
  columns: ConsoleColumn<Row>[];
  rows?: number;
}) {
  const template = columnsTemplate(columns);
  // Varied bar widths per column so the rows don't read as a uniform block.
  const widths = ['62%', '48%', '40%', '34%', '52%', '44%'];
  return (
    <div aria-hidden>
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={r}
          className="console-row grid items-center gap-3 px-4 py-2.5"
          style={{ gridTemplateColumns: template }}
        >
          {template.split(' ').map((_, c) => (
            <div key={c} className="min-w-0">
              {/* The stagger is the product's, the pulse is the kit's: rows
                  that all breathe on the same beat read as one blinking block
                  rather than as a list arriving. */}
              <Skeleton
                className="h-3"
                style={{ width: widths[c % widths.length], animationDelay: `${(r * 60) % 360}ms` }}
              />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/**
 * Actionable error card in the interface's voice — not a raw stack trace and not
 * an apology. States what failed, shows the technical detail as mono so it's
 * copyable, and offers Retry. Use for a list-fetch failure
 * where the table can't render at all.
 *
 * The same `Empty` as the states above — a fetch that failed and a list with
 * nothing in it are the same hole in the page, and the reader should not have
 * to re-learn where the title and the action sit between them. Only the media
 * tile changes hue, because only this one is a failure (§7).
 */
export function ConsoleErrorState({
  title,
  detail,
  onRetry,
  className,
}: {
  title?: React.ReactNode;
  detail?: React.ReactNode;
  onRetry?: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    // Named because "this fetch is over and it failed" is otherwise only
    // legible as a colour and a word, and both change with the theme and the
    // language. The visual-grammar audit waits on it to know a record page has
    // settled — reading the heading a frame earlier reads the loading state.
    // It replaces the kit's own `data-slot` deliberately: this card is the
    // failure, and nothing reads the generic marker.
    //
    // role="alert" for the same reason ErrorNote carries it: this is the
    // product's other shared failure primitive — the full-card shape for a
    // fetch the page cannot render without — and a failure a screen reader is
    // never told about is the malformed-response gate's definition of blank.
    <Empty role="alert" data-slot="error-state" className={className}>
      <EmptyHeader>
        <EmptyMedia variant="icon" className="bg-crimson-tint text-crimson-fg">
          <AlertTriangle />
        </EmptyMedia>
        {/* No eyebrow. "ERROR" over "Sandbox not found" is the same word twice
            in two registers (§1), and it shouts (§6) — the crimson tile above
            it already says which kind of state this is. */}
        <EmptyTitle>{title ?? t('manage:console.error_default_title')}</EmptyTitle>
        {detail != null && detail !== '' && (
          // Mono because it is the deployment's own words, which the reader
          // pastes into a search or a ticket (§6) — the audit reads the slot
          // above to know this run of prose is quoted, not written here.
          <EmptyDescription className="console-val break-words">{detail}</EmptyDescription>
        )}
      </EmptyHeader>
      {onRetry && (
        <EmptyContent>
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RefreshCw />
            {t('common:retry')}
          </Button>
        </EmptyContent>
      )}
    </Empty>
  );
}

/**
 * A record page whose record has not arrived.
 *
 * Named rather than written inline on each record page, because "this page is
 * still fetching" has to be legible to something other than a reader: the
 * visual-grammar audit measures a record page and must not measure it here.
 * Arrival cannot be inferred from the trail's last segment changing either —
 * that never happens for a record whose URL segment is its name.
 *
 * A line rather than an `Empty`: the record is coming, so the page is not
 * empty, and a full state card here would say it is.
 */
export function ConsoleRecordLoading() {
  const { t } = useTranslation();
  return (
    <p data-slot="loading-state" className="t-copy text-muted-foreground">
      {t('common:loading')}
    </p>
  );
}
