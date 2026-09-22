# 分页

AstraBox API 的 Session 分页列表采用**游标分页**（Cursor-based Pagination）。使用上一页
响应中的 `next_cursor` 作为下一次请求的 `cursor` 参数。游标代表有序列表中的不透明位置，
不是数据变化时保持不变的快照。

## 请求参数

| 参数 | 类型 | 必需 | 默认值 | 说明 |
|---|---|---|---|---|
| `page` | integer | 是 | — | 设置为 `1`，请求分页响应结构 |
| `limit` | integer | 否 | 50 | 每页返回数量，限制在 1–100 |
| `cursor` | string | 否 | — | 上一次响应 `next_cursor` 返回的不透明游标 |

<Note>使用 `limit` 或 `cursor` 时应传入 `page=1`。未传入 `page=1` 时，
`GET /api/v1/sessions` 返回未分页的 Session 数组。</Note>

## 响应结构

Session 分页接口返回 AstraBox 标准响应格式，`data` 中包含分页对象：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [
      { "session_id": "session_abc123", "title": "my-session", "...": "..." },
      { "session_id": "session_def456", "title": "another-session", "...": "..." }
    ],
    "next_cursor": "eyJzZXNzaW9uX2lkIjoic2Vzc2lvbl9kZWY0NTYiLCJ1cGRhdGVkX2F0IjoiLi4uIn0",
    "has_more": true
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 请求成功时为 `OK` |
| `message` | string | 请求成功时为 `success` |
| `data.sessions` | array | 当前页的 Session 列表 |
| `data.next_cursor` | string \| null | 下一页的不透明游标。下一次请求时作为 `cursor` 参数传入 |
| `data.has_more` | boolean | 是否还有更多 Session |

## 基本用法

### 获取第一页

```bash
# 获取前 10 个 Session
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=10" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

### 获取下一页

使用上一页响应中的 `data.next_cursor` 作为 `cursor`：

```bash
# 获取下一页 10 个 Session
curl --fail --silent --show-error --get \
  "$SERVICE_URL/api/v1/sessions" \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  --data-urlencode 'page=1' \
  --data-urlencode 'limit=10' \
  --data-urlencode "cursor=$NEXT_CURSOR"
```

### 页码分页

部分管理接口使用 `page` 和 `page_size`，而不是游标。例如：

```bash
# 获取 Session 管理列表的第 2 页
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/admin/sessions/all?page=2&page_size=50" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

每个接口的分页参数和响应字段以 `/docs` 或 `/openapi.json` 中的 OpenAPI 定义为准。

## 完整遍历示例

以下脚本遍历当前身份可以访问的所有 Session：

```bash
#!/bin/bash
# 遍历所有 Session 并打印标题
BASE_URL="${SERVICE_URL}/api/v1"
next_cursor=""
page_num=1

while true; do
  # 构造 URL
  url="$BASE_URL/sessions?page=1&limit=50"
  if [ -n "$next_cursor" ]; then
    url="$url&cursor=$next_cursor"
  fi

  # 发起请求
  response=$(curl --fail --silent --show-error "$url" \
    -H "Authorization: Bearer $ACCESS_TOKEN")

  # 解析响应
  count=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d['data']['sessions']))")
  next_cursor=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'].get('next_cursor') or '')")

  echo "第 ${page_num} 页：获取 ${count} 条记录"

  if [ -z "$next_cursor" ]; then
    break
  fi

  page_num=$((page_num + 1))

  # 请求间隔，避免过快
  sleep 0.1
done

echo "遍历完成"
```

## limit 参数说明

| 值 | 行为 |
|---|---|
| 不传 | 默认返回 50 条 |
| 1 | 最小值，最多返回 1 个 Session |
| 100 | 最大值，最多返回 100 个 Session |
| 0 或负数 | 限制为 1 |
| > 100 | 限制为 100 |

<Warning>
  传入 `limit > 100` 不会返回超过 100 个 Session。需要更多数据时，请使用
  `limit=100` 并通过 `cursor` 翻页。
</Warning>

```bash
# 仅获取 1 个 Session，用于检查是否存在数据
curl --fail --silent --show-error \
  "$SERVICE_URL/api/v1/sessions?page=1&limit=1" \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## 空结果

当没有数据或已到达末尾时：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "sessions": [],
    "next_cursor": null,
    "has_more": false
  }
}
```

## 注意事项

1. **游标处理** — `cursor` 是不透明游标，应按响应原样传回
2. **排序方向** — Session 按更新时间降序排列，并使用 Session ID 处理相同时间
3. **数据变化** — 游标不会固定数据快照；分页期间更新的 Session 可能移动到列表前面
4. **并发分页** — 每个客户端应分别保存自己的游标链

## 下一步

- [概览](overview.md) — 了解 AstraBox 的整体架构。
