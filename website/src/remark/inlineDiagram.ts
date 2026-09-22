/**
 * Inlines a figure written as `![alt](./img/name.svg#inline)` into the page.
 *
 * The documents under `docs/` are CommonMark read in two places: on this site
 * and on GitHub. An `<img>` satisfies GitHub but isolates the SVG from the
 * page — it cannot reach `--fg-1`, `--astra` or the loaded Geist face, so a
 * figure would carry its own frozen palette and a substituted font. Inlining
 * the same file at build time gives the site a figure that follows the theme
 * toggle and the page's typography, while the markdown source stays one line
 * and GitHub still renders the file as a plain image.
 *
 * The `#inline` fragment is what opts a figure in. A file server ignores the
 * fragment, so the GitHub rendering is unaffected by it.
 */

import fs from 'node:fs';
import path from 'node:path';

const MARKER = '#inline';

// The standalone block carries the dark palette for the `<img>` rendering.
// Inlined, the page's own `data-theme` already picks the values, and leaving
// the block in would let the reader's OS preference override an explicit
// choice made with the site's toggle.
const STANDALONE_STYLE = /<style\b[^>]*\bdata-standalone\b[^>]*>[\s\S]*?<\/style>/gi;
const XML_PROLOG = /^\s*<\?xml[^>]*\?>\s*/;
const COMMENT = /<!--[\s\S]*?-->/g;

type Node = {
  type: string;
  url?: string;
  alt?: string;
  value?: string;
  children?: Node[];
};

type VFile = {path?: string; history?: string[]};

function sourcePath(file: VFile): string | undefined {
  return file.path ?? file.history?.[file.history.length - 1];
}

function inlineOne(node: Node, file: VFile): Node | null {
  const url = node.url;
  if (!url?.endsWith(`.svg${MARKER}`)) {
    return null;
  }

  const from = sourcePath(file);
  if (!from) {
    throw new Error(
      `inlineDiagram: cannot resolve ${url} — the markdown file has no path.`,
    );
  }

  const target = path.resolve(path.dirname(from), url.slice(0, -MARKER.length));
  // A missing figure must stop the build. Rendered as a broken image it would
  // reach the published site, and the pages this runs on are the manual.
  const raw = fs.readFileSync(target, 'utf8');

  const open = raw.indexOf('<svg');
  const close = raw.lastIndexOf('</svg>');
  if (open < 0 || close < 0) {
    throw new Error(`inlineDiagram: ${target} has no <svg> element.`);
  }

  const svg = raw
    .slice(open, close + '</svg>'.length)
    .replace(XML_PROLOG, '')
    .replace(STANDALONE_STYLE, '')
    .replace(COMMENT, '');

  return {
    type: 'html',
    value: `<figure class="astraDiagram">${svg}</figure>`,
  };
}

function walk(node: Node, file: VFile): void {
  const children = node.children;
  if (!children) {
    return;
  }
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index];
    if (child.type === 'image') {
      const inlined = inlineOne(child, file);
      if (inlined) {
        children[index] = inlined;
      }
      continue;
    }
    walk(child, file);
  }
}

/**
 * List SVG paths directly within the supplied directories; skip missing roots.
 *
 * The inliner reads figures from disk rather than importing them. The
 * figuresAreBuildInputs plugin in website/docusaurus.config.ts registers these
 * paths as cache dependencies so a figure edit invalidates the compiled page.
 */
export function figurePaths(roots: string[]): string[] {
  const found: string[] = [];
  for (const root of roots) {
    if (!fs.existsSync(root)) {
      continue;
    }
    for (const entry of fs.readdirSync(root)) {
      if (entry.endsWith('.svg')) {
        found.push(path.join(root, entry));
      }
    }
  }
  return found.sort();
}

export default function inlineDiagram() {
  return (tree: Node, file: VFile): void => {
    walk(tree, file);
    // A figure is normally the only content of its paragraph. Left inside one,
    // the block-level <figure> would be nested in a <p>, which the HTML parser
    // closes early and turns into an empty paragraph before the figure.
    const children = tree.children;
    if (!children) {
      return;
    }
    for (let index = 0; index < children.length; index += 1) {
      const child = children[index];
      if (
        child.type === 'paragraph' &&
        child.children?.length === 1 &&
        child.children[0].type === 'html' &&
        child.children[0].value?.startsWith('<figure class="astraDiagram">')
      ) {
        children[index] = child.children[0];
      }
    }
  };
}
