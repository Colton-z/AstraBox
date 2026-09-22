import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

import { cn } from '@/lib/utils';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { SHIPPED_LANGUAGES, resolveLanguage } from '@/i18n';

/**
 * A row of mutually exclusive choices, one of them pressed.
 *
 * One implementation, because the theme switch and the language switch are the
 * same control. A second copy carries its own radius and its own label to keep
 * in step, and neither surface shows the two apart: the difference is visible
 * only to a reader crossing between them. Callers own the value.
 *
 * On the kit's `ToggleGroup`, which makes the row one tab stop the arrow keys
 * move inside rather than one tab stop per choice — the difference between
 * passing a two-choice control and passing through it.
 */
export function Segmented({
  value,
  options,
  onChange,
  ariaLabel,
  className,
}: {
  value: string;
  options: ReadonlyArray<{ value: string; label: ReactNode }>;
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <ToggleGroup
      // The segments tile: this is one control divided, not a row of chips.
      spacing={0}
      aria-label={ariaLabel}
      value={[value]}
      onValueChange={(next) => {
        // Pressing the pressed segment empties the group's value. Exactly one
        // of these choices is in force at any moment — there is no "neither
        // language" — so that is not a state this control has, and the press
        // is a no-op.
        if (next[0]) onChange(next[0]);
      }}
      className={cn('inline-flex w-full border bg-card/60 p-0.5', className)}
    >
      {options.map((option) => (
        <ToggleGroupItem
          key={option.value}
          value={option.value}
          className={cn(
            // Slim: this control stands in a menu panel and in the rail's
            // foot, not in the toolbar band the kit sizes toggles for.
            //
            // `grow`, not `flex-1`: `flex-1` is a zero basis, which splits the
            // row into equal shares whatever the labels measure. In the
            // 234px user menu that is 68px a segment while the zh
            // "跟随系统" label needs 84px, leaving insufficient padding with
            // equal shares. Each segment starts at its
            // label's width (the kit's `shrink-0` and `whitespace-nowrap`
            // hold it there) and the segments share only the remainder.
            'h-auto grow gap-1.5 py-1 text-xs',
            // The house ring is 2px wide at a 2px outward offset — a 4px
            // reach, and segments that touch leave it none — so outward it
            // would be drawn into the neighbour and past the container's
            // padding. Turned inward it stays inside the segment it marks
            // (docs/frontend-design.md §11).
            //
            // Three utilities, because the kit swaps the shared ring for a 3px
            // one of its own: dropping that one's width leaves a single
            // indicator, and `outline-none` suppresses only the outline's
            // STYLE, so naming a style again is what paints the shared ring.
            'focus-visible:outline-solid focus-visible:-outline-offset-2 focus-visible:ring-0',
            value === option.value
              ? 'text-foreground'
              : // No fill under the pointer: the kit's hover wash is the same
                // token as its pressed wash, so an unpressed segment being
                // hovered would look exactly as chosen as the chosen one.
                'text-muted-foreground hover:bg-transparent hover:text-foreground',
          )}
        >
          {option.label}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  );
}

/**
 * Pick the language, from whatever the bundle ships.
 *
 * A select rather than a segmented row: the row divides one control between
 * its choices, which reads well at two and stops fitting the menu panel it
 * stands in somewhere past four. The options carry each language's own name
 * and its `lang`, so a screen reader pronounces 中文 in Chinese rather than
 * spelling it in the page's language.
 *
 * Persists through i18next's language detector (localStorage `astrabox-lang`).
 */
export function LanguageSwitcher({ className }: { className?: string }) {
  const { t, i18n } = useTranslation();
  const active = resolveLanguage(i18n.resolvedLanguage ?? i18n.language);

  return (
    <NativeSelect
      className={className}
      aria-label={t('shell:language')}
      value={active}
      onChange={(event) => { void i18n.changeLanguage(event.target.value); }}
    >
      {SHIPPED_LANGUAGES.map((language) => (
        <NativeSelectOption key={language.code} value={language.code} lang={language.code}>
          {language.label}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

export default LanguageSwitcher;
