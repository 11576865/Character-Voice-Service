# Character Voice System Graph

Character Voice System 不以文件是否位于同一目录来定义“整合”。物理路径只是实现属性；系统关系由稳定身份、所有权和生命周期连接定义。

## 关系链

```text
Character / Reference
        |
        v
VoiceBinding
        |
        v
Model
        |
        v
Engine
        |
        v
Runtime
        |
        +-- lifecycle owner -> System Supervisor
        +-- resource        -> GPU exclusive group
        `-- dependencies   -> Python / source / model store / sidecar / other runtime assets
```

这条链把语义层和执行层连接起来。

- Character / Reference：角色及其参考语音资产；
- VoiceBinding：角色如何绑定到共享或角色专属模型；
- Model：Model Registry 中的不可变模型身份；
- Engine：GPT-SoVITS、IndexTTS 等推理引擎；
- Runtime：一个实际可运行的引擎实例身份；
- Dependency：仅为该 Runtime 服务的 Python、源码树、模型仓库、sidecar 等依赖；
- Lifecycle Owner：实际拥有启动/停止权限的 Supervisor；
- Resource：例如单 GPU 互斥组 `gpu-0`。

## 路径不是身份

例如下面两个事实可以同时成立：

```text
python.exe 物理位置:
D:\BaiduNetdiskDownload\GPT-SoVITS-env\python.exe

系统身份:
dependency:gpt-sovits-local/python-runtime
ownership:
engine-private
belongs_to:
runtime:gpt-sovits-local
```

不需要搬动 Python，也能让它在体系中不再“散落”。

## Runtime Registry

Runtime Registry 现在记录：

- `runtime_id`；
- `runtime_version`；
- `engine_id`；
- `lifecycle_owner`；
- endpoint / health；
- GPU 互斥组；
- owned dependencies；
- Runtime 配置修订号。

如果 Runtime 由 System Supervisor 管理，CVS 不直接启动该进程，只通过控制桥请求目标引擎进入 READY。

## Supervisor live state

System Supervisor 应原子写入：

```text
character_voice_supervisor\control\status.json
```

其中包含：

- Supervisor session；
- active engine；
- engine switch phase；
- 每个服务的 state / detail；
- worker PID；
- root PID；
- listener PID；
- `system_identity`。

CVS Runtime Status 会读取这份状态，因此 System Graph 的 Runtime 节点可以关联到真实进程，而不只依赖 HTTP health。

## API

管理员令牌保护：

```http
GET /v1/system/graph
GET /v1/system/voices/{voice_id}/trace
GET /v1/runtime
```

`/v1/system/graph` 返回完整节点/边关系和结构警告。

`/v1/system/voices/{voice_id}/trace` 从指定角色出发，仅保留其可达依赖链，用于回答“这个角色的一次生成最终依赖了什么”。

## 生成可追溯性

语音生成现在把 Runtime 身份加入 generation revision：

```text
voice
engine
runtime_id
runtime_revision
model_id
model_revision
binding_revision
reference revision
parameters
speed
```

响应头增加：

```text
X-CVS-Runtime
X-CVS-Runtime-Revision
```

因此同一个模型如果换了 Runtime 配置，其 `X-CVS-Generation-Revision` 也会改变。

## 结构警告

System Graph 会报告至少以下不一致：

- Model 使用了没有 Runtime 的 Engine；
- Engine 在语义层存在，但 Runtime Registry 未登记；
- VoiceBinding 指向未注册 Model；
- VoiceBinding 指向不存在的 Reference；
- Profile 无法读取；
- Model Registry / Binding Registry 无法读取。

这些警告不把“路径不在同一目录”视为问题。真正的问题是没有身份、没有所有权、没有依赖关系或没有生命周期控制。
