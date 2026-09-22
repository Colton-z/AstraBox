import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

/**
 * tailwind-merge has to be told about this project's font sizes.
 *
 * `--text-10/11/13/15` generate `text-10`…`text-15`, and tailwind-merge's
 * built-in font-size group only recognises the t-shirt scale (`text-xs`,
 * `text-sm`, …). Without this extension an arbitrary `text-*` token can fall
 * into the colour group, causing `cn('text-13', 'text-muted-foreground')` to
 * discard the font size without an error.
 *
 * `t-copy`/`t-copy-sm` are the same group by a different route. They are
 * `@layer components` rules in `styles.css` and a Tailwind `text-*` is
 * `@layer utilities`, which layer order puts last — so a vendored component's
 * own `text-sm` outranks the token a caller passes and silently resizes the
 * text. Naming them here makes tailwind-merge drop the loser from the class
 * list, so the cascade is never asked to choose between them.
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": ["text-10", "text-11", "text-13", "text-15", "t-copy", "t-copy-sm"],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}
