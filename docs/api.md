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

Replace `astrabox.example.com` with the public origin of your deployment. A
deployment published under a path puts that path before `/api/v1`, such as
`https://example.com/astrabox/api/v1`, and serves `/docs` and `/openapi.json`
below it as well; see
[Publish AstraBox under a path](deploy.md#publish-under-a-path).

## Versioning

The API is currently at version `v1`. Endpoints use the `/api/v1` prefix; no
additional version header is required. Health, readiness, metrics, and generated
API documentation use top-level paths.

## Available APIs

| Resource | Description | Base path |
|---|---|---|
| Agents | Create, read, update, delete, and authorize Agents | `/agents` |
| Assistants | Manage Assistants and their persistent workspaces | `/assistants` |
| Artifacts | Manage durable pages, versions, sharing, and viewer capabilities | `/artifacts`, `/artifact-share/{token}/runtime` |
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

## Artifacts

All paths below are relative to `/api/v1`. JSON results use `ApiEnvelope`
(`{"code": "OK", "message": "success", "data": ...}`); live and model
responses use SSE. Only the owner can use the management surface; a foreign
artifact and a missing one both return `404 ARTIFACT_NOT_FOUND`.

| Method | Path | Input or result |
| --- | --- | --- |
| GET | `/artifacts` | Filters: `cursor`, `limit`, `session_id`, `assistant_id`, `q`; result: `items`, `next_cursor`, `has_more` |
| GET | `/artifacts/{id}` | Artifact metadata, `manifest`, origin, content, and a nested current `version` record |
| PATCH | `/artifacts/{id}` | `{title?, description?}` |
| DELETE | `/artifacts/{id}` | Delete the artifact, its versions, data, and share state |
| GET | `/artifacts/{id}/versions` | Version metadata without content |
| GET | `/artifacts/{id}/versions/{n}` | Version metadata and content |
| POST | `/artifacts/{id}/versions/{n}/restore` | Publish the saved content as a new version; returns an artifact record |
| GET | `/artifacts/{id}/share` | Share configuration |
| POST | `/artifacts/{id}/share` | `{access, audience, version?, expires_in_seconds?}` |
| DELETE | `/artifacts/{id}/share` | Revoke the link |

`access` is `view` or `interact`; `audience` is `anyone` or `signed_in`.
`version: null` follows the latest publication; a number pins that version.
Share state contains `enabled`, `token`, `access`, `audience`, `version`,
`created_at`, and `expires_at`. An anonymous visitor receives only `view`.
Artifacts declaring `sample` or `agent` refuse `anyone` with
`ARTIFACT_SHARE_AUDIENCE_RESTRICTED`; their anonymous runtime access returns
`401 ARTIFACT_SIGN_IN_REQUIRED`. Revoked, expired, and invalid tokens return 404.

The viewer runtime has one surface exposed through two prefixes:
`/artifacts/{id}/runtime` for the logged-in owner and
`/artifact-share/{token}/runtime` for link viewers. The latter accepts signed-in
credentials when present; the token does not elevate an anonymous viewer to
Interact. Each call checks the artifact, token, served manifest, and viewer
permissions. Substitute either prefix for `{runtime}` below.
Anonymous calls include the same per-load `guest_id` query parameter on every
runtime request. The server prefixes this id with `guest_`; it is not a profile id.

| Method | Path | Input or result |
| --- | --- | --- |
| GET | `{runtime}` | `{artifact, version, content, manifest, viewer: {level, id, guest}, pinned, after_seq}`; share views include `artifact.share: {version, access, audience}` |
| GET | `{runtime}/live?after_seq=N` | SSE `version`, `data`, `room`, `presence`, `deleted`, and `error`; journal events carry their sequence as the SSE id |
| POST | `{runtime}/publish` | `{content, base_version}`; owner only, requires `artifact`, unavailable on pinned links |
| GET | `{runtime}/data/{path}` | `{path, exists, data}`; missing data is `null`; denied reads return `403 ARTIFACT_DATA_DENIED` |
| PUT | `{runtime}/data/{path}` | `{data}` replaces a document |
| PATCH | `{runtime}/data/{path}` | `{data}` updates a document |
| DELETE | `{runtime}/data/{path}` | Delete a document |
| POST | `{runtime}/data-query` | `{collection, where: [{field, op, value}], order_by: [{field, direction}], limit}` → `{items: [{id, path, data}]}` |
| POST | `{runtime}/data-add` | `{collection, data}` → `{id, path}` |
| POST | `{runtime}/sample` | `{input, max_tokens?, tools?}`; SSE `delta {text}`, `done {text, tool_calls, truncated}`, `error {code, message, text}` |
| POST | `{runtime}/room/{room}/emit` | `{topic, data}` |
| POST | `{runtime}/room/{room}/presence` | `{state}` or `{state: null}` |
| GET | `{runtime}/viewers?ids=...` | `{profiles: [{id, display_name}]}` for opaque per-artifact viewer ids |
| POST | `{runtime}/agent-runs` | `{prompt}` → `{run_id, session_id, url}` |
| GET | `{runtime}/agent-runs/{run_id}` | `{state, session_id, error?}` |

Artifact creation and content edits use the built-in `artifacts` MCP tools:
`create_artifact`, `update_artifact`, `get_artifact`, `list_artifacts`,
`artifact_data`, and `artifact_guide`. There is no owner REST creation endpoint.
Content updates, page publications, and restores create immutable versions.
`update_artifact` accepts `content`, `file_path`, or
`edits: [{old_string, new_string, replace_all?}]`, with optional `base_version`.
Stale edits return `ARTIFACT_VERSION_CONFLICT`. Tool results that identify an
artifact carry `astrabox_artifact: {id, version, title, kind}`.

The artifact journal is separate from the conversation event stream; it does
not introduce a conversation-stream event type. See the [artifact manual](artifacts.md)
for capability declarations, consent, isolation, and size limits.

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
