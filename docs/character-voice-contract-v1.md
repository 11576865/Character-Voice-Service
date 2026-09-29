# Character Voice Contract v1

This document defines the current client-facing boundary of Character Voice Service.

## Request

Primary endpoint:

```http
POST /v1/audio/speech
```

Minimal request:

```json
{
  "voice": "march-7th",
  "input": "Hello."
}
```

Optional fields:

- `model_id`: select a registered model alias/identity exposed for the character.
- `reference_id`: select a registered reference; `"auto"` requests the current automatic reference policy.
- `speed`: positive playback/generation speed factor.
- `response_format`: currently `wav`.

The compatibility field `model` is not the authoritative engine-selection mechanism. Engine selection follows the resolved model and its Engine Adapter.

Clients must not send local checkpoint paths, local reference WAV paths, CUDA/runtime paths, or engine-private loading commands.

## Discovery

```text
GET /health
GET /v1
GET /v1/voices
GET /v1/models
GET /v1/engines
```

Public discovery must not expose local model paths, local reference paths, or reference transcripts.

## Response metadata

Successful speech generation returns `audio/wav` and may include:

```text
X-CVS-Request-ID
X-CVS-Voice
X-CVS-Model
X-CVS-Engine
X-CVS-Model-Revision
X-CVS-Generation-Revision
X-Selected-Reference
X-Reference-Reason
```

`X-CVS-Generation-Revision` is the current request-level revision identifier for the resolved serving configuration. Its exact inputs may evolve while v1 remains young; clients should treat it as an opaque identifier rather than reconstructing it.

## Administrative boundary

Model lifecycle mutation and protected reference preview are administrative operations and require the CVS admin token.

Browser-facing applications should normally keep this token server-side.

## Outside this contract

The following are application concerns, not CVS Core concerns:

- document parsing;
- EPUB/TXT/DOCX reading;
- book libraries;
- reading progress and bookmarks;
- whole-book generation jobs;
- offline book packaging;
- Reader UI.

The current first-party application for those concerns is Character Voice Reader.

## Engine boundary

Engine-specific checkpoint formats, speaker prompts, emotion controls, tokenizer/runtime details and model-loading procedures live behind Engine Adapter implementations.

A future shared zero-shot engine and a voice-bound fine-tuned engine should both be able to serve this contract without requiring client changes.
