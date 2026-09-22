import { Search } from 'lucide-react';

import { cn } from '@/lib/utils';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';

/**
 * Under this many rows the eye finds a row faster than typing does, so search
 * would only announce "there is too much here" about a list where there isn't
 * (docs/frontend-design.md §5). Shared by every list page through the `total`
 * prop below, so no page re-decides the number.
 */
const SCANNABLE_ROWS = 12;

/**
 * Search field with a leading magnifier — the combo every list page repeats.
 *
 * The kit's `InputGroup` owns the pairing: the magnifier is a real addon inside
 * the field's border, so the field keeps one focus ring around both parts and
 * the glyph cannot drift out of the text as the control's height changes. An
 * icon absolutely positioned over a plain field would instead need its padding
 * kept in step with the icon's size by hand.
 */
export function ConsoleSearch({
  value,
  onChange,
  total,
  placeholder,
  className,
}: {
  value: string;
  onChange: (v: string) => void;
  /** How many rows exist unfiltered. Below a scannable count the field does not render. */
  total: number;
  placeholder?: string;
  className?: string;
}) {
  // A typed query keeps its field even if rows fall below the threshold —
  // hiding the control while it is still filtering would leave the list
  // invisibly narrowed with nothing on screen to clear.
  if (total <= SCANNABLE_ROWS && !value) return null;
  return (
    <InputGroup className={cn('max-w-xs flex-1', className)}>
      <InputGroupAddon>
        <Search />
      </InputGroupAddon>
      <InputGroupInput
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </InputGroup>
  );
}

/**
 * The recurring first-column treatment: a display name over a secondary line.
 * The secondary line is mono only when it is a machine identity — a slug the
 * reader will type, a session id they will paste. Prose (a description) stays
 * in the text face; mono on prose claims an identity it does not have
 * (docs/frontend-design.md §6).
 */
export function NameCell({
  name,
  sub,
  subKind = 'id',
  className,
}: {
  name: React.ReactNode;
  sub?: React.ReactNode;
  /** What the secondary line is: a machine identity (mono) or prose (text face). */
  subKind?: 'id' | 'text';
  className?: string;
}) {
  return (
    <div className={cn('min-w-0', className)}>
      <div className="truncate font-medium text-foreground">{name}</div>
      {sub != null && sub !== '' && (
        <div
          className={cn(
            'truncate text-11 text-muted-foreground',
            subKind === 'id' && 'console-val',
          )}
        >
          {sub}
        </div>
      )}
    </div>
  );
}

/** The toolbar row that holds search + filter chips above a console table. */
export function ConsoleToolbar({ children, className }: { children: React.ReactNode; className?: string }) {
  // empty:hidden — when every child hides itself (§5: zero rows, so search is
  // under its threshold and every chip count agrees), the band leaves no trace
  // above the empty state. A `display:none` element is not a flex item, so the
  // gap its page column would put beside it goes with it.
  return <div className={cn('flex flex-wrap items-center gap-3 empty:hidden', className)}>{children}</div>;
}
