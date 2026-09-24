# Character Voice Service

一个面向本地角色语音生成的轻量服务层。当前后端使用 **GPT-SoVITS**，通过稳定的 HTTP API 将“客户端”与“具体语音模型实现”分离。

> 当前阶段：Milestone 0 / 局域网原型。目标不是一次性完成多角色、缓存、流式传输和 Android 本地推理，而是先把“手机 → Character Voice Service → GPT-SoVITS → 音频”这条路径稳定下来。

## 当前架构

```text
Android / 浏览器 / 其他客户端
            │
            │ HTTP
            ▼
Character Voice Service :9881
            │
            │ localhost
            ▼
      GPT-SoVITS :9880
            │
            ▼
          WAV
```

GPT-SoVITS 建议继续只监听 `127.0.0.1:9880`，局域网只暴露本项目的适配层。

## 已实现

- `POST /v1/audio/speech`：OpenAI 风格的语音生成入口
- `GET /health`：服务与 GPT-SoVITS 后端状态
- `GET /v1/voices`：列出本地角色配置
- `GET /test`：手机/电脑浏览器测试页面
- JSON 角色配置
- GPT-SoVITS 参数映射
- WAV 返回与可选本地保存
- 局域网监听

## 目录

```text
Character-Voice-Service/
├── server/
│   ├── app.py
│   ├── config.py
│   └── backends/
│       └── gpt_sovits.py
├── voices/
│   └── example.json
├── references/
├── web/
│   └── index.html
├── tests/
├── docs/
├── scripts/
│   └── run_server.ps1
├── requirements.txt
└── .gitignore
```

## 角色配置

复制：

```text
voices/example.json
```

为：

```text
voices/default.json
```

然后修改其中的参考音频路径和逐字参考文本。参考文本必须对应参考音频实际说出的内容；它不是给模型的自然语言指令。

示例：

```json
{
  "name": "default",
  "reference_audio": "C:/path/to/reference.wav",
  "reference_text": "The exact sentence spoken in the reference audio.",
  "reference_language": "en",
  "target_language": "en",
  "parameters": {
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0,
    "text_split_method": "cut5",
    "repetition_penalty": 1.35,
    "sample_steps": 32
  }
}
```

真实角色配置、参考音频、模型权重和生成缓存默认不应提交到 GitHub。

## 启动

先启动 GPT-SoVITS：

```powershell
.\runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

然后在本仓库根目录启动 Character Voice Service。

如果使用 GPT-SoVITS 整合包的 Python：

```powershell
C:\path\to\GPT-SoVITS\runtime\python.exe -m server.app
```

默认：

- Character Voice Service：`0.0.0.0:9881`
- GPT-SoVITS：`127.0.0.1:9880`
- 生成 WAV 调试副本：Windows“音乐”目录

浏览器测试：

```text
http://127.0.0.1:9881/test
```

手机在同一局域网时：

```text
http://<电脑局域网IP>:9881/test
```

## API 示例

```json
POST /v1/audio/speech

{
  "model": "gpt-sovits",
  "voice": "default",
  "input": "This is a test from my phone.",
  "response_format": "wav",
  "speed": 1.0
}
```

返回 `audio/wav`。

## 当前范围

当前版本刻意不处理：

- 自动情绪/场景选择
- 多参考音频自动路由
- 多模型热切换
- 流式音频
- N+1 段落预取
- 音频缓存
- Android 本地 GPT-SoVITS
- 公网暴露
- Wake-on-LAN

这些功能在基础链路经过连续阅读测试后再增加。

## 下一阶段

1. 验证多个角色配置。
2. 用测试网页完成手机端连续请求。
3. 开发 Android 系统 TTS Engine。
4. 在真实阅读器中连续朗读 20 段文本。
5. 再评估缓存、预取、流式传输和离线 fallback。
