import { useMemo, useState } from 'react';
import { CalendarDays, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { DateRange } from 'react-day-picker';

import { Button } from '@/components/ui/button';
import { ButtonGroup } from '@/components/ui/button-group';
import { Calendar } from '@/components/ui/calendar';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { Separator } from '@/components/ui/separator';
import i18n from '@/i18n';

/**
 * A start/end window, as one control.
 *
 * One range calendar, not two native `<input type="date">` boxes: the browser
 * draws those itself, so they carry none of this console's type, height or
 * focus grammar and sit in the toolbar looking like something else entirely.
 * Two separate fields also leave the reader to hold the relationship between
 * them, which a range shows directly.
 *
 * The presets answer the question this filter exists for — "that agent, last
 * week" — without navigating a calendar to express it.
 *
 * Dates only, never times. The window is over which day a conversation started;
 * an hour field would offer a precision the operator has no reason to think in.
 * The caller turns these into instants — see `SessionsListPage`, which pushes
 * `until` to the end of the day it names so the last day's own conversations are
 * inside the window rather than just outside it.
 */
export type DateWindow = { since?: string; until?: string };

/** `YYYY-MM-DD` in the local calendar — `toISOString()` would shift the day. */
function isoDay(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

function dayLabel(value: string): string {
  const parsed = new Date(`${value}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(i18n.language, { month: 'short', day: 'numeric' });
}

function daysAgo(n: number): Date {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return d;
}

export function DateRangeFilter({
  value,
  onChange,
}: {
  value: DateWindow;
  onChange: (next: DateWindow) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  const selected = useMemo<DateRange | undefined>(() => {
    if (!value.since && !value.until) return undefined;
    return {
      from: value.since ? new Date(`${value.since}T00:00:00`) : undefined,
      to: value.until ? new Date(`${value.until}T00:00:00`) : undefined,
    };
  }, [value.since, value.until]);

  const label = value.since
    ? value.until && value.until !== value.since
      ? `${dayLabel(value.since)} – ${dayLabel(value.until)}`
      : dayLabel(value.since)
    : value.until
      ? t('manage:filter.until_only', { day: dayLabel(value.until) })
      : t('manage:filter.all_dates');

  const preset = (days: number) => () => {
    onChange({ since: isoDay(daysAgo(days)), until: isoDay(new Date()) });
    setOpen(false);
  };

  const isSet = Boolean(value.since || value.until);

  return (
    // Two controls, one object: the window and the way out of it. `ButtonGroup`
    // joins them on a shared edge, which is what lets the clear control be a
    // real button and a sibling of the trigger. Nested inside the trigger it
    // would be a control within a control — markup no browser allows, and a
    // click that has to be stopped from reaching the button around it.
    <ButtonGroup>
      <Popover open={open} onOpenChange={setOpen}>
        {/* Same band as the selects beside it, so it is the same height (§9).
            `render` hands the trigger a real `<button>` — `Button`
            (`@base-ui/react/button`) renders one — so the trigger keeps its
            native button semantics and `nativeButton` stays at its default. */}
        <PopoverTrigger render={<Button variant="outline" className="justify-start font-normal" />}>
          <CalendarDays className="size-4 text-muted-foreground" />
          {label}
        </PopoverTrigger>
        {/* `gap-0`: the popup is a flex column with a 10px gap by default, and
            the three parts here are meant to sit flush — the separator's job is
            to divide the presets from the calendar, which it cannot do from the
            middle of a gap. */}
        <PopoverContent className="w-auto gap-0 p-0" align="start">
          <div className="flex flex-col gap-1 p-2">
            <Button variant="ghost" size="sm" className="justify-start" onClick={preset(0)}>
              {t('manage:filter.today')}
            </Button>
            <Button variant="ghost" size="sm" className="justify-start" onClick={preset(6)}>
              {t('manage:filter.last_7_days')}
            </Button>
            <Button variant="ghost" size="sm" className="justify-start" onClick={preset(29)}>
              {t('manage:filter.last_30_days')}
            </Button>
          </div>
          <Separator />
          <Calendar
            mode="range"
            animate
            // `animate` asks react-day-picker to animate a month change; these
            // eight keys are the classes it applies while it does, and their
            // keyframes are in `frontend/src/styles.css`. One class token per
            // value: react-day-picker hands each straight to `classList.add()`,
            // which takes a single token, so a space-separated utility string
            // would throw InvalidCharacterError and white-screen the page that
            // opened this popup.
            classNames={{
              weeks_before_enter: 'rdp-weeks-enter-back',
              weeks_before_exit: 'rdp-weeks-exit-back',
              weeks_after_enter: 'rdp-weeks-enter-fwd',
              weeks_after_exit: 'rdp-weeks-exit-fwd',
              caption_before_enter: 'rdp-caption-enter-back',
              caption_before_exit: 'rdp-caption-exit-back',
              caption_after_enter: 'rdp-caption-enter-fwd',
              caption_after_exit: 'rdp-caption-exit-fwd',
            }}
            numberOfMonths={2}
            defaultMonth={selected?.from}
            selected={selected}
            // A future day holds no conversations, so it is not offerable.
            disabled={{ after: new Date() }}
            onSelect={(range: DateRange | undefined) =>
              onChange({
                since: range?.from ? isoDay(range.from) : undefined,
                until: range?.to ? isoDay(range.to) : undefined,
              })
            }
          />
        </PopoverContent>
      </Popover>
      {isSet && (
        // A filter that cannot be undone is a trap: the reader has to be able
        // to get back to "all dates" without guessing. It only renders while
        // there is something to clear (§5), which is why the group's right
        // edge belongs to whichever of the two is last.
        <Button
          variant="outline"
          size="icon"
          aria-label={t('manage:filter.clear_dates')}
          onClick={() => onChange({})}
        >
          <X />
        </Button>
      )}
    </ButtonGroup>
  );
}
