# Pagination

> Use the cursor and page-number schemes exposed by AstraBox list endpoints.

The paginated Session list in the AstraBox API uses **cursor-based pagination**.
Use the `next_cursor` value from a response as the `cursor` query parameter on
the next request. A cursor is an opaque position in an ordered list, not a
snapshot of changing data.

## Request parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `page` | integer | Yes | — | Set to `1` to request the paginated response shape |
| `limit` | integer | No | 50 | Items per page, constrained to 1–100 |
| `cursor` | string | No | — | Opaque cursor returned by the previous response's `next_cursor` |

<Note>Send `page=1` whenever using `limit` or `cursor`. Without `page=1`,
`GET /api/v1/sessions` returns the unpaginated Session array.</Note>

## Response structure

The paginated Session endpoint returns the standard AstraBox envelope with a
page object in `data`:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [
      { "session_id": "session_abc123", "title": "my-session", "...": "..." },
      { "session_id": "session_def456", "title": "another-session", "...": "..." }
    ],
    "next_cursor": "eyJzZXNzaW9uX2lkIjoic2Vzc2lvbl9kZWY0NTYiLCJ1cGRhdGVkX2F0IjoiLi4uIn0",
    "has_more": true
  }
}
```

### Field descriptions

| Field | Type | Description |
|---|---|---|
| `code` | string | `OK` for a successful request |
| `message` | string | `success` for a successful request |
| `data.sessions` | array | Sessions on the current page |
| `data.next_cursor` | string \| null | Opaque cursor for the next page. Pass it as the `cursor` query parameter |
| `data.has_more` | boolean | Whether more Sessions remain |

## Basic usage

### Fetch the first page

```bash
# Get the first 10 Sessions
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=10" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### Fetch the next page

Use `data.next_cursor` from the previous response as `cursor`:

```bash
# Get the next 10 Sessions
curl --fail --silent --show-error --get \
  "$SERVICE_URL/api/v1/sessions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode 'page=1' \
  --data-urlencode 'limit=10' \
  --data-urlencode "cursor=$NEXT_CURSOR"
```

### Page-number pagination

Some administration endpoints use `page` and `page_size` instead of a cursor.
For example:

```bash
# Get page 2 of the Session administration list
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/admin/sessions/all?page=2&page_size=50" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

Use each endpoint's OpenAPI definition at `/docs` or `/openapi.json`; it defines
that endpoint's pagination parameters and response fields.

## Full traversal example

The script below iterates through every Session visible to the current identity:

```bash
#!/bin/bash
# Iterate over all Sessions and print titles
BASE_URL="${SERVICE_URL}/api/v1"
next_cursor=""
page_num=1

while true; do
  url="$BASE_URL/sessions?page=1&limit=50"
  if [ -n "$next_cursor" ]; then
    url="$url&cursor=$next_cursor"
  fi

  response=$(curl --fail --silent --show-error "$url" \
    -H "Authorization: Bearer $ACCESS_TOKEN")

  count=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['sessions']))")
  next_cursor=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'].get('next_cursor') or '')")

  echo "Page ${page_num}: ${count} records"

  echo "$response" | python3 -c "
import sys, json
data = json.load(sys.stdin)['data']['sessions']
for item in data:
    print(f\"  - {item['session_id']}: {item.get('title') or 'untitled'}\")
"

  if [ -z "$next_cursor" ]; then
    break
  fi

  page_num=$((page_num + 1))
  sleep 0.1
done

echo "Done"
```

## `limit` behavior

| Value | Behavior |
|---|---|
| Omitted | Defaults to 50 |
| 1 | Minimum, returns at most 1 Session |
| 100 | Maximum, returns at most 100 Sessions |
| 0 or negative | Constrained to 1 |
| > 100 | Constrained to 100 |

<Warning>
  Passing `limit > 100` does not return more than 100 Sessions. Use `limit=100`
  and `cursor` to page through larger result sets.
</Warning>

```bash
# Fetch a single Session to check whether data exists
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=1" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## Empty results

When there is no data or the end of the list has been reached:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [],
    "next_cursor": null,
    "has_more": false
  }
}
```

## Notes

1. **Cursor handling** — `cursor` is opaque and should be passed back exactly as returned.
2. **Sort order** — Sessions are ordered by update time, newest first, with Session ID as the tie-breaker.
3. **Changing data** — a cursor does not freeze a snapshot; a Session updated while paging may move earlier in the order.
4. **Concurrent paging** — each client should keep its own cursor chain.

## Next steps

- [Overview](overview.md) — how AstraBox fits together.
