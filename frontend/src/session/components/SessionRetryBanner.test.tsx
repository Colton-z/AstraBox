// @vitest-environment jsdom
import { describe, it, expect, afterEach, beforeAll, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';

import i18n from '../../i18n';
import type { RetryIntent } from '../utils/chatHelpers';
import { SessionRetryBanner } from './SessionRetryBanner';

beforeAll(async () => {
  await i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
});

const NONE: RetryIntent = { kind: 'none' };

describe('SessionRetryBanner', () => {
  it('shows the generic backend-unavailable copy and ignores the retry intent while transient', () => {
    render(
      <SessionRetryBanner
        showTransientBackendMessage
        retryIntent={{ kind: 'turn-failed', summary: 'should not show' }}
        bannerErrorMessage=""
        showRetryButton={false}
        handleRetry={() => {}}
      />,
    );

    expect(screen.getByText('The backend is temporarily unavailable. Please try again shortly.')).toBeTruthy();
    expect(screen.queryByText('should not show')).toBeNull();
  });

  it('shows the retry intent summary and uses its label on the retry button when present', () => {
    const handleRetry = vi.fn();
    render(
      <SessionRetryBanner
        showTransientBackendMessage={false}
        retryIntent={{ kind: 'recover', label: 'Reconnect', summary: 'Connection lost' }}
        bannerErrorMessage=""
        showRetryButton
        handleRetry={handleRetry}
      />,
    );

    expect(screen.getByText('Connection lost')).toBeTruthy();
    const button = screen.getByRole('button', { name: 'Reconnect' });
    button.click();
    expect(handleRetry).toHaveBeenCalledTimes(1);
  });

  it('falls back to the generic "Retry" label when the intent carries no label', () => {
    render(
      <SessionRetryBanner
        showTransientBackendMessage={false}
        retryIntent={{ kind: 'turn-failed', summary: 'Turn failed' }}
        bannerErrorMessage=""
        showRetryButton
        handleRetry={() => {}}
      />,
    );

    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('renders only the banner error message and no retry button for kind "none" with retry disabled', () => {
    render(
      <SessionRetryBanner
        showTransientBackendMessage={false}
        retryIntent={NONE}
        bannerErrorMessage="last_error: sandbox unreachable"
        showRetryButton={false}
        handleRetry={() => {}}
      />,
    );

    expect(screen.getByText('last_error: sandbox unreachable')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});
