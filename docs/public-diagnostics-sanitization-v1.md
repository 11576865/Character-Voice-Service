# CVS public diagnostics sanitization v1

## Why this is a core contract issue

The public `GET /v1/models` previously caught filesystem and manifest
exceptions from `model_registry.resolve_model` and inserted
`item["error"] = str(exc)`. A missing manifest generates an OS error
with its **absolute host Model Root path**, which was returned to any
unauthenticated discovery client. The top-level public route also
returned raw Registry exception strings.

The public `GET /health` previously combined `{"engine": engine_id,
**adapter.health()}`; the IndexTTS, GPT-SoVITS and third-party Sidecar
health adapters can return raw `str(exc)` or arbitrary fields from
external backends. A third-party or badly configured process could
accidentally disclose host file paths, runtime URL, model filenames or
active request identity.

The public voice profile error path also previously reflected
`ValueError` messages from validation, and the model/reference
selection error path exposed the raw `KeyError` value.

## Minimal corrective boundary

- `model_registry.list_models()` preserves model IDs, status and public
  metadata but returns a stable `error="model metadata unavailable"`
  and `error_code="model-metadata-unavailable"` if resolving installed
  metadata fails. No local exception string is reflected.
- A top-level Registry failure returns HTTP 500 with
  `"Model Registry unavailable"`, without local filesystem detail.
- Public `/health` retains the service status, engine ID, a constrained
  backend status identifier, boolean `busy`, valid finite
  `active_seconds`, and short simple `precision` where present.
  It does **not** forward arbitrary sidecar fields, raw exception
  messages, model paths or `active_request_id`. Errors use a generic
  message; a thrown health exception is an observational offline state.
- Public profile validation and model/reference selection errors
  return stable generic details that do not expose the internal
  exception value.

No changes were made to administrator authentication, Registry
transaction semantics, model hashes, TTS generation or the shared
GPU lifecycle protocol. The public discovery endpoints remain public.

## Verification

`tests/test_public_diagnostics_sanitization.py` includes seven test
functions with additional parametrized cases. It tests a real
temporary Model Registry whose previously valid manifest is deleted,
corrupted or modified; verifies that public discovery never emits
that private absolute path or the damaged file content; and tests
top-level registry errors, Sidecar-injected diagnostics, invalid
health metrics, thrown health errors, voice validation errors, and
untrusted model/reference lookup exceptions.

Linux full pytest and Windows targeted pytest must succeed on the
final PR head before merge.

## Remaining operational and security boundaries

This hardening does **not** claim the complete public attack surface
has been reviewed. Other public routes, optional integrations,
administrator-visible logs, legitimate model metadata, request limits,
network controls and dependencies require their own audits.
`GET /health` is still a liveness-oriented endpoint and cannot prove
all model processes or GPUs are ready. Real end-to-end actor/voice
identity and GPU model workloads remain FIELD-PENDING.
