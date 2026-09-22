import { AstraMark, StatusPill, type PillTone } from '@/components/AstraConsole';

/**
 * The console's status pill is the app shell's status pill: this module is the
 * console's import point for it, not a second implementation.
 *
 * A copy that restated the six tone classes here would lose the `data-testid`,
 * `data-state`, `data-tone` and `data-pulse` attributes the shared component
 * carries — so the selectors that make a run state assertable on the user
 * surface would not match on the console at all — and the two would drift in
 * colour (`Recovery needed` citrine on one side and crimson on the other).
 */
export { AstraMark, StatusPill };
export type { PillTone };
