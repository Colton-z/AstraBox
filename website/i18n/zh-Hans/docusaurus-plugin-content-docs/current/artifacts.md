# Artifacts

Artifact 是 Agent 或 Assistant 发布的独立页面、应用、图表、文档或代码视图。
内容、版本、数据和分享设置都保存在 AstraBox 数据库中。Session 结束或沙箱被销毁后，
仍可打开 Artifact；不需要持久化工作区卷。

Artifact 默认私有，属于创建会话所代表的用户。管理员不会因此获得其他用户的 Artifact 访问权。

## 创建 Artifact {#create-an-artifact}

让 Agent 或 Assistant 将可重复使用的完整输出发布为 Artifact，例如：

> 创建一个标题为“项目清单”的 HTML Artifact，包含可在浏览器中使用的任务清单。
> 请使用 artifacts 工具发布。

Agent 和 Assistant 会话可使用内置 `artifacts` 工具；Agent 关闭了**作品**开关，或 Session 由消息渠道启动时，不提供这些工具。
`create_artifact` 接受标题、类型，
以及内容或工作区 `file_path`。平台在发布时复制文件；之后打开页面不依赖该文件或沙箱。

如果不希望某个 Agent 发布 Artifact，请在该 Agent 的「MCP、Skill 与 Plugin」设置中关闭**作品**开关，或在创建、更新时传入 `"artifacts_enabled": false`。创建时省略此字段的 Agent 提供这些工具。
关闭后，下一个对话启动时就没有这些工具，使用已准备沙箱的对话也一样；该 Agent 现有对话对这些工具的调用会被拒绝。
该 Agent 已发布的 Artifact 仍可查看，分享链接保持有效。

| 类型 | 展示内容 |
| --- | --- |
| `html` | 可执行脚本的 HTML 页面 |
| `react` | 带默认导出的 React 组件，支持 JSX 和 TypeScript |
| `svg` | 矢量图 |
| `markdown` | 控制台渲染的文档，不执行页面脚本 |
| `mermaid` | 图表 |
| `code` | 按指定 `language` 展示源码，不执行代码 |

React 页面可导入 `react`、`react-dom`、`lucide-react`、`recharts`、`lodash`、`d3`、
`papaparse`、`mathjs`、`three`、`xlsx`，以及受支持的 `@/components/ui/*` 控件：
button、card、input、label、textarea、select、checkbox、switch、tabs、badge、alert、
separator、progress、slider 和 dialog。支持 Tailwind。这些运行库由 AstraBox 提供；
未知导入会显示错误和可用模块列表。

## 打开和使用面板 {#open-and-use-the-panel}

发布后，会话中会出现带标题、类型和版本的 Artifact 卡片，位于折叠的工具过程之外。
新 Artifact 会打开会话旁的 **Artifacts** 页签；点击卡片的**打开**可再次查看。
Agent 面板列出该 Session 发布的 Artifact；Assistant 面板列出该 Assistant 的 Artifact。

**完整页面**提供更大的查看空间，**复制源码**复制正在查看的版本，**下载源码**保存它。
导航中的 **Artifacts** 打开 `/artifacts` 图库；完整页面地址为 `/artifacts/<id>`。
完整页面提供来源会话或 Assistant 链接以及删除操作。删除会同时移除所有版本、数据和分享链接。

页面错误显示在查看器上方。在会话面板中，**请 Agent 修复**会准备一条包含 Artifact id、
版本和错误的消息。检查消息并发送，才能请求修复。

## 版本和后续更新 {#versions-and-later-updates}

每次内容发布都会创建不可变的新版本。可让 Agent 使用 `update_artifact` 重写内容或执行精确字符串替换。
匹配不存在或不唯一时，替换失败且不会发布；过期的 `base_version` 会触发冲突。
存在多个版本时，通过**版本历史**切换。

跟随最新版本的查看器会在发布后自动刷新。查看历史版本时不授予运行时能力。
通过 API 恢复旧版本会创建新版本，不会改写历史。Artifact 数据独立于版本内容，更新内容时保留。

同一用户在同一 Agent 或 Assistant 中的后续会话可通过 `list_artifacts`、`get_artifact` 和
`update_artifact` 查找并更新同一页面。该 Agent 的定时 Deployment 也可这样做。
这些工具不能访问其他 Agent 或 Assistant 来源的 Artifact。
定时、Webhook 和手动运行提供这些工具，除非 Agent 关闭了**作品**开关；消息渠道启动的 Session 不提供。

## 分享 {#sharing}

打开**分享 Artifact**，选择访问级别、受众、版本和可选的过期时间，然后**保存**。
复制显示的链接。**撤销链接**会关闭访问；已撤销、过期或已删除 Artifact 的链接无法打开。

| 设置 | 含义 |
| --- | --- |
| **查看** | 读取内容，使用当前能力声明允许 View 级别使用的能力 |
| **交互** | 已登录用户可在能力声明允许时写数据和发送房间事件 |
| **持链接的任何人** | 匿名用户始终只有查看权限，即使链接设为交互 |
| **仅已登录用户** | 必须先登录才能使用链接 |
| **跟随最新版本** | 分享页面随后续发布更新 |
| **固定版本** | 固定页面内容；能力、数据规则和房间主题使用当前版本的声明，页面不能自行发布新版本 |

只有所有者可发布新版本、改变分享设置或删除 Artifact。交互链接不会转移所有权。
分享页面显示“内容由用户生成，未经验证”；匿名访问者看不到作者身份。

当前版本声明 `sample` 或 `agent` 时，访问者必须登录，固定版本链接也一样。
选择**持链接的任何人**会返回 `ARTIFACT_SHARE_AUDIENCE_RESTRICTED`。
给已分享的 Artifact 添加任一能力后，匿名访问者在后续运行时请求中必须登录
（`ARTIFACT_SIGN_IN_REQUIRED`）。分享对话框使用当前策略，即使正在查看历史版本。

模型请求和 Agent 运行还要求访问者有权使用来源 Agent，Artifact 所有者也需通过此鉴权。
Assistant Artifact 的这两项能力仅供所有者使用。
有来源 Agent 使用权的已登录 View 访问者可使用 `sample`；`agent` 要求 Interact 或 Owner 权限。

## 实时页面能力 {#live-page-capabilities}

在 `create_artifact` 或 `update_artifact` 中声明能力，例如：
`{"db": {}, "user": {}, "room": {"topics": {"ping": "interact"}}}`。
更新时省略 `capabilities` 会保留声明，传入 `{}` 会清空声明。
页面通过 `await window.astrabox.use(name)` 获取命名空间或 `null`；使用前应检查。
声明本身不等于授权，访问级别和当前能力声明决定每次调用能否执行。
固定版本保留选定内容，能力、数据规则和房间主题使用当前策略。历史视图没有运行时能力。
重新加载固定版本页面可获得新声明的能力；已有 frame 保持打开时，服务端操作也会重新检查当前策略。

下面的示例放在异步页面脚本中，并假定相应能力已获授权。`permissions` 为内置能力，无需声明。

### 自行发布：`artifact` {#republish-artifact}

声明 `{"artifact": {}}`。仅所有者可发布。正在查看的版本是基础版本，同时修改可能返回 `conflict`。
固定版本的分享视图不授予此能力，所有者打开链接也一样。

```js
const artifact = await window.astrabox.use('artifact');
if (artifact) await artifact.publish('<html><body>Updated page</body></html>');
```

### 存储数据：`db` {#store-data-db}

声明 `{"db": {}}`。文档路径段数为偶数，例如 `live/status`；集合路径段数为奇数，例如 `notes`。
支持 `doc`、`collection`、`get`、`set`、`update`、`delete`、`add`、`where`、`orderBy`、
`limit` 和实时 `onSnapshot` 订阅。

```js
const db = await window.astrabox.use('db');
if (db) {
  db.collection('notes').onSnapshot(snapshot => {
    document.querySelector('#notes').textContent =
      snapshot.docs.map(doc => doc.data().text).join('\n');
  });
  await db.collection('notes').add({text: 'A shared note'});
}
```

默认读权限为 View，写权限为 Interact。能力声明可通过 `rules` 指定 `path`、`read` 和 `write` 级别。
更深的前缀规则覆盖继承的级别；写权限不得低于读权限。
`data/users/{self}` 子树默认仅供当前访问者使用，所有者也不能读取其他访问者的私有数据。
`{self}` 替换为该用户在此 Artifact 中的不透明 id。
例如文档可放在 `data/users/{self}/profile`。匿名用户没有私有 `{self}` 路径且不能写数据。
显式的 `{self}` 规则也会创建私有子树。
在 `data/users` 等私有前缀上同时设置 `read` 和 `write` 的规则可开放该前缀。
最多声明 64 条规则；`{self}` 只能位于规则路径的最后一段。
设置 `write: 'view'` 的规则允许已登录 View 访问者写入；匿名访问者在任何规则级别下都不能写入。

### 请求模型：`sample` {#ask-the-model-sample}

声明 `{"sample": {}}`。外层界面每次页面加载仅请求一次同意。
请求使用来源 Agent 或 Assistant 配置的模型，会产生模型用量，允许前请检查页面。
请求使用配置的服务端模型凭证。内置模型网关接收标识访问者和 Artifact 的用量归属请求头。

```js
const model = await window.astrabox.use('sample');
if (model) {
  const answer = await model.sample('Suggest one checklist item.', {maxTokens: 100});
  document.querySelector('#answer').textContent = answer.text;
}
```

选项包括流式 `onText`、页面提供的工具和缓存。`sample.json` 可解析 JSON 答案。
接受 `modelTier` 参数，但它不会选择另一模型。

每个服务端请求限时 60 秒，JSON 上限为 256 KiB，输出最多 8,192 tokens（默认 4,096）。
每位访问者最多并发两个请求，每分钟最多发起 30 个请求，跨 Artifact 和后端副本共同计数。
页面提供的工具最多可使用八轮模型请求；每轮都计入这些限额。可选缓存在本次页面加载中保留五分钟。

### 识别访问者：`user` {#identify-viewers-user}

声明 `{"user": {}}`。id 仅在当前 Artifact 内有效，不是平台账号 id。
可通过 `me().guest` 判断匿名访问者；匿名 id 按分享链接和页面加载隔离。

```js
const user = await window.astrabox.use('user');
if (user) document.querySelector('#role').textContent =
  user.isOwner() ? 'Owner' : (user.canEdit() ? 'Interactor' : 'Viewer');
```

`id`、`me`、`can` 和 `profiles` 提供访问者状态和允许读取的资料。
资料仅包含不透明 id 和显示名称；匿名访问者读取已登录用户的资料时，显示名称为空。
不提供账号 id 或邮件地址。

### 在线状态和事件：`room` {#presence-and-events-room}

声明 `{"room": {"topics": {"ping": "interact"}}}`。所有访问者都可使用在线状态。
发送事件默认要求 Owner，除非主题声明了其他级别。`join(name)` 选择命名房间。

```js
const room = await window.astrabox.use('room');
if (room) {
  room.onPeers(peers => { document.querySelector('#count').textContent = peers.length; });
  room.on('ping', data => { document.querySelector('#message').textContent = data.text; });
  await room.presence({viewing: true});
  await room.emit('ping', {text: 'Hello'});
}
```

房间事件在十分钟内可见。在线状态在最后一次心跳后 45 秒过期，页面运行时会在在线期间发送心跳。
`leave()` 可主动离开。事件和在线状态共享每位访问者每秒 10 次、突发 20 次的写入限额，
跨 Artifact 和后端副本共同计数。

### 保存文件：`downloads` {#save-a-file-downloads}

声明 `{"downloads": true}`。每次保存都由外层界面确认；页面直接发起的浏览器下载被阻止。

```js
const downloads = await window.astrabox.use('downloads');
if (downloads) await downloads.save({filename: 'notes.txt', data: 'A shared note'});
```

### 通过 Agent 刷新：`agent` {#refresh-through-the-agent-agent}

声明 `{"agent": {}, "db": {}}`，运行来源 Agent 并观察数据更新。
外层界面每次都显示页面写出的完整 prompt 并请求同意。
运行会创建属于访问者的新会话，沿用访问者的 Artifact 权限和数据规则，
使用 Agent 配置的 MCP 服务、凭证和网络策略。

此运行的 artifacts 工具只能访问当前 Artifact，即使访问者还拥有其他 Artifact，也不能访问它们。
运行不能创建另一个 Artifact；只有 Artifact 所有者可发布新版本。
共享访问者的工具权限会重新检查链接的启用状态、token、过期时间、受众、当前权限和提供的版本。
撤销或过期会移除访问权，权限降级会降低运行使用的访问级别。
所有者发起的运行要求当前所有权，不依赖分享链接。

```js
const agent = await window.astrabox.use('agent');
if (agent) {
  const run = await agent.run({prompt:
    'Use artifact_data to write the latest project status to live/status on this artifact.'});
  agent.watch(run.runId, status => {
    document.querySelector('#state').textContent = status.state;
  });
}
```

`run` 返回 `runId`、`sessionId` 和 `url`；Agent 需要输入时可打开该会话。
来源 Agent 关闭了**作品**开关时，运行无法访问 Artifact，`run` 会以 `unavailable`（`ARTIFACT_AGENT_UNAVAILABLE`）失败。
`status` 和 `watch` 提供 `running`、`waiting_input`、`completed` 或 `failed` 状态。
写入的数据通过页面的 `db` 订阅送达。每位访问者在每个 Artifact 上同时最多运行一次，
每小时最多发起 10 次运行，跨 Artifact 和后端副本共同计数。等待输入也算活动运行。

### 管理授权：`permissions` {#manage-grants-permissions}

授权只适用于当前页面加载。访问者可移除或恢复符合条件的能力，不能超出能力声明和自身权限。

```js
const permissions = await window.astrabox.use('permissions');
if (permissions) await permissions.manage();
```

`state(name)` 查询授权，`request(names)` 请求符合条件的能力。

## 安全和限制 {#security-and-limits}

脚本内容只在不透明来源的沙箱 iframe 中执行，不能读取控制台 cookie、存储或 `parent.document`。
页面不能直接访问网络数据；需要服务端处理的运行时请求通过外层界面的消息桥转发，服务端再次检查权限。
外层界面还会校验路径，将请求限定在当前 Artifact 的运行时路由内。
外部图片和嵌套 frame 被阻止。允许内联脚本、样式、内置运行库和受支持的脚本与字体 CDN。
不要把秘密写进源码或共享数据。

运行时提供按 Artifact 和访问者隔离的 `localStorage`，限额为 5 MiB；
分享视图还按 token 隔离。`sessionStorage` 只保留到本次页面加载结束。
这些存储不会暴露控制台存储。

会话记录中的 Artifact 工具结果包含 Artifact 标记和 URL；`get_artifact` 另提供内容、描述、
能力声明和当前访问者的不透明身份。数据操作返回数据结果和标记。
工具元数据不包含所有者 id、来源或分享状态。分享通过所有者控制台和 API 管理。

| 项目 | 上限 | API 错误 |
| --- | --- | --- |
| 每个版本的内容 | 4 MiB UTF-8 | `ARTIFACT_CONTENT_TOO_LARGE` |
| 每个 Artifact 的版本数 | 1,000 | `ARTIFACT_VERSION_LIMIT` |
| 标题 | 200 字符 | `ARTIFACT_TITLE_LIMIT` |
| 单个数据文档 | 256 KiB JSON | `ARTIFACT_DATA_LIMIT` |
| 每个 Artifact 的数据文档数 | 10,000 | `ARTIFACT_DATA_COUNT_LIMIT` |
| 房间事件内容 | 16 KiB JSON | `ARTIFACT_ROOM_LIMIT` |
| 模型请求 JSON，含原始 HTTP 字节 | 256 KiB | `ARTIFACT_SAMPLE_TOO_LARGE` |
| 每次模型请求的输出 tokens | 8,192（默认 4,096） | `ARTIFACT_SAMPLE_TOKEN_LIMIT` |
| 每位访问者的并发模型请求 | 2，跨 Artifact 和副本 | `ARTIFACT_SAMPLE_RATE_LIMITED` |
| 每位访问者发起的模型请求 | 每分钟 30 次，跨 Artifact 和副本 | `ARTIFACT_SAMPLE_RATE_LIMITED` |
| 每位访问者的房间和在线状态写入 | 每秒 10 次，突发 20 次，跨 Artifact 和副本 | `ARTIFACT_LIVE_RATE_LIMITED` |
| 每个后端副本中每位访问者的实时流 | 8，跨 Artifact | `ARTIFACT_LIVE_RATE_LIMITED` |
| 每位访问者在每个 Artifact 上的活动 Agent 运行 | 1，含等待输入 | `ARTIFACT_AGENT_RUN_LIMITED` |
| 每位访问者发起的 Agent 运行 | 每小时 10 次，跨 Artifact 和副本 | `ARTIFACT_AGENT_RUN_LIMITED` |

不支持沙箱文件访问（`files`），因为 Artifact 必须脱离沙箱仍可使用。
不支持访问者连接器直连（`mcp`）；请通过 `agent`，使调用遵循 Agent 的凭证和网络策略。
`assets` 和 `comments` 返回 `null`，尚不支持 blob 存储和评论线程。
不支持模板、派生副本、邮件邀请或编辑者角色。
Artifact 是平台托管的输出，不是支持任意网络访问或软件包安装的通用应用托管服务。

### 运行时错误 {#runtime-errors}

页面调用失败时返回 `code` 和 `message`：`not_granted`、`not_writer`、`conflict`、
`invalid_argument`、`not_found`、`rate_limited`、`unavailable`、`too_large` 或 `cancelled`。
页面应明确展示错误；模型错误可能包含部分 `text`。
运行时端点将 Artifact API 错误映射到 `data.runtime_code`；
实时流和模型 SSE 错误事件使用运行时错误码。

例如，过期的 `base_version` 返回 `ARTIFACT_VERSION_CONFLICT`（`conflict`）；
无权读取数据返回 `ARTIFACT_DATA_DENIED`（`not_granted`）；
请求未声明的能力返回 `ARTIFACT_CAPABILITY_NOT_DECLARED`（`not_granted`）。
所有者资源不存在或属于他人时返回 `ARTIFACT_NOT_FOUND`；无效分享链接返回
`ARTIFACT_SHARE_NOT_FOUND`，两者均对应 `not_found`。
