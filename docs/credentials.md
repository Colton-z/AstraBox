# Authenticate with Vaults

> Store credentials in your deployment and make them available to Agent
> Sessions without hard-coding secrets.

Agents often need to access third-party services — GitHub, Jira, databases, or custom remote MCP servers. Vaults provide secure credential storage so you can store tokens in your own AstraBox deployment and use them in Sessions on demand without hard-coding secrets in your code.

## Core concepts

| Concept | Description |
|---|---|
| Vault | A credential container that can hold multiple Credentials |
| Credential | A single credential bound to a service URL or environment variable name |
| `auth.type` | Credential auth type: Bearer token for an MCP service (`static_bearer`), OAuth token for an MCP service (`mcp_oauth`), API-key header for an MCP service (`mcp_static_header`), HTTP Basic for an HTTPS destination (`http_basic`), or Environment variable for another service (`environment_variable`) |
| `vault_ids` | The ordered list of Vault IDs assigned to an Agent or Assistant |

## Security

- `access_token` is **never** returned in API responses.
- Other secrets such as `token`, `password`, `refresh_token`, and `client_secret` are also never returned.
- Credentials are encrypted at rest.
- Credentials are available only to assigned workloads. Agent preparation also
  uses assigned HTTP Basic credentials to download private Skills and Plugins
  before a Session exists.

## End-to-end flow

### 1. Create a Vault

Create Vaults in **Console → Credentials**, or use the administration API:

```bash
curl -X POST https://astrabox.example.com/api/v1/admin/vaults \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "display_name": "My GitHub credentials",
    "metadata": {}
  }'
```

Example response:

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "vault_id": "vlt_8a15f9c8d1d34cf4b7b485d735c77d75",
    "display_name": "My GitHub credentials",
    "metadata": {},
    "archived_at": null,
    "created_at": "2026-08-24T08:00:00Z",
    "updated_at": "2026-08-24T08:00:00Z"
  }
}
```

### 2. Add a Credential {#2-add-a-credential}

For a static Bearer token, add a Credential with nested `auth`:

A target can receive multiple Vaults. The Session uses the current assignment
at creation time. Removing an assignment stops later Sessions from receiving
that Vault. The platform automatically admits the exact destination hosts named
by active outbound credential bindings. Those hosts are already part of the
effective sandbox policy, so the Environment allowlist needs no duplicate
entries.

```bash
curl -X POST \
  https://astrabox.example.com/api/v1/admin/vaults/vlt_8a15f9c8d1d34cf4b7b485d735c77d75/credentials \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "auth": {
      "type": "static_bearer",
      "mcp_server_url": "https://jira.example.com/mcp",
      "token": "jira_token_xxxxxxxx"
    }
  }'
```

The response returns `credential_id` and a sanitized `auth` object. It does not
include secret values.

For MCP OAuth, import the access token and optional refresh configuration:

```bash
curl -X POST \
  https://astrabox.example.com/api/v1/admin/vaults/vlt_8a15f9c8d1d34cf4b7b485d735c77d75/credentials \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "auth": {
      "type": "mcp_oauth",
      "mcp_server_url": "https://mcp.linear.app/mcp",
      "access_token": "access_token_xxxxxxxx",
      "expires_at": "2026-08-24T09:00:00Z",
      "refresh": {
        "token_endpoint": "https://api.linear.app/oauth/token",
        "client_id": "astrabox",
        "auth_method": "none",
        "refresh_token": "refresh_token_xxxxxxxx"
      }
    }
  }'
```

AstraBox does not run the browser authorization flow. Obtain the token from the
remote MCP service, then store it in the Vault. When the Credential contains a
valid refresh configuration, AstraBox refreshes an expired access token when
preparing or reconnecting the runtime and before each top-level Agent task.
It does not check token expiry before every individual MCP HTTP request.

For a service that uses a custom request header, use `mcp_static_header`. To
make another service credential available as an environment variable, use
`environment_variable`; see [Protect credentials used by Agents](egress-credential-injection.md)
for its host and request restrictions.

For a private Git repository accessed over HTTPS, choose **HTTP Basic (Git
HTTPS)** in the console, or create an `http_basic` credential:

```http
POST /api/v1/admin/vaults/{vault_id}/credentials
Content-Type: application/json

{
  "display_name": "Private Skill repository",
  "auth": {
    "type": "http_basic",
    "url": "https://github.com/acme/private-skills.git",
    "username": "x-access-token",
    "password": "<repository-access-token>"
  }
}
```

Use the username required by the Git host and a token with the repository
permissions the operation needs. The type describes the authentication method,
not a Git vendor. `password` is write-only; the response includes only the type,
destination URL, and username. The URL must be HTTPS on port 443 with a non-root
path, without embedded credentials, query, fragment, or wildcard patterns.

Assign the Vault to the Agent and keep the Skill or Plugin Git URL free of
credentials. AstraBox supplies the binding before downloading those sources.
OpenSandbox adds HTTP Basic authentication to `GET`, `HEAD`, and `POST`
requests for the configured path and its descendants, not sibling repository
paths. No token or placeholder needs to be placed in the Git URL or sandbox
environment. This uses the same platform path for every Agent program and for
custom or LiteLLM-managed Git Skill sources.

### 3. Use in a Session

Assign the Vault to an Agent or Assistant in **Console → Credentials**, or set
the ordered `vault_ids` through the administration API:

```bash
curl -X PUT \
  https://astrabox.example.com/api/v1/admin/agents/agent_xxx/credential-vaults \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "vault_ids": ["vlt_8a15f9c8d1d34cf4b7b485d735c77d75"]
  }'
```

New Sessions created from that Agent automatically gain access to every active
Credential in the Vault. For MCP credentials, the first Vault with a matching
MCP server URL wins. HTTP Basic credentials likewise use the first Vault with a
matching destination URL. An Assistant can use MCP credentials, but it cannot
be assigned a Vault containing `environment_variable` or `http_basic`
credentials.

## Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `display_name` | string | Yes | Display name for the Vault |
| `metadata` | object | No | Custom metadata |
| `auth.type` | string | Yes for credentials | `static_bearer`, `mcp_oauth`, `mcp_static_header`, `http_basic`, or `environment_variable` |
| `auth.mcp_server_url` | string | Yes for MCP credentials | Remote MCP server URL |
| `auth.token` | string | Yes for `static_bearer` | Bearer token value; write-only |
| `auth.access_token` | string | Yes when importing `mcp_oauth` | OAuth access token; write-only |
| `auth.header_name` | string | Yes for `mcp_static_header` | Custom request-header name |
| `auth.value` | string | Yes for `mcp_static_header` | Custom request-header value; write-only |
| `auth.url` | string | Yes for `http_basic` | Clean HTTPS destination URL with a non-root path |
| `auth.username` | string | Yes for `http_basic` | HTTP Basic username; no colon or control characters |
| `auth.password` | string | Yes for `http_basic` | Password or access token; write-only |
| `auth.secret_name` | string | Yes for `environment_variable` | Environment variable name |
| `auth.secret_value` | string | Yes for `environment_variable` | Secret value; write-only |
| `auth.expires_at` | string | No | OAuth access-token expiration time in RFC 3339 format |
| `auth.refresh` | object | No | OAuth refresh configuration |

## FAQ

**Q: What happens when an MCP OAuth token expires?** A: If the Credential has a
refresh token and refresh configuration, AstraBox refreshes an expired token
during runtime preparation or reconnect and before the next top-level Agent
task. If refresh is unavailable or no longer valid, rotate the
Credential.

**Q: Can I update a Credential's token?** A: Yes. A `PATCH` request rotates the
supplied write-only secret fields. The credential type, MCP server URL, custom
header name, HTTP Basic destination URL and username, environment variable name,
OAuth token endpoint, and OAuth client ID are immutable; archive the Credential
and create a new one to change them.

**Q: How many Vaults can a Session reference?** A: There's no hard limit, but group by service for clarity.

**Q: My token leaked. What now?** A: Delete the Credential immediately, revoke the token in the third-party platform, and create a new Credential.

**Q: Can I read stored tokens?** A: No. For security, credential secrets are
write-only — you can only rotate, archive, or delete them.

> Use separate Vaults per environment (development vs. production) to avoid mixing credentials.

## Next steps

- [Sessions](sessions.md) — Run an Agent with its assigned Vaults.
- [Defining an Agent](authoring-agents.md) — Assign the Environment and
  extensions that use credentials.
- [Protect credentials used by Agents](egress-credential-injection.md) —
  Configure protected delivery and outbound restrictions.
- [Environments](environments.md) — Configure the runtime and its network
  access.
