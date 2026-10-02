# Transcript scroll anchoring

The console uses the ESM entry point of `react-virtuoso` 4.18.13, pinned to
upstream commit `ebd6b0f0ff41e1fe003a50fe47b168b3ed698868`. Its MIT license is
retained in `../public/licenses/react-virtuoso.txt` and included in the built SPA.

The patch changes the existing upward-scroll compensation to compare the same
visible item's measured offset before and after a size update. A change below
that item must not move the reader. Older rows rendered above it must still be
compensated when the overscan range includes the first loaded row. Count changes,
scroll direction, prepend timing and the supplier's Safari handling keep their
existing paths.

`npm ci` applies the patch through `patch-package --error-on-fail`. The server
image copies patches before installing dependencies, so the install layer is
invalidated when a patch changes. Keep the exact dependency pin: an upgrade must
either carry the correction or remove the patch after verifying the replacement.
Do not skip install scripts when building the console.

After changing the patch, run the long-history and history-gap browser E2Es on
the AWS testbed. They check variable-height prepends on every frame, retained
history, refresh, retries, bottom following and anonymous shared history.
