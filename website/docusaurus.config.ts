import {themes as prismThemes} from 'prism-react-renderer';
import type {Config, Plugin} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

import path from 'node:path';

import inlineDiagram, {figurePaths} from './src/remark/inlineDiagram';

const FIGURE_DIRECTORIES = [
  path.resolve(__dirname, '../docs/img'),
  path.resolve(__dirname, '../docs/providers/img'),
  path.resolve(
    __dirname,
    'i18n/zh-Hans/docusaurus-plugin-content-docs/current/img',
  ),
  path.resolve(
    __dirname,
    'i18n/zh-Hans/docusaurus-plugin-content-docs/current/providers/img',
  ),
];

// Figures are read from disk by the `inlineDiagram` remark plugin, so the
// bundler cannot discover them as inputs on its own: editing one would rebuild
// successfully and publish the previous drawing. Registering them as build
// dependencies is what makes an edit take effect. Both cache shapes are
// declared because the bundler in use decides which one it reads; each is a
// fragment merged into a cache configuration the bundler has already typed.
function figuresAreBuildInputs(): Plugin {
  return {
    name: 'astrabox-figures-are-build-inputs',
    configureWebpack: () => {
      const figures = figurePaths(FIGURE_DIRECTORIES);
      return {
        cache: {buildDependencies: {astraboxFigures: figures}},
        experiments: {cache: {buildDependencies: figures}},
      } as unknown as ReturnType<NonNullable<Plugin['configureWebpack']>>;
    },
    getPathsToWatch: () => FIGURE_DIRECTORIES,
  };
}

// The site builds directly from the repository's `docs/` tree. Maintainer
// procedures and dated design records are excluded below because they are not
// product documentation.
//
// URL is the public site; organization and project name identify the GitHub repository.

const config: Config = {
  title: 'AstraBox',
  tagline:
    'The open-source, self-hosted alternative to Claude Managed Agents: conversations start and resume in seconds, with any model.',
  favicon: 'img/favicon.svg',

  future: {
    v4: true,
  },

  url: 'https://www.astrabox.ai',
  baseUrl: '/',
  organizationName: 'Colton-z',
  projectName: 'AstraBox',
  trailingSlash: false,

  // Repo docs cross-link files this site deliberately excludes (maintainer
  // procedures, dated records). Warn, don't fail the build over them.
  onBrokenLinks: 'warn',

  markdown: {
    // Repo docs are plain CommonMark `.md`; only parse `.mdx` as MDX (angle
    // brackets and braces in prose would otherwise break the build).
    format: 'detect',
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },

  plugins: [figuresAreBuildInputs],

  themes: [
    '@docusaurus/theme-mermaid',
    [
      // Offline search: the index is built at build time and shipped with the
      // site, so search works on GitHub Pages with no Algolia account and no
      // third-party request at read time. Both locales are indexed.
      '@easyops-cn/docusaurus-search-local',
      {
        hashed: true,
        indexBlog: false,
        language: ['en', 'zh'],
        docsRouteBasePath: '/docs',
        // The docs tree is the repository's, not a copy under this directory —
        // without this the plugin looks for `website/docs`, finds nothing, and
        // ships an index with no doc content in it.
        docsDir: ['../docs', 'i18n/zh-Hans/docusaurus-plugin-content-docs/current'],
        highlightSearchTermsOnTargetPage: true,
      },
    ],
  ],

  i18n: {
    defaultLocale: 'en',
    locales: ['en', 'zh-Hans'],
    localeConfigs: {
      en: {label: 'English'},
      'zh-Hans': {label: '简体中文'},
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          path: '../docs',
          sidebarPath: './sidebars.ts',
          // Runs ahead of the default image handling, which would otherwise
          // resolve a figure marked `#inline` into an `<img>` asset first.
          beforeDefaultRemarkPlugins: [inlineDiagram],
          editUrl: 'https://github.com/Colton-z/AstraBox/edit/main/docs/',
          exclude: [
            'maintainers/**',
            'design-*.md',
            'architecture-recomb-*.md',
            'RUN_E2E.md',
            'channel-spine.md',
            'comment-style.md',
            'configuration.md',
            'development.md',
            'domain-model.md',
            'frontend-design.md',
            'migrations.md',
          ],
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // Link previews (Slack, X, WeChat) read og:image; without one the card is
    // blank wherever the site gets shared.
    image: 'img/og-card.png',
    colorMode: {
      respectPrefersColorScheme: true,
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
      options: {
        fontFamily: "'Geist', ui-sans-serif, system-ui, sans-serif",
        // `themeVariables` set at this level apply to both colour modes, so a
        // node fill pinned here also applies to dark-mode diagrams and renders
        // them as white boxes on a dark background. The per-mode themes carry the
        // palette.
      },
    },
    navbar: {
      title: 'AstraBox',
      logo: {
        // One asset for both modes — the mark is a solid tile, so it needs no
        // dark variant.
        alt: 'AstraBox',
        src: 'img/astrabox-mark.svg',
        width: 26,
        height: 26,
      },
      items: [
        {
          to: '/docs/capabilities',
          label: 'Capabilities',
          position: 'left',
        },
        {
          to: '/docs/quickstart',
          label: 'Quickstart',
          position: 'left',
        },
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/docs/deploy',
          label: 'Deployment',
          position: 'left',
        },
        {
          type: 'localeDropdown',
          position: 'right',
        },
        {
          // A mark, not the word: the right-hand cluster is chrome, and a text
          // link there carries the same reading weight as the nav labels on
          // the left, which are the actual destinations.
          type: 'html',
          position: 'right',
          value:
            '<a href="https://github.com/Colton-z/AstraBox" target="_blank" rel="noopener noreferrer" class="navbarIconLink" aria-label="AstraBox on GitHub">' +
            '<svg viewBox="0 0 16 16" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.4 7.4 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>' +
            '</a>',
        },
      ],
    },
    footer: {
      style: 'light',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Capabilities', to: '/docs/capabilities'},
            {label: 'Quickstart', to: '/docs/quickstart'},
            {label: 'Models', to: '/docs/models'},
            {label: 'Deploy', to: '/docs/deploy'},
            {label: 'Environments', to: '/docs/environments'},
          ],
        },
        {
          title: 'Project',
          items: [
            {label: 'GitHub', href: 'https://github.com/Colton-z/AstraBox'},
            {label: 'Architecture', to: '/docs/architecture'},
            {label: 'API reference', to: '/docs/api'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Colton Qi. Apache-2.0.`,
    },
    prism: {
      theme: {
        ...prismThemes.github,
        styles: [
          ...prismThemes.github.styles,
          // Small shell commands, variables, and quoted strings need AA contrast on the
          // light code surface; the theme's string colour reaches only 4.2:1 on it.
          {types: ['function'], style: {color: '#b62635'}},
          {types: ['variable'], style: {color: '#167472'}},
          {types: ['string'], style: {color: '#b8105a'}},
        ],
      },
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'yaml', 'docker'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
