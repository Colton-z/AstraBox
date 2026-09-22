import fs from 'node:fs';
import path from 'node:path';

type WorkerHangReportOptions = Readonly<{
  delayMs: number;
  reportDir: string;
}>;

function writeDiagnostic(message: string): void {
  try {
    process.stderr.write(message);
  } catch {
    // A broken diagnostic stream must not replace the test result.
  }
}

/**
 * Register a delayed diagnostic report for a Playwright worker that is stopping.
 *
 * Playwright workers have an IPC channel; the runner does not, so loading the
 * shared config in the runner leaves it untouched. A healthy worker exits before
 * this timer matters. A worker stuck in teardown remains alive long enough for
 * its JavaScript stack and active handles to be written before force-kill.
 */
export function armWorkerHangReport({ delayMs, reportDir }: WorkerHangReportOptions): void {
  if (typeof process.send !== 'function') return;

  let armed = false;
  const arm = () => {
    if (armed) return;
    armed = true;

    const timer = setTimeout(() => {
      const file = path.join(reportDir, `astrabox-worker-hang-${process.pid}.json`);
      try {
        fs.mkdirSync(reportDir, { recursive: true });
        process.report.writeReport(file);
        writeDiagnostic(
          `[hangReport] worker ${process.pid} still stopping; wrote ${file}\n`,
        );
      } catch (error) {
        writeDiagnostic(
          `[hangReport] worker ${process.pid} still stopping; failed to write ${file}: ${String(error)}\n`,
        );
      }
    }, delayMs);
    timer.unref();
  };

  process.on('message', (message: unknown) => {
    if ((message as { method?: string } | null)?.method === '__stop__') arm();
  });
  process.on('disconnect', arm);
}
