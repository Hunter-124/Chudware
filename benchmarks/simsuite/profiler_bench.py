"""profiler simsuite bench — per-phase simulation timing surface (prof_phases RPC).

Contract: benchmarks/simsuite/README.md (``run(quick=False) -> list[dict]``).

This module benchmarks the PROFILING SURFACE itself, not solver accuracy: it proves
the engine's always-on per-phase timers (core/prof/phases.{h,cpp}, surfaced by the
``prof_phases`` RPC / ``engine_profile(action="phases")``) produce a complete, cheap,
per-study breakdown for the sim paths, and guards the overlay mesh-cache fix.

Cases (same in quick and full mode; the instrumented solves are already coarse):
  * phases_fea_static  : box cantilever sim_run_static -> the fea_static study must
        carry mesh/setup/solve/post + solve.* sub-phases; metric = phase coverage
        (top-level phase ms / study total ms; regression-only, expected ~1.0).
  * phases_fea_modal   : sim_run_modal -> fea_modal study with mesh+solve phases.
  * phases_mesh_cache  : re-running the SAME static study must mesh via the content
        cache — metric = number of full mesh_tet studies recorded during the re-run
        (must be 0; guards the mesh_step_cached overlay fix in core/api/libchudware.cpp).
  * phases_lbm         : tiny sim_run_flow -> lbm study with init/loop/convergence/
        output phases + a flow_run study wrapping it.
  * phases_rpc_overhead: mean prof_phases round-trip (regression-only; the report is
        a bounded ring, so this must stay ~sub-millisecond engine-side).

Solver lib: whatever services.mcp.core_bridge resolves ($CHUDWARE_LIBCHUDWARE ->
core/build-occt-linux fallback). A lib without the prof_phases RPC (not yet rebuilt
with core/prof/phases.cpp) or without sim_run_static (no CHUDWARE_WITH_NETGEN) produces
clean, reasoned skips.

No analytic references: every metric here is a structural/timing regression, so
``ref`` is None throughout (per README, regression-only cases).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

DOMAIN = "profiler"


def _cb():
    from services.mcp import core_bridge  # never bare `import core_bridge`
    return core_bridge


def _case(case_id: str, **kw) -> dict:
    d = {"domain": DOMAIN, "case": case_id, "setup_s": 0.0, "mesh_s": 0.0,
         "solve_s": 0.0, "total_s": 0.0, "metric": None, "ref": None,
         "rel_err": None, "status": "ok", "detail": ""}
    d.update(kw)
    return d


def _skip(case_id: str, reason: str) -> dict:
    return _case(case_id, status="skip", detail=reason)


def _guard(fn, case_id: str) -> dict:
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a crashing case must not kill the module
        tb = traceback.format_exc(limit=4)
        return _case(case_id, status="fail", detail=f"exception: {tb[-500:]}")


_CASE_IDS = ["phases_fea_static", "phases_fea_modal", "phases_mesh_cache",
             "phases_lbm", "phases_rpc_overhead"]


def _capabilities() -> tuple[bool, str]:
    """(ok, reason): needs the prof_phases RPC AND the Netgen FEA RPCs."""
    cb = _cb()
    try:
        if not cb.available():
            return False, "core_bridge reports libchudware not loadable"
        cb.call("prof_phases", {})
    except Exception as exc:  # noqa: BLE001
        if "unknown method" in str(exc):
            return False, ("loaded libchudware.so predates the per-phase timers "
                           "(rebuild core with prof/phases.cpp): %s" % exc)
        return False, f"engine probe failed: {exc}"
    try:
        cb.call("sim_run_static", {"study": {}})
    except Exception as exc:  # noqa: BLE001
        if "unknown method" in str(exc):
            return False, f"libchudware.so lacks sim_run_static (no CHUDWARE_WITH_NETGEN): {exc}"
        return False, f"engine probe failed: {exc}"
    return True, ""


# ------------------------------------------------------------------ fixture part

def _build_beam(cb) -> tuple[str, str, dict, dict]:
    """box beam doc -> (doc_id, step_path, left faceset, right faceset)."""
    doc = cb.call("doc_new", {"kind": "part", "name": "simsuite_profiler_beam"})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Beam", "params": {
            "length": {"value": 100.0}, "width": {"value": 20.0},
            "height": {"value": 10.0}}}}]})
    step = tempfile.mktemp(suffix=".step", prefix="simsuite_profiler_")
    cb.call("doc_export_step", {"doc_id": doc, "path": step})

    def faceset(side: str) -> dict:
        q = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": side}})
        m = [x for x in q.get("matches", []) if x.get("kind") == "face"][0]
        return cb.call("doc_face_points", {"doc_id": doc, "selector": m})

    return doc, step, faceset("left"), faceset("right")


_STEEL = {"name": "simsuite-steel", "E_mpa": 200000.0, "nu": 0.3,
          "density_g_cm3": 7.85, "yield_mpa": 250.0}


def _studies(cb) -> list[dict]:
    return cb.call("prof_phases", {}).get("studies", [])


def _find(studies: list[dict], domain: str) -> dict | None:
    hits = [s for s in studies if s.get("domain") == domain]
    return hits[-1] if hits else None


def _phase_ms(study: dict, name: str) -> float:
    for p in study.get("phases", []):
        if p.get("name") == name:
            return float(p.get("ms", 0.0))
    return 0.0


# ------------------------------------------------------------------ cases

def _case_fea_static(cb, study_req: dict) -> dict:
    t0 = time.perf_counter()
    cb.call("prof_phases", {"reset": True})
    res = cb.call("sim_run_static", {"study": study_req})
    total = time.perf_counter() - t0
    if not res.get("ok"):
        return _case("phases_fea_static", total_s=total, status="fail",
                     detail=f"solve failed: {res.get('error')}")
    st = _find(_studies(cb), "fea_static")
    if st is None:
        return _case("phases_fea_static", total_s=total, status="fail",
                     detail="no fea_static study in prof_phases after sim_run_static")
    names = [p["name"] for p in st.get("phases", [])]
    missing = [n for n in ("mesh", "setup", "solve", "post") if n not in names]
    n_sub = sum(1 for n in names if n.startswith("solve."))
    top_ms = sum(_phase_ms(st, n) for n in ("mesh", "setup", "solve", "post"))
    total_ms = float(st.get("total_ms", 0.0))
    coverage = (top_ms / total_ms) if total_ms > 0 else 0.0
    status, detail = "ok", f"phases={names}"
    if missing:
        status, detail = "fail", f"missing top-level phases {missing} (got {names})"
    elif n_sub < 3:
        status, detail = "fail", f"expected >=3 solve.* sub-phases, got {n_sub} ({names})"
    elif not (0.5 <= coverage <= 1.01):
        # The four top-level phases tile the study; big gaps mean a lost scope.
        status, detail = "fail", f"phase coverage {coverage:.2f} outside [0.5, 1.01]"
    return _case("phases_fea_static", total_s=total,
                 mesh_s=_phase_ms(st, "mesh") / 1e3, solve_s=_phase_ms(st, "solve") / 1e3,
                 metric=coverage, status=status, detail=detail)


def _case_fea_modal(cb, step: str, fix: dict) -> dict:
    t0 = time.perf_counter()
    cb.call("prof_phases", {"reset": True})
    res = cb.call("sim_run_modal", {"study": {"step_path": step, "material": _STEEL,
                                              "fixtures": [fix], "mesh_order": 2,
                                              "fineness": 0.3}, "n_modes": 3})
    total = time.perf_counter() - t0
    if not res.get("ok"):
        return _case("phases_fea_modal", total_s=total, status="fail",
                     detail=f"modal failed: {res.get('error')}")
    st = _find(_studies(cb), "fea_modal")
    if st is None:
        return _case("phases_fea_modal", total_s=total, status="fail",
                     detail="no fea_modal study in prof_phases after sim_run_modal")
    names = [p["name"] for p in st.get("phases", [])]
    ok = "mesh" in names and "solve" in names
    return _case("phases_fea_modal", total_s=total,
                 mesh_s=_phase_ms(st, "mesh") / 1e3, solve_s=_phase_ms(st, "solve") / 1e3,
                 metric=float(len(names)), status="ok" if ok else "fail",
                 detail=f"phases={names}")


def _case_mesh_cache(cb, study_req: dict) -> dict:
    # The fixture solve already ran once, so the mesh content-cache is warm: a re-run
    # must NOT record a full mesh_tet study (resolve_mesh AND the overlay recovery both
    # go through mesh_step_cached — the latter is the libchudware.cpp fix this guards).
    t0 = time.perf_counter()
    cb.call("prof_phases", {"reset": True})
    res = cb.call("sim_run_static", {"study": study_req})
    total = time.perf_counter() - t0
    if not res.get("ok"):
        return _case("phases_mesh_cache", total_s=total, status="fail",
                     detail=f"re-solve failed: {res.get('error')}")
    studies = _studies(cb)
    n_mesh = sum(1 for s in studies if s.get("domain") == "mesh_tet")
    st = _find(studies, "fea_static")
    mesh_ms = _phase_ms(st, "mesh") if st else -1.0
    ok = n_mesh == 0
    return _case("phases_mesh_cache", total_s=total, mesh_s=max(mesh_ms, 0.0) / 1e3,
                 solve_s=(_phase_ms(st, "solve") / 1e3) if st else 0.0,
                 metric=float(n_mesh), status="ok" if ok else "fail",
                 detail=("cache hit (mesh phase %.2f ms)" % mesh_ms) if ok else
                        f"{n_mesh} full mesh_tet run(s) on a warm cache — duplicate meshing")


def _case_lbm(cb, doc: str, quick: bool) -> dict:
    t0 = time.perf_counter()
    cb.call("prof_phases", {"reset": True})
    res = cb.call("sim_run_flow", {"doc_id": doc, "velocity": 10.0,
                                   "cells": 32 if quick else 48,
                                   "n_steps": 200 if quick else 500})
    total = time.perf_counter() - t0
    if not res.get("ok"):
        return _case("phases_lbm", total_s=total, status="fail",
                     detail=f"flow failed: {res.get('error')}")
    studies = _studies(cb)
    lbm = _find(studies, "lbm")
    flow = _find(studies, "flow_run")
    if lbm is None or flow is None:
        return _case("phases_lbm", total_s=total, status="fail",
                     detail=f"missing lbm/flow_run studies: {[s['domain'] for s in studies]}")
    names = [p["name"] for p in lbm.get("phases", [])]
    missing = [n for n in ("init", "loop", "convergence", "output") if n not in names]
    fnames = [p["name"] for p in flow.get("phases", [])]
    ok = not missing and "voxelize" in fnames and "solve" in fnames
    return _case("phases_lbm", total_s=total, solve_s=_phase_ms(lbm, "loop") / 1e3,
                 metric=float(len(names)), status="ok" if ok else "fail",
                 detail=f"lbm={names} flow_run={fnames}" if ok else
                        f"missing lbm phases {missing}; flow_run={fnames}")


def _case_rpc_overhead(cb) -> dict:
    n = 50
    t0 = time.perf_counter()
    for _ in range(n):
        cb.call("prof_phases", {})
    total = time.perf_counter() - t0
    mean_ms = total * 1e3 / n
    # Regression-only ceiling: the report is a bounded 64-study ring; a full engine
    # round-trip for it staying under 50 ms means callers can poll it freely.
    ok = mean_ms < 50.0
    return _case("phases_rpc_overhead", total_s=total, metric=mean_ms,
                 status="ok" if ok else "fail",
                 detail=f"mean prof_phases round-trip {mean_ms:.3f} ms over {n} calls")


# ------------------------------------------------------------------ entry point

def run(quick: bool = False) -> list[dict]:
    os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    ok, reason = _capabilities()
    if not ok:
        return [_skip(cid, reason) for cid in _CASE_IDS]

    cb = _cb()
    t_setup0 = time.perf_counter()
    doc, step, fix, load = _build_beam(cb)
    setup_s = time.perf_counter() - t_setup0
    study_req = {"step_path": step, "material": _STEEL, "fixtures": [fix],
                 "loads": [{"faces": load, "type": "force", "vector": [0.0, 0.0, -100.0]}],
                 "mesh_order": 2, "fineness": 0.3}

    cases = []
    try:
        c = _guard(lambda: _case_fea_static(cb, study_req), _CASE_IDS[0])
        c["setup_s"] = setup_s  # part build + STEP export charged to the first case
        cases.append(c)
        cases.append(_guard(lambda: _case_fea_modal(cb, step, fix), _CASE_IDS[1]))
        cases.append(_guard(lambda: _case_mesh_cache(cb, study_req), _CASE_IDS[2]))
        cases.append(_guard(lambda: _case_lbm(cb, doc, quick), _CASE_IDS[3]))
        cases.append(_guard(lambda: _case_rpc_overhead(cb), _CASE_IDS[4]))
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc})
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(step)
        except OSError:
            pass
    return cases


if __name__ == "__main__":
    import json
    print(json.dumps(run(quick="--quick" in sys.argv), indent=2))
