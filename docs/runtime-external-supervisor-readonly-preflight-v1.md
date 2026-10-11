# External System Supervisor read-only preflight v1

## Purpose and safety boundary

CVS has a file-bridge **command producer**, and PR #29 verified a
separate **test-only** consumer. A real deployed System Supervisor may
have different paths, service keys, runtime identities or stale status
files. Before issuing any activation on a user's machine, a safe
preflight must compare the real configuration and report what is known
without silently creating credentials, command files, or engine workers.

`scripts/external_supervisor_preflight.py` is a **standalone standard
library** utility. It deliberately does **not import** CVS's
`server.config` or `runtime_registry` modules, because import-time
CVS admin-token initialization can create `data/admin-token.txt`.
It does not call `RuntimeSupervisor`, acquire file leases, create
directories, open a write-mode file, publish activation requests, or
start/stop any process.

Default execution is offline and reads the supplied JSON files only.
The optional health probe performs a GET only against an
`http://localhost`, `http://127.0.0.0/8` or `http://[::1]` endpoint
and **never follows HTTP redirects**. No other hosts, HTTPS, or
credential-bearing URL userinfo are accepted for this probe.

## CLI

Run from the CVS repository or provide an absolute script path:

```powershell
python .\scripts\external_supervisor_preflight.py --registry .\config\runtimes.local.json --supervisor-config D:\path\to\supervisor.config.json
```

The `--supervisor-config` input is optional when the actual external
config is not available. Omitting it reduces the confidence of path
and service-identity checks; it is **not equivalent to a successful
live Supervisor interoperability test**.

Opt in to read-only local health checks only after confirming the
endpoint belongs to the intended service:

```powershell
python .\scripts\external_supervisor_preflight.py --registry .\config\runtimes.local.json --supervisor-config D:\path\to\supervisor.config.json --probe-health --timeout 1
```

Output is JSON on stdout. Exit codes are:

| Code | Meaning |
| --- | --- |
| `0` | No detected inconsistencies in checked fields; not evidence of real consumer acknowledgment |
| `1` | One or more warnings or unresolved observations |
| `2` | Configuration inconsistency, unsafe probe target, or invalid/mismatched pending evidence |

The tool may report a ready Supervisor `status_file` entry while
health remains `not-probed`. Status files are asynchronous
**observations**, not proof of a currently healthy TTS endpoint. A
matching `<request_id>.json` command is **only evidence of a
published command**, not an external consumer acknowledgment.

## What it checks

- CVS Registry schema, external engine IDs, external control
  `mode=file`, non-empty `service_key`, `request_dir` and
  `status_file`.
- With a real Supervisor config: compare the resolved control bridge
  paths, exact external `services[].key`, and
  `system_identity.entity_kind=runtime` /
  `system_identity.engine_id`. An enabled CVS external engine must
  not reference a disabled matching Supervisor service.
- Read the current `status_file` by exact `service_key` without
  assuming its `ready` state is authoritative.
- Inspect the known per-service SHA256 pending receipt under
  `request_dir/.cvs-activation-state/`. Check its schema, service and
  engine identity, request ID shape, and any existing top-level
  `<request_id>.json` activation command.
- Report missing top-level command files **as ambiguous**, since the
  real consumer may have removed the command after reading, or CVS
  may have failed before publishing it. Never infer success or
  permission to resend from file absence.
- Optional bounded loopback GET. A responsive endpoint does **not**
  prove the external owner accepted a specific request ID, that
  the same process was launched, or that GPU memory is free.

## Security and evidence limitations

The JSON report omits credentials, environment values, model prompts,
weights, generated audio, and the full external status/command payload.
It may still contain local directory paths, service names, and
pending opaque request IDs; treat the report as operational evidence
and review it before publishing.

The preflight uses best-effort, point-in-time filesystem reads.
It does not lock a running external service or take a transactionally
consistent snapshot. File changes between reads can occur; ambiguity
is reported instead of being silently repaired. It never
automatically deletes a pending receipt or activation command.

Actual System Supervisor command consumption, correlated
request-ID acknowledgment, real model loading, process-tree cleanup,
GPU utilization, and TTS generation correctness are still **FIELD-PENDING**
without access to the deployed external application and target host.

## Automated verification

`tests/test_external_supervisor_preflight.py` covers the standalone
CLI's no-write/no-admin-token side effect, matching and mismatched
config, pending receipt/command uncertainty, unsafe IDs, corrupt
records, observational status, disabled Supervisor runtime,
loopback-only health, redirect refusal and missing registry.
It runs in Linux full pytest and Windows targeted pytest.

No actual activation or GPU workload is required to exercise these
tests. CI result must be obtained from the new PR's Linux and Windows
jobs before merging.
