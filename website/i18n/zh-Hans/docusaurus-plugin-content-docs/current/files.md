# Session 文件

文件 API 让你向 Session 提供文件上下文——代码仓库、配置文件、参考文档等。Agent 可以读取这些文件来理解任务背景。控制台右侧的**文件**页签展示的是同一个工作区。文件上传后即可通过工作区路径使用，无需创建单独的 File 资源，也无需执行挂载。

## 核心流程

打开 Agent 并启动 Session。打开**文件**页签，选择目录后上传一个或多个文件，也可以直接把文件拖入面板。Session 运行期间 Agent 读取文件内容，完成任务。

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

普通文件不超过 64 MiB 时可以下载。目录直接在工作区中管理，不会打包下载。

## curl 上传示例

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
AstraBox 的标准 Agent 镜像把工作区展示为 `/workspace`，**文件**页签会显示实际的
根目录和当前目录。

### Prompt 示例

上传 `app.py` 后，在同一个 Session 中发送任务：

```text
检查 /workspace/app.py 并修复其中的 bug。直接保存修改后的文件，
并把总结写入 /workspace/review.md。
```

Agent 可以立即打开 `app.py`，它创建的文件也会出现在同一个工作区中。

## 下载文件

在**文件**页签中打开文件的操作菜单并选择**下载**，也可以调用：

```bash
curl --get \
  "$SERVICE_URL/api/v1/sessions/$SESSION_ID/files/download" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode "path=/workspace/review.md" \
  -o review.md
```

工作区中的任何普通文件都可以下载，单个文件不能超过 64 MiB。

## 查看文件信息

**文件**页签展示当前路径、目录树、文件名、文件类型和大小。这些信息直接来自
Session 当前的工作区，因此 Agent 所做的修改会出现在相同路径下。

## 列出文件

在**文件**页签中展开文件夹即可浏览工作区。每轮任务完成后，已展开的目录会自动
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
4. 任务完成后，在**文件**页签中打开 `review.md` 的操作菜单并选择**下载**。

## 常见问题

<b>Q：上传的文件存储多久？</b>

A：文件属于 Session 工作区。配置可选的持久工作区存储后，文件可以跨沙箱释放或更换
保留；未配置时，仅存于沙箱的文件会随沙箱删除而丢失。平台数据库单独保存原生会话
状态，恢复对话不等于恢复工作区文件。需要单独长期保存的重要结果，可以下载到本地；
代码修改也可以提交并推送到代码仓库。

<b>Q：能否直接在创建 Session 时附带文件？</b>

A：启动 Session 并等待运行环境就绪后即可上传文件。文件会直接写入工作区，之后
无需再执行挂载。

<b>Q：哪些文件可以下载？</b>

A：Session 工作区中的任何普通文件都可以下载，只要单个文件不超过 64 MiB；无论
文件是用户上传的，还是 Agent 生成的，规则都相同。

<b>Q：支持哪些文件格式？</b>

A：无格式限制，任意二进制文件均可上传。Agent 对文本类文件（代码、配置、文档）的理解效果最佳。
