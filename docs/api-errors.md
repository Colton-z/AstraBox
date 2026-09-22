# Errors

> Handle the AstraBox error envelope and decide whether a failed request can be
> retried.

The AstraBox API returns errors in a consistent envelope. Each error response
carries structured fields suitable for programmatic handling and debugging.

## Error envelope

AstraBox REST resource handlers use this JSON structure:

```json
{
  "code": "SESSION_BUSY",
  "message": "session is busy",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session is busy"
  }
}
```

Every response also carries a W3C `traceparent` header. Unexpected server errors
include the same trace ID in `data.trace_id` so an operator can correlate the
response with logs.

### Field descriptions

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | Yes | Stable error code for programmatic handling |
| `message` | string | Yes | Message safe to show to the caller |
| `data` | any \| null | Yes | Error-specific structured data, when available |
| `error.code` | string | Yes | Same stable error code as the top-level `code` |
| `error.status_code` | integer | Yes | Status registered for the error code; use the actual HTTP response status for transport handling |
| `error.category` | string | Yes | Error category, such as `request`, `auth`, `state`, or `persistence` |
| `error.retryable` | boolean | Yes | Whether retrying after a delay can succeed without changing the request |
| `error.owner` | string | Yes | Component or party that must act: `client`, `session`, `mongo`, `runtime`, `template`, `platform`, or `unknown` |
| `error.user_message` | string | Yes | Message safe to show to the caller |
| `error.debug_message` | string | No | Additional diagnostic message when the error provides one |
| `error.evidence` | object | No | Structured evidence for diagnosis |
| `error.cause_code` | string | No | Lower-level cause code when one is available |

## Error types

Error codes describe the specific failure; HTTP status describes how the request
completed. Common status groups are:

| HTTP status | Example `code` | Description |
|---|---|---|
| 400 or 422 | `INVALID_REQUEST` | Invalid or missing request parameters |
| 401 | `AUTH_REQUIRED`, `UNAUTHORIZED`, `TOKEN_EXPIRED` | Authentication failed or is required |
| 403 | `FORBIDDEN`, `API_TOKEN_SCOPE_INSUFFICIENT` | Authenticated but not authorized for the operation |
| 404 | `NOT_FOUND`, `SESSION_NOT_FOUND` | Target resource is missing or inaccessible |
| 409 | `SESSION_BUSY`, `IDEMPOTENCY_KEY_CONFLICT` | Resource state conflicts with the operation |
| 429 | `ADMISSION_DENIED` | Deployment admission policy refused the work |
| 499 | `REQUEST_CANCELLED` | The caller cancelled the request |
| 5xx | `PERSISTENCE_UNAVAILABLE`, `UNEXPECTED_SERVER_ERROR` | AstraBox, infrastructure, or an upstream dependency failed |

The table contains examples, not a complete error-code catalog. Use the
deployed release's `/openapi.json` and the fields returned by the failing route.

## Error type details

### 400 or 422 — `INVALID_REQUEST`

The request format or parameters are invalid.

**Common triggers:**

- Missing required field (for example, `permission_mode`)
- Field type mismatch (such as a number where a string is expected)
- Parameter outside the accepted range
- Malformed JSON

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

```bash
# Example trigger: missing the permission_mode field
curl --fail-with-body --silent --show-error \
  -X POST "$SERVICE_URL/api/v1/sessions/$SESSION_ID/permission-mode" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 401 — authentication errors

Authentication failed.

**Common triggers:**

- Missing credential on a protected route
- Malformed or invalid bearer token
- Expired or revoked Access Token
- Credential issued by an untrusted issuer

```json
{
  "code": "TOKEN_EXPIRED",
  "message": "bearer token expired; obtain a new one from your token issuer",
  "data": null,
  "error": {
    "code": "TOKEN_EXPIRED",
    "status_code": 401,
    "category": "auth",
    "retryable": false,
    "owner": "client",
    "user_message": "bearer token expired; obtain a new one from your token issuer"
  }
}
```

```bash
# Example trigger: invalid token
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer invalid-token"
```

### 403 — authorization errors

The caller is authenticated but not authorized.

**Common triggers:**

- The identity cannot manage the target Agent
- The Access Token does not include the scope required for the operation
- A non-administrator calls an administration route

```json
{
  "code": "API_TOKEN_SCOPE_INSUFFICIENT",
  "message": "API token requires scope astrabox:admin",
  "data": { "required_scope": "astrabox:admin" },
  "error": {
    "code": "API_TOKEN_SCOPE_INSUFFICIENT",
    "status_code": 403,
    "category": "auth",
    "retryable": false,
    "owner": "client",
    "user_message": "API token requires scope astrabox:admin"
  }
}
```

```bash
# Example trigger: a read-only token calls an administration API
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/admin/environments" \
  -H "Authorization: Bearer $READ_TOKEN"
```

### 404 — not-found errors

The target resource does not exist or is not visible to the caller.

**Common triggers:**

- Agent, Session, or Environment ID does not exist
- The resource was deleted
- The caller is not authorized to discover another owner's resource
- URL path typo

```json
{
  "code": "SESSION_NOT_FOUND",
  "message": "session not found",
  "data": null,
  "error": {
    "code": "SESSION_NOT_FOUND",
    "status_code": 404,
    "category": "request",
    "retryable": false,
    "owner": "session",
    "user_message": "session not found"
  }
}
```

```bash
# Example trigger: nonexistent Session
curl --fail-with-body --silent --show-error \
  "$SERVICE_URL/api/v1/sessions/session_nonexistent_123" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### 409 — conflict errors

Resource state conflict prevents the operation.

**Common triggers:**

- Same idempotency key reused for a different conversation
- Session is already processing another turn
- Agent or Environment state does not allow the requested operation

```json
{
  "code": "SESSION_BUSY",
  "message": "session is busy",
  "data": null,
  "error": {
    "code": "SESSION_BUSY",
    "status_code": 409,
    "category": "state",
    "retryable": true,
    "owner": "session",
    "user_message": "session is busy"
  }
}
```

### 5xx — server and dependency errors

AstraBox, its infrastructure, or an upstream dependency failed.

**Common triggers:**

- Database unavailable
- Sandbox or Agent program failed to start
- Identity provider or model gateway unavailable
- Unexpected internal failure

```json
{
  "code": "PERSISTENCE_UNAVAILABLE",
  "message": "mongodb timeout/unavailable, please retry",
  "data": null,
  "error": {
    "code": "PERSISTENCE_UNAVAILABLE",
    "status_code": 503,
    "category": "persistence",
    "retryable": true,
    "owner": "mongo",
    "user_message": "mongodb timeout/unavailable, please retry"
  }
}
```

<Note>Retry when `error.retryable` is `true`, or when a `429` response provides
`data.retry_after_seconds`. Use bounded exponential backoff and wait at least
the provided number of seconds.</Note>

## Error handling best practices

1. Branch on `code` and `error.retryable`, not only on the HTTP status.
2. Log the `traceparent` response header, `code`, and `message` for diagnostics.
3. Inspect `data`, `error.evidence`, and `error.cause_code` when present.
4. Do not retry when `error.retryable` is `false` unless a `429` response provides `data.retry_after_seconds`.
5. Use bounded exponential backoff for retryable responses.

```bash
# Request with error handling
headers=$(mktemp)
trap 'rm -f "$headers"' EXIT
response=$(curl --silent --show-error -D "$headers" -w "\n%{http_code}" \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN")

http_code=$(echo "$response" | tail -1)
body=$(echo "$response" | sed '$d')

if [ "$http_code" -ge 400 ]; then
  error_code=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])")
  retryable=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin)['error']['retryable'])")
  traceparent=$(sed -n 's/^[Tt]raceparent: //p' "$headers" | tr -d '\r')
  echo "API error: $error_code retryable=$retryable traceparent=$traceparent"
fi
```

## Next steps

- [Overview](overview.md) — how AstraBox fits together.
