/**
 * Shared selected-state treatment for rail rows.
 *
 * Every rail row in the product is a `SidebarMenuButton`, and its selected
 * state is the single place this design departs from the kit's. The kit paints
 * `data-active` and `:hover` the same `bg-sidebar-accent`, so the row under the
 * pointer and the active row would otherwise have the same treatment. Selection
 * takes the product accent, which is the job `--astra` owns
 * (docs/frontend-design.md §0, Settled), and hover keeps the neutral fill.
 *
 * The bar is not decoration on top of that. Colour alone does not survive a
 * greyscale screenshot or a red-green reader (§7), so the state carries a shape
 * as well.
 *
 * The label is `--astra-fg`, the astra family's text step, not `--astra-2`,
 * which is its pressed-background step. The text token maintains contrast
 * against the tint in both themes.
 *
 * Tailwind scans comments as source, so a comment here must not spell a utility
 * class that the file does not actually use: naming one emits it.
 */
export const RAIL_ROW_ACTIVE = [
  'relative',
  'data-active:bg-astra-tint data-active:text-astra-fg',
  'data-active:before:absolute data-active:before:left-0',
  'data-active:before:top-1.5 data-active:before:bottom-1.5',
  'data-active:before:w-0.5 data-active:before:rounded-full data-active:before:bg-primary',
].join(' ');
