"""Read-only GPT-SoVITS model inventory. Does not load weights or edit profiles."""

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


SAFE_MANIFEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

def contained_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise ValueError("File is missing or resolves outside the configured root")
    return candidate


def fingerprint(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("File changed while hashing; retry after training finishes")
    return digest.hexdigest()


def revision_id(layout: str, artifacts: list[dict]) -> str | None:
    if not artifacts or any(not item.get("sha256") for item in artifacts):
        return None
    identity = {"engine": "gpt-sovits", "layout": layout,
                "artifacts": sorted((item["kind"], item["sha256"]) for item in artifacts)}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "rev-" + hashlib.sha256(encoded).hexdigest()


def read_roots(path: Path) -> list[dict]:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1 or not isinstance(config.get("roots"), list):
        raise ValueError("Expected schema_version=1 and a roots list")
    seen = set()
    for root in config["roots"]:
        if not isinstance(root, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(root.get("id", ""))):
            raise ValueError("Each root needs a stable id")
        if root["id"] in seen:
            raise ValueError("Duplicate root id: " + root["id"])
        seen.add(root["id"])
        if root.get("engine") != "gpt-sovits":
            raise ValueError("Scanner currently supports only gpt-sovits")
        if not isinstance(root.get("path"), str) or not Path(root["path"]).is_absolute():
            raise ValueError("Root path must be absolute")
    return config["roots"]


def scan(roots: list[dict], voice_dir: Path, *, full_hash: bool = False) -> dict:
    report = {"schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
              "mode": "sha256" if full_hash else "metadata_only", "roots": [],
              "models": [], "profile_links": [], "issues": []}
    profiles = []
    if not voice_dir.is_dir():
        report["issues"].append({"type": "profile_directory_unavailable", "path": str(voice_dir)})
    for path in sorted(voice_dir.glob("*.json")):
        if path.name.lower() == "example.json":
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            models = raw.get("models", {"loaded": {}} if raw.get("reference_audio") else {})
            if not isinstance(models, dict):
                raise ValueError("models must be an object")
            for model_id, model in models.items():
                if not isinstance(model, dict):
                    raise ValueError("model must be an object")
                profiles.append((path.stem, model_id, model, str(raw.get("target_language") or ""),
                                 model_id == raw.get("default_model")))
        except (OSError, ValueError, AttributeError) as exc:
            report["issues"].append({"type": "invalid_profile", "file": path.name, "detail": str(exc)})

    discovered = {}
    artifact_cache = {}
    for config in roots:
        root = Path(config["path"]).resolve()
        if not root.is_dir():
            report["roots"].append({"id": config["id"], "status": "root_unavailable"})
            continue
        groups = {}
        declared_models = []
        claimed_paths = set()
        invalid_groups = set()
        try:
            for manifest_path in sorted(root.rglob("*.cvs-model.json")):
                try:
                    if not manifest_path.resolve().is_relative_to(root):
                        raise ValueError("Manifest resolves outside root")
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    if manifest.get("schema_version") != 1 or manifest.get("engine") != "gpt-sovits":
                        raise ValueError("Expected schema_version=1 and engine=gpt-sovits")
                    assignment = {key: str(manifest.get(key) or "").strip()
                                  for key in ("character_id", "model_id", "target_language")}
                    if not SAFE_MANIFEST_ID.fullmatch(assignment["character_id"]) or \
                            not SAFE_MANIFEST_ID.fullmatch(assignment["model_id"]):
                        raise ValueError("Manifest needs stable character_id and model_id")
                    raw_artifacts = manifest.get("artifacts")
                    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != {"gpt", "sovits"}:
                        raise ValueError("Manifest artifacts must contain exactly gpt and sovits")
                    artifacts = []
                    for kind in ("gpt", "sovits"):
                        path = contained_file(root, str(raw_artifacts[kind]))
                        stat = path.stat()
                        if not stat.st_size:
                            raise ValueError("Empty model artifact")
                        relative = path.relative_to(root).as_posix()
                        claimed_paths.add(relative.casefold())
                        artifacts.append({"kind": kind, "relative_path": relative, "size": stat.st_size,
                                          "mtime_ns": stat.st_mtime_ns,
                                          "sha256": fingerprint(path) if full_hash else None})
                    layout = str(manifest.get("layout") or "manifest").strip()
                    name = str(manifest.get("name") or assignment["model_id"]).strip()
                    declared_models.append({"root_id": config["id"], "layout": layout, "name": name,
                                            "status": "paired", "artifacts": artifacts,
                                            "revision_id": revision_id(layout, artifacts),
                                            "profile_matches": [], "association": "declared",
                                            "declared_assignment": assignment,
                                            "discovery_key": (f'manifest:{config["id"]}:'
                                                              f'{assignment["character_id"]}:'
                                                              f'{assignment["model_id"]}')})
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    report["issues"].append({"type": "invalid_manifest",
                                             "path": manifest_path.relative_to(root).as_posix(),
                                             "detail": str(exc)})
            folders = sorted(root.iterdir())
            for folder in folders:
                match = re.fullmatch(r"(GPT|SoVITS)_weights(?:_(.+))?", folder.name)
                if not match or not folder.is_dir():
                    continue
                if not folder.resolve().is_relative_to(root):
                    report["issues"].append({"type": "invalid", "root_id": config["id"],
                                             "path": folder.name, "detail": "Directory resolves outside root"})
                    continue
                kind = "gpt" if match[1] == "GPT" else "sovits"
                layout = match[2] or "legacy"
                suffix = ".ckpt" if kind == "gpt" else ".pth"
                pattern = r"(.+)-e\d+\.ckpt" if kind == "gpt" else r"(.+)_e\d+.*\.pth"
                for path in sorted(folder.glob("*" + suffix)):
                    relative = path.relative_to(root).as_posix()
                    if relative.casefold() in claimed_paths:
                        continue
                    name = re.fullmatch(pattern, path.name)
                    try:
                        checked = contained_file(root, relative)
                        stat = checked.stat()
                        if not stat.st_size:
                            raise ValueError("Empty model file")
                        artifact = {"kind": kind, "relative_path": relative, "size": stat.st_size,
                                    "mtime_ns": stat.st_mtime_ns, "sha256": fingerprint(checked) if full_hash else None}
                        artifact_cache[str(checked)] = (config["id"], layout, artifact)
                    except (OSError, ValueError) as exc:
                        if name:
                            invalid_groups.add((layout, name[1]))
                            groups.setdefault((layout, name[1]), {"gpt": [], "sovits": []})
                        report["issues"].append({"type": "invalid", "root_id": config["id"],
                                                 "path": relative, "detail": str(exc)})
                        continue
                    if not name:
                        report["issues"].append({"type": "unrecognized", "root_id": config["id"], "path": relative})
                        continue
                    groups.setdefault((layout, name[1]), {"gpt": [], "sovits": []})[kind].append(artifact)
            report["roots"].append({"id": config["id"], "status": "available"})
        except OSError as exc:
            report["roots"].append({"id": config["id"], "status": "scan_error", "detail": str(exc)})
        report["models"].extend(declared_models)
        for item in declared_models:
            parts = {artifact["kind"]: artifact for artifact in item["artifacts"]}
            key = tuple(str((root / parts[k]["relative_path"]).resolve()) for k in ("gpt", "sovits"))
            discovered[key] = item
        for (layout, name), parts in groups.items():
            paired = len(parts["gpt"]) == len(parts["sovits"]) == 1 and (layout, name) not in invalid_groups
            status = ("invalid" if (layout, name) in invalid_groups else
                      "paired" if paired else "incomplete" if not all(parts.values()) else "ambiguous")
            artifacts = parts["gpt"] + parts["sovits"]
            item = {"root_id": config["id"], "layout": layout, "name": name, "status": status,
                    "artifacts": artifacts, "revision_id": revision_id(layout, artifacts) if paired else None,
                    "profile_matches": [], "association": "unregistered",
                    "discovery_key": f'{config["id"]}:{layout}:{name}'}
            report["models"].append(item)
            if paired:
                key = tuple(str((root / parts[k][0]["relative_path"]).resolve()) for k in ("gpt", "sovits"))
                discovered[key] = item

    for character, model_id, model, target_language, is_default in profiles:
        link = {"character_id": character, "model_id": model_id, "target_language": target_language,
                "is_default": is_default, "revision_id": None}
        paths = [model.get("gpt_weights"), model.get("sovits_weights")]
        if not any(paths):
            link["status"] = "unmanaged"
        elif not all(isinstance(path, str) and Path(path).is_absolute() for path in paths):
            link["status"] = "invalid_paths"
        else:
            key = tuple(str(Path(path).resolve()) for path in paths)
            if key in discovered:
                item = discovered[key]
                item["profile_matches"].append({"character_id": character, "model_id": model_id,
                                                "target_language": target_language,
                                                "is_default": is_default})
                item["association"] = "registered"
                link.update(status="registered", revision_id=item["revision_id"])
            elif any(not Path(path).is_file() for path in paths):
                link["status"] = "missing"
            elif all(path in artifact_cache for path in key):
                parts = [artifact_cache[path] for path in key]
                if parts[0][:2] == parts[1][:2] and [part[2]["kind"] for part in parts] == ["gpt", "sovits"]:
                    link.update(status="registered_explicit_pair", root_id=parts[0][0],
                                artifacts=[part[2] for part in parts],
                                revision_id=revision_id(parts[0][1], [part[2] for part in parts]))
                else:
                    link["status"] = "incompatible_layout"
            else:
                link["status"] = "outside_scan_or_invalid"
        report["profile_links"].append(link)
    report["summary"] = {"groups": len(report["models"]),
                         "paired": sum(item["status"] == "paired" for item in report["models"]),
                         "profile_links": len(report["profile_links"]), "issues": len(report["issues"])}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", type=Path, required=True, help="Local roots JSON configuration")
    parser.add_argument("--voices", type=Path, default=Path(__file__).resolve().parent.parent / "voices")
    parser.add_argument("--hash", action="store_true", help="Read complete weights to calculate content revisions")
    args = parser.parse_args()
    try:
        result = scan(read_roots(args.roots), args.voices, full_hash=args.hash)
    except (OSError, ValueError, AttributeError) as exc:
        parser.exit(2, f"Scan configuration error: {exc}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
