"""Leaderboard aggregator — gym reports + diag → aicad.leaderboard/1 JSON.

Contract: contracts/leaderboard.md (schema aicad.leaderboard/1).
Producer for GUI consumers (Settings AI leaderboard, AI workspace pane).

CLI::

    python -m tools.flywheel.bench.gym.leaderboard build [--out PATH]
                                                         [--reports DIR]
                                                         [--diag PATH]
                                                         [--mock]

Loads exact per-cell metadata for an explicit cycle set, folds in eligible
mode-matched diag-v1 axis scores, and emits benchmarks/leaderboard/leaderboard.json.
Idempotent: re-running rebuilds from authoritative cell records.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "aicad.leaderboard/1"
TASK_SUITE = "gym-C1..C15 + diag-v1"

_HERE = Path(__file__).resolve().parent
REPORTS_DIR = _HERE / "reports"
DEFAULT_OUT = _HERE.parents[3] / "benchmarks" / "leaderboard" / "leaderboard.json"
DIAG_LATEST = REPORTS_DIR / "diag" / "diag-latest.json"


# ---------------------------------------------------------------------------
# Model identity from tier strings
# ---------------------------------------------------------------------------

def model_id_for_tier(tier: str) -> dict[str, str]:
    """Map a gym tier string to leaderboard model identity fields."""
    from services.agent.tiers import (
        LOCAL_TIER_MODELS, parse_openrouter_tier, parse_xai_tier, load_local_models,
    )

    or_model = parse_openrouter_tier(tier)
    if or_model is not None:
        label = or_model.split("/")[-1].replace("-", " ").title()
        return {
            "id": or_model,
            "label": label,
            "provider": "openrouter",
            "kind": "paid",
        }
    xai_model = parse_xai_tier(tier)
    if xai_model is not None:
        wire = "grok-4.5" if xai_model == "grok-4-5" else xai_model
        return {
            "id": wire,
            "label": wire.replace("-", " ").title(),
            "provider": "xai",
            "kind": "paid",
        }
    if isinstance(tier, str) and tier.startswith("ollama:"):
        mid = tier[len("ollama:"):].strip() or "ollama"
        return {
            "id": mid,
            "label": mid,
            "provider": "ollama",
            "kind": "local",
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


def discover_cells(
    reports_dir: Path,
    *,
    cycles: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load exact persisted ``CellResult`` records for the selected cycles.

    Directory names are portable, lossy storage details and are never parsed. Old
    ``graded.json`` files without ``cell.json`` metadata are intentionally ineligible:
    they do not carry trustworthy tier/model/mode identity.
    """
    runs = reports_dir / "runs"
    if not runs.is_dir():
        return []

    if cycles is None:
        metadata_paths = sorted(runs.glob("*/cell.json"))
    else:
        metadata_paths = [
            path
            for cycle in sorted(cycles)
            for path in sorted((runs / cycle).glob("*/cell.json"))
        ]

    cells: list[dict[str, Any]] = []
    required = {"challenge", "tier", "seed", "division", "cycle",
                "model", "mode", "run_id"}
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable cell metadata {metadata_path}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"cell metadata {metadata_path} must be a JSON object")
        missing = sorted(required - metadata.keys())
        if missing:
            raise ValueError(
                f"cell metadata {metadata_path} missing exact identity: {', '.join(missing)}")
        cycle = str(metadata["cycle"])
        if cycles is not None and cycle not in cycles:
            continue
        mode = str(metadata["mode"]).lower()
        if mode not in ("real", "mock"):
            raise ValueError(f"cell metadata {metadata_path} has invalid mode {mode!r}")

        run_dir = metadata_path.parent
        cell = dict(metadata)
        cell["mode"] = mode
        cell["passed"] = bool(cell.get("passed"))
        cell["graded"] = cell.get("graded") if isinstance(cell.get("graded"), dict) else {}
        cell["classifications"] = (
            cell.get("classifications")
            if isinstance(cell.get("classifications"), list)
            else []
        )
        cell["run_dir"] = str(run_dir)
        cell["ledger_path"] = str(run_dir / "ledger.json")
        cells.append(cell)
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
    """Pull diagnostic axis scores for a model; missing axes → null.

    Transport-error honesty: an axis with ``score is None`` and an ``errors``
    note (or ``skipped``) is untested, never a dishonest 0.
    """
    axes = {
        "tool_validity": None,
        "vision_fix": None,
        "feature_id": None,
        "vc_hygiene": None,
        "discipline_index": None,
        "verify_efficacy": None,
        "checkpoint_util": None,
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
                # skipped OR score null (incl. >50% transport errors) → untested
                if payload.get("skipped") or payload.get("score") is None:
                    axes[axis] = None
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
    cycles: set[str] | None = None,
) -> dict[str, Any]:
    """Build an aicad.leaderboard/1 document from explicit cycles and cell modes."""
    reports_dir = Path(reports_dir) if reports_dir else REPORTS_DIR
    if cycle_reports is None:
        cycle_reports = discover_cycle_reports(reports_dir)

    report_cycles = {
        str(report.get("cycle"))
        for report in cycle_reports
        if report.get("cycle") is not None
    }
    selected_cycles = set(cycles) if cycles is not None else report_cycles
    cycle_reports = [
        report for report in cycle_reports
        if str(report.get("cycle")) in selected_cycles
    ]

    if cells is None:
        cells = discover_cells(reports_dir, cycles=selected_cycles)
    else:
        cells = [
            cell for cell in cells
            if str(cell.get("cycle")) in selected_cycles
        ]
    if diag is None:
        diag = load_diag(diag_path)

    selected_mode = "mock" if force_mock else "real"
    cells = [
        cell for cell in cells
        if str(cell.get("mode") or "").lower() == selected_mode
    ]
    cycle_reports = [
        report for report in cycle_reports
        if str(report.get("execution_mode") or "").lower() == selected_mode
    ]

    # A mock diagnostic source is ineligible for a real leaderboard, just like a
    # mock cell. It may be included only by an explicit force_mock build.
    if diag and diag.get("mock") and not force_mock:
        diag = None

    diag_model_ids: set[str] = set()
    if diag:
        for row in diag.get("results") or []:
            mid = (row.get("model") or {}).get("id")
            if mid and not row.get("error"):
                diag_model_ids.add(mid)

    # Group by the exact persisted model id. Tier parsing supplies labels/provider
    # metadata only; it is never asked to recover identity from a run directory.
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    tier_for_model: dict[str, str] = {}
    for cell in cells:
        tier = str(cell["tier"])
        parsed = model_id_for_tier(tier)
        mid = str(cell.get("model") or parsed["id"])
        by_model[mid].append(cell)
        tier_for_model.setdefault(mid, tier)

    # Also include models that only appear in an eligible diag (no gym cells yet).
    if diag:
        for row in diag.get("results") or []:
            model = row.get("model") or {}
            mid = model.get("id")
            if mid and mid not in by_model:
                by_model[mid] = []
                tier_for_model[mid] = row.get("tier") or model.get("tier") or mid

    models_out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for mid, mcells in sorted(by_model.items()):
        tier = tier_for_model.get(mid, mid)
        meta = {**model_id_for_tier(tier), "id": mid}
        # Prefer diag display metadata when present, while preserving the exact id.
        if diag:
            for row in diag.get("results") or []:
                if (row.get("model") or {}).get("id") == mid:
                    meta = {**meta, **{k: v for k, v in (row.get("model") or {}).items()
                                       if k in ("label", "provider", "kind")}}
                    break

        if not force_mock and meta.get("kind") == "paid":
            if not mcells and mid not in diag_model_ids:
                continue
        n_total = len(mcells)
        n_passed = sum(1 for cell in mcells if cell.get("passed"))
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

        # --- additive division / discipline keys ---
        def _div_stats(div: str) -> dict:
            dcells = [c for c in mcells if (c.get("division") or "local") == div]
            nt = len(dcells)
            np_ = sum(1 for c in dcells if c.get("passed"))
            di_vals = [float(c["discipline_index"])
                       for c in dcells
                       if c.get("discipline_index") is not None]
            # also accept nested graded / report fields
            if not di_vals:
                for c in dcells:
                    for key in ("discipline_index",):
                        v = c.get(key)
                        if isinstance(v, (int, float)):
                            di_vals.append(float(v))
            di_mean = round(sum(di_vals) / len(di_vals), 4) if di_vals else None
            return {
                "pass_rate": round(np_ / nt, 4) if nt else 0.0,
                "cells_total": nt,
                "cells_passed": np_,
                "discipline_index": di_mean if di_mean is not None else 0.0,
            }

        divisions = {
            "local": _div_stats("local"),
            "open": _div_stats("open"),
        }
        # calibration subset: challenges marked calibration or shared dual-div cells
        cal_local = [c for c in mcells
                     if (c.get("division") or "local") == "local"
                     and c.get("calibration", True)]
        cal_open = [c for c in mcells
                    if (c.get("division") or "local") == "open"
                    and c.get("calibration", True)]
        def _pr(xs):
            return round(sum(1 for c in xs if c.get("passed")) / len(xs), 4) if xs else 0.0
        local_pr = _pr(cal_local) if cal_local else divisions["local"]["pass_rate"]
        open_pr = _pr(cal_open) if cal_open else divisions["open"]["pass_rate"]
        calibration = {
            "local_pass_rate": local_pr,
            "open_pass_rate": open_pr,
            "bridge_delta": round(local_pr - open_pr, 4),
        }

        # Fold report-level instrumentation if cells carry it; else null axes stay null.
        di_all = [float(c["discipline_index"]) for c in mcells
                  if isinstance(c.get("discipline_index"), (int, float))]
        if di_all:
            axes["discipline_index"] = round(sum(di_all) / len(di_all), 4)
        # verify_efficacy / checkpoint_util from cell fields when present
        ve_vals = [c["verify_efficacy"] for c in mcells
                   if isinstance(c.get("verify_efficacy"), (int, float))]
        if ve_vals:
            axes["verify_efficacy"] = round(sum(ve_vals) / len(ve_vals), 4)
        cu_vals = [c["checkpoint_util"] for c in mcells
                   if isinstance(c.get("checkpoint_util"), (int, float))]
        if cu_vals:
            axes["checkpoint_util"] = round(sum(cu_vals) / len(cu_vals), 4)

        # per_challenge gains optional division
        per_ch_div: dict[tuple, list[bool]] = defaultdict(list)
        for c in mcells:
            key = (c["challenge"], c.get("division") or "local", c["tier"])
            per_ch_div[key].append(bool(c.get("passed")))
        per_challenge = [
            {
                "challenge": ch,
                "division": div,
                "tier": t,
                "pass_rate": round(sum(vs) / len(vs), 4) if vs else 0.0,
                "seeds": len(vs),
            }
            for (ch, div, t), vs in sorted(per_ch_div.items())
        ]

        # instrumentation_summary from cycle reports when available
        phase_top: dict = {}
        top_fp = None
        for rep in cycle_reports:
            inst = rep.get("instrumentation") or {}
            summary = rep.get("instrumentation_summary") or inst.get("summary") or {}
            if summary.get("phase_mortality_top"):
                phase_top = summary["phase_mortality_top"]
            if summary.get("top_misuse_fingerprint"):
                top_fp = summary["top_misuse_fingerprint"]
            # also dig rates
            rates = (inst.get("phase_mortality") or {}).get("rates") or {}
            if rates and not phase_top:
                top = max(rates.items(), key=lambda kv: kv[1])
                phase_top = {top[0]: top[1]}
            fps = inst.get("misuse_fingerprints") or []
            if fps and not top_fp:
                top_fp = fps[0].get("fingerprint") if isinstance(fps[0], dict) else str(fps[0])
            di_rep = rep.get("discipline_index") or {}
            if axes.get("discipline_index") is None:
                # prefer mixed mean of local/open from report
                vals = [v for v in (di_rep.get("local"), di_rep.get("open")) if v is not None]
                if vals:
                    axes["discipline_index"] = round(sum(vals) / len(vals), 4)
            ve = (inst.get("verify_efficacy") or {}).get("rate")
            if ve is not None and axes.get("verify_efficacy") is None:
                axes["verify_efficacy"] = ve
            cu = (inst.get("checkpoint_util") or {}).get("overall")
            if cu is not None and axes.get("checkpoint_util") is None:
                axes["checkpoint_util"] = cu

        instrumentation_summary = {
            "phase_mortality_top": phase_top,
            "top_misuse_fingerprint": top_fp,
        }

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
            "divisions": divisions,
            "calibration": calibration,
            "instrumentation_summary": instrumentation_summary,
        })

    # The badge follows eligible scored data, not a global report toggle. A lone
    # REAL report with no REAL cell can no longer launder historical MOCK scores.
    is_mock = bool(force_mock or not (cells or diag_model_ids))

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
        for k in ("tool_validity", "vision_fix", "feature_id", "vc_hygiene",
                  "discipline_index", "verify_efficacy", "checkpoint_util"):
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
    cycles: set[str] | None = None,
) -> Path:
    """Aggregate and write leaderboard.json. Returns the output path."""
    doc = aggregate(
        reports_dir=reports_dir,
        diag_path=diag_path,
        force_mock=force_mock,
        cycles=cycles,
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
        cycles={cycle.strip() for cycle in args.cycles.split(",") if cycle.strip()}
        if args.cycles else None,
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
    b.add_argument("--cycles", default=None,
                   help="explicit comma-separated cycle ids to score")
    b.add_argument("--mock", action="store_true",
                   help="force -MOCK task_suite suffix (smoke/mock runs)")
    b.set_defaults(func=cmd_build)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
