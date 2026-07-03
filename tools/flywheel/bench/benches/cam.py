"""CAM bench (IP-4): gouge-free + machined-fraction on a small CAM samples set.

Score per example (survey §6 CAM success bar): a candidate operation plan is simulated
with `cam_simulate`; the run is a hard fail if it gouges (`gouges > 0`), REGARDLESS of how
much material it removed — a high `machined_fraction` never masks a gouge. Otherwise the
score is the raw `machined_fraction` (0..1), and `passed` additionally requires clearing a
completion bar (`machined_fraction >= TARGET_FRACTION`, default 0.90).

Battery path (T-BL23): `_extract_score` now recognises `machined_fraction` (scalar) and
treats `gouges` as a binary-safety pre-gate (any gouge → score 0.0, count surfaced in
findings). This bench routes its score computation through `battery._extract_score` directly
so the battery path and the raw-result path produce identical verdicts on every fixture.
The raw `cam_simulate` result is still read for diagnostics (`detail` fields, aggregates,
ROC labels) — only the *score* derivation delegates to the battery function.
The `battery_cam_simulate` detail field (through-battery check score from `verify()`) is
still populated in live mode for CI/gate comparability.

Two evaluation paths:
  * fixture mode (offline, default, no core needed): each committed job fixture under
    `training/benches/cam/jobs/*.json` carries a recorded `expected_sim` -- a
    cam_simulate-shaped result for that job's stock + candidate op plan (the deterministic
    roughing+finishing template's own prior output, hand-recorded since there is no C++
    core to regenerate it offline). Scoring reads those recorded fields directly. This is
    an HONESTY TRADE-OFF worth stating plainly: offline numbers are RECORDED, not
    recomputed -- they do not verify today's `cam_simulate` still produces them.
  * live mode (core present): the same job (stock/tools/setup/op_plan) is built through
    `mcp_server.cam_tools` (the same tool surface the MCP server exposes) and RECOMPUTED
    by calling `cam_simulate` for real, so the live path is a genuine, non-recorded check.
    Guarded skip-if-no-core, mirroring `regen_fix`'s live path and gate.sh's OCCT-absent
    skip. Never hardcodes a sibling worktree's `.so` path -- resolution is entirely
    `mcp_server.core_bridge`'s own `$AI_CAD_LIBAICAD` / `$AI_CAD_DIST` / platform search.

The deterministic baseline is not a separately-run competing strategy here: the fixtures
ARE the deterministic candidate's own recorded results (a fixed roughing+finishing op-plan
template -- see `deterministic_op_plan()` -- applied to each job's stock/part). So
`baselines["deterministic"]` is computed directly from the per-example scores/passes.
"""
from __future__ import annotations

import glob
import json
import os
from typing import Any, Callable, Optional

from ..baseline import Baseline, pending_frontier
from ..dataset import DatasetSpec
from ..metrics import mean, pass_rate, roc_auc, residual as residual_metric
from ..schema import BenchResult, ExampleScore
from services.verify.battery import _extract_score as _battery_extract_score

TARGET_FRACTION = 0.90  # survey §6 CAM success bar: gouge-free AND machined_fraction >= this

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "..", ".."))
JOBS_DIR = os.path.join(_REPO_ROOT, "training", "benches", "cam", "jobs")

# LiveSim: callable(fixture: dict) -> (raw_sim_result: dict, battery_cam_simulate: Optional[float])
LiveSim = Callable[[dict], "tuple[dict, Optional[float]]"]


# ---- deterministic roughing+finishing op-plan template ----------------------

def deterministic_op_plan(part_family: str, stock: dict, *, tool_number: int = 1,
                          margin: float = 5.0) -> list[dict]:
    """A fixed, no-model roughing(+finishing) operation-plan template.

    Not a real CAM strategist -- a reasonable, always-available default: for a milled
    pocket/contour family it centers a rectangular roughing pocket inset `margin` mm from
    the stock XY bounds and cuts to `margin` mm above the stock floor, then a matching
    finishing contour pass; for a drilled family it centers a single through-hole; any
    other family gets a conservative single facing pass. This is the "kill-switch
    candidate op plan" the committed job fixtures' `expected_sim` results were recorded
    against.
    """
    lo = stock.get("min", [-25, -25, -10])
    hi = stock.get("max", [25, 25, 0])
    x0, y0, z0 = lo[0] + margin, lo[1] + margin, lo[2] + margin
    x1, y1, z1 = hi[0] - margin, hi[1] - margin, hi[2]
    region = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    if part_family == "drilled_plate":
        cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
        return [{
            "type": "drill", "tool_number": tool_number,
            "heights": {"top": hi[2], "bottom": lo[2], "retract": hi[2] + 2},
            "geometry": {"points": [[cx, cy, hi[2]]]},
            "params": {"cycle": "peck", "peck": 2},
        }]

    if part_family in ("pocketed_block", "contoured_part"):
        return [
            {
                "type": "pocket2d", "tool_number": tool_number,
                "heights": {"top": hi[2], "bottom": z0},
                "geometry": {"regions": [{"outer": region}]},
                "params": {"stepdown": 2, "stepover": 3},
            },
            {
                "type": "contour2d", "tool_number": tool_number,
                "heights": {"top": hi[2], "bottom": z0},
                "geometry": {"contours": [region]},
                "params": {"stock_to_leave": 0.0, "direction": "climb"},
            },
        ]

    # unknown family: a conservative single facing pass over the top
    return [{
        "type": "face", "tool_number": tool_number,
        "heights": {"top": hi[2], "bottom": hi[2] - 0.5},
        "geometry": {"regions": [{"outer": [[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]]}]},
        "params": {"stepover": 4},
    }]


# ---- fixtures -----------------------------------------------------------------

def load_fixtures(jobs_dir: str = JOBS_DIR) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(jobs_dir, "*.json")))
    fixtures = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            fixtures.append(json.load(f))
    return fixtures


def freeze_dataset(jobs_dir: str = JOBS_DIR, *, split: str = "all") -> DatasetSpec:
    fixtures = load_fixtures(jobs_dir)
    structures = sorted({f["part_family"] for f in fixtures})
    return DatasetSpec.freeze("cam_jobs", jobs_dir, split=split,
                              n_examples=len(fixtures), structures=structures)


# ---- scoring --------------------------------------------------------------

def score_example(fixture: dict, *, live_sim: Optional[LiveSim] = None,
                  target: float = TARGET_FRACTION) -> ExampleScore:
    """Score one CAM job fixture.

    `live_sim`, when provided (live mode), is a callable(fixture) -> (raw_sim_result,
    battery_cam_simulate_score) that builds the job through `mcp_server.cam_tools` and
    runs the real `cam_simulate`. When None (offline / no core), the fixture's recorded
    `expected_sim` is used directly.
    """
    mode = "fixture"
    battery_score: Optional[float] = None
    if live_sim is not None:
        try:
            raw, battery_score = live_sim(fixture)
            mode = "live"
        except Exception as e:  # core error mid-run -> fall back to the recorded fixture
            raw = fixture["expected_sim"]
            mode = f"fixture (live_error: {type(e).__name__}: {e})"[:200]
    else:
        raw = fixture["expected_sim"]

    gouges = int(raw.get("gouges", 0))
    machined_fraction = float(raw.get("machined_fraction", 0.0))
    gouge_free = gouges == 0

    # Route score derivation through the battery's _extract_score so this bench and
    # any future battery consumer produce identical verdicts (T-BL23 DoD item 3).
    # The gouge pre-gate in _extract_score ensures gouges > 0 → score 0.0 regardless
    # of machined_fraction.  machined_fraction is a clean 0..1 scalar, so it maps 1:1.
    score, _bat_findings = _battery_extract_score(
        "cam_simulate", "scalar", json.dumps(raw))
    passed = bool(gouge_free and machined_fraction >= target)

    detail: dict[str, Any] = {
        "mode": mode,
        "part_family": fixture["part_family"],
        "gouges": gouges,
        "max_gouge_mm": raw.get("max_gouge_mm", 0.0),
        "machined_fraction": machined_fraction,
        "target_fraction": target,
        "rapid_collisions": raw.get("rapid_collisions", 0),
        "warnings": raw.get("warnings", []),
        "sim_ok": raw.get("ok", None),
    }
    if battery_score is not None:
        detail["battery_cam_simulate"] = battery_score

    return ExampleScore(
        example_id=fixture["id"], score=score, passed=passed,
        structure=fixture["part_family"], label=gouge_free,
        baselines={"deterministic": score}, detail=detail,
    )


def run(fixtures: list[dict], dataset_hash: str, *, split: str = "all",
       live_sim: Optional[LiveSim] = None, target: float = TARGET_FRACTION,
       frontier: Optional[Baseline] = None) -> BenchResult:
    examples = [score_example(f, live_sim=live_sim, target=target) for f in fixtures]

    det_mean = mean([e.score for e in examples])
    det_pass = pass_rate([e.passed for e in examples])
    gouge_free_rate = pass_rate([e.detail["gouges"] == 0 for e in examples])
    mean_fraction = mean([e.detail["machined_fraction"] for e in examples])
    auc = roc_auc([bool(e.label) for e in examples], [e.score for e in examples])

    per_family: dict[str, dict] = {}
    for e in examples:
        fam = e.structure
        d = per_family.setdefault(fam, {"n": 0, "passed": 0, "gouge_free": 0})
        d["n"] += 1
        d["passed"] += int(e.passed)
        d["gouge_free"] += int(e.detail["gouges"] == 0)

    frontier = frontier or pending_frontier()
    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(),
    ]
    resid = ({"status": "pending", "baseline_mean": det_mean,
              "note": "frontier candidate pending; residual measured when it lands"}
             if not frontier.available else
             residual_metric(mean([e.baselines.get(frontier.name, 0.0) for e in examples]),
                             det_mean))

    return BenchResult(
        bench_id="cam", dataset_id="cam_jobs", dataset_hash=dataset_hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate={
            "gouge_free_rate": gouge_free_rate,
            "mean_machined_fraction": mean_fraction,
            "target_fraction": target,
            "pass_rate": det_pass,
            "roc_auc_gouge_free": auc,
            "per_family": per_family,
            "success_bar": {
                "gouge_free_rate==1.0": gouge_free_rate == 1.0,
                f"mean_machined_fraction>={target}": mean_fraction >= target,
                "gouge_free_and_fraction_ge_target": bool(
                    gouge_free_rate == 1.0 and mean_fraction >= target),
            },
        },
        residual=resid if isinstance(resid, dict) else {**resid, "status": "measured"},
    )


# ---- live-core path (guarded skip-if-no-core) --------------------------------

def make_live_sim() -> Optional[LiveSim]:
    """Build a `live_sim` callable if the C++ core is available; else None (skip).

    Never hardcodes a sibling worktree's `.so` path -- resolution goes entirely through
    `mcp_server.core_bridge`'s own `$AI_CAD_LIBAICAD` / `$AI_CAD_DIST` / platform search.
    """
    try:
        from mcp_server import core_bridge as cb
    except Exception:
        return None
    if not cb.available():
        return None

    def _live_sim(fixture: dict) -> "tuple[dict, Optional[float]]":
        from mcp.server.fastmcp import FastMCP
        from mcp_server.cam_tools import register_cam_tools

        mcp = FastMCP("bench-cam")
        register_cam_tools(mcp)
        fns = {name: tool.fn for name, tool in mcp._tool_manager._tools.items()}

        r = json.loads(fns["cam_new_job"](name=fixture["id"], post="grbl"))
        doc_id = r.get("doc_id")
        try:
            for tool in fixture["tools"]:
                json.loads(fns["cam_add_tool"](**tool))

            setup_kwargs = dict(fixture["setup"])
            setup_kwargs["stock"] = fixture["stock"]
            setup_kwargs["material"] = fixture.get("material", "aluminum")
            setup_r = json.loads(fns["cam_add_setup"](**setup_kwargs))
            setup_id = setup_r.get("setup_id")

            for op in fixture["op_plan"]:
                op_kwargs = dict(op)
                op_kwargs["setup_id"] = setup_id
                json.loads(fns["cam_add_operation"](**op_kwargs))

            json.loads(fns["cam_generate"](setup_id=setup_id))
            sim = json.loads(fns["cam_simulate"](setup_id=setup_id))
            results = sim.get("results") or [{}]
            raw = results[0]

            battery_score: Optional[float] = None
            try:
                from services.verify.battery import verify  # noqa: local import (core-only path)
                v = verify(None, ["cam_simulate"])
                battery_score = v.get("scores", {}).get("cam_simulate")
            except Exception:
                battery_score = None

            return raw, battery_score
        finally:
            try:
                cb.call("cam_close", {"doc_id": doc_id})
            except Exception:
                pass

    return _live_sim


# ---- BenchDriver wiring -------------------------------------------------------

def _run(smoke: bool, workdir: str) -> BenchResult:  # noqa: ARG001 (workdir unused: fixtures are committed)
    fixtures = load_fixtures()
    if smoke:
        fixtures = fixtures[:4]
    spec = freeze_dataset(split="smoke" if smoke else "all")
    live_sim = make_live_sim()
    return run(fixtures, spec.hash, split=spec.split, live_sim=live_sim)


from ..driver import BenchDriver  # noqa: E402 (after helpers so `run`/`score_example` names are free)

DRIVER = BenchDriver("cam", _run)
