# Character Voice Service

Character Voice Service 是本地角色语音基础设施。当前后端使用 **GPT-SoVITS**，通过稳定 HTTP API 把角色资产、模型权重、参考语音与 Reader 等客户端分离。

当前主线：

```text
训练好的 GPT-SoVITS 模型
        +
Reference Pack / Reference Library
        ↓
Character Registry
        ↓
Character Voice Service
        ↓
Reader / 后续其他客户端
```

## 当前能力

### Character Registry

- 一个角色可以注册多个 GPT-SoVITS 模型版本；
- 一个角色可以注册多个参考语音；
- 请求可显式选择 `model_id` 与 `reference_id`；
- `scripts/import_reference_pack.cmd` 可将 HSR Reference Pack 的音频、文本和情绪标签导入一个已存在的 v2 角色；重复运行按原始语音身份更新。
- `reference_id: "auto"` 可试用保守的文本情绪选参考；默认仍为角色注册表中的参考。
- 未指定时使用角色默认模型与默认参考语音；
- 注册了 `.ckpt/.pth` 路径的模型可自动调用 GPT-SoVITS 官方权重切换接口；
- 旧版“单模型 + 单参考语音”配置继续兼容；
- `GET /v1/voices` 不暴露本机模型路径、参考 WAV 路径或参考音频逐字文本。

详细设计见 [Character Registry](docs/character-registry.md)。

### Reader

首页 `/` 和兼容入口 `/test` 支持：

- 手动文本；
- UTF-8 TXT；
- EPUB；
- 章节与片段切分；
- 连续播放；
- 单段预取；
- 本地阅读进度；
- 同一 TXT/EPUB 的继续阅读 / 从头开始；
- 上一/下一章；
- 上一/下一段；
- 当前段落高亮与正文跟随；
- 角色、模型、参考语音选择。
- 当前段落重新生成，并在此浏览器保存至多七个可比较的段落版本。
- 深色听读界面为默认主题；导入、朗读、正文和进阶工具分区显示，手机窄屏可操作。

导入前先在 `voices/` 创建真实角色配置，并登记至少一个模型和默认参考。双击 `scripts/import_reference_pack.cmd`，输入 Reference Pack 文件夹及角色 ID。导入内容写入本机 `references/` 和该角色 JSON；两处都被 Git 忽略。仓库不附带真实模型或角色语音。

如果手里是已有的 `voices/角色名/reference_audios/英语/emotions/【情绪】逐字文本.wav` 目录，使用 `scripts/import_voice_folders.cmd`。第一次输入包含各角色子目录的 `voices` 路径，第二次输入 GPT-SoVITS 根目录（其下应有 `GPT_weights_v4` 与 `SoVITS_weights_v4`）。脚本先预览每个角色及参考数量，确认后才复制 WAV 并登记各自的 v4 模型权重。原目录不会修改；再次运行按文件来源更新，不会增加重复条目。文件名中 `【】` 后的完整内容被当作准确参考文本，导入前请核对。没有 WAV 的角色会跳过；缺少或不唯一的模型权重会明确报错。新参考质量先标为 `unrated`，自动情绪选择不会使用它们，需试听确认后再标记质量。重复导入时，音频和文本未改变的参考保留人工评级；音频或文本改变则恢复 `unrated`。`config/reference_reviews.json` 只记录已确认参考的音频与文本校验值，另一台机器导入相同文件时可恢复评级，校验不匹配则仍为 `unrated`。新角色优先用“中立”参考作为默认项，没有时选“其他”。

### 私人书库与成品

- 首页可将当前 TXT、EPUB 或文档保存到电脑书库；书籍、原文件、生成音频及任务状态保存在 `data/`，不会进入 Git。
- 先在书库登录，再用“整本书一键生成”。任务逐段落盘；中断后再次启动会复用配置、文本均未变化的已完成片段。
- 旁白用顶部角色，点选正文段落可指定其他角色和参考语音。明确的“角色名：”前缀可生成待确认的角色建议。
- Markdown、DOCX 文档配音提取标题和正文；可试听所选文字，整篇生成后导出 WAV。
- 完整生成的书可下载到当前设备离线听读，也可导出 EPUB 3 Media Overlays 同步成品。手机断网、电脑关闭后只能播放已经下载的音频。
- 浏览器离线数据可能被清理；同步 EPUB 是独立备份。整书导出使用 ffmpeg 转换 MP3，需让 `ffmpeg` 可在命令行找到。
- 离线书库显示已下载音频的估计大小和本站点占用；删除本设备副本前会再次确认。
- 联网后按上次同步时间核对本设备与电脑的阅读位置；只有一端改变时自动同步，两端都改变且位置不同时让用户选择。
- `data/admin-token.txt` 会在首次启动时自动生成。双击 `scripts/show_library_token.cmd` 可在本机查看，并在网页书库面板登录；也可用 `CVS_ADMIN_TOKEN` 环境变量覆盖。登录会话为七天。整书任务、私人书库及参考 WAV 试听都需要登录。
- 新增的 `/epub-prototype` 是独立的 foliate-js EPUB 排版映射验证页。正式 Reader 仍使用现有文本视图，直到段落定位和高亮实测通过。

本仓库包含 foliate-js 的固定 Git 子模块。克隆后运行 `git submodule update --init --recursive`，否则 EPUB 组件验证页不可用。组件采用 MIT 许可，许可文本保留在 `vendor/foliate-js/LICENSE`。

### 实测边界

自动情绪参考、长篇连续规划和多角色建议均为可选试用功能，需用真实角色和 [试听验收表](docs/listening-evaluation.md) 判断是否适合默认启用。当前仓库只有示例角色；本机实际模型、手机断网、第三方阅读 App 的 EPUB 兼容性需分别验收。`CVS_SAVE_GENERATED_WAV=1` 可恢复每次实时合成都另存 Music 的旧行为；默认关闭，避免与段落版本及书库音频重复占用空间。

Reader 架构见 [Reader 架构](docs/reader-architecture.md)，进度规则见 [Reader 状态](docs/reader-state.md)，EPUB 解析见 [EPUB Text Source](docs/epub-source.md)。

## 架构

```text
TXT / EPUB / 手动文本
          ↓
      Reader Core
          ↓
Character Voice Service :9881
          ↓
   Character Registry
      ↙          ↘
 Model Registry   Reference Library
      \          /
       GPT-SoVITS :9880
             ↓
            WAV
```

GPT-SoVITS 建议只监听 `127.0.0.1:9880`。局域网只按需暴露 Character Voice Service。

## 角色配置

真实角色继续使用文件名 stem 作为稳定 voice ID：

```text
voices/march-7th.json
```

Profile v2 示例：

```json
{
  "schema_version": 2,
  "name": "March 7th",
  "target_language": "en",

  "default_model": "self-400-v2pro",
  "models": {
    "self-400-v2pro": {
      "name": "Self 400 v2Pro",
      "engine": "gpt-sovits",
      "version": "v2pro",
      "gpt_weights": "D:/Models/March7th/march.ckpt",
      "sovits_weights": "D:/Models/March7th/march.pth"
    }
  },

  "default_reference": "neutral-01",
  "references": {
    "neutral-01": {
      "name": "Neutral 01",
      "audio": "D:/References/March7th/neutral-01.wav",
      "text": "Exact official transcript.",
      "language": "en",
      "emotion": "neutral",
      "intensity": 0.4,
      "quality": "good"
    }
  }
}
```

旧配置仍然可以直接运行：

```json
{
  "name": "March 7th",
  "reference_audio": "D:/ref.wav",
  "reference_text": "Exact transcript.",
  "reference_language": "en",
  "target_language": "en"
}
```

旧配置中的模型被视为 GPT-SoVITS 已经手工加载的外部模型，不要求立即迁移。

## 模型切换

受管理模型同时配置：

```text
gpt_weights
sovits_weights
```

Service 在需要时调用：

```text
GET /set_sovits_weights
GET /set_gpt_weights
POST /tts
```

当前模型会缓存，同一模型连续请求不会重复加载。

GPT-SoVITS 一个进程只有一组活动权重，因此“模型切换 + 一次语音合成”会被串行保护，避免并发请求在错误模型上生成。

如果 Service 已经切换到受管理模型，它不会静默回到一个没有登记权重路径的旧版外部模型。此时需要注册该模型路径，或重启 GPT-SoVITS 和 Service。

## Windows 首次设置

需要 Python 3.10 或更高版本：

```powershell
.\scripts\first_setup.cmd
```

复制模板：

```powershell
Copy-Item .\voices\example.json .\voices\march-7th.json
```

真实角色配置默认被 Git 忽略，不要提交本机模型路径、参考音频或其他私有资产。

## 日常启动

先启动 GPT-SoVITS：

```powershell
cd C:\Users\27619\Downloads\GPT-SoVITS-v2pro-20250604-nvidia50
.\runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

再启动本服务：

```powershell
.\scripts\run_server.cmd
```

Reader：

```text
http://127.0.0.1:9881/test
```

## API

使用默认模型与默认参考：

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "input": "This is a test.",
  "response_format": "wav",
  "speed": 1.0
}
```

显式选择角色资产：

```json
{
  "model": "gpt-sovits",
  "voice": "march-7th",
  "model_id": "self-400-v2pro",
  "reference_id": "surprised-01",
  "input": "What are you doing here?",
  "response_format": "wav",
  "speed": 1.0
}
```

成功时返回 `audio/wav`。

## 测试

Python 测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

自动测试不需要真实 GPT-SoVITS、模型文件或参考 WAV。

浏览器模块测试页：

- `tests/reader_core_test.html`
- `tests/epub_source_test.html`
- `tests/reader_state_test.html`

真实连续朗读：

```powershell
.\.venv\Scripts\python.exe .\tests\continuous_read_test.py --voice march-7th
```

## 安全边界

- GPT-SoVITS 建议仅监听 `127.0.0.1:9880`；
- Character Voice Service 当前面向本机与可信局域网；
- 不直接把 9880/9881 暴露到公共网络；
- 当前没有公网认证、账户系统或多租户隔离。

## 接下来的主线

当前工作分成两条并行线：

1. 在 HSR-Voice-Archive-Builder 中试听并筛选真实 Reference Pack；
2. 在 Character Voice Service 中完成多角色、多模型、多参考语音注册和调度。

等真实参考语音实验确认不同 reference 能稳定产生有意义的情绪差异后，再进入：

```text
Reference Library
      ↓
Emotion Router
      ↓
Continuity Planner
      ↓
长篇 EPUB 情绪轨迹
```

Android System TTS 保留为后续第三方 Android App 兼容层，不是当前主线。
