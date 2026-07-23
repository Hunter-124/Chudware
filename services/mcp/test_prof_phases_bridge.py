"""Live-bridge regression for the per-phase simulation timers (prof_phases RPC).

Drives the REAL engine in-process on the raw core bridge (no MCP server), the same
bootstrap as test_fea_live_bridge.py: probe candidate .so paths in a fresh subprocess
each (core_bridge binds one .so per process) and run against the first that exposes
BOTH prof_phases (core built with prof/phases.cpp) and sim_run_static (CHUDWARE_WITH_NETGEN).
It skips honestly, with the probed reasons, when no candidate has the phase timers —
e.g. before the core rebuild that ships them.

Asserts, after one small box-cantilever sim_run_static + sim_run_modal:
  * prof_phases returns {ok, completed, studies, totals, unscoped};
  * a fea_static study exists with the mesh/setup/solve/post split plus >=3 solve.*
    sub-phases (FeaProf mark boundaries), phases tiling ~100% of the study wall time;
  * a mesh_tet study exists with load_step/generate/extract;
  * a fea_modal study exists with mesh+solve;
  * re-running the same study records NO new mesh_tet study (warm content cache —
    guards the mesh_step_cached overlay fix in core/api/libchudware.cpp);
  * prof_report carries the additive "phases" key (shape extension, never a break);
  * prof_status carries the additive "phase_studies" counter.

Run directly:  CHUDWARE_LIBCHUDWARE=core/build-prof/bin/libchudware.so \
               python -m pytest services/mcp/test_prof_phases_bridge.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Subprocess driver (fresh process per candidate .so — core_bridge binds once)
# ---------------------------------------------------------------------------

def _driver() -> None:
    def _emit(obj: dict) -> None:
        print("PHASES_RESULT " + json.dumps(obj))

    from services.mcp import core_bridge as cb  # never bare `import core_bridge`
    from services.mcp.core_bridge import CoreError

    so_path = os.environ.get("CHUDWARE_LIBCHUDWARE", "<default resolution>")
    for method, params in (("prof_phases", {}), ("sim_run_static", {"study": {}})):
        try:
            cb.call(method, params)
        except CoreError as exc:
            if "unknown method" in str(exc):
                _emit({"skipped": f"lib lacks {method}: {so_path}"})
                return
            # sim_run_static rejects the empty study IN-BAND (ok:false) — reaching a
            # non-unknown-method CoreError means something else broke; surface it.
            if method == "prof_phases":
                raise

    doc = cb.call("doc_new", {"kind": "part", "name": "prof_phases_beam"})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": doc, "ops": [
        {"op": "add_feature", "feature": {"type": "box", "name": "Beam", "params": {
            "length": {"value": 100.0}, "width": {"value": 20.0},
            "height": {"value": 10.0}}}}]})
    step = tempfile.mktemp(suffix=".step", prefix="prof_phases_")
    cb.call("doc_export_step", {"doc_id": doc, "path": step})

    def faceset(side: str) -> dict:
        q = cb.call("doc_query", {"doc_id": doc, "query": {"of": "face", "side": side}})
        m = [x for x in q.get("matches", []) if x.get("kind") == "face"][0]
        return cb.call("doc_face_points", {"doc_id": doc, "selector": m})

    steel = {"name": "phases-steel", "E_mpa": 200000.0, "nu": 0.3,
             "density_g_cm3": 7.85, "yield_mpa": 250.0}
    study = {"step_path": step, "material": steel, "fixtures": [faceset("left")],
             "loads": [{"faces": faceset("right"), "type": "force",
                        "vector": [0.0, 0.0, -100.0]}],
             "mesh_order": 2, "fineness": 0.3}

    cb.call("prof_phases", {"reset": True})
    static = cb.call("sim_run_static", {"study": study})
    modal = cb.call("sim_run_modal", {"study": {"step_path": step, "material": steel,
                                                "fixtures": study["fixtures"],
                                                "mesh_order": 2, "fineness": 0.3},
                                      "n_modes": 3})
    first = cb.call("prof_phases", {"reset": True})

    # Warm-cache re-run: same study content + options -> the mesher must not run again.
    rerun = cb.call("sim_run_static", {"study": study})
    second = cb.call("prof_phases", {})

    status = cb.call("prof_status", {})
    report = cb.call("prof_report", {})

    cb.call("doc_close", {"doc_id": doc})
    try:
        os.unlink(step)
    except OSError:
        pass

    _emit({
        "static_ok": bool(static.get("ok")), "static_error": static.get("error"),
        "modal_ok": bool(modal.get("ok")), "modal_error": modal.get("error"),
        "rerun_ok": bool(rerun.get("ok")),
        "first": first, "second": second,
        "status_has_phase_studies": "phase_studies" in status,
        "report_has_phases": "phases" in report,
    })


# ---------------------------------------------------------------------------
# Pytest side
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _so_candidates() -> list:
    """Candidate .so paths, most-preferred first (env wins). Probes this checkout,
    then the main checkout behind a `git worktree` (untracked build dirs don't
    travel into worktrees) — same trick as test_fea_live_bridge.py."""
    roots = [_repo_root()]
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, cwd=str(_repo_root()), timeout=10,
        ).stdout.strip()
        if common:
            main_root = Path(common).parent
            if main_root not in roots:
                roots.append(main_root)
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    env = os.environ.get("CHUDWARE_LIBCHUDWARE")
    if env and Path(env).exists():
        out.append(Path(env))
    for root in roots:
        for rel in ("core/build-prof/bin/libchudware.so",
                    "core/build-occt-linux/bin/libchudware.so"):
            p = root / rel
            if p.exists() and p not in out:
                out.append(p)
    return out


def _run_driver(so: Path | None) -> dict:
    env = dict(os.environ)
    env["CHUDWARE_ENGINE_DAEMON"] = "0"
    if so is not None:
        env["CHUDWARE_LIBCHUDWARE"] = str(so)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", __file__, "--driver"],
        capture_output=True, text=True, timeout=600, env=env, cwd=str(_repo_root()),
    )
    for line in proc.stdout.splitlines():
        if line.startswith("PHASES_RESULT "):
            return json.loads(line[len("PHASES_RESULT "):])
    raise AssertionError(
        f"driver produced no result (rc={proc.returncode})\n"
        f"stdout:\n{proc.stdout[-2000:]}\nstderr:\n{proc.stderr[-2000:]}")


def _phase_names(study: dict) -> list:
    return [p["name"] for p in study.get("phases", [])]


def _find(report: dict, domain: str) -> dict | None:
    hits = [s for s in report.get("studies", []) if s.get("domain") == domain]
    return hits[-1] if hits else None


def test_prof_phases_live() -> None:
    candidates = _so_candidates()
    if not candidates:
        pytest.skip("no libchudware.so candidate found (no OCCT build in this checkout)")
    reasons = []
    result = None
    for so in candidates:
        r = _run_driver(so)
        if "skipped" in r:
            reasons.append(r["skipped"])
            continue
        result = r
        break
    if result is None:
        pytest.skip("no candidate .so exposes the phase timers: " + "; ".join(reasons))

    assert result["static_ok"], result.get("static_error")
    assert result["modal_ok"], result.get("modal_error")
    assert result["rerun_ok"]
    assert result["status_has_phase_studies"]
    assert result["report_has_phases"]

    first = result["first"]
    assert first.get("ok") is True
    assert first.get("completed", 0) >= 3  # mesh_tet + fea_static + fea_modal at least

    fea = _find(first, "fea_static")
    assert fea is not None, f"studies: {[s['domain'] for s in first['studies']]}"
    names = _phase_names(fea)
    for want in ("mesh", "setup", "solve", "post"):
        assert want in names, f"missing '{want}' in {names}"
    assert sum(1 for n in names if n.startswith("solve.")) >= 3, names
    top_ms = sum(p["ms"] for p in fea["phases"]
                 if p["name"] in ("mesh", "setup", "solve", "post"))
    assert fea["total_ms"] > 0
    # The four top-level phases tile the study wall time (loose lower bound: shared box).
    assert 0.5 <= top_ms / fea["total_ms"] <= 1.01

    mesh = _find(first, "mesh_tet")
    assert mesh is not None
    mnames = _phase_names(mesh)
    for want in ("load_step", "generate", "extract"):
        assert want in mnames, mnames

    modal = _find(first, "fea_modal")
    assert modal is not None
    assert {"mesh", "solve"} <= set(_phase_names(modal))

    # Warm cache: the re-run must not have re-meshed (guards the overlay
    # mesh_step_cached fix — a regression here re-doubles every static RPC).
    second = result["second"]
    n_mesh = sum(1 for s in second.get("studies", []) if s.get("domain") == "mesh_tet")
    assert n_mesh == 0, f"expected no mesh_tet study on a warm cache, got {n_mesh}"


if __name__ == "__main__":
    if "--driver" in sys.argv:
        sys.path.insert(0, str(_repo_root()))
        _driver()
    else:
        raise SystemExit(pytest.main([__file__, "-v"]))
