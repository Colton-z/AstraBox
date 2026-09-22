# AstraBox documentation site

The site uses Docusaurus. English source documents live in `../docs`; Simplified
Chinese translations live under `i18n/zh-Hans`.

Every page is published in both languages: an English change needs the matching
Simplified Chinese change in the same commit, and `tests/website_docs_parity_test.py`
fails when one language moves without the other.

## Local development

From the repository root, use the Node.js release pinned in `.nvmrc`:

```bash
python3 scripts/node-toolchain.py npm --prefix website ci
python3 scripts/node-toolchain.py npm --prefix website run start
```

To open the Chinese site directly, run:

```bash
python3 scripts/node-toolchain.py npm --prefix website run start -- --locale zh-Hans
```

## Checks

Local static checks:

```bash
python3 scripts/check_comment_style.py
python3 scripts/node-toolchain.py npm --prefix website run typecheck
git diff --check
```

Run the tests and the production build from a checkout containing the candidate
documents:

```bash
.venv/bin/python -m pytest -q tests/check_i18n_test.py tests/website_content_facts_test.py
python3 scripts/node-toolchain.py npm --prefix website run build
python3 scripts/node-toolchain.py npm --prefix e2e ci
python3 scripts/node-toolchain.py npm --prefix e2e run install:browser -- --with-deps
python3 scripts/node-toolchain.py npm --prefix e2e run test:website
```

The browser setup uses Playwright's supported `--with-deps` installation;
downloading Chromium alone does not install its Linux shared libraries. This
step needs permission to install system packages. See
[Playwright browser dependencies](https://playwright.dev/docs/browsers#install-system-dependencies).

The production build generates both locales in `website/build/`. The website
browser configuration serves the site locally; it does not deploy AstraBox,
create sandboxes or call a model. It checks both languages,
navigation, local links, accessibility and responsive layout. Do not run the
product E2E lanes for a documentation-only change.
