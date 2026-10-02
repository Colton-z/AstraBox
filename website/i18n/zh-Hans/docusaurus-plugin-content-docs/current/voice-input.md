# 语音输入

语音输入会录制一段语音，将其转成文字并追加到会话草稿。你可以编辑文字，
确认后再发送给 Agent。录音最长两分钟，到时自动停止；单次上传最多 25 MiB。
取消会丢弃当前录音，或忽略尚未返回的转写结果。取消不能撤销模型服务已经开始的处理。

转写失败时，麦克风会停止，录音保留在当前页面的内存中。选择**重试转写**，
可将同一段录音重新提交给同一个模型，无需再次录音；选择**丢弃录音**可清除它。
现有草稿仍可编辑，但需要先完成转写或丢弃录音才能发送。重试成功后，转写文字只会
追加到最新草稿一次，随后清除保留的录音。系统不会自动重试。

音频不会保存到浏览器存储，也不会由 AstraBox 持久化保存。刷新页面、离开会话或关闭页面
都会清除录音。当会话无法继续接收输入，或配置的语音模型路由被移除时，也会丢弃录音。
重试会再次向模型服务提交音频，因此可能产生额外的转写费用，包括上一次响应丢失的情况。

麦克风控件改编自 Vercel AI Elements 的 Apache-2.0
[SpeechInput](https://elements.ai-sdk.dev/components/speech-input)，始终使用所选的网关模型。
源码与改编说明位于 `frontend/src/components/voice/SpeechInput.tsx`；
未经改动的注册表组件仍位于 `components/ai-elements`。

## 配置转写模型

通过现有 LiteLLM 网关的模型管理界面或配置文件，配置一个或多个音频转写路由。例如：

```yaml
model_list:
  - model_name: voice-cloud
    litellm_params:
      model: openai/whisper-1
      api_key: os.environ/OPENAI_API_KEY
    model_info:
      mode: audio_transcription
  - model_name: voice-local
    litellm_params:
      model: openai/your-transcription-model
      api_base: os.environ/VOICE_LOCAL_BASE_URL
      api_key: os.environ/VOICE_LOCAL_API_KEY
    model_info:
      mode: audio_transcription
```

第二个路由是可选的。自托管服务必须实现兼容 OpenAI 的音频转写接口，接受浏览器录音的格式，
并且能**从网关访问**。仅提供聊天接口的服务不适用；云端网关中的 `localhost` 不是用户的电脑。

使用 JSON 列表将路由名称配置到 AstraBox 服务端：

```sh
ASTRABOX_SPEECH_INPUT_MODELS='["voice-cloud", "voice-local"]'
```

也可以在 `app.yml` 中设置 `astrabox.speech_input.models`。修改后重启服务端。
空列表会禁用麦克风控件。第一个模型为默认选项；配置多个模型时，用户可以在麦克风旁切换，
浏览器会记住选择。

使用现有部署方式向 LiteLLM 提供模型服务的密钥。AstraBox 不部署语音模型，
也不要求额外部署媒体服务。

## 请求流程

浏览器通过 multipart 表单将音频上传到
`POST /api/v1/sessions/{session_id}/speech-input`。AstraBox 检查会话归属、所选路由和
录音大小，解析现有模型端点提供方，然后使用服务端可访问的网关地址与会话推理凭据，
调用 `/v1/audio/transcriptions`。不提供会话凭据的模型端点提供方使用其声明的服务端凭据。
所选提供方必须提供兼容的音频转写接口。

浏览器只接收转写文字。音频不会加入沙箱、会话历史或工作区。网关和模型服务自身的数据保留
政策仍然适用。此接口使用平台配置的 `model_endpoint_provider`，语音模型独立于 Agent 的聊天模型。

录音需要麦克风以及安全的浏览器上下文（HTTPS 或 localhost）。停止录音后才会开始转写；
此功能不提供实时逐字转写，也不是与 Agent 进行语音通话。
