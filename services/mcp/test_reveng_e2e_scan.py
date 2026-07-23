"""RE-24 — end-to-end reverse-engineering scan-pipeline test on the upgraded bracket fixture.

Walks the honest scan pipeline (contracts/reveng.md §8):

    reveng_start_session -> reveng_add_images -> reveng_reconstruct (RE-17 DAG launcher)
    -> scale (RE-05 ArUco solver) -> reveng_build_parametric

**Provisioning-aware.**  pycolmap / OpenMVS are NOT installed in this environment, so real
reconstruction returns the honest ``backend_not_provisioned`` envelope.  The test therefore
asserts two things that *do* hold here:

1. The pipeline runs and **degrades honestly** — reconstruct / build_parametric return
   ``backend_not_provisioned`` (never a traceback), and a mission started with backends
   absent produces a clear *partial* run with stage events (not a hang).

2. The upgraded fixture is genuinely **photogrammetry- and scale-ready** — a real COLMAP
   text sparse model is synthesised from the fixture's own recorded intrinsics + poses, and
   RE-05's ``solve_scale_aruco`` is run *for real* on the rendered images: the ArUco marker
   is detected in >=2 views, its 4 corners are triangulated into the reconstruction frame,
   and the recovered metric scale (``mm_per_unit`` ~ 1.0, since the fixture is authored in mm)
   and recovered marker edge length match the known 40 mm size within tolerance.

When the backends ARE provisioned (env has sparse+dense capability), the ``@requires_backends``
tests assert the full-reconstruction numbers instead: scale residual <1 %, match_pct >= 80,
and hole dimensions within 2 % of ground truth.  Those are skipped in this env by design; the
degradation + real-ArUco path is the part that PASSES here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from tools.reveng.gen_bracket_fixture import build_fixture  # noqa: E402

# RE-05 solver + provisioning probe
from services.vision.scale_solver import solve_scale_aruco  # noqa: E402
from services.vision.fiducial import HAVE_CV2  # noqa: E402
from services.vision.reveng_provision import reveng_status  # noqa: E402


# ---------------------------------------------------------------------------
# Capability gating
# ---------------------------------------------------------------------------

def _caps() -> dict:
    try:
        return (reveng_status() or {}).get("capabilities", {})
    except Exception:  # noqa: BLE001
        return {}


_CAPS = _caps()
_HAS_RECON = bool(_CAPS.get("sparse") or _CAPS.get("dense"))
_HAS_DENSE = bool(_CAPS.get("dense"))

requires_backends = pytest.mark.skipif(
    not _HAS_RECON,
    reason="reconstruction backend (colmap/openmvs) not provisioned — full-recon asserts skipped",
)
requires_cv2 = pytest.mark.skipif(not HAVE_CV2, reason="cv2 not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory):
    """Build the upgraded (perspective + real-ArUco) bracket fixture once per module."""
    d = tmp_path_factory.mktemp("bracket_fixture")
    build_fixture(d)
    return d


@pytest.fixture(scope="module")
def ground_truth(fixture_dir):
    return json.loads((fixture_dir / "ground_truth.json").read_text())


@pytest.fixture(scope="module")
def camera_poses(fixture_dir):
    return json.loads((fixture_dir / "camera_poses.json").read_text())


@pytest.fixture()
def reveng_env(tmp_path, monkeypatch):
    """Isolate the reveng session + DAG-run stores under tmp so the test is hermetic."""
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(tmp_path / "reveng"))
    monkeypatch.setenv("CHUDWARE_REVENG_RUNS_DIR", str(tmp_path / "runs"))
    # make_reveng_emit's EventJournal falls back to XDG cache; redirect it under tmp too
    # so the aicad.reveng_events/1 records are captured hermetically.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    # Import inside the env so the session-store helpers pick up CHUDWARE_REVENG_DIR.
    from services.mcp import reveng_tools as rt
    return rt


# A tiny recording MCP shim: reveng_tools registers @mcp.tool() functions; we capture
# them by name so we can call the real tool bodies without a live FastMCP server.
class _CaptureMCP:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture()
def tools(reveng_env):
    mcp = _CaptureMCP()
    reveng_env.register_reveng_tools(mcp)
    return mcp.tools


def _call(tools, _tool_name, **kw) -> dict:
    return json.loads(tools[_tool_name](**kw))


# ---------------------------------------------------------------------------
# COLMAP sparse-model synthesis from the fixture's recorded poses (RE-05 input)
# ---------------------------------------------------------------------------

def _synth_colmap_sparse(sparse_dir: Path, poses: list) -> None:
    """Write a COLMAP text sparse model (cameras.txt + images.txt) from fixture poses.

    The fixture records intrinsics (fx,fy,cx,cy), world->cam quaternion (w,x,y,z) and
    translation per view in the *same* convention scale_solver.read_colmap_sparse expects
    (COLMAP PINHOLE + quat/t).  This is exactly the sparse model a real COLMAP run would
    produce for these images — RE-05 then triangulates the marker for real.
    """
    sparse_dir.mkdir(parents=True, exist_ok=True)
    intr = poses[0]["intrinsics"]
    (sparse_dir / "cameras.txt").write_text(
        "# Camera list\n"
        f"1 PINHOLE {poses[0]['image_width_px']} {poses[0]['image_height_px']} "
        f"{intr['fx']} {intr['fy']} {intr['cx']} {intr['cy']}\n"
    )
    lines = ["# Image list\n"]
    for p in poses:
        q = p["quaternion_wxyz"]
        t = p["t_world_to_cam"]
        lines.append(
            f"{p['index'] + 1} {q[0]} {q[1]} {q[2]} {q[3]} "
            f"{t[0]} {t[1]} {t[2]} 1 {p['render_file']}\n"
        )
        # Non-empty POINTS2D placeholder line — read_colmap_sparse parses images.txt as
        # alternating pose/points2d lines; a blank line would break the alternation.
        lines.append("0.0 0.0 -1\n")
    (sparse_dir / "images.txt").write_text("".join(lines))


# ===========================================================================
# PART B.1 — pipeline runs + degrades HONESTLY (passes in THIS env)
# ===========================================================================

def test_session_and_capture(tools, fixture_dir, ground_truth):
    """start_session -> add_images ingests every fixture render."""
    started = _call(tools, "reveng_start_session", name="bracket E2E")
    assert started["ok"] and started["session_id"].startswith("re_")
    sid = started["session_id"]

    added = _call(tools, "reveng_add_images", session_id=sid, dir=str(fixture_dir))
    assert added["ok"], added
    # Directory ingest copies every image: the N renders + the standalone aruco_mat.png.
    assert added["added"] == ground_truth["render_count"] + 1, added
    assert added["session"]["stages"]["capture"] == "done"


def test_reconstruct_degrades_honestly(tools, fixture_dir):
    """reveng_reconstruct returns the honest backend_not_provisioned envelope here."""
    sid = _call(tools, "reveng_start_session", name="recon")["session_id"]
    _call(tools, "reveng_add_images", session_id=sid, dir=str(fixture_dir))
    res = _call(tools, "reveng_reconstruct", session_id=sid, quality="normal")
    if _HAS_RECON:
        assert res["ok"], res
        assert "run_id" in res
    else:
        assert res["ok"] is False
        assert res["reason"] == "backend_not_provisioned"
        assert res.get("hint")  # actionable, not a traceback


def test_build_parametric_degrades_honestly(tools, fixture_dir):
    """reveng_build_parametric degrades honestly when no dense/mesh backend."""
    sid = _call(tools, "reveng_start_session", name="param")["session_id"]
    _call(tools, "reveng_add_images", session_id=sid, dir=str(fixture_dir))
    res = _call(tools, "reveng_build_parametric", session_id=sid)
    if _HAS_DENSE:
        assert res["ok"], res
    else:
        assert res["ok"] is False
        assert res["reason"] == "backend_not_provisioned"


def test_mission_partial_run_with_stage_events(reveng_env, tools, fixture_dir):
    """A mission started with backends absent produces a clear PARTIAL run + stage events,
    not a hang.  This drives RE-17's DAG launcher directly (bypassing the provisioning
    gate on the tool) to prove the executor degrades honestly and emits reveng events."""
    sid = _call(tools, "reveng_start_session", name="mission")["session_id"]
    _call(tools, "reveng_add_images", session_id=sid, dir=str(fixture_dir))

    result = reveng_env._launch_reveng_mission(
        sid, "reveng_scan", reveng_env._SCAN_NODE_STAGE
    )
    assert result["session_id"] == sid
    assert result["template"] == "reveng_scan"
    # The run completed the walk (no hang) and reached a terminal/partial state.
    assert "status" in result
    assert result.get("run_id")

    # Stage events were journaled as aicad.reveng_events/1 (GUI stepper drain source).
    # The executor writes under CHUDWARE_REVENG_RUNS_DIR; make_reveng_emit's journal falls
    # back to the XDG cache runs dir — both are redirected under tmp, so scan both.
    search_roots = [
        Path(os.environ["CHUDWARE_REVENG_RUNS_DIR"]),
        Path(os.environ["XDG_DATA_HOME"]) / "Chudware" / "cache" / "runs",
    ]
    events_files = [f for root in search_roots if root.exists()
                    for f in root.rglob("events.jsonl")]
    assert events_files, "no events.jsonl written for the run"
    stages_seen = set()
    for ef in events_files:
        for line in ef.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            payload = rec.get("payload", rec)
            if payload.get("schema") == "aicad.reveng_events/1":
                stages_seen.add(payload.get("stage"))
    assert stages_seen, f"no aicad.reveng_events/1 stage events emitted (files={events_files})"


# ===========================================================================
# PART B.2 — fixture is photogrammetry/scale READY: RE-05 solver runs for real
# ===========================================================================

@requires_cv2
def test_aruco_detected_in_multiple_views(fixture_dir, ground_truth):
    """The real ArUco marker is detectable in >=2 rendered views (photogrammetry-ready)."""
    import cv2  # type: ignore

    m = ground_truth["marker"]
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, m["dict_name"]))
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())

    n_detected = 0
    for fname in ground_truth["render_files"]:
        img = cv2.imread(str(fixture_dir / fname), cv2.IMREAD_GRAYSCALE)
        assert img is not None, fname
        corners, ids, _ = detector.detectMarkers(img)
        if ids is not None and m["marker_id"] in ids.flatten():
            n_detected += 1
    assert n_detected >= 2, (
        f"marker id={m['marker_id']} detected in only {n_detected} views — "
        "need >=2 for triangulation; the fixture is not scale-solvable"
    )


@requires_cv2
def test_re05_scale_solver_recovers_marker_size(fixture_dir, ground_truth, camera_poses, tmp_path):
    """RE-05 solve_scale_aruco recovers the known marker size + metric scale for REAL.

    Synthesises a COLMAP sparse model from the fixture poses, then runs the real RE-05
    solver on the rendered images: detect -> triangulate 4 marker corners -> edge lengths.
    Because the fixture is authored in millimetres, mm_per_unit must be ~1.0 and the
    recovered mean marker edge must match the known marker_len_mm within tolerance.
    """
    m = ground_truth["marker"]
    known_mm = m["marker_len_mm"]

    sparse_dir = tmp_path / "sparse"
    _synth_colmap_sparse(sparse_dir, camera_poses)

    res = solve_scale_aruco(
        sparse_dir=str(sparse_dir),
        images_dir=str(fixture_dir),
        marker_len_mm=known_mm,
        dict_name=m["dict_name"],
        marker_id=m["marker_id"],
    )

    # Recovered marker edge length in reconstruction units == mm here (unit scene).
    recovered_edge_mm = res.detail["mean_edge_units"]

    # Metric scale ~ 1.0 (world authored in mm), well within 2 %.
    assert abs(res.mm_per_unit - 1.0) < 0.02, (
        f"mm_per_unit={res.mm_per_unit:.4f} not within 2% of 1.0"
    )
    # Recovered marker size matches known within 2 %.
    err_pct = 100.0 * abs(recovered_edge_mm - known_mm) / known_mm
    assert err_pct < 2.0, (
        f"recovered marker edge {recovered_edge_mm:.3f} mm vs known {known_mm} mm "
        f"({err_pct:.2f}% off)"
    )
    # Triangulation used real multi-view geometry + reported an honest small residual.
    assert res.n_views >= 2
    assert res.residual_pct < 5.0

    # Surface the recovered vs known number in the test log (per acceptance).
    print(
        f"\n[RE-24] RE-05 recovered marker edge = {recovered_edge_mm:.3f} mm "
        f"(known {known_mm} mm, {err_pct:.2f}% off); mm_per_unit={res.mm_per_unit:.4f}, "
        f"residual={res.residual_pct:.3f}%, n_views={res.n_views}"
    )


# ===========================================================================
# PART B.3 — full-reconstruction numbers (asserted only WHEN provisioned)
# ===========================================================================

@requires_backends
def test_full_reconstruction_numbers(tools, fixture_dir, ground_truth):
    """When backends ARE provisioned: scale residual <1%, match_pct>=80, holes within 2%.

    Skipped in this env (no colmap/openmvs). Written so a provisioned CI asserts the real
    end-to-end numbers off the same fixture + pipeline tools.
    """
    sid = _call(tools, "reveng_start_session", name="full")["session_id"]
    _call(tools, "reveng_add_images", session_id=sid, dir=str(fixture_dir))

    recon = _call(tools, "reveng_reconstruct", session_id=sid, quality="high")
    assert recon["ok"], recon

    build = _call(tools, "reveng_build_parametric", session_id=sid)
    assert build["ok"], build

    result = _call(tools, "describe_reveng_result", session_id=sid)
    assert result["ok"], result
    r = result["result"]

    # Scale residual under 1 %.
    session = _call(tools, "get_reveng_session", session_id=sid)["session"]
    assert session["scale"]["residual_pct"] is not None
    assert session["scale"]["residual_pct"] < 1.0

    # Match percentage of the fitted parametric model >= 80.
    assert r["match_pct"] is not None and r["match_pct"] >= 80.0

    # Hole dimensions within 2 % of ground truth.
    gt_holes = {h["id"]: h for h in ground_truth["holes"]}
    for fitted in r.get("holes", []):
        gt = gt_holes[fitted["id"]]
        err = abs(fitted["diameter"] - gt["diameter"]) / gt["diameter"]
        assert err < 0.02, f"hole {fitted['id']} Ø off by {err * 100:.1f}%"
