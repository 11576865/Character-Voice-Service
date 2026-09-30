# 本机端到端验收记录 — 2026-09-30

## 验收范围

本次验证针对 Character Voice Reader 从 Character Voice Service（CVS）分离后的本机真实生成链路：

```text
Character Voice Reader :9890
        ↓
Character Voice Service :9881
        ↓
Model Registry
        ↓
GPT-SoVITS :9880
        ↓
Reference / text processing
        ↓
WAV
```

目标不是 benchmark，也不是音质评测；只确认 post-split 架构在真实 Windows / CUDA / 模型环境中可以完成一次端到端角色语音生成。

## 验收环境

- Windows
- GPT-SoVITS root: `D:\BaiduNetdiskDownload\GPT-SoVITS`
- GPT-SoVITS Python: `D:\BaiduNetdiskDownload\GPT-SoVITS-env\python.exe`
- Character Voice Service: `D:\BaiduNetdiskDownload\Character-Voice-Service`
- GPT-SoVITS API: `127.0.0.1:9880`
- CVS: `127.0.0.1:9881`
- Character Voice Reader: `127.0.0.1:9890`
- Python: 3.10.21
- torch: 2.11.0+cu128
- torchaudio: 2.11.0+cu128
- torchcodec: 0.11.1+cpu

## Model Registry 迁移

运行：

```powershell
$env:CVS_GPT_SOVITS_ROOT = "D:\BaiduNetdiskDownload\GPT-SoVITS"
.\scripts\migrate_model_registry.cmd
```

迁移结果：

```text
converted_models: 9
```

被迁移的角色：

- anaxa
- aventurine
- furina
- march-7th
- silver-wolf
- sparkle
- sunday
- trailblazer-female
- trailblazer-male

其中 `march-7th` 为：

```text
converted: 1
unchanged: 1
```

迁移后的 GPT / SoVITS 权重从旧机器绝对路径转换为 CVS Model Root 下的 immutable artifacts，并通过稳定 `model_id` 由角色配置引用。

## 运行时补齐项

迁移后的真实 TTS 调用暴露出以下 GPT-SoVITS 环境缺失项，并逐项补齐：

1. `torchcodec 0.11.1+cpu`
2. NLTK `cmudict`
3. NLTK `averaged_perceptron_tagger_eng`

`cmudict` 因当前网络环境的 Fake-IP / SSRF 安全检查无法通过 NLTK Downloader 正常下载，因此使用 NLTK 官方数据包手工安装到：

```text
D:\BaiduNetdiskDownload\GPT-SoVITS-env\nltk_data
```

实际 `torchaudio.load()` 已对 Character Voice Service 中的真实 reference WAV 验证成功：

```text
shape = torch.Size([1, 134120])
sample_rate = 44100
```

## 端到端请求

经 Character Voice Reader 代理：

```http
POST http://127.0.0.1:9890/v1/audio/speech
Content-Type: application/json
```

请求：

```json
{
  "voice": "march-7th",
  "input": "This is a post-split integration test.",
  "response_format": "wav",
  "speed": 1.0
}
```

结果：

```text
HTTP 200
Content-Type: audio/wav
Content-Length: 195884
```

关键响应头：

```text
x-selected-reference = folder-fbe178ca97e4
x-reference-reason = default
x-cvs-voice = march-7th
x-cvs-model = march-7th-gpt-sovits-v4-v4-local-eb4eb5160d7d
x-cvs-engine = gpt-sovits
x-cvs-model-revision = 9cd4767598c87485fcaea672f10f54173722fdb81053eb30ffadd62d52bc236d
x-cvs-generation-revision = 872c8b81b68d7581ca7ed2bff908e3b774152201929056d2a82684a36dabf368
x-cvs-request-id = f25270553c3f42c89a5db5d69dce7338
```

输出文件：

```text
C:\Users\27619\AppData\Local\Temp\cvs-post-split-test.wav
195884 bytes
```

WAV 已实际播放确认。

## 验收结论

2026-09-30，post-split 本机基线通过：

- Reader :9890 → CVS :9881 通信成功；
- CVS → GPT-SoVITS :9880 通信成功；
- Model Registry 迁移后的模型制品实际参与推理；
- 旧 GPT-SoVITS 权重绝对路径不再是该角色生成链路的依赖；
- Character Voice Contract 的 `X-CVS-Generation-Revision` 能经 Reader 正确透传；
- 最终返回可播放 WAV。

本记录仅证明当前 GPT-SoVITS 路径的端到端可用性，不代表 IndexTTS、多引擎路由、自动 benchmark、生产级调度或公网部署已经完成。
