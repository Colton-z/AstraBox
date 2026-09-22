import { describe, it, expect, beforeAll } from 'vitest';

import i18n from '@/i18n';
import type {
  PendingPlanConfirmationInteraction,
  PendingToolPermissionInteraction,
} from '../types';
import {
  getInteractionPermissionMode,
  getNextPermissionMode,
  normalizePermissionMode,
  stateTone,
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

describe('normalizePermissionMode', () => {
  it('passes valid modes through unchanged', () => {
    expect(normalizePermissionMode('default')).toBe('default');
    expect(normalizePermissionMode('acceptEdits')).toBe('acceptEdits');
    expect(normalizePermissionMode('plan')).toBe('plan');
    expect(normalizePermissionMode('bypassPermissions')).toBe('bypassPermissions');
  });

  it('carries an engine-owned name verbatim and maps nullish values to empty', () => {
    expect(normalizePermissionMode('observe-only')).toBe('observe-only');
    expect(normalizePermissionMode('')).toBe('');
    expect(normalizePermissionMode(undefined)).toBe('');
    expect(normalizePermissionMode(null)).toBe('');
  });
});

describe('getInteractionPermissionMode', () => {
  // The record an adapter declares for a plan decision: the approving option
  // carries the modes it may transition into, the denying ones carry the mode
  // they fall back to.
  const decision: PendingPlanConfirmationInteraction = {
    interaction_id: 'interaction-1',
    turn_id: 'turn-1',
    tool_name: 'ExitPlanMode',
    presentation: 'decision',
    options: [
      {
        id: 'approve',
        denial: false,
        permission_mode_choices: ['bypassPermissions', 'acceptEdits', 'default'],
        default_permission_mode: 'default',
      },
      { id: 'reject', denial: true, applies_permission_mode: 'plan' },
    ],
  };

  it('returns the mode the answer chose among the option', () => {
    expect(getInteractionPermissionMode(decision, {
      interaction_id: 'interaction-1',
      decision: 'approve',
      permission_mode: 'acceptEdits',
    })).toBe('acceptEdits');
  });

  it('falls back to the mode the option declared as its default, not to a UI guess', () => {
    expect(getInteractionPermissionMode(decision, {
      interaction_id: 'interaction-1',
      decision: 'approve',
    })).toBe('default');
  });

  it('returns the mode a denying option applies', () => {
    expect(getInteractionPermissionMode(decision, {
      interaction_id: 'interaction-1',
      decision: 'reject',
    })).toBe('plan');
  });

  it('returns null for an option the record never declared', () => {
    expect(getInteractionPermissionMode(decision, {
      interaction_id: 'interaction-1',
      decision: 'revise',
    })).toBeNull();
  });

  it('reads the declared contract, not the native tool name', () => {
    // Same vendor name, presented as a plain approval: with no decision
    // options there is no declared transition, so none is predicted.
    const approval: PendingToolPermissionInteraction = {
      interaction_id: 'interaction-2',
      turn_id: 'turn-1',
      tool_name: 'ExitPlanMode',
      presentation: 'tool_approval',
    };
    expect(getInteractionPermissionMode(approval, {
      interaction_id: 'interaction-2',
      decision: 'approve',
    })).toBeNull();
  });
});

describe('stateTone', () => {
  it('maps representative states to their expected tone class', () => {
    expect(stateTone('READY')).toBe('state-ready');
    expect(stateTone('WAITING_INPUT')).toBe('state-ready');
    expect(stateTone('BUSY')).toBe('state-busy');
    expect(stateTone('SENDING')).toBe('state-busy');
    expect(stateTone('RECOVERY_REQUIRED')).toBe('state-alert');
    expect(stateTone('TERMINATED')).toBe('state-alert');
  });

  it('returns the neutral tone for undefined / unknown states', () => {
    expect(stateTone(undefined)).toBe('state-neutral');
    expect(stateTone('CREATING')).toBe('state-neutral');
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
