"""Run the hardware-free usefulness measuring-instrument controls."""
from __future__ import annotations

import argparse
import json

from .analysis import evaluate_answerer, highest_aggregate_selections
from .answerers import BlindAnswerer, ComponentDegradedAnswerer, OracleAnswerer
from .binding import bind_cached_labels_for_controls, make_instrument_benchmark
from .catalog import build_question_catalog, catalogue_counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parts", type=int, default=522)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--component", default="visibility")
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--manifest", help="use cached frozen labels from a pixel-corpus manifest")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.parts <= 0:
        parser.error("--parts must be positive")
    catalog = build_question_catalog(range(args.parts), seed=args.seed)
    items = (
        bind_cached_labels_for_controls(catalog, args.manifest)
        if args.manifest
        else make_instrument_benchmark(catalog, candidates=args.candidates)
    )
    selections = highest_aggregate_selections(items)
    answerers = (
        OracleAnswerer.from_items(items),
        BlindAnswerer(seed=args.seed),
        ComponentDegradedAnswerer.from_items(items, component=args.component, seed=args.seed),
    )
    reports = {
        answerer.name: evaluate_answerer(items, answerer, selections=selections).as_dict()
        for answerer in answerers
    }
    derivations: dict[str, str] = {}
    for question in catalog:
        derivations.setdefault(question.public.kind, question.derivation)
    payload = {
        "parts": args.parts,
        "questions": len(catalog),
        "catalogue_counts": catalogue_counts(catalog),
        "ground_truth_derivations": derivations,
        "instrument_component": args.component,
        "label_source": args.manifest or "independent instrument lattice",
        "reports": reports,
        "scope_warning": "Scripted controls validate the measuring instrument; they do not measure any real vision model.",
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"catalogue: {len(catalog)} questions over {args.parts} parts")
        for kind, count in payload["catalogue_counts"].items():
            print(f"  {kind}: {count}")
        for name, report in reports.items():
            target = report["by_signal"][args.component]
            print(
                f"{name}: accuracy={report['accuracy']:.6f} chance={report['chance_accuracy']:.6f} "
                f"{args.component}_r={target['pearson_correlation']:.6f} "
                f"{args.component}_mi_bits={target['mutual_information_bits']:.6f}"
            )
        print(payload["scope_warning"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
