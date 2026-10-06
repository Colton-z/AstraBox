import mermaid from 'mermaid';
import type { PageWindow } from './bridge';
const page = window as PageWindow & { __artifactSource: string };
void (async () => {
  await page.__artifactReady;
  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
  const { svg, bindFunctions } = await mermaid.render(
    'artifact-diagram',
    page.__artifactSource,
  );
  const root = document.getElementById('root')!;
  root.innerHTML = svg;
  bindFunctions?.(root);
})().catch((error) => {
  page.__artifactReportError?.(error);
  const panel = document.createElement('pre');
  panel.setAttribute('role', 'alert');
  panel.textContent = String(error);
  document.getElementById('root')!.replaceChildren(panel);
});
