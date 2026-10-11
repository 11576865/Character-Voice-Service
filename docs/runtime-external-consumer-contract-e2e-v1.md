# External System Supervisor file-bridge producer/consumer E2E contract (mock consumer)

## Why this additional acceptance layer exists

PR #26 added a durable pending activation request, per-service OS lease,
atomic command publication and late-health reconciliation, but its tests
used stubbed `_health()` functions instead of **a separate process that
actually consumes the command**. That did not exercise the full chain:

CVS producer -> shared command directory -> external reader ->
HTTP health endpoint -> CVS confirms readiness -> request receipt
reconciliation.

The actual independent **System Supervisor application is not included
in this CVS repository**. A successful mock consumer test is therefore
not a claim that the deployed Supervisor will read the same paths or
honor the same command semantics.

## New test-only consumer

`tests/fixtures/external_file_consumer.py` runs as an independent Python
OS process. It accepts a temporary command directory, exact service key,
engine ID and local HTTP port, and reads the existing top-level JSON
file shape:

```json
{
  "schema_version": 1,
  "action": "activate-engine",
  "engine_id": "index-tts",
  "service_key": "IndexTTS",
  "request_id": "opaque-unique-id",
  "created_unix": 0
}
```

The fixture rejects nonmatching service/engine IDs and checks the
filename equals `<request_id>.json`. It records the consumed command
for tests, then waits for a controlled release marker before exposing
`GET /health` with `{"status":"ready"}`. It also writes a test-only
System Supervisor status file using the existing `services[]` shape.

The fixture is **not a production launcher**, does not load models,
does not kill real services, and deliberately has no GPU interaction.

## Acceptance scenarios

`tests/test_runtime_external_consumer_e2e.py` runs three OS-level
scenarios on Linux and Windows:

1. **End-to-end activation:** CVS `ensure_ready()` publishes one
   command; a separate consumer reads it and brings up an HTTP server.
   CVS checks live HTTP readiness, clears its pending receipt and
   reports ready. A second CVS instance must not publish a duplicate.
2. **Timeout and late completion:** hold consumer health offline after
   it reads the command. CVS times out and retains the pending request
   ID; an independent caller must reject replay. After allowing the
   already-running consumer to expose health, a later call reconciles
   the receipt without producing a second command.
3. **Producer death:** launch CVS `ensure_ready()` inside another
   interpreter, let it publish one command, then **kill the CVS
   process while it is waiting**. The consumer still owns the same
   command and has not exposed health. A fresh CVS parent must refuse
   to replay and eventually reconcile when the consumer goes healthy.

Tests use disposable local loopback HTTP ports, temporary directories
and abrupt kills **only for their own test processes**. The consumer
never launches or changes any actual TTS engine.

## What the test does NOT establish

- It does not prove that an independently deployed System Supervisor
  implementation implements this bridge or that the actual host
  config maps the same command directory and service key.
- It does not provide correlated System Supervisor acknowledgement.
  Health readiness proves an endpoint responded, **not** that the
  external owner accepted a particular `request_id`.
- The external status file is observational; its
  `last_request_id` fixture field is test-only and is **not**
  interpreted as an authoritative protocol acknowledgement by CVS.
- It does not validate real IndexTTS or GPT-SoVITS model loading,
  timing, GPU memory release, crashes, or system-wide process
  ownership.

## Live System Supervisor acceptance procedure (FIELD-PENDING)

On a machine with the real System Supervisor installed, safely verify:

1. Confirm `config/runtimes.local.json` is derived from the actual
   Supervisor configuration and that `external_control.request_dir`,
   `status_file`, `service_key` and the health URL all match the
   deployed owner. Do not overwrite an established local registry
   without review.
2. Identify the active System Supervisor session, PID/process tree
   and listening health endpoint. Do not inject an activation while an
   engine is busy with user requests.
3. Trigger one safe, non-destructive engine activation using CVS; record
   the exact opaque `request_id` and inspect whether the external
   owner actually consumes the corresponding top-level command.
4. Verify live health, external status-file changes, final owner PID,
   repeated activation suppression and late completion behavior.
   Capture logs before any cleanup.
5. Confirm that the external Supervisor rather than CVS still owns
   start/stop actions. Verify GPU residency with the host's own tools
   if hardware is available. Do **not** infer GPU memory release
   solely from HTTP health.
6. Test failure injection **only in an isolated maintenance environment**
   without production synthesis requests. Record the request/response
   correlation and any divergence in the deployment's actual protocol.

Run `python -m pytest -q tests/test_runtime_external_consumer_e2e.py`
to reproduce the deterministic mock contract checks locally.

**CI Pending for the new tests at creation; actual consumer / GPU
integration remains FIELD-PENDING regardless of mock CI results.**
