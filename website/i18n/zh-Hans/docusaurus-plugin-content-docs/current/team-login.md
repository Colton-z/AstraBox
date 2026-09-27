# 设置团队登录

AstraBox 本地部署默认不需要登录。向团队开放 AstraBox 前，请连接身份提供商，或者把服务放在认证网关后。认证确认用户是谁；鉴权决定这个用户可以访问哪些 AstraBox 资源、执行哪些操作。

## 支持的登录方式

一个部署使用以下一种身份认证方式：

| 方式 | 工作方式 | 适用场景 |
|---|---|---|
| OIDC | AstraBox 把浏览器重定向到身份提供商，用户返回后创建签名的登录 Cookie | 用户通过组织的身份提供商登录 AstraBox 控制台 |
| 可信身份请求头 | 认证网关完成登录，再把用户身份转发给 AstraBox | 所有浏览器和 API 流量已经由网关保护 |
| 已验证 JWT | AstraBox 验证 Bearer Token，并读取配置的用户与用户组 Claim | 现有客户端或网关已经提供签名 JWT |
| 本地 | AstraBox 不要求登录，使用一个本地管理员身份 | 一个人使用且只监听回环地址的安装 |

不要把本地方式暴露到其他网络，因为它不提供认证。

使用 OIDC 时，AstraBox 执行带 PKCE 的 Authorization Code 流程，验证 ID Token，并返回签名的 HttpOnly 登录 Cookie。

![浏览器如何登录 AstraBox](./img/team-login.svg#inline)

## 启用内置登录服务 {#bundled-login-service}

AstraBox 以 Compose 叠加配置 `containers/compose.sso.yaml` 的形式内置开源身份服务 Casdoor。该配置会在 AstraBox 旁边运行 Casdoor，并把控制台切换为通过它进行 OIDC 登录。

对于用安装脚本安装的部署，设置 `ASTRABOX_INSTALL_TEAM_LOGIN=casdoor` 后再次运行安装脚本（见[安装发布版本](deploy.md#turn-on-team-login)），或在它询问团队登录时回答 yes。安装脚本会在安装目录的 `containers/.env` 中设置 `COMPOSE_FILE`，因此它启动的服务，以及之后在该目录运行的每条 `docker compose` 命令，都会包含这个叠加配置。再次运行安装脚本会保持团队登录开启，并保留已生成的密钥。

从仓库源码运行时，带上叠加配置启动：

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

打开 <http://127.0.0.1:8088>，以 `admin` 登录。它的密码只生成一次，保存在部署的密钥目录中：安装目录（默认 `~/astrabox`）或仓库源码目录下的 `.astrabox/database-secrets`。

```bash
tr -d '\r\n' < ~/astrabox/.astrabox/database-secrets/casdoor_admin_password
```

同一目录还保存 OAuth Client Secret。请备份并保护这个目录。

Casdoor 还有它自己的管理员 `built-in/admin`，用于管理 Casdoor 中的所有组织。Casdoor 创建它时密码为 `123`（见 [Casdoor 服务端安装](https://casdoor.ai/docs/basic/server-installation)）；内置配置会在 Casdoor 首次启动时把这个密码替换为一个生成的密码，保存在同一目录的 `casdoor_builtin_admin_password` 中。Casdoor 每次启动都会再次检查，并保留管理员自行设置的密码。检查时先用生成的密码登录、再用 `123` 登录，因此改用自己的密码后，每次启动会记录两次失败登录；Casdoor 在失败五次后会锁定账号 15 分钟。

Casdoor 允许它的管理员登录所有应用，因此 AstraBox 只接受 `astrabox` 组织的账号；这个组织由内置配置创建，并写在 `ASTRABOX_CASDOOR_ORGANIZATION` 中。其他账号（包括 `built-in/admin`）会被拒绝，错误码为 `IDENTITY_ORGANIZATION_REJECTED`；除内置 API Client `astrabox-api` 之外的其他 OAuth Client 的令牌也会被拒绝。浏览器会话会记录登录时校验过的组织；未记录组织或记录了其他组织的会话会被登出，需要重新登录。

第一次登录后：

1. 替换或妥善保护 `astrabox` 组织中的初始 `admin` 账号。
2. 添加用户，并配置团队需要的 MFA 或外部登录方式。
3. 将密钥目录与其他部署数据一起备份。
4. 分别使用管理员和普通用户测试 AstraBox。

内置配置关闭了用户自助注册。`astrabox-admin` 组成员会获得 AstraBox 管理员角色。启用该配置后，**管理台 → 集成服务**会提供 Casdoor 身份管理和 API 访问入口。

### 设置登录页品牌 {#brand-the-sign-in-page}

Casdoor 登录页显示 `astrabox-console` 应用的 Logo，应用的显示名称作为 Logo 的替代文本。浏览器标签页显示该应用的标题和图标；应用未设置时，显示组织的显示名称和图标。管理员可以随时在 Casdoor 控制台的 **Applications** 和 **Organizations** 中修改它们。

应用的 **Providers** 列表用于添加其他登录方式。Casdoor 支持 Google、Microsoft Entra ID（Azure AD）、Okta、GitHub、GitLab、Slack 等 OAuth 身份提供方，以及 SAML 和 LDAP，也支持钉钉、飞书（Lark）、企业微信等区域性身份提供方（见 [Casdoor OAuth 身份提供方](https://casdoor.ai/docs/provider/oauth/overview)）。先在 **Providers** 中创建身份提供方，再把它添加到应用中。内置配置没有启用其中任何一个。

请保留组织名称 `astrabox`：内置配置只接受这个组织的账号。连接自有 Casdoor 且使用其他组织的部署，需要在 `ASTRABOX_CASDOOR_ORGANIZATION` 中写明该组织，方法见下文。

AstraBox 和 Casdoor 都只监听本机回环地址。需要从其他计算机登录时，见[通过代理提供登录](#put-the-login-flow-behind-a-proxy)。

## 连接已有 OIDC 身份提供商

在 AstraBox 服务上设置身份提供商和控制台 Client：

```bash
ASTRABOX_WEB_IDENTITY=oidc
ASTRABOX_OIDC_ISSUER=https://login.example.com
ASTRABOX_OIDC_CLIENT_ID=astrabox-console
ASTRABOX_OIDC_CLIENT_SECRET=<oidc-client-secret>
```

在身份提供商中登记以下回调地址：

```text
https://<astrabox-host>/api/v1/auth/callback
```

AstraBox 默认从 `groups` Claim 读取用户组，并为 `astrabox-admin` 组成员授予管理员角色。如果身份提供商使用其他值，请修改 Claim 和用户组名称：

```bash
ASTRABOX_OIDC_GROUPS_CLAIM=roles
ASTRABOX_OIDC_ADMIN_GROUP=platform-admins
```

身份提供商是 Casdoor 时，还要把 `ASTRABOX_CASDOOR_ORGANIZATION` 设置为允许登录的组织。Casdoor 允许其 `built-in` 组织的管理员登录所有应用，而 AstraBox 从 Casdoor 签发的 JWT 的 `owner` Claim 中读取账号所属组织。

公网回调地址与 AstraBox 从代理请求中取得的地址不同时，请设置 `ASTRABOX_OIDC_REDIRECT_URL`。如果 AstraBox 服务通过内网地址访问身份提供商，请在 `ASTRABOX_OIDC_ISSUER` 中保留公网 Issuer，并把内网地址写入 `ASTRABOX_OIDC_INTERNAL_ISSUER`。

浏览器登录 Cookie 为 HttpOnly，默认有效期为 7 天，可以通过 `ASTRABOX_AUTH_SESSION_TTL_SECONDS` 修改。运行多个 AstraBox 副本时，请在所有副本中设置相同的 `ASTRABOX_AUTH_SESSION_SECRET`，保证请求切换副本后登录仍然有效。

## 把 AstraBox 放在认证网关后

### 可信身份请求头

如果认证代理等网关已经负责登录，可以使用可信身份请求头。选择该方式，并配置一个网关共享密钥：

```bash
ASTRABOX_WEB_IDENTITY=trusted_header
ASTRABOX_TRUSTED_HEADER_GATEWAY_SECRET=<shared-random-value>
```

AstraBox 默认从 `x-forwarded-user` 读取用户 ID，从 `x-forwarded-preferred-username` 读取显示名称，从 `x-forwarded-email` 读取邮箱，从 `x-forwarded-groups` 读取用户组。网关使用其他约定时，可以配置不同的请求头名称。

网关必须移除客户端提供的身份请求头，再自行添加通过认证的用户信息和所配置的网关密钥。应阻止所有绕过网关直接访问 AstraBox 的路径。

### 已验证 JWT

如果调用方或认证网关已经为请求添加 Bearer Token，可以使用已验证 JWT：

```bash
ASTRABOX_WEB_IDENTITY=jwt
ASTRABOX_JWT_ISSUER=https://login.example.com
ASTRABOX_JWT_AUDIENCE=astrabox
```

AstraBox 会验证签名、过期时间以及配置的 Issuer 和 Audience，再映射用户与用户组 Claim。除了通过 Issuer 发现签名密钥，也可以直接配置 JWKS 地址。这种方式不会提供浏览器重定向；客户端或网关必须为每个请求添加 Token。

## 登录后的鉴权

成功登录不会自动让用户成为管理员。用户组映射提供管理员角色；每项操作还会分别检查资源所有权和 Agent 鉴权。向团队开放前，请同时使用普通用户和管理员账号测试。

开启团队登录之前创建的对话属于本地单用户身份，之后登录的账号看不到它们。Agent、Assistant 和 Environment 不受影响。

每个已登录用户都可以创建 Agent 和 Assistant。Agent 自身字段能让沙箱访问和使用的，仅限管理员提供的内容：管理员列出的仓库 Deploy Key、管理员分配的凭证，以及 Environment 放行或属于公网的主机（参见 [Agent 自身扩展可以访问的范围](adding-tools.md#what-extensions-may-reach)和[访问 GitHub](working-with-repos.md#who-may-use-which-credentials)）。如果用户只应使用管理员发布的 Agent，例如开放或公开的部署，可以只允许管理员创建：

```bash
ASTRABOX_AUTHORING_ADMIN_ONLY=true
```

其他用户仍可使用管理员向他们开放的 Agent，创建时会收到 `FORBIDDEN`（403）。

机器集成可以使用 OAuth Access Token、已验证 JWT，或者认证网关接受的凭据。Token 置换、权限范围和请求头见 [API 请求认证](api-authentication.md)。

## 通过代理提供登录 {#put-the-login-flow-behind-a-proxy}

使用内置登录服务时，其他计算机上的浏览器需要访问两个地址：控制台，以及控制台把它送去登录的 Casdoor 登录页。AstraBox（端口 8088）和 Casdoor（端口 8087）只监听回环地址，因此请在同一台主机的反向代理上为二者各分配一个 HTTPS 主机名：

| 公网 URL | 代理到 | 公开的路径 |
|---|---|---|
| `https://astrabox.example.com` | `http://127.0.0.1:8088` | 全部 |
| `https://login.example.com` | `http://127.0.0.1:8087` | Casdoor 登录页及其 OAuth 2.0 和 OIDC 端点 |

保留两个回环绑定，让代理成为唯一的入口。

### 告诉 AstraBox 它的公网 URL {#give-astrabox-its-public-urls}

对于用安装脚本安装的部署，带上以下设置运行安装脚本：

```bash
ASTRABOX_INSTALL_TEAM_LOGIN=casdoor
ASTRABOX_CONSOLE_ORIGIN=https://astrabox.example.com
ASTRABOX_OIDC_ISSUER=https://login.example.com
```

安装脚本会把这两个 URL 写入 `~/astrabox/containers/.env`，同时写入服务端由控制台 URL 推导出的两项设置：

```bash
ASTRABOX_OIDC_REDIRECT_URL=https://astrabox.example.com/api/v1/auth/callback
ASTRABOX_ALLOWED_HOSTS=astrabox.example.com,localhost,127.0.0.1,[::1]
```

从仓库源码运行时，在 `scripts/compose.sh` 的环境中设置这四项。

| 设置 | 为什么需要 |
|---|---|
| `ASTRABOX_OIDC_ISSUER` | Casdoor 把它作为自己的 Issuer 发布，并在它下面提供登录页。AstraBox 用它校验每个 ID Token 的 Issuer。 |
| `ASTRABOX_CONSOLE_ORIGIN` | Casdoor 在它下面登记控制台的回调地址。 |
| `ASTRABOX_OIDC_REDIRECT_URL` | 代理通过普通 HTTP 访问 AstraBox，AstraBox 无法从请求推导出公网 HTTPS 回调地址。设置后，AstraBox 会把 Casdoor 登记的回调地址发给 Casdoor，并把登录 Cookie 标记为 Secure。 |
| `ASTRABOX_ALLOWED_HOSTS` | AstraBox 只响应列表中的 Host。该列表会替换默认列表，因此其中要保留安装脚本和 CLI 使用的回环名称。安装脚本会保留你写入的列表；列表缺少控制台主机名或 `127.0.0.1` 时，安装脚本会拒绝继续。 |

请在启用团队登录时就设置公网 URL。Casdoor 在首次启动时登记控制台的回调地址 `<控制台 URL>/api/v1/auth/callback`，之后的启动只补充缺失的记录。如果之后控制台 URL 发生变化，还要在 Casdoor 控制台中修改 `astrabox-console` 应用的 **Redirect URLs**。

### 配置代理 {#configure-the-proxy}

下面的 nginx 配置公开整个控制台，而 Casdoor 只公开浏览器和 API 客户端登录所需的部分：

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

| Casdoor 路径 | 提供的内容 |
|---|---|
| `/login/oauth/authorize` | 授权端点：控制台把浏览器送去的登录页 |
| `/api/login/oauth/access_token`、`/api/login/oauth/refresh_token`、`/api/login/oauth/introspect` | Token、刷新和内省端点，API 客户端在这里换取凭据 |
| `/api/userinfo`、`/api/logout` | Userinfo 和结束会话端点 |
| `/.well-known/openid-configuration`、`/.well-known/jwks` | OIDC 发现文档和签发 ID Token 的密钥 |
| `/static/`、`/api/get-app-login`、`/api/get-account`、`/api/login` | 登录页的文件及其发出的请求 |

Casdoor 在 [OAuth 2.0](https://casdoor.ai/docs/how-to-connect/oauth) 中说明了它的 OAuth 2.0 和 OIDC 端点，运行中的服务也会在发现文档中列出这些端点。登录页发出的请求来自叠加配置固定的版本 `casbin/casdoor:3.128.0`；更换版本时请重新核对。AstraBox 服务端本身在部署内部通过 `http://casdoor:8000` 访问 Casdoor，不经过代理。

`/api/login` 同样可以登录 Casdoor 自己的管理员 `built-in/admin`，因此请像保护其他密钥一样保护它生成的密码。

### 在 AstraBox 主机上管理 Casdoor {#manage-casdoor-from-the-astrabox-host}

Casdoor 的管理控制台仍在 <http://127.0.0.1:8087>。从其他计算机访问时，使用 SSH 隧道：

```bash
ssh -L 8087:127.0.0.1:8087 <astrabox-host>
```

**管理台 → 集成服务**中的入口默认在 Issuer 下打开 Casdoor，而上面的代理会在那里返回 404。在 `containers/.env` 中把它们指向隧道地址，再运行一次安装脚本：

```bash
ASTRABOX_CASDOOR_ADMIN_URL='http://127.0.0.1:8087'
ASTRABOX_CASDOOR_API_ACCESS_URL='http://127.0.0.1:8087/applications/astrabox/astrabox-api'
```

### 邀请用户之前

所有团队部署都需要：

- 在可信反向代理或 Ingress 终止 HTTPS；
- 把公网主机名加入 `ASTRABOX_ALLOWED_HOSTS`；
- 不要公开身份提供商的管理控制台；
- 阻止客户端直接访问 AstraBox 服务端口；
- 在受保护的密钥存储中保存 OIDC Client Secret、网关密钥、JWT Key 和登录 Cookie 签名密钥；
- 验证登录、退出、Cookie 过期、普通用户鉴权、管理员访问和身份提供商错误。

## 相关指南

- [部署 AstraBox](deploy.md)
- [API 认证](api-authentication.md)
- [Agent 鉴权](authoring-agents.md)
