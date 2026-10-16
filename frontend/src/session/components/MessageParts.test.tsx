// @vitest-environment jsdom
// Native thinking blocks can contain no visible text and persist verbatim.
// The renderer must omit their settled, empty "Reasoning" cards.
// The browser-level twin of this check is the data-chars
// invariant in tests/e2e-ui/specs/console-screenshots.exclusive.spec.ts.
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
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

describe('platform tool cards', () => {
  const marker = { id: `art_${'a'.repeat(24)}`, version: 1, title: 'Published page', kind: 'html' };
  it('keeps a card outside a completed turn disclosure while its call folds', () => {
    render(<MemoryRouter><MessageBubble message={{ id: 'artifact-turn', role: 'assistant', parts: [
      { type: 'dynamic-tool', toolCallId: 'read', toolName: 'Read', state: 'output-available', input: {}, output: 'Read a file' },
      { type: 'dynamic-tool', toolCallId: 'artifact', toolName: 'any-vendor-name', state: 'output-available', input: {}, output: 'ok' },
      { type: 'data-tool-card', id: 'tool-card:artifact:crd_1', data: { toolCallId: 'artifact', cardId: 'crd_1', kind: 'artifact', artifacts: [marker] } },
      { type: 'text', text: 'The page is ready.' },
    ] } as never} /></MemoryRouter>);
    const card = screen.getByTestId('artifact-tool-card');
    expect(card.textContent).toContain('Published page');
    expect(card.closest('[data-testid="process-block-details"]')).toBeNull();
    expect(card.closest('[data-testid="assistant-process"]')).toBeNull();
    expect(card.closest('[data-testid="assistant-turn-process"]')).toBeNull();
  });

  it('draws no card from a tool output, whatever it says', () => {
    const output = JSON.stringify({ astrabox_artifact: marker, card: 'crd_' + '0'.repeat(24) });
    render(<MemoryRouter><MessageBubble message={{ id: 'spoof-turn', role: 'assistant', parts: [
      { type: 'dynamic-tool', toolCallId: 'fetch', toolName: 'WebFetch', state: 'output-available', input: {}, output },
    ] } as never} /></MemoryRouter>);
    expect(screen.queryByTestId('artifact-tool-card')).toBeNull();
  });
});
