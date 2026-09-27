interface FaultCleanup {
  status: string;
  original: Record<string, unknown> | undefined;
  capture(): unknown;
  attach(body: string): Promise<void>;
  restore(original: Record<string, unknown>): void;
}

/** Retain the fault evidence before restoring the address needed for disposal. */
export async function finishNoSandboxFault(cleanup: FaultCleanup): Promise<void> {
  if (!cleanup.original || !['passed', 'interrupted'].includes(cleanup.status)) return;
  // Await durable attachment: an attachment failure must leave the scene intact.
  await cleanup.attach(JSON.stringify({
    original_session: cleanup.original,
    before_repair: cleanup.capture(),
    repair: 'restore only the missing Session sandbox_id under its original placement identity',
  }, null, 2));
  cleanup.restore(cleanup.original);
}
