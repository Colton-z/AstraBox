# 接入新的消息平台

`ChannelProvider` 将消息产品接入 Agent Deployment，负责验证入站事件、映射消息与对话标识，并通过消息产品的官方 API 投递 Agent 回复。

如果已安装的适配器支持目标产品，直接在 Deployment 页面中配置即可。只有接入新的消息产品，或使用该产品官方支持的另一种传输方式时，才需要实现 `ChannelProvider`。

## 认证与消息身份

消息平台适配器有两个相互独立的职责：

| 维度 | 解决的问题 | AstraBox 接口 |
| --- | --- | --- |
| 渠道认证 | 入站事件是否来自已配置的消息产品 | `verify_and_resolve()` 或已认证的消息源连接 |
| 消息身份 | 哪些消息是重复投递，哪些消息属于同一段对话 | `ChannelInbound.dedup_key` 和 `conversation_key` |

`dedup_key` 是消息产品提供的唯一事件 ID 或消息 ID，用于避免重复投递触发两次执行。`conversation_key` 是稳定的聊天、群组、工单或话题 ID，用于让后续消息继续同一段 Agent 对话。两者不能混用。

## HTTP Webhook 模式

消息产品支持带签名的 HTTP 回调时，使用这一模式：

1. **打包适配器**：发布一个带有 `ChannelProvider` 入口点的 Python 包；
2. **描述配置字段**：声明 AstraBox 配置页面需要的普通配置和只写凭证；
3. **验证并转换回调**：根据原始请求验证消息产品的签名，再返回 `ChannelInbound`；
4. **投递回复**：使用已保存的 `reply_context` 和重新读取的凭证，通过消息产品 API 发送 Agent 回复；
5. **创建渠道 Deployment**：选择已安装的适配器，并将消息产品的事件地址指向该 Deployment。

消息产品的事件地址为：

```text
POST /api/v1/deployments/{deployment_id}/trigger
```

AstraBox 会先保存入站任务，再返回确认。适配器提供稳定的 `dedup_key` 后，重复投递会返回已有任务，不会再次执行。

:::note

部分消息产品要求特定的回调路径、HTTP 方法或响应正文。接入这类产品时，请声明 `callback_path` 并实现 `forward_callback()`，不使用共用的触发响应。

:::

## 消息身份

在 `ChannelInbound` 中返回消息产品提供的稳定信息：

| 字段 | 说明 |
| --- | --- |
| `content` | 交给 Agent 的文本 |
| `dedup_key` | 用于识别重复投递的唯一事件 ID 或消息 ID |
| `conversation_key` | 稳定的对话、群组、工单或话题 ID |
| `reply_context` | 投递回复时使用的非敏感路由信息 |
| `attention` | 私聊、提及和回复信号 |
| `participant` | 随消息记录的外部发送者身份 |

`reference` 将入站回复关联到之前发出的平台消息；`ack_extra` 在回调响应中加入产品要求的字段；`alias_link` 记录投递后才获取的平台消息 ID；`ignore_reason` 标记不应触发执行的已认证事件。

Deployment 的 `attention_policy` 决定一条已认证消息是否需要私聊、提及或回复信号。适配器只报告这些事实，不负责决定策略。

## 消息源连接模式

如果消息产品通过消息队列、长连接或官方 SDK 投递消息，而不是 HTTP 回调，请使用消息源连接。

设置 `supports_source = True` 并实现 `open_source()`。每条 `ChannelSourceEnvelope` 都应带有稳定的 `dedup_key`。只有 AstraBox 返回持久化接收凭据后才能确认消息；写入失败时应拒绝消息，让上游重新投递。

消息源支持有序重放游标时，请同时提供 `source_cursor`。AstraBox 在消息持久化后保存游标，并在重新建立连接时传回。

`verify_and_resolve()` 仍是必需方法。只使用消息源连接的适配器应明确拒绝共用 HTTP trigger，例如返回状态码为 `404` 的 `APIError`；不得在未使用的路径上接受未经认证的请求。

## 配置与凭证

适配器通过 `ChannelDescriptor` 描述配置字段：

| 字段组 | 保存方式 | 可用位置 |
| --- | --- | --- |
| `config_fields` | Deployment 中的非敏感配置 | 回调验证、消息源连接和回复投递 |
| `credential_fields` | 加密保存的只写凭证 | 消息源连接和回复投递 |

凭证只在创建或更新 Deployment 时写入；读取接口仅返回 `credentials_configured`，不会返回已经保存的值。

使用 `setup_url` 链接到消息产品的配置页面，使用 `documentation_url` 链接到适配器的配置指南。如果需要调用消息产品接口验证凭证或账户设置，请重写异步的 `validate_configuration()`；验证失败时，不会保存本次创建或更新。

直接 HTTP 回调不会读取 `credential_fields`。请按照消息产品的官方 Webhook 算法，使用 Deployment 的触发密钥或 `config_fields` 中的公开验证材料完成认证。消息源连接或出站 API 所需的私密 Token 应放在 `credential_fields` 中。

## 打包适配器

在 Python 包中声明适配器：

```toml
[project]
name = "astrabox-channel-example"
requires-python = ">=3.11"

[project.entry-points."astrabox.providers.channel"]
example = "astrabox_channel_example:ExampleChannelProvider"
```

`ExampleChannelProvider.name` 会成为渠道场景名 `channel:example`；入口点请使用同一个名称，确保插件发现名称与产品配置一致。AstraBox 启动时会加载、校验并注册这个插件组。入口点重名或能力接口不完整会导致启动失败。

适配器类也可以声明 `seams_api_version`；声明的版本不兼容时，AstraBox 会在启动阶段报错，而不是等到第一条消息到达后才失败。

## 实现适配器

继承 `astrabox.seams.channel` 中的 `ChannelProvider`，并实现 `verify_and_resolve()`：

```python
from collections.abc import Mapping
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.seams.channel import (
    ChannelAttention,
    ChannelDescriptor,
    ChannelField,
    ChannelInbound,
    ChannelProvider,
)


class ExampleChannelProvider(ChannelProvider):
    name = "example"
    uses_trigger_secret = False

    def describe(self) -> ChannelDescriptor:
        return ChannelDescriptor(
            name=self.name,
            label="Example",
            config_fields=(
                ChannelField(
                    key="signing_public_key",
                    label="Signing public key",
                ),
            ),
            credential_fields=(
                ChannelField(
                    key="bot_token",
                    label="Bot token",
                    secret=True,
                ),
            ),
            documentation_url="https://example.com/webhook-docs",
        )

    def normalize_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        public_key = str(config.get("signing_public_key") or "").strip()
        if not public_key:
            raise ValueError("signing_public_key is required")
        return {"signing_public_key": public_key}

    def normalize_credentials(
        self, credentials: Mapping[str, Any]
    ) -> dict[str, Any]:
        token = str(credentials.get("bot_token") or "").strip()
        if not token:
            raise ValueError("bot_token is required")
        return {"bot_token": token}

    def verify_and_resolve(
        self,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
        binding: Mapping[str, Any],
    ) -> ChannelInbound:
        payload = verify_official_signature_and_decode(
            public_key=str(binding["channel_config"]["signing_public_key"]),
            headers=headers,
            raw_body=raw_body,
        )
        if not payload.get("text"):
            raise APIError(
                code="EXAMPLE_BAD_PAYLOAD",
                message="message text is required",
                status_code=400,
            )
        return ChannelInbound(
            content=str(payload["text"]),
            dedup_key=str(payload["event_id"]),
            conversation_key=str(payload["conversation_id"]),
            reply_context={"conversation_id": str(payload["conversation_id"])},
            participant=str(payload.get("sender_id") or "") or None,
            attention=ChannelAttention(
                is_direct_message=bool(payload.get("is_direct")),
                is_mention=bool(payload.get("mentions_agent")),
            ),
        )
```

`verify_and_resolve()` 会在回调请求中同步执行，传入的 Header 名称均为小写。应先根据原始字节验证签名，再解析正文；认证失败时，返回状态码为 `401` 的 `APIError`。示例中的 `verify_official_signature_and_decode()` 代表消息产品要求的验证函数，请严格按照该产品的官方规范实现。

## 投递 Agent 回复

实现 `deliver_outbound()`，即可发送 Agent 的最终回复。AstraBox 会传入重新读取的 `binding`，其中 `channel_credentials` 包含只写凭证。`reply_context` 会被持久化以支持重试，只能保存路由 ID，不能包含凭证。

返回 `ChannelDeliveryReceipt`，其中包含本次创建或更新的全部平台消息 ID。AstraBox 会先保存这些 ID，再把投递标记为完成，让后续入站回复可以解析 `reference`。Receipt 本身不能保证外部发送幂等：如果发送成功但 Receipt 持久化失败，投递可能再次执行。产品 API 提供幂等支持时，应使用该能力。

如果消息产品支持在 Agent 执行期间更新消息，请设置 `supports_streaming_delivery = True` 并实现 `open_delivery()`。依次处理 `turn_started`、`progress`、`settled` 和 `failed` 事件。`prior_aliases` 参数包含之前一次投递已保存的平台消息 ID；应更新这些消息，不要创建替代消息。

每个可选能力开关都必须与对应方法一起实现。注册时会拒绝不完整的能力组合。

## 验证接入

在插件测试中接入可复用的适配器检查：

```python
from astrabox.testing.provider_conformance import ChannelProviderContractSuite

from astrabox_channel_example import ExampleChannelProvider


class TestExampleChannelProvider(ChannelProviderContractSuite):
    def make_provider(self):
        return ExampleChannelProvider()
```

再加入从消息产品采集的测试样例，覆盖签名验证、事件结构变化、事件重复投递、回复引用和出站 API 错误。最后验证完整链路：

1. 适配器出现在渠道配置页面，字段符合预期；
2. 已签名事件创建或继续预期的对话；
3. 重复事件 ID 不会再次触发执行；
4. 提及和私聊遵循 Deployment 的 `attention_policy`；
5. Agent 回复到达原始对话或话题；
6. 产品 API 拒绝回复时，服务会报告明确的投递错误。

## 相关文档

- [消息平台](./channels.md) — 配置方法与消息行为
- [Deployments](./deployments.md) — 渠道、Webhook 和定时触发
- [HTTP API](./api.md) — Deployment 与触发接口
