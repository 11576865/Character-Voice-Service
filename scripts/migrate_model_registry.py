import argparse
import json
from pathlib import Path

from server.config import VOICE_DIR
from server.model_registry import REGISTRY_PATH, migrate_profile
from server.voice_profiles import iter_real_profile_paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move GPT-SoVITS weight paths out of voices/*.json into the portable model registry."
    )
    parser.add_argument("--voice-dir", type=Path, default=VOICE_DIR)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    reports = []
    try:
        for path in iter_real_profile_paths(args.voice_dir):
            reports.append(
                migrate_profile(
                    path,
                    registry_path=args.registry,
                    backup=not args.no_backup,
                )
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Migration failed: {exc}\n")

    converted = sum(item["converted"] for item in reports)
    print(json.dumps(
        {
            "registry": str(args.registry),
            "profiles": reports,
            "converted_models": converted,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
