# Character Voice Service

一个面向本地角色语音生成的服务层。当前后端使用 **GPT-SoVITS**，目标不是绑定某一个角色或某一个模型，而是把角色、模型版本、参考语音和客户端之间的关系稳定下来。

当前主线：

```text
角色训练模型 + Reference Pack
              ↓
        Character Registry
              ↓
   Character Voice Service
              ↓
       Reader / 其他客户端
```

## 当前能力

- `POST /v1/audio/speech`：稳定的语音生成入口；
- `GET /health`：服务与 GPT-SoVITS 后端状态；
- `GET /v1/voices`：返回角色注册表的安全公开元数据；
- `GET /test`：浏览器 Reader；
- 一个角色可注册多个 GPT-SoVITS 模型版本；
- 一个角色可注册多个参考语音；
- 可按请求显式选择 `model_id` 和 `reference_id`；
- 注册了权重路径的模型可通过 GPT-SoVITS 官方 `/set_gpt_weights` 与 `/set_sovits_weights` 自动切换；
- 旧版单模型/单参考配置继续兼容；
- Reader 支持手动文本、UTF-8 TXT、EPUB、连续播放和单段预取；
- WAV 返回与可选本地保存。

角色注册表设计见 [Character Registry](docs/character-registry.md)。Reader 数据流见 [Reader 架构](docs/reader-architecture.md)。

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

模型权重与参考 WAV 不复制进仓库。真实角色配置只注册本机路径，且默认由 `.gitignore` 排除。

GPT-SoVITS 建议只监听 `127.0.0.1:9880`；局域网只按需暴露 Character Voice Service。

## Character Registry

真实角色仍以文件名 stem 作为稳定 ID：

```text
voices/march-7th.json
```

Profile v2 可以同时注册：

```text
March 7th
├── models
│   ├── self-400-v2pro
│   └── downloaded-v2pro
└── references
    ├── neutral-01
    ├── surprised-01
    └── ...
```

示例见 `voices/example.json`。

旧配置：

```json
{
  "name": "March 7th",
  "reference_audio": "D:/ref.wav",
  "reference_text": "Exact transcript.",
  "reference_language": "en",
  "target_language": "en"
}
```

仍然可以直接运行，不要求立即迁移。旧配置中的模型视为“GPT-SoVITS 已在外部手工加载”。

## 模型切换

Profile v2 中若某个模型同时配置：

```text
gpt_weights
sovits_weights
```

Character Voice Service 会在需要时切换 GPT-SoVITS 当前权重，并缓存当前活动模型。同一模型连续请求不会重复加载。

由于 GPT-SoVITS 进程只有一组活动权重，**模型切换与一次语音合成会被串行保护**，避免并发请求交叉使用错误模型。

如果服务已经切换到一个受管理模型，则不会再静默回退到“外部手工加载但未登记路径”的模型；这种情况会明确报错。详见 [Character Registry](docs/character-registry.md)。

## Windows 首次设置

需要 Python 3.10 或更高版本：

```powershell
.\scripts\first_setup.cmd
```

复制模板创建本地角色：

```powershell
Copy-Item .\voices\example.json .\voices\march-7th.json
```

然后填写本机模型权重、参考 WAV 与参考音频逐字文本。参考文本必须对应音频实际说出的内容。

除 `voices/example.json` 外，真实角色配置不应提交到 Git。

## 日常启动

先启动 GPT-SoVITS：

```powershell
cd C:\Users\27619\Downloads\GPT-SoVITS-v2pro-20250604-nvidia50
.\runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

再启动 Character Voice Service：

```powershell
.\scripts\run_server.cmd
```

Reader：

```text
http://127.0.0.1:9881/test
```

Reader 会读取注册表，允许选择角色、模型和参考语音。

## API

默认模型和默认参考：

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "input": "This is a test.",
  "response_format": "wav",
  "speed": 1.0
}
```

显式指定角色资产：

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

`GET /v1/voices` 会返回模型 ID、显示名、版本、参考语音 ID、情绪标签、强度等安全元数据，但不会暴露：

- 本地 `.ckpt/.pth` 路径；
- 本地参考 WAV 路径；
- 参考音频逐字文本。

## Reader Core

`/test` 页面支持：

- 手动文本；
- UTF-8 TXT；
- EPUB；
- 章节与片段切分；
- 连续播放；
- 单段预取；
- 角色、模型、参考语音选择。

Reader 只决定“使用哪个已注册资产”，不直接处理模型路径。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

自动测试不需要真实 GPT-SoVITS、参考音频或模型文件。

真实连续阅读链路：

```powershell
.\.venv\Scripts\python.exe .\tests\continuous_read_test.py --voice march-7th
```

## 安全边界

服务当前面向本机与可信局域网。不要直接把 9880/9881 暴露到公共网络。

## 后续主线

当前并行工作分成两条：

1. 在 HSR-Voice-Archive-Builder 中试听并人工筛选真实 Reference Pack；
2. 在 Character Voice Service 中稳定多角色、多模型、多参考语音注册与调度。

等真实 Reference Pack 验证“不同参考语音确实能产生有意义的情绪差异”后，再进入：

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
