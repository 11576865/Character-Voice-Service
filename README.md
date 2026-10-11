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

## Model Registry 并发事务

[Model Registry transaction boundary v1](docs/model-registry-transactions-v1.md) 使用跨进程系统文件锁，将扫描、生命周期状态修改、模型晋升和退休操作的完整「读取—修改—持久化」过程串行化。锁文件不应人工删除。此机制防止协作进程的丢失更新，但不替代受控模型目录、完整性检查或数据库事务。

## IndexTTS 推理卡死隔离

[IndexTTS hang containment v1](docs/indextts-hang-containment-v1.md) 为单模型推理加上了忙碌时快速拒绝、健康状态中的运行时长、可配置的 240 秒默认推理看门狗，以及超限时的独立 Sidecar 进程级恢复。它不是细粒度取消接口；真实 GPU/系统监督器恢复仍需验收。

## 引擎异常退出与重启限流

[Runtime Supervisor crash-loop containment v1](docs/runtime-crash-loop-containment-v1.md) 保留托管引擎的异常退出码与日志路径，并对重复启动失败采用 5–60 秒自动重试退避。正常停机不触发退避；管理员显式重启可以覆盖退避，但不会清除上一条退出记录。本机制为进程内防护，不替代真实 GPU 故障验收。

## 跨进程 GPU 独占组租约

[Runtime GPU slot ownership v1](docs/runtime-slot-cross-process-v1.md) 为托管引擎增加操作系统文件锁：同一 `CVS_DATA_DIR` 的多个 CVS 进程不再仅凭健康探测同时启动占用同一 `exclusive_group` 的语音引擎。租约随已观测的托管子进程生命周期持有，异常终止和正常停止会释放。它不能证明 CVS 父进程崩溃后没有遗留仍在占用 GPU 的孤儿引擎，也不自动协调独立的 System Supervisor。

## 异常退出后的引擎所有权恢复

[Managed runtime orphan-owner receipt v1](docs/runtime-orphan-ownership-receipt-v1.md) 在 GPU 组锁保护下持久化启动意图、子进程 PID 与进程创建身份。CVS 父进程崩溃后，新的管理实例不会仅因锁释放而假定 GPU 已空闲；若旧引擎仍存活、启动记录不完整或无法验证，就拒绝重复启动并要求人工核查。当前适用 Linux/Windows 托管引擎，不代表所有 GPU 子进程或外部 Supervisor 都已纳入同一协议。

## 外部 System Supervisor 激活去重

[External activation single-flight v1](docs/runtime-external-activation-single-flight-v1.md) 对同一外部 `service_key` 的激活命令增加跨进程互斥和持久化待确认记录。超时不会自动重复投递激活命令；实时健康恢复后可按请求身份清理。陈旧的外部状态文件不再覆盖实时健康状态。此机制不代替外部 Supervisor 对 PID、GPU 资源及子进程树的生命周期管理。

## 托管引擎进程树隔离

[Managed runtime process-tree containment v1](docs/runtime-process-tree-containment-v1.md) 针对配置 `exclusive_group` 的托管引擎，使用 Linux/POSIX 独立会话与进程组、Windows kill-on-close Job Object 包装器，在停止时清理后代进程。直接 PID 意外退出但后代仍存活时保留 GPU 租约和所有权记录，拒绝自动接管。真实 GPU、逃逸子进程与外部 System Supervisor 仍需单独验证。

## CVS 控制进程异常终止的恢复验证

[Controller crash fault injection v1](docs/runtime-parent-crash-fault-injection-v1.md) 使用独立 Python 解释器、真实子进程树与强制终止，验证 CVS 父进程死亡后旧引擎仍存活时，新实例不会因为 GPU 组文件锁已释放就重复启动；同时验证启动意图中断与 Linux `/proc` 权限不可读时的失败封闭行为。该测试不代表真实 GPU 或外部 System Supervisor 的验收。
