# Character Voice Service

一个面向本地角色语音生成的轻量服务层。当前后端使用 **GPT-SoVITS**，通过稳定的 HTTP API 将客户端与具体语音模型实现分离。

## Milestone 0

完整目标是：让 Android 手机通过系统 TTS，稳定地让一个 GPT-SoVITS 角色连续朗读 20 段文字。

浏览器页面已扩展为基础 Reader，支持手动文本、UTF-8 TXT 和 EPUB、章节与片段切分、连续播放、单段预取及本地阅读进度。系统 TTS 的 Android 验收仍需在后续阶段完成。

## Milestone 1：Reader Core

`/test` 页面使用独立的文本来源、切分器、导航、进度、队列和播放器模块。手动输入、UTF-8 TXT 和 EPUB 都转换为统一的 `TextDocument`；阅读队列只保留当前音频和一个预取音频。实现边界见 [Reader 架构](docs/reader-architecture.md)，进度规则见 [Reader 状态](docs/reader-state.md)，EPUB 解析方式见 [EPUB Text Source](docs/epub-source.md)。

## 当前架构

```text
Android 系统 TTS / 浏览器测试页
            ↓ HTTP
Character Voice Service :9881
            ↓ localhost
      GPT-SoVITS :9880
            ↓
          WAV
```

GPT-SoVITS 建议只监听 `127.0.0.1:9880`，局域网只暴露本项目的适配层。

## 已实现

- `POST /v1/audio/speech`：OpenAI 风格的语音生成入口；
- `GET /health`：服务与 GPT-SoVITS 后端状态；
- `GET /v1/voices`：读取本地角色配置；
- `GET /test`：电脑或手机浏览器 Reader 页面；
- JSON 角色配置和 GPT-SoVITS 参数映射；
- WAV 返回与可选本地保存；
- 局域网监听。

## Windows 首次设置

需要 Python 3.10 或更高版本。首次运行：

```powershell
.\scripts\first_setup.cmd
```

脚本会检查可用的 Python、创建项目 `.venv`、安装 `requirements.txt`，并确认 `voices/` 中至少有一个有效的真实角色配置。`voices/example.json` 只是模板，不计入真实角色。

复制角色配置模板：

```powershell
Copy-Item .\voices\example.json .\voices\march-7th.json
```

文件名 stem 是稳定的 voice ID，例如 `march-7th.json` 对应 API 参数 `"voice": "march-7th"`。JSON 内的 `name` 是界面显示名称，例如 `"March 7th"`。然后填写参考音频路径和逐字参考文本；参考文本必须对应音频实际说出的内容。除 `example.json` 外的角色配置都被 Git 忽略，不要提交真实路径、参考音频或模型文件。

## 日常启动

先启动本机 GPT-SoVITS，再在仓库根目录运行：

```powershell
.\scripts\run_server.cmd
```

首次设置的 `.cmd` 入口会以仅对当前进程生效的方式调用 PowerShell，因此不需要修改系统执行策略；日常启动入口会直接使用项目 `.venv`。

浏览器 Reader 地址：`http://127.0.0.1:9881/test`。同一可信局域网内的手机可访问 `http://<电脑局域网IP>:9881/test`。导入 UTF-8 TXT / EPUB，或在折叠区粘贴文本并点击“使用粘贴文本”；选择角色后点击“开始”。TXT 会识别独立成段的章节标题；EPUB 显示书名、作者并按 spine 顺序阅读章节。页面提供章节列表、上一/下一章、上一/下一段，以及高亮和正文跟随。暂停、停止都会保留当前位置；停止会取消请求并清空预取，随后可继续阅读。再次选择同一个 TXT/EPUB 时，可选择“继续阅读”或“从头开始”。进度仅保存在当前浏览器的 localStorage；手动粘贴文本不做跨刷新恢复。

## API 示例

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "input": "This is a test from my phone.",
  "response_format": "wav",
  "speed": 1.0
}
```

发送到 `POST /v1/audio/speech`，成功时返回 `audio/wav`。

## 测试

完成首次设置后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

自动测试不需要真实 GPT-SoVITS、参考音频或模型权重。

Reader Core、EPUB 解析及阅读状态的浏览器模块测试页分别位于 `tests/reader_core_test.html`、`tests/epub_source_test.html` 和 `tests/reader_state_test.html`。可从仓库根目录运行 `python -m http.server 8765`，再打开相应的 `http://127.0.0.1:8765/tests/<测试页文件名>`。测试覆盖 TXT/EPUB 读取、章节与片段切分、队列状态、进度恢复和跳转清理。

### 连续阅读真实链路测试

启动 GPT-SoVITS 和本服务后，顺序生成 20 段测试文本：

```powershell
.\.venv\Scripts\python.exe .\tests\continuous_read_test.py --voice march-7th
```

如果 `/v1/voices` 仅返回一个有效角色，可以省略 `--voice`。测试会逐段校验 WAV，并将音频和 `report.json` 写入被 Git 忽略的 `test-results/continuous-read/`。

### 角色一致性测试

使用同一角色和同一句文本重复生成 10 次：

```powershell
.\.venv\Scripts\python.exe .\tests\voice_consistency_test.py --voice march-7th
```

输出保存在 `test-results/voice-consistency/`。报告记录 API 请求参数、`seed`、`temperature`、`top_k`、`top_p`、生成耗时、WAV 时长、文件大小和 SHA-256 差异。

GPT-SoVITS 输出具有采样随机性。该测试用于记录差异并确定参数基线，不自动判断音色好坏，也不作为质量评分。

## 安全边界

服务仅面向可信局域网。不要配置公网端口转发，也不要直接暴露在校园网、公共 Wi-Fi 或其他不可信网络中。

## 下一阶段

本轮工程基线完成后，再开始 Android TTS 接入，最终在真实阅读器中验证连续朗读 20 段文字。
