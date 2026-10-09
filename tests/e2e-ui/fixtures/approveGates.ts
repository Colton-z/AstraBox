/**
 * Answer the gates an engine puts on a posted turn, as the Agent's user would.
 *
 * An engine may gate a tool call the way it gates any unannotated tool (Codex
 * asks through its native elicitation). A turn posted without a stream holding
 * it waits parked for the answer, so this answers each gate with the option
 * that allows the call once, until the turn settles. An answered gate can
 * still read as pending for a moment after the answer lands, so each id is
 * answered once.
 */
import { expect } from '@playwright/test';

import type { AstraApi } from './astraApi';

export async function approveGates(api: AstraApi, sessionId: string): Promise<number> {
  const answered = new Set<string>();
  const deadline = Date.now() + 150_000;
  while (Date.now() < deadline) {
    const pending = await api.waitForPendingInteractionOrSettledTurn(sessionId, 90_000);
    if (!pending) return answered.size;
    if (answered.has(pending.interaction_id)) {
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      continue;
    }
    const ids = ((pending.options ?? []) as Array<Record<string, unknown>>).map((option) => String(option.id ?? ''));
    const decision = ['accept', 'approve', 'allow'].find((id) => ids.includes(id)) ?? 'approve';
    await api.answerPendingInteraction(sessionId, pending.interaction_id, { decision });
    answered.add(pending.interaction_id);
    expect(answered.size, 'one tool call raises at most a few gates').toBeLessThan(4);
  }
  throw new Error(`session ${sessionId} did not settle after its gates were answered`);
}
