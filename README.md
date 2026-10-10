# Character Voice Service

Character Voice Service 是一个刚刚起步的本地角色语音基础设施项目。

它目前仍很小，目标也暂时保持克制：在角色、语音模型、参考语音和不同 TTS 引擎之间提供一个相对稳定的中间层，让上层应用不必直接依赖某一种模型或某一套运行方式。

未来，它可能逐步发展成一套面向角色语音的统一服务层，用于管理角色身份、模型与参考资产、评测结果和引擎路由，并通过稳定 API 为 Reader、Android TTS、Agent、批量配音或其他客户端提供语音能力。

当前阶段仍以验证架构边界、整理资产与建立可复现的多引擎基础为主。

相关项目：

- [Character Voice Reader](https://github.com/11576865/Character-Voice-Reader) — 独立的文档阅读与语音朗读客户端。


## Windows 环境隔离

Windows 上建议让每个语音引擎使用自己的 Python 运行环境，并关闭 Conda `base` 自动激活，避免旧版 FFmpeg、Python 或 DLL 路径污染其他项目。

诊断当前环境：

```powershell
.\scripts\environment_doctor.ps1
```

需要关闭 `base` 自动激活时：

```powershell
.\scripts\environment_doctor.ps1 -FixBaseAutoActivate
```

详细说明见 [docs/windows-environment-isolation.md](docs/windows-environment-isolation.md)。


生成本机 Runtime Registry：

```powershell
.\scripts\init_runtime_registry.ps1
```

然后启动 CVS：

```powershell
.\scripts\run_server.ps1
```

运行时状态可通过受管理员令牌保护的 `GET /v1/runtime` 查看（请求头 `X-CVS-Token`）。已登记且启用 `start_on_demand` 的引擎会在首次请求时由 Runtime Supervisor 使用各自的 Python 绝对路径启动。

## 固定角色语音评测数据集

运行跨引擎语音评测前，先使用 [`Benchmark Dataset v1`](docs/benchmark-dataset-v1.md) 将人工校对的原始 WAV/文本及 Train/Dev/Test/Reference 成员关系冻结为带 SHA-256 的私有 manifest。该步骤不修改原始 WAV，也不自动进行语音合成或质量评分。

评测登记与模型晋升另遵循 [Evaluation Provenance v1.1](docs/evaluation-provenance-v1.md)：历史 v1.0 评测可读取，但没有数据集指纹、模型修订和参考成员关系的记录不能再批准模型晋升。

## 语音生成路径实测（voicebench v1）

使用 [voicebench v1](docs/voicebench-v1.md) 对冻结数据集的完整 Test-recorded 集合逐条生成 WAV，固定角色/模型/参考身份，记录响应来源、输出哈希与 RTF，并支持中断恢复。需要本机 CVS 和真实引擎；成功生成不代表已通过声音质量评测，也不会自动允许模型晋升。

## 可复核的跨引擎运行对照

通过 [voicebench A/B audit v1](docs/voicebench-comparison-v1.md)，在重新校验冻结数据集和两次运行的输出 WAV 后，生成一份仅包含描述性生成性能与身份信息的对照报告。该报告不推断音质优劣，也不自动推动模型晋升。

## 模型资产与注册表完整性

模型晋升前重新验证 Model Root 中的原始 manifest 和全部权重 SHA-256；即使内部显式跳过评测检查，也不能跳过资产完整性检查。同一 `model_id` 对应多份物理清单时会隔离，失效的默认映射不再用于自动选型。详情见 [Evaluation Provenance v1.1](docs/evaluation-provenance-v1.md)。注册表采用原子快照写入，但仍要求调用方串行提交变更。
