"""Run a frozen original-WAV holdout through the local CVS speech API."""

import argparse
import json
import os
from pathlib import Path

from server.voicebench import run_voicebench


def main() -> int:
    parser = argparse.ArgumentParser(description="CVS voicebench v1: pinned local holdout synthesis")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--voice", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--reference-id", required=True)
    parser.add_argument("--reference-item-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:9881")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--admin-token-file", type=Path, help="private CVS admin-token.txt; not logged")
    args = parser.parse_args()
    token = os.environ.get("CVS_ADMIN_TOKEN", "")
    if args.admin_token_file is not None:
        token = args.admin_token_file.read_text(encoding="utf-8").strip()
    try:
        result = run_voicebench(
            manifest_path=args.manifest, audio_root=args.audio_root,
            output_dir=args.output_dir, voice=args.voice, model_id=args.model_id,
            reference_id=args.reference_id, reference_item_id=args.reference_item_id,
            admin_token=token, base_url=args.base_url, speed=args.speed,
            timeout=args.timeout, resume=args.resume,
        )
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"voicebench failed: {exc}\n")
    print(json.dumps({
        "run_id": result["run_id"], "status": result["status"],
        "dataset_sha256": result["settings"]["dataset_sha256"],
        "items": len(result["items"]), "quality_evaluation": result["quality_evaluation"],
        "run_manifest": str(args.output_dir / "run.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
