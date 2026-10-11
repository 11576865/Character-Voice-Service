# Runtime Supervisor crash-loop containment v1

## Failure mode

Before this change, `RuntimeSupervisor._clean_exited()` removed the
supervisor-owned process and closed its log handle when `poll()` showed
an exit. The next `ensure_ready()` call could immediately spawn a
replacement if `start_on_demand=true`, even if the process had just
exited for the same unresolved reason. Repeat requests could therefore
generate an unbounded succession of short-lived interpreter/model loads.
The status payload lost the exit code and returned an ambiguous
`stopped` state.

This is a source-level failure mode. It is not evidence of a particular
user machine repeatedly crashing, nor a claim of GPU-driver recovery.

## Implemented contract

- Preserve an unexpected process exit observation in memory, including
  `pid`, `returncode`, uptime, observation timestamp, and log path.
  Administrative `GET /v1/runtime` diagnostics expose `last_exit`,
  `last_start_failure`, `restart_failures`, and
  `restart_backoff_seconds`. They do **not** expose model prompts,
  engine environment variables or the full log content.
- Automatic start attempts are rate-limited per managed engine after
  unexpected exit, process-spawn failure or health-readiness timeout.
  Backoff starts at **5 seconds**, doubles per consecutive failure and
  caps at **60 seconds**. Failures older than 300 seconds start a new
  sequence; a process that ran for at least 300 seconds likewise resets
  the earlier failure sequence on its next observed exit.
- While a cooldown is active, ordinary `ensure_ready()` and `start()`
  avoid repeated multi-second health probes. They use an initial
  **0.2-second bounded health check**, preserving recognition of an
  already-healthy endpoint, and otherwise fail quickly with a
  `RuntimeSupervisorError` and diagnostic `restart-backoff`.
  This is preferable to retrying a heavy broken model load on every request.
- Explicit administrative `restart(engine_id)` is a **manual override**:
  after its ordinary stop and endpoint checks, it clears the in-memory
  cooldown before attempting one new start. Prior `last_exit`
  evidence remains available.
- Intentional `stop()`, exclusive-GPU-group shutdown and
  `shutdown_all()` do not create unexpected-exit backoff.
- When a managed service was healthy but has since exited, the next
  supervisor observation still records that unexpected exit only once.
- External lifecycle owners retain authority over their engines; CVS
  does not attempt to kill or auto-restart their processes through this
  managed-process backoff.

## Limitations and operations

This is **in-memory crash-loop protection**, not a durable crash ledger
or a distributed lock. Restarting the CVS process resets counters and
last-exit observations; operators should retain sidecar process logs and
external supervisor evidence for longer-lived incident analysis.

The automatic backoff is not a guarantee that a failed GPU driver,
deadlocked kernel, or unreaped descendant process has recovered. The
administrator must inspect the saved log path and the owning process
before overriding the cooldown. A returned HTTP 503 does not justify
blindly duplicating a potentially non-idempotent generation request.

Failed readiness takes the configured `startup_timeout` before its
cooldown starts. The existing `supervisor-transition` status remains
nonblocking while another thread owns startup/stop. No new retry
scheduler, background daemon, or automatic exponential-delay sleep
has been introduced.

## Verification

`tests/test_runtime_crash_recovery.py` uses actual disposable Python
child processes to exercise startup crashes with return code 17,
automatic retry suppression, administrative recovery to a healthy HTTP
server, a normal intentional stop, a post-readiness unexpected exit,
an explicit startup timeout and deterministic exponential caps.
The suite runs in Linux full pytest and in the Windows targeted job.

**GitHub CI remains Pending until the new PR run completes.** Real
IndexTTS/GPT-SoVITS model-loading, actual GPU faults and external
system-supervisor restart contracts remain field-pending.

## CI timing regression

The first GitHub Windows run of PR #23 passed 88 targeted cases but
failed one test that assumed a 5-second real-time cooldown would still
be active after multiple Windows HTTP health probes. The CI showed
the crashed engine had been relaunched and failed as designed after
its short cooldown expired; the failure was not evidence of an incorrect
crash exit code.

Follow-up uses a widened **test-only** cooldown for the slow-machine
fault-injection scenario, while the independent backoff-policy test still
verifies the true production 5/10/20/40/60-second sequence. Additional
regressions verify that cooldown-path health probes use the bounded
0.2-second timeout and a genuinely healthy endpoint still takes precedence
over a stale local crash record. New Linux/Windows CI is pending.
