<!-- Thanks for contributing! A short, concrete PR beats a long one. -->

## What & why

<!-- What changes, and what problem it solves. Link the issue if one exists. -->

## How it was verified

<!-- Check what you ran. `make test` is the merge gate; the live lanes are opt-in. -->

- [ ] `make test` (ruff + mypy + unit pytest + frontend tsc/build/vitest) is green
- [ ] `make e2e` — only if you touched the live turn path (needs Docker + an LLM key)
- [ ] New behavior is covered by an E2E test (a unit test only for a database race or a security boundary), or this is docs-only

## Anything reviewers should know

<!-- Breaking changes, config/env additions (register them — scripts/check_env_registry.py
     enforces it), migration notes, deliberate trade-offs, follow-ups. -->
