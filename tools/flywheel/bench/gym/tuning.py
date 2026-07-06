"""Gym tuning loop (T-DP10) — measures best scaffold level per task-class.

Runs ≥2 scaffold configurations over the C-suite challenges and records per-class
win rates.  The router (services/agent/pipeline/workflow_router.py) consumes the
output (``reports/tuning_results.json``) to pick the measured-best scaffold level
per task-class; when configs tie, the router falls back to the tier default.

Honesty rules (non-negotiable):
  - Every number cites a real gym cell run (or an honest mock cell).
  - A tie (all configs at the same win_rate) is recorded as tied=True and
    consumed by the router as a fallback to the tier default.
  - This module never fabricates win-rates or scaffolds a false gradient.
  - The ``mode`` field in every cell record is the literal CellResult.mode
    ("real" or "mock") — never overridden.

Usage (real CUDA run):
    python3 -m tools.flywheel.bench.gym.tuning run \\
        --cycle dp10-tuning \\
        --configs eco maximum \\
        --challenges C1 C1-DP2 \\
        --tiers local-24k \\
        --seeds 1

Usage (mock / CI):
    python3 -m tools.flywheel.bench.gym.tuning run \\
        --cycle dp10-tuning-mock \\
        --configs eco maximum \\
        --challenges C1 \\
        --tiers local-24k \\
        --seeds 1 \\
        --mock

Output: ``reports/tuning_results.json`` (and telemetry via the standard path).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
sys.path.insert(0, str(_REPO_ROOT))

TUNING_SCHEMA = "aicad.tuning/1"
REPORTS_DIR = _HERE / "reports"
TUNING_RESULTS_PATH = REPORTS_DIR / "tuning_results.json"


# ---------------------------------------------------------------------------
# Per-config cell result (wraps the gym CellResult)
# ---------------------------------------------------------------------------

@dataclass
class ConfigCellResult:
    """One gym cell result with the scaffold config it ran under."""
    challenge: str
    tier: str
    seed: int
    scaffold_config: str    # the effort level (eco / standard / deliberate / maximum)
    passed: bool
    mode: str               # "real" | "mock" — verbatim from CellResult
    mission_status: str
    mission_phase: str
    failure_classes: list[str] = field(default_factory=list)
    run_dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge": self.challenge, "tier": self.tier, "seed": self.seed,
            "scaffold_config": self.scaffold_config, "passed": self.passed,
            "mode": self.mode, "mission_status": self.mission_status,
            "mission_phase": self.mission_phase,
            "failure_classes": self.failure_classes, "run_dir": self.run_dir,
        }


# ---------------------------------------------------------------------------
# Win-rate aggregation
# ---------------------------------------------------------------------------

def _task_class_for_challenge(challenge_id: str) -> str:
    """Map a challenge id to its task-class (for the per_class_winners key).

    Uses the same classification the router uses for consistency: we look up the
    challenge's request text and run it through the router classifier.
    """
    try:
        from tools.flywheel.bench.gym.challenges import load_challenge
        from services.agent.pipeline.workflow_router import classify_task
        ch = load_challenge(challenge_id)
        return classify_task(ch.request).value
    except Exception:
        return "unknown"


def compute_win_rates(cells: list[ConfigCellResult],
                      configs: list[str]) -> dict[str, Any]:
    """Aggregate per-config win rates and elect a per-class winner.

    Returns a dict with:
      per_config_win_rates:  {config: {challenge|tier: {pass_rate, n}}}
      per_class_winners:     {task_class|tier: {scaffold_level, win_rate, n, tied, cycle}}
    """
    # -- per-config aggregation -----------------------------------------------
    per_config: dict[str, dict[str, list[bool]]] = {c: {} for c in configs}
    for cell in cells:
        key = f"{cell.challenge}|{cell.tier}"
        per_config[cell.scaffold_config].setdefault(key, []).append(cell.passed)

    per_config_rates: dict[str, dict[str, Any]] = {}
    for cfg, cell_map in per_config.items():
        per_config_rates[cfg] = {
            k: {"pass_rate": sum(v) / len(v) if v else 0.0, "n": len(v)}
            for k, v in cell_map.items()
        }

    # -- per-class winner election --------------------------------------------
    # key: task_class|tier  → best config (highest win_rate; tie → tied=True)
    # Collect per (task_class, tier) → {config: [pass results]}
    class_tier_config: dict[str, dict[str, list[bool]]] = {}
    for cell in cells:
        tc = _task_class_for_challenge(cell.challenge)
        norm_tier = _normalize_tier(cell.tier)
        ctkey = f"{tc}|{norm_tier}"
        class_tier_config.setdefault(ctkey, {}).setdefault(cell.scaffold_config, []).append(cell.passed)

    per_class_winners: dict[str, Any] = {}
    for ctkey, cfg_map in class_tier_config.items():
        rates = {cfg: (sum(v) / len(v) if v else 0.0, len(v))
                 for cfg, v in cfg_map.items()}
        best_cfg = max(rates, key=lambda c: rates[c][0])
        best_rate, best_n = rates[best_cfg]
        # Check tie: any other config at the same rate?
        all_rates = [r for r, _ in rates.values()]
        is_tied = all(r == all_rates[0] for r in all_rates)
        per_class_winners[ctkey] = {
            "scaffold_level": best_cfg,
            "win_rate": best_rate,
            "n": best_n,
            "tied": is_tied,
            "all_configs": {c: {"win_rate": r, "n": n} for c, (r, n) in rates.items()},
        }

    return {
        "per_config_win_rates": per_config_rates,
        "per_class_winners": per_class_winners,
    }


def _normalize_tier(tier: str) -> str:
    for prefix in ("frontier", "local", "mid"):
        if tier.startswith(prefix):
            return prefix
    return tier


# ---------------------------------------------------------------------------
# Per-config cell runner
# ---------------------------------------------------------------------------

def run_config_cell(
    challenge_id: str,
    tier: str,
    seed: int,
    scaffold_config: str,
    *,
    cycle: str,
    runs_dir: Path,
    mock: bool = False,
    config_loader=None,
) -> ConfigCellResult:
    """Run one (challenge, tier, seed) cell under *scaffold_config*.

    *scaffold_config* is an effort preset level (eco / standard / deliberate / maximum).
    The cell injects the config level via a monkey-patched load_config so the gym's
    existing resolve_effort path picks it up (the ONE resolution path — effort.py).

    *mock*: force MOCK mode regardless of probe (used in tests and CI).
    *config_loader*: optional callable returning a base config dict (tests inject this).
    """
    import services.agent.config as cfg_mod
    from tools.flywheel.bench.gym.runner import run_cell, execution_probe
    from tools.flywheel.bench.gym.challenges import load_challenge

    ch = load_challenge(challenge_id)
    probe = execution_probe()

    # Patch load_config to inject the scaffold level.
    _orig = config_loader or cfg_mod.load_config

    def _patched():
        c = _orig() if callable(_orig) else {}
        c.setdefault("ai_effort", {})["level"] = scaffold_config
        return c

    _saved = cfg_mod.load_config
    cfg_mod.load_config = _patched
    try:
        result = run_cell(
            ch, tier, seed,
            cycle=f"{cycle}_{scaffold_config}",
            probe=probe,
            runs_dir=runs_dir,
            real=(not mock),
        )
    finally:
        cfg_mod.load_config = _saved

    return ConfigCellResult(
        challenge=challenge_id,
        tier=tier,
        seed=seed,
        scaffold_config=scaffold_config,
        passed=result.passed,
        mode=result.mode,
        mission_status=result.mission_status,
        mission_phase=result.mission_phase,
        failure_classes=[c.failure_class for c in result.classifications],
        run_dir=result.run_dir,
    )


# ---------------------------------------------------------------------------
# Full tuning run
# ---------------------------------------------------------------------------

@dataclass
class TuningResult:
    """The output of one tuning run over a config × challenge × tier × seed matrix."""
    cycle: str
    configs: list[str]
    challenges: list[str]
    tiers: list[str]
    seeds: list[int]
    cells: list[ConfigCellResult] = field(default_factory=list)
    win_rates: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": TUNING_SCHEMA,
            "cycle": self.cycle,
            "configs": self.configs,
            "challenges": self.challenges,
            "tiers": self.tiers,
            "seeds": self.seeds,
            "cells": [c.to_dict() for c in self.cells],
            **self.win_rates,
        }


def run_tuning(
    *,
    cycle: str = "dp10-tuning",
    configs: list[str] | None = None,
    challenge_ids: list[str] | None = None,
    tiers: list[str] | None = None,
    seeds: list[int] | None = None,
    runs_dir: Path | None = None,
    output_path: Path | None = None,
    mock: bool = False,
    config_loader=None,
) -> TuningResult:
    """Run ≥2 scaffold configs over the specified challenges and record win rates.

    Default configs: ``eco`` and ``maximum`` (the two extremes of the §7 dial,
    covering full=MAXIMUM vs minimal=ECO — the widest possible contrast).
    Default challenges: C1 and C1-DP2 (the C-suite pair with the most task-class
    diversity available without a frontier key).
    Default tiers: local-24k.
    Default seeds: [1] (one seed per cell is sufficient for the probe; the full
    cycle-1 suite uses seeds 1-3 per challenge).

    Writes ``reports/tuning_results.json`` (the router's consumption path).
    """
    _configs = configs if configs is not None else ["eco", "maximum"]
    _challenges = challenge_ids if challenge_ids is not None else ["C1", "C1-DP2"]
    _tiers = tiers if tiers is not None else ["local-24k"]
    _seeds = seeds if seeds is not None else [1]
    _runs_dir = runs_dir or (REPORTS_DIR / "runs" / cycle)
    _runs_dir.mkdir(parents=True, exist_ok=True)

    if len(_configs) < 2:
        raise ValueError(
            f"tuning requires ≥2 scaffold configs; got {_configs!r}. "
            "Pass at least ['eco', 'maximum'] to get a meaningful comparison."
        )

    result = TuningResult(
        cycle=cycle,
        configs=_configs,
        challenges=_challenges,
        tiers=_tiers,
        seeds=_seeds,
    )

    for challenge_id in _challenges:
        for tier in _tiers:
            for seed in _seeds:
                for cfg in _configs:
                    print(f"  tuning cell: {challenge_id} {tier} s{seed} config={cfg} ...",
                          flush=True)
                    try:
                        cell = run_config_cell(
                            challenge_id, tier, seed, cfg,
                            cycle=cycle,
                            runs_dir=_runs_dir,
                            mock=mock,
                            config_loader=config_loader,
                        )
                    except Exception as exc:
                        # A cell error is DATA (honest failure), never fatal to the run.
                        print(f"    ERROR: {exc}", flush=True)
                        cell = ConfigCellResult(
                            challenge=challenge_id, tier=tier, seed=seed,
                            scaffold_config=cfg, passed=False,
                            mode="error", mission_status="error",
                            mission_phase="intake",
                            failure_classes=["execution_stall"],
                        )
                    result.cells.append(cell)
                    print(f"    → passed={cell.passed} mode={cell.mode} "
                          f"phase={cell.mission_phase}", flush=True)

    result.win_rates = compute_win_rates(result.cells, _configs)

    # Persist the tuning results for the router to consume.
    out_path = output_path or TUNING_RESULTS_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    print(f"\nTuning results written → {out_path}")

    # Print per-class winner summary.
    print("\nPer-class winners (scaffold level → router default):")
    for ctkey, winner in result.win_rates.get("per_class_winners", {}).items():
        tied_str = " [TIED → tier-default fallback]" if winner.get("tied") else ""
        print(f"  {ctkey}: {winner['scaffold_level']} "
              f"win_rate={winner['win_rate']:.2f} n={winner['n']}{tied_str}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python3 -m tools.flywheel.bench.gym.tuning",
        description="Run the gym tuning loop (T-DP10): ≥2 scaffold configs over the C-suite.",
    )
    sub = ap.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Execute the tuning loop and write results.")
    run_p.add_argument("--cycle", default="dp10-tuning",
                       help="Cycle label (default: dp10-tuning)")
    run_p.add_argument("--configs", nargs="+", default=["eco", "maximum"],
                       help="Scaffold configs to compare (default: eco maximum)")
    run_p.add_argument("--challenges", nargs="+", default=None,
                       help="Challenge ids (default: C1 C1-DP2)")
    run_p.add_argument("--tiers", nargs="+", default=["local-24k"],
                       help="Tier names (default: local-24k)")
    run_p.add_argument("--seeds", nargs="+", type=int, default=[1],
                       help="Seeds (default: 1)")
    run_p.add_argument("--mock", action="store_true",
                       help="Force MOCK mode (no real model calls; for CI)")
    run_p.add_argument("--output", default=None,
                       help="Override output path (default: reports/tuning_results.json)")

    show_p = sub.add_parser("show", help="Print current tuning results.")
    show_p.add_argument("--path", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "run":
        out_path = Path(args.output) if args.output else None
        try:
            result = run_tuning(
                cycle=args.cycle,
                configs=args.configs,
                challenge_ids=args.challenges,
                tiers=args.tiers,
                seeds=args.seeds,
                mock=args.mock,
                output_path=out_path,
            )
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"\nDone. {len(result.cells)} cells run.")
        return 0

    elif args.cmd == "show":
        p = Path(args.path) if args.path else TUNING_RESULTS_PATH
        if not p.exists():
            print(f"No tuning results at {p}", file=sys.stderr)
            return 1
        data = json.loads(p.read_text(encoding="utf-8"))
        print(json.dumps(data, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    # T-DP19: re-exec into the mcp-venv if this interpreter lacks the DAG deps, so real
    # tuning cells reach BUILD instead of dying at PLAN (and tying every config at 0.00).
    from tools.flywheel.bench.gym.runner import ensure_capable_interpreter
    ensure_capable_interpreter("tools.flywheel.bench.gym.tuning")
    raise SystemExit(_cli(sys.argv[1:]))
