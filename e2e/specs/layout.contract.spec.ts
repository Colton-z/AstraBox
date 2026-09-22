import { test, expect } from "@playwright/test";
import { LIVE_TESTIDS } from "./helpers";
import { settle, stubApi } from "./layoutHelpers";

/**
 * Verify the selectors used by live E2E helpers without running a live turn.
 *
 * Every navigational test id is declared in `LIVE_TESTIDS`. The stubbed API
 * makes this contract fast and independent of Docker, a sandbox, and a model
 * key. It proves that selectors resolve, not that a turn succeeds.
 *
 * `run-composer-stop` requires the button's live streaming state and remains
 * covered by the live gate.
 */

test.describe("live-gate selector contract", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the agent picker renders what the live helpers click", async ({ page }) => {
    await stubApi(page);
    await page.goto("/agents", { waitUntil: "domcontentloaded" });
    await settle(page);

    // openSessions() waits on the sidebar; createRunViaComposer() picks a card.
    await expect(page.getByTestId(LIVE_TESTIDS.sessionsPage)).toBeVisible();
    const cards = page.getByTestId(LIVE_TESTIDS.agentOption);
    expect(await cards.count()).toBeGreaterThan(0);

    await expect(page.getByText("Production services")).toHaveCount(0);

    // The helper clicks the card's only button rather than matching its label,
    // which is translated. If the card grows a second button — a menu, a link —
    // that click becomes ambiguous and the live gate fails on a strict-mode
    // violation, so the count is part of the contract.
    expect(await cards.first().getByRole("button").count()).toBe(1);
  });

  test("an Assistant conversation request exposes no Vault selection", async ({ page }) => {
    await stubApi(page);
    await page.goto("/assistants", { waitUntil: "domcontentloaded" });
    await settle(page);

    const readyAssistant = page.locator(
      `[data-testid="${LIVE_TESTIDS.assistantOption}"][data-assistant-state="READY"]`,
    ).first();
    await expect(readyAssistant).toBeVisible();

    const requestPromise = page.waitForRequest((request) =>
      request.method() === "POST"
      && /\/api\/v1\/assistants\/[^/]+\/conversations(?:\?|$)/.test(request.url()),
    );
    await readyAssistant.getByRole("button").click();
    const request = await requestPromise;
    expect(request.postData()).toBeNull();
    await expect(page).toHaveURL(/\/sessions\/s-001(?:[/?#]|$)/);
  });

  test("Credential Vaults are available only in the management console", async ({ page }) => {
    await stubApi(page);
    await page.goto("/manage/credentials", { waitUntil: "domcontentloaded" });
    await settle(page);

    await expect(page.getByTestId(LIVE_TESTIDS.credentialVaultPage)).toBeVisible();
    await expect(page.getByTestId(LIVE_TESTIDS.credentialVaultCreate)).toBeVisible();
    expect(await page.getByTestId(LIVE_TESTIDS.credentialVaultRow).count()).toBeGreaterThan(0);
    await page.getByTestId(LIVE_TESTIDS.credentialVaultRow).first().click();
    // Each channel is named where the page says how that channel's secret is
    // protected. The rail carries the three facts themselves; the sentence
    // below is the answer they share, and it says nothing until the reader
    // knows which kinds of credential it is the answer for.
    await expect(page.getByText("Model credentials", { exact: true })).toBeVisible();
    await expect(page.getByText("MCP credentials", { exact: true })).toBeVisible();
    await expect(page.getByText("Other service credentials", { exact: true })).toBeVisible();
    const protectedValues = page.getByText(
      "The agent receives a placeholder; the saved credential stays outside the sandbox",
      { exact: true },
    );
    await expect(protectedValues).toHaveCount(2);
    await expect(protectedValues.first()).toBeVisible();
    await expect(page.getByText("egress placeholder", { exact: true })).toHaveCount(0);

    // Deleting a Vault is two steps in the page, and the live gate walks both:
    // it arms the record header's Delete by label and confirms by this id.
    // Only the button inside the question carries the id, so asking for it
    // unarmed asks the page for a control it has not rendered — and would
    // report the live helper as broken while it works.
    //
    // First in DOM order is the record header's: the credentials list below
    // carries a Delete with the same label. The id on the line after is what
    // makes taking the first safe, exactly as it is in the live gate.
    await page.getByRole("button", { name: "Delete", exact: true }).first().click();
    await expect(page.getByTestId(LIVE_TESTIDS.credentialVaultDelete)).toBeVisible();

    // Escape leaves the question unanswered — the vault is still there for the
    // language switch below to read.
    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "ZH", exact: true }).click();
    await page.goto("/manage/credentials", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.getByTestId(LIVE_TESTIDS.credentialVaultRow).first().click();
    await expect(page.getByText("模型服务凭证", { exact: true })).toBeVisible();
    await expect(page.getByText("MCP 服务凭证", { exact: true })).toBeVisible();
    await expect(page.getByText("其他服务凭证", { exact: true })).toBeVisible();
    const protectedValuesZh = page.getByText(
      "Agent 收到占位符，保存的凭证留在沙箱外",
      { exact: true },
    );
    await expect(protectedValuesZh).toHaveCount(2);
    await expect(protectedValuesZh.first()).toBeVisible();
  });

  test("the pool note explains reuse without implying tenant isolation in both languages", async ({
    page,
  }) => {
    const english = "A pool reuses sandboxes across sessions in this environment to reduce start-up time. It does not isolate users or tenants.";
    const chinese = "池中的沙箱可供本环境下的不同会话复用，用于缩短启动时间，但不能隔离不同用户或租户。";

    await stubApi(page);
    await page.goto("/manage/environments/environment-1", {
      waitUntil: "domcontentloaded",
    });
    await settle(page);
    // Once, not somewhere: the sentence belongs with the pool's counters in
    // the body, and a second copy — in the rail's pool fact, say — is the same
    // statement twice in one viewport (§1). The count is the assertion.
    // `.first()` passes over a duplicate, and a bare `toBeVisible()` reports
    // one only as a strict-mode violation with no name on what went wrong.
    await expect(page.getByText(english, { exact: true })).toHaveCount(1);
    await expect(page.getByText(english, { exact: true })).toBeVisible();

    await page.keyboard.press("Escape");
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await page.getByRole("button", { name: "ZH", exact: true }).click();
    await page.goto("/manage/environments/environment-1", {
      waitUntil: "domcontentloaded",
    });
    await settle(page);
    await expect(page.getByText(chinese, { exact: true })).toHaveCount(1);
    await expect(page.getByText(chinese, { exact: true })).toBeVisible();
  });

  test("the session view renders what the live helpers assert on", async ({ page }) => {
    await stubApi(page);
    // s-004 is READY in the fixture, so the composer renders its submit action.
    // A BUSY fixture correctly renders the stop action instead, so it cannot
    // exercise the live helper's submit selector.
    await page.goto("/sessions/s-004", { waitUntil: "domcontentloaded" });
    await settle(page);

    for (const key of [
      "runView",
      "composerPrompt",
      "composerSubmit",
      "userMessage",
      "assistantMessage",
      "assistantText",
    ] as const) {
      const id = LIVE_TESTIDS[key];
      expect(await page.locator(`[data-testid="${id}"]`).count(), `${key} (${id})`).toBeGreaterThan(0);
    }

    // The live gate reads the pill from inside the run view, not from the
    // sidebar, so assert it where the helper looks for it.
    expect(
      await page.getByTestId(LIVE_TESTIDS.runView).getByTestId(LIVE_TESTIDS.statusPill).count(),
    ).toBeGreaterThan(0);
  });
});
