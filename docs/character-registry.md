# Character Registry and Model Registry

Character Voice Service treats a **character** as a stable identity. Character identity, model identity, reference assets, evaluation history and serving policy are separate concerns.

The current architecture follows this boundary:

```text
Character Registry
      │ stable model_id
      ▼
Model Registry / Model Root
      │ engine + immutable artifacts
      ▼
Engine Adapter
      │
      ▼
Speech Engine
```

## Character profile v2

A real profile keeps the stable character ID in its filename:

```text
voices/march-7th.json
```

A registered model is referenced by stable `model_id`. Local aliases remain useful for client-facing labels and compatibility:

```json
{
  "schema_version": 2,
  "name": "March 7th",
  "target_language": "en",
  "default_model": "local-v4",
  "models": {
    "local-v4": {
      "name": "Local v4",
      "model_id": "march7-en-gsv-v4-local-0123456789ab"
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

Legacy entries containing explicit `gpt_weights` + `sovits_weights` remain supported during migration.

## Model Root

Models are immutable artifacts. The default local Model Root is `data/models` and can be changed with `CVS_MODEL_ROOT`.

```text
data/models/
└── march-7th/
    └── gpt-sovits/
        └── march7-en-gsv-v4-local-0123456789ab/
            ├── model.json
            └── artifacts/
                ├── gpt.ckpt
                └── sovits.pth
```

The directory name is not the identity. Identity is carried by `model_id`, the manifest and SHA-256 values.

A GPT-SoVITS manifest contains, at minimum:

```json
{
  "schema_version": "1.0",
  "model_id": "march7-en-gsv-v4-local-0123456789ab",
  "voice_id": "march-7th",
  "engine": {
    "name": "gpt-sovits",
    "engine_version": "v4",
    "adapter_api_version": "1"
  },
  "artifacts": {
    "gpt": {
      "path": "artifacts/gpt.ckpt",
      "sha256": "..."
    },
    "sovits": {
      "path": "artifacts/sovits.pth",
      "sha256": "..."
    }
  },
  "training": {},
  "runtime": {},
  "capabilities": {
    "fine_tuned_model": true,
    "zero_shot": true,
    "emotion": "reference"
  },
  "lifecycle": {
    "status": "candidate"
  }
}
```

Artifact paths are relative to the immutable model directory. Scanner validates SHA-256 before registration.

## Scanner and local index

`data/model-registry.json` is a local scan index and lifecycle store. It is **not** the canonical model manifest.

Scanner may automatically:

```text
discover
validate
hash
register
```

It must not automatically:

```text
promote-to-default
```

If an already registered immutable manifest changes, the entry is quarantined rather than silently accepted.

## Lifecycle

```text
discovered
   ↓
candidate
   ↓
validated
   ↓
default
   ↓
retired

candidate
   ↓
quarantined
```

A newly imported model is a candidate. Production default is a manually promoted, validated `model_id`, never "latest checkpoint".

The local registry can hold one default `model_id` per `voice_id`. Character routing honors that promoted default when the character profile contains the corresponding model.

## Evaluation Registry

Evaluation records live under `data/evaluations` and are versioned independently of model artifacts. A promotion may require a record whose decision is:

```json
{
  "decision": {
    "status": "validated",
    "promotable": true
  }
}
```

The current implementation provides the Evaluation Registry and promotion gate. A full automatic benchmark generator / blind-listening UI remains later work.

## Engine Adapter

Character Voice Service owns:

- character identity;
- model registration and lifecycle;
- reference assets;
- evaluation records;
- request routing;
- stable API.

Each engine owns:

- checkpoint format;
- tokenizer and internal architecture;
- model loading;
- inference implementation;
- engine-specific parameters.

The current adapter contract exposes engine identity, capabilities, health, model load/unload and synthesis. Only the GPT-SoVITS adapter is implemented today; other engines can be added without changing Character IDs or the Reader API.

## Legacy migration

Run:

```powershell
.\scripts\migrate_model_registry.cmd
```

For each v2 profile that still contains explicit GPT-SoVITS weight paths, migration:

1. locates the current `.ckpt/.pth` files;
2. copies them into Model Root;
3. verifies SHA-256;
4. creates an immutable `model.json`;
5. registers the model as `candidate`;
6. creates `.json.pre-model-registry.bak` once;
7. replaces the two machine-specific paths with stable `model_id`.

Legacy source discovery checks:

- `CVS_MODEL_SOURCE_ROOTS`;
- `CVS_GPT_SOVITS_ROOT`;
- a sibling `GPT-SoVITS` directory.

## Reference selection

References remain character-owned assets. Parameter precedence is:

```text
character defaults
  < model manifest serving parameters
  < character model overrides
  < reference overrides
```

## Current boundary

Implemented:

- stable character IDs;
- multiple models and references per character;
- immutable Model Root manifests with SHA-256 verification;
- Model Registry scanning and lifecycle state;
- quarantine on manifest mutation;
- Evaluation Registry storage and promotion gate;
- manual default promotion/retirement API;
- EngineAdapter boundary with GPT-SoVITS implementation;
- legacy inline GPT-SoVITS compatibility and migration;

Not yet implemented:

- automatic Evaluation generation;
- blind A/B listening management UI;
- automatic candidate benchmark execution;
- adapters for IndexTTS / CosyVoice / F5 / Fish / other engines;
- multi-engine scheduler and resident-model pool.
