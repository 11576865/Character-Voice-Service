# CVS immutable Model Root import hardening

This change addresses concrete source-level failure modes in the legacy
GPT-SoVITS asset migration and model registration path. It does **not**
install or execute any TTS engine and it does not replace Model Registry's
runtime revision validation.

## Correct model identity

The imported `model_id` uses a readable prefix and the 12-hex-character
fingerprint derived from both GPT and SoVITS weight SHA-256 digests.

The old implementation truncated the entire `model_id` to 127 characters,
potentially discarding all or part of the final weight-derived suffix when
the role or source model name was long. Different weight pairs could thus
produce the same truncated identifier.

The new algorithm **reserves the complete suffix first**, truncates only
the descriptive prefix, and tests this independently of filesystem path
limits. The 12-hex fingerprint is a compact namespace suffix, not a
cryptographic collision proof. Model Registry continues to validate the
full artifact SHA-256 values during scan and resolution.

## Create-only asset publication

For each imported weight:

1. Check that the destination lies in the configured Model Root and no
   child component from that root is a symbolic link. A symlink used
   explicitly for the Model Root itself is permitted.
2. If the artifact already exists, accept it only if its bytes match the
   required SHA-256 and it is not a symlink.
3. Otherwise copy to a uniquely named, exclusively created temporary file,
   compute the SHA-256 during copy, fsync the file and independently hash it.
4. Use a filesystem hard link to publish the verified staged file under
   the immutable destination name **without overwriting an existing
   artifact**, then remove the temporary link.

The immutable `model.json` uses the same write/flush/fsync/create-only
publication approach. A repeated import may differ in the
`lifecycle.created_at` timestamp, but **not** in name, language, engine,
serving parameters, source artifact digests, capabilities or any other
manifest field. Changes require a deliberate new model revision/identity;
they are not silently applied to an existing immutable directory.

The import is still a **resumable multi-file operation**, not one atomic
transaction across both weights and `model.json`. An interrupted import
may leave one already-verified immutable weight but no manifest, and a
subsequent retry must verify and reuse that weight. Operators should not
manually edit the model directory to force a retry.

## Platform and adversarial limits

Hard-link publication is intended for local filesystems supporting
`os.link` (notably NTFS and POSIX filesystems). If unsupported, the
operation **fails closed**; CVS does not silently downgrade to an
overwrite-capable copy or rename.

Filesystem preflight is not a descriptor-relative operation and is not
sufficient to defend against a hostile local process swapping directories
between checks. The immutable Model Root must remain administratively
controlled; do not expose it as an untrusted writable directory. The
directory entry itself is not explicitly fsynced for power-loss
durability on every platform.

Linux pytest and Windows Python regression jobs cover long identity
truncation, changed metadata, symlink destinations, staged-publish
failure, source mutation, and idempotent retries. Their actual CI
outcomes must be inspected after PR submission. Real GPT-SoVITS weight
import on user hardware remains a separate acceptance step.

Related infrastructure work: [Evaluation Provenance](evaluation-provenance-v1.md)
and [voicebench](voicebench-v1.md).
