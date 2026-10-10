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
- The **single** `references[]` entry maps the served voice's stable
  `reference_id` to one frozen **reference-pool item ID**. That item must
  not be in the held-out test split. Because `generation_revision` binds
  one reference selection, v1.1 rejects multiple reference entries for a
  single evaluation record; use separate evaluation IDs for distinct prompts.
- The explicit `decision` is still a **human/authorized evaluator's
  decision**, not an automatic quality claim. `promotable` must be a JSON
  boolean. `pending` and `rejected` are nonpromotable.

The record is created once and retains its `created_at` on identical
retries. Changing an existing record under the same ID is rejected. Create
a new `evaluation_id` for an intentional new evaluation.

## Promotion boundary

A model can be promoted only if:

1. Model Registry status is `validated` or an existing `default`,
   the model is physically present and not quarantined, and promotion
   revalidates its current immutable manifest **and every artifact SHA-256**.
   This asset integrity check still runs with the explicit internal
   `require_evaluation=False` override. A caller using a nondefault
   Model Root must pass `model_root` explicitly.
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

## Registry reliability and identity collisions

Model Root scan now rejects **all copies of an ambiguous model ID**, even
if their manifest bytes are identical in different directories. Existing
entries with a duplicated ID become quarantined and non-present. A stale
`defaults` mapping is preserved for operator inspection, but
`default_model_id` and discovery do not advertise it as an active default.
A path that resolves outside Model Root, including a `model.json` symlink,
is reported as an invalid scan item instead of terminating the scan.

Registry snapshots are written through uniquely named staging files and
fsynced before atomic replacement. This protects readers from truncated
JSON on interrupted writes; it is **not** a multi-writer transaction. Callers
must serialize simultaneous registry mutations, and operators must still
verify power-loss recovery and storage durability on their host filesystem.

### Limitations

The integrity recheck occurs immediately **before promotion**; it does not
lock weight files against later modification. Runtime-serving integrity
continues to depend on Model Root and the serving resolver, and real Windows
filesystem failure injection remains outside local unit coverage.
