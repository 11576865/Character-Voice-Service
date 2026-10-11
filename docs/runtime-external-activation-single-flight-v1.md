# External System Supervisor activation single-flight v1

## Existing failure path

For an external Runtime Registry engine with `start_on_demand=true`,
CVS previously created a fresh `<request_id>.json` activation command
every time `ensure_ready()` saw an unhealthy HTTP endpoint, then waited
for health for the configured `startup_timeout`. Different independent
CVS processes had no shared per-service serialization. After timeout,
CVS did not retain the activation request's identity, so the next
request could issue another command even if the external System
Supervisor was still starting the same model.

Separately, a stale external `status_file` could advertise `stopped`
after an already-healthy HTTP endpoint reported `ready`, or advertise
`ready` while HTTP health was actually unavailable.

## New cooperative command contract

Only external runtimes with `external_control.mode=file` participate.
The existing command schema is **unchanged**:

```json
{
  "schema_version": 1,
  "action": "activate-engine",
  "engine_id": "index-tts",
  "service_key": "IndexTTS",
  "request_id": "<unique opaque identifier>",
  "created_unix": 0
}
```

The file remains published as `<request_id>.json` in the configured
`external_control.request_dir`. The placeholder timestamp in the
example is not the actual emitted wall-clock time.

Before issuing a command, CVS takes a **nonblocking OS file lock**
derived from the pair of canonical request directory and `service_key`
using the existing Linux/Windows `acquire_runtime_slot` primitive.
Only one CVS caller can issue or await an activation for a given
service at a time. Another concurrent caller gets a classified
`RuntimeConflictError` instead of publishing a duplicate command.
Unrelated service keys have separate locks.

While holding that lock:

1. Recheck the live HTTP health endpoint.
2. Inspect the persistent pending activation receipt in the internal
   `request_dir/.cvs-activation-state/` subdirectory. This **must not**
   be consumed as an external `*.json` command from the top-level
   request inbox.
3. Reject an unresolved or corrupted previous receipt. Also reject a
   matching `service_key` receipt whose `engine_id` identifies a
   different engine; never clear someone else's activation.
4. Publish an atomic, fsynced **pending** receipt before the external
   command. Then publish the original request schema through a staged
   file and `os.replace`. An interrupted gap between these writes
   leaves an uncertain pending receipt and deliberately fails closed.
5. While waiting for health, keep the per-service lock. On verified
   live health, clear **only that request ID's** receipt; on timeout,
   retain it, include `request_id` in the error, and require later
   reconciliation instead of automatic command replay.
6. If the external engine becomes healthy **after** CVS itself timed
   out or restarted, a later `ensure_ready()` call checks live health
   again while temporarily owning the per-service lock. It may then
   remove the matching old receipt **without issuing another command**.

Administrative `GET /v1/runtime` exposes `external_activation`
with a pending request ID, engine ID and timestamp, or an unreadable
state. When HTTP health is unavailable, runtime status becomes
`external-activation-pending` or
`external-activation-unresolved`. External status-file details remain
available in `external_supervisor` as evidence, but cannot supersede
real-time HTTP readiness/busy information or turn a failed HTTP
health check into an authoritative ready state.

## Scope and recovery limitations

**This is command admission and correlation, not remote process
ownership.** The external System Supervisor remains the sole lifecycle
authority. CVS does not adopt its PID, kill its engine, or assume that
the GPU is free. Both CVSs must share the exact filesystem request
directory to coordinate. Different directories, out-of-band launch
scripts, and independently managed service instances do not participate
in this protocol.

An HTTP health success confirms an endpoint responded; it does **not**
prove that a particular `request_id` caused the success, or that a
different service has not taken over the endpoint. End-to-end
request-correlated acknowledgement remains an external supervisor
protocol enhancement, **not something this v1 implementation claims**.

The OS lock protects cooperative command publication while the CVS
caller is alive. A pending receipt covers CVS crashes during command
publication, but may stay indefinitely unresolved after a timeout or
partial filesystem failure. Operators must inspect the pending request
ID, the top-level command inbox, external supervisor logs, actual
process tree and health endpoint before manually clearing the internal
receipt under controlled maintenance. Never delete the persistent
`*.lock` file. There is no automatic TTL deletion, blind retry,
background activation daemon, or escalation that kills external
processes.

This repair also does **not** solve the distinct issue of managed
engines spawning grandchildren that outlive the tracked direct child.
That requires process-tree or OS-job ownership and separate fault
injection tests.

## Automated evidence

`tests/test_runtime_external_activation.py` contains 14 tests for
pending-before-command publication, concurrent callers, timeout
replay suppression, late healthy recovery, corrupt pending state,
per-service lock scope, configuration validation, stale Supervisor
status precedence, unrelated engine identity protection, internal
receipt/inbox separation, request-ID-only acknowledgement, and admin
diagnostics. Linux full pytest and the targeted Windows suite include
these tests.

**CI status is Pending at PR creation.** Tests use simulated endpoints
and local temporary directories; live System Supervisor command
consumption, real TTS models, GPU lifecycle and producer/consumer
cross-process e2e interoperability are FIELD-PENDING.
