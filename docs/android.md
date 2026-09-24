# Android 接入计划

## 目标链路

```text
阅读器
  ↓ Android TextToSpeech API
自定义 TTS Engine
  ↓ HTTP POST
Character Voice Service
  ↓
GPT-SoVITS
```

## 第一阶段

先完成一个最小 Android 客户端：

- 输入文本；
- 选择角色；
- 调用 `POST /v1/audio/speech`；
- 播放返回 WAV；
- 验证停止/取消。

## 第二阶段

实现 Android `TextToSpeechService`：

- 正确处理 `onSynthesizeText()` 生命周期；
- 将 WAV PCM 数据写入 TTS callback；
- 实现 `onStop()`；
- 支持英语 Locale；
- 明确处理局域网 HTTP cleartext 配置。

## 验收

首轮真实阅读测试建议记录：

- 冷启动首音频等待时间；
- 热启动首音频等待时间；
- 20 段连续请求成功率；
- 段落间停顿；
- 顺序错误；
- 停止/取消响应；
- 长文本行为。
