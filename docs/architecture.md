# 架构说明

## Milestone 0 目标

完整的 Milestone 0 验收目标是：让 Android 手机通过系统 TTS，稳定地让一个 GPT-SoVITS 角色连续朗读 20 段文字。

浏览器测试页只用于验证局域网 HTTP 请求和 WAV 返回链路，不是 Milestone 0 的最终验收。

## 当前边界

Character Voice Service 是 GPT-SoVITS 与客户端之间的适配层：

```text
Android 系统 TTS / 浏览器测试页
  ↓ HTTP
Character Voice Service
  ↓ localhost HTTP
GPT-SoVITS
  ↓
WAV
```

服务层负责稳定 API、单角色配置解析、参数映射和错误转换。当前保持单角色、单语言约束。缓存、流式传输、多角色自动路由、情绪、LLM 和 EPUB 均不属于本阶段。

## 角色标识约定

- `voices/example.json` 只是模板，不是可用角色。
- 真实配置使用稳定的 voice ID 作为文件名，例如 `voices/march-7th.json`。
- API 的 `voice` 参数使用文件名 stem，例如 `march-7th`。
- JSON 中的 `name` 只用于用户界面显示，例如 `March 7th`。
- Milestone 0 仍只要求配置和使用一个真实角色。

## 安全边界

- GPT-SoVITS 仅监听 `127.0.0.1:9880`。
- Character Voice Service 按需监听 `0.0.0.0:9881`，仅用于可信局域网。
- 不配置路由器端口转发。
- 校园网、公共 Wi-Fi 或不可信网络不应直接暴露此端口。
- 后续远程访问应优先考虑受控 VPN 或 overlay network。
