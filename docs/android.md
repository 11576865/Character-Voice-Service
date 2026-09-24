# Android 接入计划

> 本文仅记录下一阶段计划。本轮工程工作不实现 Android 客户端。

## Milestone 0 最终目标

```text
阅读器
  ↓ Android TextToSpeech API
自定义 TTS Engine
  ↓ HTTP POST
Character Voice Service
  ↓
GPT-SoVITS
```

最终验收是 Android 手机通过系统 TTS，使用一个 GPT-SoVITS 角色稳定连续朗读 20 段文字。

## 第一阶段

先完成一个最小 Android 客户端：输入文本，使用固定角色和固定语言，调用 `POST /v1/audio/speech`，播放返回的 WAV，并验证停止和取消。

## 第二阶段

实现 Android `TextToSpeechService`：

- 正确处理 `onSynthesizeText()` 生命周期；
- 将 WAV PCM 数据写入 TTS callback；
- 实现 `onStop()`；
- 支持约定的单一 Locale；
- 明确处理局域网 HTTP cleartext 配置。

## 验收

使用真实阅读器连续朗读 20 段文字，并记录冷启动和热启动等待时间、连续请求成功率、段落间停顿、顺序错误、停止和取消响应以及长文本行为。
