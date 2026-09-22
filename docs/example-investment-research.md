# Build an investment research Agent

An investment research Agent can stay available in AstraBox to collect permitted
company data, run repeatable analysis, and save reports in its cloud workspace.
The work continues in the cloud after the developer's laptop disconnects, and
the same Agent can be started from the web, a schedule, a signed webhook, or a
messaging platform.

| Capability | What it enables |
|---|---|
| Research method | A prompt, Skill, or Plugin can define sourcing, calculations, review, and output requirements. |
| Current data | A remote MCP server or an Agent program's web capability can reach approved data sources. |
| Analysis | The Agent program can run scripts and use files in the Session workspace. |
| Automation | A Deployment can start the Agent on a schedule or when an external event arrives. |

## Start with the research task

A simple research Agent may need only a model and a clear prompt. Add MCP
servers, Skills, Plugins, credentials, and wider network access only when the
task needs them.

Open **Management console → Agents** and create or edit an Agent. A useful
research prompt can require the Agent to:

- cite the source and reporting date for every material fact;
- separate reported facts, calculations, and interpretation;
- show formulas, units, currencies, and period definitions;
- explain missing or conflicting data; and
- save useful tables, scripts, and reports in the workspace.

Choose an Environment that runs the required Agent program and permits the
connections the research sources need. An Environment determines the sandbox,
network access, and model connection used for the work.

## Connect research sources

Add a remote MCP server when a licensed filing, market-data, document, or
internal service provides one. The server remains remote whether its definition
is entered directly on the Agent or supplied by a Plugin.

When the source requires a credential, store it in a Credential Vault and use
protected delivery for the source's exact destination. If the Environment uses
limited networking, allow the remote host. The Agent should only access data
that the operator and user are authorized to use.

See [Configure MCP servers, Plugins, and Skills](adding-tools.md),
[Credential Vaults](credentials.md), and
[Protect credentials used by Agents](egress-credential-injection.md).

## Add a repeatable research method

A Skill can provide a focused procedure and supporting files. A Plugin can
package Skills, commands, and MCP definitions. Both are optional: the selected
Agent program keeps its native capabilities when neither is configured.

Use a reviewed source and pin Git-based extensions to a reviewed revision. The
research method can define how to choose comparable periods, handle
restatements, calculate metrics, cite evidence, and format the final report.

## Run and inspect the work

Start the Agent and give it a concrete task, for example:

> Compare reported segment revenue for the last three fiscal years. Cite the
> filing and reporting date behind every value, explain classification changes,
> save the extraction script, and write the result to `segments.csv`.

The conversation and its work belong to an AstraBox Session. The Agent can read
and write files under `/workspace`; the web interface can preview and download
those files. Keeping the script, intermediate data, and report together makes
the result easier to reproduce and review.

## Run the research automatically

Use a scheduled Deployment for recurring monitoring. Use a signed webhook when
another system should start the Agent after an event, or connect a messaging
platform when people should request and receive research from chat.

Each scheduled or webhook invocation creates a new Session. A messaging
platform starts or continues the Session mapped to its external conversation.

See [Automate Agent runs](deployments.md) for these trigger options.

## Review the result

Check every cited source, date, calculation, unit, and saved file before using
the output. Confirm that comparable companies and periods use the same basis,
and treat missing data explicitly. Generated research is an aid to analysis,
not personalized investment advice.

## Related guides

- [Configure an Agent](authoring-agents.md)
- [Configure MCP servers, Plugins, and Skills](adding-tools.md)
- [Run a Session](sessions.md)
- [Automate Agent runs](deployments.md)
