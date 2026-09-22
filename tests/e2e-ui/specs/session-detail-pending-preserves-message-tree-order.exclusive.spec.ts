/**
 * E2E: a pending interaction preserves chronological message-part order.
 *
 * One default-mode turn requests Write followed by Bash. After the user approves
 * Write, the same turn must pause on Bash. The completed Write card and any
 * reasoning card must remain above the pending Bash card; the Bash card must sit
 * above the approval panel. Neither the page nor durable messages may expose raw
 * tool-protocol markup.
 *
 * Language-dependent labels match both shipped locales, while tool names and the
 * run-specific path provide stable anchors. Each permission is model-dependent,
 * so bounded probes skip when the requested two-tool sequence does not occur.
 */
import { test, expect, type Page } from '@playwright/test';

import { insist } from '../fixtures/insist';
import { AstraApi, messageText, type PendingInteraction } from '../fixtures/astraApi';
import { revealAssistantProcess } from '../fixtures/assistantProcess';
import { trackSessions } from '../fixtures/sessionCleanup';
import { appPath, parseTimeoutEnv } from '../fixtures/env';

// One sandbox provision + two paused tools + the approve-resume continuation +
// the UI settle sit well above the 240s suite default; keep it generous/tunable.
// Bounded probe: how long to wait for the FIRST pending interaction before
// the test skips. The POST stream closes at the interaction's segment
// `finish`, so a real pending is visible promptly; this budget only covers
// projection lag.
/** What a user says when a model answers instead of acting. */
const INSIST_NUDGE =
  "You answered without using the tool. Do the tool call now, exactly as asked above, before replying again. \u8bf7\u73b0\u5728\u5c31\u6309\u4e0a\u9762\u7684\u8981\u6c42\u8c03\u7528\u5de5\u5177\uff0c\u4e0d\u8981\u53ea\u7528\u6587\u5b57\u56de\u7b54\u3002";

const PROBE_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_PROBE_TIMEOUT_MS', 60_000);
// After approving the first tool, how long to wait for the distinct Bash pending.
const SECOND_PENDING_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_PENDING_TIMEOUT_MS', 180_000);
// How long to let the paused turn's inline Bash card + pending panel render.
const RENDER_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TREE_ORDER_RENDER_TIMEOUT_MS', 60_000);
// How long to let the pending scroll settle before the spatial/order assertions.
const SPATIAL_TIMEOUT_MS = parseTimeoutEnv('ASTRABOX_E2E_TREE_ORDER_SPATIAL_TIMEOUT_MS', 30_000);
// Max px the pending card's bottom may sit above the pending panel's top (the
// card must be just above the panel — not covered, not pushed far away). A
// spatial tolerance, not a timeout; env-tunable for layout drift.
const GAP_MAX_PX = parseTimeoutEnv('ASTRABOX_E2E_TREE_ORDER_GAP_MAX_PX', 220);

// The tool protocol must never leak into rendered text as literal `<write>`/`<bash>`.
const RAW_TOOL_MARKUP = /<\/?\s*(write|bash)(?:\s|>|\/)/i;
// The inline pending tool card header: the "Bash" tool name + the awaiting-
// confirmation badge (chat:tool_state.awaiting_confirmation — en/zh).
const BASH_PENDING_HEADER = /Bash\s+(Awaiting confirmation|等待确认)/i;
// The composer approve button (misc:composer.allow_continue / apply_suggestion_continue — en/zh).
const APPROVE_BUTTON = /Allow and continue|Apply suggestion and continue|允许并继续|应用建议并继续/;
// The reasoning card eyebrow (chat:message.thought_process — en/zh); opportunistic.
const REASONING_LABEL = /Reasoning|思考过程/;

// ── Page helpers ───────────────────────────────────────────────────────────
async function openSessionView(page: Page, sessionId: string): Promise<void> {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto(appPath(`/sessions/${sessionId}`), { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('run-view')).toBeVisible({ timeout: 45_000 });
}

// Filling avoids key events that could submit the multiline prompt early. The
// user bubble confirms that the page dispatched the turn.
async function sendPromptFromComposer(page: Page, prompt: string, echoSubstring: string): Promise<void> {
  const composer = page.getByTestId('composer-prompt');
  await expect(composer, 'the composer must be enabled before sending').toBeEnabled({ timeout: 45_000 });
  await composer.fill(prompt);
  const submit = page.getByTestId('composer-submit');
  await expect(submit).toBeEnabled({ timeout: 15_000 });
  await submit.click();
  await expect(
    page.getByTestId('user-message').filter({ hasText: echoSubstring }).last(),
    'the user bubble should render — proof the turn was dispatched',
  ).toBeVisible({ timeout: 30_000 });
}

async function closePageAfterAssertions(page: Page): Promise<void> {
  await page.goto('about:blank', { waitUntil: 'domcontentloaded', timeout: 5_000 }).catch(() => undefined);
  await page.close({ runBeforeUnload: false }).catch(() => undefined);
}

// Sessions created here are deleted only when the test passes. A failure keeps
// the scene and names it in the report tail — see fixtures/sessionCleanup.ts.
//
// This spec kept its own scene already, through a `catch` that rethrows before
// reaching the delete. That covers a thrown assertion and not a timeout or an
// interrupt, which never unwind through it; the tracker's KEEP set covers all
// three, and resolves the sandbox id while the session still answers.
const sessions = trackSessions();

test('session-detail pending does not reorder the browser message tree before live resume', async ({ request, page }) => {
  const api = new AstraApi(request);
  const runId = Date.now();
  const targetPath = `e2e-ui-order-${runId}.py`;

  const agent = await api.defaultAgent();
  const created = await api.startConversation(agent.agent_id);
  const sessionId = created.session_id;
  sessions.push(sessionId);

  try {
    await api.waitForSessionReady(sessionId);

    // The UI dispatches the turn under the session's permission_mode
    // (useSessionChat sends `permission_mode: permissionModeRef.current`, seeded
    // from session.permission_mode). Community conversations open in
    // bypassPermissions, so switch to 'default' BEFORE opening the page — that is
    // what makes the Write/Bash tool permissions surface.
    await api.setPermissionMode(sessionId, 'default');
    const ready = await api.getSession(sessionId);
    expect(
      ready.permission_mode,
      'UI order regression must run with tool approvals enabled (permission_mode=default)',
    ).toBe('default');

    await openSessionView(page, sessionId);

    const prompt = [
      `E2E_TOOL_ORDER_${runId}`,
      'You must follow this exact tool protocol. The task is not complete until both tool calls below have completed.',
      `Step 1: call the Write tool exactly once to create relative file ${targetPath} with exactly this one-line content: print("tool order ${runId}")`,
      'After the Write tool permission is approved and the Write result is available, keep the same assistant turn active: emit one brief bridge sentence that does not contain the words Write or Bash, then continue without stopping.',
      `Step 2: immediately call the Bash tool exactly once with exactly this command: python3 ${targetPath}`,
      'Do not use any other tool. Do not skip Bash. Do not end the turn after Step 1. If a tool permission prompt appears, wait for approval and then continue with the next required tool.',
    ].join('\n');
    await sendPromptFromComposer(page, prompt, `E2E_TOOL_ORDER_${runId}`);

    // ── GATE 1 (probe): the first native tool permission. deepseek-chat may never
    //    raise one (the turn just finishes with stop); skip — do not fake-pass. ──
    // Ask again rather than skip: a skip ends the round exactly as a
    // failure does, so the model's choice decided it instead of the
    // platform. The probe returns the moment the turn settles ungated,
    // so a declined ask costs that turn rather than the whole budget.
    const first = await insist<PendingInteraction>({
      ask: async (attempt) => {
        if (attempt > 1) await api.postTurnInput(sessionId, INSIST_NUDGE);
      },
      probe: () =>
        api.waitForPendingInteractionOrSettledTurn(sessionId, PROBE_TIMEOUT_MS),
      what: "deepseek-chat raised no tool-permission interaction for the Write step under permission_mode 'default' " + '(the turn finished with stop) — no paused tool to resume, nothing to assert tree-order against',
      budgetMs: PROBE_TIMEOUT_MS * 2,
      probeMs: PROBE_TIMEOUT_MS,
    });
    expect(first.presentation, 'a raised interaction for the Write step should be a tool approval').toBe('tool_approval');
    test.skip(
      String(first.tool_name || '').trim() !== 'Write',
      `deepseek-chat raised a ${first.tool_name} permission first, not Write — the Write→Bash ` +
        'protocol precondition for the tree-order assertion was not met',
    );

    // ── Approve the first (Write) permission from the composer pending footer,
    //    like a user — this resumes the SAME turn. ──────────────────────────────
    const pendingPanel = page.getByTestId('pending-interaction-panel');
    await expect(pendingPanel, 'the Write permission should surface the composer pending panel').toBeVisible({
      timeout: RENDER_TIMEOUT_MS,
    });
    const approveButton = pendingPanel.getByRole('button', { name: APPROVE_BUTTON }).last();
    await expect(approveButton, 'the approve button should be visible in the pending panel').toBeVisible({
      timeout: 45_000,
    });
    await approveButton.click();

    // ── GATE 2 (probe): the distinct Bash permission after the resume. If deepseek
    //    does not chain to Bash, skip with a precise reason. ─────────────────────
    const secondDeadline = Date.now() + SECOND_PENDING_TIMEOUT_MS;
    let secondPending: PendingInteraction | null = null;
    let lastDetail: Awaited<ReturnType<AstraApi['getSession']>> | null = null;
    while (Date.now() < secondDeadline) {
      lastDetail = await api.getSession(sessionId);
      const pending = lastDetail.pending_interaction ?? null;
      if (
        pending &&
        String(pending.interaction_id || '').trim() !== first.interaction_id &&
        String(pending.tool_name || '').trim() === 'Bash'
      ) {
        secondPending = pending;
        break;
      }
      await page.waitForTimeout(1_500);
    }
    test.skip(
      secondPending === null,
      'deepseek-chat did not chain to a distinct Bash tool permission after approving Write ' +
        `(last detail: state=${lastDetail?.state} last_turn_status=${lastDetail?.last_turn_status} ` +
        `pending=${JSON.stringify(lastDetail?.pending_interaction ?? null)}) — the second-tool ` +
        'precondition for the tree-order assertion was not met',
    );
    const second = secondPending as PendingInteraction;
    expect(second.presentation, 'the second pending interaction should also be a tool approval').toBe('tool_approval');
    expect(
      String(second.tool_call_id || '').trim(),
      'the Bash pending interaction should expose a real tool_call_id',
    ).not.toEqual('');

    // ── The Bash pending renders as an INLINE tool card inside the assistant
    //    transcript — not only as a composer-footer summary. ────────────────────
    const assistantTranscript = page.getByTestId('assistant-message').last();
    await expect(
      assistantTranscript.getByText('Bash', { exact: true }),
      'Bash pending must render as an inline tool card inside the assistant transcript, not only as composer pending',
    ).toBeVisible({ timeout: RENDER_TIMEOUT_MS });
    const bashPendingTool = assistantTranscript.getByRole('button', { name: BASH_PENDING_HEADER }).last();
    await expect(
      bashPendingTool,
      'the Bash pending tool card header (Bash + awaiting-confirmation) must be visible in the last assistant message',
    ).toBeVisible({ timeout: RENDER_TIMEOUT_MS });
    await expect(pendingPanel, 'the composer pending panel should be visible below the transcript').toBeVisible();

    // ── No raw tool-protocol markup leaks — live transcript AND durable messages. ─
    // The approved Write call is finished work, so it sits inside the turn's
    // process group, which starts closed; the pending Bash card and the panel
    // below it stay outside. Opening the group is what puts the approved card
    // back on screen, and both the markup read and the order assertions below
    // are about what the reader sees there.
    await revealAssistantProcess(page);
    const transcriptText = await assistantTranscript.innerText();
    expect(
      transcriptText,
      'the last assistant transcript should not render raw tool-protocol markup while Bash is pending',
    ).not.toMatch(RAW_TOOL_MARKUP);
    const durable = await api.getMessages(sessionId, 50);
    const durableAssistantText = durable.messages
      .filter((message) => message.role === 'assistant')
      .map((message) => messageText(message))
      .join('\n');
    expect(
      durableAssistantText,
      'durable assistant messages should not persist raw tool-protocol markup during the pending resume',
    ).not.toMatch(RAW_TOOL_MARKUP);

    // ── The pending Bash card sits visibly ABOVE the pending panel — not covered,
    //    not pushed away. ───────────────────────────────────────────────────────
    await expect
      .poll(
        async () => {
          const [bashBox, panelBox] = await Promise.all([
            bashPendingTool.boundingBox(),
            pendingPanel.boundingBox(),
          ]);
          if (!bashBox || !panelBox) {
            return false;
          }
          const gap = panelBox.y - (bashBox.y + bashBox.height);
          return bashBox.y >= 0 && gap >= 0 && gap <= GAP_MAX_PX;
        },
        {
          timeout: SPATIAL_TIMEOUT_MS,
          message: 'the paused turn\'s Bash tool card should sit visibly above the pending panel, not covered or pushed away',
        },
      )
      .toBe(true);

    // ── CORE: no reorder. The already-approved Write card must stay ABOVE the
    //    pending Bash card in the message tree (its target path is a unique,
    //    language-neutral anchor). Bounding-box Y is robust whether the two tools
    //    landed in one assistant message or split across two. ───────────────────
    const writeCardAnchor = page.getByTestId('assistant-message').getByText(targetPath).first();
    await expect(
      writeCardAnchor,
      'the approved Write tool card (its target path) should remain rendered in the transcript',
    ).toBeVisible({ timeout: RENDER_TIMEOUT_MS });
    const [writeBox, bashBoxForOrder] = await Promise.all([
      writeCardAnchor.boundingBox(),
      bashPendingTool.boundingBox(),
    ]);
    expect(writeBox && bashBoxForOrder, 'both the Write card and the Bash card should have a layout box').toBeTruthy();
    expect(
      (writeBox as { y: number }).y,
      'the earlier (approved) Write tool card must stay ABOVE the pending Bash tool card — ' +
        'the pending interaction must not reorder/hoist the browser message tree',
    ).toBeLessThan((bashBoxForOrder as { y: number }).y);

    // ── Optional reasoning order: if the model emitted a reasoning card, it
    //    must sit above Bash too.
    //    deepseek-chat emits no reasoning, so absence is annotated, not failed. ──
    const reasoningCard = page.getByTestId('assistant-message').getByText(REASONING_LABEL).first();
    if ((await reasoningCard.count()) > 0) {
      const reasoningBox = await reasoningCard.boundingBox();
      if (reasoningBox) {
        expect(
          reasoningBox.y,
          'a rendered reasoning card must also stay above the pending Bash card',
        ).toBeLessThan((bashBoxForOrder as { y: number }).y);
      }
    } else {
      test.info().annotations.push({
        type: 'e2e_reasoning_card_absent',
        description:
          'no reasoning card rendered (deepseek-chat emits no reasoning tokens); the no-reorder ' +
          'invariant is proven via the approved Write card staying above the pending Bash card',
      });
    }

    await closePageAfterAssertions(page);
  } catch (error) {
    // On failure the session IS the evidence: the inline-card miss can only be
    // discriminated (projection dropped the tool blocks vs the client rendered
    // from the wrong source) by reading the durable event-derived blocks and resume
    // stream AFTER the red — and teardown purges exactly that unless this catch
    // keeps it. test.info().status is not final inside the test body's own
    // finally, so keying teardown-skip logic on it deletes the evidence anyway;
    // an explicit catch is the reliable boundary. Mirrors keep-failed-sandboxes.
    console.log('TREE_ORDER_EVIDENCE_PRESERVED', JSON.stringify({ session_id: sessionId }));
    throw error;
  }
});
