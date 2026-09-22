// @vitest-environment jsdom
/**
 * The two things the panel's chrome has to keep doing: the prompt still sends,
 * and the copy control can be named and reached.
 *
 * The prompt is an `InputGroupInput`, and a key handler that stops firing looks
 * exactly like a session refusing commands. `TerminalCopyButton` arrives from
 * `components/ai-elements/terminal` with an icon and no accessible name at all,
 * so "it renders" says nothing about whether anyone can find it.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import i18n from '@/i18n';

const api = vi.hoisted(() => ({
  runTerminalCommandStream: vi.fn(async () => {}),
  interruptSession: vi.fn(async () => {}),
}));

vi.mock('../api', () => api);

import TerminalPanel from './TerminalPanel';

beforeEach(async () => {
  api.runTerminalCommandStream.mockClear();
  api.interruptSession.mockClear();
  await i18n.changeLanguage('en');
});

afterEach(cleanup);

function renderPanel(enabled = true) {
  return render(
    <TerminalPanel sessionId="s-1" enabled={enabled} initialCwd="/home/agent/workspace" />,
  );
}

describe('TerminalPanel', () => {
  it('runs what was typed when the prompt takes Enter', () => {
    renderPanel();

    const prompt = screen.getByPlaceholderText(i18n.t('misc:terminal.input_placeholder'));
    fireEvent.change(prompt, { target: { value: 'ls -la' } });
    fireEvent.keyDown(prompt, { key: 'Enter' });

    expect(api.runTerminalCommandStream).toHaveBeenCalledTimes(1);
    const [sessionId, command] = api.runTerminalCommandStream.mock.calls[0] as unknown as [string, string];
    expect(sessionId).toBe('s-1');
    expect(command).toBe('ls -la');
  });

  it('does not run an empty prompt', () => {
    renderPanel();

    const prompt = screen.getByPlaceholderText(i18n.t('misc:terminal.input_placeholder'));
    fireEvent.keyDown(prompt, { key: 'Enter' });

    expect(api.runTerminalCommandStream).not.toHaveBeenCalled();
  });

  /*
    The vendored `Terminal` uses a fixed Tailwind neutral ramp outside the
    product palette (docs/frontend-design.md §7, enforced by
    `scripts/check_palette.py`). `TerminalPanel` overrides those colours at the
    composition boundary, including any optional terminal element that it
    renders.

    This test checks the rendered palette as a property instead of pinning the
    current override classes. The baseline row for the vendored source in
    `scripts/palette_baseline.json` counts source names and cannot establish
    what this panel paints.
  */
  it('paints nothing from outside the palette, whatever the vendored terminal spells', () => {
    const { container } = renderPanel();

    const ramp = /\b(?:slate|gray|zinc|neutral|stone)-(?:50|[1-9]00|950)\b/;
    const offPalette = Array.from(container.querySelectorAll<HTMLElement>('[class]'))
      .map((el) => el.className)
      .filter((name) => typeof name === 'string' && ramp.test(name));

    expect(offPalette).toEqual([]);
  });

  it('names the copy control, and offers it only once there is output to copy', () => {
    renderPanel();

    // The name is the assertion: the registry's button carries an icon and
    // nothing else, so without the label supplied at the composition this
    // query finds no element at all.
    const copy = screen.getByRole('button', { name: i18n.t('misc:terminal.copy_output') });
    expect((copy as HTMLButtonElement).disabled).toBe(true);

    const prompt = screen.getByPlaceholderText(i18n.t('misc:terminal.input_placeholder'));
    fireEvent.change(prompt, { target: { value: 'echo hi' } });
    fireEvent.keyDown(prompt, { key: 'Enter' });

    expect((screen.getByRole('button', { name: i18n.t('misc:terminal.copy_output') }) as HTMLButtonElement).disabled).toBe(false);
  });
});
