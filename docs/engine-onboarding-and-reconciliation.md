# Engine Onboarding and Host Reconciliation

This document covers the two system-level mechanisms that keep Character Voice System extensible without turning future engines into another collection of one-off scripts.

## 1. Engine Registry

Engine identity is declarative.

Descriptors live under:

```text
engines/*.json
```

Existing engines:

```text
engines/gpt-sovits.json
engines/index-tts.json
```

A future speech engine can use the generic Character Voice Sidecar Protocol v1 by adding a descriptor based on:

```text
engines/sidecar.example.json
```

The descriptor declares:

- stable `engine_id`;
- human-readable name;
- `capability_kind`;
- adapter kind;
- capabilities;
- protocol endpoints;
- optional metadata.

For engines that implement `cvs-sidecar-v1`, CVS no longer needs a hard-coded Python adapter class. The sidecar receives a normalized request containing input text, target language, model identity, speaker reference, optional emotion reference, VoiceBinding identity and effective parameters.

Built-in GPT-SoVITS and IndexTTS adapters remain because their current upstream APIs do not yet implement the generic sidecar contract.

## 2. Runtime registration is separate

Registering an Engine does not imply that a local Runtime exists.

A complete executable engine requires both:

```text
Engine Registry descriptor
+
Runtime Registry entry
```

The System Graph reports an `engine-without-runtime` warning when the semantic engine exists but no executable Runtime has been registered.

This is intentional: Engine identity survives even when the engine is not installed on a particular host.

## 3. Capability graph

Every Engine Registry descriptor declares a `capability_kind`, currently normally:

```text
speech-synthesis
```

System Graph materializes:

```text
engine:<id>
    |
    `-- implements_capability --> capability:speech-synthesis
```

This is the extension point for future capability families. The current production request path is still speech synthesis; non-speech capabilities require their own request contracts before they are executable.

## 4. Non-destructive host discovery

Run:

```powershell
.\scripts\discover_host_runtime.ps1
```

The scanner does not move, delete or modify discovered software.

It records a host inventory at:

```text
data/host-runtime-inventory.json
```

Current discovery sources include:

- all `python.exe` visible through PATH;
- all `ffmpeg.exe` / `ffprobe.exe` visible through PATH;
- Conda visible through PATH;
- Conda environments returned by `conda env list --json`;
- FFmpeg found inside Conda environments;
- dependency paths already declared by Runtime Registry.

The purpose is not to treat every executable as part of Character Voice System. Discovery only produces candidates.

## 5. Reconciliation

The authoritative reconciliation endpoint is:

```http
GET /v1/system/reconcile
X-CVS-Token: <admin token>
```

It compares:

```text
what exists on the host
        versus
what the system says it owns
```

It currently detects:

- registered Runtime without Engine descriptor;
- Engine descriptor without Runtime;
- Model referencing an unregistered Engine;
- declared dependency whose path no longer exists;
- one engine-private dependency claimed by multiple Runtimes;
- discovered Python / Conda environments not claimed by any Runtime;
- multiple host-visible Python / FFmpeg / Conda tool versions whose selection would otherwise depend on PATH order.

An unclaimed item is not automatically considered obsolete. It is a reconciliation candidate until ownership is established.

## 6. Ownership rule

Physical location is not ownership.

For example:

```text
C:\Users\...\miniconda3\envs\foo\python.exe
```

can remain where it is while Runtime Registry declares:

```text
runtime: index-tts-2.5-local
dependency: python-runtime
ownership: engine-private
```

At that point it is no longer semantically orphaned.

Conversely, moving ten unrelated environments into one directory does not integrate them.

## 7. Future engine onboarding

For a new speech engine that can expose Sidecar Protocol v1:

1. install/configure the engine wherever appropriate;
2. expose `/health` and `/v1/synthesize`;
3. copy `engines/sidecar.example.json` to `engines/<engine-id>.json`;
4. create a Runtime Registry entry with endpoint, lifecycle owner, dependencies and resource group;
5. register models in Model Registry;
6. create VoiceBindings where a character uses the model;
7. run host discovery and reconciliation;
8. inspect `GET /v1/system/graph`.

No new core CVS adapter code is required for this path.

For an upstream engine with an incompatible API, write one sidecar that translates its native API into Character Voice Sidecar Protocol v1. The incompatibility then remains outside CVS core.
