# Managed runtime process-tree containment v1

## Root cause

CVS previously treated the direct `Popen` PID as the complete lifetime
of a managed GPU engine. `_stop_owned()` terminated only that PID;
`_clean_exited()` could clear its durable owner receipt and unlock
`exclusive_group` after `Popen.poll()` returned, even if the engine
had created independently running Python/GPU descendants.

For a GPU workload, direct PID death is **not proof** that child model
processes and their allocations have stopped.

## Managed exclusive-group engines

The new process-tree containment applies to **CVS-managed** engines
with an `exclusive_group`. External System Supervisor engines and
managed runtimes without a configured exclusive group preserve their
previous lifecycle boundary.

**Linux/POSIX:**

- The engine is spawned with `subprocess.Popen(start_new_session=True)`.
  Its session and process group are named by the original child PID.
- On a requested stop, CVS sends `SIGTERM` to the whole managed process
  group, waits for group members to exit, and escalates to group
  `SIGKILL` only after the configured shutdown timeout.
- Linux inspections use `/proc/<pid>/stat` process group AND session
  identity, ignoring zombie entries that can no longer consume GPU
  memory. This can identify descendants after the original group
  leader has exited or been reparented.
- If the direct leader unexpectedly exits while a nonzombie descendant
  remains, CVS records `process-tree-unresolved`, retains the
  **GPU group OS lease** and the **durable launch receipt**, and
  declines another automatic start. Explicit admin stop can terminate
  the group, verify it is quiescent and then release resources.
- A new CVS parent must inspect a previous durable
  `tree_kind=posix-session` receipt and refuse takeover if the old
  session still contains members, even when the old leader PID is dead.
  A legacy receipt with **no tree identity** is not automatically
  considered clear solely because the direct PID has exited.

**Windows:**

- For managed exclusive-group engines, CVS starts a dedicated
  `server/runtime_windows_job.py` wrapper as the tracked process.
  The wrapper installs a `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job
  Object and assigns **itself** BEFORE launching the actual engine,
  avoiding the common race of spawning a child before attaching it.
- Normal engine descendants inherit the wrapper's Job Object.
  When the wrapper exits or CVS terminates it, Windows closes the
  wrapper-owned job handle and terminates remaining job members.
  The wrapper refuses to spawn if the OS denies creation,
  configuration or assignment (including incompatible preexisting
  OS Job restrictions), instead of silently falling back to unmanaged
  process creation.
- The durable receipt records `tree_kind=windows-job-wrapper`.
  This relies on the OS job kill-on-close contract rather than a
  one-PID health check. It is not a proof of GPU driver cleanup.

## Ownership and error handling

The OS slot lease and durable receipt remain held until
`stop_managed_tree` or `reconcile_exited_tree` establishes the
required termination boundary. If the group cannot be enumerated,
signaled or verified, the lease and receipt remain held and the admin
status includes `process_tree_unresolved`. No blind restart is
performed and other engines in the same GPU group are blocked from
taking over while evidence remains.

The new owner-receipt `tree_kind` is additive. Existing receipts
without a tree scope intentionally require manual reconciliation
when their recorded PID is dead. Do not manually delete `*.lock`
files. A human operator must confirm the process tree, system service
and GPU memory before removing stale owner evidence.

There is **no general adoption or control** of a non-cooperating
external process, an independent System Supervisor, or a subprocess
that deliberately escapes its inherited group/job. Linux members may
use `setsid` to escape, Windows may use other OS breakaway features
where allowed, and GPU allocations can persist due to kernel/driver
faults. POSIX platforms without readable Linux-style procfs treat
undetermined group presence conservatively.

The Windows Job wrapper is a new runtime execution boundary: the
tracked PID in CVS diagnostics represents the wrapper, not the
wrapped model PID. Model files, CLI args and environment pass through
unchanged, but independently launched subprocesses/sidecars may need
integration validation.

## Tests and acceptance

`tests/test_runtime_process_tree.py` exercises a disposable managed
HTTP runtime that forks a long-lived child. Tests verify tree-wide stop
and lock/receipt release on Linux and Windows. Linux-specific tests
cover root PID exit while descendants still live, refused restart,
admin teardown, and a new parent refusing a stale receipt that has
surviving session members. Windows tests exercise the actual Job
Object launcher through the normal Runtime Supervisor path.

The existing crash-loop, orphan receipt, GPU lease and external
activation suites remain in the Linux and Windows CI matrix.

**CI and hardware acceptance are pending at PR creation.** No real
GPU driver, TTS model, real external System Supervisor, adversarial
process escape, or kernel-level crash was exercised.
