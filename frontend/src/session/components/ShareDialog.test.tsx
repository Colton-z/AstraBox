// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeAll, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import { ShareDialog } from './ShareDialog';

// Rendering smoke test for the self-contained share-link dialog: it mounts its
// own trigger button. The dialog starts closed, so this only exercises the
// trigger shell — opening it would fetch the share config through src/api.ts
// and mount the expiry `Select`'s popup, whose pointer handling needs browser
// APIs jsdom does not implement (hasPointerCapture, scrollIntoView).

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

describe('ShareDialog', () => {
  it('renders a closed trigger button and performs no network call until opened', () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    render(<ShareDialog sessionId="session-123" />);

    const trigger = screen.getByRole('button', { name: 'Share' });
    expect(trigger).toBeTruthy();
    expect(trigger.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('Share conversation')).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();

    vi.unstubAllGlobals();
  });
});
