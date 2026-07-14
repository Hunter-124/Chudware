"""Leaderboard aggregator — gym reports + diag → aicad.leaderboard/1 JSON.

Contract: contracts/leaderboard.md (schema aicad.leaderboard/1).
Producer for GUI consumers (Settings AI leaderboard, AI workspace pane).

CLI::

    python -m tools.flywheel.bench.gym.leaderboard build [--out PATH]
                                                         [--reports DIR]
                                                         [--diag PATH]
                                                         [--mock]

Walks gym cycle JSON reports + per-cell graded/classifications under
reports/runs/, folds in diag-v1 axis scores, and emits
benchmarks/leaderboard/leaderboard.json. Idempotent: re-running rebuilds
from sources (append-cycles semantics via counting distinct cycles per model).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "aicad.leaderboard/1"
TASK_SUITE = "gym-C1..C8 + diag-v1"

_HERE = Path(__file__).resolve().parent
REPORTS_DIR = _HERE / "reports"
DEFAULT_OUT = _HERE.parents[3] / "benchmarks" / "leaderboard" / "leaderboard.json"
DIAG_LATEST = REPORTS_DIR / "diag" / "diag-latest.json"


# ---------------------------------------------------------------------------
# Model identity from tier strings
# ---------------------------------------------------------------------------

def model_id_for_tier(tier: str) -> dict[str, str]:
    """Map a gym tier string to leaderboard model identity fields."""
    from services.agent.tiers import LOCAL_TIER_MODELS, parse_openrouter_tier, load_local_models

    or_model = parse_openrouter_tier(tier)
    if or_model is not None:
        label = or_model.split("/")[-1].replace("-", " ").title()
        return {
            "id": or_model,
            "label": label,
            "provider": "openrouter",
            "kind": "paid",
        }
    if tier.startswith("local"):
        try:
            fanout = load_local_models().get("fanout") or "qwen3-4b"
        except Exception:  # noqa: BLE001
            fanout = "qwen3-4b"
        mid = LOCAL_TIER_MODELS.get(tier) or fanout
        return {
            "id": mid,
            "label": mid,
            "provider": "llamacpp",
            "kind": "local",
        }
    if tier in ("frontier", "frontier-200k", "mid"):
        return {
            "id": tier,
            "label": tier,
            "provider": "openrouter" if "frontier" in tier else "openai",
            "kind": "paid",
        }
    return {
        "id": tier,
        "label": tier,
        "provider": "unknown",
        "kind": "local" if tier.startswith("local") else "paid",
    }


# ---------------------------------------------------------------------------
# Source loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def discover_cycle_reports(reports_dir: Path) -> list[dict[str, Any]]:
    """Load cycle-*.json report files (not nested under runs/)."""
    out: list[dict[str, Any]] = []
    if not reports_dir.is_dir():
        return out
    for p in sorted(reports_dir.glob("*.json")):
        if p.name in ("tuning_results.json",):
            continue
        if p.name.startswith("diag"):
            continue
        data = _load_json(p)
        if not isinstance(data, dict):
            continue
        if "pass_rates" not in data and "cells" not in data:
            continue
        data["_source"] = str(p)
        out.append(data)
    return out


def discover_cells(reports_dir: Path) -> list[dict[str, Any]]:
    """Walk reports/runs/**/graded.json + classifications.json into cell records."""
    runs = reports_dir / "runs"
    cells: list[dict[str, Any]] = []
    if not runs.is_dir():
        return cells
    for graded_path in sorted(runs.rglob("graded.json")):
        run_dir = graded_path.parent
        graded = _load_json(graded_path)
        if not isinstance(graded, dict):
            continue
        classifications = _load_json(run_dir / "classifications.json") or []
        # Infer challenge/tier/seed from dirname: C1_local-24k_s1 or C1_openrouter:x_s1
        name = run_dir.name
        m = re.match(r"^(?P<ch>[A-Za-z0-9-]+)_(?P<tier>.+)_s(?P<seed>\d+)$", name)
        if m:
            challenge, tier, seed = m.group("ch"), m.group("tier"), int(m.group("seed"))
        else:
            challenge, tier, seed = graded.get("challenge", "?"), "unknown", 0
        # Cycle name = parent folder under runs/
        try:
            cycle = run_dir.relative_to(runs).parts[0]
        except (ValueError, IndexError):
            cycle = "unknown"
        cells.append({
            "challenge": challenge,
            "tier": tier,
            "seed": seed,
            "cycle": cycle,
            "passed": bool(graded.get("passed")),
            "graded": graded,
            "classifications": classifications if isinstance(classifications, list) else [],
            "run_dir": str(run_dir),
            "ledger_path": str(run_dir / "ledger.json"),
        })
    return cells


def load_diag(diag_path: Path | None) -> dict[str, Any] | None:
    if diag_path is None:
        diag_path = DIAG_LATEST
    if not diag_path.is_file():
        return None
    data = _load_json(diag_path)
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Cost / latency extraction (honest nulls when absent)
# ---------------------------------------------------------------------------

def _median_or_none(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(statistics.median(xs))


def extract_cost_metrics(cells: list[dict[str, Any]]) -> dict[str, float | None]:
    """Pull cost/latency/token medians from cell ledgers' effort_profile / usage.

    Honest: returns null for any series with no observations (local models
    have null usd; missing ledgers → all null).
    """
    usd: list[float] = []
    latency: list[float] = []
    tokens: list[float] = []
    for c in cells:
        lp = Path(c.get("ledger_path") or "")
        if not lp.is_file():
            continue
        doc = _load_json(lp)
        if not isinstance(doc, dict):
            continue
        for ev in doc.get("events") or []:
            if not isinstance(ev, dict):
                continue
            payload = ev.get("payload") or {}
            kind = ev.get("kind") or ""
            # Usage / cost events (when a provider emits them).
            if kind in ("usage", "cost", "model_usage", "ai_usage"):
                if payload.get("usd") is not None:
                    try:
                        usd.append(float(payload["usd"]))
                    except (TypeError, ValueError):
                        pass
                if payload.get("cost_usd") is not None:
                    try:
                        usd.append(float(payload["cost_usd"]))
                    except (TypeError, ValueError):
                        pass
                if payload.get("latency_s") is not None:
                    try:
                        latency.append(float(payload["latency_s"]))
                    except (TypeError, ValueError):
                        pass
                if payload.get("tokens") is not None:
                    try:
                        tokens.append(float(payload["tokens"]))
                    except (TypeError, ValueError):
                        pass
            # effort_profile carries budgets, not cost — skip for usd.
            if kind == "agent_turn" and isinstance(payload, dict):
                if payload.get("latency_s") is not None:
                    try:
                        latency.append(float(payload["latency_s"]))
                    except (TypeError, ValueError):
                        pass
                if payload.get("tokens") is not None:
                    try:
                        tokens.append(float(payload["tokens"]))
                    except (TypeError, ValueError):
                        pass
    return {
        "usd_per_task_med": _median_or_none(usd),
        "latency_s_med": _median_or_none(latency),
        "tokens_per_task_med": _median_or_none(tokens),
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _axis_scores_for_model(diag: dict[str, Any] | None, model_id: str, tier: str) -> dict[str, float | None]:
    """Pull diagnostic axis scores for a model; missing axes → null."""
    axes = {
        "tool_validity": None,
        "vision_fix": None,
        "feature_id": None,
        "vc_hygiene": None,
    }
    if not diag:
        return axes
    for row in diag.get("results") or []:
        if not isinstance(row, dict):
            continue
        m = row.get("model") or {}
        if m.get("id") == model_id or row.get("tier") == tier:
            for axis, payload in (row.get("axes") or {}).items():
                if axis not in axes:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("skipped"):
                    axes[axis] = None  # untested
                else:
                    sc = payload.get("score")
                    axes[axis] = float(sc) if sc is not None else None
            break
    return axes


def aggregate(
    *,
    reports_dir: Path | None = None,
    diag_path: Path | None = None,
    force_mock: bool = False,
    cells: list[dict[str, Any]] | None = None,
    cycle_reports: list[dict[str, Any]] | None = None,
    diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an aicad.leaderboard/1 document from gym + diag sources."""
    reports_dir = Path(reports_dir) if reports_dir else REPORTS_DIR
    if cells is None:
        cells = discover_cells(reports_dir)
    if cycle_reports is None:
        cycle_reports = discover_cycle_reports(reports_dir)
    if diag is None:
        diag = load_diag(diag_path)

    # Group cells by model id (derived from tier).
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tier_for_model: dict[str, str] = {}
    for c in cells:
        meta = model_id_for_tier(c["tier"])
        mid = meta["id"]
        by_model[mid].append(c)
        tier_for_model.setdefault(mid, c["tier"])

    # Also include models that only appear in diag (no gym cells yet).
    if diag:
        for row in diag.get("results") or []:
            m = (row.get("model") or {})
            mid = m.get("id")
            if mid and mid not in by_model:
                by_model[mid] = []
                tier_for_model[mid] = row.get("tier") or m.get("tier") or mid

    models_out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for mid, mcells in sorted(by_model.items()):
        tier = tier_for_model.get(mid, mid)
        meta = model_id_for_tier(tier)
        # Prefer diag model meta when present.
        if diag:
            for row in diag.get("results") or []:
                if (row.get("model") or {}).get("id") == mid:
                    meta = {**meta, **{k: v for k, v in (row.get("model") or {}).items()
                                       if k in ("id", "label", "provider", "kind")}}
                    break

        n_total = len(mcells)
        n_passed = sum(1 for c in mcells if c.get("passed"))
        pass_rate = round(n_passed / n_total, 4) if n_total else 0.0

        # Failure taxonomy counts.
        tax: dict[str, int] = defaultdict(int)
        for c in mcells:
            for cl in c.get("classifications") or []:
                if isinstance(cl, dict):
                    fc = cl.get("failure_class")
                    if fc:
                        tax[fc] += 1

        # Per-challenge pass rates.
        per_ch: dict[tuple[str, str], list[bool]] = defaultdict(list)
        cycles_seen: set[str] = set()
        for c in mcells:
            per_ch[(c["challenge"], c["tier"])].append(bool(c.get("passed")))
            if c.get("cycle"):
                cycles_seen.add(str(c["cycle"]))
        # Also count cycle reports that mention this tier.
        for rep in cycle_reports:
            if tier in (rep.get("tiers") or []):
                cycles_seen.add(str(rep.get("cycle") or rep.get("_source") or "cycle"))

        per_challenge = [
            {
                "challenge": ch,
                "tier": t,
                "pass_rate": round(sum(vs) / len(vs), 4) if vs else 0.0,
                "seeds": len(vs),
            }
            for (ch, t), vs in sorted(per_ch.items())
        ]

        cost = extract_cost_metrics(mcells)
        # Local models: usd always null (never invent a price).
        if meta.get("kind") == "local":
            cost["usd_per_task_med"] = None

        axes = _axis_scores_for_model(diag, mid, tier)

        models_out.append({
            "id": meta["id"],
            "label": meta.get("label") or meta["id"],
            "provider": meta.get("provider") or "unknown",
            "kind": meta.get("kind") or "local",
            "evaluated": now,
            "cycles": max(1, len(cycles_seen)) if (mcells or axes) else 0,
            "primary": {
                "pass_rate": pass_rate,
                "cells_total": n_total,
                "cells_passed": n_passed,
            },
            "axes": axes,
            "failure_taxonomy": dict(tax),
            "cost": cost,
            "per_challenge": per_challenge,
        })

    # Mock detection: force flag, diag.mock, or all cycle reports in mock mode.
    is_mock = bool(force_mock)
    if diag and diag.get("mock"):
        is_mock = True
    if cycle_reports and all(r.get("execution_mode") == "mock" for r in cycle_reports):
        is_mock = True
    # Existing cycle-1 cells that are mock-mode (no real provider usage events)
    # — if we only have local scripted cells and a mock diag, badge it.
    if not is_mock and diag is None and cycle_reports:
        # Keep real if any report says real.
        if not any(r.get("execution_mode") == "real" for r in cycle_reports):
            is_mock = True

    suite = TASK_SUITE + ("-MOCK" if is_mock else "")

    return {
        "schema": SCHEMA,
        "generated": now,
        "task_suite": suite,
        "models": models_out,
    }


def validate_leaderboard(doc: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty = valid)."""
    errs: list[str] = []
    if doc.get("schema") != SCHEMA:
        errs.append(f"schema must be {SCHEMA!r}, got {doc.get('schema')!r}")
    if not isinstance(doc.get("generated"), str):
        errs.append("generated must be an ISO-8601 string")
    if not isinstance(doc.get("task_suite"), str):
        errs.append("task_suite must be a string")
    models = doc.get("models")
    if not isinstance(models, list):
        errs.append("models must be a list")
        return errs
    required_model = ("id", "label", "provider", "kind", "evaluated", "cycles",
                      "primary", "axes", "failure_taxonomy", "cost", "per_challenge")
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            errs.append(f"models[{i}] not a dict")
            continue
        for k in required_model:
            if k not in m:
                errs.append(f"models[{i}] missing {k}")
        primary = m.get("primary") or {}
        for k in ("pass_rate", "cells_total", "cells_passed"):
            if k not in primary:
                errs.append(f"models[{i}].primary missing {k}")
        axes = m.get("axes") or {}
        for k in ("tool_validity", "vision_fix", "feature_id", "vc_hygiene"):
            if k not in axes:
                errs.append(f"models[{i}].axes missing {k}")
        cost = m.get("cost") or {}
        for k in ("usd_per_task_med", "latency_s_med", "tokens_per_task_med"):
            if k not in cost:
                errs.append(f"models[{i}].cost missing {k}")
        if m.get("kind") not in ("paid", "local"):
            errs.append(f"models[{i}].kind must be paid|local")
        if not isinstance(m.get("per_challenge"), list):
            errs.append(f"models[{i}].per_challenge must be a list")
    return errs


def build_and_write(
    *,
    out: Path | None = None,
    reports_dir: Path | None = None,
    diag_path: Path | None = None,
    force_mock: bool = False,
) -> Path:
    """Aggregate and write leaderboard.json. Returns the output path."""
    doc = aggregate(
        reports_dir=reports_dir,
        diag_path=diag_path,
        force_mock=force_mock,
    )
    errs = validate_leaderboard(doc)
    if errs:
        raise ValueError("leaderboard contract violations:\n  - " + "\n  - ".join(errs))
    out_path = Path(out) if out else DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    out = build_and_write(
        out=Path(args.out) if args.out else None,
        reports_dir=Path(args.reports) if args.reports else None,
        diag_path=Path(args.diag) if args.diag else None,
        force_mock=bool(args.mock),
    )
    doc = json.loads(out.read_text(encoding="utf-8"))
    print(f"leaderboard written: {out}")
    print(f"  schema:     {doc['schema']}")
    print(f"  task_suite: {doc['task_suite']}")
    print(f"  models:     {len(doc['models'])}")
    for m in doc["models"]:
        pr = m["primary"]
        print(f"    - {m['id']} ({m['kind']}/{m['provider']}): "
              f"pass={pr['pass_rate']:.3f} "
              f"({pr['cells_passed']}/{pr['cells_total']}) "
              f"axes={m['axes']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Leaderboard aggregator (aicad.leaderboard/1)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="rebuild leaderboard.json from gym + diag")
    b.add_argument("--out", default=None,
                   help=f"output path (default: {DEFAULT_OUT})")
    b.add_argument("--reports", default=None,
                   help="gym reports directory (default: tools/flywheel/bench/gym/reports)")
    b.add_argument("--diag", default=None,
                   help="diag-latest.json path (default: reports/diag/diag-latest.json)")
    b.add_argument("--mock", action="store_true",
                   help="force -MOCK task_suite suffix (smoke/mock runs)")
    b.set_defaults(func=cmd_build)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
