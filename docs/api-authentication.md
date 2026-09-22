# Authentication

> Authenticate AstraBox API requests with a browser login cookie or OAuth Access Token.

An AstraBox deployment using OIDC accepts two types of credentials: a
<strong>browser login cookie</strong> and an <strong>OAuth Access Token</strong>.
Protected API requests must include one valid credential.

| Credential | Use case | How to obtain it |
|---|---|---|
| Browser login cookie | User identity; suited to browser applications and the AstraBox console | Sign in through the configured identity provider |
| OAuth Access Token | Machine identity; suited to server-side integrations and automation | Exchange a confidential OAuth client's credential for a short-lived token |

<Note>Use the long-lived OAuth client secret to obtain a short-lived Access
Token. Do not use the client secret directly to call AstraBox API routes.</Note>

## Set the service URL

API requests use the public origin of the self-hosted AstraBox deployment. Set
the service endpoint:

```bash
export SERVICE_URL="https://astrabox.example.com"
```

For a local installation, use `http://127.0.0.1:8088`.

## Option 1: Use a browser login cookie

### Sign in through the identity provider

1. Open the AstraBox console.
2. Select **Sign in**.
3. Complete authentication with the configured identity provider.
4. Use the API from the same browser origin; the browser sends the signed
   HTTP-only cookie automatically.

<Note>The browser cookie is not exposed to JavaScript. Browser applications use
same-origin requests instead of copying the cookie into an authorization
header.</Note>

## Option 2: Use an OAuth client and Access Token

### Obtain and configure the client credential

1. Create a confidential OAuth client in the identity provider.
2. Allow the `client_credentials` grant.
3. Grant the scopes required for the integration:
   - Read non-administration APIs: `astrabox:read`
   - Write non-administration APIs: `astrabox:write`
   - Administration APIs: `astrabox:admin`
4. Copy the client ID and secret and set them as environment variables:

```bash
export CLIENT_ID="astrabox-api"
export CLIENT_SECRET="client-secret"
```

<Note>The full client secret is a long-lived credential. Store it in a secrets
manager; never put it in source code or logs.</Note>

### Exchange the client credential for an Access Token

Call the identity provider's token endpoint to exchange the client credential
for an Access Token. With the bundled loopback identity provider:

```bash
ACCESS_TOKEN="$(
  curl --fail --silent --show-error \
    --user "$CLIENT_ID:$CLIENT_SECRET" \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode 'scope=astrabox:read' \
    http://127.0.0.1:8087/api/login/oauth/access_token \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
)"
export ACCESS_TOKEN
```

| Field | Description |
|---|---|
| `grant_type` | Must be `client_credentials` |
| `scope` | A space-separated subset of `astrabox:read`, `astrabox:write`, and `astrabox:admin`; it must not exceed the permissions granted to the OAuth client |

The `access_token` in the response is the Access Token. When it expires, use the
client credential to request another one.

### Use one Access Token for the required API scopes

If a server-side integration needs to read Sessions and manage instance
configuration with the same Access Token, request both `astrabox:read` and
`astrabox:admin`:

```bash
ACCESS_TOKEN="$(
  curl --fail --silent --show-error \
    --user "$CLIENT_ID:$CLIENT_SECRET" \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode 'scope=astrabox:read astrabox:admin' \
    http://127.0.0.1:8087/api/login/oauth/access_token \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
)"
export ACCESS_TOKEN
```

<Warning>
  An Access Token containing `astrabox:admin` can manage instance-level
  configuration. Use it only in trusted server-side environments. Do not
  provide it to end users or untrusted clients. Use separate OAuth clients for
  each environment and grant only the scopes each integration needs.
</Warning>

The same token can call every route allowed by its scopes:

```bash
# Read API
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions" \
  -H "Authorization: Bearer $ACCESS_TOKEN"

# Administration API
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/admin/environments" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### Compatibility with other authentication profiles

- The JWT profile accepts a bearer JWT verified with the configured issuer,
  audience, algorithms, and signing keys.
- The trusted-header profile receives identity from an authentication gateway;
  API clients authenticate with that gateway.
- The local profile requires no credential and is intended for a one-person,
  loopback-only installation.

See [Authentication](team-login.md) for deployment-side configuration.

## Bearer header format

Pass an OAuth Access Token or verified JWT to the AstraBox API as a bearer token:

```text
Authorization: Bearer <access-token>
```

Set the selected token as a common environment variable:

```bash
export ACCESS_TOKEN="access-token"
```

Full request example:

```bash
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/agents" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## Security recommendations

- Use separate OAuth clients or JWT issuers for development, staging, and production.
- Store client secrets and signing keys in a secrets manager instead of hard-coding them.
- Grant each client only the scopes required by the integration.
- Obtain a replacement Access Token before the current one expires, then rotate it safely in the running service.
- Revoke or rotate any leaked client secret, token, or signing key immediately in the identity provider.
