# 从云镜像运行 AstraBox

> 用 AstraBox 镜像在云主机上启动 AstraBox，首次开机时自动配置团队登录和 HTTPS。

AstraBox 云镜像基于 Ubuntu 24.04，预装了 Docker、一个 AstraBox 版本以及该版本运行所需的
全部容器镜像。由它启动的机器会在首次开机时用自己生成的密钥安装 AstraBox，你不需要运行安装
脚本，也不需要回答它的问题：

1. 读取 `/etc/astrabox/cloud.env` 中的可选设置，例如模型服务。可以在启动机器时通过 user
   data 写入。
2. 查找机器的公网 IPv4 地址。
3. 从镜像安装 AstraBox，并开启[团队登录](team-login.md)，控制台不会在无登录的状态下对外
   提供。
4. 在 AstraBox 前面启动 [Caddy](https://caddyserver.com/)：控制台位于 `https://<公网 IP>`，
   登录页位于 `https://<公网 IP>:8443`，证书是 Let's Encrypt 为该 IP 地址签发的。
5. 把登录信息写入 `/root/astrabox-credentials.txt`。

首次开机不会从镜像仓库拉取任何镜像，也不需要访问 GitHub，几分钟即可完成。

## 要求

- **机器规格**：至少 2 vCPU、8 GiB 内存。没有对话运行时，AstraBox 及其服务约占 2 GiB；
  每个 Agent 预留一个就绪沙箱，各约 0.5 GiB；每个对话在自己的沙箱中运行，最多 1 vCPU、
  1 GiB。容量规划见[部署 AstraBox](deploy.md)。
- **磁盘**：至少 60 GiB。镜像解压后约 13 GiB，对话工作区和数据库会在此基础上增长。
- **入站端口**：从互联网开放 80、443 和 8443。Let's Encrypt 通过 80 端口验证地址。只有
  SSH 需要 22 端口。
- **不会变化的公网 IPv4 地址**，例如弹性公网 IP。见[地址变化时](#if-the-address-changes)。

## 启动机器

从云市场中的 AstraBox 镜像，或从你自己构建的镜像（见[构建镜像](#build-the-image)）启动机器，
并在安全组或防火墙中开放上面的端口。

所有设置都是可选的。如需设置，请通过 user data 写入 `/etc/astrabox/cloud.env`。例如，下面的
配置接入 DeepSeek：

```yaml
#cloud-config
write_files:
  - path: /etc/astrabox/cloud.env
    permissions: '0600'
    content: |
      ASTRABOX_INSTALL_MODEL_PROVIDER=deepseek
      ASTRABOX_INSTALL_MODEL_API_KEY=your-deepseek-api-key
```

| 设置 | 含义 |
|---|---|
| `ASTRABOX_INSTALL_MODEL_PROVIDER` | `anthropic`、`deepseek`、`anthropic-compatible`、`openai-compatible` 或 `none`（默认）。为 `none` 时，之后在控制台中添加模型服务。 |
| `ASTRABOX_INSTALL_MODEL_API_KEY` | 模型服务的 API Key。首次开机会把它移入只有 root 可读的安装设置，并从 `cloud.env` 中删除。 |
| `ASTRABOX_INSTALL_MODEL_NAME` | 默认 Agent 使用的模型 ID。DeepSeek 默认为 `deepseek-flash`。 |
| `ASTRABOX_INSTALL_MODEL_BASE_URL` | 兼容 Anthropic 或 OpenAI 协议的服务的 Base URL。 |
| `ASTRABOX_CONSOLE_DOMAIN`、`ASTRABOX_LOGIN_DOMAIN` | 两个 DNS 解析指向这台机器的主机名，一个用于控制台，一个用于登录页。两个都设置或都不设置。设置后控制台位于 `https://<控制台域名>`，不再使用 8443 端口。 |
| `ASTRABOX_PUBLIC_ADDRESS` | 浏览器访问用的 IPv4 地址，在机器无法自行找到公网地址时设置。 |
| `ASTRABOX_TLS` | `acme`（默认）：使用 Let's Encrypt 证书，失败时回退到自签名证书。`internal`：只使用自签名证书。 |
| `ASTRABOX_ACME_EMAIL` | Let's Encrypt 发送证书通知的邮箱。 |

user data 会随机器配置保存在你的云账号中，能读取该配置的人也能看到其中的 API Key。如果不想
这样，请不设置模型，登录后在控制台中添加模型服务。

## 登录 {#sign-in}

首次开机完成后，登录这台机器时的欢迎信息会显示控制台地址。查看登录信息：

```bash
sudo astrabox-cloud info
```

打开控制台地址，用 `admin` 和显示的密码登录。要邀请其他人，请按同一输出中的说明在 Casdoor 的
管理控制台中为他们创建账号。Casdoor 管理控制台只能在这台机器上通过 SSH 隧道访问。

如果没有设置模型服务，请打开 **控制台 > 集成服务 > LiteLLM 网关** 添加一个，见
[接入模型](models.md)。

查看首次开机进度，或查找失败原因：

```bash
sudo astrabox-cloud status
sudo astrabox-cloud logs
```

排除失败原因后，运行 `sudo systemctl restart astrabox-first-boot` 重试，之前生成的内容都会保留。

## 证书

Let's Encrypt 为 IP 地址签发的证书有效期只有约 6 天，Caddy 会在到期前自动续期。签发时
Let's Encrypt 必须能通过 80 端口访问这台机器。

如果 Let's Encrypt 无法验证地址（例如 80 端口未开放），Caddy 会改用自签名证书，浏览器会显示
警告。开放端口后运行 `sudo docker restart astrabox-edge` 重试。

在中国内地地域，云厂商会拦截访问未备案域名的 HTTP 和 HTTPS 请求，直接访问 IP 地址不受影响。
只有已备案的域名才能在这些地域使用 `ASTRABOX_CONSOLE_DOMAIN` 和 `ASTRABOX_LOGIN_DOMAIN`。

## 地址变化时 {#if-the-address-changes}

首次开机会把控制台地址注册到 Casdoor，作为登录后浏览器返回的地址。如果公网 IP 变化（例如没有
弹性公网 IP 的机器停止后再启动），登录就会失败。邀请用户前，请为机器分配固定地址。

把 AstraBox 迁移到新地址或域名：

1. 在 Casdoor 管理控制台中打开 **Applications > astrabox-console**，在 **Redirect URLs**
   中添加新的回调地址 `https://<新控制台地址>/api/v1/auth/callback`。
2. 在 `/etc/astrabox/cloud.env` 中设置新地址（`ASTRABOX_PUBLIC_ADDRESS`，或两个域名设置）。
3. 重新运行首次开机：

   ```bash
   sudo rm /var/lib/astrabox-cloud/first-boot.done
   sudo systemctl restart astrabox-first-boot
   ```

## 升级

对首次开机创建的安装运行 main 分支的安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/Colton-z/AstraBox/main/scripts/install.sh \
  | sudo ASTRABOX_INSTALL_DIR=/opt/astrabox bash
```

升级会保留密钥、数据库、团队登录和公网地址，HTTPS 边缘不变。

## 构建镜像 {#build-the-image}

镜像由仓库中的两个脚本构建：`cloud/image/build.sh` 和 `cloud/image/cleanup.sh`。Packer
模板 `cloud/packer/astrabox.pkr.hcl` 在 AWS、DigitalOcean、阿里云、腾讯云和华为云上运行它们。
在其他云上，启动一台 Ubuntu 24.04 机器，以 root 运行这两个脚本，再从停止的机器创建镜像。
`cloud/README.md` 介绍构建设置，以及如何在各云市场发布镜像。

## 相关指南

- [部署 AstraBox](deploy.md)
- [设置团队登录](team-login.md)
- [接入模型](models.md)
