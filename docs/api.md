# API Overview

> Connect to a deployed AstraBox API and discover its exact current surface.

The AstraBox API provides full management capabilities for self-hosted cloud
Agents, covering Agent creation, Environment configuration, Session lifecycle,
event streaming, files, Deployments, credentials, and more. REST endpoints use
JSON for requests and responses; streaming endpoints use Server-Sent Events.

<Note>Every AstraBox instance publishes interactive API documentation at
`/docs`; the OpenAPI document at `/openapi.json` describes the exact API
surface of the deployed release.</Note>

## Gateway URL

| Environment | URL |
|---|---|
| Self-hosted production | `https://astrabox.example.com/api/v1` |
| Local development | `http://127.0.0.1:8088/api/v1` |

Replace `astrabox.example.com` with the public origin of your deployment.

## Versioning

The API is currently at version `v1`. Endpoints use the `/api/v1` prefix; no
additional version header is required. Health, readiness, metrics, and generated
API documentation use top-level paths.

## Available APIs

| Resource | Description | Base path |
|---|---|---|
| Agents | Create, read, update, delete, and authorize Agents | `/agents` |
| Assistants | Manage Assistants and their persistent workspaces | `/assistants` |
| Environments | Manage the infrastructure available to Agents | `/admin/environments` |
| Sessions | Read Session state, submit turns, stream events, and manage lifecycle | `/sessions` |
| Files | List, upload, move, download, and delete files in a Session workspace | `/sessions/{session_id}/files` |
| Extensions | Assign remote MCP servers and Skills to an Agent | `/agents/{agent_id}/extensions` |
| Remote MCP servers | Manage administrator-provided remote MCP connections | `/admin/mcp-servers` |
| Vaults | Store Credentials and assign Vaults to Agents or Assistants | `/admin/vaults` |
| Deployments | Schedule an Agent, expose a webhook, or connect a messaging product | `/admin/agents/{agent_id}/deployments` |
| MCP | Expose accessible Agents to an MCP client | `/mcp` |
| Authentication | Browser login, callback, logout, and current-login state | `/auth` |
| Administration | Inspect sandboxes, Sessions, operations, and instance configuration | `/admin` |

A Session is created through an Agent or Assistant conversation endpoint; there
is no independent `POST /sessions` payload.

## Request size limits

AstraBox does not impose one global JSON request-body limit across every route.
The reverse proxy may set a deployment-wide limit, and individual endpoints
enforce limits required by their resource. File uploads are streamed. A single
Files API download is limited to 64 MiB so one response cannot consume
unbounded API-process memory.

When operating behind a proxy, configure its request, response, and streaming
timeouts for the largest operation the deployment permits. Return `413` from
the proxy when a request is too large.

## Required headers

Team deployments require a valid browser cookie or bearer token on protected
routes. JSON requests should include `Content-Type`:

```text
Authorization: Bearer $ACCESS_TOKEN
Content-Type: application/json
```

The `Authorization` header is omitted in loopback-only local identity mode.
Conversation creation also accepts an optional `Idempotency-Key` header. Reuse
the same key only for the same user and Agent or Assistant.

## Release compatibility

1. The API surface belongs to the installed AstraBox release.
2. Pin the AstraBox release used in production.
3. During an upgrade, review that release's `/openapi.json` document and
   regenerate typed clients from it.

## Quick connectivity check

```bash
# List Agents visible to the current identity
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Successful response:

```json
{
  "code": "OK",
  "message": "success",
  "data": []
}
```

## Rate Limiting

The API application layer has no active rate limiting by default. A deployment
can install admission policy and enforce traffic limits at its proxy or gateway.
Those controls may return `429`, while unavailable infrastructure may return
`503`.

Clients should limit concurrency and use bounded exponential backoff for `429`
and retryable `5xx` responses. When an admission denial includes
`data.retry_after_seconds`, wait at least that long before retrying.

## Next steps

- [Authentication](api-authentication.md) — authenticate API requests.
- [Errors](api-errors.md) — error codes and troubleshooting.
- [Pagination](api-pagination.md) — pagination for list endpoints.
