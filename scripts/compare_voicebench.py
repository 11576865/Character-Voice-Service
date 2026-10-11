"""Verify two completed voicebench runs and freeze a descriptive comparison."""
import argparse
import json
from pathlib import Path

from server.voicebench_compare import compare_runs


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two completed voicebench runs without inferring speech quality")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--run-a", type=Path, required=True)
    parser.add_argument("--run-b", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare_runs(
            manifest_path=args.manifest, audio_root=args.audio_root,
            first_run=args.run_a, second_run=args.run_b, output=args.output,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(1, f"comparison refused: {exc}\n")
    print(json.dumps({
        "report_sha256": report["report_sha256"],
        "dataset_sha256": report["dataset"]["dataset_sha256"],
        "test_items": len(report["paired_items"]),
        "quality_evaluation": report["quality_evaluation"],
        "A": report["runs"]["A"]["summary"],
        "B": report["runs"]["B"]["summary"],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
