# 查看、上传和下载文件

> 在控制台中查看工作区文件、上传上下文并下载结果。

文件 API 让你向 Session 提供文件——代码仓库、配置文件、参考文档等，供 Agent 在执行任务时读取。控制台的**文件**页签展示的是同一个工作区。文件上传后即可通过工作区路径使用，无需创建单独的 File 资源，也无需执行挂载。

## 核心流程

1. <b>打开 Session</b> 用将要使用这些文件的 Agent 启动 Session。
2. <b>上传文件</b> 打开**文件**页签，选择目录，然后上传一个或多个文件，也可以直接把文件拖入面板。
3. <b>Agent 使用文件</b> Session 运行期间 Agent 读取文件内容，完成任务。

## 上传文件

```text
POST /api/v1/sessions/{session_id}/files/upload
Content-Type: multipart/form-data
```

### 参数

| 字段      | 类型       | 必填 | 说明                         |
| --------- | ---------- | ---- | ---------------------------- |
| `files`   | binary[]   | 是   | 一个或多个文件               |
| `path`    | string     | 否   | 目标目录，默认为 Session 根目录 |

### 文件操作

| 操作       | 含义                         |
| ---------- | ---------------------------- |
| 上传       | 向选中的文件夹添加一个或多个文件 |
| 新建文件夹 | 在工作区中创建目录           |
| 重命名     | 重命名或移动文件、目录       |
| 删除       | 删除文件或目录               |
| 下载       | 下载普通文件                 |
| 查看       | 不下载，直接打开文件内容     |

> 任何大小的普通文件都可以下载，下载内容从沙箱流式传输。目录直接在工作区中管理，
> 不会打包下载。

## 上传示例

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/upload" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "files=@./src/main.py"
```

响应：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "root_path": "/workspace",
    "current_path": "/workspace",
    "parent_path": null,
    "entries": [
      {
        "path": "/workspace/main.py",
        "name": "main.py",
        "kind": "file"
      }
    ],
    "uploaded_count": 1
  }
}
```

上传多个文件：

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/upload" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -F "files=@./config.yaml" \
  -F "files=@./requirements.txt"
```

## 在 Session 中使用文件

上传接口本身属于一个 Session，因此上传的文件已经位于该 Session 的工作区中。
AstraBox 的标准 Agent 镜像把工作区展示为 `/workspace`，**文件**页签始终显示实际的
根目录和当前目录。

### Prompt 示例

上传 `app.py` 后，在同一个 Session 中发送任务：

```text
检查 /workspace/app.py 并修复其中的 bug。直接保存修改后的文件，
并把总结写入 /workspace/review.md。
```

Agent 可以立即打开 `app.py`，它创建的文件也会出现在同一个工作区中。

## 管理文件

### 查看文件

在**文件**页签中选择文件名即可打开文件内容。UTF-8 文本和代码使用只读查看器；
Markdown 还提供渲染视图。图片和 PDF 直接在面板中打开。选择**返回文件列表**会回到
同一个目录树。文本预览上限为 2 MiB，图片和 PDF 预览上限为 20 MiB。超过上限或
无法显示的文件仍可完整下载。

HTML 文件有**预览**和**源码**两个页签。预览通过 OpenSandbox 的浏览器端点提供实时的
工作区内容，因此相对路径的 CSS、JavaScript、图片、模块导入和链接都会按文件所在位置
解析。脚本在沙箱化的 frame 中运行。刷新会重新加载页面及其资源；Agent 完成回复后，
查看器也会自动刷新。

HTML 预览需要更新后的内置沙箱镜像，以及浏览器可以访问的 OpenSandbox 端点。镜像会在
8002 端口启动一个独立的 nginx 服务；AIO 网关及其 API key 不会暴露。预览链接是当前
文件根目录的 bearer 凭证，最长 20 分钟后过期。选择**刷新**可以获取新链接。每个资源
请求都会检查签名和有效期，并拒绝符号链接。请勿公开这些链接。

在 Kubernetes 上，请部署官方 OpenSandbox ingress Gateway，并使用 URI 或通配符路由；
Pod 的直接地址不能作为面向用户的预览地址。HTTPS 控制台需要 HTTPS Gateway。按照
[面向浏览器的 Gateway 配置](providers/opensandbox.md#configure-a-browser-facing-kubernetes-gateway)
配置 TLS 和相匹配的 Secure Access 签名密钥。平台的 Lifecycle API 中继设置不会发布
浏览器 URL。

静态查看器支持相对于工作区的 URL。`/assets/main.js` 这类以根路径开头的地址，需要
应用自己的 Web 服务器，并为 OpenSandbox 端点配置好基础路径。服务端渲染的应用应为该
应用服务器使用 `expose_port`。静态预览不会运行远程浏览器，也不会启动应用运行时。

经过认证的预览操作会返回 URL 和过期时间：

```text
POST /api/v1/sessions/{session_id}/files/preview
{"path": "/workspace/report/index.html"}
```

### 下载文件

在**文件**页签中打开文件的操作菜单并选择**下载**，也可以调用：

```bash
curl --get \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/download" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "path=/workspace/review.md" \
  -o review.md
```

工作区中的任何普通文件都可以下载。下载在客户端读取时从沙箱流式传输，文件大小不受
服务端内存限制。

### 查看文件信息

**文件**页签展示当前路径、目录树、文件名、文件类型和大小。这些信息直接来自
Session 当前的工作区，因此 Agent 所做的修改会出现在相同路径下。

### 列出文件

在**文件**页签中展开文件夹即可浏览工作区。Agent 完成回复后，已展开的目录会自动
刷新；也可以随时选择**刷新**重新加载。

对应的 API 操作每次列出一个目录：

```text
POST /api/v1/sessions/{session_id}/files/list
Content-Type: application/json
```

```bash
curl -X POST \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/list" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/workspace"}'
```

## 完整工作流示例

1. 打开 Agent 并启动 Session。
2. 打开**文件**页签，选中工作区根目录并上传 `app.py`。
3. 让 Agent 检查 `app.py`、修复 bug，并把报告保存为 `review.md`。
4. 任务完成后，在**文件**页签中选择 `review.md` 阅读，或使用**下载**保存一份本地
   副本。

## 常见问题

<b>Q：上传的文件存储多久？</b>

A：文件属于 Session 工作区。配置可选的持久工作区存储后，文件可以跨沙箱释放或更换
保留；未配置时，仅存于沙箱的文件会随沙箱删除而丢失。平台数据库单独保存原生对话
状态，恢复对话不等于恢复工作区文件。需要单独长期保存的重要结果，可以下载到本地；
代码修改也可以提交并推送到代码仓库。

<b>Q：能否直接在创建 Session 时附带文件？</b>

A：启动 Session 并等待运行环境就绪后即可上传文件。文件会直接写入工作区，之后
无需再执行挂载。

<b>Q：哪些文件可以下载？</b>

A：Session 工作区中的任何普通文件都可以下载，不限大小；无论文件是用户上传的，
还是 Agent 生成的，规则都相同。

<b>Q：支持哪些文件格式？</b>

A：无格式限制，任意二进制文件均可上传。Agent 对文本类文件（代码、配置、文档）的理解效果最佳。

## 下一步

- [Session](sessions.md) — 先启动 Session，再上传工作区文件。
- [访问 GitHub](working-with-repos.md) — 为 Session 准备代码仓库检出。
- [Assistant](assistants.md) — 在长期使用的个人工作区中使用文件。

## Browser 面板

Browser 面板会在对话独占的沙箱中打开 AIO 桌面。使用 Chrome 地址栏可以访问 URL，
或访问该沙箱内的 `file:///workspace/...` 路径。鼠标和键盘输入会控制沙箱中的
Chrome。**重新连接**会刷新查看器连接，不会重启 Chrome；**在新标签页打开**可以为
桌面提供更大的空间。共享沙箱会显示不支持的提示。

使用 Browser 面板需要以 Session 所有者身份登录。查看器链接是最长 20 分钟有效的
bearer 链接；不要分享给不应控制该沙箱的人。有效期只限制新建的连接，不会断开已经
打开的 WebSocket。Chrome 的状态随沙箱保留；沙箱更换后，面板不会保存或恢复浏览器
配置文件。此功能推出之前创建的沙箱不启用浏览器，需要更换为新沙箱。

**文件**中的 HTML 预览在用户自己的浏览器中渲染静态 HTML，支持共享沙箱和独占沙箱。
Browser 面板适合检查远程 Chrome 本身，或访问在沙箱内运行的应用。
