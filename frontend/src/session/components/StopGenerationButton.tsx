import type { ComponentProps } from 'react';
import { useTranslation } from 'react-i18next';
import { PromptInputSubmit } from '@/components/ai-elements/prompt-input';
import { Spinner } from '@/components/ui/spinner';

type StopGenerationButtonProps = ComponentProps<typeof PromptInputSubmit> & {
  isStopping?: boolean;
};

/**
 * The stop control, with the state the vendored button has no vocabulary for.
 *
 * `PromptInputSubmit` knows two things: generating, and not. An interrupt that
 * has been asked for and has not yet landed is a third — the turn is still
 * streaming, so the button still reads as "stop", while pressing it again does
 * nothing. `isStopping` paints that wait, and names it in the reader's
 * language; the vendored button names itself "Stop" in English only.
 *
 * Composed around the vendored component rather than edited into it: a
 * vendored file is upstream's or the product's and never both, and a product
 * decision written into one is deleted by the next re-vendor with nothing to
 * mark that it was ever there (docs/maintainers/upstream-drift-ledger.md).
 */
export function StopGenerationButton({
  isStopping = false,
  status,
  children,
  ...props
}: StopGenerationButtonProps) {
  const { t } = useTranslation();
  const isGenerating = status === 'submitted' || status === 'streaming';
  const showStopping = isGenerating && isStopping;
  const label = showStopping
    ? t('misc:prompt_input.stopping')
    : isGenerating
      ? t('misc:prompt_input.stop_generating')
      : t('misc:prompt_input.send');

  return (
    <PromptInputSubmit aria-label={label} title={label} status={status} {...props}>
      {showStopping ? (
        <Spinner className="text-muted-foreground" aria-label={t('misc:prompt_input.stopping')} />
      ) : (
        children
      )}
    </PromptInputSubmit>
  );
}
