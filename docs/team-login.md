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

## Turn on the bundled login service {#bundled-login-service}

AstraBox ships Casdoor, an open-source identity service, as a Compose overlay,
`containers/compose.sso.yaml`. The overlay runs Casdoor beside AstraBox and
switches the console to OIDC login against it.

On an installation, run the installer again with
`ASTRABOX_INSTALL_TEAM_LOGIN=casdoor` set, as in
[Install a release](deploy.md#turn-on-team-login), or answer yes to its
team-login question. It sets `COMPOSE_FILE` in the installation's
`containers/.env`, so the stack it starts, and every `docker compose` command
run in that directory, includes the overlay. Running it again keeps team login
on and keeps the generated secrets.

From a clone, start the stack with the overlay:

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

Open <http://127.0.0.1:8088> and sign in as `admin`. Its password was generated
once into the deployment's secrets directory: `.astrabox/database-secrets` in
the installation directory, `~/astrabox` by default, or in a clone.

```bash
tr -d '\r\n' < ~/astrabox/.astrabox/database-secrets/casdoor_admin_password
```

The same directory holds the OAuth client secrets. Back it up and protect it.

Casdoor also has an administrator of its own, `built-in/admin`, which manages
every organization in Casdoor. Casdoor creates it with the password `123`
([Casdoor server installation](https://casdoor.ai/docs/basic/server-installation));
at its first start the bundled configuration replaces that password with a
generated one, kept in `casdoor_builtin_admin_password` in the same directory.
Casdoor checks it again at every start and keeps a password an administrator
set in its place. That check signs in with the generated password and then with
`123`, so a password of your own costs two failed sign-ins per start, and
Casdoor locks an account for 15 minutes after five.

Casdoor lets its administrators sign in to every application, so AstraBox
accepts only accounts of the `astrabox` organization, which the bundled
configuration seeds and names in `ASTRABOX_CASDOOR_ORGANIZATION`. Any other
account, `built-in/admin` included, is refused with
`IDENTITY_ORGANIZATION_REJECTED`, and so is a client token from any OAuth
client but the bundled API client, `astrabox-api`. A browser session records the
organization its sign-in checked; one that names no organization, or another,
is signed out and must sign in again.

After the first sign-in:

1. Replace or secure the bootstrap `admin` account of the `astrabox`
   organization.
2. Add users and configure any MFA or external login providers your team needs.
3. Back up the secrets directory with the rest of the deployment data.
4. Test AstraBox with both an administrator and an ordinary user.

Self-signup is disabled in the bundled configuration. Members of the
`astrabox-admin` group receive the AstraBox administrator role. When the overlay
is active, **Management console → Integrated services** provides shortcuts to
Casdoor identity management and API access.

### Brand the sign-in page {#brand-the-sign-in-page}

Casdoor's sign-in page shows the `astrabox-console` application's logo, with
the application's display name as the logo's text alternative. The browser tab
shows the application's title and favicon, or, where those are empty, the
organization's display name and favicon. An
administrator changes them at any time in the Casdoor console, under
**Applications** and **Organizations**.

The application's **Providers** list adds other ways to sign in. Casdoor
supports OAuth providers such as Google, Microsoft Entra ID (Azure AD), Okta,
GitHub, GitLab and Slack, SAML and LDAP, and regional providers such as
DingTalk, Lark (Feishu) and WeCom
([Casdoor OAuth providers](https://casdoor.ai/docs/provider/oauth/overview)).
Create the provider under **Providers**, then add it to the application. The
bundled configuration enables none of them.

Keep the organization's name, `astrabox`: the bundled configuration accepts
accounts of that organization only. A deployment that connects its own Casdoor
with another organization names it in `ASTRABOX_CASDOOR_ORGANIZATION`, as
described below.

AstraBox and Casdoor both listen on this host's loopback address. To sign in
from other computers, see
[Put the login flow behind a proxy](#put-the-login-flow-behind-a-proxy).

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

When the provider is Casdoor, also set `ASTRABOX_CASDOOR_ORGANIZATION` to the
organization whose accounts may sign in. Casdoor lets the administrators of its
`built-in` organization sign in to every application, and AstraBox reads each
account's organization from the `owner` claim of the JWTs Casdoor issues.

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

Conversations created before team login was turned on belong to the local
single-user identity, so accounts that sign in afterwards do not see them.
Agents, Assistants and Environments stay available.

Every signed-in user may create Agents and Assistants. What an Agent's own
fields make its sandbox reach and use is limited to what an administrator made
available: repository deploy keys an administrator listed, Credentials an
administrator assigned, and hosts the Environment allows or that are public
(see [What an Agent's own extensions may reach](adding-tools.md#what-extensions-may-reach)
and [Access GitHub](working-with-repos.md#who-may-use-which-credentials)).
When users should only use the Agents an administrator publishes, as on an open
or public deployment, reserve creation to administrators:

```bash
ASTRABOX_AUTHORING_ADMIN_ONLY=true
```

Everyone else can still use the Agents an administrator makes available to
them, and receives `FORBIDDEN` (403) on create.

Machine integrations use OAuth Access Tokens, verified JWTs, or the credential
accepted by the authentication gateway. See
[Authenticate API requests](api-authentication.md) for token exchange, scopes,
and request headers.

## Put the login flow behind a proxy {#put-the-login-flow-behind-a-proxy}

With the bundled login service, a browser on another computer needs two
addresses: the console, and Casdoor's sign-in page, where the console sends it
to sign in. AstraBox (port 8088) and Casdoor (port 8087) listen on loopback
only, so give each an HTTPS hostname on a reverse proxy on the same host:

| Public URL | Proxied to | Published paths |
|---|---|---|
| `https://astrabox.example.com` | `http://127.0.0.1:8088` | All |
| `https://login.example.com` | `http://127.0.0.1:8087` | Casdoor's sign-in page and its OAuth 2.0 and OIDC endpoints |

Keep both loopback bindings, so that the proxy is the only way in.

### Give AstraBox its public URLs {#give-astrabox-its-public-urls}

On an installation, run the installer with:

```bash
ASTRABOX_INSTALL_TEAM_LOGIN=casdoor
ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com
ASTRABOX_OIDC_ISSUER=https://login.example.com
```

It writes both URLs to `~/astrabox/containers/.env`, and with them the two
settings the server derives from the console's URL:

```bash
ASTRABOX_OIDC_REDIRECT_URL=https://astrabox.example.com/api/v1/auth/callback
ASTRABOX_ALLOWED_HOSTS=astrabox.example.com,localhost,127.0.0.1,[::1]
```

From a clone, set these four in the environment of `scripts/compose.sh`.

| Setting | Why it is needed |
|---|---|
| `ASTRABOX_OIDC_ISSUER` | Casdoor publishes it as its issuer and serves its sign-in page under it. AstraBox checks each ID Token's issuer against it. |
| `ASTRABOX_CONSOLE_ORIGIN` | Casdoor registers the console's callback under it. |
| `ASTRABOX_OIDC_REDIRECT_URL` | The proxy reaches AstraBox over plain HTTP, so AstraBox cannot derive the public HTTPS callback from the request. With this set, AstraBox sends Casdoor the callback Casdoor registered and marks its login cookie Secure. |
| `ASTRABOX_ALLOWED_HOSTS` | AstraBox answers only the Host names it lists, and the list replaces the default one, so it keeps the loopback names the installer and the CLI use. The installer keeps a list you wrote, and refuses one without the console's host and `127.0.0.1`. |

Set the public URLs when you turn team login on. Casdoor registers the
console's callback, `<console URL>/api/v1/auth/callback`, when it first starts,
and later starts add only missing records. If the console's URL changes
afterwards, also change the **Redirect URLs** of the `astrabox-console`
application in Casdoor's console.

### Configure the proxy {#configure-the-proxy}

This nginx configuration publishes the whole console, and only the parts of
Casdoor that browsers and API clients use to sign in:

```nginx
# The console: every path.
server {
    listen 443 ssl;
    server_name astrabox.example.com;
    ssl_certificate     /etc/nginx/tls/astrabox.example.com.crt;
    ssl_certificate_key /etc/nginx/tls/astrabox.example.com.key;
    # Files upload through the console; nginx accepts 1 MB by default.
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # Session events and terminal output arrive as long-lived streams.
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}

# Casdoor: its sign-in page and its OAuth 2.0 and OIDC endpoints only.
server {
    listen 443 ssl;
    server_name login.example.com;
    ssl_certificate     /etc/nginx/tls/login.example.com.crt;
    ssl_certificate_key /etc/nginx/tls/login.example.com.key;

    location ~ ^/(login/oauth/|static/|\.well-known/|api/login/oauth/|api/(login|get-app-login|get-account|userinfo|logout)$) {
        proxy_pass http://127.0.0.1:8087;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Casdoor's management console and the rest of its API.
    location / {
        return 404;
    }
}
```

| Casdoor path | What it serves |
|---|---|
| `/login/oauth/authorize` | The authorization endpoint: the sign-in page the console sends browsers to |
| `/api/login/oauth/access_token`, `/api/login/oauth/refresh_token`, `/api/login/oauth/introspect` | The token, refresh and introspection endpoints, where API clients exchange their credentials |
| `/api/userinfo`, `/api/logout` | The userinfo and end-session endpoints |
| `/.well-known/openid-configuration`, `/.well-known/jwks` | OIDC discovery and the keys that sign ID Tokens |
| `/static/`, `/api/get-app-login`, `/api/get-account`, `/api/login` | The sign-in page's files, and the calls it makes |

Casdoor documents its OAuth 2.0 and OIDC endpoints in
[OAuth 2.0](https://casdoor.ai/docs/how-to-connect/oauth), and its discovery
document lists them for a running server. The sign-in page's calls are those of
`casbin/casdoor:3.128.0`, the version the overlay pins; check them again when
you change that version. The AstraBox server itself reaches Casdoor inside the
deployment, at `http://casdoor:8000`, not through the proxy.

`/api/login` also signs in Casdoor's own administrator, `built-in/admin`, so
keep its generated password as private as the other secrets.

### Manage Casdoor from the AstraBox host {#manage-casdoor-from-the-astrabox-host}

Casdoor's management console stays at <http://127.0.0.1:8087>. From another
computer, reach it through an SSH tunnel:

```bash
ssh -L 8087:127.0.0.1:8087 <astrabox-host>
```

The **Management console → Integrated services** shortcuts open Casdoor at the
issuer, where this proxy answers 404. Point them at the tunnel in
`containers/.env`, then run the installer again:

```bash
ASTRABOX_CASDOOR_ADMIN_URL='http://127.0.0.1:8087'
ASTRABOX_CASDOOR_API_ACCESS_URL='http://127.0.0.1:8087/applications/astrabox/astrabox-api'
```

### Before inviting users

For any team deployment:

- terminate HTTPS at a trusted reverse proxy or ingress;
- add the public hostname to `ASTRABOX_ALLOWED_HOSTS`;
- keep the identity provider's management console private;
- prevent clients from reaching the AstraBox service port directly;
- store OIDC client secrets, gateway secrets, JWT keys, and login-cookie signing
  keys in protected secret storage;
- verify login, logout, expired cookies, ordinary-user authorization,
  administrator access, and provider errors.

## Related guides

- [Deploy AstraBox](deploy.md)
- [API authentication](api-authentication.md)
- [Agent authorization](authoring-agents.md)
