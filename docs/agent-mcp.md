# Use AstraBox Agents from an MCP client

AstraBox exposes the Agents an identity is authorized to use through one remote
MCP server. Connect it once, and an MCP client can start Agent work, follow the
result, respond when the Agent asks for input, and stop active work.

| Item | Value |
|---|---|
| Transport | Streamable HTTP |
| Server URL | `<your AstraBox URL>/api/v1/mcp` |
| Authentication | An AstraBox MCP client key, an accepted identity-provider token, or local single-user mode |

Every compatible client connects to the same remote MCP server. Only the
client's configuration syntax and where it stores that configuration differ.

![How an MCP client reaches an AstraBox Agent](./img/agent-mcp.svg#inline)

## Connect a client

1. Open **Management console → MCP clients**.
2. Select **Issue key**, name the machine or client, and set the key scope.
3. Copy the generated MCP configuration before leaving the page. It already
   contains this deployment's server URL and the issued key.
4. Paste the block into the client's remote HTTP MCP configuration and check the
   connection in that client.

The secret is shown only once. Keep a configuration containing it in user scope
and outside version control. If the key is lost, revoke it and issue another.

## Key scope

| Scope | What it allows |
|---|---|
| **Read only** | Discover authorized Agents and read the status of existing work. |
| **Read and converse** | Read, start work, send input, answer interactions, and cancel active work. |

An MCP client key carries the issuing user's identity and this additional scope.
AstraBox also runs the current Agent authorization check on every tool call, so
revoking a key or changing Agent access takes effect on the next request.

OIDC deployments can instead accept an access token from the configured
identity provider. JWT deployments accept a token that passes their configured
issuer, signature, audience, and expiry checks. Local single-user mode accepts a
local connection without an `Authorization` header.

## Run a task

The MCP client discovers the available operations and their inputs directly
from the server. A task normally follows this sequence:

1. The client finds an Agent the current identity is authorized to use.
2. `create_conversation` creates a new AstraBox Session and returns its
   `session_id` and URL.
3. `send_message` submits the work and returns without holding one MCP call open
   for the whole task.
4. The client uses `get_status` to read current state and recent messages.
5. If the state is `WAITING_INPUT`, the client sends the answer and continues
   checking status. At `READY`, it reads the result or opens the Session URL.

Despite the tool name, `create_conversation` creates an AstraBox Session. Its
`session_id` is not an MCP transport session ID. Create a new Session after one
reaches `TERMINATED` or `DELETED`.

## Understand the connection

The endpoint is a stateless Streamable HTTP MCP server. The client handles MCP
initialization, protocol-version negotiation, tool discovery, and JSON-RPC
messages. `send_message` returns after AstraBox accepts the work so that a task
can continue in the cloud independently of one long-lived tool call.

Putting the server URL in a configuration file on a laptop does not make it a
local MCP server. The server remains remote; only its configuration is stored
locally.

## Related guides

- [Run a Session](sessions.md)
- [Set up team login](team-login.md)
- [Configure MCP servers, Plugins, and Skills for an Agent](adding-tools.md)
