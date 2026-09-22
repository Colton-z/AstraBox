import { execFileSync } from "node:child_process";

/** Remove containers owned by this Playwright stack.
 *
 * Playwright may stop a webServer process before its shell EXIT trap runs. The
 * sandbox and Vault are deleted by each test through the public API; this
 * fallback handles only the two deterministic, test-only gateway containers.
 */
export default function globalTeardown(): void {
  if (process.env.E2E_SKIP_WEBSERVER === "1") return;

  const backendPort = process.env.E2E_BACKEND_PORT ?? "8123";
  try {
    execFileSync(
      "docker",
      [
        "rm",
        "-f",
        `astrabox-e2e-litellm-${backendPort}`,
        `astrabox-e2e-gateway-dns-${backendPort}`,
      ],
      { stdio: "ignore" },
    );
  } catch {
    // A missing Docker daemon or already-removed container needs no cleanup.
  }
}
