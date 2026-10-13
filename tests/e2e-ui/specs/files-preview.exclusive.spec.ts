import { test, expect } from "@playwright/test";
import { AstraApi } from "../fixtures/astraApi";
import { appPath, apiPath } from "../fixtures/env";
import { onPassOnly, trackSessions } from "../fixtures/sessionCleanup";
import { requireSandboxHandle, sandboxExec } from "../fixtures/sandboxOps";

const sessions = trackSessions();
const agents: string[] = [];
onPassOnly(async ({ request }) => {
  const api = new AstraApi(request);
  for (const id of agents.splice(0)) await api.deleteAgent(id);
});

for (const placement of ["shared", "conversation"]) {
  test(`Files reads contents and previews relative HTML resources (${placement})`, async ({
    page,
    request,
    playwright,
  }) => {
    const api = new AstraApi(request);
    const agent =
      placement === "shared"
        ? await api.defaultAgent()
        : await api.createColdTestAgent(`__e2e_file_preview_${Date.now()}`);
    if (placement === "conversation") agents.push(agent.agent_id);
    const first = await api.startConversation(agent.agent_id);
    const sibling = await api.startConversation(agent.agent_id);
    sessions.push(first.session_id, sibling.session_id);
    const [ready, other] = await Promise.all([
      api.waitForSessionReady(first.session_id),
      api.waitForSessionReady(sibling.session_id),
    ]);
    if (placement === "shared") expect(ready.sandbox_id).toBe(other.sandbox_id);
    const detail = await api.adminSessionDetail(first.session_id);
    const identity = detail.runtime_identity as Record<string, string>;
    const otherDetail = await api.adminSessionDetail(sibling.session_id);
    const otherIdentity = otherDetail.runtime_identity as Record<
      string,
      string
    >;
    const root = identity.file_root_source_dir;
    const visibleRoot = identity.file_root_dir;
    expect(root).toMatch(/^\//);
    const box = await requireSandboxHandle(api, String(ready.sandbox_id));
    const otherBox = await requireSandboxHandle(api, String(other.sandbox_id));
    const marker = `preview-${Date.now()}`;
    const folder = `--preview-${Date.now().toString(36)}`;
    const text = `const result = "${marker}";\n// <script>must remain text</script>\n`;
    const html = `<link rel="stylesheet" href="../theme.css"><h1>${marker}</h1><button id="action">Run</button><output></output><img src="../pixel.png" alt="fixture"><script type="module" src="../app.mjs"></script>`;
    const script = `document.querySelector('#action').onclick = () => document.querySelector('output').textContent = '${marker}-clicked';`;
    sandboxExec(
      otherBox,
      `python3 - <<'PY'\nfrom pathlib import Path\nPath(${JSON.stringify(otherIdentity.file_root_source_dir + "/private.txt")}).write_text('sibling-secret')\nPY`,
    );
    // The oracle is the native filesystem, independently of the Files adapter.
    sandboxExec(
      box,
      `python3 - <<'PY'\nfrom pathlib import Path\nimport base64\np=Path(${JSON.stringify(root + "/" + folder)})\np.mkdir()\n(p/'nested').mkdir()\n(p/'sample.ts').write_text(${JSON.stringify(text)})\n(p/'readme.md').write_text('# ${marker}\\n\\n**Readable** markdown.')\n(p/'nested'/'--index.html').write_text(${JSON.stringify(html)})\n(p/'theme.css').write_text('h1 { color: rgb(0, 128, 0); }')\n(p/'app.mjs').write_text(${JSON.stringify(script)})\n(p/'pixel.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII='))\n(p/'binary.bin').write_bytes(b'\\x00\\xff')\n(p/'outside').symlink_to('/etc')\n(p/'sibling').symlink_to(${JSON.stringify(otherIdentity.file_root_source_dir)})\nPY`,
    );
    await page.goto(appPath(`/sessions/${first.session_id}`));
    await page.getByRole("tab", { name: "Files", exact: true }).click();
    await page.getByText(folder, { exact: true }).click();
    const folderRow = page
      .getByText(folder, { exact: true })
      .locator('xpath=ancestor::*[@role="treeitem"][1]');
    await folderRow.getByRole("button").first().click();
    const reader = page.getByRole("region", { name: "File contents" });
    await page.getByText("sample.ts", { exact: true }).click();
    await expect(reader).toContainText(marker);
    await expect(reader).toContainText("<script>must remain text</script>");
    await reader.getByRole("button", { name: "Back to files" }).click();
    await page.getByText("readme.md", { exact: true }).click();
    await expect(reader.getByRole("heading", { name: marker })).toBeVisible();
    await reader.getByRole("tab", { name: "Source", exact: true }).click();
    await expect(reader).toContainText(`**Readable**`);
    await reader.getByRole("button", { name: "Back to files" }).click();
    await page.getByText("pixel.png", { exact: true }).click();
    await expect(reader.getByRole("img")).toBeVisible();
    await expect
      .poll(() =>
        reader
          .getByRole("img")
          .evaluate((img: HTMLImageElement) => img.naturalWidth),
      )
      .toBe(1);
    await reader.getByRole("button", { name: "Back to files" }).click();
    await page.getByText("binary.bin", { exact: true }).click();
    await expect(reader.getByRole("alert")).toContainText("not UTF-8");
    const downloadPromise = page.waitForEvent("download");
    await reader.getByRole("button", { name: "Download", exact: true }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("binary.bin");
    const stream = await download.createReadStream();
    expect(stream).not.toBeNull();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    expect([...Buffer.concat(chunks)]).toEqual([0, 255]);
    await reader.getByRole("button", { name: "Back to files" }).click();
    const nestedRow = page
      .getByText("nested", { exact: true })
      .locator('xpath=ancestor::*[@role="treeitem"][1]');
    await nestedRow.getByRole("button").first().click();
    await page.getByText("--index.html", { exact: true }).click();
    const frame = reader.frameLocator("iframe");
    await expect(frame.getByRole("heading", { name: marker })).toHaveCSS(
      "color",
      "rgb(0, 128, 0)",
    );
    await frame.getByRole("button", { name: "Run", exact: true }).click();
    await expect(frame.locator("output")).toHaveText(`${marker}-clicked`);
    await expect
      .poll(() =>
        frame
          .getByRole("img")
          .evaluate((img: HTMLImageElement) => img.naturalWidth),
      )
      .toBe(1);
    await expect(reader.locator("iframe")).toHaveAttribute(
      "sandbox",
      "allow-scripts",
    );
    const url = (await reader.locator("iframe").getAttribute("src"))!;
    const anonymous = await playwright.request.newContext({
      storageState: { cookies: [], origins: [] },
      extraHTTPHeaders: {},
    });
    try {
      expect((await anonymous.storageState()).cookies).toHaveLength(0);
      // An API login is required to mint a grant, while its bearer can read
      // only this workspace, for a bounded lifetime, through OpenSandbox.
      const denied = await anonymous.post(
        `${new URL(page.url()).origin}${apiPath(`/sessions/${first.session_id}/files/preview`)}`,
        { data: { path: `${visibleRoot}/${folder}/nested/--index.html` } },
      );
      expect([401, 403]).toContain(denied.status());
      for (const relative of [
        "../outside/passwd",
        "../sibling/private.txt",
        "../../../../etc/passwd",
      ]) {
        const response = await anonymous.get(new URL(relative, url).href);
        expect([400, 403, 404]).toContain(response.status());
        expect(await response.text()).not.toContain("sibling-secret");
      }
      const tampered = url.replace(/\/p\/\d+\//, "/p/1/");
      expect((await anonymous.get(tampered)).status()).toBe(403);
    } finally {
      await anonymous.dispose();
    }
    sandboxExec(
      box,
      `python3 - <<'PY'\nfrom pathlib import Path\np=Path(${JSON.stringify(root + "/" + folder)})\n(p/'theme.css').write_text('h1 { color: rgb(128, 0, 0); }')\n(p/'app.mjs').write_text(${JSON.stringify(script.replace("-clicked", "-updated"))})\nPY`,
    );
    await reader.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(frame.getByRole("heading", { name: marker })).toHaveCSS(
      "color",
      "rgb(128, 0, 0)",
    );
    await frame.getByRole("button", { name: "Run", exact: true }).click();
    await expect(frame.locator("output")).toHaveText(`${marker}-updated`);
    await reader.getByRole("tab", { name: "Source", exact: true }).click();
    await expect(reader).toContainText('<script type="module"');
    test
      .info()
      .annotations.push({
        type: "preview_native_source",
        description: `${ready.sandbox_id}:${root}/${folder}`,
      });
  });
}
