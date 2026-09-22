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

## 试用内置登录服务

仓库维护的 SSO 叠加配置会和 AstraBox 一起运行 Casdoor：

```bash
scripts/compose.sh -f containers/compose.sso.yaml up -d
```

打开 <http://127.0.0.1:8088>。初始用户名为 `admin`，密码保存在受保护的密钥目录中：

```bash
tr -d '\r\n' < .astrabox/database-secrets/casdoor_admin_password
```

:::warning 仅用于本地评估

启动程序只会在 `.astrabox/database-secrets` 下生成一次管理员密码和 OAuth Client Secret。请备份并保护这个目录。仓库维护的本地配置只在回环地址发布 AstraBox 和 Casdoor；公开任一服务前，请先配置 TLS 并限制直接访问。

:::

第一次登录后：

1. 打开 <http://127.0.0.1:8087> 的 Casdoor 控制台，替换或妥善保护初始管理员。
2. 添加用户，并配置团队需要的 MFA 或外部登录方式。
3. 将 `.astrabox/database-secrets` 与其他部署数据一起备份。
4. 分别使用管理员和普通用户测试 AstraBox。

内置配置关闭了用户自助注册。`astrabox-admin` 组成员会获得 AstraBox 管理员角色。启用该配置后，**管理台 → 集成服务**会提供 Casdoor 身份管理和 API 访问入口。

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

机器集成可以使用 OAuth Access Token、已验证 JWT，或者认证网关接受的凭据。Token 置换、权限范围和请求头见 [API 请求认证](api-authentication.md)。

## 通过代理提供登录

团队部署需要：

- 在可信反向代理或 Ingress 终止 HTTPS；
- 转发原始协议和主机名，让 OIDC 回调使用公网 HTTPS 地址；
- 把公网主机名加入 `ASTRABOX_ALLOWED_HOSTS`；
- 不要公开身份提供商的管理端口；
- 阻止客户端直接访问 AstraBox 服务端口；
- 在受保护的密钥存储中保存 OIDC Client Secret、网关密钥、JWT Key 和登录 Cookie 签名密钥；
- 邀请用户前，验证登录、退出、Cookie 过期、普通用户鉴权、管理员访问和身份提供商错误。

## 相关指南

- [部署 AstraBox](deploy.md)
- [API 认证](api-authentication.md)
- [Agent 鉴权](authoring-agents.md)
