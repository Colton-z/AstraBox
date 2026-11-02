# Backend client

Install `httpx` and `websockets`, then run:

```sh
python client.py --url https://astrabox.example.com --agent AGENT_ID \
  --token-file /run/secrets/delegated-execution-token 'Hello'
```

The token is supplied by your deployment's identity provider. Employees remain
logged into your business application; this example does not log them into
AstraBox. Keep credentials on the backend. See
[the API contract](../../docs/integrations/satori-agent.md) for preflight actions,
interaction answers and durable reconnect cursors.

运行前安装 `httpx` 和 `websockets`。凭据由部署方身份 provider 提供，代表已验证的员工执行身份；
员工继续使用业务系统，无需登录 AstraBox。凭据仅保留在业务后端。
[中文契约](../../docs/integrations/satori-agent.zh-CN.md)说明授权操作、交互回答和断线补发。
