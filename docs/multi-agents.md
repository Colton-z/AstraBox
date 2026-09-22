# Multiagent orchestration

> Let an Agent delegate work to child runs and inspect their activity from the
> parent Session.

Multiagent orchestration lets the Agent program in a Session divide work among
child runs. Each child run has its own engine conversation, and the parent
Agent combines their results into the final response.

Use it for complex tasks that can be divided by responsibility, run in
parallel, or executed in stages. For one-step work, strictly sequential tasks,
or tasks where multiple workers would frequently edit the same files, use a
single Agent to keep the workflow simpler.

## How it works

The installed Agent program owns delegation. It decides whether to start a
child run, what context to send, and whether a completed child can receive a
follow-up. AstraBox projects those engine-native runs into one Session-level
view without translating them into a separate Agent roster or Thread model.

| Concept | Description |
| --- | --- |
| Parent Agent | The Agent handling the Session. It can divide the task, follow up on child results, and produce the final response. |
| Child run | One delegated engine conversation, identified by `child_run_id`. It has its own messages, status, and optional usage summary. |
| Child-run tree | The parent/child relationship reported by the Agent program. `depth` and `parent_child_run_id` preserve nested delegation when the program supports it. |
| Available operations | Actions the current Agent program exposes for a child run. AstraBox publishes them in `operations` instead of inferring support from the program name. |

Resources and context have the following scopes:

| Scope | Behavior |
| --- | --- |
| Environment and filesystem | Child runs execute inside the parent Session runtime and share its Environment, sandbox, and workspace. |
| Vaults | Child runs use the credentials made available to the parent Agent runtime. |
| Conversation history | Each child run has its own engine-owned messages. It receives only the context passed by its parent. |
| Agent configuration | Delegation follows the installed Agent program and the Agent configuration resolved for the Session runtime. |
| Event stream | The parent Session stream emits `data-child-runs-changed` when clients should refresh the child-run view. A child-run endpoint returns one child's complete projected messages. |

:::warning
Parallel child runs share a filesystem. Define clear file or directory
responsibilities in the system prompt so that they do not edit the same file
concurrently.
:::

## What to delegate

Design the system prompt around task dependencies and the division of responsibilities:

- Assign independent research, module implementation, or data collection tasks
  to separate child runs so they can run in parallel.
- Split implementation, testing, and review by responsibility. For example, a
  child run can implement a change while another reviews the result and returns
  an issue list.
- Run dependent work in stages, such as implementation followed by review. The
  parent Agent can use the review result to decide whether another iteration is
  needed.

The system prompt should also define each child's expected output and identify
work that the parent Agent must handle itself.

## Configure the parent Agent

### Configure in the console

1. Open **Console → Agents** and create or edit an Agent whose installed Agent
   program supports delegation.
2. Write a system prompt that defines task decomposition, delegation criteria,
   deliverable formats, and conflict handling.
3. Add only the MCP servers, Skills, Plugins, and repository access required by
   the work.
4. Save the Agent and start a Session.

AstraBox does not define a `multiagent.agents` roster. Delegation tools and
child-run semantics belong to the selected Agent program. The **Agents** tab
stays visible in a Session and shows an empty state until the program starts a
child run.

### Configure with the API

Use the normal Agent create or update API. There is no second coordinator
resource or platform-owned child-Agent configuration. Read the live Agent
schema from `GET /api/v1/agent-configuration/schema` to see the inputs consumed
by the selected Agent program, then configure its system prompt and extensions
as described in [Defining an Agent](authoring-agents.md).

### Agent configuration and Session runtime

A Session does not freeze a selectable Agent version or a roster snapshot.
AstraBox resolves the current Agent and Environment when it prepares or
rebuilds the Session runtime. Saving the Agent does not rewrite a running task
in place; a later runtime preparation can use the current saved configuration.

## Create and run a Session

Create a Session from the parent Agent:

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/agents/$AGENT_ID/conversations" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{}'
```

Send a task that names a useful division of responsibilities:

```bash
curl --no-buffer --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/ai-stream" \
  --header "Authorization: Bearer $ACCESS_TOKEN" \
  --header 'Content-Type: application/json' \
  --data '{
    "content": "Analyze the login module implementation and security risks. Delegate independent implementation and review work when useful, then recommend fixes.",
    "client_message_id": "login-review-1"
  }'
```

A prompt does not force delegation. The Agent program decides whether a child
run is useful and how to execute it.

## Connect MCP servers and Vaults

MCP servers, Skills, Plugins, repository access, and Vaults are configured on
the parent Agent or its Environment:

- Child runs use capabilities available inside the same Session runtime.
- The Environment's network policy applies to parent and child activity.
- Vault credentials follow the same Agent or Assistant assignment and outbound
  protection rules as the parent Session.
- Grant high-privilege credentials only when the delegated work requires them.

See [Agent tools and extensions](adding-tools.md),
[Vaults](credentials.md), and [Permission modes](permission-modes.md).

## Observe child runs and events

Open a Session and select the **Agents** tab. It shows the child-run tree,
current status, description, summary, and usage values reported by the Agent
program. Select a child to open its messages and tool activity.

List all child runs in tree order:

```text
GET /api/v1/sessions/{session_id}/child-runs
```

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

Read one child run's projected messages:

```text
GET /api/v1/sessions/{session_id}/child-runs/{child_run_id}/messages
```

```bash
curl --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs/$CHILD_RUN_ID/messages" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

The Session stream uses `data-child-runs-changed` as a refresh signal. Read the
child-run endpoints for the current tree and transcript instead of reconstructing
state from transient notifications.

## Interrupt one child run

The `operations` array contains `stop` only while the selected Agent program
provides a valid control for that child run. Request the stop through that
published operation:

```text
POST /api/v1/sessions/{session_id}/child-runs/{child_run_id}/stop
```

```bash
curl --silent --show-error --request POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/child-runs/$CHILD_RUN_ID/stop" \
  --header "Authorization: Bearer $ACCESS_TOKEN"
```

A successful request returns `status: "accepted"`. A closed child run returns
`CHILD_RUN_ALREADY_TERMINAL`; a live run without stop support returns
`CHILD_RUN_CONTROL_UNAVAILABLE`.

## Tool permissions and interactions

The Session's engine-native permission mode applies while the Agent program
runs parent and child work. When the program requests a confirmation or other
input, AstraBox publishes the interaction in the parent Session and returns the
answer through the same engine adapter.

Use the interaction ID with `POST
/api/v1/sessions/{session_id}/interaction-respond`. A pending interaction means
the current work is waiting for input, not that the child run completed. See
[Permission modes](permission-modes.md) for the response shapes.

## Limits

| Item | Behavior or limit |
| --- | --- |
| Agent roster | AstraBox has no platform child-Agent roster. The installed Agent program owns delegation. |
| Child runs | Count and concurrency are defined by the Agent program and its current configuration. |
| Delegation depth | The Agent program decides whether nested delegation is available; `depth` and `parent_child_run_id` report the resulting tree. |
| Session foreground state | A Session can report `BACKGROUND_RUNNING` and accept foreground input while child tasks remain active. |
| Agent references | Child runs are engine conversations, not references to separate AstraBox Agent records or versions. |
| Controls | Each row's `operations` array is authoritative; AstraBox does not require or infer a common delegation toolset. |

## Troubleshooting

### The Agent does not delegate

Confirm that the installed Agent program supports child runs and that the
system prompt defines work that can be divided. A Session with no child runs is
valid and shows an empty **Agents** tab.

### A Session does not use an updated Agent configuration

Saving an Agent does not reconfigure a task or runtime already in progress. A
new Session uses the saved configuration, and an existing Session can use it
after AstraBox later recreates that Session's runtime.

### Creating or updating the parent Agent returns an error

Read `GET /api/v1/agent-configuration/schema` and use the validation error to
locate the rejected field. Do not add a platform `multiagent.agents` roster: any
delegation-specific configuration is declared by the selected Agent program.
An outdated Agent `version` instead returns `409 AGENT_VERSION_CONFLICT`; read
the current Agent before retrying the update.

## Related documentation

- [Defining an Agent](authoring-agents.md)
- [Start a Session](sessions.md)
- [SSE Event Stream](events-stream.md)
- [Permission modes](permission-modes.md)
- The deployed instance's `/docs` and `/openapi.json` API references
