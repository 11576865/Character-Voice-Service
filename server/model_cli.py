"""Local command line for model scanning, lifecycle, and Evaluation records."""

import argparse
import json
from pathlib import Path

from server.config import DATA_DIR, VOICE_DIR
from server.model_management import ModelManager


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR / "model-management")
    parser.add_argument("--voices", type=Path, default=VOICE_DIR)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Scan configured roots")
    scan.add_argument("--hash", action="store_true")
    scan.add_argument("--register", action="store_true")
    commands.add_parser("list", help="Show the Registry")
    assign = commands.add_parser("assign", help="Assign an unassigned candidate")
    assign.add_argument("revision_id")
    assign.add_argument("--character", required=True)
    assign.add_argument("--model-id", required=True)
    assign.add_argument("--language", default="")
    assign.add_argument("--reason", required=True)
    promote = commands.add_parser("promote", help="Promote a revision and activate it in the profile")
    promote.add_argument("revision_id")
    promote.add_argument("--reason", required=True)
    promote.add_argument("--evaluation")
    promote.add_argument("--manual-override", action="store_true")
    retire = commands.add_parser("retire", help="Retire a non-default revision")
    retire.add_argument("revision_id")
    retire.add_argument("--reason", required=True)
    evaluate = commands.add_parser("evaluation-new", help="Create an Evaluation from a sample-set JSON")
    evaluate.add_argument("--character", required=True)
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--baseline")
    evaluate.add_argument("--samples", type=Path, required=True)
    evaluate.add_argument("--engine", default="gpt-sovits")
    evaluate.add_argument("--adapter-version", default="1")
    evaluate.add_argument("--reference-set", default="")
    commands.add_parser("evaluation-list", help="List Evaluation records")
    update = commands.add_parser("evaluation-update", help="Apply status/result fields from a JSON object")
    update.add_argument("evaluation_id")
    update.add_argument("--json", type=Path, required=True)
    review = commands.add_parser("evaluation-review", help="Append an A/B human review")
    review.add_argument("evaluation_id")
    review.add_argument("--pair", required=True)
    review.add_argument("--preference", required=True, choices=["a", "b", "similar", "both_problematic"])
    review.add_argument("--ratings", type=Path, required=True)
    review.add_argument("--notes", default="")
    decision = commands.add_parser("evaluation-decision", help="Record a completed Evaluation decision")
    decision.add_argument("evaluation_id")
    decision.add_argument("decision", choices=["promote", "keep_default", "reject", "inconclusive"])
    decision.add_argument("--reason", required=True)
    args = parser.parse_args()
    manager = ModelManager(args.data, args.voices)
    try:
        if args.command == "scan":
            if args.register and not args.hash:
                parser.error("--register requires --hash")
            output(manager.scan(full_hash=args.hash, register=args.register))
        elif args.command == "list":
            output(manager.registry.read())
        elif args.command == "assign":
            output(manager.assign(args.revision_id, args.character, args.model_id,
                                  args.language, args.reason))
        elif args.command == "promote":
            output(manager.promote(args.revision_id, reason=args.reason,
                                   evaluation_id=args.evaluation,
                                   allow_without_evaluation=args.manual_override))
        elif args.command == "retire":
            output(manager.retire(args.revision_id, reason=args.reason))
        elif args.command == "evaluation-new":
            samples = json.loads(args.samples.read_text(encoding="utf-8-sig"))
            output(manager.create_evaluation(
                character_id=args.character, candidate_revision_id=args.candidate,
                baseline_revision_id=args.baseline, sample_set=samples,
                engine={"id": args.engine, "adapter_version": args.adapter_version},
                reference_set_id=args.reference_set))
        elif args.command == "evaluation-list":
            output({"evaluations": manager.evaluations.list()})
        elif args.command == "evaluation-update":
            value = json.loads(args.json.read_text(encoding="utf-8-sig"))
            output(manager.evaluations.update(
                args.evaluation_id, status=value.get("status"),
                effective_parameters=value.get("effective_parameters"),
                unsupported_parameters=value.get("unsupported_parameters"),
                result=value.get("result")))
        elif args.command == "evaluation-review":
            ratings = json.loads(args.ratings.read_text(encoding="utf-8-sig"))
            output(manager.evaluations.review(args.evaluation_id, blind_pair_id=args.pair,
                                              preference=args.preference, ratings=ratings,
                                              notes=args.notes))
        elif args.command == "evaluation-decision":
            output(manager.evaluations.decide(args.evaluation_id,
                                              decision=args.decision, reason=args.reason))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Model management error: {exc}\n")


if __name__ == "__main__":
    main()
