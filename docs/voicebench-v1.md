# voicebench v1 — reproducible CVS synthesis evidence

This is the first **execution** layer above the frozen Benchmark Dataset v1
(PR #14) and provenance-gated Evaluation Registry v1.1 (PR #15). It invokes
the real CVS HTTP API; it does **not** install or invoke an inference engine
directly. GPT-SoVITS and IndexTTS can be tested in separate runs through the
same request/record format.

## Prerequisites and provenance

1. Freeze a private ORIGINAL WAV corpus with
   [Benchmark Dataset v1](benchmark-dataset-v1.md). It must include at
   least one `test-recorded` item and a non-test `reference` member.
2. Configure a **registered, immutable Model Registry model** for the voice.
   The server must expose non-null 64-digit
   `model_revision` and `generation_revision` through
   `POST /v1/audio/resolve`. Transitional unregistered profiles are
   intentionally not accepted as benchmark evidence.
3. Point CVS to an explicit `reference_id`, not `auto`, and specify the
   corresponding original reference `item_id` from the frozen manifest.
4. Use the local CVS admin token to read the registered voice reference
   via protected `GET /v1/voices/{voice_id}/references/{reference_id}/audio`.
   The runner SHA-256 compares its live WAV bytes with the frozen source.
   A missing or changed reference **fails before generation**. This first
   slice cannot benchmark a binding whose served reference cannot be
   retrieved through that endpoint.

The CLI refuses non-loopback addresses. Admin tokens are used only for local
reference verification and are never serialized to the output. Requests do not
inherit environment HTTP proxies or follow redirects.

## Windows example

Run CVS and its intended engine normally, then from the CVS repository:

```powershell
python -m scripts.run_voicebench `
  --manifest data\benchmarks\march7-en-v1.json `
  --audio-root D:\cvs-private\march7-wav `
  --output-dir data\voicebench-runs\march7-gsv-run-001 `
  --voice march-7th `
  --model-id self-400-v2pro `
  --reference-id neutral-01 `
  --reference-item-id line-0001 `
  --admin-token-file data\admin-token.txt
```

These names are **illustrative**. Replace them with the actual private
manifest and currently registered voice/model/reference IDs; do not infer
which reference WAV corresponds to which ID. Alternatively set
`CVS_ADMIN_TOKEN` in the shell (do not pass credentials in URLs).

If a run stopped mid-generation, retry **with identical parameters** plus
`--resume`. Never delete the previous run folder to bypass a provenance
mismatch; create a fresh run with a distinct output directory.

## Run manifest and validation boundary

The runner always re-verifies every frozen original WAV and the current
server-side reference audio before synthesis. It then calls
`POST /v1/audio/resolve` for each held-out text and checks every resolved
identity against the initial pinned model/engine/runtime/reference. It calls
`POST /v1/audio/speech` and checks all required provenance response headers.
Only a non-empty, readable uncompressed PCM WAV with complete frames is
written to `audio/<test_item_id>.wav`.

Private `run.json` contains:

- frozen `dataset_sha256`, voice, requested model/reference IDs and original
  reference WAV SHA-256;
- actual model, engine, runtime, binding and generation revisions;
- per-item status, output SHA-256, HTTP request ID, WAV duration/sample rate,
  end-to-end speech request elapsed seconds and **Real-Time Factor** (RTF =
  elapsed seconds / generated audio seconds);
- `running`, `partial` or `complete` and an explicit
  `quality_evaluation: not_performed`.

Output audio and the run directory belong in private `data/`, which is
Git-ignored. Checkpoint JSON is updated after each completed item. On resume,
already-completed WAV files are rehashed and not regenerated; missing or
mutated output, a new corpus, changed run parameters or changed server
identity causes failure. One run folder must have **one writer**.

A completed run certifies **generation-path evidence only**. It does *not*
assert intelligibility, voice similarity, expressiveness, transcription
accuracy, valid source/target text pairing or human acceptance. The runner
**never** writes `decision.promotable=true`, never auto-creates a validated
Evaluation Registry record, never promotes a model and never treats a fake
HTTP mock as real engine evidence.

Run one configuration at a time and retain distinct run directories to
later compare GPT-SoVITS and IndexTTS. Cross-engine score aggregation,
unseen-text/long-form suites, human listening records, WER, MOS, speaker
embedding comparison and statistical inference are separate later work.

## Two-stage publication and offline comparison

Generated audio is now journaled as `prepared` after a staged `.wav.part`
file is durably written and before it is published under the final `.wav`
name. `--resume` verifies and recovers the prepared output without
regeneration after an interruption in that window. Untracked staged files
fail closed and require operator investigation.

Two complete runs can be checked using the [offline A/B audit](voicebench-comparison-v1.md).
The comparison produces descriptive timings only and never a quality score.
