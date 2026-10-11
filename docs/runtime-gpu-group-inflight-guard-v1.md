# Managed GPU-group inference admission and lifecycle handoff v1

## What the previous runtime lock did NOT protect

CVS PRs #24–#28 created a managed-process lifetime lease, durable
child PID/process-tree receipts and controller-crash protection.
PR #32 adds a GPT-SoVITS endpoint-scoped model-switch/inference
transaction. But `RuntimeSupervisor::_enforce_exclusive_group()`
could still **automatically stop a healthy sibling** on the same GPU
while the sibling was already processing an HTTP synthesis request.

Checking `/health` busy flags once before stopping is not sufficient:
a new inference can be admitted after that check and before process
termination. Likewise the managed lifetime OS lease cannot be used as
an inference admission lock because it is held for the engine's
entire uptime.

## New cooperative protocol

For each configured `RuntimeSpec.exclusive_group`, a dedicated
resource-scoped activity lock and durable receipt are stored in the
**same** shared `RuntimeSupervisor._slot_dir` as the managed GPU
lifetime locks. The activity lock file uses a SHA256-derived stable
group identifier. **Never delete this persistent lock file.**

All cooperating CVS processes use that same activity boundary:

- `server/engines/__init__.py::synthesize()` obtains the group
  activity OS lease **before** calling `ensure_ready`. It retains it
  for the entire adapter synthesis. Prior to requesting backend work,
  it publishes a durable `inference-pending` intent with a unique
  request ID, engine ID and GPU group. It clears only the matching
  intent after a completed synthesis response.
- `RuntimeSupervisor.start()`, `stop()`, `restart()` and
  `shutdown_all()` also require the activity OS lease **before**
  affecting managed process lifecycles. The existing
  `_enforce_exclusive_group()` auto-stop occurs inside the lock, so
  it cannot terminate another engine while that group is serving
  an admitted CVS synthesis.
- The admission lock allows **same-thread nesting** only, needed for
  `synthesize -> ensure_ready -> start` and `restart -> start`.
  A different thread or Python interpreter does not bypass the lock.
  If another inference/lifecycle owns the group, the operation fails
  quickly: admin lifecycle operations report `RuntimeConflictError`,
  client synthesis reports HTTP 503 with `Retry-After`, and no
  new model request is submitted.
- After abrupt CVS process death, the OS activity lease releases, but
  an in-flight HTTP backend may still synthesize. Its durable receipt
  blocks future group admission, lifecycle stop/restart and managed
  sibling startup until an operator verifies backend quiescence and
  reconciles that specific receipt. No TTL expiration or blind cleanup.
- Built-in adapters' deterministic local HTTP 400/409 input/mode
  rejection is considered safe to clear, because their backend
  requests have not been submitted; upstream HTTP/network errors
  are translated to 502/503 and retain unresolved evidence.

These are **different locks**:

1. Managed process lifetime GPU lease — who owns a launched process.
2. GPU group activity lease — who may start/stop/switch/synthesize
   **right now**.
3. GPT-SoVITS endpoint transaction lease — who may change a shared
   backend's weights and infer a selected model.

They must not be conflated; one cannot replace another.

## Failure handling and explicit limitations

A receipt can remain even when the backend completed but CVS failed
to observe the response. This is intentional uncertain-outcome
behavior: no automatic re-submission of potentially dangerous work.

Manual reconciliation is safe only after an operator verifies the
current backend PID/process tree, absence of active inference and
relevant logs. The operator must take the activity lock before
removing its pending receipt, and must never remove a lock file.
This release does not add a destructive automatic recovery command.

The activity guard protects **cooperating CVS instances sharing the
same data root and runtime slot directory**. Out-of-band scripts,
noncooperating remote clients, separate data directories and independent
System Supervisors are not participants. A server without a configured
`exclusive_group` retains its older policy. An external System
Supervisor may have separate lifecycle authority.

A successful synthesis return does **not** attest the exact real
speaker, loaded model or WAV validity. Real GPT-SoVITS/IndexTTS GPU
workloads, driver memory recovery, external Supervisor coordination
and speech-output contract validation remain FIELD-PENDING.

## Automated regression evidence

`tests/test_runtime_inference_admission.py` exercises real
cross-process lock contention against a disposable managed HTTP
engine and OS group, including:

- A real independently executing inference-admission worker blocks
  admin stop while the owned engine remains running.
- Same-group startup refuses to auto-stop an in-flight sibling.
  After inference completes, normal handoff resumes.
- Force-kill the inference-admitting CVS worker, check the OS lock
  is released but the durable pending receipt blocks stop/restart.
- Exercise `server.engines.synthesize` with a held adapter call,
  including the actual Runtime Supervisor lifecycle path.
- Ensure local pre-backend 400 rejection retires its own receipt,
  rather than poisoning future requests.
- Verify same-thread nested managed start works, and cross-thread
  activity-lock ownership remains exclusive.

Tests use disposable subprocesses and no TTS weights or GPU.
Both Linux full pytest and Windows targeted CI must succeed on the
**final PR head** before merge. The scenario remains FIELD-PENDING
for real model loading and GPU memory release.
