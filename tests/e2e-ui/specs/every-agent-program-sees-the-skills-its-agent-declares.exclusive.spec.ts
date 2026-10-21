/**
 * E2E: every bundled Agent program discovers the Skills its Agent declares.
 *
 * Each engine has its own Skills root (Claude Code its configuration
 * directory, Codex, Pi and DeepSeek Harness the shared `~/.agents/skills`),
 * which its adapter declares and the conversation bootstrap links the Agent's
 * Skills into. Agents prewarm by default, so the conversation claims a box the
 * platform prepared before anyone talked to the Agent, which is where its
 * Skills were linked. The evidence is the engine's own Skill index: asked to
 * name the Skills available to it without using any tool, the Agent names the
 * pinned public Skill, whose name appears nowhere in the prompt.
 */
import { randomUUID } from 'node:crypto';
import { expect, test } from '@playwright/test';

import { AstraApi } from '../fixtures/astraApi';
import { engineCases, engineProfileFor } from '../fixtures/engineProfile';
import { PlatformApi } from '../fixtures/platformApi';
import { onPassOnly, trackSessions } from '../fixtures/sessionCleanup';

const SKILL = 'https://github.com/anthropics/skills.git@683bc88e56f3e09ba94f7055977f3d3aa499f202#skills/brand-guidelines';

const sessions = trackSessions();
const agentIds: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const agentId of agentIds) await api.deleteAgent(agentId);
});

for (const engine of engineCases()) {
  test(`${engine.engine_kind}: an Agent's program lists the Skill its Agent declares`, async ({ request }) => {
    const api = new AstraApi(request);
    const profile = engineProfileFor(engine.engine_kind);
    const agent = await api.createAgent({
      name: `__e2e_skill_${engine.engine_kind}_${randomUUID().slice(0, 8)}`,
      model: profile.model,
      runtime_profile_name: profile.runtime_profile_name,
      prepared_capacity: 1,
      skills: [SKILL],
    });
    agentIds.push(agent.agent_id);
    const platform = new PlatformApi(request);
    let prepared: Record<string, unknown> = {};
    await expect.poll(async () => {
      prepared = await platform.preparedRuntime(agent.agent_id);
      return prepared.ready === true && Number(prepared.prepared_count || 0) > 0 && Boolean(prepared.sandbox_id);
    }, { timeout: 120_000, intervals: [2_000], message: 'the Agent must prepare a runtime' }).toBe(true);
    const sessionId = String((await api.startConversation(agent.agent_id)).session_id || '');
    sessions.push(sessionId);
    const ready = await api.waitForSessionReady(sessionId, 120_000);
    expect(ready.sandbox_id, 'the conversation claims the prepared box').toBe(prepared.sandbox_id);
    const turn = await api.sendTurn(sessionId, [
      'Without using any tool, list the names of the Skills available to you, one per line,',
      'exactly as your instructions name them, and nothing else.',
    ].join(' '), 120_000);
    expect(turn.errorText).toBeFalsy();
    expect(turn.toolNames, 'the answer comes from the Skill index, not a tool').toEqual([]);
    expect(turn.text).toContain('brand-guidelines');
  });
}
