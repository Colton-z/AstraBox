import { Link } from 'react-router-dom';

import { AstraMark } from '@/components/AstraConsole';

/**
 * Shared product identity for both application surfaces.
 *
 * The mark always links to the application root, and its dimensions and
 * typography stay identical when navigation crosses into the console. The
 * wordmark identifies the product without a surface-specific subtitle.
 */
export function BrandBlock() {
  return (
    // `aria-label`: collapsed, the wordmark is `display:none` and the mark is
    // `aria-hidden`, which leaves the rail's first tab stop with no name at all.
    <Link
      to="/"
      aria-label="AstraBox"
      className="flex items-center gap-2.5 px-1 pt-1 leading-none group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
    >
      <AstraMark size={22} />
      <span className="text-15 font-semibold tracking-[-0.01em] group-data-[collapsible=icon]:hidden">AstraBox</span>
    </Link>
  );
}
