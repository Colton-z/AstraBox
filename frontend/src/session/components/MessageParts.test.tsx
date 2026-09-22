// @vitest-environment jsdom
// Native thinking blocks can contain no visible text and persist verbatim.
// The renderer must omit their settled, empty "Reasoning" cards.
// The browser-level twin of this check is the data-chars
// invariant in tests/e2e-ui/specs/console-screenshots.exclusive.spec.ts.
import React from 'react';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';

import { MessageBubble, PartRenderer } from './MessageParts';
import i18n from '../../i18n';

afterEach(cleanup);

describe('process headings without a generated summary', () => {
  it.each([
    ['en', 'Process', 'Tool calls', 'Reasoning'],
    ['zh', '执行过程', '工具调用', '思考'],
  ])('distinguishes both levels in %s and keeps tool details accessible', async (language, outer, tools, reasoning) => {
    await i18n.changeLanguage(language);
    const { container } = render(<MessageBubble message={{
      id: 'without-summary', role: 'assistant', metadata: { turn_id: 'settled' }, parts: [
        { type: 'reasoning', text: 'Planning the read', state: 'done' },
        { type: 'text', text: 'I will read the file.' },
        { type: 'dynamic-tool', toolCallId: 'read-file', toolName: 'Read', state: 'output-available',
          input: { file_path: '/workspace/example.txt' }, output: 'File contents' },
        { type: 'text', text: 'The file has been read.' },
      ],
    } as never} />);
    const heading = screen.getByTestId('assistant-turn-process-trigger');
    expect(heading.textContent).toBe(outer);
    fireEvent.click(heading);
    const groups = screen.getAllByTestId('assistant-process-trigger');
    expect(groups).toHaveLength(2);
    expect(groups[0].textContent).toContain(reasoning);
    expect(groups[1].textContent).toContain(tools);
    expect(groups.every((group) => !group.textContent?.includes(outer))).toBe(true);
    fireEvent.click(groups[1]);
    expect(groups[1].getAttribute('aria-expanded')).toBe('true');
    const toolCard = container.querySelector<HTMLElement>('[data-tool-call-id="read-file"]');
    expect(toolCard).toBeTruthy();
    fireEvent.click(within(toolCard!).getByRole('button'));
    expect(screen.getByText('File contents')).toBeTruthy();
    expect(screen.getByText('The file has been read.')).toBeTruthy();
  });
});

function renderReasoning(text: string, isStreaming?: boolean) {
  return render(
    <PartRenderer
      part={{ type: 'reasoning', text } as never}
      isActiveTurn={false}
      isStreaming={isStreaming}
      isUser={false}
    />,
  );
}

describe('reasoning part rendering', () => {
  it('renders nothing for a settled empty reasoning part', () => {
    renderReasoning('');
    expect(screen.queryByTestId('reasoning-part')).toBeNull();
  });

  it('renders nothing for a settled whitespace-only reasoning part', () => {
    renderReasoning('  \n  ');
    expect(screen.queryByTestId('reasoning-part')).toBeNull();
  });

  it('keeps the empty card while streaming — the "thinking started" affordance', () => {
    renderReasoning('', true);
    expect(screen.getByTestId('reasoning-part')).toBeTruthy();
  });

  it('renders a settled reasoning part with content, stamped with its size', () => {
    renderReasoning('planning the answer');
    const card = screen.getByTestId('reasoning-part');
    expect(card.getAttribute('data-chars')).toBe(String('planning the answer'.length));
  });

  // A rendered card alone does not prove its content is accessible. Check
  // that the trigger opens the settled block and exposes its text.
  it('keeps a settled thinking block closed until its trigger is pressed', () => {
    renderReasoning('planning the answer');

    expect(screen.queryByText('planning the answer')).toBeNull();

    fireEvent.click(screen.getByRole('button'));

    expect(screen.getByText('planning the answer')).toBeTruthy();
  });

  it('shows a streaming thinking block already open', () => {
    renderReasoning('planning the answer', true);

    expect(screen.getByText('planning the answer')).toBeTruthy();
  });
});

describe('tool part rendering', () => {
  it('marks a file diff as quoted content rather than product prose', () => {
    const { container } = render(
      <PartRenderer
        part={{
          type: 'dynamic-tool',
          toolName: 'Write',
          toolCallId: 'write-plan',
          state: 'input-available',
          input: {
            file_path: '/home/agent/.claude/plans/example.md',
            content: '# Plan\nCreate the requested file after approval.',
          },
        } as never}
        pendingToolCallId="write-plan"
        isActiveTurn={false}
        isUser={false}
      />,
    );

    const diff = container.querySelector('[data-slot="verbatim"]');
    expect(diff).toBeTruthy();
    expect(diff?.className).toContain('font-mono');
    expect(diff?.textContent).toContain('Create the requested file after approval.');
  });

  it('keeps an opened tool card open when durable history replaces the live message', () => {
    const writeMessage = (id: string) => ({
      id,
      role: 'assistant',
      metadata: { turn_id: 'turn-write' },
      parts: [{
        type: 'dynamic-tool',
        toolName: 'Write',
        toolCallId: 'write-file',
        state: 'output-available',
        input: {
          file_path: '/workspace/result.txt',
          content: 'kept across settlement',
        },
        output: 'wrote result.txt',
      }],
    } as never);
    const { container, rerender } = render(<MessageBubble message={writeMessage('turn-write')} />);

    fireEvent.click(screen.getByTestId('assistant-process-trigger'));
    const toolCard = container.querySelector<HTMLElement>('[data-tool-call-id="write-file"]');
    expect(toolCard).toBeTruthy();
    const toolTrigger = within(toolCard!).getByRole('button');
    fireEvent.click(toolTrigger);
    expect(toolTrigger.hasAttribute('data-panel-open')).toBe(true);
    expect(screen.getByText('kept across settlement')).toBeTruthy();

    rerender(<MessageBubble message={writeMessage('durable-message')} />);

    expect(container.querySelector('[data-tool-call-id="write-file"]')).toBe(toolCard);
    expect(within(toolCard!).getByRole('button')).toBe(toolTrigger);
    expect(toolTrigger.hasAttribute('data-panel-open')).toBe(true);
    expect(screen.getByText('kept across settlement')).toBeTruthy();
  });
});

/**
 * One state, one pulse.
 *
 * A streaming turn has exactly one live indicator on screen. The message-level
 * "Generating…" line is the fallback for a surface that says nothing itself;
 * a part that pulses on its own takes over, rather than pulsing beside it. A
 * reader who sees "Thinking…" and "Generating…" shimmering together reads two
 * concurrent activities where there is one.
 */
describe('a streaming message shows exactly one live indicator', () => {
  function renderStreaming(parts: unknown[], isStreaming = true) {
    return render(
      <MessageBubble
        message={{ id: 'm1', role: 'assistant', parts } as never}
        isStreaming={isStreaming}
      />,
    );
  }

  const pulses = (container: HTMLElement) => container.querySelectorAll('.astra-dot--live');

  it('pulses once before any part has arrived', () => {
    const { container } = renderStreaming([]);
    expect(pulses(container)).toHaveLength(1);
    expect(container.querySelector('[data-testid="reasoning-part"]')).toBeNull();
  });

  it('pulses once while text streams — the text does not announce itself', () => {
    const { container } = renderStreaming([{ type: 'text', text: 'answering' }]);
    expect(pulses(container)).toHaveLength(1);
  });

  it('hands the pulse to a streaming thinking block instead of doubling it', () => {
    const { container } = renderStreaming([{ type: 'reasoning', text: 'weighing options' }]);
    expect(pulses(container)).toHaveLength(1);
    expect(container.querySelector('[data-testid="reasoning-part"] .astra-dot--live')).toBeTruthy();
  });

  it('takes the pulse back when a settled thinking block is no longer the active part', () => {
    const { container } = renderStreaming([
      { type: 'reasoning', text: 'weighing options' },
      { type: 'text', text: 'answering' },
    ]);
    expect(pulses(container)).toHaveLength(1);
    expect(container.querySelector('[data-testid="reasoning-part"] .astra-dot--live')).toBeNull();
  });

  it('pulses nowhere once the turn has settled', () => {
    const { container } = renderStreaming([{ type: 'text', text: 'answered' }], false);
    expect(pulses(container)).toHaveLength(0);
  });
});

/**
 * A settled turn folds the work that ran; the call that never returned stays
 * outside the fold.
 *
 * The rendered shape is what matters, not the grouping alone: the whole-turn
 * fold selects its groups by its own rule, and a grouping that keeps an
 * unfinished call out of a process group would still let the fold sweep that
 * group up. The durable projection leaves a call with no result outside, so a
 * reader who watched the stop and one who reloads must see the same card in
 * the same place. Both settled ends are covered: a normal end with a
 * conclusion folds errored calls but never an unfinished one, and an
 * interruption leaves it outside beside its stop line.
 */
describe('a settled turn keeps an unfinished call outside its fold', () => {
  const tool = (toolCallId: string, state: string, extra: Record<string, unknown> = {}) => ({
    type: 'dynamic-tool',
    toolName: 'Bash',
    toolCallId,
    state,
    input: { command: `echo ${toolCallId}` },
    ...extra,
  });
  function renderSettled(parts: unknown[]) {
    return render(
      <MessageBubble
        message={{ id: 'settled-turn', role: 'assistant', metadata: { turn_id: 'settled-turn' }, parts } as never}
        activeTurnId={null}
        isStreaming={false}
      />,
    );
  }

  it('leaves the call a stop landed on outside, and folds the one that finished', async () => {
    const { container } = renderSettled([
      tool('finished', 'output-available', { output: 'done' }),
      tool('stopped', 'input-available'),
      { type: 'data-turn-failure', data: { error: 'Request interrupted by user', failure_phase: 'post_dispatch' } },
    ]);
    const fold = await screen.findByTestId('assistant-turn-process', undefined, { timeout: 3_000 });
    expect(fold.querySelector('[data-tool-call-id="finished"]')).toBeTruthy();
    expect(fold.querySelector('[data-tool-call-id="stopped"]')).toBeNull();
    const outside = container.querySelector('[data-tool-call-id="stopped"]');
    expect(outside).toBeTruthy();
    expect(outside?.closest('[data-testid="assistant-turn-process"]')).toBeNull();
  });

  it('keeps a call still awaiting its answer outside a normally ended turn', async () => {
    const { container } = renderSettled([
      tool('finished', 'output-available', { output: 'done' }),
      tool('errored', 'output-error', { errorText: 'boom' }),
      tool('awaiting', 'approval-responded', { approval: { id: 'a1', approved: true } }),
      { type: 'text', text: 'the conclusion' },
      { type: 'data-result', data: { usage: { input_tokens: 1, output_tokens: 1 } } },
    ]);
    const fold = await screen.findByTestId('assistant-turn-process', undefined, { timeout: 3_000 });
    expect(fold.querySelector('[data-tool-call-id="finished"]')).toBeTruthy();
    expect(fold.querySelector('[data-tool-call-id="errored"]')).toBeTruthy();
    expect(fold.querySelector('[data-tool-call-id="awaiting"]')).toBeNull();
    expect(container.querySelector('[data-tool-call-id="awaiting"]')).toBeTruthy();
    expect(screen.getByText('the conclusion')).toBeTruthy();
  });
});
