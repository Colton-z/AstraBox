/**
 * Open the folded work inside an assistant turn, the way a reader opens it.
 *
 * A settled turn that ran tools shows one header in place of that work. Live,
 * the page already holds the blocks behind that header; after a reload the
 * header is a lazy block whose blocks are fetched when it is opened. Inside
 * either one, consecutive reasoning and settled tool cards sit in a group that
 * starts closed. A closed group keeps its cards out of the accessibility tree
 * and a lazy block keeps them out of the DOM entirely, so a spec that asserts
 * on a card has to open what holds it first.
 */
import { expect, type Locator, type Page } from '@playwright/test';

export interface RevealAssistantProcessOptions {
  /** Restrict the reveal to one subtree, such as a single assistant message. */
  within?: Locator;
}

/** How long one lazy block gets to deliver the blocks its header stands for. */
const DETAIL_TIMEOUT_MS = 30_000;

async function expandTurnProcess(container: Locator): Promise<void> {
  const trigger = container.getByTestId('assistant-turn-process-trigger');
  if ((await trigger.getAttribute('aria-expanded')) === 'true') return;
  const blockId = await container.getAttribute('data-process-block-id');
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  if (!blockId) {
    await expect(container.getByTestId('assistant-turn-process-panel')).toBeVisible();
    return;
  }
  // A lazy header's blocks arrive by a read the page makes once and keeps
  // (LazyProcessBlock caches the promise per session, block and cursor), so a
  // header opened, closed and opened again — or remounted by the virtualized
  // list — legitimately makes no request. What this helper waits for is the
  // content: the panel is visible from its loading line onwards, and what a
  // header folds is thinking and completed tool calls, which render as at
  // least one process group. That group is the evidence the blocks are on the
  // page. How many reads a header costs, and what a failed read shows, are
  // asserted by assistant-process.parallel.spec.ts, not here.
  await expect(
    container.getByTestId('process-block-details').getByTestId('assistant-process').first(),
    `process block ${blockId} must render the work it stands for`,
  ).toBeAttached({ timeout: DETAIL_TIMEOUT_MS });
}

async function expandProcessGroup(group: Locator): Promise<void> {
  if ((await group.getAttribute('data-open')) !== null) return;
  await group.getByTestId('assistant-process-trigger').click();
  await expect(group).toHaveAttribute('data-open', /.*/);
  await expect(group.getByTestId('assistant-process-panel')).toBeVisible();
}

/**
 * Expand every collapsed process container in `page`, or inside `within`.
 *
 * Turn-level headers go first: the groups a lazy block holds do not exist
 * until its detail read lands, so counting groups before that would miss them.
 */
export async function revealAssistantProcess(
  page: Page,
  options: RevealAssistantProcessOptions = {},
): Promise<void> {
  const scope = options.within ?? page.locator('body');
  const turns = scope.getByTestId('assistant-turn-process');
  for (let index = 0; index < await turns.count(); index += 1) {
    await expandTurnProcess(turns.nth(index));
  }
  const groups = scope.getByTestId('assistant-process');
  for (let index = 0; index < await groups.count(); index += 1) {
    await expandProcessGroup(groups.nth(index));
  }
}

/**
 * Open the group holding the settled cards of a turn that is still running.
 *
 * A live turn has no turn-level header yet — that one appears when the turn
 * settles — so a spec reading a card mid-turn opens the group alone.
 */
export async function openLiveProcessGroup(
  page: Page,
  options: RevealAssistantProcessOptions = {},
): Promise<void> {
  const scope = options.within ?? page.locator('body');
  const groups = scope.getByTestId('assistant-process');
  await expect(groups.first()).toBeAttached();
  for (let index = 0; index < await groups.count(); index += 1) {
    await expandProcessGroup(groups.nth(index));
  }
}
