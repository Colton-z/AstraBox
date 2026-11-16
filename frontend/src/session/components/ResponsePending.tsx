import { createContext } from 'react';
import { Shimmer } from '@/components/ai-elements/shimmer';
import { cn } from '@/lib/utils';

/**
 * The user message whose pending line the conversation shows where the reply
 * will appear. Its bubble then says nothing about being on its way: one state
 * has one place on screen.
 */
export const PendingResponseContext = createContext<string | null>(null);

/**
 * The line that stands where a reply is about to be written. It keeps one
 * position and one shape from the moment a message is sent until the first
 * content arrives; only its words change.
 */
export function ResponsePending({ label, announce, late }: {
  label: string;
  announce?: boolean;
  /** Between two steps of a running response, where the state is often over before it could be read. */
  late?: boolean;
}) {
  return (
    <div
      role={announce ? 'status' : undefined}
      data-testid="response-pending"
      className={cn('flex items-center gap-2', late && 'astra-enter-late')}
    >
      <span className="astra-dot astra-dot--live" />
      <Shimmer duration={1.5}>{label}</Shimmer>
    </div>
  );
}
