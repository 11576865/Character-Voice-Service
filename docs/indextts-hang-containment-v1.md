# IndexTTS inference hang containment v1

CVS IndexTTS 2.5 is a **single-inference-slot** engine: its sidecar hosts
one model instance, and it advertises `max_concurrency=1` and
`supports_cancel=false`.

Previously, `/synthesize` used a blocking `with _INFER_LOCK` section.
If native or GPU inference never returned, the active HTTP call would wait
up to the CVS adapter's 300-second network timeout, but **other requests
could wait indefinitely in server threads**. `GET /health` returned
`ready` even while this only inference slot was occupied.

This repair makes the failure **bounded and diagnosable**:

1. Validate each input normally. Acquire the sole inference slot using
   `acquire(blocking=False)`. When occupied, **do not queue**. Respond
   `HTTP 503` with `Retry-After: 2` and a JSON
   `{"error": "...", "retryable": true, "busy": true, ...}`.
   The CVS IndexTTS adapter retains 503 rather than masking it as 502.
2. `GET /health` continues to respond with HTTP 200 to distinguish
   an alive service from a crashed one, but reports `status=busy`,
   `busy=true`, the active **opaque request ID** and elapsed seconds
   while inference runs. It never includes the source text or model prompt.
   `RuntimeSupervisor` interprets this as **healthy but busy**, so
   its diagnostic status is `busy`, not idle `ready`. The returned
   `ready=true` means the sidecar process is reachable, **not** that
   it can currently admit another inference.
3. Each inference has a **process watchdog**, configured via
   `INDEX_TTS_INFER_DEADLINE_SECONDS`. Default: **240**; allowed 1–280
   seconds, or **0** to explicitly disable protection. Invalid numeric
   values fail startup validation. The deadline is intentionally below
   the CVS IndexTTS adapter's 300-second HTTP timeout.
4. If `IndexTTS2.infer()` fails to return before the deadline, the
   watchdog writes a reason and active request ID to the sidecar process
   log, then terminates **the dedicated sidecar process** with exit code
   124. This clears the occupied native/GPU inference slot at OS process
   scope. The in-flight HTTP connection fails; CVS reports an unavailable
   sidecar. When lifecycle policy is `start_on_demand`, a subsequent
   request can start/reactivate a healthy sidecar through its configured
   Runtime Supervisor. No interrupted synthesis is automatically retried
   or falsely reported as completed.
5. If inference ends in time, the watchdog is canceled. Late timer
   callbacks check the active request identity before terminating any
   process.

## Operational boundaries

**Not a general cancellation API.** IndexTTS has not gained per-request
GPU cancellation. A hung native call cannot safely be stopped by raising
a Python exception from another thread. Killing the dedicated process
also discards the loaded model and requires a potentially expensive
reinitialization. Users should select a realistic deadline for their
hardware and input lengths.

The watchdog does not guarantee cleanup when the **OS kernel or GPU driver
itself** is unresponsive, and external supervisors may need an activation
request to restart an externally managed engine. Watch the Runtime
Supervisor log for the watchdog exit code and check the exact engine
instance that owns the GPU. No parallel replacement process should be
started while the old process is still alive.

`Retry-After` is advisory; do not automatically hammer the engine every
two seconds while another task is running. Use bounded, backoff-based
retries and confirm whether the prior request actually completed before
resubmitting non-idempotent speech generation.

For locally generated runtime config, the default deadline is installed
by `scripts/init_runtime_registry.ps1`. Existing machine-local config
files are intentionally not overwritten; add this environment setting
manually if required. If an external system supervisor launches the
IndexTTS process, configure its **actual service environment**, not just
the CVS-side external Runtime Registry metadata.

## Tests and evidence

`tests/test_index_tts_hang_recovery.py` exercises real localhost HTTP
requests against a fake IndexTTS object: blocked inference, concurrent
fail-fast 503 and `Retry-After`, observable health, subsequent release,
error propagation through the CVS adapter, invalid deadline settings,
and subprocess tests where the watchdog really terminates only a
disposable test process with code 124. The Runtime Supervisor tests
cover busy and stopping JSON health responses.

No user voice recordings, GPU model weights or real IndexTTS2 inference
were used. Linux/Windows CI outcomes and actual machine acceptance
remain **pending until reported by GitHub Actions**.
