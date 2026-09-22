import { cn } from '@/lib/utils';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';

export type FilterChipOption<T extends string = string> = {
  value: T;
  label: string;
  /**
   * Optional trailing count. Set in tabular figures, not mono: a count is not a
   * machine identity (docs/frontend-design.md §6).
   */
  count?: number;
};

/**
 * Segmented filter chips — the kit's All/Running/Mine row, on the kit's
 * `ToggleGroup`. Controlled: pass `value` + `onChange`.
 *
 * Single-select, which is what `ToggleGroup` is by default: exactly one of
 * these narrowings is in force at a time, and the pressed item carries
 * `aria-pressed` so the narrowing is legible to a reader who cannot see the
 * fill.
 *
 * Use these when the options are a small set known at build time, so every
 * choice can be visible and counted at once. When the options come from the
 * data — the distinct states in a result set, the template names in use — they
 * are open-ended and belong in a select: a row of chips is unusable once there
 * are a dozen of them, or one of them is a long name.
 *
 * Chips and a select both stand in the toolbar band, so both are 32px and
 * switching between them does not move the row (§9).
 */
export function FilterChips<T extends string = string>({
  options,
  value,
  onChange,
  className,
  'aria-label': ariaLabel,
}: {
  options: FilterChipOption<T>[];
  value: T;
  onChange: (value: T) => void;
  className?: string;
  'aria-label'?: string;
}) {
  // A filter is an answer to "there is too much here to scan"
  // (docs/frontend-design.md §5), so a chip that selects nothing does not
  // render — except the active one, which must stay on screen so the reader
  // can see the narrowing and undo it.
  const visible = options.filter((o) => o.count == null || o.count > 0 || o.value === value);
  // When every visible chip is counted and all the counts agree, no choice can
  // change the rows below — the whole control is announcing a distinction that
  // does not exist, so none of it renders. (One environment: All 1 / Enabled 1.)
  // An uncounted chip is an unknown, not a known-empty, and keeps the group.
  const counts = visible.map((o) => o.count);
  if (counts.every((c) => c != null) && new Set(counts).size <= 1) return null;
  return (
    <ToggleGroup
      variant="outline"
      className={cn('flex-wrap', className)}
      aria-label={ariaLabel}
      value={[value]}
      onValueChange={(next) => {
        // Pressing the pressed chip empties the group's value. A list is always
        // filtered by something — "All" is a narrowing too — so that is not a
        // state this control has, and the press is a no-op. Looking the value
        // back up in the options is also what keeps the callback typed in T
        // without asserting: the group speaks in strings, this speaks in the
        // choices it was given.
        const picked = visible.find((o) => o.value === next[0]);
        if (picked) onChange(picked.value);
      }}
    >
      {visible.map((opt) => (
        <ToggleGroupItem key={opt.value} value={opt.value}>
          {opt.label}
          {opt.count != null && (
            <span className="text-xs tabular-nums text-muted-foreground">{opt.count}</span>
          )}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}
