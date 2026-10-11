# Managed runtime orphan-owner receipt v1

## Why the slot lock alone was insufficient

CVS PR #24 introduced a cooperative OS file lock for every managed
`exclusive_group`. That lock is owned by the **CVS parent process**.
If the parent dies while its Python/GPU child survives, the OS correctly
releases the parent's lock. A new CVS instance could then take the
lock while the old model process still uses GPU resources.

PR #25 adds a durable, fail-closed **launch receipt** under the same
shared `<CVS_DATA_DIR>/runtime-slots` directory.

## Publication and reconciliation contract

Only a cooperating managed runtime owner with the group OS lease may
modify that group's `<exclusive_group>.owner.json` receipt.

1. After acquiring the group lease and before launching anything, check
   the previous owner receipt.
2. If a prior receipt is `running`, verify its recorded child PID
   against an OS process creation identity. On Linux this uses the
   `/proc/<pid>/stat` process start tick; on Windows it uses
   `OpenProcess`, `WaitForSingleObject` and `GetProcessTimes`,
   without invoking `os.kill(pid, 0)` on Windows.
3. Only if the old process is confirmed **absent**, or the PID has been
   demonstrably reused for a different creation identity, delete the
   prior receipt and continue. If its old identity still exists,
   process inspection is inaccessible, or the receipt is corrupt or
   unrecognized, reject startup with `RuntimeConflictError`.
4. Publish an atomic, fsynced **`starting` intent** before calling
   `subprocess.Popen`. This seals the parent-crash window between
   deciding to launch and obtaining a child handle. An abandoned
   `starting` receipt is deliberately **not auto-cleared**, because
   the parent might have died immediately after creating the child.
5. Once `Popen` returns, capture the child's OS process creation
   identity and atomically replace the intent with a `running`
   receipt containing `pid`, process identity, engine/group identity,
   launch UUID, and timestamps. No prompt, sample or secret env data
   is stored.
6. On confirmed normal child termination or verified unexpected exit,
   remove **only the matching launch UUID** before releasing the group
   lease. When launch confirmation fails after a child was returned,
   attempt to kill/reap it before cleaning the matching intent.
   If process death remains uncertain, preserve the intent for
   manual reconciliation.

Each atomic receipt update stages a new JSON file in the same directory,
writes and fsyncs it, then uses `os.replace`. The lock file itself is
permanent and must **never be deleted** to reset ownership.

## Operations and failure recovery

An unresolved receipt is a protective stop, **not proof that its child
is still alive**. The administrator should inspect the recorded PID,
its actual OS creation time, child command/parentage, listening port
and GPU residency. After the engine is confirmed terminated and no
other cooperating CVS process owns the group, an administrator can
clear the **owner receipt** manually under controlled maintenance.
Never remove a live group `.lock` file, nor ignore a corrupted record
as though it were absent.

The OS lock serializes collaborating CVS processes, while the receipt
preserves launch intent across crashes. Both are scoped to the shared
`CVS_DATA_DIR`; processes using different data roots, a separate
System Supervisor, or unrelated GPU programs are not coordinated.
A managed engine may itself spawn independent grandchildren that survive
after the recorded immediate child is gone. This is **not a guarantee**
that all descendants/GPU allocations disappeared.

This version supports **Linux and Windows** process identity checks.
Other POSIX kernels without Linux `/proc` fall back to an unresolved
status for a live PID; do not automatically clear a receipt by PID
alone. Kernel crashes, hostile filesystem changes, cross-host volumes,
and external lifecycle authority remain outside the current protocol.

Some OS process inspection failures are intentionally fail-closed and
may require manual intervention. The receipt is not a durable full
crash-event history or a general task queue.

## Automated evidence

`tests/test_runtime_orphan_receipt.py` validates live orphan identity,
interrupted `starting` intent, corrupt/unsupported receipts,
confirmed-dead PID cleanup, PID-reuse token mismatch, managed HTTP
engine normal termination, observed unexpected exit cleanup, launch
failure ambiguity, and rejection of an unrelated launch's receipt
deletion. Uses fake disposable subprocesses and no GPU assets.
The tests run in Linux full pytest and the targeted Windows Python 3.12
job.

**CI Pending** until the new PR's runs finish. Real IndexTTS model
loading, actual orphan GPU processes and independent System Supervisor
integration remain field-pending.
