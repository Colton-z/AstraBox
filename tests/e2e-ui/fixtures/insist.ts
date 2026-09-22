/**
 * Ask again when the model declines, instead of skipping the round.
 *
 * A third of the exclusive lane probes a state the model has to choose to reach
 * — a gated Write, a background Agent, a blocking Bash — and skipped when it
 * did not. The lane reports COMPLETE only on "zero skip or retry", so each of
 * those skips ends the round exactly as a failure does, and the set of specs
 * that pass is different every time.
 *
 * Re-sending the stimulus is SETUP, not a retry of the test: no assertion is
 * relaxed, nothing is retried after a failure, and the spec still proves the
 * platform's behaviour exactly once, on the first attempt that reaches the
 * state. What changes is that a model declining to call a tool costs a second
 * ask rather than the whole round's coverage.
 *
 * The budget is the caller's. Every attempt spends a real turn, and the lane's
 * per-test wall is fixed, so `insist` never asks again once the remaining time
 * cannot hold another probe.
 */

export interface InsistOptions<T> {
  /**
   * Send the stimulus, given the attempt number (1-based).
   *
   * Attempt 1 is usually a no-op: the spec has already sent its prompt in its
   * own words, above, and hoisting that block into a closure to re-run it is a
   * bigger edit than this is worth. Later attempts send a short follow-up in
   * the SAME conversation, which is what a user does when a model answers
   * without doing the thing.
   */
  ask: (attempt: number) => Promise<void>;
  /** The state the spec needs. Null means the model did not get there. */
  probe: () => Promise<T | null>;
  /** What never happened, for the failure message. */
  what: string;
  /** Total wall this may spend, including every attempt. */
  budgetMs: number;
  /** How long one attempt's probe may wait. */
  probeMs: number;
  /** Most asks to make. Default 2: one, and one more. */
  attempts?: number;
}

export async function insist<T>(options: InsistOptions<T>): Promise<T> {
  const attempts = Math.max(1, options.attempts ?? 2);
  const deadline = Date.now() + options.budgetMs;
  const declined: string[] = [];

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    // Out of budget, not "cannot fit another whole probe". Requiring a full
    // `probeMs` to remain made "one, and one more" unreachable in exactly the
    // case it exists for: a probe that runs to its own deadline leaves less
    // than one behind it, so callers passing the natural `budgetMs: probeMs *
    // 2` asked once and reported the model had declined. Two specs failed that
    // way — the gated Write and the held live turn — and both read as a model
    // that would not cooperate rather than as an ask that never happened.
    //
    // The caller's budget is still the wall: a second probe may run past it
    // only by whatever the first one left, and `budgetMs` is what bounds that.
    if (Date.now() >= deadline && attempt > 1) {
      break;
    }
    await options.ask(attempt);
    let reached: T | null = null;
    try {
      reached = await options.probe();
    } catch (error) {
      // A probe that throws is a probe that did not reach the state. The
      // reason is kept for the failure message: "timed out" and "the endpoint
      // refused" are different stories and the reader needs whichever it was.
      declined.push(`attempt ${attempt}: ${(error as Error).message}`);
      reached = null;
    }
    if (reached !== null && reached !== undefined) {
      return reached;
    }
    if (declined.length < attempt) {
      declined.push(`attempt ${attempt}: the state was never reached`);
    }
  }

  throw new Error(
    `${options.what} — asked ${declined.length} time(s) and the model never got ` +
      `there. This is not a skip: the spec's subject is what the platform does ` +
      `once the state exists, and it cannot be observed without it. ` +
      declined.join('; '),
  );
}
