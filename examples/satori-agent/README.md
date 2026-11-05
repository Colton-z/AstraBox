# Backend client

Install `httpx`, `websockets` and `satori-python`, then run:

```sh
python client.py --url https://astrabox.example.com --agent AGENT_ID \
  --token-file /run/secrets/delegated-execution-token \
  --image screenshot.png --file report.csv 'What does the screenshot show, and what is the total in the report?'
```

`--image` and `--file` are optional and repeatable. The client uploads them,
sends them with the message, follows the reply, then reads the conversation back
and downloads every file it names, including files the Agent wrote.

The token is supplied by your deployment's identity provider. Employees remain
logged into your business application; this example does not log them into
AstraBox. Keep credentials on the backend. See
[the API contract](../../docs/integrations/satori-agent.md) for preflight actions,
interaction answers and durable reconnect cursors.

运行前安装 `httpx`、`websockets` 和 `satori-python`。`--image`、`--file` 可选、可重复：示例会上传文件、随消息发送、跟随回复，最后回读会话并下载其中出现的每个文件，包括 Agent 写出的文件。凭据由部署方身份 provider 提供，代表已验证的员工执行身份；
员工继续使用业务系统，无需登录 AstraBox。凭据仅保留在业务后端。
[中文契约](../../docs/integrations/satori-agent.zh-CN.md)说明授权操作、交互回答和断线补发。
