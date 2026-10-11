# CVS controller crash: cross-process fault injection v1

## Verification gap

PRs #24–#27 introduced resource-scoped OS leases, launch receipts,
POSIX process groups and Windows kill-on-close Job Objects. Their tests
covered engine crashes and simulated release of a CVS controller's
lease, but **did not actually terminate a separate CVS controller
process while its managed engine remained running**.

The distinction matters. An OS file lease is owned by the CVS
controller, while its engine and grandchildren can survive controller
termination. The next CVS controller must not equate a now-acquirable
lock with available GPU memory.

## New fault-injection tests

`tests/test_runtime_parent_crash.py` launches a *separate Python
interpreter* running `RuntimeSupervisor`, which starts a disposable
HTTP engine that spawns a long-lived worker child in a shared
`exclusive_group`. The test process then:

1. Observes a persisted `running` receipt containing the managed
   engine's direct process identity, and verifies the GPU group lock
   is held by the controller.
2. Abruptly **kills the CVS controller process** without running any
   shutdown handler.
3. Verifies the OS lock is released even though the previous managed
   engine and its worker child are still alive.
4. Creates a second independent Runtime Supervisor with a different
   health endpoint but the same GPU group and shared slot directory.
   It must refuse to spawn the replacement, preserving the first
   engine's durable owner receipt.
5. Performs explicit test-only cleanup of the old group or Windows
   wrapper and verifies the mock descendant exits. It never targets
   arbitrary external processes or actual TTS model workloads.

A second test interrupts a separate controller **after** it has
written the `starting` owner receipt and **before** `Popen` returns.
A later controller must reject the unresolved startup intent. The
test knows the old process never spawned a child and can therefore
safely clear that specific receipt under its group lock. Production
code does **not** infer this from age or timeout.

## Linux procfs visibility

Before this test addition, `_live_posix_group_members` skipped any
`OSError` while reading an individual `/proc/<pid>/stat` entry and
then might return `False` when no known members were found. This
confused **a vanished process** (safe to skip) with **a process whose
identity cannot be inspected** (not safe to ignore). The fix treats
permission-denied or otherwise unreadable entries as an unknown group
state unless a matching live member is found. It continues to ignore
transient `FileNotFoundError` from processes that exited during the
scan. Two deterministic Linux-only regressions inject these conditions.

## Scope and constraints

This is real OS-level **mock process-tree** fault injection, *not* a
GPU hardware acceptance test, a fault injection into IndexTTS or
GPT-SoVITS, or a System Supervisor interoperability test.

Linux/POSIX descendants intentionally escaping their process
session, Windows processes permitted to break away from the job,
OS/GPU-driver crashes and malicious filesystem mutation remain
outside the protocol. On platforms with unreadable procfs entries,
the safe consequence is an unresolved owner and possible manual
administrative reconciliation, not a guessed recovery.

Tests use unique temporary registries, loopback HTTP ports, slot
directories and disposable children. The controller PID and descendant
PID are recorded to restrict test-only cleanup to the test's own
process scope. Live user processes are never touched.

Cross-platform CI and any eventual main merge must be verified from
the new PR head's Linux and Windows workflow results. Until then,
the result is **source changes and committed tests, CI Pending**.
