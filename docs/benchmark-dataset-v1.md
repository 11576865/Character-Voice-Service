# Benchmark Dataset v1: freeze original speech before comparing engines

CVS's Model Registry identifies model artifacts; its Evaluation Registry records
operator decisions. Neither is a substitute for a **frozen test dataset**.
This tool freezes **user-curated, sentence-level ORIGINAL WAV + human-reviewed
transcripts** without running ASR, modifying audio, assigning random splits, or
copying private speech into Git.

## Input and split ownership

Prepare a UTF-8 JSONL file with one explicit record per source WAV:

```json
{"id":"line-0001","audio":"session-a/0001.wav","text":"Example sentence.","language":"en","source":"ORIGINAL","split":"train","reference":true,"style":"neutral"}
{"id":"line-0002","audio":"session-a/0002.wav","text":"Another example.","language":"en","source":"ORIGINAL","split":"test-recorded","style":"surprised"}
```

`audio` is a POSIX-style path relative to `--audio-root`. The path must point to
an existing, nonempty, readable PCM WAV **within** that root. The only accepted
source type for this corpus is `ORIGINAL`; synthetic generations require a
separate dataset with different provenance rules. The tool never infers the
transcript or the emotion, and does not select references automatically.

Choose each split deliberately. Supported roles:

- `train`: optional role for adapting a model;
- `dev`: hyperparameter/checkpoint selection;
- `test-recorded`: original held-out audio used for evaluation;
- `reference`: optional exclusive split for fixed zero-shot speaker/style prompts.
  Alternatively, set `"reference": true` on a `train` or `dev` item to
  include it in the reference pool **without duplicating the WAV**. A
  `test-recorded` item can never be a reference.

A useful *planning target* for a 400-clip corpus is 300 train, 40 dev and 60
test clips, with a curated reference pool of 5–10 non-test clips. Reference
prompts may be selected from train/dev (via `reference: true`), avoiding a
misleading requirement for 405–410 distinct source files. Actual counts
must be grounded in the real inventory; the tool does **not** fabricate missing
clips or enforce these illustrative numbers. Never put test audio in train, dev
or reference. The tool rejects duplicate WAV paths **and byte-identical files**,
including two names referring to the same audio in different splits. It reports
identical normalized transcripts across splits for manual review; matching
text alone is **not** proof of identical audio.

## Freeze and verify

From the CVS repository root (using the CVS Python environment):

```powershell
python -m scripts.freeze_benchmark_dataset freeze `
  --input D:\cvs-private\march7-reviewed.jsonl `
  --audio-root D:\cvs-private\march7-wav `
  --output data\benchmarks\march7-en-v1.json `
  --dataset-id march7-en-v1

python -m scripts.freeze_benchmark_dataset verify `
  --manifest data\benchmarks\march7-en-v1.json `
  --audio-root D:\cvs-private\march7-wav
```

The manifest stores relative names, exact transcript bytes, PCM metadata,
SHA-256 of each original WAV and transcript, explicit split membership and a
canonical content fingerprint (`dataset_sha256`). It stores no machine-absolute
paths and contains no embedded audio. Running `freeze` again with identical
inputs is idempotent; a changed split/transcript/WAV cannot silently overwrite
an existing frozen manifest. Use a **new dataset ID and output filename** for an
intentional new dataset version. Keep original WAVs immutable in your private
archive. `verify` checks the manifest fingerprint and re-hashes *every* WAV.

Since private transcriptions are included, store the curated JSONL and frozen
manifest in private `data/` or other non-public storage, not the public repo.

## Evidence boundary and next step

This is **dataset provenance, not audio quality evaluation**. It does not run
GPT-SoVITS, IndexTTS or ASR; it does not compute WER, speaker similarity, RTF,
TTFA, MOS or prove that a model is promotable. It also does not create the
separate 60-sentence unseen-text set or 20-paragraph long-form set. Those should
be frozen as subsequent suites with their own identities before introducing an
engine-neutral voicebench runner. Evaluation records now bind to the `dataset_sha256`, immutable model
revision, full held-out `test_item_ids`, and selected reference IDs/item IDs.
See [Evaluation Provenance v1.1](evaluation-provenance-v1.md).
