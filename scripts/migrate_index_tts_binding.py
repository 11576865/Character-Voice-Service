from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.config import VOICE_DIR
from server.model_registry import MODEL_ROOT, scan_model_root
from server.voice_bindings import register_binding


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote a transitional inline IndexTTS alias into a shared model + VoiceBinding."
    )
    parser.add_argument("--voice", required=True, help="voice id, e.g. march-7th")
    parser.add_argument("--alias", default="index-tts-2.5")
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("INDEX_TTS_SOURCE_REVISION", ""),
        help="optional upstream IndexTTS source/model revision",
    )
    args = parser.parse_args()

    profile_path = VOICE_DIR / f"{args.voice}.json"
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    alias = (raw.get("models") or {}).get(args.alias)
    if not isinstance(alias, dict) or str(alias.get("engine") or "").lower() != "index-tts":
        raise SystemExit(
            f"IndexTTS alias {args.alias!r} not found in {profile_path.name}"
        )

    model_id = args.alias
    model_dir = MODEL_ROOT / "_shared" / "index-tts" / model_id
    manifest_path = model_dir / "model.json"
    manifest = {
        "schema_version": "1.1",
        "scope": "shared",
        "model_id": model_id,
        "name": str(alias.get("name") or "IndexTTS 2.5 shared runtime"),
        "language": ["zh", "en", "ja", "es", "ar"],
        "engine": {
            "name": "index-tts",
            "engine_version": str(alias.get("version") or "2.5"),
            "adapter_api_version": "1",
        },
        "artifacts": {},
        "training": {},
        "runtime": {
            "ownership": "external-sidecar",
            "endpoint": "http://127.0.0.1:9882",
            "precision": "bf16",
            "source_revision": str(args.source_revision or "").strip() or None,
        },
        "capabilities": {
            "zero_shot": True,
            "shared_engine_model": True,
            "emotion_reference": True,
            "emotion_vector": True,
            "emotion_text": False,
            "max_concurrency": 1,
            "output_sample_rate": 22050,
        },
        "serving": {"parameters": {}},
        "lifecycle": {"status": "validated"},
    }

    model_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing != manifest:
            raise SystemExit(
                f"shared model manifest already exists with different content: {manifest_path}"
            )
    else:
        manifest_path.write_text(encoded, encoding="utf-8")

    scan_model_root()

    default_reference = str(raw.get("default_reference") or "").strip()
    if not default_reference:
        raise SystemExit("profile has no default_reference")

    binding = register_binding({
        "binding_id": f"{args.voice}-{model_id}",
        "voice_id": args.voice,
        "engine": "index-tts",
        "model_id": model_id,
        "speaker_reference_id": default_reference,
        "emotion_reference_id": None,
        "emotion_policy": "speaker",
        "parameters": dict(alias.get("parameters") or {}),
        "enabled": True,
    })

    print(json.dumps({
        "model_manifest": str(manifest_path),
        "model_id": model_id,
        "binding": binding,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
