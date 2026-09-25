# Character Voice Service

Character Voice Service 是本地角色语音基础设施。当前后端使用 **GPT-SoVITS**，通过稳定 HTTP API 把角色资产、模型权重、参考语音与 Reader 等客户端分离。

当前主线：

```text
训练好的 GPT-SoVITS 模型
        +
Reference Pack / Reference Library
        ↓
Character Registry
        ↓
Character Voice Service
        ↓
Reader / 后续其他客户端
```

## 当前能力

### Character Registry

- 一个角色可以注册多个 GPT-SoVITS 模型版本；
- 一个角色可以注册多个参考语音；
- 请求可显式选择 `model_id` 与 `reference_id`；
- 未指定时使用角色默认模型与默认参考语音；
- 注册了 `.ckpt/.pth` 路径的模型可自动调用 GPT-SoVITS 官方权重切换接口；
- 旧版“单模型 + 单参考语音”配置继续兼容；
- `GET /v1/voices` 不暴露本机模型路径、参考 WAV 路径或参考音频逐字文本。

详细设计见 [Character Registry](docs/character-registry.md)。

### Reader

`/test` 已支持：

- 手动文本；
- UTF-8 TXT；
- EPUB；
- 章节与片段切分；
- 连续播放；
- 单段预取；
- 本地阅读进度；
- 同一 TXT/EPUB 的继续阅读 / 从头开始；
- 上一/下一章；
- 上一/下一段；
- 当前段落高亮与正文跟随；
- 角色、模型、参考语音选择。

Reader 架构见 [Reader 架构](docs/reader-architecture.md)，进度规则见 [Reader 状态](docs/reader-state.md)，EPUB 解析见 [EPUB Text Source](docs/epub-source.md)。

## 架构

```text
TXT / EPUB / 手动文本
          ↓
      Reader Core
          ↓
Character Voice Service :9881
          ↓
   Character Registry
      ↙          ↘
 Model Registry   Reference Library
      \          /
       GPT-SoVITS :9880
             ↓
            WAV
```

GPT-SoVITS 建议只监听 `127.0.0.1:9880`。局域网只按需暴露 Character Voice Service。

## 角色配置

真实角色继续使用文件名 stem 作为稳定 voice ID：

```text
voices/march-7th.json
```

Profile v2 示例：

```json
{
  "schema_version": 2,
  "name": "March 7th",
  "target_language": "en",

  "default_model": "self-400-v2pro",
  "models": {
    "self-400-v2pro": {
      "name": "Self 400 v2Pro",
      "engine": "gpt-sovits",
      "version": "v2pro",
      "gpt_weights": "D:/Models/March7th/march.ckpt",
      "sovits_weights": "D:/Models/March7th/march.pth"
    }
  },

  "default_reference": "neutral-01",
  "references": {
    "neutral-01": {
      "name": "Neutral 01",
      "audio": "D:/References/March7th/neutral-01.wav",
      "text": "Exact official transcript.",
      "language": "en",
      "emotion": "neutral",
      "intensity": 0.4,
      "quality": "good"
    }
  }
}
```

旧配置仍然可以直接运行：

```json
{
  "name": "March 7th",
  "reference_audio": "D:/ref.wav",
  "reference_text": "Exact transcript.",
  "reference_language": "en",
  "target_language": "en"
}
```

旧配置中的模型被视为 GPT-SoVITS 已经手工加载的外部模型，不要求立即迁移。

## 模型切换

受管理模型同时配置：

```text
gpt_weights
sovits_weights
```

Service 在需要时调用：

```text
GET /set_sovits_weights
GET /set_gpt_weights
POST /tts
```

当前模型会缓存，同一模型连续请求不会重复加载。

GPT-SoVITS 一个进程只有一组活动权重，因此“模型切换 + 一次语音合成”会被串行保护，避免并发请求在错误模型上生成。

如果 Service 已经切换到受管理模型，它不会静默回到一个没有登记权重路径的旧版外部模型。此时需要注册该模型路径，或重启 GPT-SoVITS 和 Service。

## Windows 首次设置

需要 Python 3.10 或更高版本：

```powershell
.\scripts\first_setup.cmd
```

复制模板：

```powershell
Copy-Item .\voices\example.json .\voices\march-7th.json
```

真实角色配置默认被 Git 忽略，不要提交本机模型路径、参考音频或其他私有资产。

## 日常启动

先启动 GPT-SoVITS：

```powershell
cd C:\Users\27619\Downloads\GPT-SoVITS-v2pro-20250604-nvidia50
.\runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

再启动本服务：

```powershell
.\scripts\run_server.cmd
```

Reader：

```text
http://127.0.0.1:9881/test
```

## API

使用默认模型与默认参考：

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "input": "This is a test.",
  "response_format": "wav",
  "speed": 1.0
}
```

显式选择角色资产：

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "model_id": "self-400-v2pro",
  "reference_id": "surprised-01",
  "input": "What are you doing here?",
  "response_format": "wav",
  "speed": 1.0
}
```

成功时返回 `audio/wav`。

## 测试

Python 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

自动测试不需要真实 GPT-SoVITS、模型文件或参考 WAV。

浏览器模块测试页：

- `tests/reader_core_test.html`
- `tests/epub_source_test.html`
- `tests/reader_state_test.html`

真实连续朗读：

```powershell
.\.venv\Scripts\python.exe .\tests\continuous_read_test.py --voice march-7th
```

## 安全边界

- GPT-SoVITS 建议仅监听 `127.0.0.1:9880`；
- Character Voice Service 当前面向本机与可信局域网；
- 不直接把 9880/9881 暴露到公共网络；
- 当前没有公网认证、账户系统或多租户隔离。

## 接下来的主线

当前工作分成两条并行线：

1. 在 HSR-Voice-Archive-Builder 中试听并筛选真实 Reference Pack；
2. 在 Character Voice Service 中完成多角色、多模型、多参考语音注册和调度。

等真实参考语音实验确认不同 reference 能稳定产生有意义的情绪差异后，再进入：

```text
Reference Library
      ↓
Emotion Router
      ↓
Continuity Planner
      ↓
长篇 EPUB 情绪轨迹
```

Android System TTS 保留为后续第三方 Android App 兼容层，不是当前主线。
