# IndexTTS-2.5 sidecar

This branch introduces the first integration boundary for IndexTTS-2.5 without
putting IndexTTS dependencies into the Character Voice Service environment.

## Runtime boundary

- IndexTTS checkout/environment: external to CVS, using its own `.venv`
- launcher calls `INDEX_TTS_ROOT\.venv\Scripts\python.exe` by absolute path; the active shell's Conda/Python environment is not authoritative
- sidecar: `127.0.0.1:9882`
- CVS: `127.0.0.1:9881`
- Reader continues to call CVS only
- inference concurrency: 1
- QwenEmotion: disabled by default
- DeepSpeed: disabled
- custom CUDA kernel: disabled
- BF16: enabled by default
- output: WAV / 22050 Hz

Start the sidecar on Windows:

```cmd
scripts\run_index_tts_sidecar.cmd
```

Override the checkout when needed:

```cmd
set INDEX_TTS_ROOT=D:\path\to\index-tts
scripts\run_index_tts_sidecar.cmd
```

Health:

```text
GET http://127.0.0.1:9882/health
GET http://127.0.0.1:9882/capabilities
```

The sidecar intentionally knows nothing about Character IDs. CVS resolves the
voice profile and reference audio, then sends the resolved reference path and
generation parameters to the sidecar.

## Shared model and VoiceBinding

Model Registry v1.1 now supports `scope: "shared"` engine models with no
character owner and, for external runtimes such as IndexTTS, no duplicated
local model artifacts. Character-specific serving identity is represented by a
separate VoiceBinding.

Existing runtime-only profile aliases remain accepted as a compatibility bridge:

```json
"index-tts-2.5": {
  "name": "IndexTTS 2.5 shared runtime",
  "engine": "index-tts",
  "version": "2.5",
  "parameters": {
    "emo_alpha": 1.0
  }
}
```

Select it with `model_id: "index-tts-2.5"`. When a formal VoiceBinding exists,
the binding takes precedence over the transitional alias and resolves the shared
engine model plus speaker/emotion reference roles.

Migrate an existing IndexTTS alias after pulling the current main branch:

```cmd
.venv\Scripts\python.exe scripts\migrate_index_tts_binding.py --voice march-7th
```

This creates:

- a shared `index-tts-2.5` Model Registry v1.1 manifest;
- a persistent VoiceBinding for the character;
- no duplicate IndexTTS checkpoints inside CVS.

The existing profile alias is intentionally retained for rollback compatibility.

## Residency policy

IndexTTS is not added to the Supervisor in this change. On 12 GB GPUs it should
be treated as mutually exclusive with GPT-SoVITS until an explicit residency
policy is implemented and validated.


## Windows PATH / Conda note

Do not install IndexTTS dependencies into Conda `base` and do not rely on an activated `base` environment to launch the sidecar. Use the repository launcher above. For diagnostics, run `scripts\environment_doctor.ps1` from Character Voice Service. This keeps TTS Python environments independent from system FFmpeg and from unrelated video tools.
