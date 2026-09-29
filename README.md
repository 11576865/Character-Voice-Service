# Character Voice Service

Character Voice Service 是一个刚刚起步的本地角色语音基础设施项目。

它目前仍很小，目标也暂时保持克制：在角色、语音模型、参考语音和不同 TTS 引擎之间提供一个相对稳定的中间层，让上层应用不必直接依赖某一种模型或某一套运行方式。

未来，它可能逐步发展成一套面向角色语音的统一服务层，用于管理角色身份、模型与参考资产、评测结果和引擎路由，并通过稳定 API 为 Reader、Android TTS、Agent、批量配音或其他客户端提供语音能力。

当前阶段仍以验证架构边界、整理资产与建立可复现的多引擎基础为主。

相关项目：

- [Character Voice Reader](https://github.com/11576865/Character-Voice-Reader) — 独立的文档阅读与语音朗读客户端。
