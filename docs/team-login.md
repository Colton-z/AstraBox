# Set up team login

The local AstraBox deployment does not require login. Before making AstraBox
available to a team, connect it to an identity provider or place it behind an
authentication gateway. Authentication establishes who the user is;
authorization determines which AstraBox resources and operations that user can
access.

## Supported login methods

A deployment uses one of these identity methods:

| Method | How it works | Use when |
|---|---|---|
| OIDC | AstraBox redirects the browser to an identity provider and creates a signed login cookie after the user returns | People sign in to the AstraBox console through your identity provider |
| Trusted identity headers | An authentication gateway signs the user in and forwards their identity to AstraBox | The gateway already protects all browser and API traffic |
| Verified JWT | AstraBox verifies a bearer token and reads the configured user and group claims | Existing clients or a gateway already provide signed JWTs |
| Local | AstraBox uses one local administrator identity without login | One-person use on a loopback-only installation |

Do not expose the local method to another network. It provides no
authentication.

With OIDC, AstraBox uses the Authorization Code flow with PKCE, verifies the ID
Token, and returns a signed HttpOnly login cookie.

![How a browser signs in to AstraBox](./img/team-login.svg#inline)

## Try the bundled login service

The maintained SSO overlay runs Casdoor with AstraBox:

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

Open <http://127.0.0.1:8088>. The initial username is `admin`. Read its generated
password from the protected secrets directory:

```bash
tr -d '\r\n' < .astrabox/database-secrets/casdoor_admin_password
```

:::warning Local evaluation only

The launcher generates the administrator password and OAuth client secrets once
under `.astrabox/database-secrets`. Back up and protect this directory. Both
AstraBox and Casdoor listen on loopback in the maintained local configuration;
add TLS and restrict direct access before publishing either service.

:::

After the first sign-in:

1. Open the Casdoor console at <http://127.0.0.1:8087> and replace or secure the
   bootstrap administrator.
2. Add users and configure any MFA or external login providers your team needs.
3. Back up `.astrabox/database-secrets` with the rest of the deployment data.
4. Test AstraBox with both an administrator and an ordinary user.

Self-signup is disabled in the bundled configuration. Members of the
`astrabox-admin` group receive the AstraBox administrator role. When the overlay
is active, **Management console → Integrated services** provides shortcuts to
Casdoor identity management and API access.

## Connect an existing OIDC provider

Set the provider and console client on the AstraBox service:

```bash
ASTRABOX_WEB_IDENTITY=oidc
ASTRABOX_OIDC_ISSUER=https://login.example.com
ASTRABOX_OIDC_CLIENT_ID=astrabox-console
ASTRABOX_OIDC_CLIENT_SECRET=<oidc-client-secret>
```

Register the following callback URL with the provider:

```text
https://<astrabox-host>/api/v1/auth/callback
```

AstraBox reads user groups from the `groups` claim by default and grants the
administrator role to members of `astrabox-admin`. Change the claim and group
name when your provider uses different values:

```bash
ASTRABOX_OIDC_GROUPS_CLAIM=roles
ASTRABOX_OIDC_ADMIN_GROUP=platform-admins
```

Set `ASTRABOX_OIDC_REDIRECT_URL` when the public callback address differs from
the origin AstraBox receives through the proxy. If the AstraBox service reaches
the provider through a private address, keep the public issuer in
`ASTRABOX_OIDC_ISSUER` and put the private address in
`ASTRABOX_OIDC_INTERNAL_ISSUER`.

The browser login cookie is HttpOnly and lasts seven days by default. Change its
lifetime with `ASTRABOX_AUTH_SESSION_TTL_SECONDS`. For multiple AstraBox
replicas, set the same `ASTRABOX_AUTH_SESSION_SECRET` on every replica so a
login remains valid when requests move between them.

## Put AstraBox behind an authentication gateway

### Trusted identity headers

Use trusted headers when a gateway such as an authentication proxy already
handles login. Select the method and configure a shared gateway secret:

```bash
ASTRABOX_WEB_IDENTITY=trusted_header
ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET=<shared-random-value>
```

By default, AstraBox reads the user ID from `x-forwarded-user`, the display name
from `x-forwarded-preferred-username`, the email from `x-forwarded-email`, and
groups from `x-forwarded-groups`. Configure different header names when the
gateway uses another convention.

The gateway must remove client-supplied identity headers, add the authenticated
values itself, and send the configured gateway secret. Block every path that
can reach AstraBox without passing through that gateway.

### Verified JWT

Use verified JWTs when callers or an authentication gateway already attach a
bearer token:

```bash
ASTRABOX_WEB_IDENTITY=jwt
ASTRABOX_JWT_ISSUER=https://login.example.com
ASTRABOX_JWT_AUDIENCE=astrabox
```

AstraBox verifies the signature, expiry, configured issuer and audience, then
maps the user and group claims. You can configure a JWKS URL directly instead
of using issuer discovery. This method does not provide a browser redirect;
the client or gateway must attach the token to each request.

## Authorization after login

A successful login does not automatically make a user an administrator. Group
mapping supplies the administrator role; resource ownership and Agent
authorization are checked separately for each operation. Test both ordinary and
administrator accounts before opening the deployment to a team.

Machine integrations use OAuth Access Tokens, verified JWTs, or the credential
accepted by the authentication gateway. See
[Authenticate API requests](api-authentication.md) for token exchange, scopes,
and request headers.

## Put the login flow behind a proxy

For a team deployment:

- terminate HTTPS at a trusted reverse proxy or ingress;
- forward the original scheme and host so OIDC callback URLs use the public
  HTTPS address;
- add the public hostname to `ASTRABOX_ALLOWED_HOSTS`;
- keep the identity provider's management port private;
- prevent clients from reaching the AstraBox service port directly;
- store OIDC client secrets, gateway secrets, JWT keys, and login-cookie signing
  keys in protected secret storage;
- verify login, logout, expired cookies, ordinary-user authorization,
  administrator access, and provider errors before inviting users.

## Related guides

- [Deploy AstraBox](deploy.md)
- [API authentication](api-authentication.md)
- [Agent authorization](authoring-agents.md)
