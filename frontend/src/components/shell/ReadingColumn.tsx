import { cn } from '@/lib/utils';

/**
 * Aligns a block that sits outside the transcript's scroller — the composer,
 * and anything else in the frame below it — to the transcript's column.
 *
 * Inside the scroller nothing is needed: `max-w-reading` on the content is
 * enough, because the scroller reserves the gutter for everything in it. The
 * outer frame sets `scrollbar-gutter: stable both-edges`, reserving a
 * scrollbar's width on each side whether or not one is showing, so the composer
 * and messages keep the same edges below the reading-width cap.
 *
 * The reservation is CSS, not a measured scrollbar width: `stable` applies to
 * `overflow: hidden` too, so this reserves exactly what the scroller reserves,
 * on any platform, with nothing to keep in sync.
 *
 * Reserving and capping are two elements on purpose. On a single element the
 * gutter would be subtracted after the cap, leaving the two columns different
 * by the gutter's width in the other direction.
 */
export function ReadingColumn({
  children,
  className,
}: {
  children: React.ReactNode;
  /** Applied to the capped column, so padding and spacing act on content. */
  className?: string;
}) {
  return (
    <div
      data-slot="reading-gutter"
      // shrink-0: this is the frame around the transcript, not part of the
      // scrolling content, so it keeps its height as the column above grows.
      className="shrink-0 overflow-y-hidden [scrollbar-gutter:stable_both-edges]"
    >
      <div
        data-slot="reading-column"
        className={cn('mx-auto w-full max-w-reading px-4', className)}
      >
        {children}
      </div>
    </div>
  );
}
