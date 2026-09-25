# Character Registry

Character Voice Service treats a **character** as a stable identity that can own multiple GPT-SoVITS model versions and multiple reference voices.

The registry is intentionally file-based for the current local-first stage. Model weights and reference WAVs stay wherever they already live on disk; the repository stores only local paths in ignored real profile files.

## Profile v2

A real profile keeps the stable character ID in its filename:

```text
voices/march-7th.json
```

Example structure:

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
      "gpt_weights": "D:/Models/March7th/march-e15.ckpt",
      "sovits_weights": "D:/Models/March7th/march_e8_s96.pth"
    },
    "downloaded-v2pro": {
      "name": "Downloaded v2Pro",
      "engine": "gpt-sovits",
      "version": "v2pro",
      "gpt_weights": "D:/Models/March7th/downloaded.ckpt",
      "sovits_weights": "D:/Models/March7th/downloaded.pth"
    }
  },

  "default_reference": "neutral-01",
  "references": {
    "neutral-01": {
      "name": "Neutral 01",
      "audio": "D:/References/March7th/neutral-01.wav",
      "text": "Exact official transcript of the reference audio.",
      "language": "en",
      "emotion": "neutral",
      "intensity": 0.4,
      "quality": "good"
    },
    "surprised-01": {
      "name": "Surprised 01",
      "audio": "D:/References/March7th/surprised-01.wav",
      "text": "Exact official transcript of the surprised reference.",
      "language": "en",
      "emotion": "surprised",
      "intensity": 0.8,
      "quality": "good"
    }
  },

  "parameters": {
    "top_k": 15,
    "top_p": 1.0,
    "temperature": 1.0
  }
}
```

## Stable IDs

- Character ID: profile filename stem, such as `march-7th`.
- Model ID: key inside `models`, such as `self-400-v2pro`.
- Reference ID: key inside `references`, such as `surprised-01`.

IDs are machine-stable. `name` fields are display labels and may change without changing API identity.

## Managed and externally loaded models

A model with both `gpt_weights` and `sovits_weights` is **managed** by Character Voice Service.

Before synthesis the service uses GPT-SoVITS' official control endpoints:

```text
GET /set_sovits_weights?weights_path=...
GET /set_gpt_weights?weights_path=...
POST /tts
```

The active managed weight pair is cached. Repeated requests to the same model do not reload it.

Model switching and synthesis share one service-side lock. This is deliberate: GPT-SoVITS has one active weight pair, so two concurrent requests must not interleave a model switch with another request's synthesis.

Legacy profiles remain valid. They normalize to one model named `loaded`, representing whatever model was already loaded in GPT-SoVITS before Character Voice Service started.

There is one safety restriction: after Character Voice Service switches GPT-SoVITS to a managed model, it will not silently route back to an unregistered externally loaded model. The service no longer knows which weights should be restored. Register explicit weight paths or restart both processes before using that legacy/external model again.

## Reference selection

A character may register many references. Reference metadata can include:

- exact reference transcript;
- language;
- optional auxiliary reference WAVs;
- emotion label;
- 0..1 intensity;
- quality label;
- optional inference parameter overrides.

At this stage the service only performs **explicit reference selection**. Automatic emotion routing is a later layer.

## API

Existing requests remain valid:

```json
{
  "voice": "march-7th",
  "input": "Hello."
}
```

They use the character's default model and default reference.

Explicit selection:

```json
{
  "voice": "march-7th",
  "model_id": "self-400-v2pro",
  "reference_id": "surprised-01",
  "input": "What are you doing here?",
  "speed": 1.0
}
```

`GET /v1/voices` returns safe registry metadata for UI selection. It does **not** expose local model paths, local reference paths, or reference transcripts.

## Parameter precedence

Inference parameters are merged in this order:

```text
character defaults
  < model overrides
  < reference overrides
```

Later layers can therefore tune one model or one reference without duplicating the full character configuration.

## Legacy compatibility

The original profile shape remains accepted:

```json
{
  "name": "March 7th",
  "reference_audio": "D:/ref.wav",
  "reference_text": "Exact transcript.",
  "reference_language": "en",
  "target_language": "en",
  "parameters": {}
}
```

No immediate migration is required for an already working single-model installation.

## Current boundary

Implemented here:

- many characters on disk;
- many registered model versions per character;
- safe GPT-SoVITS model switching;
- many explicitly selectable references per character;
- backward-compatible legacy profiles;
- reader model/reference selectors;
- no exposure of private local paths through `/v1/voices`.

Not implemented here:

- automatic import of an HSR Reference Pack;
- automatic text-to-emotion classification;
- long-form emotion continuity planning;
- multi-GPU/model worker pools;
- loading several GPT-SoVITS models in VRAM simultaneously.
