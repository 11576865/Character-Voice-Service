# 架构说明

Character Voice Service（CVS）是角色语音基础设施层。

```text
Client Applications
        |
        | Character Voice Contract
        v
Character Voice Service
        |
        +-- Character Registry
        +-- Model Registry / Model Root
        +-- Reference Assets
        +-- Evaluation Registry
        +-- Serving Policy
        `-- Engine Adapters
                |
                +-- GPT-SoVITS
                `-- future engines
```

## 项目边界

CVS 负责角色身份、模型与参考资产、评测、引擎能力、路由和稳定语音 API。

Reader、Book Library、EPUB/TXT/DOCX、阅读进度、离线书籍和整书生成属于独立的
[Character Voice Reader](https://github.com/11576865/Character-Voice-Reader)，不属于 CVS Core。

## 角色与模型

角色是用户级稳定身份；模型是可独立管理的推理资产。当前 GPT-SoVITS 模型通过 Model Root 和不可变 `model.json` manifest 管理。

Model Registry 负责发现、校验、索引和生命周期；新模型不会因为“最新”而自动成为默认模型。

## Reference

Reference 是角色语音资产，不等同于模型本身。客户端通过稳定 `reference_id` 选择或请求自动策略，不直接读取本机 WAV 路径。

## Engine Adapter

客户端不直接理解 GPT-SoVITS、IndexTTS 或其他引擎的私有参数。引擎差异由 Engine Adapter 吸收。

当前 main 只实现 GPT-SoVITS adapter；其他引擎仍属于后续工作。

## 外部契约

当前稳定入口以 `POST /v1/audio/speech` 为核心，发现接口包括：

```text
GET /health
GET /v1/voices
GET /v1/models
GET /v1/engines
```

详细语义见 [Character Voice Contract v1](character-voice-contract-v1.md)。

## 安全边界

GPT-SoVITS 等推理引擎建议只监听本机地址。CVS 的管理接口使用独立管理令牌；客户端应用不应把该令牌直接暴露给浏览器。
