import errno
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from server.config import DATA_DIR, PROJECT_ROOT


MODEL_ROOT = Path(os.environ.get("CVS_MODEL_ROOT", str(DATA_DIR / "models"))).expanduser()
REGISTRY_PATH = DATA_DIR / "model-registry.json"
MANIFEST_FILENAME = "model.json"

SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = "1.0"
MANIFEST_SCHEMA_VERSIONS = {MANIFEST_SCHEMA_VERSION, "1.1"}
LIFECYCLE_STATES = {
    "discovered",
    "candidate",
    "validated",
    "default",
    "retired",
    "quarantined",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_id(value: object, field: str) -> str:
    item = str(value or "").strip()
    if not _ID_RE.fullmatch(item):
        raise ValueError(f"{field} must be a stable ID using letters, numbers, '.', '_' or '-'")
    return item


def _safe_component(value: str) -> str:
    item = _SAFE_RE.sub("-", value).strip(".-_")
    return item or "model"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _empty_registry() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "models": {},
        "defaults": {},
    }


def load_registry(path: Path = REGISTRY_PATH) -> dict:
    # Canonicalize a configured alias so readers and writers refer to the
    # same physical registry file rather than replacing a symlink itself.
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        return _empty_registry()

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported model registry schema")
    if not isinstance(data.get("models"), dict):
        raise ValueError("model registry models must be an object")
    if not isinstance(data.get("defaults", {}), dict):
        raise ValueError("model registry defaults must be an object")
    data.setdefault("defaults", {})
    return data


@contextmanager
def _registry_write_lock(path: Path, *, timeout: float = 120.0):
    """Advisory cross-process lock for the full registry read-modify-write.

    Never delete the .lock file: unlinking/recreating the inode would allow
    concurrent holders to believe they have mutually exclusive locks.
    Linux/macOS use flock; Windows uses msvcrt byte-range locking.
    """
    if timeout < 0:
        raise ValueError("registry lock timeout must not be negative")
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    # Open before attempting a lock; close releases the OS lock even if an
    # exception occurs. Never use an existence-test lockfile protocol.
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, 2)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            lock = lambda: msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            unlock = lambda: msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            lock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + timeout
        while True:
            try:
                lock()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EAGAIN)}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for Model Registry lock: {lock_path}") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            unlock()


def _save_registry_unlocked(data: dict, path: Path) -> None:
    """Only call inside _registry_write_lock (or from save_registry)."""
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with staging.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def save_registry(data: dict, path: Path = REGISTRY_PATH) -> None:
    """Publish a complete registry snapshot under the same writer lock.

    Do not use this API for stale read-modify-write snapshots; callers which
    mutate registry state must hold one lock from load through publication.
    """
    with _registry_write_lock(path):
        _save_registry_unlocked(data, path)


def _relative_artifact_path(value: object, field: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} path is required")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} path must stay inside the immutable model directory")
    return path


def validate_manifest(manifest_path: Path) -> dict:
    manifest_path = manifest_path.resolve()
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))

    if not isinstance(raw, dict):
        raise ValueError("model manifest root must be an object")
    schema_version = str(raw.get("schema_version"))
    if schema_version not in MANIFEST_SCHEMA_VERSIONS:
        raise ValueError("unsupported model manifest schema")

    model_id = _require_id(raw.get("model_id"), "model_id")
    scope = str(raw.get("scope") or "voice-bound").strip().lower()
    if scope not in {"voice-bound", "shared"}:
        raise ValueError(f"unsupported model scope: {scope}")

    raw_voice_id = str(raw.get("voice_id") or "").strip()
    if scope == "voice-bound":
        voice_id = _require_id(raw_voice_id, "voice_id")
    else:
        voice_id = _require_id(raw_voice_id, "voice_id") if raw_voice_id else None

    engine = raw.get("engine")
    if not isinstance(engine, dict):
        raise ValueError("engine must be an object")
    engine_name = _require_id(engine.get("name"), "engine.name")
    engine_version = str(engine.get("engine_version") or "").strip()
    adapter_api_version = str(engine.get("adapter_api_version") or "1").strip()

    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object")
    if not artifacts and scope != "shared":
        raise ValueError("artifacts must be a non-empty object for voice-bound models")

    resolved_artifacts = {}
    artifact_meta = {}
    for role, item in artifacts.items():
        role = _require_id(role, "artifact role")
        if not isinstance(item, dict):
            raise ValueError(f"artifact {role} must be an object")
        relative = _relative_artifact_path(item.get("path"), f"artifact {role}")
        artifact_path = (manifest_path.parent / relative).resolve()
        try:
            artifact_path.relative_to(manifest_path.parent)
        except ValueError as exc:
            raise ValueError(f"artifact {role} escapes the model directory") from exc
        if not artifact_path.is_file():
            raise ValueError(f"artifact {role} is missing: {relative.as_posix()}")

        expected_hash = str(item.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError(f"artifact {role} requires a SHA-256")
        actual_hash = sha256_file(artifact_path)
        if actual_hash != expected_hash:
            raise ValueError(f"artifact {role} SHA-256 mismatch")

        resolved_artifacts[role] = str(artifact_path)
        artifact_meta[role] = {
            "path": relative.as_posix(),
            "sha256": actual_hash,
        }

    lifecycle = raw.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        raise ValueError("lifecycle must be an object")
    initial_status = str(lifecycle.get("status") or "candidate").strip()
    if initial_status not in LIFECYCLE_STATES:
        raise ValueError(f"invalid lifecycle status: {initial_status}")

    serving = raw.get("serving") or {}
    if not isinstance(serving, dict):
        raise ValueError("serving must be an object")
    parameters = serving.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("serving.parameters must be an object")

    manifest_sha256 = sha256_file(manifest_path)
    return {
        "model_id": model_id,
        "scope": scope,
        "voice_id": voice_id,
        "name": str(raw.get("name") or model_id).strip() or model_id,
        "language": list(raw.get("language") or []),
        "engine": {
            "name": engine_name,
            "engine_version": engine_version,
            "adapter_api_version": adapter_api_version,
        },
        "artifacts": resolved_artifacts,
        "artifact_meta": artifact_meta,
        "training": dict(raw.get("training") or {}),
        "runtime": dict(raw.get("runtime") or {}),
        "capabilities": dict(raw.get("capabilities") or {}),
        "parameters": dict(parameters),
        "initial_status": initial_status,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "revision": manifest_sha256,
    }


def _registry_relative(path: Path, model_root: Path) -> str:
    return path.resolve().relative_to(model_root.resolve()).as_posix()


def scan_model_root(
    model_root: Path = MODEL_ROOT,
    registry_path: Path = REGISTRY_PATH,
) -> dict:
    # Include discovery itself in the same transaction: concurrent scans
    # must not publish stale inventories in reverse scan-completion order.
    with _registry_write_lock(registry_path):
        model_root = model_root.expanduser().resolve()
        model_root.mkdir(parents=True, exist_ok=True)

        discovered: dict[str, dict] = {}
        duplicate_ids: set[str] = set()
        invalid: list[dict] = []

        for manifest_path in sorted(model_root.rglob(MANIFEST_FILENAME)):
            try:
                # Resolve provenance path inside the guarded block: a model.json
                # symlink can point outside Model Root even when rglob found it
                # inside. It must be reported invalid, not abort the entire scan.
                relative_manifest = _registry_relative(manifest_path, model_root)
                model = validate_manifest(manifest_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                invalid.append({"manifest": str(manifest_path), "error": str(exc)})
                continue

            model_id = model["model_id"]
            if model_id in duplicate_ids:
                invalid.append({
                    "manifest": relative_manifest,
                    "error": f"duplicate model_id: {model_id}",
                })
                continue

            if model_id in discovered:
                # Two copies of an identical manifest are still ambiguous:
                # changing scan order must not change the Model Root locator.
                duplicate_ids.add(model_id)
                discovered.pop(model_id)
                invalid.append({
                    "manifest": relative_manifest,
                    "error": f"duplicate model_id across directories: {model_id}",
                })
                continue

            discovered[model_id] = {
                "scope": model["scope"],
                "voice_id": model["voice_id"],
                "engine": model["engine"]["name"],
                "engine_version": model["engine"]["engine_version"],
                "manifest": relative_manifest,
                "manifest_sha256": model["manifest_sha256"],
                "revision": model["revision"],
                "initial_status": model["initial_status"],
            }

        registry = load_registry(registry_path)
        now = _now_iso()
        for model_id, item in discovered.items():
            existing = registry["models"].get(model_id)
            if existing and existing.get("manifest_sha256") not in {None, item["manifest_sha256"]}:
                existing["status"] = "quarantined"
                existing["present"] = True
                existing["updated_at"] = now
                existing["integrity_error"] = "immutable manifest changed after registration"
                invalid.append({
                    "manifest": item["manifest"],
                    "error": f"immutable manifest changed after registration: {model_id}",
                })
                continue

            if existing:
                status = existing.get("status", item["initial_status"])
                if status not in LIFECYCLE_STATES:
                    status = "candidate"
                first_seen = existing.get("discovered_at") or now
            else:
                status = item["initial_status"]
                first_seen = now

            registry["models"][model_id] = {
                "scope": item["scope"],
                "voice_id": item["voice_id"],
                "engine": item["engine"],
                "engine_version": item["engine_version"],
                "manifest": item["manifest"],
                "manifest_sha256": item["manifest_sha256"],
                "revision": item["revision"],
                "status": status,
                "present": True,
                "discovered_at": first_seen,
                "updated_at": now,
            }

        # Conflicting physical identities cannot safely retain any previous
        # default/validated status. Existing entries stay inspectable but cannot
        # be resolved or promoted while the collision remains.
        for model_id in duplicate_ids:
            existing = registry["models"].get(model_id)
            if isinstance(existing, dict):
                existing["status"] = "quarantined"
                existing["present"] = False
                existing["updated_at"] = now
                existing["integrity_error"] = "duplicate model_id in Model Root"

        discovered_ids = set(discovered)
        for model_id, entry in registry["models"].items():
            if model_id not in discovered_ids:
                entry["present"] = False
                entry["updated_at"] = now

        _save_registry_unlocked(registry, registry_path)
        return {
            "model_root": str(model_root),
            "discovered": len(discovered),
            "invalid": invalid,
            "model_ids": sorted(discovered),
        }


def _entry(model_id: str, registry_path: Path = REGISTRY_PATH) -> tuple[dict, dict]:
    model_id = _require_id(model_id, "model_id")
    registry = load_registry(registry_path)
    entry = registry["models"].get(model_id)
    if not isinstance(entry, dict):
        raise ValueError(f"model registry entry not found: {model_id}")
    return registry, entry


def resolve_model(
    model_id: str,
    *,
    model_root: Path = MODEL_ROOT,
    registry_path: Path = REGISTRY_PATH,
) -> dict:
    _, entry = _entry(model_id, registry_path)
    if not entry.get("present", True):
        raise ValueError(f"model is not present in Model Root: {model_id}")
    if entry.get("status") == "quarantined" or entry.get("integrity_error"):
        raise ValueError(f"model is quarantined pending integrity review: {model_id}")

    manifest_rel = str(entry.get("manifest") or "").strip()
    if not manifest_rel:
        raise ValueError(f"model registry entry has no manifest: {model_id}")

    manifest_path = (model_root.expanduser().resolve() / manifest_rel).resolve()
    try:
        manifest_path.relative_to(model_root.expanduser().resolve())
    except ValueError as exc:
        raise ValueError(f"model manifest escapes Model Root: {model_id}") from exc

    model = validate_manifest(manifest_path)
    if model["model_id"] != model_id:
        raise ValueError(f"model manifest identity mismatch: {model_id}")
    if model["manifest_sha256"] != entry.get("manifest_sha256"):
        raise ValueError(
            f"immutable model manifest changed for {model_id}; run model sync and inspect before use"
        )

    model["status"] = entry.get("status", "candidate")
    return model


def list_models(
    *,
    model_root: Path = MODEL_ROOT,
    registry_path: Path = REGISTRY_PATH,
) -> list[dict]:
    registry = load_registry(registry_path)
    items = []
    for model_id in sorted(registry["models"]):
        entry = registry["models"][model_id]
        item = {
            "model_id": model_id,
            "scope": entry.get("scope", "voice-bound"),
            "voice_id": entry.get("voice_id"),
            "engine": entry.get("engine"),
            "engine_version": entry.get("engine_version"),
            "status": entry.get("status"),
            "present": bool(entry.get("present", True)),
            "revision": entry.get("revision"),
            "default_for_voice": (
                entry.get("status") == "default"
                and entry.get("present") is True
                and not entry.get("integrity_error")
                and registry.get("defaults", {}).get(entry.get("voice_id")) == model_id
            ),
        }
        if item["present"]:
            try:
                resolved = resolve_model(model_id, model_root=model_root, registry_path=registry_path)
                item["name"] = resolved["name"]
                item["language"] = resolved["language"]
                item["capabilities"] = resolved["capabilities"]
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                item["error"] = str(exc)
        items.append(item)
    return items


def default_model_id(
    voice_id: str,
    *,
    registry_path: Path = REGISTRY_PATH,
) -> str | None:
    voice_id = _require_id(voice_id, "voice_id")
    registry = load_registry(registry_path)
    model_id = registry.get("defaults", {}).get(voice_id)
    entry = registry["models"].get(model_id)
    if (not isinstance(entry, dict) or entry.get("status") != "default"
            or entry.get("present") is not True or entry.get("integrity_error")):
        return None
    return str(model_id)


def set_status(
    model_id: str,
    status: str,
    *,
    registry_path: Path = REGISTRY_PATH,
) -> None:
    status = str(status).strip()
    if status not in LIFECYCLE_STATES:
        raise ValueError(f"invalid lifecycle status: {status}")
    with _registry_write_lock(registry_path):
        registry, entry = _entry(model_id, registry_path)
        entry["status"] = status
        entry["updated_at"] = _now_iso()
        _save_registry_unlocked(registry, registry_path)


def promote_model(
    model_id: str,
    *,
    registry_path: Path = REGISTRY_PATH,
    require_evaluation: bool = True,
    evaluation_dir: Path | None = None,
    benchmark_dir: Path | None = None,
    model_root: Path = MODEL_ROOT,
) -> None:
    from server.evaluation_registry import model_is_promotable

    with _registry_write_lock(registry_path):
        registry, entry = _entry(model_id, registry_path)
        if entry.get("scope", "voice-bound") == "shared":
            raise ValueError("shared models cannot be promoted as a per-voice default")
        if entry.get("status") not in {"validated", "default"}:
            raise ValueError("only a validated model can be promoted")
        if entry.get("present") is not True:
            raise ValueError("model is not present in Model Root; cannot promote")
        if entry.get("integrity_error"):
            raise ValueError("quarantined model integrity error; cannot promote")

        # The persisted registry and evaluation are only claims. Re-hash the
        # currently installed immutable manifest and ALL recorded artifact bytes
        # before creating or changing a serving default, even with an explicit
        # require_evaluation=False administrative override.
        resolved = resolve_model(model_id, model_root=model_root, registry_path=registry_path)
        if (resolved.get("revision") != entry.get("revision")
                or resolved.get("scope") != entry.get("scope", "voice-bound")
                or resolved.get("voice_id") != entry.get("voice_id")):
            raise ValueError("model identity changed since registry scan; cannot promote")
        if require_evaluation:
            evaluation_options = {"model_revision": entry.get("revision")}
            if evaluation_dir is not None:
                evaluation_options["directory"] = evaluation_dir
            if benchmark_dir is not None:
                evaluation_options["benchmark_dir"] = benchmark_dir
            if not model_is_promotable(model_id, **evaluation_options):
                raise ValueError("model has no provenance-matched promotable validated evaluation")

        voice_id = _require_id(entry.get("voice_id"), "voice_id")
        previous_id = registry.get("defaults", {}).get(voice_id)
        if previous_id and previous_id != model_id:
            previous = registry["models"].get(previous_id)
            if isinstance(previous, dict) and previous.get("status") == "default":
                previous["status"] = "validated"
                previous["updated_at"] = _now_iso()

        entry["status"] = "default"
        entry["updated_at"] = _now_iso()
        registry.setdefault("defaults", {})[voice_id] = model_id
        _save_registry_unlocked(registry, registry_path)


def retire_model(model_id: str, *, registry_path: Path = REGISTRY_PATH) -> None:
    with _registry_write_lock(registry_path):
        registry, entry = _entry(model_id, registry_path)
        voice_id = entry.get("voice_id")
        if voice_id and registry.get("defaults", {}).get(voice_id) == model_id:
            raise ValueError("cannot retire the current default model before promoting another model")
        entry["status"] = "retired"
        entry["updated_at"] = _now_iso()
        _save_registry_unlocked(registry, registry_path)


def _legacy_search_roots() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("CVS_MODEL_SOURCE_ROOTS", "")
    for item in configured.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser())
    gpt_root = os.environ.get("CVS_GPT_SOVITS_ROOT", "").strip()
    if gpt_root:
        roots.append(Path(gpt_root).expanduser())
    roots.append(PROJECT_ROOT.parent / "GPT-SoVITS")

    unique: list[Path] = []
    seen = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def locate_legacy_weight(path_value: str, expected_suffix: str) -> Path:
    original = Path(path_value)
    if original.is_file():
        return original.resolve()

    candidates: list[Path] = []
    for root in _legacy_search_roots():
        if not root.is_dir():
            continue
        try:
            candidates.extend(
                path.resolve()
                for path in root.rglob(original.name)
                if path.is_file() and path.suffix.lower() == expected_suffix.lower()
            )
        except OSError:
            continue

    if not candidates:
        raise ValueError(
            f"legacy model weight not found: {path_value}. "
            "Set CVS_MODEL_SOURCE_ROOTS or CVS_GPT_SOVITS_ROOT if the weights live elsewhere."
        )

    old_parts = [item.casefold() for item in original.parts]

    def score(candidate: Path) -> tuple[int, int]:
        new_parts = [item.casefold() for item in candidate.parts]
        suffix = 0
        for left, right in zip(reversed(old_parts), reversed(new_parts)):
            if left != right:
                break
            suffix += 1
        return suffix, -len(candidate.parts)

    ranked = sorted(candidates, key=score, reverse=True)
    if len(ranked) > 1 and score(ranked[0]) == score(ranked[1]):
        raise ValueError(
            f"ambiguous legacy weight {original.name!r}: "
            + ", ".join(str(item) for item in ranked[:4])
        )
    return ranked[0]


def _assert_import_destination(destination: Path, model_root: Path) -> None:
    """Reject symlink-controlled paths within the configured Model Root.

    This is a preflight guard, not protection against a hostile concurrent
    process replacing filesystem components mid-write.
    """
    # A configured Model Root may itself be a symlink/mounted alias.
    # Normalize its lexical prefix before checking each *child* component.
    lexical_root = Path(os.path.abspath(model_root.expanduser()))
    root = lexical_root.resolve()
    lexical = Path(os.path.abspath(destination.expanduser()))
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError as exc:
        raise ValueError("model import destination escapes Model Root") from exc
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError(f"model import destination contains a symlink: {cursor}")
    if not lexical.resolve().is_relative_to(root):
        raise ValueError("model import destination escapes Model Root")


def _copy_immutable(
    source: Path, destination: Path, expected_hash: str, *, model_root: Path,
) -> None:
    """Stage, hash, then publish an immutable artifact without overwriting."""
    _assert_import_destination(destination, model_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_import_destination(destination, model_root)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != expected_hash:
            raise ValueError(f"immutable artifact already exists with different content: {destination}")
        return

    staging = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        digest = hashlib.sha256()
        with source.open("rb") as incoming, staging.open("xb") as outgoing:
            while block := incoming.read(1024 * 1024):
                outgoing.write(block)
                digest.update(block)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if digest.hexdigest() != expected_hash or sha256_file(staging) != expected_hash:
            raise ValueError(f"source artifact changed during immutable copy: {source}")
        _assert_import_destination(destination, model_root)
        try:
            # Hard-link publication is create-only on POSIX and Windows NTFS:
            # unlike rename/replace it cannot overwrite an existing artifact.
            os.link(staging, destination)
        except FileExistsError:
            if (not destination.is_file()
                    or destination.is_symlink()
                    or sha256_file(destination) != expected_hash):
                raise ValueError(f"immutable artifact was concurrently replaced: {destination}")
    finally:
        staging.unlink(missing_ok=True)


def _import_model_id(voice_id: str, source_model_id: str, version: str, identity_hash: str) -> str:
    """Preserve source-content identity when the descriptive prefix is long."""
    if not re.fullmatch(r"[0-9a-f]{12}", identity_hash):
        raise ValueError("model import fingerprint requires twelve lowercase hex digits")
    prefix = _safe_component(f"{voice_id}-gpt-sovits-{version}-{source_model_id}")
    # Reserve the entire suffix, including its separator, before truncation.
    prefix = prefix[:127 - 1 - len(identity_hash)].rstrip(".-_") or "model"
    model_id = f"{prefix}-{identity_hash}"
    return _require_id(model_id, "model_id")


def _equivalent_import_manifest(existing: dict, proposed: dict) -> bool:
    """Only the creation timestamp may vary across an idempotent retry."""
    if not isinstance(existing, dict):
        return False
    left, right = json.loads(json.dumps(existing)), json.loads(json.dumps(proposed))
    for item in (left, right):
        lifecycle = item.get("lifecycle")
        if isinstance(lifecycle, dict):
            lifecycle.pop("created_at", None)
    return left == right


def import_gpt_sovits_model(
    *,
    voice_id: str,
    source_model_id: str,
    name: str,
    version: str,
    gpt_weights: str,
    sovits_weights: str,
    parameters: dict | None = None,
    language: list[str] | None = None,
    model_root: Path = MODEL_ROOT,
    registry_path: Path = REGISTRY_PATH,
) -> str:
    voice_id = _require_id(voice_id, "voice_id")
    source_model_id = _require_id(source_model_id, "source model id")
    gpt_source = locate_legacy_weight(gpt_weights, ".ckpt")
    sovits_source = locate_legacy_weight(sovits_weights, ".pth")

    gpt_hash = sha256_file(gpt_source)
    sovits_hash = sha256_file(sovits_source)
    identity_hash = hashlib.sha256(
        f"{gpt_hash}:{sovits_hash}".encode("ascii")
    ).hexdigest()[:12]

    engine_version = str(version or "").strip() or "unknown"
    model_id = _import_model_id(voice_id, source_model_id, engine_version, identity_hash)

    model_dir = (
        model_root.expanduser()
        / _safe_component(voice_id)
        / "gpt-sovits"
        / _safe_component(model_id)
    )
    artifacts_dir = model_dir / "artifacts"
    gpt_destination = artifacts_dir / "gpt.ckpt"
    sovits_destination = artifacts_dir / "sovits.pth"

    _assert_import_destination(model_dir / MANIFEST_FILENAME, model_root)
    _copy_immutable(gpt_source, gpt_destination, gpt_hash, model_root=model_root)
    _copy_immutable(sovits_source, sovits_destination, sovits_hash, model_root=model_root)

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": model_id,
        "voice_id": voice_id,
        "name": str(name or source_model_id).strip() or source_model_id,
        "language": list(language or []),
        "engine": {
            "name": "gpt-sovits",
            "engine_version": engine_version,
            "adapter_api_version": "1",
        },
        "artifacts": {
            "gpt": {"path": "artifacts/gpt.ckpt", "sha256": gpt_hash},
            "sovits": {"path": "artifacts/sovits.pth", "sha256": sovits_hash},
        },
        "training": {},
        "runtime": {},
        "capabilities": {
            "fine_tuned_model": True,
            "zero_shot": True,
            "emotion": "reference",
        },
        "serving": {"parameters": dict(parameters or {})},
        "lifecycle": {
            "status": "candidate",
            "imported_from": "legacy-character-profile",
            "created_at": _now_iso(),
        },
    }

    model_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = model_dir / MANIFEST_FILENAME
    _assert_import_destination(manifest_path, model_root)
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not _equivalent_import_manifest(existing, manifest):
            raise ValueError(f"immutable model directory already contains a different manifest: {model_dir}")
    else:
        # No truncatable manifest on a partially successful write, and no
        # overwrite of a concurrently created immutable manifest.
        staged_manifest = manifest_path.with_name("model." + uuid.uuid4().hex + ".tmp")
        try:
            with staged_manifest.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _assert_import_destination(manifest_path, model_root)
            try:
                os.link(staged_manifest, manifest_path)
            except FileExistsError:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not _equivalent_import_manifest(existing, manifest):
                    raise ValueError(f"concurrent immutable model manifest differs: {model_dir}")
        finally:
            staged_manifest.unlink(missing_ok=True)

    scan_model_root(model_root=model_root, registry_path=registry_path)
    return model_id


def migrate_profile(
    profile_path: Path,
    *,
    model_root: Path = MODEL_ROOT,
    registry_path: Path = REGISTRY_PATH,
    backup: bool = True,
) -> dict:
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    models = raw.get("models")
    if not isinstance(models, dict):
        return {"profile": str(profile_path), "converted": 0, "unchanged": 0}

    converted = 0
    unchanged = 0
    for local_model_id, model in models.items():
        if not isinstance(model, dict):
            unchanged += 1
            continue
        if model.get("model_id"):
            unchanged += 1
            continue

        gpt = str(model.get("gpt_weights") or "").strip()
        sovits = str(model.get("sovits_weights") or "").strip()
        if not gpt and not sovits:
            unchanged += 1
            continue
        if not gpt or not sovits:
            raise ValueError(
                f"{profile_path.name}:{local_model_id} has only one GPT-SoVITS weight path"
            )

        global_model_id = import_gpt_sovits_model(
            voice_id=profile_path.stem,
            source_model_id=str(local_model_id),
            name=str(model.get("name") or local_model_id),
            version=str(model.get("version") or ""),
            gpt_weights=gpt,
            sovits_weights=sovits,
            parameters=dict(model.get("parameters") or {}),
            language=[str(raw.get("target_language") or "")] if raw.get("target_language") else [],
            model_root=model_root,
            registry_path=registry_path,
        )
        model["model_id"] = global_model_id
        model.pop("gpt_weights", None)
        model.pop("sovits_weights", None)
        converted += 1

    if converted:
        if backup:
            backup_path = profile_path.with_suffix(".json.pre-model-registry.bak")
            if not backup_path.exists():
                shutil.copy2(profile_path, backup_path)
        staging = profile_path.with_suffix(".json.tmp")
        staging.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.replace(profile_path)

    return {
        "profile": str(profile_path),
        "converted": converted,
        "unchanged": unchanged,
    }
