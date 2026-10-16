import { describe, it, expect, beforeAll } from 'vitest';

import i18n from '@/i18n';
import {
  getNextPermissionMode,
  localizeDisplayText,
  isAssistantConversationSession,
} from './format';

const CLAUDE_MODES = ['default', 'acceptEdits', 'plan', 'bypassPermissions'];

// Baseline PURE-LOGIC unit tests for the stable exported helpers in
// utils/format.ts. No network, no backend, no DOM — these exercise the exports
// exactly as shipped. The i18n instance auto-inits on import (see src/i18n);
// the beforeAll below forces it to English so the wire-string localization
// assertions are stable regardless of the host's detected locale.
beforeAll(async () => {
  await i18n.changeLanguage('en');
});

describe('getNextPermissionMode', () => {
  it('cycles default -> acceptEdits -> plan -> bypassPermissions -> default', () => {
    expect(getNextPermissionMode('default', CLAUDE_MODES)).toBe('acceptEdits');
    expect(getNextPermissionMode('acceptEdits', CLAUDE_MODES)).toBe('plan');
    expect(getNextPermissionMode('plan', CLAUDE_MODES)).toBe('bypassPermissions');
    expect(getNextPermissionMode('bypassPermissions', CLAUDE_MODES)).toBe('default');
  });

  it('walking the full cycle returns to the start in CYCLE-length steps', () => {
    let mode = CLAUDE_MODES[0];
    for (let i = 0; i < CLAUDE_MODES.length; i += 1) {
      mode = getNextPermissionMode(mode, CLAUDE_MODES);
    }
    expect(mode).toBe(CLAUDE_MODES[0]);
  });
});

describe('localizeDisplayText', () => {
  it("localizes a known wire string ('sandbox expired' -> 'Sandbox expired')", () => {
    // 'sandbox expired' maps to misc:wire.sandbox_expired, whose en value is
    // 'Sandbox expired' (see src/i18n/locales/en/misc.json).
    expect(localizeDisplayText('sandbox expired')).toBe('Sandbox expired');
  });

  it('passes an unmapped string through unchanged', () => {
    const passthrough = 'this string is not in the wire replacement table';
    expect(localizeDisplayText(passthrough)).toBe(passthrough);
  });

  it('localizes a mapped substring embedded in surrounding text', () => {
    // The helper uses sequential split/join, so an embedded wire substring is
    // still replaced while the rest of the text is preserved.
    expect(localizeDisplayText('reason: sandbox expired (last_error)')).toBe(
      'reason: Sandbox expired (last_error)',
    );
  });
});

describe('isAssistantConversationSession', () => {
  it('uses the platform product instead of an engine allowlist', () => {
    expect(isAssistantConversationSession({
      session_kind: 'assistant_chat',
      engine_kind: 'third_party_engine',
    })).toBe(true);
    expect(isAssistantConversationSession({
      session_kind: 'agent_chat',
      engine_kind: 'assistant',
    })).toBe(false);
  });
});
