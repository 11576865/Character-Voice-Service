# 架构说明

## 项目定位

Character Voice Service 是角色语音资产与客户端之间的服务层。它不再假定“一个服务只有一个角色、一个模型、一个参考音频”。

当前职责：

```text
训练好的 GPT-SoVITS 模型
        +
Reference Library
        ↓
Character Registry
        ↓
Character Voice Service
        ↓
Reader / 后续 Android TTS / 其他客户端
```

## 角色资产边界

一个角色由稳定 character ID 标识，例如：

```text
march-7th
```

角色可以拥有多个模型版本与多个参考语音：

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

模型权重与参考 WAV 保留在用户本机原位置。Character Voice Service 只在本地角色配置中注册路径，不复制模型资产进仓库。

详细 schema 见 [Character Registry](character-registry.md)。

## 服务调用

```text
Reader
  ↓ POST /v1/audio/speech
Character Voice Service :9881
  ↓ 解析 character / model / reference
Character Registry
  ↓
GPT-SoVITS :9880
  ↓
WAV
```

请求至少需要：

```json
{
  "voice": "march-7th",
  "input": "Hello."
}
```

可选显式指定：

```json
{
  "voice": "march-7th",
  "model_id": "self-400-v2pro",
  "reference_id": "surprised-01",
  "input": "Hello."
}
```

若未指定，则使用角色的默认模型与默认参考语音。

## GPT-SoVITS 模型切换

注册了 `gpt_weights` 与 `sovits_weights` 的模型由 Service 管理。

切换流程：

```text
选择 model_id
    ↓
如果已是当前模型 → 不重载
    ↓
否则：
GET /set_sovits_weights
GET /set_gpt_weights
    ↓
POST /tts
```

由于一个 GPT-SoVITS 进程只有一组活动权重，模型切换和语音合成在 Service 内串行保护，避免并发请求交叉到错误模型。

旧版 profile 继续兼容。旧版模型被视为“外部已加载”；在 Service 已主动切换到受管理模型后，不会静默回到无法确定权重路径的外部模型。

## Reader

Reader 已支持：

- 手动文本；
- UTF-8 TXT；
- EPUB；
- 章节与片段切分；
- 单段预取；
- 阅读进度；
- 章节/段落跳转；
- 当前段高亮；
- 角色、模型、参考语音选择。

Reader 不读取本机 `.ckpt/.pth` 路径；它只使用 Character Registry 暴露的安全 ID。

## 后续情绪层

当前只支持显式 reference 选择。后续层次：

```text
Reference Library
        ↓
Emotion Router
        ↓
Continuity Planner
        ↓
Reader Queue
```

Reader 负责判断“这段文字应该是什么情绪”；Character Voice Service 负责将 `character + emotion + intensity` 落到该角色可用的参考语音。自动情绪路由要等真实 Reference Pack 的听感实验确认后再实现。

## 安全边界

- GPT-SoVITS 仅监听 `127.0.0.1:9880`。
- Character Voice Service 按需监听 `0.0.0.0:9881`，仅用于可信局域网。
- `GET /v1/voices` 不返回本机模型路径、参考 WAV 路径或参考逐字文本。
- 真实角色配置被 Git 忽略。
- 当前不提供公网认证、用户账户或多租户隔离。
