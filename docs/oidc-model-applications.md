# Independent OIDC applications on the model gateway

A deployment can trust desktop applications and CLI tools independently of the
AstraBox console and API clients. A trusted application sends its employee's
Access Token directly to the bundled LiteLLM inference endpoints. No Agent or
AstraBox membership is created, and the client receives neither an AstraBox
client secret nor a gateway Virtual Key.

This opt-in integration uses LiteLLM's open-source Custom Auth extension. It
requires JWT access tokens, discovery, asymmetric JWKS, authenticated
introspection and UserInfo. Opaque access tokens are not supported by this
adapter. LiteLLM Enterprise JWT authentication is not required. Other OIDC
providers must implement those contracts; Casdoor is the initial integration.

## Configure trust

Create an independent public OAuth application in the identity provider. Register
an exact loopback redirect such as `http://127.0.0.1:43127/callback`, enable
Authorization Code with PKCE S256 and refresh tokens, and permit the required
scopes. Keep the console/API clients unchanged. The desktop never receives the
application's server-side introspection secret.

Mount a directory containing the trust policy and each introspection secret
read-only into the **AstraBox server container**. Set
`ASTRABOX_MODEL_GATEWAY_OIDC_CONFIG_FILE` to the policy's container path. For
example, add a Compose override:

```yaml
services:
  server:
    environment:
      ASTRABOX_MODEL_GATEWAY_OIDC_CONFIG_FILE: /run/model-oidc/policy.yaml
    volumes:
      - ./model-oidc:/run/model-oidc:ro
```

A policy for one Casdoor application is:

```yaml
jwks_cache_seconds: 300
providers:
  - kind: casdoor
    issuer: https://login.example.com
    internal_issuer: http://casdoor:8000
    algorithms: [RS256]
    applications:
      - id: desktop-assistant
        enabled: true
        client_id: desktop-assistant
        client_id_claim: azp
        audiences: [desktop-assistant]
        organization_claim: owner
        organizations: [employees]
        introspection_client_id: desktop-assistant
        introspection_secret_file: /run/model-oidc/desktop.secret
        app_user_id: oidc-app-desktop
        team_id: engineering
        models: [company-chat]
        required_scopes: [openid, profile]
        subject_models:
          employee-stable-provider-uuid: [company-chat]
        subject_teams:
          employee-stable-provider-uuid: engineering
```

Only `issuer`, `applications` and application fields without defaults are
required. Omit `internal_issuer` to use the public provider endpoints. Public
URLs require HTTPS; loopback HTTP is permitted for local development. Internal
rewriting applies only to endpoints below the configured issuer. An issuer may
have multiple clients, and the policy may contain multiple issuers. Application
IDs and native application users must be distinct. A console/API client cannot
also be an independent inference application. When sharing the platform issuer,
the platform must already use client-bound API introspection (`ASTRABOX_OIDC_API_CLIENT_ID`
and its server-side secret). UserInfo-only platform bearer authentication cannot
guarantee separation between clients and is refused for this shared-issuer setup.

Mount the directory rather than a single file when replacing policy files by
atomic rename. New application entries, `enabled: false`, removals, model grants
and organization restrictions apply to the **next newly authenticated request**;
there is no last-good policy fallback. Invalid edits reject independent OIDC
requests with 503. An already accepted SSE request continues under the policy
that admitted it. Changing the policy file path requires a server restart.

The file and introspection secrets are server-only. Restrict host permissions to
the account running the gateway. Never put them in a desktop bundle. For an
external gateway, configure that gateway itself; AstraBox refuses this setting
when its bundled gateway is disabled.

## Authenticate the employee

Each new request verifies signature, configured algorithm, exact issuer,
audience, expiry, issue time and client identity. Unverified fields select a
configured policy only. They never grant access. Introspection must confirm an
active **access-token record**, the same subject and the configured client.
UserInfo must return the same subject. No active-token or account-status results
are cached. Discovery is read on each request; signing keys have the configured
bounded cache (1–3600 seconds). Provider timeouts fail closed with 503.

For `kind: casdoor`, the adapter also reads the current account through Casdoor's
application-authenticated `GET /api/get-user?owner=…&userId=…`. Its UUID and
organization must match, and `isForbidden` and `isDeleted` must both be false.
The configured introspection client needs permission for this account read.
Revocation, deletion and account disable apply to the next request once Casdoor
has committed the change. Neither an old JWT profile nor UserInfo alone is used
as proof that a Casdoor account is active. Standard providers (`kind: oidc`)
must make account disable visible through introspection/UserInfo; their own
consistency delay determines account-disable enforcement. Do not promise an
immediate disable policy for an IdP that leaves disabled users' tokens active.

Casdoor 3.128.0 returns the same bytes as `access_token` and `id_token`. Identical
values cannot be distinguished by purpose. The access-token database lookup is
therefore mandatory: an arbitrary JWT signed with the same certificate is not
sufficient. Casdoor's OAuth `resource` parameter replaces the JWT audience,
including that shared ID-token value. For ordinary OIDC clients that verify the
ID-token audience, keep the default client-ID audience as above; do not set a
separate resource audience and then disable ID-token audience validation.

IdP administrator roles and groups grant **no** gateway administrator or AstraBox
permissions to these clients. Only GET model listings and POST Chat Completions
and Messages are admitted. Key creation, model administration, MCP and platform
APIs remain outside this grant.

## Grant models and govern spend

Provision the existing LiteLLM objects with its administrator UI or APIs; the
adapter does not silently create grants or issue client keys:

| Dimension | Native LiteLLM object | Identity |
|---|---|---|
| Application | Internal User, non-administrator | Policy `app_user_id` |
| Employee | Customer / End User | `oidc:` + SHA-256 of compact UTF-8 JSON `[issuer, sub]` |
| Team | Team | Policy default or `subject_teams[sub]` |

For example, provision `/user/new` with `user_id: oidc-app-desktop`,
`user_role: internal_user`, `auto_create_key: false` and native model/budget/RPM/
TPM settings. Provision each employee with `/customer/new`, using the stable
customer ID as `user_id` and its native budget and rate fields. Provision the
optional team with `/team/new`. Use the existing update endpoints to change
native limits. These operations require a gateway administrator credential and
are never performed by the desktop. Customer identity survives renamed email,
renamed login and token refresh. The same employee has one customer identity
across trusted applications; distinct issuers never share one implicitly.

The policy requires explicit deployed route names. Employee overrides narrow
those names. Native application-user and team lists further narrow access;
configure exact route names or LiteLLM's `all-proxy-models` sentinel for those
objects. Team wildcard/access-group expansion is not part of this adapter's
policy contract. The model list uses the resulting grants. Native model and
fallback checks run after Custom Auth; an application cannot request proxy
routing, credentials, mock responses or arbitrary fallback parameters through
this standard-protocol entry point. Inference parameters come from the pinned
OpenAI SDK and LiteLLM Messages protocol types. The employee accounting identity is fixed
by verified authentication rather than caller-controlled `user` data.

The adapter invokes the pinned LiteLLM native model checks, common budget
checks and budget reservation for independent application credentials only.
Application-user budgets also apply when that request has a team. It does not
enable global Custom Auth switches or change AstraBox's existing authenticators.
The proxy's native rate limiter, spend updater and audit log own enforcement. Configure
its database and shared Redis for multi-replica limits. Native grant caches and
asynchronous spend updates retain LiteLLM's documented consistency; budgets are
not a promise of zero concurrent-request overspend.

Each granted route must have native pricing, including explicit pricing for
custom models. Unpriced routes are refused with 503. Usage is attributed to the
native application user, employee customer and team, plus an opaque per-app/
employee fingerprint and non-secret `oidc_*` audit metadata. A fingerprint is
not a usable Virtual Key. Do not enable raw Authorization-header logging at a
reverse proxy or identity provider.

## Publish only model routes

Publish HTTPS with the reverse proxy and certificate mechanism your deployment
already uses. On a shared Nginx virtual host, add **exact** locations for the
model endpoints to its existing AstraBox configuration:

```nginx
location = /v1/models {
    proxy_pass http://127.0.0.1:8089;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
}
location = /v1/chat/completions {
    proxy_pass http://127.0.0.1:8089;
    proxy_set_header Authorization $http_authorization;
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
location = /v1/messages {
    proxy_pass http://127.0.0.1:8089;
    proxy_set_header Authorization $http_authorization;
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
```

The destination above must be the **gateway HTTP listener**, not AstraBox's
business API listener. In the bundled container, the gateway listener is port
80 and the business API listener is 8000. Publish the gateway container port to
a separate loopback host port (for example `127.0.0.1:8089:80`) and use that host
port in these `proxy_pass` values. Keep the existing console's port 8088 for its
existing routes. Do not replace the existing `/`, `/api/v1/*`, login callback,
management or MCP locations, and do not publish an unrestricted `/v1/*` catchall.
For an AstraBox path-prefix deployment, these model endpoints remain the paths
you explicitly publish; do not prepend the console prefix automatically.

Clients set `MODEL_BASE_URL=https://models.example.com/v1`. Chat Completions
supports ordinary and SSE responses. `/v1/messages` uses LiteLLM's native
Messages route and requires a compatible configured model; it does not promise
every provider-specific feature. The independent grant does not include Files
uploads, so DSH catalog routes needing Files for images require a separately
designed capability. Text Messages do not require that upload route.

| Response | Meaning and desktop action |
|---|---|
| 401 | Invalid, expired, inactive or revoked credential / inactive account; refresh once through the IdP, then require sign-in if rejected |
| 403 | Authenticated but client, organization, model, route or parameter is not granted; changing tokens cannot add permission |
| 429 | Native rate limit; honor Retry-After when supplied |
| Native budget error | LiteLLM `budget_exceeded` error; show the limit and request an administrator change, do not refresh-loop |
| 503 | Policy, provider, signing keys, account read, grant database or required pricing unavailable; surface service failure |

LiteLLM also rejects banned upstream-routing parameters before Custom Auth
runs. In the pinned version an `api_base` override returns its native 401
`Rejected Request` error. Remove the forbidden field; refreshing a valid token
does not fix that request. The application adapter does not change this native
pre-authentication behavior.

## Desktop integration and updates

[`examples/oidc-dsh`](../examples/oidc-dsh/README.md) pins the original reported
DSH SDK release and demonstrates public-client PKCE plus a running-process
credential reload barrier. Keep refresh tokens in the OS credential store in
the desktop main process. Gate model submissions while replacing credentials;
do not send tokens to the renderer, command-line arguments or diagnostic logs.

Upgrade the AstraBox image, mount the new files, provision native grants and
restart the server. Roll back by disabling/removing the policy setting and
returning to the previous image; retain the native accounting records. Existing
Virtual Keys and platform clients retain their own authenticators. Configuration
and source review alone are not live acceptance; record the actual source,
provider version, protocols, limits and desktop platform verified at deployment.
