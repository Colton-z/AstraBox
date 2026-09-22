import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * What the reader is agreeing to, for a destructive action that asks first.
 *
 * The words are the caller's because only the page knows which record is
 * going — "Delete claude-code" is the sentence that makes the dialog worth
 * showing, and a generic "Are you sure?" is the one that trains people to
 * click through it.
 */
export type ConsoleDangerConfirm = {
  /** Names the action and its subject: "Kill session 4f2a…". */
  title: React.ReactNode;
  /** What it costs, when the title does not already say it. */
  description?: React.ReactNode;
  /** The armed button's word — "Delete environment", not "OK". */
  action: React.ReactNode;
  onConfirm: () => void;
};

/**
 * The console kit's destructive button — one crimson grammar for every Delete / Kill
 * across the manage surface, so no page hand-rolls its own crimson
 * Tailwind. Built on the kit `<Button>` so it inherits the exact
 * height / radius / hairline / focus-ring sizing as its neighbours (Cancel, Edit); only
 * the color shifts to crimson, driven entirely by `--crimson` / `--crimson-tint`
 * tokens so it flips correctly between INK and PAPER.
 *
 * Two intensities:
 *   - `soft` (default): the resting trigger — crimson hairline + crimson tint fill +
 *     crimson text, the same vocabulary as the `tone-failed` status pill. Quiet
 *     until pressed.
 *   - `solid`: the armed confirm — filled crimson with white text, the unmistakable
 *     "this is the irreversible click" affordance.
 *
 * Pass `confirm` and the two-step is this component's: the trigger opens the
 * kit's `AlertDialog` and the armed button lives in it, wearing `solid`. That
 * is the shape a destructive question wants — it takes focus, it names what is
 * about to happen with room for a sentence, and Escape is a way out. Arming in
 * place instead swaps the page's own action row for a confirm strip, so the
 * question is asked in the width left over beside a heading and the controls
 * around it move under the reader's pointer.
 */
export function ConsoleDangerButton({
  variant = 'soft',
  size = 'default',
  className,
  children,
  confirm,
  ...props
}: Omit<React.ComponentProps<typeof Button>, 'variant'> & {
  variant?: 'soft' | 'solid';
  /** Ask before acting. Without it the button acts on its own `onClick`. */
  confirm?: ConsoleDangerConfirm;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);

  const face = {
    // Sized by the band it stands in, not by the component: fixing it at `sm`
    // would make a Delete beside a page heading shorter than the New on the
    // list that page was reached from.
    size,
    // Override the base variant's color system but keep its sizing/focus grammar.
    variant: 'outline' as const,
    'data-danger': variant,
    className: cn('console-danger-btn', className),
    ...props,
  };

  if (!confirm) {
    return <Button {...face}>{children}</Button>;
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      {/* `render` hands the trigger this button rather than a second one
          beside it, so the dialog's open state and the console's crimson are
          the same element. */}
      <AlertDialogTrigger render={<Button {...face} />}>{children}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{confirm.title}</AlertDialogTitle>
          {confirm.description && (
            <AlertDialogDescription>{confirm.description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{t('common:cancel')}</AlertDialogCancel>
          {/* Closed before the action runs, not after it: the caller reports
              what happened on the page it returns to — a record that has been
              deleted has no page left to hold a dialog, and a failure belongs
              in the page's own failure note, where every other one is. */}
          {/* Not `size` — the dialog's foot is its own band (§9). A row's
              Delete is small because the row is; the question it opens is the
              same question a page-heading Delete opens, and its two buttons
              have to match each other, not the control that summoned them. */}
          <AlertDialogAction
            variant="outline"
            data-danger="solid"
            className="console-danger-btn"
            onClick={() => {
              setOpen(false);
              confirm.onConfirm();
            }}
          >
            {confirm.action}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
