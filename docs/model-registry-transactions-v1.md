# Model Registry transaction boundary v1

CVS Model Registry already writes JSON snapshots using a uniquely named,
fsynced staging file and `os.replace`. That protects readers from a
partially written snapshot but **does not serialize independently launched
administrative commands**. Two processes could each read registry version A,
make unrelated changes and both publish; the later publish erases the first.

## Supported writer contract

`scan_model_root`, `set_status`, `promote_model` and `retire_model`
now acquire one advisory **cross-process exclusive registry lock** before
entering their read-modify-write operation. Scan holds the lock across
directory enumeration, manifest and weight verification, and JSON
publication. This also prevents an older slow scan from publishing its
inventory **after** a newer completed scan.

- POSIX: `fcntl.flock(LOCK_EX | LOCK_NB)` with bounded retry.
- Windows: `msvcrt.locking(LK_NBLCK, 1)` on a one-byte persistent lock file.
- The canonical registry path determines the lock filename:
  `<registry-path>.lock`. Aliases/symlinks to the same actual file
  acquire the same lock. Registry reads and final snapshot publication
  resolve the same path.
- A lock acquisition times out after 120 seconds by default and raises
  `TimeoutError`. Administrators should retry, not delete the lock.
- The OS releases the exclusive lock on file descriptor close or process
  termination. The **lock file itself stays on disk**. Deleting it while
  another process owns it can create two independent lock identities.
- The per-snapshot temp JSON is unique and cleaned on write failures.
  A transaction that raises before publication does not commit a changed
  in-memory registry.

`load_registry`, `resolve_model`, `default_model_id` and
`list_models` remain readers of an atomically published snapshot.
They are not long-duration readers of a held database transaction and may
observe a snapshot preceding a simultaneous state change.

`save_registry` also holds the writer lock for its raw snapshot publication.
It is a **low-level full replacement API**: if a caller loads the registry
outside the lock and later calls `save_registry`, the caller still has
a stale snapshot and can erase another writer's update. All genuine
read-modify-write operations must keep the entire operation in the same
`_registry_write_lock` critical section and call the internal
`_save_registry_unlocked` once at the end. Do not compose manual
`load_registry` + `save_registry` as an update transaction.

## Validation

`tests/test_model_registry_transactions.py` exercises separate Python
processes, a delayed first writer, contention timeout, process termination,
stable lock-file identity, exception cleanup, a registry-path symlink alias,
unrelated registry isolation and a deliberately slow Model Root scan.

Both the Ubuntu full pytest job and the Windows Python 3.12 regression job
include this suite. **GitHub CI is Pending** until actual workflow evidence
is received; writing tests or changing workflows does not constitute a pass.
No live multi-engine runtime or production Model Root was used.

## Boundaries and future work

- The lock protocol is **cooperative**. Legacy external programs editing
  `model-registry.json` without taking the same lock can still race.
- Model Root is assumed to be administratively controlled. OS-level
  registry locking does not make the physical model files immune to
  simultaneous out-of-band modification.
- Serializing scans intentionally blocks promotions/status updates during
  potentially expensive weight rehashing. A caller may see timeout and must
  retry rather than abandon the integrity gate.
- Snapshot atomicity and fsync improve process-interruption safety; they are
  not a substitute for a transactional database, a signed provenance chain
  or filesystem directory-entry fsync on all platforms.
- No lock is held across downstream TTS inference. Serving resolution and
  an already loaded engine require their own validity checks.

Related: [Model import integrity](model-import-integrity-v1.md) and
[Evaluation Provenance](evaluation-provenance-v1.md).
