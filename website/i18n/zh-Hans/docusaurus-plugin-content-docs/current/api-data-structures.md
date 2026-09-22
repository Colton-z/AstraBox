# 通用数据结构

AstraBox API 会复用以下跨资源数据结构。每个 AstraBox 实例都会在 `/docs` 和
`/openapi.json` 发布各资源自己的请求和响应结构。

## 成功响应格式

AstraBox REST 资源接口在 `data` 中返回操作结果。

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 请求成功时为 `OK` |
| `message` | string | 请求成功时为 `success` |
| `data` | any | 操作返回的数据 |

示例：

```json
{
  "code": "OK",
  "message": "success",
  "data": {
    "session_id": "21e061ee-c00b-48cb-a702-fc8f410792f7"
  }
}
```

<Note>流式传输、文件下载、MCP、重定向和 `204 No Content` 接口使用对应操作声明的响应
类型，不使用 JSON 成功响应格式。</Note>

## 分页列表

Session 分页列表在标准成功响应格式中返回游标分页对象：

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.sessions` | array | 当前页的 Session 对象 |
| `data.has_more` | boolean | 是否还有更多 Session |
| `data.next_cursor` | string \| null | 下一页的不透明游标；已到达末尾时为 `null` |

把 `data.next_cursor` 作为下一次请求的 `cursor` 参数传入。请求参数和完整遍历示例见
[分页](api-pagination.md)。

## 错误信封

错误响应使用以下结构：

```json
{
  "code": "INVALID_REQUEST",
  "message": "permission_mode is required",
  "data": null,
  "error": {
    "code": "INVALID_REQUEST",
    "status_code": 400,
    "category": "request",
    "retryable": false,
    "owner": "client",
    "user_message": "permission_mode is required"
  }
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 用于程序化处理的稳定错误码 |
| `message` | string | 可以向调用方显示的消息 |
| `data` | any \| null | 错误提供的结构化数据 |
| `error` | object | 错误状态、类别、是否可重试、责任方、调用方可见消息和可选诊断信息 |

所有错误字段和处理方式见[错误参考](api-errors.md)。

## 时间戳

平台生成的时间字段是 UTC 的 ISO 8601 / RFC 3339 字符串，例如
`"2026-08-24T19:26:39.616690+00:00"`。部分字段可以为 `null`，具体以各资源结构说明为准。

## 标识符

AstraBox 标识符是不透明字符串。各资源没有统一的公开前缀规则。

| 规则 | 说明 |
|---|---|
| 类型 | JSON string |
| 来源 | 从创建、列表或详情响应中读取 ID |
| 使用 | 在路径参数和请求字段中原样传入完整值 |
| 含义 | 根据字段和接口判断资源类型，不要根据 ID 文本判断 |
| 存储 | 为返回值保留足够字符，不要假定它是 UUID 或固定长度 |

## 下一步

- [概览](overview.md) — 了解 AstraBox 的整体架构。
