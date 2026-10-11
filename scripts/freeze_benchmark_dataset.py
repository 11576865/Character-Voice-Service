"""Command-line entry for immutable original-WAV dataset manifests."""

import argparse
import json
from pathlib import Path

from server.benchmark_dataset import freeze_dataset, verify_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze or verify a curated original-WAV TTS benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze", help="create an immutable manifest from curated JSONL")
    freeze.add_argument("--input", type=Path, required=True)
    freeze.add_argument("--audio-root", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--dataset-id", required=True)
    verify = sub.add_parser("verify", help="verify manifest fingerprint and source WAV hashes")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--audio-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze_dataset(args.input, args.audio_root, args.output, args.dataset_id)
        else:
            result = verify_dataset(args.manifest, args.audio_root)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"dataset {args.command} failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
