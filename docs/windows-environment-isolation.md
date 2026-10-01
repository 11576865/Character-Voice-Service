# Windows 环境隔离

Character Voice Service、GPT-SoVITS、IndexTTS 等引擎不应依赖当前 PowerShell 的隐式 `PATH` 状态。

## 原则

1. **CVS 自己使用项目 `.venv`**，启动脚本通过绝对路径调用 `.venv\Scripts\python.exe`。
2. **每个语音引擎拥有自己的运行环境**。IndexTTS sidecar 使用 `index-tts\.venv\Scripts\python.exe`，不要把 IndexTTS、GPT-SoVITS、CosyVoice、F5-TTS 等依赖安装到 Conda `base`。
3. **Conda base 不承担生产运行时职责**。它可以保留，但建议关闭自动激活，避免它把旧 `ffmpeg.exe`、`python.exe` 或 DLL 目录插到系统 `PATH` 前部。
4. **引擎通过 sidecar / adapter 与 CVS 通信**，而不是把不同引擎的 Python、PyTorch、CUDA、FFmpeg 依赖混到同一环境。
5. FFmpeg 属于媒体工具链，不应由某个 TTS 环境“顺便”决定系统默认版本。

## 推荐操作

查看当前环境：

```powershell
.\scripts\environment_doctor.ps1
```

如果确认不希望每次打开终端都自动进入 `(base)`：

```powershell
.\scripts\environment_doctor.ps1 -FixBaseAutoActivate
```

它等价于：

```powershell
conda config --set auto_activate_base false
```

之后重新打开终端。

## 启动方式

CVS：

```cmd
scripts\run_server.cmd
```

IndexTTS：

```cmd
scripts\run_index_tts_sidecar.cmd
```

这两个脚本都使用各自环境中的 Python 绝对路径，因此即使外层终端当前激活了其他 Python 环境，也不会把推理进程错误地启动到那个环境。

## 新引擎约定

后续接入 GPT-SoVITS / CosyVoice / F5-TTS / Fish / Chatterbox 时采用同一模式：

```text
Character Voice Service (.venv)
        |
        +-- HTTP adapter -> GPT-SoVITS sidecar (own env)
        +-- HTTP adapter -> IndexTTS sidecar   (own env)
        +-- HTTP adapter -> CosyVoice sidecar  (own env)
        +-- HTTP adapter -> F5-TTS sidecar     (own env)
        `-- ...
```

任何 engine sidecar 的启动器都应：

- 指定该引擎自己的 Python 可执行文件绝对路径；
- 显式设置工作目录；
- 只向子进程传递需要的环境变量；
- 不通过 `conda activate base` 启动；
- 不要求修改全局 `PATH`；
- 不把引擎自己的 FFmpeg/DLL 路径永久写入系统环境变量。

这样可以避免一个语音项目安装的旧 FFmpeg 或 DLL 影响字幕压制器、其他语音引擎和普通终端。
