"""Tests for RE-14: fitted-primitive HINTS on reverse_engineer (recon_tools.py).

Two layers of proof (per the RE-14 acceptance):

  1. PURE-PYTHON, engine-free — runs everywhere. Exercises `_cylinder_boss_specs`, the
     side-effect-free helper that turns an `aicad.reveng_fits/1` doc into additive
     cylinder-boss candidates. Proves: None/empty hints are a no-op; a convex cylinder fit
     that the deterministic passes could NOT recover becomes a boss spec; a fit already
     coaxial with a recovered feature (hole/turned base) is dropped as a duplicate;
     confidence and axis gates fire.

  2. ENGINE-BACKED — skips cleanly when libaicad isn't staged (mirrors
     test_recon_tools.py's skipif). A plate with a large convex Z cylinder boss: the boss
     radius > pass-5's fillet bound and it is not a bore, so every frozen pass leaves it
     `unclaimed`. Reverse-engineered WITHOUT hints it stays unrecovered; WITH the fit hint
     the additive boss batch recovers it, so hinted match_pct >= unhinted (by construction:
     hints route through the same eval_compare keep-or-revert gate).

Run:  python3 -m pytest services/mcp/test_recon_hints.py -q
"""
from __future__ import annotations

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _unshadow_mcp_sdk() -> None:
    # Same repair as test_recon_tools.py: pytest's package import shadows the `mcp` SDK
    # with this directory. Load the real SDK back as `mcp`.
    shadow = sys.modules.get("mcp")
    sfile = getattr(shadow, "__file__", None)
    if shadow is None or not sfile or os.path.dirname(os.path.realpath(sfile)) != _HERE:
        return
    for p in list(sys.path):
        init = os.path.join(os.path.abspath(p or os.getcwd()), "mcp", "__init__.py")
        if os.path.isfile(init) and os.path.dirname(os.path.realpath(init)) != _HERE:
            spec = importlib.util.spec_from_file_location(
                "mcp", init, submodule_search_locations=[os.path.dirname(init), _HERE])
            module = importlib.util.module_from_spec(spec)
            sys.modules["mcp"] = module
            spec.loader.exec_module(module)
            return
    raise ImportError("could not locate the real `mcp` SDK to un-shadow services/mcp")


_unshadow_mcp_sdk()

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

from mcp_server import core_bridge as cb  # noqa: E402
from mcp_server.recon_tools import _cylinder_boss_specs  # noqa: E402


# ---------------------------------------------------------------- pure-Python (always runs)


def _fits(*fits: dict) -> dict:
    return {"schema": "aicad.reveng_fits/1", "session_id": "re_test", "fits": list(fits)}


def _cyl(radius=8.0, height=12.0, axis=(0, 0, 1), origin=(0, 0, 0), conf=0.9):
    return {"kind": "cylinder", "confidence": conf,
            "params_mm": {"radius": radius, "height": height,
                          "axis": list(axis), "origin": list(origin)},
            "source": "ransac", "face_ids": ["f1"]}


def test_no_hints_is_a_noop():
    assert _cylinder_boss_specs(None, []) == []
    assert _cylinder_boss_specs({}, []) == []
    assert _cylinder_boss_specs({"schema": "aicad.reveng_fits/1", "fits": []}, []) == []


def test_unrecovered_cylinder_becomes_a_boss_spec():
    specs = _cylinder_boss_specs(_fits(_cyl(radius=8.0, height=12.0)), [])
    assert len(specs) == 1
    s = specs[0]
    assert abs(s["radius"] - 8.0) < 1e-9 and abs(s["height"] - 12.0) < 1e-9
    assert list(s["axis"]) == [0.0, 0.0, 1.0]
    assert list(s["origin"]) == [0.0, 0.0, 0.0]


def test_only_cylinder_fits_are_used():
    box = {"kind": "box", "confidence": 0.9,
           "params_mm": {"dx": 40, "dy": 30, "dz": 10, "origin": [0, 0, 0]}}
    plane = {"kind": "plane", "confidence": 0.9,
             "params_mm": {"normal": [0, 0, 1], "origin": [0, 0, 0]}}
    assert _cylinder_boss_specs(_fits(box, plane), []) == []


def test_low_confidence_fit_is_dropped():
    assert _cylinder_boss_specs(_fits(_cyl(conf=0.2)), []) == []


def test_degenerate_fit_is_dropped():
    assert _cylinder_boss_specs(_fits(_cyl(radius=0.0)), []) == []
    assert _cylinder_boss_specs(_fits(_cyl(height=0.0)), []) == []


def test_fit_coaxial_with_a_recovered_feature_is_a_duplicate():
    # A hole/turned base the passes already own at axis Z through the origin: a coaxial,
    # on-axis cylinder fit describes the SAME geometry -> dropped so we never double-build.
    claimed = [([0.0, 0.0, 1.0], [0.0, 0.0, 0.0])]
    assert _cylinder_boss_specs(_fits(_cyl(axis=(0, 0, 1), origin=(0, 0, 0))), claimed) == []
    # ...but a cylinder OFF that axis (a boss elsewhere on the part) is still kept.
    off = _cylinder_boss_specs(_fits(_cyl(axis=(0, 0, 1), origin=(30, 0, 0))), claimed)
    assert len(off) == 1


# ---------------------------------------------------------------- engine-backed (skips clean)
# NOTE: skipif is applied to the engine-backed test ONLY (via a decorator) — the pure-Python
# tests above must always run, so there is deliberately no module-level pytestmark.

_needs_engine = pytest.mark.skipif(
    not cb.available(),
    reason="libaicad not staged (build the C++ core); engine-backed hint test unavailable",
)


def _call(name: str, args: dict):
    from mcp_server import server
    content, structured = asyncio.run(server.mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    parsed = json.loads(text) if isinstance(text, str) else None
    return parsed, content


def _boss_plate_step() -> str:
    """A plate + a big convex Z cylinder boss that falls into `unclaimed` (r above the
    fillet bound, not a bore). Returns an exported STEP path (caller unlinks)."""
    r, _ = _call("new_part", {"name": "re_boss_plate"})
    doc = r["doc_id"]
    ops = [
        cb.feat("box", "Plate",
                {"length": cb.dim(80), "width": cb.dim(60), "height": cb.dim(10)}),
        # Boss centered on the plate top: base at z=+5 (plate half-height), extends up 15.
        # r13 > pass-5's fillet bound (0.2*median(80,60,10)=12) and not a bore, so every
        # frozen pass leaves this boss in `unclaimed` — the gap the hint fills.
        cb.feat("cylinder", "Boss",
                {"radius": cb.dim(13), "height": cb.dim(15), "mode": "boss",
                 "at": [0.0, 0.0, 12.5]}),
    ]
    rb, _ = _call("add_features", {"ops": ops, "doc_id": doc})
    assert rb["ok"], rb
    ex, _ = _call("export_part", {"fmt": "step", "doc_id": doc})
    assert ex["ok"], ex
    return ex["path"]


@_needs_engine
def test_hinted_match_pct_ge_unhinted_on_a_cylinder_boss():
    path = _boss_plate_step()
    try:
        unhinted, _ = _call("reverse_engineer", {"source": path})
        # The boss sits at z=+5..+20; fit origin = its base center, axis +Z.
        fits = {"schema": "aicad.reveng_fits/1", "session_id": "re_test",
                "fits": [{"kind": "cylinder", "confidence": 0.95,
                          "params_mm": {"radius": 13.0, "height": 15.0,
                                        "axis": [0, 0, 1], "origin": [0, 0, 0.0]},
                          "source": "ransac", "face_ids": []}]}
        hinted, _ = _call("reverse_engineer", {"source": path, "hints": fits})
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    assert unhinted["ok"] and hinted["ok"], (unhinted, hinted)
    mu, mh = unhinted["match_pct"], hinted["match_pct"]
    assert mu is not None and mh is not None, (mu, mh)
    # By construction (hints route through eval_compare keep-or-revert): hinted never worse.
    assert mh >= mu - 1e-6, (mh, mu)
    # And on this fixture the boss is real unrecovered volume, so the hint should WIN.
    # Measured on the staged libaicad: unhinted 85.77 -> hinted 95.26 with segment_fit's
    # REAL origin=[0,0,0] convention (boss base defaults to the origin plane, so along-axis
    # placement is approximate — it still recovers most of the protrusion volume). A base-
    # aligned origin would reach 100.0; the approximation is a known, reported limitation.
    hint_feats = [f for f in hinted["features"] if f.get("role") == "hint"]
    assert hint_feats, hinted["features"]
    assert mh > mu, (mh, mu, "hint boss did not improve the match")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        if not cb.available() and "hinted" in fn.__name__:
            print(f"[SKIP] {fn.__name__} (libaicad not staged)")
            continue
        try:
            fn()
            print(f"[PASS] {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {fn.__name__}: {e}")
    print(f"\n{'FAILED' if failed else 'OK'} ({failed} failed)")
    sys.exit(1 if failed else 0)
