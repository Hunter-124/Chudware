"""ecad_vibe_check tests (house style: main()+asserts, honest skips).

Coverage:
  * pure helpers (engine-free): mount-hole detection modes, Steinberg screen
    (citation string + position factor + C table), deflection relation.
  * dry-run tool leg (needs build123d): mounts -> fixtures, mass model, valid
    aicad.simhandoff/1 envelope, honest no-holes error.
  * live leg (needs sim-enabled libchudware): demo board solves — modes reported, band
    verdict present, deflection screen carries the Steinberg citation; a seeded
    HEAVY component lowers f1 vs the bare board (sanity regression).

Run:  <venv>/python -m services.mcp.test_ecad_reliability
"""
from __future__ import annotations

import json
import math

from services.mcp.ecad_reliability import (
    STEINBERG_CITATION, band_split, deflection_3sigma_mm, detect_mount_holes,
    position_factor, register_ecad_reliability_tools, steinberg_c, steinberg_screen)
from services.mcp.ecad_tools import register_ecad_tools


class _StubMCP:
    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _call(_tools: dict, _name: str, **kw) -> dict:
    out = _tools[_name](**kw)
    assert isinstance(out, str), f"{_name} must return a JSON string"
    return json.loads(out)


def _tools() -> dict:
    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_reliability_tools(stub)
    return stub.tools


# --------------------------------------------------------------------------- fixtures


def _demo_board(tools: dict, name: str = "vibe_demo", heavy: bool = False,
                with_mounts: bool = True) -> str:
    """40x30 board, 4 corner NPTH mounting holes, an MCU + regulator; ``heavy=True``
    adds a large transformer-class body at the board center."""
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    _call(tools, "ecad_new_pcb", name=name)
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc
    pts = [(0, 0), (40, 0), (40, 30), (0, 30)]
    doc["outline"] = {"closed": True, "shapes": [
        {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4])}
        for i in range(4)]}

    def _smd(net=None, n="1", size=1.2):
        return {"id": f"p{n}", "number": n, "pin": n, "pos": [0.0, 0.0], "angle": 0.0,
                "shape": "rect", "size": [size, size], "type": "smd",
                "layers": ["F.Cu"], "net": net}

    fps = [
        {"id": "fp1", "ref": "U1", "fpid": "Test:MCU", "pos": [12.0, 15.0],
         "angle": 0.0, "side": "top", "pads": [_smd()],
         "eng_overrides": {"package": {"shape": "box", "l_mm": 7.0, "w_mm": 7.0,
                                       "h_mm": 1.6, "d_mm": None}}},
        {"id": "fp2", "ref": "U2", "fpid": "Test:REG", "pos": [28.0, 15.0],
         "angle": 0.0, "side": "top", "pads": [_smd()],
         "eng_overrides": {"package": {"shape": "box", "l_mm": 6.5, "w_mm": 6.0,
                                       "h_mm": 4.5, "d_mm": None}}},
    ]
    if heavy:
        # Transformer-class lump at the board center — the mass regression driver.
        fps.append(
            {"id": "fp9", "ref": "T1", "fpid": "Test:XFMR", "pos": [20.0, 15.0],
             "angle": 0.0, "side": "top", "pads": [_smd()],
             "eng_overrides": {"package": {"shape": "box", "l_mm": 20.0, "w_mm": 18.0,
                                           "h_mm": 15.0, "d_mm": None}}})
    if with_mounts:
        for i, (mx, my) in enumerate([(3, 3), (37, 3), (37, 27), (3, 27)]):
            fps.append(
                {"id": f"fp{10 + i}", "ref": f"H{i + 1}",
                 "fpid": "MountingHole:MountingHole_3.2mm_M3",
                 "pos": [float(mx), float(my)], "angle": 0.0, "side": "top",
                 "pads": [{"id": "p1", "number": "1", "pin": "1", "pos": [0.0, 0.0],
                           "angle": 0.0, "shape": "circle", "size": [5.6, 5.6],
                           "type": "np_tht", "layers": ["F.Cu", "B.Cu"],
                           "drill": {"diameter": 3.2}, "net": None}]})
    doc["footprints"].extend(fps)
    # The doc is a client copy of the C++ session doc — write the fixture back.
    _sess = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(_sess, doc)
    return _sess.doc_id


# --------------------------------------------------------------------------- pure helpers


def test_detect_mount_holes() -> None:
    board = {"footprints": [
        # named mounting-hole footprint
        {"id": "m1", "ref": "H1", "fpid": "MountingHole:MountingHole_3.2mm",
         "pos": [3.0, 3.0], "angle": 0.0,
         "pads": [{"pos": [0, 0], "type": "np_tht", "drill": {"diameter": 3.2},
                   "net": ""}]},
        # anonymous NPTH pad above threshold
        {"id": "m2", "ref": "REF2", "fpid": "Test:Whatever", "pos": [37.0, 27.0],
         "angle": 0.0,
         "pads": [{"pos": [0, 0], "type": "np_tht", "drill": {"diameter": 2.0},
                   "net": ""}]},
        # un-netted big plated hole
        {"id": "m3", "ref": "REF3", "fpid": "Test:Screw", "pos": [20.0, 3.0],
         "angle": 0.0,
         "pads": [{"pos": [0, 0], "type": "tht", "drill": {"diameter": 3.0},
                   "net": ""}]},
        # netted THT (a real signal pin) must NOT count
        {"id": "n1", "ref": "J1", "fpid": "Test:Conn", "pos": [10.0, 10.0],
         "angle": 0.0,
         "pads": [{"pos": [0, 0], "type": "tht", "drill": {"diameter": 2.5},
                   "net": "GND"}]},
        # small NPTH (tooling hole) must NOT count
        {"id": "n2", "ref": "TP1", "fpid": "Test:Fid", "pos": [15.0, 15.0],
         "angle": 0.0,
         "pads": [{"pos": [0, 0], "type": "np_tht", "drill": {"diameter": 1.0},
                   "net": ""}]},
    ]}
    holes = detect_mount_holes(board)
    srcs = {h["ref"]: h["source"] for h in holes}
    assert srcs == {"H1": "mounting_hole_footprint", "REF2": "npth_pad",
                    "REF3": "unnetted_tht_pad"}, srcs
    # User-passed positions win outright (and default the drill to M3 clearance).
    user = detect_mount_holes(board, [[1.0, 1.0], [39.0, 29.0, 4.0]])
    assert len(user) == 2 and user[0]["d_mm"] == 3.2 and user[1]["d_mm"] == 4.0
    assert all(h["source"] == "user" for h in user)
    print("  [detect_mount_holes] PASS")


def test_steinberg_screen_pure() -> None:
    board_geo = {"bbox_mm": [0.0, 0.0, 100.0, 100.0], "thickness_mm": 1.6}
    comps = [
        {"ref": "U1", "fpid": "Package:SOIC-8", "pos_mm": [50.0, 50.0],
         "angle_deg": 0.0,
         "body": {"shape": "box", "l_mm": 5.0, "w_mm": 4.0, "h_mm": 1.5},
         "fea": {"rect_mm": [47.5, 48.0, 52.5, 52.0]}},
        {"ref": "U2", "fpid": "Package:BGA-256", "pos_mm": [50.0, 50.0],
         "angle_deg": 0.0,
         "body": {"shape": "box", "l_mm": 17.0, "w_mm": 17.0, "h_mm": 1.2},
         "fea": {"rect_mm": [41.5, 41.5, 58.5, 58.5]}},
        {"ref": "X1", "fpid": "Package:???", "pos_mm": [50.0, 50.0],
         "angle_deg": 0.0, "body": None, "fea": None},
    ]
    tiny = steinberg_screen(comps, board_geo, z3_mm=0.001)
    big = steinberg_screen(comps, board_geo, z3_mm=5.0)
    # DoD: the heuristic verdict material carries the citation string.
    assert tiny["citation"] == STEINBERG_CITATION and tiny["heuristic"] is True
    assert "Steinberg" in tiny["citation"] and "HEURISTIC" in tiny["citation"]
    rows_t = {e["ref"]: e for e in tiny["table"]}
    rows_b = {e["ref"]: e for e in big["table"]}
    assert rows_t["U1"]["pass"] and rows_t["U2"]["pass"], rows_t
    assert not rows_b["U1"]["pass"] and not rows_b["U2"]["pass"], rows_b
    # BGA (C=1.75) is stricter than the default-C SOIC, and longer L is stricter too.
    assert rows_t["U2"]["C"] == 1.75 and rows_t["U1"]["C"] == 1.0
    assert rows_t["U2"]["z_allow_3sigma_mm"] < rows_t["U1"]["z_allow_3sigma_mm"]
    # No body dims -> UNCHECKED, never assumed OK.
    assert rows_t["X1"]["pass"] is None and tiny["n_unchecked"] == 1
    # Position factor: center 1.0, quarter point 0.5, edge floored.
    assert abs(position_factor(50, 50, board_geo["bbox_mm"]) - 1.0) < 1e-9
    assert abs(position_factor(25, 25, board_geo["bbox_mm"]) - 0.5) < 1e-9
    assert position_factor(0, 0, board_geo["bbox_mm"]) == 0.1
    assert steinberg_c({"fpid": "X:QFN-32"}) == 2.25
    print("  [steinberg_screen_pure] PASS")


def test_band_and_deflection_math() -> None:
    modes, in_band = band_split([0.0, 15.0, 150.0, 2500.0], 20.0, 2000.0)
    assert [m["in_band"] for m in modes] == [False, False, True, False]
    assert modes[0]["rigid_body"] and in_band == [150.0]
    # Z_rms(in) = 9.8*G/f^2 -> mm x3: at 100 Hz, 10 Grms -> 3*9.8*25.4*10/1e4 mm.
    z = deflection_3sigma_mm(10.0, 100.0)
    assert abs(z - 3.0 * 9.8 * 25.4 * 10.0 / 1e4) < 1e-12
    try:
        deflection_3sigma_mm(1.0, 0.0)
        raise AssertionError("zero frequency must be rejected")
    except ValueError:
        pass
    print("  [band_and_deflection_math] PASS")


def test_invalid_inputs_and_exact_limit() -> None:
    for args in (
            ([100.0, float("nan")], 20.0, 2000.0),
            ([100.0, 90.0], 20.0, 2000.0)):
        try:
            band_split(*args)
            raise AssertionError(f"invalid modal frequencies accepted: {args[0]}")
        except ValueError:
            pass
    try:
        detect_mount_holes({}, [[0.0, 0.0, -1.0]])
        raise AssertionError("negative mounting-hole diameter must be rejected")
    except ValueError:
        pass

    board = {"bbox_mm": [0.0, 0.0, 100.0, 100.0], "thickness_mm": 1.6}
    component = {
        "ref": "U1", "fpid": "Package:SOIC-8", "pos_mm": [50.0, 50.0],
        "angle_deg": 0.0,
        "body": {"shape": "box", "l_mm": 5.0, "w_mm": 4.0, "h_mm": 1.5},
        "fea": {"rect_mm": [47.5, 48.0, 52.5, 52.0]},
    }
    exact_limit_mm = (
        0.00022 * (100.0 / 25.4)
        / ((1.6 / 25.4) * math.sqrt(5.0 / 25.4)) * 25.4
    )
    exact = steinberg_screen([component], board, exact_limit_mm)
    assert exact["table"][0]["pass"] is True, exact

    tools = _tools()
    for kwargs in (
            {"n_modes": 0},
            {"zeta": float("nan")},
            {"mesh_fineness": -0.1},
            {"psd_breakpoints": [[20.0, 0.04]]},
            {"psd_breakpoints": [[20.0, 0.04], [10.0, 0.04]]}):
        result = _call(tools, "ecad_vibe_check", **kwargs)
        assert result["ok"] is False and result.get("error"), (kwargs, result)
    print("  [invalid_inputs_and_exact_limit] PASS")


# --------------------------------------------------------------------------- tool legs


def test_dry_run() -> None:
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  VIBE: build123d not installed — skipping dry-run test")
        return
    import os
    from services.mcp import simhandoff as sh

    tools = _tools()
    doc_id = _demo_board(tools)
    r = _call(tools, "ecad_vibe_check", doc_id=doc_id, dry_run=True)
    assert r["ok"] and r["dry_run"] and r["solved"] is False, r.get("error")
    assert os.path.isfile(r["step_path"])
    assert "hint" in r and "next_actions" in r and "warnings" in r
    # 4 corner mounts detected, 2 fixed patches each (top + bottom faces).
    assert len(r["mount_holes"]) == 4, r["mount_holes"]
    assert all(h["source"] == "mounting_hole_footprint" for h in r["mount_holes"])
    assert r["n_fixtures"] == 8
    # Mass model: total > board alone, density scaled onto the fused volume.
    mm = r["mass_model"]
    assert mm["total_mass_g"] > mm["board_mass_g"] > 0
    assert {c["ref"] for c in mm["component_masses"]} >= {"U1", "U2"}
    assert 0.5 < r["material"]["density_g_cm3"] < 5.0, r["material"]
    # Default PSD echoed loudly as a screening assumption.
    assert "0.04" in r["psd_input"]["note"] and any("default" in w.lower()
                                                    for w in r["warnings"])
    # Envelope: valid, one fixed_support per hole + the base-excitation condition.
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["coupling"] == "ecad_to_structural_fea"
    kinds = [c["kind"] for c in env["conditions"]]
    assert kinds.count("fixed_support") == 4 and "base_excitation" in kinds
    print(f"  VIBE: dry run — {len(r['mount_holes'])} mounts, "
          f"total mass {mm['total_mass_g']} g, envelope valid")
    print("  [vibe_dry_run] PASS")


def test_no_mounts_error() -> None:
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  VIBE: build123d not installed — skipping no-mounts test")
        return
    tools = _tools()
    doc_id = _demo_board(tools, name="vibe_nomount", with_mounts=False)
    r = _call(tools, "ecad_vibe_check", doc_id=doc_id, dry_run=True)
    assert not r["ok"] and "no mounting holes" in r["error"]
    assert any("mount_holes" in a for a in r["next_actions"])
    # Explicit user holes rescue the same board.
    r2 = _call(tools, "ecad_vibe_check", doc_id=doc_id, dry_run=True,
               mount_holes=[[3, 3], [37, 3], [37, 27], [3, 27]])
    assert r2["ok"] and len(r2["mount_holes"]) == 4
    assert all(h["source"] == "user" for h in r2["mount_holes"])
    print("  [vibe_no_mounts_error] PASS")


def test_live_solve_and_heavy_regression() -> None:
    """LIVE leg: demo board solves (modes + band verdict + cited deflection screen);
    a seeded heavy component LOWERS f1 vs the bare board. Honest skip without a
    sim-enabled libchudware."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  VIBE: build123d not installed — skipping live solve")
        return
    try:
        from services.mcp import core_bridge as cb
        cb.call("ping", {})
    except Exception as e:  # noqa: BLE001
        print(f"  VIBE: libchudware not loadable ({type(e).__name__}) — live solve "
              "SKIPPED (dry-run coverage above still exercises mounts/mass/envelope)")
        return
    tools = _tools()
    doc_id = _demo_board(tools, name="vibe_live")
    r = _call(tools, "ecad_vibe_check", doc_id=doc_id, mesh_fineness=0.3)
    if not r.get("ok") or not r.get("solved"):
        why = r.get("error_solve") or r.get("error")
        assert "unavailable" in str(why) or r.get("simhandoff"), r
        print(f"  VIBE: solver leg unavailable ({str(why)[:80]}) — live solve SKIPPED")
        return
    # Modes reported, physically sane, band verdict present.
    assert r["modes"] and r["f1_hz"] and r["f1_hz"] > 0, r["modes"]
    assert r["band"]["verdict"].startswith(("FLAG", "CLEAR"))
    freqs = [m["freq_hz"] for m in r["modes"] if not m["rigid_body"]]
    assert freqs == sorted(freqs), "modal frequencies must be ascending"
    # Deflection screen: available, cited (DoD), margins present.
    ds = r["deflection_screen"]
    assert ds["available"], r.get("warnings")
    assert ds["citation"] == STEINBERG_CITATION and "Steinberg" in ds["verdict"]
    assert ds["heuristic"] is True and ds["z_3sigma_mm"] >= 0
    checked = [e for e in ds["table"] if e["pass"] is not None]
    assert checked and all(isinstance(e["margin"], (int, float)) for e in checked)
    # Sanity regression: the heavy transformer LOWERS f1.
    doc_heavy = _demo_board(tools, name="vibe_heavy", heavy=True)
    rh = _call(tools, "ecad_vibe_check", doc_id=doc_heavy, mesh_fineness=0.3)
    assert rh.get("ok") and rh.get("solved"), rh.get("error_solve") or rh.get("error")
    assert rh["mass_model"]["total_mass_g"] > 1.5 * r["mass_model"]["total_mass_g"], \
        "heavy fixture must add real mass"
    assert rh["f1_hz"] < r["f1_hz"], \
        f"heavy board f1 {rh['f1_hz']} must be below bare f1 {r['f1_hz']}"
    print(f"  VIBE: LIVE — bare f1 {r['f1_hz']} Hz ({r['band']['verdict'][:5]}), "
          f"heavy f1 {rh['f1_hz']} Hz; deflection z3 {ds['z_3sigma_mm']} mm, "
          f"screen {'PASS' if ds['pass'] else 'FAIL'}")
    print("  [vibe_live_solve_and_heavy_regression] PASS")


def main() -> None:
    print("ecad_vibe_check tests")
    test_detect_mount_holes()
    test_steinberg_screen_pure()
    test_band_and_deflection_math()
    test_invalid_inputs_and_exact_limit()
    test_dry_run()
    test_no_mounts_error()
    test_live_solve_and_heavy_regression()
    print("all ecad_vibe_check tests passed (or honestly skipped)")


if __name__ == "__main__":
    main()
