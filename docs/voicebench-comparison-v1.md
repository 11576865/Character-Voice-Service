# voicebench A/B audit v1 — complete evidence before comparison

This is an **offline descriptive audit** of two CVS voicebench run directories.
It is **not** a voice-quality evaluator, performance benchmark ranking, blind
listening study, or artifact attestation service.

The comparison layer depends on frozen corpus verification (#14),
provenance-gated evaluation identities (#15), and voicebench generation runs
(#16). A and B may target **different engines and different model versions**,
provided the controlled source inputs match.

## Required inputs and scope

Both run directories must have been completed by the same voicebench run format:

- `run.json` has `status=complete` and `quality_evaluation=not_performed`;
- run dataset SHA-256 and dataset ID equal the **reverified** frozen corpus;
- all `test-recorded` item IDs are present, with status `ok`;
- each `audio/<item_id>.wav` exists as a real file, is readable uncompressed
  PCM, and matches recorded SHA-256, byte count and PCM metadata;
- recorded duration and elapsed/RTF values are internally consistent;
- both runs have the **same character voice**, source reference **item ID**
  and SHA-256, and speech speed. The serving `reference_id` may be different
  if each engine's profile maps to the same frozen original reference WAV;
- generation and model revision identities must be explicit and nonempty.

A partial run or unexpected staged `.wav.part` output is not comparable.
The tool neither regenerates outputs nor repairs corrupted manifests.

## Example

After collecting two successful `voicebench` runs using the **same frozen**
manifest and original reference WAV:

```powershell
python -m scripts.compare_voicebench `
  --manifest data\benchmarks\march7-en-v1.json `
  --audio-root D:\cvs-private\march7-wav `
  --run-a data\voicebench-runs\march7-gsv-run-001 `
  --run-b data\voicebench-runs\march7-indextts-run-001 `
  --output data\voicebench-reports\march7-a-v-b-001.json
```

Inputs above are examples only; **no actual paired runs or audio results**
were available to this implementation. The report file is create-only.
Repeat the comparison with a different `--output` for a separate report.
Generated WAVs and report JSON should stay in **private** `data/`.

## What the report measures

For each original test item the report includes both output WAV digests,
individual CVS request IDs, speech-request elapsed time, generated audio
duration and **RTF = elapsed time / generated duration**. Each run includes
total elapsed seconds, total generated seconds, aggregate RTF and median
per-item RTF. The report has a deterministic `report_sha256` over its
canonical contents. That fingerprint detects ordinary accidental edits
*only when compared with a trusted prior copy*; it is **not a signature**.

`identical_output_bytes` detects exact identical WAV bytes for a paired
text. It is not a speaker-similarity or transcription-correctness metric.

## Interpretation and exclusions

These are **observed end-to-end API request timings**, including any server
waiting and startup delays, not pure model inference timings. Hardware, loaded
model state, warmup, thermal limits, memory pressure and concurrency are not
yet controlled. A single run comparison does **not** warrant the claim that
one engine is generally faster.

The tool does **not** compute WER, CER, MOS, speaker embedding similarity,
prosody, intelligibility, or human preference. It never writes Evaluation
Registry decisions, `promotable=true`, or model defaults. Run manifests
themselves are not digitally signed; a party able to rewrite all source
records and artifacts may falsify their asserted provenance.

A future controlled multi-repeat benchmark and source-linked **blind
listening** dataset may build on this paired report. Quality comparison
requires genuine independent measurements.

## Crash-safe voicebench generation update

`voicebench` now uses a two-stage WAV publication protocol:

1. fsync `audio/<id>.wav.part`;
2. journal `status=prepared` with the expected WAV hash and measurements;
3. publish `audio/<id>.wav`, then journal `status=ok`.

If interrupted between steps 2 and 3, `--resume` first verifies the
original corpus, live reference and serving identity, checks the prepared
artifact hash, and finishes the publication **without regenerating** it.
If interrupted after publication but before step 3's final journal update,
the already-published WAV is revalidated and marked complete instead.

A crash *before* the durable prepared journal can leave an untracked staged
file. That state fails closed and requires explicit operator inspection
or a new run directory; it is never silently deleted or overwritten.
Only one writer may own a run directory.
