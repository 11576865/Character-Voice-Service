# Evaluation Registry v1.1: provenance-linked promotion evidence

CVS Model Registry historically allowed a model to be promoted when **any**
matching evaluation record contained `decision.status = validated` and
`decision.promotable = true`. A legacy record could contain an arbitrary
`model_sha256` string and no frozen dataset reference.

v1.1 retains read access to v1.0 files, but **v1.0 is no longer sufficient to
authorize model promotion**. Existing defaults are not rewritten or demoted;
this gate applies to subsequent promotion requests.

## Declared evaluation record

The evaluation must be stored under private `data/evaluations/<evaluation_id>.json`
and created through `write_evaluation` (e.g. by a future voicebench runner).
Illustrative structure, not an actual quality measurement:

```json
{
  "schema_version": "1.1",
  "evaluation_id": "march7-gsv-v4-holdout-001",
  "model_id": "march7-gsv-v4-a",
  "model_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "generation_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "dataset": {
    "dataset_id": "march7-en-v1",
    "dataset_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "test_item_ids": ["test-001", "test-002"],
    "references": [
      {"reference_id": "character-ref-neutral", "item_id": "ref-001"}
    ]
  },
  "decision": {"status": "validated", "promotable": true}
}
```

- `model_revision` is the **immutable Model Registry manifest SHA-256**,
  not a display version string and not an arbitrary file path.
- `generation_revision` is the opaque request-level revision returned by
  CVS speech resolution/generation. It identifies a declared synthesis
  configuration, but this gate currently checks its valid shape, not that
  audio was actually generated under it.
- `dataset_sha256` must match the frozen manifest at
  `data/benchmarks/<dataset_id>.json`.
- `test_item_ids` must cover **exactly** every `test-recorded` item in
  that manifest. This first contract is for a *complete* recorded holdout.
- The single `references[]` entry maps the served voice's stable
  `reference_id` to a frozen **reference-pool item ID**. That item must
  not be in the held-out test split. This preserves a traceable mapping
  from serving selection to original WAV hash without exposing local paths.\n  Version 1.1 deliberately requires **exactly one speaker reference per record**\n  because the opaque `generation_revision` includes that reference identity.\n  Use separate evaluations for different reference selections.
- The explicit `decision` is still a **human/authorized evaluator's
  decision**, not an automatic quality claim. `promotable` must be a JSON
  boolean. `pending` and `rejected` are nonpromotable.

The record is created once and retains its `created_at` on identical
retries. Changing an existing record under the same ID is rejected. Create
a new `evaluation_id` for an intentional new evaluation.

## Promotion boundary

A model can be promoted only if:

1. Model Registry status is `validated` or an existing `default`.
2. The record uses schema v1.1 and has a validated/promotable decision.
3. `model_revision` equals the **currently registered** model revision.
4. The private frozen dataset manifest exists and its canonical fingerprint
   equals the recorded `dataset_sha256`.
5. Every recorded test ID is the exact held-out test set and the selected
   references resolve to non-test reference-pool members.

Missing/stale/malformed records and missing datasets fail closed. The
administrative `POST /v1/admin/models/{model_id}/promote` uses this gate.
The existing explicit internal `require_evaluation=False` override is
not used by the HTTP administrative endpoint.

### Distinct evidence levels

This gate establishes **declared provenance consistency**. It does not:
- independently verify that source WAVs are still unchanged (run
  `scripts.freeze_benchmark_dataset verify` with `--audio-root`);
- attest that the engine actually generated the test outputs;
- measure WER, speaker embedding similarity, prosody, MOS, RTF, TTFA or
  long-form continuity;
- verify that a human genuinely listened to the results or that a
  validated decision meets any objective quality threshold.

Do not call this a full benchmark runner or a verified quality promotion.
A future benchmark executor should capture output hashes, measured metrics,
human review evidence and precise `generation_revision` per run.
