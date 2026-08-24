#!/usr/bin/env python3
"""Validate the simulation optimizer registry without third-party dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

SIMULATION_FAMILIES = frozenset(
    {
        "fea_static_adaptive",
        "modal",
        "thermal",
        "fatigue",
        "buckling",
        "contact",
        "frequency_response",
        "lbm_cfd",
        "openfoam_cfd",
        "motion",
    }
)
PROMOTION_STATES = frozenset({"gated", "candidate", "validated", "promoted"})
SAFE_SPLIT_GROUPS = frozenset(
    {"geometry_family", "cad_template", "physical_asset", "case_family"}
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class RegistryValidationError(ValueError):
    """Raised when the registry violates one or more governance invariants."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_keys(value: Any, keys: set[str], where: str, errors: list[str]) -> bool:
    if not isinstance(value, dict):
        errors.append(f"{where} must be an object")
        return False
    missing = sorted(keys - value.keys())
    if missing:
        errors.append(f"{where} missing required provenance fields: {', '.join(missing)}")
        return False
    return True


def _validate_track(track: Any, index: int, errors: list[str]) -> None:
    where = f"tracks[{index}]"
    required = {
        "track_id",
        "simulation_family",
        "optimizer_role",
        "solver_authority",
        "deterministic_baseline",
        "corpus",
        "split_policy",
        "optimization_target",
        "safety_vetoes",
        "model",
        "decision_metric",
        "promotion",
    }
    if not _require_keys(track, required, where, errors):
        return

    if not _nonempty_string(track["track_id"]):
        errors.append(f"{where}.track_id must be a non-empty string")
    family = track["simulation_family"]
    if not isinstance(family, str) or family not in SIMULATION_FAMILIES:
        errors.append(f"{where}.simulation_family is unknown: {family!r}")
    if track["optimizer_role"] != "advisory_only":
        errors.append(f"{where}.optimizer_role must be 'advisory_only'")
    if track["solver_authority"] != "deterministic_solver_and_safety_gates":
        errors.append(
            f"{where}.solver_authority must remain 'deterministic_solver_and_safety_gates'"
        )

    baseline = track["deterministic_baseline"]
    if _require_keys(
        baseline,
        {"name", "implementation", "implementation_sha256", "configuration"},
        f"{where}.deterministic_baseline",
        errors,
    ):
        if not _nonempty_string(baseline["name"]) or not _nonempty_string(
            baseline["implementation"]
        ):
            errors.append(f"{where}.deterministic_baseline has empty provenance")
        if not _is_hash(baseline["implementation_sha256"]):
            errors.append(
                f"{where}.deterministic_baseline.implementation_sha256 must be sha256"
            )
        if not isinstance(baseline["configuration"], dict) or not baseline["configuration"]:
            errors.append(f"{where}.deterministic_baseline.configuration must be non-empty")

    corpus = track["corpus"]
    corpus_status = None
    if _require_keys(
        corpus,
        {
            "status",
            "manifest_path",
            "manifest_sha256",
            "dataset_sha256",
            "provenance_note",
        },
        f"{where}.corpus",
        errors,
    ):
        corpus_status = corpus["status"]
        if corpus_status not in {"available", "absent"}:
            errors.append(f"{where}.corpus.status must be 'available' or 'absent'")
        if not _nonempty_string(corpus["provenance_note"]):
            errors.append(f"{where}.corpus.provenance_note must explain provenance")
        if corpus_status == "available":
            if not _nonempty_string(corpus["manifest_path"]):
                errors.append(f"{where}.corpus.manifest_path is required when available")
            if not _is_hash(corpus["manifest_sha256"]):
                errors.append(f"{where}.corpus.manifest_sha256 must be sha256 when available")
            if not _is_hash(corpus["dataset_sha256"]):
                errors.append(f"{where}.corpus.dataset_sha256 must be sha256 when available")
        elif any(
            corpus[key] is not None
            for key in ("manifest_path", "manifest_sha256", "dataset_sha256")
        ):
            errors.append(
                f"{where}.corpus absent provenance must use null paths/hashes, not placeholders"
            )

    split = track["split_policy"]
    if _require_keys(
        split,
        {"kind", "group_by", "held_out_unit", "seed", "leakage_controls"},
        f"{where}.split_policy",
        errors,
    ):
        if split["kind"] != "group_holdout":
            errors.append(f"{where}.split_policy.kind is leakage-prone; use group_holdout")
        if split["group_by"] not in SAFE_SPLIT_GROUPS:
            errors.append(
                f"{where}.split_policy.group_by is leakage-prone: {split['group_by']!r}"
            )
        if split["held_out_unit"] != split["group_by"]:
            errors.append(f"{where}.split_policy must hold out the complete grouping unit")
        controls = split["leakage_controls"]
        if not isinstance(controls, list) or not controls or not all(
            _nonempty_string(item) for item in controls
        ):
            errors.append(f"{where}.split_policy.leakage_controls must be non-empty")

    target = track["optimization_target"]
    if _require_keys(
        target,
        {"primary", "direction", "unit", "constraints"},
        f"{where}.optimization_target",
        errors,
    ):
        if target["direction"] not in {"minimize", "maximize"}:
            errors.append(f"{where}.optimization_target.direction is invalid")
        if not isinstance(target["constraints"], list) or not target["constraints"]:
            errors.append(f"{where}.optimization_target.constraints must be non-empty")

    vetoes = track["safety_vetoes"]
    if not isinstance(vetoes, list) or not vetoes or not all(
        _nonempty_string(veto) for veto in vetoes
    ):
        errors.append(f"{where}.safety_vetoes must contain at least one explicit veto")

    model = track["model"]
    model_status = None
    if _require_keys(
        model,
        {
            "status",
            "artifact_path",
            "artifact_sha256",
            "model_card_path",
            "model_card_sha256",
            "model_revision",
            "limitations",
        },
        f"{where}.model",
        errors,
    ):
        model_status = model["status"]
        if model_status not in {"absent", "trained"}:
            errors.append(f"{where}.model.status must be 'absent' or 'trained'")
        if not isinstance(model["limitations"], list) or not model["limitations"]:
            errors.append(f"{where}.model.limitations must be non-empty")
        artifact_fields = (
            "artifact_path",
            "artifact_sha256",
            "model_card_path",
            "model_card_sha256",
            "model_revision",
        )
        if model_status == "trained":
            if not all(_nonempty_string(model[key]) for key in artifact_fields):
                errors.append(f"{where}.model trained candidate lacks artifact/model-card provenance")
            if not _is_hash(model["artifact_sha256"]):
                errors.append(f"{where}.model.artifact_sha256 must be sha256")
            if not _is_hash(model["model_card_sha256"]):
                errors.append(f"{where}.model.model_card_sha256 must be sha256")
            if corpus_status != "available":
                errors.append(f"{where}.model cannot be trained without an available corpus")
        elif any(model[key] is not None for key in artifact_fields):
            errors.append(f"{where}.model absent state must use null artifact fields")

    metric = track["decision_metric"]
    if _require_keys(
        metric,
        {"name", "direction", "threshold", "unit", "evaluation_scope"},
        f"{where}.decision_metric",
        errors,
    ):
        if metric["direction"] not in {"at_most", "at_least"}:
            errors.append(f"{where}.decision_metric.direction is invalid")
        if not isinstance(metric["threshold"], (int, float)) or isinstance(
            metric["threshold"], bool
        ):
            errors.append(f"{where}.decision_metric.threshold must be numeric")

    promotion = track["promotion"]
    if _require_keys(
        promotion,
        {"state", "evidence_hashes", "blockers", "reviewed_on"},
        f"{where}.promotion",
        errors,
    ):
        state = promotion["state"]
        evidence = promotion["evidence_hashes"]
        if state not in PROMOTION_STATES:
            errors.append(f"{where}.promotion.state is invalid: {state!r}")
        if not isinstance(evidence, list) or not all(_is_hash(item) for item in evidence):
            errors.append(f"{where}.promotion.evidence_hashes must contain only sha256 values")
        if state == "gated":
            if not isinstance(promotion["blockers"], list) or not promotion["blockers"]:
                errors.append(f"{where}.promotion gated state requires blockers")
        elif state in {"candidate", "validated", "promoted"}:
            if model_status != "trained":
                errors.append(f"{where}.promotion {state} requires a trained model")
            if not evidence:
                errors.append(f"{where}.promotion {state} requires evidence hashes")
            if state in {"validated", "promoted"} and corpus_status != "available":
                errors.append(f"{where}.promotion {state} requires corpus provenance")


def validate_registry(data: Any) -> None:
    """Validate registry data or raise RegistryValidationError with all violations."""
    errors: list[str] = []
    if not isinstance(data, dict):
        raise RegistryValidationError(["registry root must be an object"])
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        errors.append("tracks must be an array")
    else:
        for index, track in enumerate(tracks):
            _validate_track(track, index, errors)
        ids = [
            track.get("track_id")
            for track in tracks
            if isinstance(track, dict) and isinstance(track.get("track_id"), str)
        ]
        if len(ids) != len(set(ids)):
            errors.append("track_id values must be unique")
        families = [
            track.get("simulation_family")
            for track in tracks
            if isinstance(track, dict) and isinstance(track.get("simulation_family"), str)
        ]
        if len(families) != len(set(families)):
            errors.append("simulation_family values must be unique")
        family_set = set(families)
        missing = sorted(SIMULATION_FAMILIES - family_set)
        extra = sorted(family_set - SIMULATION_FAMILIES)
        if missing:
            errors.append(f"registry missing simulation families: {', '.join(missing)}")
        if extra:
            errors.append(f"registry contains unknown simulation families: {', '.join(extra)}")
    if errors:
        raise RegistryValidationError(errors)

def _source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_source(root: Path, implementation: Any, where: str) -> Path:
    if not _nonempty_string(implementation):
        raise RegistryValidationError([f"{where}.implementation must be a non-empty path"])
    root = root.resolve()
    source = (root / implementation).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise RegistryValidationError(
            [f"{where}.implementation escapes the source root: {implementation!r}"]
        ) from exc
    if not source.is_file():
        raise RegistryValidationError(
            [f"{where}.implementation source does not exist: {implementation!r}"]
        )
    return source


def verify_baseline_source_hashes(data: dict[str, Any], source_root: str | Path) -> None:
    """Reject registry baselines whose hashes do not match the current source tree."""
    root = Path(source_root)
    errors: list[str] = []
    for index, track in enumerate(data["tracks"]):
        baseline = track["deterministic_baseline"]
        where = f"tracks[{index}].deterministic_baseline"
        try:
            source = _baseline_source(root, baseline["implementation"], where)
        except RegistryValidationError as exc:
            errors.extend(exc.errors)
            continue
        actual = _source_digest(source)
        expected = baseline["implementation_sha256"]
        if actual != expected:
            errors.append(
                f"{where}.implementation_sha256 is stale for "
                f"{baseline['implementation']}: expected {actual}, registry has {expected}"
            )
    if errors:
        raise RegistryValidationError(errors)


def refresh_baseline_source_hashes(
    data: dict[str, Any], source_root: str | Path
) -> int:
    """Refresh source digests after an integrated source change; return changed count."""
    root = Path(source_root)
    changed = 0
    for index, track in enumerate(data["tracks"]):
        baseline = track["deterministic_baseline"]
        where = f"tracks[{index}].deterministic_baseline"
        source = _baseline_source(root, baseline["implementation"], where)
        actual = _source_digest(source)
        if baseline["implementation_sha256"] != actual:
            baseline["implementation_sha256"] = actual
            changed += 1
    return changed


def load_registry(path: str | Path | None = None) -> dict[str, Any]:
    registry_path = Path(path) if path is not None else Path(__file__).with_name("registry.json")
    with registry_path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    validate_registry(data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="registry JSON (defaults beside this module)")
    parser.add_argument(
        "--refresh-baseline-hashes",
        action="store_true",
        help="rewrite baseline source hashes from the integrated source tree",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="source root for baseline implementations (defaults to repository root)",
    )
    args = parser.parse_args(argv)
    registry_path = Path(args.path) if args.path else Path(__file__).with_name("registry.json")
    try:
        data = load_registry(registry_path)
        changed = 0
        if args.refresh_baseline_hashes:
            changed = refresh_baseline_source_hashes(data, args.source_root)
            validate_registry(data)
            registry_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        verify_baseline_source_hashes(data, args.source_root)
    except (OSError, json.JSONDecodeError, RegistryValidationError) as exc:
        print(f"simulation optimizer registry invalid: {exc}", file=sys.stderr)
        return 2
    suffix = (
        f"; refreshed {changed} baseline hashes" if args.refresh_baseline_hashes else ""
    )
    print(
        f"simulation optimizer registry valid: {len(data['tracks'])} tracks; "
        f"baseline source hashes match{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
