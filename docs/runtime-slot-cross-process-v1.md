# Runtime GPU slot ownership across CVS processes (v1)

## Threat / failure path

`RuntimeSupervisor` previously used only a Python `threading.RLock`
and best-effort HTTP health checks to coordinate managed engine starts.
Two independent CVS service processes can both observe a free endpoint
while the engine is still initializing and each start a different model
in the same exclusive GPU group. An in-process mutex cannot serialize
those actions across processes.

## Cooperative lease contract

- All cooperating CVS processes on the **same host** must use the same
  writable `CVS_DATA_DIR` (default: project `data/`). The default lease
  directory is `<CVS_DATA_DIR>/runtime-slots`. An isolated
  `slot_dir` injection exists for tests; it is not a replacement for
  one shared production resource scope.
- An exclusive group `gpu-0` maps to one stable `gpu-0.lock` file.
  Every managed engine start must obtain its OS-level **nonblocking**
  advisory lease before launching a child. On POSIX, this is
  `fcntl.flock`; on Windows, a one-byte `msvcrt.locking` lease.
- The lease's **open file descriptor stays in the owning CVS process**
  for the full observed lifetime of its managed engine process. Another
  participating CVS instance trying the same group is refused with a
  classified `RuntimeConflictError` before spawning a second engine.
  Different independent groups do not block each other.
- A confirmed normal stop, startup failure, or observed process exit
  releases the lease. The last supervisor process exiting releases its
  OS-owned lease, even if the lock file remains on disk.
- **Never delete a lock file to release ownership.** Deleting and
  recreating a lock's inode permits split-brain lock ownership.
  The existence of the file alone does **not** mean the slot is busy.
- Existing exclusive-group discovery and external endpoint health
  preflights remain in place. The lease adds **cooperative cross-process
  serialization**, not replacement of the process/health validation.

## Recovery and important limitations

The lease belongs to the **CVS parent process**, not the spawned Python
engine process. If the CVS process crashes while its managed TTS engine
survives as an **orphan**, the parent-held OS lock is released even
though GPU memory may still be occupied. Other CVS instances should
treat a healthy endpoint as an external owner and should not start another
engine in the same group. An orphan that is alive but not yet serving a
health endpoint may evade such probing. Operators must verify process
ownership and GPU utilization before starting another engine after
an abnormal CVS termination.

This mechanism does **not** coordinate with a separate **System
Supervisor**, external scripts, other applications, other physical hosts,
or CVS instances configured with different data roots unless those
actors explicitly participate in the same lock protocol. External
lifecycle owners retain control of their managed processes; CVS does
not take their slot lease or claim exclusive ownership of them.

Lock files must be created under an **administratively controlled**
local directory. Symlink replacement, hostile local writers, lock-file
unlinking and network filesystems with unreliable lock semantics are not
part of the current safety contract. Health checks cannot prove that GPU
memory has been freed or that a dead driver recovered.

No process is forcibly killed on lease conflict; an error is returned.
No synthesis output is retried or marked complete because of a lock
acquisition decision.

## Verification

`tests/test_runtime_slot_lease.py` includes two independent Python
processes contending for one lock, unrelated slots, OS cleanup after
a killed lock owner, symlinked directory identity, supervisor-managed
mock HTTP child lifecycle, and launch failure cleanup. It runs under
Linux full pytest and the targeted Windows Python 3.12 suite.

**CI status is Pending until the new PR's jobs finish.** No real GPU,
IndexTTS weights, GPT-SoVITS process, actual orphan-engine scenario,
or independent System Supervisor integration was exercised.
