/** Compare the real supplier child tool with its public transcript and drawer. */
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { expect, type Locator, type Page } from '@playwright/test';

import type { ChildRunMessagePage } from './astraApi';
import type { ChildGate, NativeChildTool } from './nativeChildLifecycle';

export function expectToolBlocks(transcript: ChildRunMessagePage, native: NativeChildTool, receipt?: string): void {
  const blocks = transcript.messages.flatMap((message) => message.content);
  const calls = blocks.filter((block) => block.type === 'tool_use' && block.id === native.id);
  expect(calls, 'retain the actual child tool identity exactly once').toHaveLength(1);
  expect(calls[0]!.name).toBe(native.name);
  if (native.name === 'commandExecution') {
    const input = calls[0]!.input as Record<string, unknown>;
    // exec_command.workdir is optional in Codex 0.153.4. Its absence retains
    // the child's native cwd; it does not remove cwd from the executed item.
    const cwd = native.commandArguments!.workdir ?? native.nativeSessionCwd;
    expect(cwd, 'the independent child SessionStore must identify the command cwd').toMatch(/^\//);
    expect(input).toMatchObject({
      type: 'commandExecution', id: native.id, cwd,
    });
    // Codex presents the shell argv as a quoted string. Use the standard POSIX
    // lexer to compare its complete command argument with the actual rollout
    // call, without reimplementing the vendor's presentation quoting rules.
    const argv = JSON.parse(execFileSync('python3', [
      '-c', 'import json, shlex, sys; print(json.dumps(shlex.split(sys.stdin.read())))',
    ], { input: String(input.command), encoding: 'utf8', timeout: 5000 })) as string[];
    expect(argv.at(-1), 'the complete command must equal the independently stored child call').toBe(native.commandArguments!.cmd);
    // Codex's item status/output evolve; the command identity and input do not.
    if (native.input.type === 'commandExecution') {
      expect(input).toMatchObject({ command: native.input.command, cwd: native.input.cwd });
    }
    if (native.completedCommand) {
      expect(argv, 'the completed native item retains the full executed argv').toEqual(native.completedCommand.command);
      expect(input.cwd).toBe(fileURLToPath(String(native.completedCommand.cwd)));
    }
  } else {
    expect(calls[0]!.input, 'show the supplier arguments, not a summary of the command').toEqual(native.input);
  }
  const results = blocks.filter((block) => block.type === 'tool_result' && block.tool_use_id === native.id);
  if (receipt === undefined) {
    expect(native.result, 'the native child tool has not finished while held').toBeUndefined();
    expect(results, 'a held tool must not acquire a synthetic result').toHaveLength(0);
  } else {
    expect(native.result?.isError, 'the real child command must have succeeded').toBe(false);
    expect(JSON.stringify(native.result!.output), 'the native result independently proves the unknown receipt').toContain(receipt);
    expect(results, 'the result must attach to the same native tool call').toHaveLength(1);
    expect(results[0]!.is_error).not.toBe(true);
    expect(JSON.stringify(results[0]!.content)).toContain(receipt);
    if (native.name === 'commandExecution') {
      const output = JSON.parse(String(results[0]!.content)) as Record<string, unknown>;
      expect(output).toMatchObject({
        id: native.id, type: 'commandExecution', status: native.completedCommand!.status,
        exitCode: native.completedCommand!.exit_code,
        aggregatedOutput: native.completedCommand!.aggregated_output,
      });
    }
  }
}

export async function expectChildToolCard(
  page: Page, native: NativeChildTool, gate: ChildGate, receipt?: string,
): Promise<void> {
  const column = page.getByTestId('subagent-transcript-column');
  const escaped = native.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const cards = column.locator('[data-slot="collapsible"]').filter({
    has: page.getByRole('button', { name: new RegExp(`^${escaped} (Working|处理中|Done|已完成|Failed|执行失败)$`) }),
  });
  await expect(cards, 'the child drawer must render native tools, not only conversation text').not.toHaveCount(0);
  const matches: Locator[] = [];
  for (let index = 0; index < await cards.count(); index += 1) {
    const card = cards.nth(index);
    const header = card.locator('[data-slot="collapsible-trigger"]');
    if (await header.getAttribute('aria-expanded') !== 'true') await header.click();
    const content = card.locator('[data-slot="collapsible-content"]');
    await expect(content).toBeVisible();
    // CodeBlock uses content-visibility:auto. Expanding its header does not
    // bring the parameters into view, so innerText can contain only the title.
    const input = content.getByRole('heading', { name: 'Parameters', exact: true })
      .locator('..').locator('[data-language="json"]');
    await input.scrollIntoViewIfNeeded();
    await expect(input).toBeInViewport();
    await expect(input).not.toHaveText('', { useInnerText: true });
    if ((await input.innerText()).includes(gate.started)) matches.push(card);
    else await header.click();
  }
  expect(matches, 'exactly one child tool card must contain this real held command').toHaveLength(1);
  const card = matches[0]!;
  await expect(card.locator('[data-slot="collapsible-trigger"] [data-slot="badge"]'))
    .toHaveText(receipt === undefined ? /^(Working|处理中)$/ : /^(Done|已完成)$/);
  const content = card.locator('[data-slot="collapsible-content"]');
  const parameters = content.getByRole('heading', { name: 'Parameters', exact: true }).locator('..');
  await expect(parameters).toContainText(gate.started, { useInnerText: true });
  await expect(parameters).toContainText(gate.release, { useInnerText: true });
  await expect(parameters).toContainText(gate.completed, { useInnerText: true });
  const resultHeading = content.getByRole('heading', { name: 'Result', exact: true });
  if (receipt === undefined) {
    await expect(resultHeading, 'a held tool has no completed Result section').toHaveCount(0);
  } else {
    await resultHeading.locator('..').locator('[data-language="json"]').scrollIntoViewIfNeeded();
    await expect(resultHeading.locator('..'), 'show the receipt in Result, not an input snapshot or final answer')
      .toContainText(receipt, { useInnerText: true });
  }
}
