// @vitest-environment jsdom
import type { ComponentProps } from 'react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import i18n from '../../i18n';
import { SessionComposerBox } from './SessionComposerBox';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

type ComposerProps = ComponentProps<typeof SessionComposerBox>;

function composerProps(overrides: Partial<ComposerProps> = {}): ComposerProps {
  return {
    displayedQueue: [],
    removeQueueItem: vi.fn(),
    retryQueueItem: vi.fn(),
    canRetryQueued: true,
    showSlashPopup: false,
    slashPopupRef: { current: null },
    filteredSlashCommands: [],
    hasSlashCommands: true,
    slashPopupIndex: 0,
    acceptSlashCommand: vi.fn(),
    submitText: vi.fn(),
    acceptsImages: true,
    submitLabel: 'Queue',
    draft: 'queued follow-up',
    setDraft: vi.fn(),
    handleKeyDown: vi.fn(),
    canSend: true,
    isAgentRuntimeDeleted: false,
    isTerminated: false,
    lifecycleState: 'busy',
    isStreaming: true,
    isSubmitted: false,
    hasPendingInteraction: false,
    permissionMode: 'default',
    permissionModes: ['default', 'acceptEdits', 'plan', 'bypassPermissions'],
    showPermissionMode: true,
    canChangePermissionMode: true,
    modeSwitching: false,
    cyclePermissionMode: vi.fn().mockResolvedValue(undefined),
    selectPermissionMode: vi.fn().mockResolvedValue(undefined),
    isInterruptSettling: false,
    wrappedStopGeneration: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

describe('SessionComposerBox', () => {
  it('opens slash commands from a visible composer action without requiring a hidden shortcut', () => {
    const setDraft = vi.fn();
    render(
      <SessionComposerBox
        {...composerProps({ setDraft })}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Commands' }));
    expect(setDraft).toHaveBeenCalledWith('/');
  });

  it('portals the slash command menu outside the overflow-constrained reading gutter', async () => {
    const slashPopupRef = { current: null };
    render(
      <SessionComposerBox
        {...composerProps({
          draft: '/',
          showSlashPopup: true,
          slashPopupRef,
          filteredSlashCommands: [{ name: 'compact', description: 'Compact context' }],
        })}
      />,
    );

    const menu = await screen.findByTestId('slash-command-menu');
    expect(menu.closest('[data-slot="reading-gutter"]')).toBeNull();
    expect(slashPopupRef.current).toBe(menu);
  });

  it('keeps a native FIFO input visible until SDK consumption', () => {
    const marker = 'queued follow-up marker';
    render(
      <SessionComposerBox
        {...composerProps({
          displayedQueue: [{ id: 'queued-1', content: marker, status: 'queued' }],
        })}
      />,
    );

    expect(screen.getByTestId('composer-queue').textContent).toContain(marker);
    expect(screen.queryByRole('button', { name: /Remove queued message/i })).toBeNull();
  });

  it('keeps the non-native local busy queue removable', () => {
    render(
      <SessionComposerBox
        {...composerProps({
          displayedQueue: [{
            id: 'local-1',
            content: 'local follow-up',
            status: 'queued',
            source: 'local',
          }],
        })}
      />,
    );

    const remove = screen.getByRole('button', {
      name: /Remove queued message/i,
    }) as HTMLButtonElement;
    expect(remove.disabled).toBe(false);
  });

  it('keeps pointer-accessible Queue and Stop actions independent while a turn is busy', async () => {
    const submitText = vi.fn();
    const wrappedStopGeneration = vi.fn().mockResolvedValue(undefined);
    render(
      <SessionComposerBox
        {...composerProps({ submitText, wrappedStopGeneration })}
      />,
    );

    expect((screen.getByTestId('composer-prompt') as HTMLTextAreaElement).disabled).toBe(false);
    const queue = screen.getByRole('button', { name: 'Queue' }) as HTMLButtonElement;
    const stop = screen.getByRole('button', { name: 'Stop generating' }) as HTMLButtonElement;
    expect(queue.dataset.testid).toBe('composer-submit');
    expect(queue.disabled).toBe(false);
    expect(stop.dataset.testid).toBe('run-composer-stop');
    expect(stop.disabled).toBe(false);

    fireEvent.click(queue);
    await waitFor(() => {
      expect(submitText).toHaveBeenCalledWith('queued follow-up', []);
    });
    expect(wrappedStopGeneration).not.toHaveBeenCalled();

    fireEvent.click(stop);
    expect(wrappedStopGeneration).toHaveBeenCalledTimes(1);
  });

  it('locks the input and both busy actions once interruption is settling', () => {
    render(
      <SessionComposerBox
        {...composerProps({ canSend: false, isInterruptSettling: true })}
      />,
    );

    expect((screen.getByTestId('composer-prompt') as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByTestId('composer-submit') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('run-composer-stop') as HTMLButtonElement).disabled).toBe(true);
  });

  // Pasting from a slide deck puts the slide's words on the clipboard beside
  // its picture. Cancelling that paste to take the picture keeps the words out
  // of the textarea, so the composer only pre-empts a paste with nothing in it
  // to insert.
  function pasteInto(textarea: HTMLTextAreaElement, options: {
    text?: string;
    image?: boolean;
  }): { defaultPrevented: boolean } {
    const png = new File([new Uint8Array([137, 80, 78, 71])], 'slide.png', {
      type: 'image/png',
    });
    const items: Array<Record<string, unknown>> = [];
    if (options.text !== undefined) {
      items.push({ kind: 'string', type: 'text/plain', getAsFile: () => null });
    }
    if (options.image) {
      items.push({ kind: 'file', type: 'image/png', getAsFile: () => png });
    }
    const clipboardData = {
      items,
      getData: (format: string) => (
        format === 'text/plain' ? (options.text ?? '') : ''
      ),
    };
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: clipboardData });
    fireEvent(textarea, event);
    return { defaultPrevented: event.defaultPrevented };
  }

  it('keeps the text a slide paste carries and attaches the image beside it', async () => {
    render(<SessionComposerBox {...composerProps({ draft: '' })} />);
    const textarea = screen.getByTestId('composer-prompt') as HTMLTextAreaElement;

    const paste = pasteInto(textarea, { text: 'Slide title', image: true });

    // Cancelling here is what dropped the words: the textarea is controlled,
    // so the browser's own insertion is what puts them in the draft.
    expect(paste.defaultPrevented).toBe(false);
    await waitFor(() => {
      expect(screen.getByTestId('composer-attachment').getAttribute('data-filename')).toBe('slide.png');
    });
  });

  it('pre-empts a paste that carries only an image, which has nothing to insert', async () => {
    render(<SessionComposerBox {...composerProps({ draft: '' })} />);
    const textarea = screen.getByTestId('composer-prompt') as HTMLTextAreaElement;

    const paste = pasteInto(textarea, { image: true });

    expect(paste.defaultPrevented).toBe(true);
    await waitFor(() => {
      expect(screen.getByTestId('composer-attachment').getAttribute('data-filename')).toBe('slide.png');
    });
  });

  it('offers to send an image that has no caption', async () => {
    render(<SessionComposerBox {...composerProps({ draft: '' })} />);
    const submit = screen.getByTestId('composer-submit') as HTMLButtonElement;
    // An empty composer has nothing to send…
    expect(submit.disabled).toBe(true);

    pasteInto(screen.getByTestId('composer-prompt') as HTMLTextAreaElement, { image: true });

    // …but a picture on its own is a message.
    await waitFor(() => {
      expect((screen.getByTestId('composer-submit') as HTMLButtonElement).disabled).toBe(false);
    });
  });

  it('does not attach a pasted file the engine cannot read', () => {
    render(<SessionComposerBox {...composerProps({ draft: '' })} />);
    const textarea = screen.getByTestId('composer-prompt') as HTMLTextAreaElement;
    const pdf = new File([new Uint8Array([1])], 'deck.pdf', { type: 'application/pdf' });
    const event = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', {
      value: {
        items: [{ kind: 'file', type: 'application/pdf', getAsFile: () => pdf }],
        getData: () => '',
      },
    });

    fireEvent(textarea, event);

    // No chip promising to carry something the send would refuse.
    expect(screen.queryByTestId('composer-attachment')).toBeNull();
    expect(event.defaultPrevented).toBe(false);
  });

  it('does not attach an image when the engine declares text-only input', () => {
    render(
      <SessionComposerBox
        {...composerProps({ draft: '', acceptsImages: false })}
      />,
    );
    const textarea = screen.getByTestId('composer-prompt') as HTMLTextAreaElement;

    const paste = pasteInto(textarea, { image: true });

    expect(paste.defaultPrevented).toBe(false);
    expect(screen.queryByTestId('composer-attachment')).toBeNull();
  });

  it('does not stage a dropped image when the engine declares text-only input', () => {
    render(
      <SessionComposerBox
        {...composerProps({ draft: '', acceptsImages: false })}
      />,
    );
    const textarea = screen.getByTestId('composer-prompt') as HTMLTextAreaElement;
    const form = textarea.closest('form');
    const png = new File([new Uint8Array([137, 80, 78, 71])], 'slide.png', {
      type: 'image/png',
    });

    fireEvent.drop(form!, {
      dataTransfer: { files: [png], types: ['Files'] },
    });

    expect(screen.queryByTestId('composer-attachment')).toBeNull();
    expect((screen.getByTestId('composer-submit') as HTMLButtonElement).disabled).toBe(true);
  });

  it('clears a staged image when the selected engine becomes text-only', async () => {
    const props = composerProps({ draft: '' });
    const view = render(<SessionComposerBox {...props} />);
    pasteInto(screen.getByTestId('composer-prompt') as HTMLTextAreaElement, {
      image: true,
    });
    await screen.findByTestId('composer-attachment');

    view.rerender(<SessionComposerBox {...props} acceptsImages={false} />);

    await waitFor(() => {
      expect(screen.queryByTestId('composer-attachment')).toBeNull();
    });
    expect((screen.getByTestId('composer-submit') as HTMLButtonElement).disabled).toBe(true);
    view.rerender(<SessionComposerBox {...props} acceptsImages />);
    expect(screen.queryByTestId('composer-attachment')).toBeNull();
  });

  it('does not invent a permission control for an engine with no modes', () => {
    render(
      <SessionComposerBox
        {...composerProps({
          permissionMode: '',
          permissionModes: [],
          showPermissionMode: false,
        })}
      />,
    );

    expect(screen.queryByText('Default mode')).toBeNull();
  });
});
