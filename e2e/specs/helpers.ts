import { expect, type Page, type Locator } from "@playwright/test";

/**
 * Shared selectors + flows for the AstraBox Community console e2e.
 *
 * All selectors are data-testid hooks on the REAL shipping components
 * (frontend/src/...). The empty-reply failure signature the live-turn gate exists
 * to catch is centralized here so every spec asserts against the exact same literal.
 */

/** The fake-success signature a turn must NEVER render. */
export const EMPTY_REPLY_SIGNATURE = "(empty reply, exit=0)";

/**
 * Every data-testid this file navigates by, in one list.
 *
 * The layout gate uses the list to validate selectors with a stubbed API. Add an
 * entry whenever a flow below navigates by a new test id; live-turn coverage
 * requires Docker, a sandbox, and a model key.
 */
export const LIVE_TESTIDS = {
  /** Always-mounted sidebar; `openSessions` waits on it. */
  sessionsPage: "sessions-page",
  /** An agent card on `/agents`; its only button starts a session. */
  agentOption: "agent-option",
  /** An Assistant card on `/assistants`. */
  assistantOption: "assistant-option",
  /** Administrator-only credential management. */
  credentialVaultPage: "credential-vault-page",
  credentialVaultCreate: "credential-vault-create",
  credentialVaultRow: "credential-vault-row",
  credentialVaultName: "credential-vault-name",
  credentialVaultSave: "credential-vault-save",
  credentialVaultDelete: "credential-vault-delete",
  credentialAdd: "credential-add",
  credentialType: "credential-type",
  credentialTarget: "credential-target",
  credentialSecret: "credential-secret",
  credentialAllowedHosts: "credential-allowed-hosts",
  credentialSave: "credential-save",
  credentialBindingOpen: "credential-binding-open",
  credentialBindingTarget: "credential-binding-target",
  credentialBindingSave: "credential-binding-save",
  /** The session view, mounted once the session leaves CREATING. */
  runView: "run-view",
  /** Composer textarea and its submit (the submit becomes the stop button mid-turn). */
  composerPrompt: "composer-prompt",
  composerSubmit: "composer-submit",
  composerStop: "run-composer-stop",
  /** Turn content, asserted against EMPTY_REPLY_SIGNATURE. */
  userMessage: "user-message",
  assistantMessage: "assistant-message",
  assistantText: "assistant-text",
  /** Session state, read from inside the run view. */
  statusPill: "status-pill",
} as const;

export const tid = (page: Page, id: string): Locator => page.getByTestId(id);

/**
 * Open the app and wait for the sessions surface to mount.
 *
 * The real console has no dedicated `/sessions` list route — `/` and unknown
 * paths redirect to `/agents`, and the session "list" IS the always-mounted left
 * Sidebar (data-testid="sessions-page"). This helper lands on `/agents` (the
 * template gallery, where a session is created) and asserts the sidebar is present.
 */
export async function openSessions(page: Page): Promise<void> {
  await page.goto("/agents");
  await expect(tid(page, "sessions-page")).toBeVisible();
}

/**
 * Create a run the real way. On `/agents`, click an agent card's start button →
 * the app POSTs /api/v1/sessions and navigates to the RunView
 * (`/sessions/:id`). The prompt is typed in the RunView composer AFTER landing: the
 * composer textarea and its submit only enable once the per-session sandbox reaches
 * READY (a cold provision can exceed the default action timeout, so this helper waits
 * for the composer to enable before filling/submitting). An empty `prompt` kicks no turn —
 * useful for tests that just need a session row (e.g. archive).
 *
 * The card is addressed by `data-testid`/`data-agent-name` rather than by its
 * visible text, because the button's label is translated — matching on the
 * English string makes this pass or fail on the browser's locale.
 *
 * Returns the new session id parsed from the `/sessions/:id` URL.
 */
export async function createRunViaComposer(
  page: Page,
  opts: { prompt: string; agentName?: string },
): Promise<string> {
  // Pick the agent card (by name when given, else the first one).
  const options = tid(page, "agent-option");
  await expect(options.first()).toBeVisible();
  const card = opts.agentName
    ? page.locator(`[data-testid="agent-option"][data-agent-name="${opts.agentName}"]`)
    : options.first();
  await expect(card).toBeVisible();

  // The card's only button creates the session and navigates to the RunView.
  await Promise.all([
    page.waitForURL(/\/sessions\/[0-9a-f-]{36}/i, { timeout: 30_000 }),
    card.getByRole("button").click(),
  ]);

  // RunView only mounts once the session leaves CREATING (a cold sandbox
  // provision can take well over a minute), so wait past the default action
  // timeout for the run-view container to appear.
  await expect(tid(page, "run-view")).toBeVisible({ timeout: 150_000 });

  const m = page.url().match(/\/sessions\/([0-9a-f-]{36})/i);
  if (!m) throw new Error(`could not parse session id from url: ${page.url()}`);
  const sessionId = m[1];

  if (opts.prompt) {
    // The composer stays disabled until the sandbox is READY (canSend); waiting for
    // it to enable is the real "sandbox ready" signal — a cold warm-pool can take
    // well over the 15s action timeout, so give it generous headroom here.
    const composer = tid(page, "composer-prompt");
    await expect(composer).toBeEnabled({ timeout: 150_000 });
    await composer.fill(opts.prompt);
    const submit = tid(page, "composer-submit");
    await expect(submit).toBeEnabled({ timeout: 15_000 });
    await submit.click();
  }

  return sessionId;
}

/**
 * Read the current run-header StatusPill state attribute (the UI-state string the
 * backend derived, e.g. CREATING / READY / PROCESSING). Returns null if no pill.
 */
export async function runHeaderPillState(page: Page): Promise<string | null> {
  const pill = tid(page, "run-view").getByTestId("status-pill").first();
  if ((await pill.count()) === 0) return null;
  return pill.getAttribute("data-state");
}
