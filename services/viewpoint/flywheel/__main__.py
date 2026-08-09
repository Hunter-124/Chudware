"""Explicit CLI; importing the flywheel never runs or spends anything."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .demo import DemoRenderBackend, demo_job
from .loop import run_flywheel
from .scorers import RealModelConfig, RealModelScorer, ScriptedScorer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the viewpoint flywheel demo end to end")
    parser.add_argument("--output", type=Path, default=Path("scratch/viewpoint-flywheel-demo"))
    parser.add_argument("--raters", type=int, default=2)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--sheets", type=int, default=1,
                        help="number of demo sheets (useful for realistic dry-run costing)")
    parser.add_argument("--first-position-biased", action="store_true",
                        help="run the adversarial slot-A scripted control")
    parser.add_argument("--scripted-answer", default="yes",
                        help="offline scorer answer; use 'no' for the correctness control")
    parser.add_argument("--dry-run", action="store_true",
                        help="render and persist manifests, but make no scorer request")
    parser.add_argument("--live-model-config", type=Path, action="append",
                        help="repeatable JSON config for repository provider-backed scorers")
    parser.add_argument("--allow-live-model", action="store_true",
                        help="second explicit opt-in required to spend on a live scorer")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.live_model_config:
        scorers = tuple(RealModelScorer(
            RealModelConfig.from_json_file(path), allow_live=args.allow_live_model)
                        for path in args.live_model_config)
        if not args.dry_run and not args.allow_live_model:
            raise SystemExit("live execution requires --allow-live-model (dry-run does not)")
        jobs = tuple(
            demo_job(seed=seed, request_id=f"flywheel-demo-{seed}")
            for seed in range(args.seed, args.seed + args.sheets)
        )
        repeats = args.raters
    elif args.first_position_biased:
        scorers = (ScriptedScorer(
            ranking={"view-a": 4, "view-b": 2, "view-c": 1},
            answer_text=args.scripted_answer,
            first_position_biased=True,
            model_id="scripted-first-position-control",
        ),)
        jobs = tuple(demo_job(seed=seed, request_id=f"flywheel-bias-{seed}")
                     for seed in range(args.seed, args.seed + 8))
        repeats = 1
    else:
        scorers = tuple(ScriptedScorer(
            ranking={"view-a": 4, "view-b": 2, "view-c": 1},
            answer_text=args.scripted_answer,
            model_id=f"scripted-rater-{index + 1}",
        ) for index in range(args.raters))
        jobs = tuple(
            demo_job(seed=seed, request_id=f"flywheel-demo-{seed}")
            for seed in range(args.seed, args.seed + args.sheets)
        )
        repeats = 1
    result = run_flywheel(
        jobs,
        scorer=scorers,
        render_backend=DemoRenderBackend(),
        output_dir=args.output,
        raters_per_sheet=repeats,
        dry_run=args.dry_run,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
