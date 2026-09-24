# 架构说明

## 当前边界

Character Voice Service 是 GPT-SoVITS 与客户端之间的适配层。

```text
客户端
  ↓ HTTP
Character Voice Service
  ↓ localhost HTTP
GPT-SoVITS
  ↓
WAV
```

服务层负责：

- 稳定 API；
- 角色配置解析；
- GPT-SoVITS 参数映射；
- 错误转换；
- 调试阶段的 WAV 保存；
- 后续缓存、路由、模型切换等扩展。

客户端不需要知道 GPT-SoVITS 的参考音频字段、prompt 字段或推理参数细节。

## 安全边界

当前建议：

- GPT-SoVITS：仅监听 `127.0.0.1:9880`。
- Character Voice Service：按需监听 `0.0.0.0:9881`，仅用于可信局域网。
- 不配置路由器端口转发。
- 校园网、公共 Wi-Fi 或不可信网络不应直接暴露此端口；后续远程访问优先考虑受控 VPN/overlay network。
