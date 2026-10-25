/** Confirm a native credential reload without exporting the secret. */
import { createHash } from 'node:crypto';
import { writeFile, rename } from 'node:fs/promises';

export const inject = ['credentials'];

export async function apply(ctx, { reference, receipt }) {
  let pending = Promise.resolve();
  const acknowledge = () => {
    pending = pending.then(async () => {
      const credential = await ctx.credentials.resolve(reference);
      if (!credential) throw new Error('Desktop model credential is missing');
      const value = {
        pid: process.pid,
        sha256: createHash('sha256').update(credential.value).digest('hex'),
      };
      await writeFile(`${receipt}.tmp`, JSON.stringify(value), { mode: 0o600 });
      await rename(`${receipt}.tmp`, receipt);
    });
    return pending;
  };
  ctx.on('credentials/reference-updated', (changed) => {
    if (changed === reference) return acknowledge();
  });
  await acknowledge();
}
