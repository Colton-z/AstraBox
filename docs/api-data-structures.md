# Common data structures

> Reuse the response envelopes and identifiers shared across the AstraBox API.

AstraBox API groups reuse the following cross-resource structures. Each
AstraBox instance publishes resource-specific request and response structures
at `/docs` and `/openapi.json`.

## Success envelope

AstraBox REST resource handlers return the operation's result in `data`.

| Field | Type | Description |
|---|---|---|
| `code` | string | `OK` for a successful request |
| `message` | string | `success` for a successful request |
| `data` | any | The operation's response value |

Example:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "21e061ee-c00b-48cb-a702-fc8f410792f7"
  }
}
```

<Note>Streaming, file-download, MCP, redirect, and `204 No Content` endpoints
use the response type declared for that operation instead of the JSON success
envelope.</Note>

## Paginated list

The paginated Session list returns its cursor page inside the standard success
envelope:

| Field | Type | Description |
|---|---|---|
| `data.sessions` | array | Session objects on the current page |
| `data.has_more` | boolean | Whether more Sessions are available |
| `data.next_cursor` | string \| null | Opaque cursor for the next page, or `null` at the end |

Pass `data.next_cursor` back as the `cursor` query parameter. See
[Pagination](api-pagination.md) for request parameters and traversal examples.

## Error envelope

Error responses use this envelope:

```json
{
  "code": "INVALID_REQUEST",
  "message": "permission_mode is required",
  "data": null,
  "error": {
    "code": "INVALID_REQUEST",
    "status_code": 400,
    "category": "request",
    "retryable": false,
    "owner": "client",
    "user_message": "permission_mode is required"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `code` | string | Stable error code for programmatic handling |
| `message` | string | Message safe to show to the caller |
| `data` | any \| null | Error-specific structured data, when available |
| `error` | object | Error status, category, retryability, owner, caller-safe message, and optional diagnostics |

See [Errors](api-errors.md) for every envelope field and handling guidance.

## Timestamp

Platform-generated timestamps are ISO 8601 / RFC 3339 strings in UTC, for
example `"2026-08-24T19:26:39.616690+00:00"`. Some fields are nullable; each
resource schema calls that out explicitly.

## Identifiers

AstraBox identifiers are opaque strings. Resources do not share one public ID
prefix convention.

| Rule | Description |
|---|---|
| Type | JSON string |
| Source | Read the ID from the create, list, or detail response |
| Use | Pass the complete value unchanged in path parameters and request fields |
| Meaning | Determine the resource type from its field and endpoint, not from the ID text |
| Storage | Store enough characters for the returned value; do not assume a UUID or fixed length |

## Next steps

- [Overview](overview.md) — how AstraBox fits together.
