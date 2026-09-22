<!-- Thanks for contributing! A short, concrete PR beats a long one. -->

## What & why

<!-- What changes, and what problem it solves. Link the issue if one exists. -->

## How it was verified

<!-- Check what you ran. `make test` is the merge gate; the live lanes are opt-in. -->

- [ ] `make test` (ruff + mypy + unit pytest + frontend tsc/build/vitest) is green
- [ ] `make e2e` — only if you touched the live turn path (needs Docker + an LLM key)
- [ ] `make test-mongo` — only if you touched the persistence layer (needs a mongod)
- [ ] New behavior is covered by a test (unit or conformance), or this is docs-only

## Anything reviewers should know

<!-- Breaking changes, config/env additions (register them — the env census test
     enforces it), migration notes, deliberate trade-offs, follow-ups. -->
