import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Card, CardAction, CardContent, CardFooter, CardHeader } from '@/components/ui/card';
import { ConsoleSavedTag } from './ConsoleForm';

/**
 * One settings card on a record's own page.
 *
 * A card is the unit that saves. Its head names what the group is and says in a
 * sentence why these fields belong together; its foot carries the constraint
 * worth knowing before you change them, and the Save for these fields alone.
 *
 * Putting the save in the foot rather than beside the title binds it to the
 * fields it writes: it sits at the end of them, after the sentence explaining
 * what cannot be changed, where a reader who has just finished editing is
 * already looking.
 */
export function ConsoleCard({
  title,
  intro,
  actions,
  note,
  dirty = false,
  blocked = false,
  blockedReason,
  saving = false,
  saved = false,
  saveLabel,
  onSave,
  onRevert,
  revertLabel,
  children,
  className,
}: {
  title: React.ReactNode;
  /** One line on why these fields are one group. */
  intro?: React.ReactNode;
  /**
   * What acts on the group as a whole — adding to a list the card holds, not
   * writing the fields in it. A save belongs in the foot, beside the fields it
   * writes; this belongs beside the title, because it is about the set.
   */
  actions?: React.ReactNode;
  /** The constraint a reader should know before changing them. */
  note?: React.ReactNode;
  /** Something here differs from what is stored. */
  dirty?: boolean;
  /** This card cannot be written yet — `blockedReason` says what is missing. */
  blocked?: boolean;
  blockedReason?: React.ReactNode;
  saving?: boolean;
  saved?: boolean;
  saveLabel?: React.ReactNode;
  onSave?: () => void;
  onRevert?: () => void;
  revertLabel?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Card
      className={cn(
        // ui/card wears the transcript's surface: 12px corners, a ring, no
        // shadow. The console's is 8px with a real border and the flat card
        // shadow, and its rhythm is 24px rather than 16. `text-base` holds the
        // page's own size — ui/card sets `text-sm`, which would shrink every
        // string in a card that names no size of its own.
        '[--card-spacing:--spacing(6)] rounded-lg border text-base shadow-(--shadow-card) ring-0',
        className,
      )}
    >
      {/* The row gap sets the title off its intro; the column gap keeps the
          actions the same 16px from the title block. */}
      <CardHeader className="gap-x-4">
        {/* By hand because CardTitle is a div, and the heading level is what
            the page outline is built from. */}
        <h2 className="t-h2-tight text-15">{title}</h2>
        {intro && <p className="t-copy max-w-[62ch] text-muted-foreground">{intro}</p>}
        {actions && <CardAction className="flex items-center gap-2">{actions}</CardAction>}
      </CardHeader>
      {/* CardContent carries the horizontal padding and nothing else, so the
          24px between a card's fields is re-supplied here. */}
      <CardContent className="flex flex-col gap-(--card-spacing)">{children}</CardContent>
      {(note || onSave) && (
        /* The foot is a shallower band than the body it closes: 12px against 24. */
        <CardFooter className="justify-between gap-4 rounded-b-lg bg-muted px-(--card-spacing) py-3">
          <p className="t-copy-sm max-w-[64ch] text-muted-foreground">
            {blocked && dirty && blockedReason ? (
              <span className="text-crimson-fg">{blockedReason}</span>
            ) : (
              note
            )}
          </p>
          <div className="flex shrink-0 items-center gap-2">
            {saved && !dirty && <ConsoleSavedTag />}
            {dirty && onRevert && (
              <Button variant="ghost" size="sm" disabled={saving} onClick={onRevert}>
                {revertLabel}
              </Button>
            )}
            {/* Only once something differs from what is stored (§5): a card
                with nothing to write shows no Save at all, rather than giving
                every card of an untouched record a permanently greyed one. */}
            {dirty && onSave && (
              <Button size="sm" disabled={saving || blocked} onClick={onSave}>
                {saveLabel}
              </Button>
            )}
          </div>
        </CardFooter>
      )}
    </Card>
  );
}

/**
 * The column of observed facts beside a record's cards — what the deployment
 * reports about it, as opposed to what the operator sets.
 *
 * Separate from the cards on purpose: a number nobody can edit, sitting in a
 * form, reads as a field that failed to render a control.
 */
export function ConsoleFactRail({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <aside className={cn('flex w-full shrink-0 flex-col gap-4 lg:w-56', className)}>
      {children}
    </aside>
  );
}

/** One fact: a quiet label over the value it names. */
export function ConsoleFact({
  label,
  value,
  detail,
}: {
  label: React.ReactNode;
  value: React.ReactNode;
  /** A line under the value — what it is made of, or why it is empty. */
  detail?: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <div className="console-label">{label}</div>
      {/* A fact is metadata, not a heading: at a heading size such as
          `t-h2-tight text-lg` the rail becomes the largest type on the page
          after the title — larger than the cards' own headings — so the
          quietest column shouts the loudest (§6: pick the role, then let the
          size follow).

          `break-words` because a fact is often a machine identity with no break
          opportunity in it. Measured: a 32-character trigger id rendered 81px
          past the viewport's right edge inside a clipping ancestor, so it was
          cut with no ellipsis and no scroll — the reader could not tell the
          value was incomplete, let alone read the rest of something they were
          there to copy. Wrapping keeps it whole; truncating would not. */}
      <div className="t-label mt-1 break-words text-sm text-foreground">{value}</div>
      {detail && <div className="t-copy-sm mt-1 text-muted-foreground">{detail}</div>}
    </div>
  );
}
