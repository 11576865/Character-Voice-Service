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
        +-- Runtime Registry
        +-- Runtime Supervisor
        `-- Engine Adapters
                |
                +-- GPT-SoVITS
                +-- IndexTTS
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

当前实现包含 GPT-SoVITS 与 IndexTTS adapter。Adapter 负责推理协议；Runtime Registry / Runtime Supervisor 负责“用哪一个本机运行时、怎样启动、是否已经健康、是否与其他 GPU 引擎互斥”。两层职责分离。

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


## Runtime Registry 与 Runtime Supervisor

这里的 **Runtime Registry（运行时注册表）** 不是 Windows Registry。它只是一个有 schema 的本机配置目录：用稳定的 engine ID 映射到该引擎自己的 executable、工作目录、启动参数、健康检查地址和资源策略。

机器私有配置位于：

```text
config/runtimes.local.json
```

该文件不提交 Git。仓库只保存 `config/runtimes.example.json`。

Runtime Supervisor 读取这张表，用**绝对可执行文件路径**启动 sidecar，并清理父进程遗留的 Conda/Python 环境变量。它支持：

- 按需启动（`start_on_demand`）；
- health probe；
- start / stop / restart；
- 子进程日志；
- CVS 退出时停止自己拥有的 sidecar；
- `exclusive_group` 资源互斥。目前可把 GPT-SoVITS 与 IndexTTS 都标为 `gpu0`，避免 12 GB GPU 上两个大模型同时常驻。

如果同一互斥组中的另一个引擎由 Supervisor 启动，它会先被停止；如果另一个引擎是外部手工启动的，Supervisor 不会擅自杀死它，而是返回冲突。

首次生成本机配置：

```powershell
.\scripts\init_runtime_registry.ps1
```

诊断：

```powershell
.\scripts\environment_doctor.ps1
```

管理接口需要 CVS admin token：

```text
GET  /v1/runtime
POST /v1/admin/runtime/{engine_id}/start
POST /v1/admin/runtime/{engine_id}/stop
POST /v1/admin/runtime/{engine_id}/restart
```

当 `/v1/audio/speech` 选择了一个已注册且设置 `start_on_demand: true` 的引擎时，CVS 会在合成前确保该 sidecar 已健康；未注册的旧式手工运行方式仍保留兼容。


## System Graph

系统整合以稳定身份、所有权和依赖关系为准，不以文件系统共址为准。Character / Reference、VoiceBinding、Model、Engine、Runtime、Runtime Dependency、Lifecycle Owner 和 GPU Resource 被连接为一张可查询关系图。

详细说明见 [System Graph](system-graph.md)。
