# Agent Skills

> Attach domain expertise to your Agent.

Skills add **domain expertise** to an Agent. A Skill is a structured set of
instructions and procedures that makes an Agent more capable and reliable on a
specific kind of task.

Open **Console → Agents**, create or open an Agent, and select Skills in the
**MCP, Skills and Plugins** section.

![Select Skills while creating an Agent](img/agent-create-console-en.png)

## Ways to add Skills

| Method | Where | Description |
| --- | --- | --- |
| Administrator-managed Skill | **MCP servers and Skills** on the Agent detail page | Select a reusable Git-backed Skill from the administrator-managed catalog |
| Direct Git source | **MCP, Skills and Plugins** on the Agent create or edit page | Add a repository URL with an optional revision and subdirectory |

## Versioning model

Skills use a Git source and an optional revision:

- **Skill source** — a repository URL with an optional `@ref` and `#path`, for
  example `https://github.com/example/skills.git@main#skills/code-review`.
- **Pinned revision** — use a reviewed commit SHA when the Skill must not
  change until the Agent is edited.
- **Unpinned revision** — omitting the ref follows the repository's default
  branch; a branch or tag follows that named ref when a runtime is prepared.
- **Administrator-managed Skill** — the Skills management page supplies the
  Git source behind a catalog entry; the Agent stores the selection.

AstraBox does not create a second Skill-content format. It prepares the Skill
in the native Skill directory used by the selected Agent program.

## What Skills Do

- **Inject domain knowledge** — give a generalist Agent specialized abilities (code review, document generation, etc.).
- **Standardize procedures** — ensure the Agent follows consistent steps and produces consistent output.
- **Reusable** — define once and share across multiple Agents.

## Skill File Layout

A Skill repository contains a directory whose core file is `SKILL.md`:

```
my-skill/
├── SKILL.md          # Required: Skill definition
├── templates/        # Optional: template files
│   └── report.md
└── examples/         # Optional: example files
    └── sample.json
```

`SKILL.md` is the core file, written as YAML frontmatter plus Markdown:

```markdown
---
name: my-skill
description: Perform structured code reviews and produce improvement suggestions
---

# Code Review

## Steps
1. Analyze the structure and architecture of the code.
2. Check for common issues (security, performance, maintainability).
3. Output a structured review report.

## Pitfalls
- Don't fixate on formatting — prioritize logic errors.
- Provide concrete fixes rather than vague critiques.
```

## Create a Skill

Create the Skill directory in a Git repository and commit its complete content.
Record the repository URL, revision, and subdirectory. AstraBox loads Skills
from Git rather than accepting a separate zip upload.

When **Manage Skills** is available on the Agent detail page, use it to add a
Git-backed Skill to the administrator-managed catalog.

## Bind to an Agent

Open the Agent in the web console. To use an administrator-managed Skill,
select it under **MCP servers and Skills** and save. To use a direct Git source,
add its descriptor to **Skills** under **MCP, Skills and Plugins** and
save that section.

## Versioning

To publish a new Skill revision, commit the changes. Update the Agent's Git ref
or the administrator-managed catalog entry when that revision should be used.
An already-running Agent program is not rewritten in place; when AstraBox first
prepares or rebuilds a Session runtime, it resolves the Agent's current Skill
sources.

With prewarming enabled, Skills may already be prepared before a Session starts.
Use **Reprepare** on the Agent to fetch its saved Skill sources again and replace
unclaimed capacity. Existing Sessions are not interrupted, and pinned commits
remain pinned.

## Get a Skill

Open the Agent detail page. Direct Git sources appear in **Skills** under
**MCP, Skills and Plugins**. The selected administrator-managed Skills
appear in **MCP servers and Skills**.

## List Skills

Open the Agent detail page. The **MCP servers and Skills** selector lists the
administrator-managed Skills available to that Agent. Select **Manage Skills**
to open the catalog management page when that button is available.

## Authoring Tips

1. **State the trigger** — write the `description` so it's clear when this Skill should be used.
2. **Be concrete in steps** — describe precise actions, not vague guidance.
3. **Document pitfalls** — help the Agent avoid common mistakes.
4. **Provide validation** — tell the Agent how to confirm the task is complete.

## FAQ

**Q: How are Skills different from the Agent `system` prompt?**

A: `system` is general guidance that applies to every task. A Skill is an
on-demand expertise module the Agent activates based on the task at hand.

**Q: How many Skills can an Agent reference?**

A: There's no hard limit, but keep it under 10 to maintain predictable behavior.

**Q: Which Agent programs support Skills?**

A: Support is declared by the selected Agent program. AstraBox rejects Skill
configuration that the program does not consume.

**Q: Is there a size limit on a Skill zip?**

A: AstraBox does not upload Skills as zip archives. Repository and sandbox
limits apply to the Git content loaded into the runtime.

## Next steps

- [Agent tools and extensions](adding-tools.md) — Configure MCP servers,
  Skills, and Plugins.
- [Defining an Agent](authoring-agents.md) — Review Agent configuration.
- [Sessions](sessions.md) — Start work with an Agent.
- [Quickstart](quickstart.md) — Run your first AstraBox Agent in five steps.
