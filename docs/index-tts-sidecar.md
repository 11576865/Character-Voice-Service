# IndexTTS-2.5 sidecar

This branch introduces the first integration boundary for IndexTTS-2.5 without
putting IndexTTS dependencies into the Character Voice Service environment.

## Runtime boundary

- IndexTTS checkout/environment: external to CVS
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

## Transitional profile binding

Until shared engine models and VoiceBinding are represented by Model Registry
v1.1, a voice profile may contain a runtime-only model alias:

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

Select it with `model_id: "index-tts-2.5"`. This is intentionally a bridge for
real-machine integration, not the final ownership model. The final architecture
remains Engine Model + Voice Binding + Reference Assets.

## Residency policy

IndexTTS is not added to the Supervisor in this change. On 12 GB GPUs it should
be treated as mutually exclusive with GPT-SoVITS until an explicit residency
policy is implemented and validated.
