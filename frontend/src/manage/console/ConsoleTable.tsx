import { ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { Ellipsis } from '@/components/shell';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/**
 * Semantic column kinds shared by every management table.
 *
 * Pages declare content intent rather than CSS widths, keeping equivalent
 * columns aligned. Track sizes follow their content constraints:
 *   name        the row's subject; the column that absorbs slack
 *   text        a secondary human-readable value
 *   identifier  a machine value — id, image, model, engine name
 *   status      a pill; the floor is the widest label one can carry
 *               ("Running in background" measures 149px, so the 152px floor
 *               clears it; at 120px that label is cut mid-word, no ellipsis)
 *   timestamp   a formatted date; its width is known and does not need slack
 *   compact     a short scalar — a duration, a yes/no
 *
 * Typography and alignment are part of the same intent mapping.
 */
export type ColumnIntent =
  | 'name'
  | 'text'
  | 'identifier'
  | 'status'
  | 'timestamp'
  | 'compact';

/**
 * Face and figure style per intent.
 *
 * Mono says "machine identity" — a value the reader will type, paste, or match
 * against a log line (docs/frontend-design.md §6). It does not mean "this is
 * data" and it does not mean "these digits line up": `tabular-nums` lines
 * figures up on its own, so `timestamp` and `compact` stay in the sans face
 * rather than telling the reader a date is something to paste somewhere.
 */
const INTENT: Record<ColumnIntent, { track: string; face: 'sans' | 'mono'; num: boolean; end: boolean }> = {
  // The name column absorbs the slack; the others take close to what their
  // content needs. Spreading it evenly (1.6 / 1.2 / 1.2) holds together in the
  // six-column sessions table but stretches the four-column agents table into
  // three short values marooned across 1200px: a table reads as composed when
  // its values sit near each other and one column carries the give.
  name: { track: 'minmax(180px,2.8fr)', face: 'sans', num: false, end: false },
  text: { track: 'minmax(140px,1fr)', face: 'sans', num: false, end: false },
  identifier: { track: 'minmax(140px,0.8fr)', face: 'mono', num: false, end: false },
  status: { track: 'minmax(152px,176px)', face: 'sans', num: false, end: false },
  timestamp: { track: 'minmax(148px,168px)', face: 'sans', num: true, end: true },
  compact: { track: 'minmax(96px,112px)', face: 'sans', num: true, end: true },
};

/** Column descriptor for {@link ConsoleTable}. */
export type ConsoleColumn<Row> = {
  key: string;
  /** The column's name (Name/Model/Status…), as a reader would say it. */
  header: React.ReactNode;
  cell: (row: Row) => React.ReactNode;
  /** What this column holds. Decides its width, face and alignment. */
  intent: ColumnIntent;
  /**
   * What this cell's value reads as in full, for a column whose `cell` renders
   * something richer than text. Cells that render plain text disclose
   * themselves and need nothing here.
   */
  title?: (row: Row) => string;
  /** Extra className applied to both the header and the cell. */
  className?: string;
};

/**
 * The grid track list for a set of columns — the single source the table and
 * its loading skeleton both read, so the two cannot drift apart.
 */
export function columnsTemplate<Row>(
  columns: ConsoleColumn<Row>[],
  { withChevron = false }: { withChevron?: boolean } = {},
): string {
  const tracks = columns.map((c) => INTENT[c.intent].track);
  // A fixed 20px track, so the affordance never eats into a data column.
  if (withChevron) tracks.push('20px');
  return tracks.join(' ');
}

/**
 * Dense hairline console table. Borders not shadows; tight rows; eyebrow
 * column headers; astra left-accent on the active row; subtle hover fill.
 *
 * The elements, the scroll container and the row/cell chrome come from
 * `@/components/ui/table`, so a row is a `<tr>` and a cell is a `<td>` — the
 * table is a table to a screen reader and to `getByRole('table')` without this
 * file naming a single ARIA role. What this file adds is the console's own
 * grammar: the frame (`.console-tbody`), the column intent system, and the
 * row-opens-a-record affordance.
 *
 * Where the two overlap, the console's rules in `styles.css` win by sitting
 * outside `@layer` while Tailwind's utilities sit inside `@layer utilities` —
 * so `.console-row` and `.console-label` override the kit's `border-b` and
 * `font-medium` without `!important`. Where a kit utility has to be gone rather
 * than overridden, passing the conflicting one through `className` is enough:
 * `cn` runs tailwind-merge, which drops the earlier class of the same group.
 *
 * Layout is CSS grid: a column declares what it holds (see ColumnIntent) and
 * the track, face and alignment follow from that — a page does not choose a
 * width, so two pages cannot disagree about how wide a status column is. The
 * grid needs the boxes it sizes, so the table, the head and the body are
 * `block` and each row is a `grid`; the elements and their roles stay as they
 * are.
 */
export function ConsoleTable<Row>({
  columns,
  rows,
  rowKey,
  onRowClick,
  isRowClickable,
  isRowActive,
  empty,
  footer,
  className,
}: {
  columns: ConsoleColumn<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  onRowClick?: (row: Row) => void;
  /** Restricts row-open semantics to rows that have a record to open. */
  isRowClickable?: (row: Row) => boolean;
  isRowActive?: (row: Row) => boolean;
  /** Shown in place of the body when `rows` is empty (e.g. a ConsoleEmptyState). */
  empty?: React.ReactNode;
  /** Optional node rendered after the body, inside the table frame (loading line). */
  footer?: React.ReactNode;
  className?: string;
}) {
  const { t } = useTranslation();
  const gridStyle = {
    gridTemplateColumns: columnsTemplate(columns, { withChevron: !!onRowClick }),
  } as React.CSSProperties;
  const columnCount = columns.length + (onRowClick ? 1 : 0);
  // A row that spans the frame rather than the grid: the footer is one
  // full-width cell, so it must not inherit the track list.
  const spanRow = 'block hover:bg-transparent';
  const spanCell = 'block p-0 whitespace-normal';

  // With no rows the frame IS the empty state. Two things follow from there
  // being no rows: the column labels have nothing to label, and the frame can
  // fill the column it stands in — `.console-tbody` is a `<tbody>` inside a
  // `<table>`, so a height given to the element below reaches the kit's
  // scroll container and stops one level short of the box that draws the
  // border. The caller's column supplies the height (`PageShell`).
  if (rows.length === 0) {
    return (
      <div
        data-testid="console-table"
        className={cn('console-table flex min-h-0 flex-1 flex-col', className)}
      >
        <div className="console-tbody flex min-h-0 flex-1 flex-col">
          {empty}
          {footer}
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="console-table"
      className={cn(
        'console-table flex min-h-0 flex-col',
        // Hug the content, so a short list is not stretched into a mostly-blank
        // frame. `min-h-0` lets it shrink below that content, which is what makes
        // the body scroll instead of pushing the page taller.
        'flex-[0_1_auto]',
        // `<Table>`'s own scroll container is this element's only child and
        // carries no flex classes, so the height contract has to reach it from
        // here.
        '*:min-h-0 *:flex-1',
        // A region that scrolls takes keyboard focus so it can be scrolled, and
        // that region is the kit's container, whose class list is the kit's —
        // `.console-scroll` cannot be attached to it. Its ring is drawn here to
        // the console's rule instead of the browser's, and inward, because a
        // ring offset outward from a box whose neighbour touches it is drawn
        // into the neighbour.
        '*:focus-visible:outline-2 *:focus-visible:outline-ring *:focus-visible:-outline-offset-2',
        className,
      )}
    >
      {/*
        `min-w-min` gives the table the floor its rows already have: the grid
        minimum of a row is the sum of the INTENT tracks' minima plus its gaps
        and padding. The frame is then as wide as the columns it holds, so a
        viewport too narrow for them scrolls the whole table inside the kit's
        container. Without it the body is held at the container's width while
        the row's tracks keep their minima, and every cell past the frame is cut
        — `.console-tbody` clips, because that is what rounds its corners — with
        no ellipsis and nothing to scroll.
      */}
      <Table className="block min-w-min">
        {/*
          Not sticky. A sticky header needs the table to be its own vertical
          scroller, and a table only scrolls vertically if something caps its
          height — the page column is `min-h-full`, so the table grows and the
          page scrolls instead. Adding `sticky` here would render a rule that
          never fires. It belongs with a height contract that does not exist yet.

          The rows are the card; the header is not, so the kit's rule under the
          header row is dropped: the frame starts below it.
        */}
        <TableHeader className="console-thead block [&_tr]:border-b-0">
          <TableRow className="grid items-center gap-3 px-5 pb-2.5 hover:bg-transparent" style={gridStyle}>
            {columns.map((c) => (
              <TableHead
                key={c.key}
                className={cn(
                  'console-label h-auto min-w-0 truncate p-0',
                  INTENT[c.intent].end && 'text-right',
                  c.className,
                )}
              >
                {c.header}
              </TableHead>
            ))}
            {onRowClick && (
              <TableHead className="h-auto p-0">
                <span className="sr-only">{t('common:open_record')}</span>
              </TableHead>
            )}
          </TableRow>
        </TableHeader>

        {/* The rows, and only the rows, are the framed card. */}
        <TableBody className="console-tbody block">
          {rows.map((row) => {
            const active = isRowActive?.(row) ?? false;
            const openRow =
              onRowClick && (isRowClickable?.(row) ?? true)
                ? () => onRowClick(row)
                : undefined;
            return (
              <TableRow
                key={rowKey(row)}
                tabIndex={openRow ? 0 : undefined}
                aria-description={openRow ? t('common:open_record') : undefined}
                aria-keyshortcuts={openRow ? 'Enter Space' : undefined}
                onClick={openRow}
                onKeyDown={
                  openRow
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          openRow();
                        }
                      }
                    : undefined
                }
                className={cn(
                  'console-row grid items-center gap-3 px-5 py-3',
                  // The fill belongs to the click affordance, so it is drawn by
                  // `.console-row--click:hover`. The kit tints every row on
                  // hover, which on a table without `onRowClick` promises a
                  // record that is not there.
                  'hover:bg-transparent',
                  openRow && 'console-row--click',
                  // The console marks the active row with an astra left-accent
                  // and tint rather than the kit's `data-state="selected"`
                  // fill, which is the same `bg-muted` as its hover state and
                  // so cannot be told apart from the pointer resting on a row.
                  active && 'console-row--active',
                )}
                style={gridStyle}
              >
                {columns.map((c) => {
                  const content = c.cell(row);
                  // A cell that renders plain text can disclose itself; anything
                  // richer has to say what its full value reads as. Clipping
                  // without disclosure is how a truncated sandbox id becomes
                  // unreadable without opening a drawer.
                  const full =
                    typeof content === 'string' || typeof content === 'number'
                      ? String(content)
                      : (c.title?.(row) ?? null);
                  return (
                    <TableCell
                      key={c.key}
                      // `min-w-0`: this cell is the grid item, and a grid item
                      // defaults to refusing any width below its content —
                      // which pushes the excess out of the track, past the
                      // Ellipsis that would otherwise clip and disclose it.
                      className={cn(
                        'min-w-0 p-0',
                        INTENT[c.intent].face === 'mono' && 'console-val',
                        INTENT[c.intent].num && 'tabular-nums',
                        INTENT[c.intent].end && 'text-right',
                        c.className,
                      )}
                    >
                      <Ellipsis title={full}>{content}</Ellipsis>
                    </TableCell>
                  );
                })}
                {/* Says the row opens something. The pointer cursor only tells
                    a reader who is already on the row; this tells the one
                    scanning it. */}
                {onRowClick && (
                  <TableCell
                    className="p-0"
                    aria-label={openRow ? t('common:open_record') : undefined}
                  >
                    {openRow && (
                      <ChevronRight className="console-row-chevron size-4" aria-hidden />
                    )}
                  </TableCell>
                )}
              </TableRow>
            );
          })}

          {footer && (
            <TableRow className={spanRow}>
              <TableCell colSpan={columnCount} className={spanCell}>
                {footer}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
