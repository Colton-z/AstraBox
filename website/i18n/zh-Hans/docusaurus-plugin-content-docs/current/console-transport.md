# 扩展控制台的 HTTP 传输

提供浏览器认证的部署，可以在控制台启动前注册一个传输函数。将
`globalThis.astraboxConsoleTransport` 设置为 `(request: Request) => Promise<Response>`。
TypeScript 类型 `ConsoleTransport` 由 `frontend/src/consoleTransport.ts` 导出。

定制控制台构建时，将部署自己的入口模块作为 HTML 入口：

```ts
import { fetchWithAuthRecovery } from './deployment/authRecovery';
import type { ConsoleTransport } from './consoleTransport';

globalThis.astraboxConsoleTransport = (
  (request) => fetchWithAuthRecovery(request)
) satisfies ConsoleTransport;

await import('./main');
```

`authRecovery` 由部署方实现，不是 AstraBox 模块。把这个入口与控制台一起打包，动态导入
确保注册完成后才启动控制台。提供预构建资源的部署，也可以在控制台入口执行前加载自己的
启动脚本。不要替换 `globalThis.fetch`、安装 Service Worker 或复制 API 客户端。
传输函数应在整个页面生命周期内保持安装。

未注册时，控制台 API 使用浏览器原生 fetch。注册后，传输函数收到标准 `Request`，其中
包含绝对 URL，以及原始方法、请求头、凭证、正文、取消信号和其他 fetch 选项。
FormData 由浏览器封装，包含匹配的 multipart boundary。返回实际 `Response`，再由控制台
按原有规则解析响应封装并显示错误。已配置的传输函数失败时，错误会向上传递，不会绕过它另发请求。

## 覆盖范围与调用顺序

传输函数覆盖生成的 API 调用、身份探测与退出登录、上传、文件内容读取、终端流、Session
输出流和 Artifact API，包括实时流与在线状态清理。不拦截浏览器导航、下载链接、iframe 文档、
WebSocket、blob URL、控制台自身的版本文档检查或沙箱预览 URL。

已有网络恢复机制的操作按以下顺序执行：

1. API 准备请求，并启动调用方设置的截止时间。
2. 现有网络恢复机制在每次尝试时调用已配置的传输函数。
3. 部署传输函数调用原生 fetch，识别自己的认证协议，返回最终响应。
4. API 解析响应并显示成功或失败。

网络恢复只处理抛出的传输错误；包括 500 在内的 HTTP 响应不会触发它重试。
读取保留现有的短时网络重试预算。只有调用方明确声明了服务端重放保证的写操作才有网络重试；
交互回答和普通 POST 没有这类重试。流式路径保留原有的游标与存活检查，不在这里增加网络恢复循环。

## 认证与重放由部署负责

部署必须识别自己的认证协议。AstraBox 不能从 HTTP 500、消息字符串或成功状态码推断业务
控制器未执行。只有部署的认证边界能证明请求在业务执行前被拒绝，传输函数才可以重放写操作。
交付结果不确定、业务失败、网关 HTML 响应或权限拒绝都不能转为认证重试。

发送可重放请求前，保留 `request.clone()`，因为 fetch 会消耗正文。需要检查响应时使用
`response.clone()`，让返回的响应仍可读取。不要为了检查认证而缓冲成功的事件流。
克隆流式正文可能缓冲未读取字节，因此部署必须限制恢复次数和正文处理。这些是原生
[Request.clone() 语义](https://developer.mozilla.org/en-US/docs/Web/API/Request/clone)，
[openapi-fetch](https://openapi-ts.dev/openapi-fetch/api#createclient) 的请求式自定义 fetch
也使用相同机制。

在部署传输函数内部合并认证探测，用原生 fetch 发送探测，避免递归调用控制台传输函数。
每次尝试都保留 `request.signal`，取消后停止恢复等待与重放。单次超时也必须遵守调用方信号；
注册不会重置交互回答现有的 15 秒截止时间。短暂恢复期间不得刷新页面或丢弃表单状态。

退出登录不参与自动认证恢复：不能续期登录或重放退出 POST。保留它的重定向模式以及
[确认与导航约定](team-login.md#sign-out)。身份提供方返回的退出 URL 仍由账户菜单执行顶层导航。

这个入口提供传输集成；认证分类、探测端点、去重、恢复预算，以及重放安全的证据仍由部署负责。
