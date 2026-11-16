import { createContext } from 'react';
import { Shimmer } from '@/components/ai-elements/shimmer';

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
export function ResponsePending({ label, announce, testId = 'response-pending' }: {
  label: string;
  announce?: boolean;
  testId?: string;
}) {
  return (
    <div
      role={announce ? 'status' : undefined}
      data-testid={testId}
      className="flex min-w-0 items-center gap-1"
    >
      {/* The dot's halo reaches 7px past it. The slot holds the whole halo, so
          no container that clips at its edge can cut the left of it off. */}
      <span className="flex size-5 shrink-0 items-center justify-center">
        <span className="astra-dot astra-dot--live" />
      </span>
      <Shimmer className="min-w-0 truncate" duration={1.5}>{label}</Shimmer>
    </div>
  );
}
