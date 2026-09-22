import { cn } from '@/lib/utils';

/**
 * A box that may be truncated, and says so when it is.
 *
 * Truncation on its own loses information silently: a clipped table cell with
 * no `title` or tooltip leaves a value such as a sandbox id unreadable on the
 * page. Clipping and disclosure are one decision here, not two.
 *
 * Two properties are deliberate:
 *
 *   The title is attached only when the text is measured as clipped, on hover.
 *   Attaching it unconditionally would give every cell in a table a native
 *   tooltip repeating text already fully visible. Measuring on hover rather
 *   than with a ResizeObserver keeps the cost at zero until a pointer arrives,
 *   which matters when a page holds hundreds of these.
 *
 *   Disclosure is enforced by the type. `children` that is a string discloses
 *   itself; anything else has to pass `title`, or `null` to say explicitly that
 *   some other affordance covers it, so "clipped with no way to read the rest"
 *   is a compile error.
 *
 * Note the DOM still holds the full text — CSS clipping does not affect the
 * accessibility tree, so a screen reader reads all of it either way. `title` is
 * for the sighted pointer user, which is also why hover is the right moment.
 */
type EllipsisOwnProps = {
  /** Clamp to N lines instead of one. */
  lines?: number;
  className?: string;
  role?: React.AriaRole;
};

export type EllipsisProps = EllipsisOwnProps &
  (
    | { children: string; title?: string }
    // Non-string content cannot disclose itself: say what the full value is,
    // or pass null to declare that something else already does.
    | { children: React.ReactNode; title: string | null }
  );

export function Ellipsis({ children, title, lines = 1, className, role }: EllipsisProps) {
  const full = title === null ? undefined : (title ?? (typeof children === 'string' ? children : undefined));

  const disclose = (event: React.MouseEvent<HTMLSpanElement>) => {
    const el = event.currentTarget;
    if (!full) return;
    const clipped = lines === 1 ? el.scrollWidth > el.clientWidth : el.scrollHeight > el.clientHeight;
    if (clipped) el.setAttribute('title', full);
    else el.removeAttribute('title');
  };

  return (
    <span
      data-slot="ellipsis"
      role={role}
      onMouseEnter={disclose}
      // min-w-0 is what lets this be squeezed at all: a flex or grid child
      // defaults to min-width:auto, which refuses to shrink below its content
      // and pushes the overflow onto an ancestor — where it gets cut without an
      // ellipsis, because the ellipsis is drawn by whoever does the clipping.
      className={cn(
        'block min-w-0',
        lines === 1 ? 'truncate' : 'overflow-hidden [display:-webkit-box] [-webkit-box-orient:vertical]',
        className,
      )}
      style={lines === 1 ? undefined : { WebkitLineClamp: lines }}
    >
      {children}
    </span>
  );
}
