# Protect credentials used by Agents

AstraBox can keep model service, remote MCP, and external API credentials
outside the sandbox. Depending on the authentication method, the Agent uses
an opaque placeholder or a credential-free request. A compatible sandbox's
outbound proxy adds the real credential only to a request that
matches its destination and request rules. The Agent program, terminal, model,
and prompt do not receive the saved value.

![Where a protected credential is added](./img/egress-credential-injection.svg#inline)

## Credentials that can stay outside the sandbox

| Credential | How it is used | Availability |
|---|---|---|
| Model-service credential | Added only to requests for the model gateway selected for the Agent | Agent and Assistant Sessions |
| Remote MCP credential | Added as the configured bearer token, OAuth token, or API-key header for that remote MCP service | Agent and Assistant Sessions |
| HTTP Basic credential | Added to a clean HTTPS URL, including private Git repository requests, without a sandbox-side token or placeholder | Agent Sessions |
| Credential for another API | Exposed to the Agent program as an opaque environment-variable value and substituted in matching HTTP requests | Agent programs that support protected environment credentials |

An Assistant can use saved credentials for remote MCP services. It cannot be
assigned a Credential Vault that contains an external-API environment variable,
because its long-lived workspace cannot safely receive a
conversation-specific placeholder. HTTP Basic Vault assignments are also
unavailable to Assistants. Unsupported assignments are rejected.

## Configure protected credentials in the console

1. Open **Management console → Credentials** and create a credential vault.
2. Select **Add credential**, choose what the credential is for, and enter the
   service address or environment-variable name and the secret value.
3. In **Assigned to**, choose the Agent or Assistant that may use the vault.

People starting conversations do not see or select credential vaults. An
administrator owns the assignment. The vault detail also reports how the
current deployment handles model credentials, remote MCP credentials, and
credentials for other services.

Secrets are encrypted at rest and are write-only after they are saved. See
[Manage service credentials](credentials.md) for creation, rotation, archival,
and assignment behavior.

## How protected delivery works

For placeholder-based credentials:

1. AstraBox resolves the credential and creates a new opaque placeholder for
   the sandbox.
2. The Agent program receives the placeholder while the sandbox's outbound
   proxy receives the real value and its match rules.
3. The proxy checks the request's destination, method, path, and permitted
   substitution location.
4. A matching request receives the real value. A request that does not match
   receives no credential and may also be blocked by the Environment's network
   rules.

The real value is never returned by the credentials API or shown in the
console. Rotated MCP credentials are refreshed before the next top-level Agent
task; the Agent uses its placeholder. Outbound environment-variable and HTTP
Basic credentials are resolved when AstraBox prepares or reconnects the runtime,
not before every task in an already running runtime.

HTTP Basic uses direct header injection instead. Configure the clean target
URL, username, and write-only password or token in the Vault; the sandbox
request contains no credential placeholder. AstraBox installs the credential
rules before Skill and Plugin Git downloads, including prepared capacity.
OpenSandbox supplies the native Basic authentication header when the HTTPS
host and path match. See the [private Git example](credentials.md#2-add-a-credential).

## Destination and request limits

Credentials for another API require one or more allowed hosts. The maintained
OpenSandbox backend accepts fully qualified domain names, optional ports 80 or
443, and a leftmost wildcard such as `*.example.com`. It rejects IP addresses,
single-label hostnames, and other ports.

HTTPS is required by default. Cleartext HTTP is available only on port 80 when
the credential explicitly allows it. A credential can be restricted to request
headers, request bodies, or both; optional method and path rules can narrow it
further.

Protected authentication for a remote MCP service requires a query-free
Streamable HTTP URL on HTTPS port 443, or HTTP port 80 with explicit consent to
cleartext transport. Authenticated SSE is not supported because its POST
destination is supplied dynamically and cannot be restricted in advance.

HTTP Basic requires a clean HTTPS URL on port 443 with a non-root path. It
matches that exact path and its descendants for `GET`, `HEAD`, and `POST`.
For example, `https://github.com/acme/private-skills.git` covers
`/acme/private-skills.git/info/refs`, but not
`/acme/private-skills.git-other/info/refs`. An unmatched request receives no
Basic credential from that binding. Keep any redirected destination within
the configured scope; AstraBox does not forward the credential to a different
host or repository path.

## Network access remains separate

An Environment still controls ordinary outbound access with its `limited` or
`unrestricted` network mode. Assigning a protected credential adds its exact
destination to the effective sandbox rules; it does not change the saved
Environment or allow unrelated destinations. The credential's method and path
rules decide which matching requests receive the secret.

Remote MCP calls and other sandbox traffic use the deployment's network egress.
See [Network access](networking.md) for Environment settings and outbound IP
addresses.

## Deployment requirements

Protected delivery is enabled in the maintained deployment:

```bash
ASTRABOX_SANDBOX_CREDENTIAL_VAULT=true
ASTRABOX_SANDBOX_EGRESS_MODE=dns+nft
```

The sandbox backend must provide the outbound proxy and support protected
credential delivery. The maintained OpenSandbox deployment includes these
parts in the standard create path for both cold and client-pool capacity. They
exist before a Session can claim the sandbox; they are not installed when a
Session first needs a credential.

If protected delivery is enabled but the selected backend cannot provide it,
AstraBox rejects the runtime instead of placing the real credential in the
sandbox.

The Agent program receives a non-secret placeholder. After sandbox creation,
AstraBox writes the real gateway credential and its match rules to the outbound
proxy. The selected Agent program integration scopes that credential to its
model API methods and paths.

Set `ASTRABOX_SANDBOX_CREDENTIAL_VAULT=false` for explicit sandbox-environment
delivery. In that mode, the selected Agent program receives its model or gateway
credential through its process environment.
Saved remote MCP, HTTP Basic, and external API environment credentials are
unavailable in that mode because their values must not enter the workload.

The management console shows the active delivery mode and returns credential
metadata without saved values.

## Configure ordinary network access independently

An Environment states ordinary outbound reachability independently of
credential delivery. `unrestricted` permits all destinations. `limited` permits
its allowlist plus platform-known destinations; it does not duplicate Vault
bindings or inspect Plugin-internal MCP declarations:

```json
{
  "networking": {
    "type": "limited",
    "allowed_hosts": ["registry.npmjs.org", "api.example.com"],
    "allow_mcp_servers": true
  }
}
```

The model endpoint, AstraBox callback host, and declared Plugin Git origins are
derived by the platform. `allow_mcp_servers` admits remote MCP URLs declared
directly on the Agent. A Plugin remains engine-native and opaque; an
administrator lists any destinations used inside it. Requests to every other
host are blocked unless an attached Vault binding explicitly authorizes that
exact destination.

The AstraBox credential plan preserves destination, method, path, transport,
and secure-transport intent. A provider implements that scope exactly or
refuses it before allocation; it may not silently widen a credential.
Assigning that plan also admits its binding hosts in the effective sandbox
policy. This is not a second administrator task: assigning a credential scoped
to `api.example.com` is the authorization to reach `api.example.com`. Method and
path rules still decide where the secret is injected; requests elsewhere on
that host remain credential-free.

Vault assignment leaves the Environment record unchanged. Environment
networking and Vault record lifecycle are independent. Removing the assignment
removes its managed host grant from new sandbox policy.

OpenSandbox egress v1.1.7 supports credential interception on HTTPS 443, or HTTP
80 when the credential explicitly permits insecure transport, and requires
`dns+nft`. It also validates that each runtime binding host is explicitly
reachable. The adapter therefore renders each attached binding host as an exact
allow rule. Under unrestricted networking that rule is redundant; under limited
networking it is the host grant carried by the Vault assignment. The Environment
allowlist needs no duplicate destination.

## Add an outbound credential

An `environment_variable` credential gives an Agent access to a
specific external API while keeping the saved value outside the workload.
Create the credential in an AstraBox Vault:

```http
POST /api/v1/admin/vaults/{vault_id}/credentials
Content-Type: application/json

{
  "display_name": "GitHub API token",
  "auth": {
    "type": "environment_variable",
    "secret_name": "GITHUB_TOKEN",
    "secret_value": "ghp_…",
    "networking": {
      "type": "limited",
      "allowed_hosts": ["api.github.com"]
    },
    "injection_location": {
      "header": true,
      "body": false
    },
    "allowed_requests": {
      "methods": ["GET"],
      "paths": ["/repos/acme/*"]
    }
  }
}
```

Assign the Vault to the Agent:

```http
PUT /api/v1/admin/agents/{agent_id}/credential-vaults
Content-Type: application/json

{"vault_ids": ["<vault_id>"]}
```

The Session receives an opaque value in `GITHUB_TOKEN`. The selected provider
substitutes the saved value when the host, method, path, and injection location
match. Replacing the sandbox also replaces its placeholder.

MCP credential types can be assigned to both Agents and Assistants. The engine
connects directly to the configured MCP URL; AstraBox merges the provider
gateway header and matching Vault header into one provider-neutral,
destination-bound plan. Outbound environment credentials are available to
Agents using a backend that supports protected delivery. The common runtime
preparation gives each assignment its own placeholder context.

## Scope credential substitution

| Field | Accepted scope |
|---|---|
| `networking.type` | `limited` or `unrestricted` |
| `allowed_hosts` | Exact hosts, optional ports, or a leftmost wildcard such as `*.example.com` |
| Destination | HTTPS, or HTTP with `allow_insecure_http: true` |
| `injection_location` | Header, body, or both |
| `allowed_requests.methods` | One or more HTTP methods |
| `allowed_requests.paths` | Absolute patterns such as `/repos/acme/*` |

Omitting `allowed_requests` applies the host scope alone. Updating a credential
rotates its write-only value. Archiving it removes the saved secret and keeps
its metadata; the next root-turn preparation replaces the live MCP binding
with a non-injecting binding for the same destination and Vault scope.

The neutral MCP credential plan preserves the configured URL and transport.
OpenSandbox currently requires a query-free Streamable HTTP endpoint on HTTPS
443 or HTTP 80 for authenticated delivery. Its adapter refuses authenticated
SSE because OpenSandbox cannot pre-bind the dynamic POST URL returned by that
transport; providers that can preserve that scope may support it.
Credential-free SSE remains available.

## Use OpenSandbox prepared capacity

A prewarmed sandbox already exists when AstraBox claims it. The SDK client-pool
creator builds every member through the same standard OpenSandbox create path
as cold capacity, supplying the effective network policy and enabling the
credential proxy before any Session exists. OpenSandbox provisions the proxy,
mints authentication for that sandbox's endpoints, and returns it to the SDK.
The pool retains the sandbox ID; acquisition reconnects through OpenSandbox and
recovers that per-sandbox authentication. There is no deployment-wide egress
token.

The creator supplies the Agent's complete current credential plan to the
egress-side Vault before preparation downloads Skills or Plugins. Model,
environment, MCP and HTTP Basic credentials use the same delivery path as cold
creation; prewarming does not substitute fake secret values. Protected values
remain outside the sandbox environment. Allocation and resume refresh current
bindings through the same plan. The Environment allowlist does not need to
duplicate the Vault binding hosts.

See [OpenSandbox credential protection](providers/opensandbox.md#credential-protection)
for custom deployments and prepared capacity.

## Verify a running sandbox

1. Open the assigned vault under **Management console → Credentials** and check
   the three delivery summaries.
2. Start a Session with the assigned Agent and make one request to an allowed
   destination.
3. Open **Management console → Sandboxes**, select that sandbox, and inspect
   **Network and credentials**. The panel is reported by the sandbox itself and
   shows its network rules and active credential names, never saved values.

If the sandbox cannot report its security settings, the console shows that as
an error instead of treating an empty response as proof that credentials are
protected.

## Related guides

- [Manage service credentials](credentials.md)
- [Network access](networking.md)
- [OpenSandbox](providers/opensandbox.md)
- [Connect a model service](models.md)
