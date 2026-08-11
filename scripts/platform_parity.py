#!/usr/bin/env python3
"""Compare native Chudware geometry dumps across operating systems and architectures."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "chudware.platform-parity/1"
REL_TOL = 1e-9
EXACT_FIELDS = ("topology", "mesh_tri_count", "op_tree")
NUMERIC_FIELDS = ("mass_props", "bbox")
HASH_FIELDS = ("aicm_sha256", "stepplus_sha256")


class ParityError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ParityError(f"{path}: cannot read parity dump: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ParityError(f"{path}: expected schema {SCHEMA!r}")
    documents = value.get("documents")
    if not isinstance(documents, list):
        raise ParityError(f"{path}: documents must be an array")
    by_name: dict[str, Any] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, dict) or not isinstance(document.get("doc"), str):
            raise ParityError(f"{path}: documents[{index}] has no string doc name")
        name = document["doc"]
        if name in by_name:
            raise ParityError(f"{path}: duplicate document {name!r}")
        by_name[name] = document
    return by_name


def _relative_difference(left: float, right: float) -> float:
    if left == right:
        return 0.0
    scale = max(abs(left), abs(right))
    return math.inf if scale == 0.0 else abs(left - right) / scale


def _compare_numeric(
    baseline: Any,
    candidate: Any,
    *,
    document: str,
    field: str,
    errors: list[str],
) -> None:
    if isinstance(baseline, bool) or isinstance(candidate, bool):
        errors.append(f"{document}: {field} type differs ({baseline!r} vs {candidate!r})")
        return
    if isinstance(baseline, (int, float)) and isinstance(candidate, (int, float)):
        left, right = float(baseline), float(candidate)
        if not math.isfinite(left) or not math.isfinite(right):
            if left != right:
                errors.append(f"{document}: {field} differs ({left!r} vs {right!r})")
            return
        difference = _relative_difference(left, right)
        if difference > REL_TOL:
            errors.append(
                f"{document}: {field} differs ({left:.17g} vs {right:.17g}; "
                f"relative difference {difference:.3e} > {REL_TOL:.1e})"
            )
        return
    if isinstance(baseline, list) and isinstance(candidate, list):
        if len(baseline) != len(candidate):
            errors.append(f"{document}: {field} length differs ({len(baseline)} vs {len(candidate)})")
            return
        for index, (left, right) in enumerate(zip(baseline, candidate)):
            _compare_numeric(left, right, document=document, field=f"{field}[{index}]", errors=errors)
        return
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        if baseline.keys() != candidate.keys():
            errors.append(
                f"{document}: {field} keys differ "
                f"({sorted(baseline)} vs {sorted(candidate)})"
            )
            return
        for key in sorted(baseline):
            _compare_numeric(
                baseline[key], candidate[key], document=document,
                field=f"{field}.{key}", errors=errors,
            )
        return
    errors.append(f"{document}: {field} type differs ({type(baseline).__name__} vs {type(candidate).__name__})")


def compare(paths: Iterable[Path]) -> tuple[list[str], list[str]]:
    dump_paths = list(paths)
    if len(dump_paths) < 2:
        raise ParityError("at least two parity dumps are required")
    loaded = [(path, _load(path)) for path in dump_paths]
    baseline_path, baseline = loaded[0]
    errors: list[str] = []
    reports: list[str] = []

    for candidate_path, candidate in loaded[1:]:
        baseline_docs, candidate_docs = set(baseline), set(candidate)
        if baseline_docs != candidate_docs:
            missing = sorted(baseline_docs - candidate_docs)
            extra = sorted(candidate_docs - baseline_docs)
            errors.append(
                f"{candidate_path}: document set differs from {baseline_path} "
                f"(missing={missing}, extra={extra})"
            )
            continue
        for name in sorted(baseline):
            left, right = baseline[name], candidate[name]
            for field in EXACT_FIELDS:
                if field not in left or field not in right:
                    errors.append(f"{name}: {field} missing in {baseline_path} or {candidate_path}")
                elif left[field] != right[field]:
                    errors.append(
                        f"{name}: {field} differs between {baseline_path.name} and "
                        f"{candidate_path.name} ({left[field]!r} vs {right[field]!r})"
                    )
            for field in NUMERIC_FIELDS:
                if field not in left or field not in right:
                    errors.append(f"{name}: {field} missing in {baseline_path} or {candidate_path}")
                else:
                    _compare_numeric(left[field], right[field], document=name, field=field, errors=errors)
            for field in HASH_FIELDS:
                if field not in left or field not in right:
                    reports.append(f"WARNING: {name}: {field} missing in one dump")
                elif left[field] == right[field]:
                    reports.append(
                        f"OK: {name}: {field} matches across {baseline_path.name} and "
                        f"{candidate_path.name}"
                    )
                else:
                    reports.append(
                        f"WARNING: {name}: {field} differs across {baseline_path.name} and "
                        f"{candidate_path.name}; serialization hashes are informational"
                    )
    return errors, reports


def _paths(arguments: list[str]) -> list[Path]:
    paths: list[Path] = []
    for argument in arguments:
        path = Path(argument)
        if path.is_dir():
            paths.extend(sorted(path.glob("*.json")))
        else:
            paths.append(path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dumps", nargs="+", help="dump JSON files or directories containing dumps")
    args = parser.parse_args(argv)
    try:
        errors, reports = compare(_paths(args.dumps))
    except ParityError as exc:
        print(f"platform parity: ERROR: {exc}", file=sys.stderr)
        return 2
    for report in reports:
        print(report)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"platform parity: FAILED with {len(errors)} divergence(s)", file=sys.stderr)
        return 1
    print(f"platform parity: PASS ({len(_paths(args.dumps))} dumps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
