import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/** The longest title the server stores (`_SESSION_TITLE_MAX_CHARACTERS`). */
const SESSION_TITLE_MAX_LENGTH = 200;

/**
 * A conversation's title, edited where it is shown.
 *
 * Enter or leaving the field keeps the text and Escape keeps the old title.
 * The title is one line, so runs of whitespace become one space, as the server
 * stores them; a title that is empty or the same after that is not sent.
 */
export function SessionTitleInput({
  title,
  onSubmit,
  onCancel,
  className,
}: {
  title: string;
  onSubmit: (title: string) => void;
  onCancel: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(title);
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Enter unmounts the field, and the blur that follows must not finish it twice.
  const finished = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const finish = (keep: boolean) => {
    if (finished.current) return;
    finished.current = true;
    const next = value.split(/\s+/).filter(Boolean).join(' ');
    if (keep && next && next !== title) onSubmit(next);
    else onCancel();
  };

  return (
    <Input
      ref={inputRef}
      value={value}
      maxLength={SESSION_TITLE_MAX_LENGTH}
      aria-label={t('shell:session_title')}
      className={cn('h-7 px-1.5 text-13 md:text-13', className)}
      onChange={(event) => setValue(event.target.value)}
      onKeyDown={(event) => {
        // Enter also confirms an input method's candidate, which submits nothing.
        if (event.nativeEvent.isComposing) return;
        if (event.key === 'Enter') {
          event.preventDefault();
          finish(true);
        } else if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          finish(false);
        }
      }}
      onBlur={() => finish(true)}
    />
  );
}
