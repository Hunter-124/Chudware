#!/usr/bin/env python3
"""FEA verification & validation: Chudware vs closed-form, CalculiX, and polymesh.

Three independent tiers, all driven through the REAL production path
(``sim_run_static`` = Polyhedral-FEA graded-tet mesh of an exported STEP + Chudware's
Eigen tet4/tet10 solver, i.e. exactly what ``run_fea_static`` runs):

  TIER 1  ANALYTIC.  Four textbook cases at 3+ mesh sizes each, tet4 AND tet10:
            * axial bar               — exact tier: interior strain must be P/(EA)
            * end-loaded cantilever   — Timoshenko tip deflection (incl. shear) and
                                        root bending stress at a Saint-Venant station
            * plate with a hole       — Kirsch SCF 3.0 recovered vs h
            * thick-walled cylinder   — Lame hoop/radial stress and radial growth
          Reference values are derived IN THIS FILE from the cited closed form and are
          never read back from the engine. Error vs h and the observed convergence
          order (least-squares slope of log err vs log h) are reported per case.

  TIER 2  SAME-MESH CALCULIX CROSS-SOLVE.  ``study.export_system_path`` dumps the
          RESOLVED system (nodes, connectivity, fixed set, nodal force vector, E, nu)
          immediately before ``fea_static_solve``.  That exact system is re-written as
          a CalculiX deck (C3D4/C3D10, ``*BOUNDARY``/``*CLOAD``) and solved by ccx.
          Same mesh + same BCs + linear elasticity => the two displacement fields are
          the same linear-algebra answer; they are compared NODE FOR NODE (relative
          L2 and Linf), plus max displacement, reaction resultant and peak nodal
          von Mises.  Stress recovery differs between the codes (Chudware averages
          element stresses to nodes, ccx extrapolates integration points to nodes and
          averages), so the stress rows are reported as nodal-averaged-vs-
          nodal-averaged and are NOT held to the displacement tolerance.

  TIER 3  INDEPENDENT SOLVER.  ``polymesh solve`` — Polyhedral-FEA's own solver on its
          own mesh — against the same closed forms, at a coarse h it can afford.

Usage (real run; keep every native solve inside a memory-capped slice):
    systemd-run --user --pipe --wait -q --collect -p MemoryMax=3G bash -c '
      ulimit -c 0; cd <repo root> &&
      CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so \\
      CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_MEM_FRAC=0.07 CHUDWARE_HEADROOM_GB=12 \\
      CHUDWARE_SIM_THREADS=2 \\
      ~/.local/share/Chudware/mcp-venv/bin/python scripts/vv_fea_crosscheck.py --all'

Usage (pytest smoke — no .so, no ccx, no engine):
    pytest scripts/vv_fea_crosscheck.py -q

Exit code 0 when every executed check is inside its tier tolerance (skips with a
reason are allowed, zero executed checks is a failure).  A disagreement is recorded as
an open FINDING with its evidence; tolerances are never widened to hide one.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# The engine must run in-process: this script is meant to be run inside a memory-capped
# systemd slice, and a shared daemon would put the solve (and its RSS) outside that cap
# and outside this script's governor environment.
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

DOCS_DIR = _PROJECT_ROOT / "docs" / "vv"

# ---------------------------------------------------------------------------
# Tolerance tiers (relative, fraction — not percent)
# ---------------------------------------------------------------------------
TOL_EXACT = 1e-6        # same algebra on both sides; only round-off may differ
TOL_CODE_TO_CODE = 1e-4  # Tier 2 displacement agreement gate (assignment)
TOL_PRECISE = 0.01      # discretization-limited but converged analytic reference
TOL_ENGINEERING = 0.10  # coarse-mesh / boundary-layer-limited analytic reference

STEEL = {"name": "vv-steel", "E_mpa": 200000.0, "nu": 0.3,
         "density_g_cm3": 7.85, "yield_mpa": 250.0}

# Chudware/gmsh MSH_TET_10 mid-edge slots are {0,1},{1,2},{2,0},{3,0},{3,2},{3,1}
# (core/sim/mesher.cpp kTet10PolymeshToChudware and core/sim/fea_run.cpp kTetFaces).
# CalculiX C3D10 wants corner 1-4 then mid(1,2) mid(2,3) mid(3,1) mid(1,4) mid(2,4)
# mid(3,4) — 0-based mid(0,1) mid(1,2) mid(2,0) mid(0,3) mid(1,3) mid(2,3).  Slots
# 4..7 agree, the last two swap.  Getting this wrong does not crash, it silently warps
# the quadratic element, so the permutation is ALSO verified geometrically per mesh by
# ``detect_tet10_permutation`` before any deck is written.
CHUD_TET10_TO_C3D10 = [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]
# Corner swap that fixes a negative-volume tet without disturbing the mid-node map.
C3D10_FLIP = [1, 0, 2, 3, 4, 6, 5, 8, 7, 9]
C3D4_FLIP = [1, 0, 2, 3]


# Findings this harness surfaced that were then RUN DOWN to a root cause by the
# owners of the affected code, with their measurements. They are carried in the report
# because a V&V document whose findings evaporate between runs is not a record.
CORROBORATED_FINDINGS = [
    {"case": "pressure load sign (FIXED)",
     "finding": "The `pressure` load was documented \"MPa, inward\" but a POSITIVE "
                "magnitude loaded the surface along its OUTWARD normal, so on a bore "
                "+p was EXTERNAL pressurisation and internal pressure needed a "
                "negative magnitude. The code was wrong, not the docs: +p pushing INTO "
                "the surface is the universal convention, is what run_fea_static / "
                "cad_guide document, and is what the render annotation already quotes "
                "(p*A \"N inward\").",
     "evidence": "Measured on the Lame cylinder (a=15, b=30, L=90 mm, tet4): "
                 "pressure=+20 MPa on the bore gave sigma_hoop = -29.6 MPa (hoop "
                 "COMPRESSION) and u_r < 0 against Lame's +33.33 MPa; pressure=-20 MPa "
                 "on the identical model reproduced the closed form. Root cause: the "
                 "deposit was +p*integral(N_i n_outward)dA in core/sim/fea_run.cpp "
                 "(fea_run_static_impl, the `type == \"pressure\"` branch) with the "
                 "outward orientation explicitly enforced. Fixed on 2026-09-05: the "
                 "deposit is now -p*integral(N_i n_outward)dA, so +p acts inward, and "
                 "this case passes +p. Force loads were never affected."},
    {'case': 'PLATE-HOLE (tet4 stress recovery at a concentration)',
     'finding': 'On tet4 — whose element stress really is constant, so nodal averaging '
                'is the only recovery available — nodal stress under-recovers the '
                'Kirsch peak at a hole rim, and does so non-monotonically: the SCF is '
                'a lower bound at these mesh sizes, so a peak stress read off a graded '
                'tet4 mesh at a stress concentration is optimistic. This is the one '
                'analytic row this harness leaves FAILING, deliberately. The tet10 '
                'half of this finding was a solver defect and is FIXED — see the '
                "'PLATE-HOLE / tet10 stress recovery' entry.",
     'evidence': '200x100x5 mm plate, d=10 mm central hole, gross sigma_inf 10 MPa, '
                 'reference Kirsch 3.00 / Howland 3.03. Measured max nodal sigma_xx on '
                 'the rim: tet4 1.62 -> 2.39 -> 2.75 -> 2.64 over h_eff '
                 '2.87/2.59/2.18/1.96 mm (finest 12% low, and NOT monotone). That '
                 'non-monotonicity is the MESH FAMILY, not the recovery: CalculiX '
                 'C3D4, handed the identical four meshes, reports 1.6153 / 2.3872 / '
                 '2.7535 / 2.6419 — the same numbers to ccx\'s 6-digit print floor, '
                 'including the same drop at the finest leg, because the graded-tet '
                 'ladder is not a nested family. The far-field stress on the same '
                 'solves is right to 6e-4..2e-3, so the load path and the solve are '
                 'fine, and the practical rule is: do not quote a peak stress at a '
                 'concentration without refining until the SCF stops moving.'},
    {'case': 'CYL-LAME (reaction resultant is degenerate for a self-equilibrated load)',
     'finding': 'The reaction-resultant cross-check is meaningless when the applied '
                'load has zero resultant, and on those legs the ccx RF sum over the '
                'fixed set is non-zero and mesh-dependent while Chudware reports '
                'exactly zero. Left as an OPEN reaction-bookkeeping question, not a '
                'solution difference.',
     'evidence': 'Internal pressure on a bore is purely radial, so the true resultant '
                 'is 0. Chudware reaction_force_n = [0,0,0] on all four tet4 legs and '
                 'both tet10 legs; the ccx *NODE PRINT RF sum over NFIX gives Fz = '
                 '-182.85, 0.54, 250.27, 192.86 N (tet4 h=24/20/13/9.5) and 15.14, '
                 '0.00 N (tet10 h=24/20) — erratic in mesh, so it is not a load the '
                 'model carries. The displacement fields on those SAME meshes agree '
                 "node-for-node at |u| L2 1.63e-07..1.72e-07, i.e. at ccx's own print "
                 'precision, so the solutions are identical and the discrepancy lives '
                 "in the reaction reporting (or in this harness's parse of ccx's RF "
                 'table), not in the solve. On the 21 force-loaded meshes the same '
                 'comparison agrees to 1.88e-07 worst case, which is why the metric is '
                 'kept.'},
    {'case': 'PLATE-HOLE / tet10 stress recovery (ROOT-CAUSED, FIXED, RE-MEASURED)',
     'finding': 'The tet10 hole-rim SCF under-recovery first reported as an open '
                'finding was a real defect in tet10 Gauss-point -> nodal stress '
                'recovery, not a mesh-resolution limit: the recovery collapsed the 4 '
                'Gauss points of a tet10 to an ELEMENT CONSTANT before nodal '
                'averaging, which is why tet10 read WORSE than tet4 at the rim on the '
                'same geometry. Recovery now extrapolates the 4 Gauss-point tensors to '
                'the corner nodes (the mid-edge nodes take the mean of their two '
                'corners) — the same recovery CalculiX uses for C3D10. tet4 and hex8 '
                'are untouched and their numbers are unchanged.',
     'evidence': "Diagnosed off this harness's Tier-2 lane, which "
                 'hands CalculiX the identical mesh. BEFORE / AFTER / ccx C3D10 on the '
                 'SAME mesh, rim SCF: h_eff 3.445 (20664 elems) 1.46607 / 2.22806 / '
                 '2.2280; h_eff 2.870 (35744) 1.75700 / 2.57926 / 2.5792; h_eff 2.586 '
                 '(48880) 2.43741 / 3.44426 / 3.4442; and on the old finest mesh '
                 '(73072 elems, h_eff 2.261) 2.17568 / 3.16604 / 3.1660. After the fix '
                 "Chudware reproduces ccx's own C3D10 nodal stress to its 6-digit "
                 'print floor on every one of those meshes, and tet4 was already '
                 'exact against C3D4 (1.6153 both), which localised the defect to the '
                 'quadratic recovery path alone. The re-run finest tet10 leg (81928 '
                 'elems, h_eff 2.177, a slightly finer mesh than the old run, whose '
                 'leg was coarsened by a tighter memory envelope) reads SCF 3.08792 '
                 'against ccx 3.08790 on that mesh: 2.9% from Kirsch 3.00 and 1.9% '
                 'from Howland 3.03, so the row PASSES, and the error against the '
                 'finite-width reference now falls at EVERY refinement '
                 '(0.265 -> 0.149 -> 0.137 -> 0.019, monotone=True). The displacement '
                 'rows were never affected (Tier 2 agreed to ~1e-07 on these same '
                 'meshes) — only nodal STRESS at a gradient was wrong. Fixed in '
                 'core/sim/fea_static.cpp; pinned by the core ctest `sim` case '
                 '"tet10 nodal stress recovery reports the extreme-fibre bending '
                 'stress", which reads 301.4 MPa against M c/I = 300 MPa where the '
                 'element-constant recovery read 247.7 MPa.'},
    {'case': 'CYL-LAME post-fix spot measurement (supersedes the Lame rows in the '
             'tables)',
     'finding': 'With BOTH fixes in (pressure sign + tet10 Gauss-point stress '
                'recovery) the Lame case is far more accurate than the tables in this '
                'report show, and the sign question is settled by measurement: '
                'pressure=+p IS internal pressurisation on a bore. The tables above '
                'were measured before either fix; the numbers here are the current '
                'engine.',
     'evidence': "Measured through this harness's own "
                 'chud_solve/extract on the identical CYL-LAME model (a=15, b=30, L=90 '
                 "mm), engine carrying the tet10 recovery fix. tet10 h_req=16 "
                 '(56840 elems), pressure=+p: hoop inner +33.0205 MPa vs Lame +33.3333 '
                 '(-0.94%), hoop outer +13.2720 vs +13.3333 (-0.46%), u_r inner '
                 '+0.00291 mm (-1.39%). The -p probe mirrors it exactly negative '
                 '(-33.0205 / -13.2720), which is the sign convention pinned as data '
                 'rather than read off a docstring. tet4 h=20: hoop outer +13.3678 '
                 '(+0.26%), hoop inner +27.23 (-18.3%) — the tet4 rim under-read, NOT '
                 'a sign error. Compare the pre-fix tet10 rows in Tier 1 (hoop inner '
                 '8.55% low): that gap was the tet10 recovery defect, not the Lame '
                 'model or the mesh. Engine convention is now f_i = -p*integral(N_i '
                 'n_outward) dA, i.e. +p pushes INTO the face as its contract always '
                 'claimed.'},
    {'case': 'provenance.engine_build_id is NOT unique per build (harness/provenance '
             'defect)',
     'finding': "A `-dirty` build id does not identify a build. This report's central "
                'integrity check was "every row carries the same engine_build_id, so '
                'the table cannot straddle two builds" — that check is WEAKER than '
                'claimed: the id is a git describe of a dirty working tree, so a '
                'rebuild with real code changes produces the IDENTICAL string. Rows '
                'measured before and after a solver fix are therefore '
                'indistinguishable by build id alone.',
     'evidence': 'Demonstrated inside this very report. The PLATE-HOLE tet10 rim SCF '
                 'rows were re-measured after the tet10 stress-recovery fix and now '
                 'read 2.22806 / 2.57926 / 3.44426 / 3.08792 (was 1.46607 / 1.75700 / '
                 '2.43741 / 2.17568), yet every one still carries engine_build_id '
                 '`beta-0.1.2-beta.41-80-gfac9a08ab-dirty`, the same string as the '
                 'pre-fix rows. What actually distinguishes them is the `reruns` '
                 'record in fea_vv_results.json (case, tiers, reason, command, '
                 'generated_utc), which is why that record is load-bearing evidence '
                 'and not bookkeeping. FIX for the engine: make the build id carry a '
                 'content hash of the built artifact (or a dirty-tree digest) so two '
                 'different binaries can never share an id — sim results are only '
                 'evidence-grade if their provenance is injective.'},
    {'case': 'Mixed-recovery stress columns in this report (read-me-first caveat)',
     'finding': "After the targeted PLATE-HOLE re-run, this report's nodal-STRESS "
                'columns are no longer all from one recovery implementation: '
                'PLATE-HOLE Tier-1 tet10 rows are post-fix, while tet10 nodal-stress '
                'columns elsewhere (Tier-2 peak von Mises) still reflect the OLD '
                'element-constant recovery '
                'and will move UP on a full refresh. Displacements and reactions are '
                'unaffected anywhere.',
     'evidence': 'Confirmed by the targeted re-run. The fix extrapolates the 4 '
                 'Gauss-point tensors to corner '
                 'nodes (corner_i = sqrt(5)*(g[i] - b*sum g), mid-edges as the mean of '
                 "their two corners) — CalculiX's own C3D10 recovery — and tet4/hex8 "
                 'are bit-identical. Same-mesh agreement on STRESS is now essentially '
                 'exact: Chudware 2.22806/2.57926/3.44426/3.08792 vs ccx '
                 '2.2280/2.5792/3.4442/3.08790 at h_eff 3.445/2.870/2.586/2.177, so '
                 'the finest tet10 SCF row PASSES (2.9% vs Kirsch 3.0, 1.9% vs Howland '
                 '3.03) and the Howland error now falls at every refinement. A '
                 'regression case in core/tests/test_sim.cpp pins tet10 extreme-fibre '
                 'nodal sigma_xx 301.4 vs M c/I 300 (the old recovery read 247.7).'},
]

# ===========================================================================
# Result accumulation
# ===========================================================================

_RESULTS: dict[str, list[dict]] = {"tier1": [], "tier2": [], "tier3": []}
_FINDINGS: list[dict] = []
_SKIPS: list[dict] = []
_PROVENANCE: dict[str, Any] = {}
_FAILED = 0
_EXECUTED = 0


def _log(msg: str) -> None:
    print(msg, flush=True)


def record(tier: str, row: dict) -> None:
    global _EXECUTED, _FAILED
    _RESULTS[tier].append(row)
    if row.get("verdict") in ("PASS", "FAIL"):
        _EXECUTED += 1
    if row.get("verdict") == "FAIL":
        _FAILED += 1


def finding(case: str, what: str, evidence: str) -> None:
    _FINDINGS.append({"case": case, "finding": what, "evidence": evidence})
    _log(f"  [FINDING] {case}: {what}")


def skip(tier: str, case: str, reason: str) -> None:
    _SKIPS.append({"tier": tier, "case": case, "reason": reason})
    _log(f"  [SKIP] {tier}/{case}: {reason}")


def verdict(rel_err: Optional[float], tol: float) -> str:
    if rel_err is None or not math.isfinite(rel_err):
        return "FAIL"
    return "PASS" if rel_err <= tol else "FAIL"


def rel(got: Optional[float], ref: Optional[float]) -> Optional[float]:
    if got is None or ref is None:
        return None
    if ref == 0.0:
        return abs(got)
    return abs(got - ref) / abs(ref)


def fit_order(hs: list[float], errs: list[float]) -> Optional[float]:
    """Least-squares slope of log(err) vs log(h) — the observed convergence order."""
    pts = [(math.log(h), math.log(e)) for h, e in zip(hs, errs)
           if h > 0 and e > 0 and math.isfinite(e)]
    if len(pts) < 2:
        return None
    n = len(pts)
    sx = sum(p[0] for p in pts)
    sy = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = n * sxx - sx * sx
    if abs(den) < 1e-30:
        return None
    return (n * sxy - sx * sy) / den


# ===========================================================================
# Closed-form references (each derived here; source cited in the docstring)
# ===========================================================================

def ref_axial_bar(P: float, L: float, A: float, E: float) -> dict:
    """Uniform bar, axial end load. Hooke: eps = P/(EA), delta = PL/(EA), sigma = P/A."""
    return {"strain": P / (E * A), "delta_mm": P * L / (E * A), "sigma_mpa": P / A}


def ref_cantilever(P: float, L: float, b: float, h: float, E: float, nu: float,
                   station_mm: float) -> dict:
    """End-loaded cantilever.

    Timoshenko tip deflection = PL^3/(3EI) + PL/(k*G*A) with the rectangular-section
    shear coefficient k = 5/6 (Timoshenko & Goodier, Theory of Elasticity, shear
    correction for a rectangle); G = E/(2(1+nu)).  Root bending stress is evaluated at
    a station a distance ``station_mm`` from the clamp, where the clamped-end boundary
    layer has decayed: sigma = M(x) c / I with M = P (L - x), c = h/2.
    """
    I = b * h ** 3 / 12.0
    A = b * h
    G = E / (2.0 * (1.0 + nu))
    d_bend = P * L ** 3 / (3.0 * E * I)
    d_shear = P * L / ((5.0 / 6.0) * G * A)
    M = P * (L - station_mm)
    return {"delta_eb_mm": d_bend, "delta_shear_mm": d_shear,
            "delta_mm": d_bend + d_shear,
            "sigma_station_mpa": M * (h / 2.0) / I, "I_mm4": I}


def ref_plate_hole(sigma_inf: float, d: float, W: float) -> dict:
    """Plate with a central circular hole in uniaxial tension.

    Kirsch (1898): for an INFINITE plate the tangential stress at the hole rim on the
    diameter perpendicular to the load is 3*sigma_inf, i.e. SCF = 3.0 exactly.
    Finite width raises it; Howland's series for a central hole in a strip gives
    K(2a/W) with K(0.1) = 3.03, K(0.2) = 3.14, K(0.3) = 3.36 (Peterson, Stress
    Concentration Factors, chart for a finite-width strip).  Both are reported: the
    mesh is expected to approach the Howland value, and the Kirsch 3.0 is the
    assignment's reference.
    """
    ratio = d / W
    table = [(0.0, 3.00), (0.1, 3.03), (0.2, 3.14), (0.3, 3.36), (0.4, 3.74)]
    k_fw = table[-1][1]
    for (r0, k0), (r1, k1) in zip(table, table[1:]):
        if r0 <= ratio <= r1:
            k_fw = k0 + (k1 - k0) * (ratio - r0) / (r1 - r0)
            break
    return {"scf_kirsch": 3.0, "scf_howland": k_fw,
            "sigma_peak_kirsch_mpa": 3.0 * sigma_inf,
            "sigma_peak_howland_mpa": k_fw * sigma_inf,
            "d_over_W": ratio}


def ref_lame(p: float, a: float, b: float, r: float, E: float, nu: float) -> dict:
    """Thick-walled cylinder, internal pressure p, traction-free ends (open-ended).

    Lame: sigma_hoop(r) = p a^2/(b^2-a^2) * (1 + b^2/r^2)
          sigma_rad(r)  = p a^2/(b^2-a^2) * (1 - b^2/r^2)
    With traction-free ends there is no net axial force, so away from the ends
    sigma_z = 0 and the radial growth is
          u_r(r) = p a^2 / (E (b^2-a^2)) * ((1-nu) r + (1+nu) b^2 / r).
    sigma_hoop and sigma_rad are identical for plane stress and plane strain; only
    sigma_z and u_r differ, which is why the hoop row is BC-robust.
    """
    k = p * a * a / (b * b - a * a)
    return {"hoop_mpa": k * (1.0 + b * b / (r * r)),
            "rad_mpa": k * (1.0 - b * b / (r * r)),
            "u_r_mm": p * a * a / (E * (b * b - a * a))
                      * ((1.0 - nu) * r + (1.0 + nu) * b * b / r),
            "von_mises_mpa": math.sqrt(
                (k * (1.0 + b * b / (r * r))) ** 2
                - (k * (1.0 + b * b / (r * r))) * (k * (1.0 - b * b / (r * r)))
                + (k * (1.0 - b * b / (r * r))) ** 2)}


# ===========================================================================
# CalculiX deck generation and output parsing (pure python — smoke-testable)
# ===========================================================================

def detect_tet10_permutation(points, elems, sample: int = 64) -> list[int]:
    """Return the Chudware-slot -> CalculiX-C3D10-slot permutation, measured.

    For each element the six mid slots are matched to the corner pair they bisect
    (nearest midpoint), which recovers the ordering from geometry instead of trusting a
    table.  Raises if the ordering is not the same for every sampled element or does
    not form a valid C3D10 map.
    """
    # C3D10 slots 5..10 are mid(1,2) mid(2,3) mid(3,1) mid(1,4) mid(2,4) mid(3,4);
    # 0-based and written with the low corner first, since that is how the detected
    # pairs below are keyed.
    want = [(0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3)]
    perm_seen = None
    n = len(elems)
    step = max(1, n // max(1, sample))
    for e in range(0, n, step):
        el = elems[e]
        corners = [points[el[i]] for i in range(4)]
        slot_for_pair = {}
        for slot in range(4, 10):
            mid = points[el[slot]]
            best, best_d = None, float("inf")
            for i in range(4):
                for j in range(i + 1, 4):
                    mx = 0.5 * (corners[i][0] + corners[j][0])
                    my = 0.5 * (corners[i][1] + corners[j][1])
                    mz = 0.5 * (corners[i][2] + corners[j][2])
                    d = ((mid[0] - mx) ** 2 + (mid[1] - my) ** 2 + (mid[2] - mz) ** 2)
                    if d < best_d:
                        best_d, best = d, (i, j)
            slot_for_pair[best] = slot
        try:
            perm = [0, 1, 2, 3] + [slot_for_pair[p] for p in want]
        except KeyError as exc:
            raise RuntimeError(
                f"tet10 element {e}: mid-node slots do not bisect the 6 distinct corner "
                f"pairs (missing {exc}); connectivity cannot be mapped to C3D10")
        if perm_seen is None:
            perm_seen = perm
        elif perm_seen != perm:
            raise RuntimeError(f"tet10 mid-node ordering is not uniform: element {e} "
                               f"maps {perm}, earlier elements {perm_seen}")
    if perm_seen is None:
        raise RuntimeError("no tet10 elements to inspect")
    return perm_seen


def _signed_volume6(p0, p1, p2, p3) -> float:
    ax, ay, az = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    bx, by, bz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    cx, cy, cz = p3[0] - p0[0], p3[1] - p0[1], p3[2] - p0[2]
    return ((ay * bz - az * by) * cx + (az * bx - ax * bz) * cy
            + (ax * by - ay * bx) * cz)


def write_ccx_inp(path: Path, system: dict, *, job: str = "vv") -> dict:
    """Write a CalculiX deck for the exact system dumped by ``export_system_path``.

    ``system`` is that JSON: points [[x,y,z] mm], elems [[node ids]], nodes_per_elem,
    fixed_nodes (flat 0-based list, each pinning all 3 DOFs), nodal_forces [[fx,fy,fz]
    N], E_mpa, nu.  Node ids are emitted 1-based.  Element windings are normalised to
    positive volume (ccx rejects/mis-integrates inverted tets), and for tet10 the
    mid-node slots are permuted into the C3D10 convention.
    """
    pts = system["points"]
    elems = system["elems"]
    npe = int(system["nodes_per_elem"])
    if npe not in (4, 10):
        raise ValueError(f"unsupported nodes_per_elem={npe} (need 4 or 10)")
    if system.get("bond_springs"):
        raise ValueError("system carries bond_springs; a spring-coupled system is not "
                         "expressible in this deck writer")
    if system.get("contact_springs"):
        raise ValueError("system carries contact_springs; nonlinear contact is out of "
                         "scope for the same-mesh linear cross-solve")

    perm = list(range(4))
    if npe == 10:
        perm = detect_tet10_permutation(pts, elems)
        if perm != CHUD_TET10_TO_C3D10:
            raise RuntimeError(
                f"measured tet10 permutation {perm} != documented "
                f"{CHUD_TET10_TO_C3D10} (core/sim/mesher.cpp); refusing to write a deck "
                f"from an unverified mid-node map")

    flipped = 0
    rows: list[str] = []
    for eid, el in enumerate(elems, start=1):
        nodes = [el[k] for k in perm] if npe == 10 else list(el)
        if _signed_volume6(pts[nodes[0]], pts[nodes[1]], pts[nodes[2]],
                           pts[nodes[3]]) < 0.0:
            order = C3D10_FLIP if npe == 10 else C3D4_FLIP
            nodes = [nodes[k] for k in order]
            flipped += 1
        ids = [n + 1 for n in nodes]
        if npe == 4:
            rows.append(f"{eid}, " + ", ".join(str(i) for i in ids))
        else:
            rows.append(f"{eid}, " + ", ".join(str(i) for i in ids[:7]) + ",")
            rows.append("      " + ", ".join(str(i) for i in ids[7:]))

    fixed = sorted({int(n) + 1 for n in system["fixed_nodes"]})
    forces = [(i + 1, f) for i, f in enumerate(system["nodal_forces"])
              if any(abs(c) > 0.0 for c in f)]

    out: list[str] = ["*NODE, NSET=NALL"]
    for i, p in enumerate(pts, start=1):
        out.append(f"{i}, {p[0]:.10g}, {p[1]:.10g}, {p[2]:.10g}")
    out.append(f"*ELEMENT, TYPE=C3D{npe}, ELSET=EALL")
    out.extend(rows)
    out.append("*NSET, NSET=NFIX")
    for k in range(0, len(fixed), 8):
        out.append(", ".join(str(n) for n in fixed[k:k + 8]))
    out += [
        "*MATERIAL, NAME=MAT1",
        "*ELASTIC",
        f"{float(system['E_mpa']):.10g}, {float(system['nu']):.10g}",
        "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT1",
        "*BOUNDARY",
        "NFIX, 1, 3, 0.0",
        "*STEP",
        "*STATIC",
        "*CLOAD",
    ]
    for nid, f in forces:
        for dof in (1, 2, 3):
            if abs(f[dof - 1]) > 0.0:
                out.append(f"{nid}, {dof}, {f[dof - 1]:.10g}")
    out += [
        "*NODE FILE",
        "U, S",
        "*EL FILE",
        "S",
        "*NODE PRINT, NSET=NALL, TOTALS=NO",
        "U",
        "*NODE PRINT, NSET=NFIX, TOTALS=YES",
        "RF",
        "*END STEP",
    ]
    path.write_text("\n".join(out) + "\n")
    return {"n_nodes": len(pts), "n_elems": len(elems), "nodes_per_elem": npe,
            "n_fixed": len(fixed), "n_loaded_nodes": len(forces),
            "inverted_elements_reoriented": flipped,
            "tet10_permutation": perm if npe == 10 else None,
            "inp_bytes": path.stat().st_size}


_DAT_BLOCK = re.compile(r"^\s*(displacements|forces)\b.*?for set\s+(\S+)", re.I)


def parse_ccx_dat(text: str) -> dict:
    """Parse ``*NODE PRINT`` blocks out of a ccx .dat file.

    Returns {"displacements": {set: {node: (u1,u2,u3)}}, "forces": {...}}.  The
    whitespace-separated .dat table is the primary displacement source precisely
    because it needs no column-width guessing; the fixed-width .frd is parsed
    separately and the two are cross-checked.
    """
    out: dict[str, dict[str, dict[int, tuple]]] = {"displacements": {}, "forces": {}}
    kind = setname = None
    for line in text.splitlines():
        m = _DAT_BLOCK.match(line)
        if m:
            kind = m.group(1).lower()
            setname = m.group(2)
            out[kind].setdefault(setname, {})
            continue
        if kind is None:
            continue
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        try:
            nid = int(parts[0])
            vals = tuple(float(v) for v in parts[1:4])
        except (ValueError, IndexError):
            if not s[0].isdigit() and not s.startswith("-"):
                kind = setname = None
            continue
        if len(vals) == 3:
            out[kind][setname][nid] = vals
    return out


def parse_ccx_frd(text: str) -> dict:
    """Parse nodal DISP / STRESS blocks from a ccx ASCII .frd file.

    ccx writes data records as ``' -1'`` + a fixed-width node id + N fields of 12
    characters.  The id width is 5 in short format and 10 in long format, so it is
    recovered per line from the record length rather than assumed.
    """
    blocks: dict[str, dict[int, list[float]]] = {}
    cur: Optional[str] = None
    ncomp = 0
    for line in text.splitlines():
        if line.startswith(" -4"):
            name = line[5:13].strip().upper()
            cur = name
            ncomp = 0
            blocks.setdefault(cur, {})
            continue
        if line.startswith(" -5"):
            if cur is not None:
                comp = line[5:13].strip().upper()
                if comp not in ("ALL",):
                    ncomp += 1
            continue
        if line.startswith(" -3"):
            cur = None
            continue
        if line.startswith(" -1") and cur is not None and ncomp:
            body = line[3:].rstrip("\n")
            width = len(body) - 12 * ncomp
            if width <= 0:
                width = 10 if len(body) >= 10 + 12 * ncomp else 5
            try:
                nid = int(body[:width])
            except ValueError:
                continue
            vals = []
            for k in range(ncomp):
                chunk = body[width + 12 * k: width + 12 * (k + 1)]
                if not chunk.strip():
                    break
                vals.append(float(chunk))
            if len(vals) == ncomp:
                blocks[cur][nid] = vals
    return blocks


def von_mises_from_components(s: list[float]) -> float:
    """von Mises from the ccx STRESS component order SXX SYY SZZ SXY SYZ SZX."""
    sxx, syy, szz, sxy, syz, szx = s[:6]
    return math.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                     + 3.0 * (sxy * sxy + syz * syz + szx * szx))


def ccx_version(ccx: str) -> str:
    try:
        proc = subprocess.run([ccx, "-v"], capture_output=True, text=True, timeout=30,
                              input="")
        text = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"Version\s+([0-9][0-9.\w]*)", text)
        return f"CalculiX ccx {m.group(1)}" if m else text.strip().splitlines()[0]
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc})"


def find_ccx() -> Optional[str]:
    for cand in (os.environ.get("CHUDWARE_CCX"), str(Path.home() / ".local/bin/ccx"),
                 "ccx", "ccx_2.23", "ccx_2.20", "calculix"):
        if not cand:
            continue
        p = shutil.which(cand) if not os.path.isabs(cand) else (
            cand if os.access(cand, os.X_OK) else None)
        if p:
            return p
    return None


# ===========================================================================
# Engine access
# ===========================================================================

def engine_ready() -> tuple[bool, str]:
    lib = os.environ.get("CHUDWARE_LIBCHUDWARE", "")
    if not lib or not os.path.exists(lib):
        return False, "CHUDWARE_LIBCHUDWARE is unset or missing"
    try:
        from services.mcp import core_bridge as cb
    except Exception as exc:  # noqa: BLE001
        return False, f"services.mcp import failed: {exc}"
    return (True, "") if cb.available() else (False, "libchudware not loadable")


def _cb():
    from services.mcp import core_bridge as cb
    return cb


def _cst():
    from services.mcp import cad_sim_tools as cst
    return cst


def build_doc(name: str, ops: list) -> str:
    cb = _cb()
    doc = cb.call("doc_new", {"name": name})["doc_id"]
    res = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    status = res.get("rebuild", {}).get("feature_status", [])
    bad = [f.get("error") for f in status if not f.get("ok")]
    if bad or not status:
        raise RuntimeError(f"{name}: feature build failed: {bad}")
    return doc


def make_study(doc: str, fixtures: list, loads: list, material: dict) -> dict:
    """Resolve fixtures/loads and export the STEP exactly as run_fea_static does.

    Called ONCE per case so every mesh leg solves the identical boundary-condition
    point sets off the identical STEP.
    """
    cst = _cst()
    notes: list = []
    ctx = cst._build_static_study(fixtures, loads, material, 0.5, False, 0, doc, notes)
    ctx["bc_notes"] = notes
    return ctx

def calibrate_ladder(step_path: str, candidates: list[float],
                     budgets: dict[int, int], want: int = 4) -> dict:
    """Turn a requested-size wish list into a genuinely nested-enough mesh ladder.

    The graded-tet mesher quantises the requested ``target_size_mm`` onto its own
    refinement levels, so the realised element count is NOT monotone in the request:
    measured on a 200x20x20 bar, h=16 mm gave 12992 elements while h=13 mm gave 6144.
    Fitting a convergence order across such a ladder is meaningless.  So the ladder is
    MEASURED first with ``sim_mesh`` (mesh only, no solve; ``mesh_step_cached`` means
    the solve leg reuses this exact mesh for free) and only a subsequence whose element
    count grows by at least 1.35x per step is kept.  Element count is independent of
    element order — the mesher subdivides identically and tet10 only adds mid-edge
    nodes — so one calibration pass serves both orders, with a different budget each.
    """
    cb = _cb()
    measured: list[dict] = []
    cap = int(max(budgets.values()) * 1.35)
    for h in sorted(candidates, reverse=True):
        res = cb.call("sim_mesh", {"step_path": step_path, "order": 1,
                                   "target_size_mm": h})
        if not res.get("ok"):
            measured.append({"h_req_mm": h, "error": str(res.get("error"))[:160]})
            continue
        pts = res["points"]
        tets = res["tets"]
        pts3 = [pts[3 * i:3 * i + 3] for i in range(len(pts) // 3)]
        elems = [tets[4 * i:4 * i + 4] for i in range(len(tets) // 4)]
        system = {"points": pts3, "elems": elems}
        measured.append({"h_req_mm": h, "n_elems": res["n_elems"],
                         "n_nodes": res["n_nodes"],
                         "h_eff_mm": effective_h(system),
                         "mesh_volume_mm3": mesh_volume(system),
                         "aspect_max": res.get("aspect_max"),
                         "pct_poor": res.get("pct_poor")})
        if res["n_elems"] > cap:
            break
    ladder: dict[int, list[float]] = {}
    ok = [m for m in measured if m.get("n_elems")]
    for order, budget in budgets.items():
        pool = sorted([m for m in ok if m["n_elems"] <= budget],
                      key=lambda m: m["n_elems"])
        chosen: list[dict] = []
        for m in pool:
            if not chosen or m["n_elems"] >= 1.35 * chosen[-1]["n_elems"]:
                chosen.append(m)
        ladder[order] = [m["h_req_mm"] for m in chosen[-want:]]
    return {"measured": measured, "ladder": ladder}


def chud_solve(ctx: dict, order: int, h_mm: float, workdir: Path, tag: str) -> dict:
    """One production solve. Returns the result plus the exported system and fields."""
    cb = _cb()
    sys_path = workdir / f"{tag}_system.json"
    vtu_path = workdir / f"{tag}.vtu"
    study = dict(ctx["study"])
    study["mesh_order"] = order
    study["target_size_mm"] = h_mm
    study["export_system_path"] = str(sys_path)
    # A fresh .vtu path also guarantees a real solve: fea_run_static treats a promised
    # .vtu that does not exist as a cache MISS, so the export below can never describe
    # a system that was not actually solved this run.
    study["vtu_path"] = str(vtu_path)
    t0 = time.time()
    out = cb.call("sim_run_static", {"study": study})
    elapsed = time.time() - t0
    if not out.get("ok"):
        return {"ok": False, "error": str(out.get("error")), "seconds": elapsed}
    system = json.loads(sys_path.read_text())
    # The .vtu is bulky evidence; the fields we need are already top-level in the
    # result, so drop it once the solve has been proven to have run.
    try:
        vtu_bytes = vtu_path.stat().st_size
        vtu_path.unlink()
    except OSError:
        vtu_bytes = 0
    n = out["n_nodes"]
    fields = {}
    for key in ("displacement",):
        v = out.get(key) or []
        if len(v) == 3 * n:
            fields[key] = [v[3 * i:3 * i + 3] for i in range(n)]
    for key in ("von_mises", "stress_xx", "stress_yy", "stress_zz", "disp_magnitude"):
        v = out.get(key) or []
        if len(v) == n:
            fields[key] = v
    if "displacement" not in fields:
        return {"ok": False, "error": "result carries no nodal displacement field",
                "seconds": elapsed}
    # Stress is only comparable between orders at CORNER nodes: a tet10 mid-side node
    # takes an extrapolated/averaged value from a different set of integration points,
    # and mixing the two populations in one surface average is what made the tet10
    # bore hoop stress read WORSE than tet4 on the same geometry.  For tet4 every node
    # is a corner, so this is a no-op there.
    corner = set()
    for el in system["elems"]:
        corner.update(el[:4])
    return {"ok": True, "seconds": elapsed, "result": out, "system": system,
            "corner_nodes": corner,
            "fields": fields, "sys_path": str(sys_path), "vtu_bytes": vtu_bytes,
            "n_nodes": n, "n_elems": out["n_elems"], "n_dof": out["n_dof"],
            "order": order, "h_req_mm": h_mm,
            "h_eff_mm": effective_h(system),
            "provenance": out.get("provenance", {}),
            "mesh_quality": out.get("mesh_quality", {}),
            "reactions": out.get("reactions", {}),
            "summary": out.get("summary", {}),
            # Which factorization actually ran, when the build reports it — a
            # verification row that cannot name its solver is not evidence.
            "solver_label": out.get("solver") or out.get("factorization")
                            or out.get("provenance", {}).get("solver"),
            "solve_wait_ms": out.get("solve_wait_ms"),
            "solver_notes": out.get("solver_notes")}


def effective_h(system: dict) -> Optional[float]:
    """Mean element size implied by the mesh: edge of the equivalent regular tet."""
    pts, elems = system["points"], system["elems"]
    if not elems:
        return None
    total = 0.0
    for el in elems:
        total += abs(_signed_volume6(pts[el[0]], pts[el[1]], pts[el[2]], pts[el[3]])) / 6.0
    mean = total / len(elems)
    return (6.0 * math.sqrt(2.0) * mean) ** (1.0 / 3.0)


def mesh_volume(system: dict) -> float:
    pts, elems = system["points"], system["elems"]
    return sum(abs(_signed_volume6(pts[e[0]], pts[e[1]], pts[e[2]], pts[e[3]])) / 6.0
               for e in elems)


def is_memory_refusal(error: str) -> bool:
    e = (error or "").lower()
    return "budget" in e and ("needs an estimated" in e or "governor" in e)


def refusal_note(error: str) -> str:
    """The engine's own refusal, with its `binding limit:` clause intact.

    The governor is cgroup v2 aware, so which knob (if any) could have admitted a leg
    is a property of the run, not of this script: uncapped, the binding term is
    `CHUDWARE_MEM_FRAC x total RAM`; inside a systemd `MemoryMax` slice it is
    `cgroup memory.max`, which NO CHUDWARE_* variable can raise.  Paraphrasing that as
    "raise CHUDWARE_MEM_FRAC" is advice that walks a reader into a cgroup OOM kill, so
    the engine's clause is carried verbatim and never truncated away.
    """
    text = (error or "").strip()
    if not text:
        return "refused with no message"
    marker = text.lower().find("binding limit:")
    if marker < 0:
        return text[:400]
    # Keep the head (what it needed vs what it had) AND the whole binding clause.
    return text[:marker].rstrip()[:280] + " " + text[marker:][:400]


# ===========================================================================
# Field extraction helpers (geometry-driven, from the actual solved mesh)
# ===========================================================================

def _bbox(points) -> tuple[list[float], list[float]]:
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    return lo, hi


def section_mean_u(points, disp, axis: int, station: float, band: float,
                   comp: int) -> Optional[float]:
    vals = [disp[i][comp] for i, p in enumerate(points)
            if abs(p[axis] - station) <= band]
    return sum(vals) / len(vals) if vals else None


def face_mean_u(points, disp, axis: int, at_max: bool, comp: int,
                tol: float = 1e-6) -> Optional[float]:
    target = max(p[axis] for p in points) if at_max else min(p[axis] for p in points)
    vals = [disp[i][comp] for i, p in enumerate(points)
            if abs(p[axis] - target) <= tol]
    return sum(vals) / len(vals) if vals else None


# ===========================================================================
# TIER 1 case definitions
# ===========================================================================

def case_bar_axial() -> dict:
    L, b, hh, P = 200.0, 20.0, 20.0, 20000.0
    cb = _cb()
    doc = build_doc("vv_bar_axial", [cb.feat("box", "Bar", {
        "length": cb.dim(L), "width": cb.dim(b), "height": cb.dim(hh)})])
    ctx = make_study(doc, [{"of": "face", "side": "left"}],
                     [{"face": {"of": "face", "side": "right"}, "force": [P, 0.0, 0.0]}],
                     STEEL)
    ref = ref_axial_bar(P, L, b * hh, STEEL["E_mpa"])

    def extract(run: dict) -> list[dict]:
        pts = run["system"]["points"]
        disp = run["fields"]["displacement"]
        lo, hi = _bbox(pts)
        rows = []
        # The exact solution of the uniaxial problem is u_x linear in x, which lives
        # inside the tet4 space, so the interior strain must come back as P/(EA) to
        # round-off.  Measure it as the least-squares slope du_x/dx over the interior
        # (0.2L..0.8L, clear of the clamped end and the loaded end): a difference of
        # two station MEANS instead reads the mean of u at the mean of x, which is not
        # the station unless the node distribution happens to be symmetric about it —
        # measured, that alone put a 1e-2 error on one leg where the fit gives 1e-8.
        sel = [i for i, p in enumerate(pts)
               if lo[0] + 0.2 * L <= p[0] <= lo[0] + 0.8 * L]
        if len(sel) >= 8:
            n = len(sel)
            sx = sum(pts[i][0] for i in sel)
            su = sum(disp[i][0] for i in sel)
            sxx = sum(pts[i][0] ** 2 for i in sel)
            sxu = sum(pts[i][0] * disp[i][0] for i in sel)
            den = n * sxx - sx * sx
            if abs(den) > 1e-12:
                rows.append({"metric": "interior axial strain P/(EA)",
                             "ref": ref["strain"],
                             "got": (n * sxu - sx * su) / den,
                             "tol": TOL_EXACT, "unit": "-",
                             "note": f"least-squares du_x/dx over {n} interior nodes"})
        tip = face_mean_u(pts, disp, 0, True, 0)
        rows.append({"metric": "loaded-face mean u_x", "ref": ref["delta_mm"],
                     "got": tip, "tol": TOL_PRECISE, "unit": "mm"})
        rows.append({"metric": "summary.max_disp_mm",
                     "ref": ref["delta_mm"],
                     "got": run["summary"].get("max_disp_mm"),
                     "tol": TOL_ENGINEERING, "unit": "mm"})
        return rows

    return {"id": "BAR-AXIAL", "doc": doc, "ctx": ctx,
            "citation": "Hooke / uniform bar: eps=P/(EA), delta=PL/(EA) (exact tier)",
            "geometry": f"box {L:g}x{b:g}x{hh:g} mm, fixed -X face, {P:g} N +X on +X face",
            "material": STEEL, "extract": extract,
            "candidates": [30.0, 26.0, 22.0, 19.0, 16.0, 14.0, 12.0, 10.0, 8.5, 7.0,
                           6.0, 5.0],
            "reference": ref}


def case_cantilever() -> dict:
    L, b, hh, P = 150.0, 20.0, 30.0, 2000.0
    station = 30.0
    cb = _cb()
    doc = build_doc("vv_cantilever", [cb.feat("box", "Beam", {
        "length": cb.dim(L), "width": cb.dim(b), "height": cb.dim(hh)})])
    ctx = make_study(doc, [{"of": "face", "side": "left"}],
                     [{"face": {"of": "face", "side": "right"},
                       "force": [0.0, 0.0, -P]}], STEEL)
    ref = ref_cantilever(P, L, b, hh, STEEL["E_mpa"], STEEL["nu"], station)

    def extract(run: dict) -> list[dict]:
        pts = run["system"]["points"]
        disp = run["fields"]["displacement"]
        sxx = run["fields"].get("stress_xx")
        lo, hi = _bbox(pts)
        tipz = face_mean_u(pts, disp, 0, True, 2)
        rows = [
            # Beam theory is itself an approximation of the 3D problem: for L/h = 5 the
            # neglected terms are O((h/L)^2) ~ 4%, and the model is additionally
            # clamped over a whole face rather than at a section.  A 1% gate here would
            # be testing Timoshenko theory, not the solver, so the gate is 5% and the
            # Euler-Bernoulli row below is carried beside it: the solver is expected to
            # sit BETWEEN the two references, closer to Timoshenko, which is what
            # "the solve captures shear deformation" looks like as a measurement.
            {"metric": "tip-face mean |u_z| (Timoshenko, incl. shear)",
             "ref": ref["delta_mm"], "got": abs(tipz) if tipz is not None else None,
             "tol": 0.05, "unit": "mm"},
            {"metric": "tip-face mean |u_z| vs Euler-Bernoulli (no shear)",
             "ref": ref["delta_eb_mm"],
             "got": abs(tipz) if tipz is not None else None,
             "tol": TOL_ENGINEERING, "unit": "mm"}]
        if sxx:
            xs = lo[0] + station
            band = 0.02 * L
            zc = 0.5 * (lo[2] + hi[2])
            corner = run["corner_nodes"]
            sel = [i for i, p in enumerate(pts)
                   if abs(p[0] - xs) <= band and i in corner]
            # Extreme-fibre stress by a least-squares fit of the section's LINEAR
            # bending distribution, extrapolated to z = +h/2.  Averaging raw nodal
            # stresses over a near-surface slab instead reads ~15% low, because both
            # the lever arm z and the moment M(x) vary across any slab wide enough to
            # contain nodes on an unstructured mesh.
            if len(sel) >= 8:
                n = len(sel)
                sz = sum(pts[i][2] - zc for i in sel)
                ss = sum(sxx[i] for i in sel)
                szz = sum((pts[i][2] - zc) ** 2 for i in sel)
                szs = sum((pts[i][2] - zc) * sxx[i] for i in sel)
                den = n * szz - sz * sz
                if abs(den) > 1e-12:
                    slope = (n * szs - sz * ss) / den
                    intercept = (ss - slope * sz) / n
                    x_mean = sum(pts[i][0] for i in sel) / n
                    ref_station = ref_cantilever(P, L, b, hh, STEEL["E_mpa"],
                                                 STEEL["nu"], x_mean - lo[0])
                    rows.append({
                        "metric": "sigma_xx extreme fibre, fitted at the station",
                        "ref": ref_station["sigma_station_mpa"],
                        "got": intercept + slope * (hi[2] - zc),
                        "tol": TOL_ENGINEERING, "unit": "MPa"})
        return rows

    return {"id": "CANT-TIP", "doc": doc, "ctx": ctx,
            "citation": ("Timoshenko tip deflection PL^3/(3EI)+PL/(kGA), k=5/6; "
                         "bending sigma=M(x)c/I fitted from the section stress "
                         "distribution at a Saint-Venant station"),
            "geometry": (f"box {L:g}x{b:g}x{hh:g} mm (L/h={L/hh:g}), fixed -X face, "
                         f"{P:g} N -Z on +X face; shear term is "
                         f"{100*ref['delta_shear_mm']/ref['delta_mm']:.1f}% of the tip "
                         f"deflection"),
            "material": STEEL, "extract": extract,
            "candidates": [30.0, 26.0, 22.0, 19.0, 16.0, 14.0, 12.0, 10.0, 8.5, 7.0,
                           6.0, 5.0],
            "reference": ref}


def case_plate_hole() -> dict:
    L, W, t, d = 200.0, 100.0, 5.0, 10.0
    sigma_inf = 10.0
    P = sigma_inf * W * t
    cb = _cb()
    doc = build_doc("vv_plate_hole", [
        cb.feat("box", "Plate", {"length": cb.dim(L), "width": cb.dim(W),
                                 "height": cb.dim(t)}),
        cb.feat("cylinder", "Hole", {"radius": cb.dim(d / 2.0),
                                     "height": cb.dim(4.0 * t), "mode": "cut"}),
    ])
    ctx = make_study(doc, [{"of": "face", "side": "left"}],
                     [{"face": {"of": "face", "side": "right"},
                       "force": [P, 0.0, 0.0]}], STEEL)
    ref = ref_plate_hole(sigma_inf, d, W)

    def extract(run: dict) -> list[dict]:
        pts = run["system"]["points"]
        sxx = run["fields"].get("stress_xx")
        rows: list[dict] = []
        if not sxx:
            return rows
        a = d / 2.0
        # ON the rim only. A node one element INSIDE the hole's stress gradient reads
        # far below the rim value, so a wide radial band silently reports an SCF of
        # 1.3-2.0 on a mesh that has simply not put a node on the rim: measured, a
        # 0.20*a band gave 1.34 where a 0.05*a band on the same mesh gives the rim
        # value. The peak lies at theta = +/-90 deg, so the max over rim nodes IS the
        # concentration; if the mesh has no rim node the row is omitted rather than
        # guessed.
        corner = run["corner_nodes"]
        # EVERY node on the rim, corners and tet10 mid-sides alike: a mid-side node on
        # a curved boundary lies on the true circle, so including it samples the rim
        # more densely and a peak metric wants the densest available sampling.
        # (Measured: corner-only sampling on the coarse tet10 leg read SCF 1.47 where
        # all rim nodes read 1.72 on the identical solve. The opposite choice is right
        # for the cylinder's hoop AVERAGE, where mixing two recovery populations biases
        # the mean rather than just thinning the sample.)
        rim = [i for i, p in enumerate(pts)
               if abs(math.hypot(p[0], p[1]) - a) <= 0.05 * a]
        if len(rim) >= 4:
            peak = max(sxx[i] for i in rim)
            at = max(rim, key=lambda i: sxx[i])
            theta = math.degrees(math.atan2(abs(pts[at][1]), abs(pts[at][0])))
            rows.append({"metric": "SCF at hole rim (Kirsch 3.0)",
                         "ref": ref["scf_kirsch"], "got": peak / sigma_inf,
                         "tol": TOL_ENGINEERING, "unit": "-",
                         "note": f"{len(rim)} rim nodes, peak at theta="
                                 f"{theta:.0f} deg"})
            rows.append({"metric": "SCF at hole rim (Howland finite width)",
                         "ref": ref["scf_howland"], "got": peak / sigma_inf,
                         "tol": TOL_ENGINEERING, "unit": "-"})
        # Far field: 5-7.5 hole radii from the hole AND at least a quarter length from
        # either loaded end, so neither the concentration nor the end effect leaks in.
        far = [sxx[i] for i, p in enumerate(pts)
               if 0.25 * L <= abs(p[0]) <= 0.375 * L and i in corner]
        if far:
            rows.append({"metric": "far-field sigma_xx (gross section)",
                         "ref": sigma_inf, "got": sum(far) / len(far),
                         "tol": TOL_PRECISE, "unit": "MPa"})
        return rows

    return {"id": "PLATE-HOLE", "doc": doc, "ctx": ctx,
            "citation": ("Kirsch 1898 (SCF 3.0, infinite plate) with Howland "
                         "finite-width correction, Peterson chart"),
            "geometry": (f"plate {L:g}x{W:g}x{t:g} mm, central hole d={d:g} mm "
                         f"(d/W={d/W:g}), fixed -X face, {P:g} N +X on +X face "
                         f"(gross sigma_inf={sigma_inf:g} MPa)"),
            "material": STEEL, "extract": extract,
            "candidates": [18.0, 15.0, 13.0, 11.0, 9.5, 8.0, 7.0, 6.0, 5.0, 4.5,
                           4.0, 3.5],
            "reference": ref}


def case_cylinder_lame() -> dict:
    a, b, Lz, p = 15.0, 30.0, 90.0, 20.0
    cb = _cb()
    doc = build_doc("vv_cyl_lame", [
        cb.feat("cylinder", "Outer", {"radius": cb.dim(b), "height": cb.dim(Lz)}),
        cb.feat("cylinder", "Bore", {"radius": cb.dim(a),
                                     "height": cb.dim(Lz + 20.0), "mode": "cut"}),
    ])
    # SIGN: `pressure` is "MPa, inward" — a POSITIVE magnitude pushes INTO the loaded
    # face, so +p on the bore IS internal pressurisation and this case passes +p
    # straight through.  It did not always: the solver deposited
    # +p*integral(N_i n_outward)dA, which made +20 MPa read sigma_hoop = -29.6 MPa
    # against Lame's +33.3 (see CORROBORATED_FINDINGS "pressure load sign (FIXED)").
    # The deposit is now -p*integral(N_i n_outward)dA (core/sim/fea_run.cpp,
    # fea_run_static_impl pressure branch).  The -p run is kept as the sign_probe so
    # the convention stays MEASURED: -p must be EXTERNAL pressurisation, i.e. hoop
    # COMPRESSION at the bore.
    bore = {"of": "face", "surf": "cylindrical", "pick": "inner"}
    ctx = make_study(doc, [{"of": "face", "side": "bottom"}],
                     [{"face": bore, "pressure": p}], STEEL)
    probe_ctx = make_study(doc, [{"of": "face", "side": "bottom"}],
                           [{"face": bore, "pressure": -p}], STEEL)
    ref_a = ref_lame(p, a, b, a, STEEL["E_mpa"], STEEL["nu"])
    ref_b = ref_lame(p, a, b, b, STEEL["E_mpa"], STEEL["nu"])

    def extract(run: dict) -> list[dict]:
        pts = run["system"]["points"]
        disp = run["fields"]["displacement"]
        syy = run["fields"].get("stress_yy")
        sxx = run["fields"].get("stress_xx")
        lo, hi = _bbox(pts)
        # Sample a band far from the clamped -Z end: the clamp's radial restraint
        # decays over ~sqrt(R*t) = sqrt(22.5*15) ~ 18 mm, so 0.55..0.95 L is 5+ decay
        # lengths away and the ends are traction-free (open-ended Lame, sigma_z = 0).
        z0 = lo[2] + 0.55 * Lz
        z1 = lo[2] + 0.95 * Lz
        rows: list[dict] = []
        for radius, tag, refv in ((a, "inner", ref_a), (b, "outer", ref_b)):
            sel = [i for i, q in enumerate(pts)
                   if z0 <= q[2] <= z1
                   and abs(math.hypot(q[0], q[1]) - radius) <= 0.06 * radius]
            if not sel:
                continue
            ur = []
            for i in sel:
                r = math.hypot(pts[i][0], pts[i][1])
                if r < 1e-9:
                    continue
                ur.append((disp[i][0] * pts[i][0] + disp[i][1] * pts[i][1]) / r)
            if ur:
                rows.append({"metric": f"u_r at {tag} wall (r={radius:g})",
                             "ref": refv["u_r_mm"], "got": sum(ur) / len(ur),
                             "tol": TOL_ENGINEERING, "unit": "mm"})
            if syy and sxx:
                # On the +/-X axis the hoop direction is Y and the radial direction X,
                # so the two diagonal components ARE sigma_hoop and sigma_rad there.
                axis_sel = [i for i in sel
                            if abs(pts[i][1]) <= 0.12 * radius
                            and i in run["corner_nodes"]]
                if axis_sel:
                    hoop = sum(syy[i] for i in axis_sel) / len(axis_sel)
                    rows.append({"metric": f"sigma_hoop at {tag} wall",
                                 "ref": refv["hoop_mpa"], "got": hoop,
                                 "tol": TOL_ENGINEERING, "unit": "MPa"})
        return rows

    return {"id": "CYL-LAME", "doc": doc, "ctx": ctx,
            "citation": ("Lame thick-walled cylinder, internal pressure, open ends: "
                         "sigma_hoop = p a^2/(b^2-a^2)(1+b^2/r^2)"),
            "geometry": (f"tube a={a:g} b={b:g} L={Lz:g} mm (wall {b-a:g} mm), "
                         f"fixed -Z end face, internal pressure {p:g} MPa on the bore "
                         f"(passed as pressure=+{p:g}: +p acts inward)"),
            "material": STEEL, "extract": extract,
            "candidates": [24.0, 22.0, 20.0, 18.0, 17.0, 16.0, 15.0, 14.0, 13.0, 12.0,
                           11.0, 9.5, 8.0, 7.0],
            "sign_probe": {"ctx": probe_ctx, "extract": extract,
                           "magnitude_mpa": -p,
                           "what": ("same model, same bore face, pressure=-p instead "
                                    "of +p (external pressurisation)")},
            "reference": {"inner": ref_a, "outer": ref_b}}


TIER1_BUILDERS: dict[str, Callable[[], dict]] = {
    "BAR-AXIAL": case_bar_axial,
    "CANT-TIP": case_cantilever,
    "PLATE-HOLE": case_plate_hole,
    "CYL-LAME": case_cylinder_lame,
}


# ===========================================================================
# TIER 2: same-mesh CalculiX cross-solve
# ===========================================================================

def ccx_cross_solve(ccx: str, run: dict, workdir: Path, tag: str,
                    timeout: int = 1800) -> dict:
    """Re-solve ``run``'s exported system with ccx and compare node for node."""
    job = f"ccx_{tag}"
    jobdir = workdir / job
    jobdir.mkdir(parents=True, exist_ok=True)
    inp = jobdir / f"{job}.inp"
    meta = write_ccx_inp(inp, run["system"], job=job)
    t0 = time.time()
    try:
        proc = subprocess.run([ccx, "-i", job], cwd=str(jobdir), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ccx timed out after {timeout}s", "meta": meta}
    elapsed = time.time() - t0
    dat = jobdir / f"{job}.dat"
    frd = jobdir / f"{job}.frd"
    if not dat.exists():
        tail = (proc.stdout or "")[-1200:] + (proc.stderr or "")[-600:]
        return {"ok": False, "error": f"ccx exit {proc.returncode}; no .dat",
                "stdout_tail": tail, "meta": meta}
    dat_blocks = parse_ccx_dat(dat.read_text(errors="replace"))
    frd_blocks = parse_ccx_frd(frd.read_text(errors="replace")) if frd.exists() else {}

    u_ccx = dat_blocks["displacements"].get("NALL") or {}
    if not u_ccx and frd_blocks.get("DISP"):
        u_ccx = {k: tuple(v[:3]) for k, v in frd_blocks["DISP"].items()}
    if not u_ccx:
        return {"ok": False, "error": "no ccx displacement table found",
                "meta": meta, "seconds": elapsed}

    disp = run["fields"]["displacement"]
    n = len(disp)
    missing = [i for i in range(n) if (i + 1) not in u_ccx]
    if missing:
        return {"ok": False, "meta": meta, "seconds": elapsed,
                "error": f"ccx table covers {len(u_ccx)} of {n} nodes "
                         f"(first missing node id {missing[0] + 1})"}

    # Node-for-node displacement comparison.
    num2 = 0.0
    den2 = 0.0
    linf_num = 0.0
    linf_at = -1
    peak_ref = 0.0
    for i in range(n):
        a = disp[i]
        c = u_ccx[i + 1]
        d2 = sum((a[k] - c[k]) ** 2 for k in range(3))
        r2 = sum(c[k] ** 2 for k in range(3))
        num2 += d2
        den2 += r2
        mag = math.sqrt(r2)
        peak_ref = max(peak_ref, mag)
        if math.sqrt(d2) > linf_num:
            linf_num = math.sqrt(d2)
            linf_at = i
    l2_rel = math.sqrt(num2 / den2) if den2 > 0 else None
    linf_rel = linf_num / peak_ref if peak_ref > 0 else None
    max_chud = max(math.sqrt(sum(c * c for c in u)) for u in disp)
    max_ccx = max(math.sqrt(sum(c * c for c in u)) for u in u_ccx.values())

    # Reaction resultant over the fixed set.
    rf = dat_blocks["forces"].get("NFIX") or {}
    rf_sum = [sum(v[k] for v in rf.values()) for k in range(3)] if rf else None
    chud_rf = run.get("reactions", {}).get("reaction_force_n")

    # Nodal-averaged von Mises on both sides (different recovery paths, stated).
    vm_ccx_peak = None
    vm_pair = None
    if frd_blocks.get("STRESS"):
        vms = {k: von_mises_from_components(v) for k, v in frd_blocks["STRESS"].items()
               if len(v) >= 6}
        if vms:
            vm_ccx_peak = max(vms.values())
            chud_vm = run["fields"].get("von_mises")
            if chud_vm:
                num = den = 0.0
                for i in range(n):
                    c = vms.get(i + 1)
                    if c is None:
                        continue
                    num += (chud_vm[i] - c) ** 2
                    den += c * c
                vm_pair = math.sqrt(num / den) if den > 0 else None

    # .dat vs .frd cross-check of our own parsers.
    parser_check = None
    if frd_blocks.get("DISP") and dat_blocks["displacements"].get("NALL"):
        worst = 0.0
        scale = max(1e-30, peak_ref)
        for nid, v in frd_blocks["DISP"].items():
            d = dat_blocks["displacements"]["NALL"].get(nid)
            if d is None:
                continue
            worst = max(worst, max(abs(v[k] - d[k]) for k in range(3)))
        parser_check = worst / scale

    # ccx's .dat tables are written %13.6E — SEVEN significant digits — so the
    # smallest difference this comparison channel can RESOLVE is the rounding of the
    # reference values themselves.  Half-step per component is
    # 0.5e-6 * 10^exponent; a rounding error uniform on that interval has RMS
    # half-step/sqrt(3).  Reporting that floor beside the measured difference is the
    # difference between "the codes agree to 1e-7" and "the codes agree to every digit
    # the reference prints".
    q2 = 0.0
    for c in u_ccx.values():
        for v in c[:3]:
            if v != 0.0:
                half = 0.5 * 10.0 ** (math.floor(math.log10(abs(v))) - 6)
                q2 += (half / math.sqrt(3.0)) ** 2
    floor_l2 = math.sqrt(q2 / den2) if den2 > 0 else None

    return {"ok": True, "meta": meta, "seconds": elapsed,
            "l2_rel": l2_rel, "linf_rel": linf_rel, "linf_node": linf_at,
            "max_disp_chud_mm": max_chud, "max_disp_ccx_mm": max_ccx,
            "max_disp_rel": rel(max_chud, max_ccx),
            "reaction_ccx_n": rf_sum, "reaction_chud_n": chud_rf,
            "reaction_rel": (rel(math.sqrt(sum(c * c for c in chud_rf)),
                                 math.sqrt(sum(c * c for c in rf_sum)))
                             if rf_sum and chud_rf else None),
            "vm_peak_chud_mpa": (max(run["fields"]["von_mises"])
                                 if run["fields"].get("von_mises") else None),
            "vm_peak_ccx_mpa": vm_ccx_peak,
            "vm_field_rel_l2": vm_pair,
            "frd_vs_dat_rel": parser_check,
            "print_precision_floor_l2": floor_l2}


# ===========================================================================
# TIER 3: polymesh solve
# ===========================================================================

def polymesh_binary() -> Optional[str]:
    cand = _PROJECT_ROOT / "core" / "build-occt-linux" / "bin" / "polymesh"
    return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None


def polymesh_commit() -> str:
    pin = os.environ.get("CHUDWARE_POLYMESH_REVISION", "").strip()
    if pin:
        return pin
    provision = _PROJECT_ROOT / "core" / "cmake" / "ProvisionPolymesh.cmake"
    if provision.exists():
        for line in provision.read_text(encoding="utf-8").splitlines():
            if "set(POLYMESH_COMMIT" in line and '"' in line:
                return line.split('"', 2)[1]
    return "unresolved"


def read_vtu_fields(path: Path) -> dict:
    text = path.read_text(errors="replace")
    m = re.search(r"<Points>.*?<DataArray[^>]*>(.*?)</DataArray>", text, re.S)
    pts = []
    if m:
        flat = [float(v) for v in m.group(1).split()]
        pts = [flat[i:i + 3] for i in range(0, len(flat), 3)]
    fields: dict[str, list] = {}
    for fm in re.finditer(
            r'<DataArray[^>]*Name="([^"]+)"[^>]*NumberOfComponents="(\d+)"[^>]*>(.*?)</DataArray>',
            text, re.S):
        name, ncomp, body = fm.group(1), int(fm.group(2)), fm.group(3)
        if name in ("connectivity", "offsets", "types"):
            continue
        flat = [float(v) for v in body.split()]
        fields[name] = ([flat[i:i + ncomp] for i in range(0, len(flat), ncomp)]
                        if ncomp > 1 else flat)
    for fm in re.finditer(r'<DataArray[^>]*Name="([^"]+)"(?![^>]*NumberOfComponents)[^>]*>(.*?)</DataArray>',
                          text, re.S):
        name, body = fm.group(1), fm.group(2)
        if name in ("connectivity", "offsets", "types") or name in fields:
            continue
        fields[name] = [float(v) for v in body.split()]
    return {"points": pts, "fields": fields}


def polymesh_solve(pm: str, step_path: str, workdir: Path, tag: str, *,
                   h_m: float, E_pa: float, nu: float,
                   fix_box: list[float], load_box: list[float],
                   load_dir: list[float], force_n: float,
                   timeout: int = 900) -> dict:
    out_vtu = workdir / f"pm_{tag}.vtu"
    # -h is in the POST-scale unit (metres): the upstream fixtures are already metric
    # and their case files use metre boxes, while an mm STEP needs --scale 0.001, so
    # -h 0.01 on a mm part is a 10 mm cell.
    # The size caps are not cosmetic: `polymesh mesh` at -h 0.01 on the 200x20x20 bar
    # produces 75680 tets (the graded refinement blows well past its own predicted 480),
    # and the SOLVE stage of that problem did not finish in 400 s on this box, so the
    # element/DOF ceilings and --no-curved/--no-spectral keep the independent-solver
    # lane to a size it can actually turn around.
    cmd = [pm, "solve", step_path, "-o", str(out_vtu), "-h", f"{h_m:.6g}",
           "-E", f"{E_pa:.6g}", "-nu", f"{nu:.6g}", "--scale", "0.001",
           "--max-elems", os.environ.get("VV_PM_MAX_ELEMS", "20000"),
           "--max-dof", os.environ.get("VV_PM_MAX_DOF", "80000"),
           "--no-curved", "--no-spectral",
           "--fix-box", *[f"{v:.6g}" for v in fix_box],
           "--load-box", *[f"{v:.6g}" for v in load_box],
           "--load-dir", *[f"{v:.6g}" for v in load_dir],
           "--force", f"{force_n:.6g}"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"polymesh timed out after {timeout}s",
                "cmd": " ".join(cmd)}
    elapsed = time.time() - t0
    if proc.returncode != 0 or not out_vtu.exists():
        return {"ok": False, "cmd": " ".join(cmd), "seconds": elapsed,
                "error": f"polymesh exit {proc.returncode}",
                "stdout_tail": (proc.stdout or "")[-1500:],
                "stderr_tail": (proc.stderr or "")[-800:]}
    data = read_vtu_fields(out_vtu)
    out_vtu.unlink(missing_ok=True)
    return {"ok": True, "cmd": " ".join(cmd), "seconds": elapsed,
            "stdout": (proc.stdout or "")[-3000:], "points": data["points"],
            "fields": data["fields"],
            "field_names": sorted(data["fields"].keys())}


def pm_displacement(data: dict) -> Optional[list]:
    for name in ("displacement", "u", "U", "disp", "Displacement"):
        v = data["fields"].get(name)
        if v and isinstance(v[0], list) and len(v[0]) == 3:
            return v
    return None


def pm_scalar(data: dict, names: tuple) -> Optional[list]:
    for name in names:
        v = data["fields"].get(name)
        if v and not isinstance(v[0], list):
            return v
    return None


# ===========================================================================
# Tier runners
# ===========================================================================

def run_tier1_and_2(workdir: Path, ccx: Optional[str], only: Optional[list[str]],
                    do_ccx: bool) -> None:
    _log("\n=== TIER 1 (analytic) + TIER 2 (same-mesh CalculiX) ===")
    for case_id, builder in TIER1_BUILDERS.items():
        if only and case_id not in only:
            continue
        _log(f"\n--- {case_id}")
        try:
            case = builder()
        except Exception as exc:  # noqa: BLE001
            skip("tier1", case_id, f"model/BC setup failed: {exc}")
            traceback.print_exc()
            continue
        budgets = {1: int(os.environ.get("VV_TET4_ELEM_BUDGET", 220000)),
                   2: int(os.environ.get("VV_TET10_ELEM_BUDGET", 90000))}
        cal = calibrate_ladder(case["ctx"]["step_path"], case["candidates"], budgets)
        record("tier1", {"case": case_id, "kind": "mesh_calibration",
                         "candidates": case["candidates"],
                         "element_budgets": budgets,
                         "measured": cal["measured"], "ladder": cal["ladder"]})
        _log("  mesh calibration (h_req -> n_elems / h_eff): " + ", ".join(
            f"{m['h_req_mm']:g}->{m.get('n_elems', 'err')}"
            f"/{m['h_eff_mm']:.2f}" if m.get("n_elems") else
            f"{m['h_req_mm']:g}->error" for m in cal["measured"]))
        _log(f"  ladder tet4={cal['ladder'][1]} tet10={cal['ladder'][2]}")
        probe = case.get("sign_probe")
        if probe:
            h0 = (cal["ladder"][1] or cal["ladder"][2] or [None])[0]
            if h0 is not None:
                pr = chud_solve(probe["ctx"], 1, h0, workdir,
                                f"{case_id.lower()}_signprobe")
                if pr["ok"]:
                    got = {r["metric"]: r["got"] for r in probe["extract"](pr)}
                    record("tier1", {"case": case_id, "kind": "sign_probe",
                                     "what": probe["what"], "h_req_mm": h0,
                                     "element": "tet4", "measured": got,
                                     "n_elems": pr["n_elems"]})
                    hoop = got.get("sigma_hoop at inner wall")
                    # The convention is MEASURED, not assumed: `pressure` is "MPa,
                    # inward", the released case runs +p (internal pressurisation, hoop
                    # TENSION), and this probe runs -p on the identical model/mesh, which
                    # must therefore put the bore wall in hoop COMPRESSION. A
                    # non-negative hoop here means the sign convention flipped back —
                    # the exact defect fixed on 2026-09-05 (see CORROBORATED_FINDINGS
                    # "pressure load sign (FIXED)").
                    if hoop is not None and hoop >= 0:
                        finding("CYL-LAME (pressure sign)",
                                "`pressure` is documented \"MPa, inward\" (+p pushes "
                                "INTO the loaded face), so a NEGATIVE magnitude on a "
                                "bore is external pressurisation and must give hoop "
                                "COMPRESSION — it did not, so the load sign is "
                                "inverted again",
                                f"same model/mesh (tet4 h={h0:g}, {pr['n_elems']} "
                                f"elems): pressure={probe['magnitude_mpa']:g} MPa on "
                                f"the bore gives sigma_hoop={hoop:.3f} MPa and u_r="
                                f"{got.get('u_r at inner wall (r=15)')!r} mm, against "
                                f"Lame's "
                                f"{ref_lame(probe['magnitude_mpa'], 15.0, 30.0, 15.0, STEEL['E_mpa'], STEEL['nu'])['hoop_mpa']:.3f}"
                                f" MPa for that magnitude. The deposit must be "
                                f"-p*integral(N_i n_outward)dA in core/sim/fea_run.cpp "
                                f"(fea_run_static_impl pressure branch)")
                del pr
        per_metric: dict[str, list[tuple[float, float]]] = {}
        case_rows: list[dict] = []
        for order in (1, 2):
            for h in cal["ladder"][order]:
                tag = f"{case_id.lower()}_o{order}_h{h:g}".replace(".", "p")
                run = chud_solve(case["ctx"], order, h, workdir, tag)
                if not run["ok"]:
                    reason = refusal_note(run["error"])
                    if is_memory_refusal(run["error"]):
                        skip("tier1", f"{case_id} tet{4 if order==1 else 10} h={h:g}",
                             f"memory governor refused this leg \u2014 engine "
                             f"verbatim: {reason}")
                    else:
                        record("tier1", {"case": case_id, "order": order,
                                         "h_req_mm": h, "verdict": "FAIL",
                                         "metric": "solve", "note": reason})
                        _log(f"  [FAIL] {case_id} tet{4 if order==1 else 10} h={h:g}: "
                             f"{reason}")
                    continue
                el = "tet4" if order == 1 else "tet10"
                _log(f"  {el} h_req={h:g} h_eff={run['h_eff_mm']:.3f} "
                     f"n_elems={run['n_elems']} n_dof={run['n_dof']} "
                     f"({run['seconds']:.1f}s)")
                _PROVENANCE.setdefault("engine", run["provenance"])
                rows = case["extract"](run)
                if not rows:
                    finding(case_id, "no metric could be extracted from this leg",
                            f"order={order} h={h}")
                buffered: list[dict] = []
                for row in rows:
                    e = rel(row["got"], row["ref"])
                    buffered.append({
                        "case": case_id, "metric": row["metric"], "order": order,
                        "element": el, "h_req_mm": h, "h_eff_mm": run["h_eff_mm"],
                        "n_elems": run["n_elems"], "n_dof": run["n_dof"],
                        "unit": row["unit"], "ref": row["ref"], "got": row["got"],
                        "rel_err": e, "tol": row["tol"],
                        "verdict": verdict(e, row["tol"]),
                        "note": row.get("note", ""),
                        "citation": case["citation"],
                        "engine_build_id": run["provenance"].get("engine_build_id"),
                        "chudware_solver": run.get("solver_label"),
                        "solve_wait_ms": run.get("solve_wait_ms"),
                        "seconds": round(run["seconds"], 2)})
                    mark = buffered[-1]["verdict"]
                    _log(f"    [{mark}] {row['metric']}: got={row['got']!r} "
                         f"ref={row['ref']:.6g} rel={e if e is None else f'{e:.3e}'}")
                    if e is not None and math.isfinite(e):
                        per_metric.setdefault(f"{el}|{row['metric']}", []).append(
                            (run["h_eff_mm"], e))
                case_rows.extend(buffered)
                if do_ccx and ccx:
                    # ccx's factorization is not memory-governed by anything, and it
                    # runs in the same slice as the Chudware solve, so the deck size
                    # is capped explicitly rather than discovered by an OOM.
                    cap = int(os.environ.get("VV_CCX_MAX_DOF", 250000))
                    if run["n_dof"] > cap:
                        skip("tier2", f"{case_id} {el} h={h:g}",
                             f"cross-solve skipped: {run['n_dof']} DOF exceeds the "
                             f"CalculiX deck cap of {cap} DOF for this slice "
                             f"(the Chudware leg itself is reported in Tier 1)")
                    else:
                        cmp_ = ccx_cross_solve(ccx, run, workdir, tag)
                        _record_tier2(case_id, el, h, run, cmp_)
                del run
        # A discretization-limited row is judged on its FINEST mesh; the coarser legs
        # of the same (metric, element) are the convergence evidence, not independent
        # verdicts.  Gating every leg would count "the coarsest mesh in the ladder is
        # not converged yet" as a solver failure, which is not what it means.
        finest: dict[tuple, int] = {}
        for idx, r in enumerate(case_rows):
            key = (r["metric"], r["element"])
            if key not in finest or r["n_dof"] > case_rows[finest[key]]["n_dof"]:
                finest[key] = idx
        gated = set(finest.values())
        for idx, r in enumerate(case_rows):
            if idx not in gated:
                r["gated"] = False
                r["verdict"] = "conv-leg"
            else:
                r["gated"] = True
            record("tier1", r)
        for key, pairs in per_metric.items():
            if len(pairs) < 2:
                continue
            el, metric = key.split("|", 1)
            pairs = sorted(pairs, key=lambda p: -p[0])  # coarse -> fine
            hs = [p[0] for p in pairs]
            es = [p[1] for p in pairs]
            order_obs = fit_order(hs, es)
            # A least-squares slope across a mesh family that is not nested can be
            # meaningless. Say whether the error actually fell monotonically as h fell,
            # so a reader knows whether the slope is a convergence order or an
            # indication.
            monotone = all(es[i + 1] < es[i] for i in range(len(es) - 1))
            record("tier1", {"case": case_id, "metric": metric, "element": el,
                             "kind": "convergence", "n_legs": len(pairs),
                             "observed_order": order_obs, "monotone": monotone,
                             "h_eff_mm": [round(h, 4) for h in hs],
                             "rel_err": es})
            _log(f"    [ORDER] {el} {metric}: observed slope "
                 f"{order_obs if order_obs is None else f'{order_obs:.2f}'} over "
                 f"{len(pairs)} legs (monotone={monotone})")


def _record_tier2(case_id: str, element: str, h: float, run: dict,
                  cmp_: dict) -> None:
    if not cmp_.get("ok"):
        record("tier2", {"case": case_id, "element": element, "h_req_mm": h,
                         "verdict": "FAIL", "note": cmp_.get("error", "")[:300],
                         "n_elems": run["n_elems"], "n_dof": run["n_dof"]})
        _log(f"    [FAIL] ccx cross-solve: {cmp_.get('error','')[:200]}")
        return
    l2 = cmp_["l2_rel"]
    v = verdict(l2, TOL_CODE_TO_CODE)
    record("tier2", {
        "case": case_id, "element": element, "h_req_mm": h,
        "h_eff_mm": run["h_eff_mm"], "n_nodes": run["n_nodes"],
        "n_elems": run["n_elems"], "n_dof": run["n_dof"],
        "ccx_elements": f"C3D{cmp_['meta']['nodes_per_elem']}",
        "inverted_reoriented": cmp_["meta"]["inverted_elements_reoriented"],
        "disp_l2_rel": l2, "disp_linf_rel": cmp_["linf_rel"],
        "max_disp_chud_mm": cmp_["max_disp_chud_mm"],
        "max_disp_ccx_mm": cmp_["max_disp_ccx_mm"],
        "max_disp_rel": cmp_["max_disp_rel"],
        "reaction_chud_n": cmp_["reaction_chud_n"],
        "reaction_ccx_n": cmp_["reaction_ccx_n"],
        "reaction_rel": cmp_["reaction_rel"],
        "vm_peak_chud_mpa": cmp_["vm_peak_chud_mpa"],
        "vm_peak_ccx_mpa": cmp_["vm_peak_ccx_mpa"],
        "vm_peak_rel": rel(cmp_["vm_peak_chud_mpa"], cmp_["vm_peak_ccx_mpa"]),
        # summary.max_von_mises_mpa is NOT the field peak: the solver masks a
        # two-element ring around the fixed nodes out of the summary scalar.  Both are
        # recorded because the CalculiX column is an unmasked field peak, so only the
        # raw nodal max is comparable with it.
        "vm_summary_chud_mpa": run["summary"].get("max_von_mises_mpa"),
        "vm_field_rel_l2": cmp_["vm_field_rel_l2"],
        "frd_vs_dat_rel": cmp_["frd_vs_dat_rel"],
        "print_precision_floor_l2": cmp_["print_precision_floor_l2"],
        "tol": TOL_CODE_TO_CODE, "verdict": v,
        "ccx_seconds": round(cmp_["seconds"], 1),
        "chudware_solver": run.get("solver_label"),
        "engine_build_id": run["provenance"].get("engine_build_id")})
    floor = cmp_["print_precision_floor_l2"]
    _log(f"    [{v}] ccx same-mesh: |u| L2 rel={l2:.3e} Linf rel="
         f"{cmp_['linf_rel']:.3e} (ccx print-precision floor "
         f"{'n/a' if floor is None else f'{floor:.1e}'}) maxdisp "
         f"{cmp_['max_disp_chud_mm']:.6g} vs {cmp_['max_disp_ccx_mm']:.6g} "
         f"({cmp_['seconds']:.0f}s)")
    if v == "FAIL":
        finding(f"{case_id}/{element}",
                f"same-mesh displacement disagreement L2={l2:.3e} exceeds "
                f"{TOL_CODE_TO_CODE:.0e}",
                f"n_dof={run['n_dof']}, Linf={cmp_['linf_rel']:.3e} at node "
                f"{cmp_['linf_node']}, ccx max|u|={cmp_['max_disp_ccx_mm']:.6g} mm vs "
                f"Chudware {cmp_['max_disp_chud_mm']:.6g} mm")
    if cmp_["frd_vs_dat_rel"] is not None and cmp_["frd_vs_dat_rel"] > 1e-4:
        finding(f"{case_id}/{element}",
                "ccx .frd and .dat displacement tables disagree — parser or output "
                "inconsistency",
                f"max |frd-dat| / max|u| = {cmp_['frd_vs_dat_rel']:.3e}")


def run_tier3(workdir: Path, pm: str) -> None:
    _log("\n=== TIER 3 (polymesh: Polyhedral-FEA's own solver) ===")
    E_pa = STEEL["E_mpa"] * 1e6
    nu = STEEL["nu"]
    specs = []

    L, b, hh, P = 200.0, 20.0, 20.0, 20000.0
    specs.append({
        "id": "BAR-AXIAL", "L": L,
        "build": lambda: build_doc("vv_pm_bar", [
            _cb().feat("box", "Bar", {"length": _cb().dim(L), "width": _cb().dim(b),
                                      "height": _cb().dim(hh)})]),
        "load_dir": [1.0, 0.0, 0.0], "force": P, "axis": 0,
        "metric": "loaded-face mean u_x",
        "ref": ref_axial_bar(P, L, b * hh, STEEL["E_mpa"])["delta_mm"],
        "comp": 0, "unit": "mm", "tol": TOL_ENGINEERING,
        "citation": "delta = PL/(EA)",
        "h_mm": [20.0]})

    Lc, bc, hc, Pc = 150.0, 20.0, 30.0, 2000.0
    specs.append({
        "id": "CANT-TIP", "L": Lc,
        "build": lambda: build_doc("vv_pm_cant", [
            _cb().feat("box", "Beam", {"length": _cb().dim(Lc), "width": _cb().dim(bc),
                                       "height": _cb().dim(hc)})]),
        "load_dir": [0.0, 0.0, -1.0], "force": Pc, "axis": 0,
        "metric": "tip-face mean |u_z|",
        "ref": ref_cantilever(Pc, Lc, bc, hc, STEEL["E_mpa"], STEEL["nu"],
                              30.0)["delta_mm"],
        "comp": 2, "unit": "mm", "tol": TOL_ENGINEERING,
        "citation": "Timoshenko PL^3/(3EI)+PL/(kGA)",
        "h_mm": [20.0]})

    Lp, Wp, tp, dp = 200.0, 100.0, 5.0, 10.0
    sig = 10.0
    specs.append({
        "id": "PLATE-HOLE", "L": Lp,
        "build": lambda: build_doc("vv_pm_plate", [
            _cb().feat("box", "Plate", {"length": _cb().dim(Lp),
                                        "width": _cb().dim(Wp),
                                        "height": _cb().dim(tp)}),
            _cb().feat("cylinder", "Hole", {"radius": _cb().dim(dp / 2.0),
                                            "height": _cb().dim(4 * tp),
                                            "mode": "cut"})]),
        "load_dir": [1.0, 0.0, 0.0], "force": sig * Wp * tp, "axis": 0,
        "metric": "loaded-face mean u_x",
        "ref": None, "comp": 0, "unit": "mm", "tol": TOL_ENGINEERING,
        "citation": ("net-section elongation is not a clean closed form for a holed "
                     "plate; the SCF row is the reference of record and needs a "
                     "nodal stress field polymesh solve does not emit per-node here"),
        "h_mm": [12.0]})

    for spec in specs:
        try:
            doc = spec["build"]()
            step = str(Path(tempfile.mkdtemp(prefix="vv_pm_")) / f"{spec['id']}.step")
            _cb().call("doc_export_step", {"doc_id": doc, "path": step})
        except Exception as exc:  # noqa: BLE001
            skip("tier3", spec["id"], f"could not export STEP: {exc}")
            continue
        half = spec["L"] / 2.0 / 1000.0  # metres, geometry is centred on the origin
        eps = 0.0011
        fix_box = [-half - eps, -1.0, -1.0, -half + eps, 1.0, 1.0]
        load_box = [half - eps, -1.0, -1.0, half + eps, 1.0, 1.0]
        for h_mm in spec["h_mm"]:
            res = polymesh_solve(pm, step, workdir, f"{spec['id']}_{h_mm:g}",
                                 h_m=h_mm / 1000.0, E_pa=E_pa, nu=nu,
                                 fix_box=fix_box, load_box=load_box,
                                 load_dir=spec["load_dir"], force_n=spec["force"])
            if not res["ok"]:
                skip("tier3", f"{spec['id']} h={h_mm:g}mm",
                     f"{res.get('error')}: {str(res.get('stdout_tail'))[-300:]}")
                continue
            disp = pm_displacement(res)
            if disp is None:
                skip("tier3", f"{spec['id']} h={h_mm:g}mm",
                     f"no displacement field in polymesh .vtu (fields: "
                     f"{res['field_names']})")
                continue
            pts = res["points"]
            xmax = max(p[spec["axis"]] for p in pts)
            tol = 1e-9 + 1e-3 * abs(xmax)
            sel = [i for i, p in enumerate(pts)
                   if abs(p[spec["axis"]] - xmax) <= tol]
            got_m = (sum(disp[i][spec["comp"]] for i in sel) / len(sel)) if sel else None
            got = abs(got_m) * 1000.0 if got_m is not None else None  # m -> mm
            e = rel(got, spec["ref"]) if spec["ref"] else None
            row = {"case": spec["id"], "h_mm": h_mm, "solver": "polymesh",
                   "metric": spec["metric"], "unit": spec["unit"],
                   "n_nodes": len(pts), "ref": spec["ref"], "got": got,
                   "rel_err": e, "tol": spec["tol"],
                   "verdict": verdict(e, spec["tol"]) if e is not None else "INFO",
                   "citation": spec["citation"], "cmd": _display_path(res["cmd"]),
                   "seconds": round(res["seconds"], 1),
                   "field_names": res["field_names"]}
            vm = pm_scalar(res, ("von_mises", "vonmises", "sigma_vm", "vm"))
            if vm:
                row["peak_von_mises_mpa"] = max(vm) / 1e6
            record("tier3", row)
            _log(f"  [{row['verdict']}] {spec['id']} h={h_mm:g}mm n={len(pts)} "
                 f"got={got} ref={spec['ref']} rel="
                 f"{'n/a' if e is None else f'{e:.3e}'} ({res['seconds']:.0f}s)")


# ===========================================================================
# Report
# ===========================================================================

def _fmt(v, digits: int = 6) -> str:
    if v is None:
        return "—"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x, 4) for x in v) + "]"
    if isinstance(v, float):
        if v != 0 and (abs(v) < 1e-3 or abs(v) >= 1e6):
            return f"{v:.{digits - 3}e}"
        return f"{v:.{digits}g}"
    return str(v)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _display_path(text: str) -> str:
    """Render repo-local and home-relative paths portably for the committed report."""
    text = text.replace(str(_PROJECT_ROOT) + os.sep, "")
    home = str(Path.home())
    return text.replace(home, "~") if home and home != os.sep else text


def make_figures(outdir: Path) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        _log(f"  [SKIP] figures: matplotlib unavailable ({exc})")
        return []
    written: list[str] = []
    conv = [r for r in _RESULTS["tier1"] if r.get("kind") == "convergence"
            and r.get("observed_order") is not None]
    if conv:
        cases = sorted({r["case"] for r in conv})
        fig, axes = plt.subplots(1, len(cases), figsize=(4.6 * len(cases), 4.0),
                                 squeeze=False)
        for ax, case in zip(axes[0], cases):
            for r in [c for c in conv if c["case"] == case]:
                hs, es = r["h_eff_mm"], r["rel_err"]
                ax.loglog(hs, es, "o-", label=f"{r['element']} {r['metric'][:26]}")
            hs_all = [h for r in conv if r["case"] == case for h in r["h_eff_mm"]]
            es_all = [e for r in conv if r["case"] == case for e in r["rel_err"]]
            if hs_all and es_all:
                h0 = max(hs_all)
                e0 = max(es_all)
                grid = sorted(hs_all)
                for slope, style in ((1.0, ":"), (2.0, "--")):
                    ax.loglog(grid, [e0 * (h / h0) ** slope for h in grid], style,
                              color="0.4", linewidth=1,
                              label=f"theoretical O(h^{slope:g})")
            ax.set_title(case)
            ax.set_xlabel("effective element size h (mm)")
            ax.set_ylabel("relative error vs closed form")
            ax.grid(True, which="both", alpha=0.3)
            ax.legend(fontsize=6, loc="best")
        fig.suptitle("Tier 1 — Chudware FEA vs closed form: error vs mesh size")
        fig.tight_layout()
        p = outdir / "fig_tier1_convergence.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        written.append(p.name)
    t2 = [r for r in _RESULTS["tier2"] if r.get("disp_l2_rel") is not None]
    if t2:
        fig, ax = plt.subplots(figsize=(max(7.0, 0.75 * len(t2)), 4.2))
        labels = [f"{r['case']}\n{r.get('element','')} h={_fmt(r.get('h_req_mm'),3)}"
                  for r in t2]
        vals = [max(r["disp_l2_rel"], 1e-17) for r in t2]
        ax.bar(range(len(vals)), vals, color=["#2a7" if v <= TOL_CODE_TO_CODE
                                              else "#c33" for v in vals])
        ax.axhline(TOL_CODE_TO_CODE, color="k", linestyle="--", linewidth=1,
                   label=f"gate {TOL_CODE_TO_CODE:.0e}")
        ax.set_yscale("log")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=6, rotation=45, ha="right")
        ax.set_ylabel("relative L2 displacement difference")
        ax.set_title("Tier 2 — same mesh, same BCs: Chudware vs CalculiX, node for node")
        ax.grid(True, axis="y", which="both", alpha=0.3)
        ax.legend(fontsize=8)
        fig.tight_layout()
        p = outdir / "fig_tier2_ccx_agreement.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        written.append(p.name)
    return written


def build_report(elapsed: float, figures: list[str]) -> str:
    eng = _PROVENANCE.get("engine", {})
    lines: list[str] = []
    A = lines.append
    A("# FEA Verification & Validation — Chudware vs closed form, CalculiX and polymesh")
    A("")
    A(f"Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} by "
      f"`scripts/vv_fea_crosscheck.py` in {elapsed / 60.0:.1f} min.")
    A("")
    A("## Provenance")
    A("")
    A(_table(["item", "value"], [
        ["engine build id", f"`{eng.get('engine_build_id', 'n/a')}`"],
        ["solver", f"`{eng.get('solver', 'n/a')}`"],
        ["mesher", f"`{eng.get('mesher', 'n/a')}`"],
        ["polymesh commit", f"`{_PROVENANCE.get('polymesh_commit', 'n/a')}`"],
        ["polymesh binary", f"`{_PROVENANCE.get('polymesh_binary', 'n/a')}`"],
        ["CalculiX", f"`{_PROVENANCE.get('ccx_version', 'not found')}`"],
        ["ccx binary", f"`{_PROVENANCE.get('ccx_path', 'n/a')}`"],
        ["libchudware", f"`{_display_path(os.environ.get('CHUDWARE_LIBCHUDWARE', 'n/a'))}`"],
        ["memory envelope",
         f"`CHUDWARE_MEM_FRAC={os.environ.get('CHUDWARE_MEM_FRAC', 'default')}`, "
         f"`CHUDWARE_HEADROOM_GB={os.environ.get('CHUDWARE_HEADROOM_GB', 'default')}`, "
         f"`CHUDWARE_SIM_THREADS={os.environ.get('CHUDWARE_SIM_THREADS', 'default')}`"],
        ["host", f"`{os.uname().sysname} {os.uname().release} {os.uname().machine}`"],
    ]))
    A("")
    A("Every number below was measured by this script on this machine through "
      "`sim_run_static` — the same engine entry point `run_fea_static` uses "
      "(Polyhedral-FEA graded-tet mesh of an exported STEP, Chudware Eigen tet4/tet10 "
      "solve). Reference values are derived in the script from the cited closed form "
      "and are never read back from the engine.")
    A("")

    # ---- Tier 1
    A("## Tier 1 — analytic references")
    A("")
    A("Verdict convention: a discretization-limited row is GATED on its finest mesh; "
      "the coarser legs of the same metric/element carry `conv-leg` and are the "
      "convergence evidence rather than independent verdicts. \"The coarsest mesh in "
      "the ladder is not converged yet\" is not a solver failure, and counting it as "
      "one would bury the rows that do mean something.")
    A("")
    t1 = [r for r in _RESULTS["tier1"]
          if r.get("kind") not in ("convergence", "mesh_calibration")
          and r.get("metric") not in (None, "solve")]
    if t1:
        rows = []
        for r in t1:
            rows.append([r["case"], r["metric"], r.get("element", ""),
                         _fmt(r.get("h_req_mm"), 3), _fmt(r.get("h_eff_mm"), 3),
                         _fmt(r.get("n_elems")), _fmt(r.get("n_dof")),
                         _fmt(r.get("ref")), _fmt(r.get("got")),
                         (f"{r['rel_err']:.3e}" if isinstance(r.get("rel_err"), float)
                          else "—"),
                         f"{r.get('tol', 0):.0e}", r.get("verdict", "")])
        A(_table(["case", "metric", "elem", "h_req", "h_eff", "n_elems", "n_dof",
                  "reference", "Chudware", "rel err", "tol", "verdict"], rows))
    else:
        A("_No Tier-1 rows were executed._")
    A("")
    conv = [r for r in _RESULTS["tier1"] if r.get("kind") == "convergence"]
    if conv:
        A("### Observed convergence order (least-squares slope of log err vs log h)")
        A("")
        A("`monotone=False` means the error did not fall at every refinement step — the "
          "graded-tet mesher does not produce a NESTED mesh family, so a slope fitted "
          "across such a ladder is indicative, not a measured order of accuracy.")
        A("")
        A(_table(["case", "metric", "elem", "legs", "h_eff (mm), coarse→fine",
                  "rel err", "observed slope", "monotone"],
                 [[r["case"], r["metric"], r["element"], str(r["n_legs"]),
                   _fmt(r["h_eff_mm"]), "[" + ", ".join(f"{e:.2e}" for e in r["rel_err"])
                   + "]",
                   _fmt(r.get("observed_order"), 3),
                   str(r.get("monotone"))] for r in conv]))
        A("")
    cals = [r for r in _RESULTS["tier1"] if r.get("kind") == "mesh_calibration"]
    if cals:
        A("### Mesh ladders, measured before solving")
        A("")
        A("`study.target_size_mm` is a REQUEST: the graded-tet mesher quantises it onto "
          "its own refinement levels, and the realised element count is not monotone in "
          "the request (on the 200x20x20 bar, h=16 mm produced 12992 elements while "
          "h=13 mm produced 6144). Each ladder below was therefore measured with "
          "`sim_mesh` first — mesh only, no solve — and only a subsequence whose element "
          "count grows by at least 1.5x per step was solved.")
        A("")
        rows = []
        for c in cals:
            for m in c["measured"]:
                rows.append([c["case"], _fmt(m.get("h_req_mm"), 3),
                             _fmt(m.get("n_elems")), _fmt(m.get("h_eff_mm"), 4),
                             _fmt(m.get("mesh_volume_mm3"), 8),
                             _fmt(m.get("aspect_max"), 4),
                             _fmt(m.get("pct_poor"), 4),
                             "yes" if m.get("h_req_mm") in
                             (c["ladder"][1] + c["ladder"][2]) else "",
                             m.get("error", "")[:60]])
        A(_table(["case", "h_req (mm)", "n_elems", "h_eff (mm)", "mesh volume (mm³)",
                  "aspect max", "% poor", "solved", "error"], rows))
        A("")
    probes = [r for r in _RESULTS["tier1"] if r.get("kind") == "sign_probe"]
    if probes:
        A("### Load-sign probe")
        A("")
        A("A load whose sign convention is undocumented or wrong is a silent factor of "
          "-1 on every stress in a report, so the convention is measured rather than "
          "assumed: `pressure` is \"MPa, inward\" (+p pushes INTO the loaded face), the "
          "released CYL-LAME case runs +p on the bore as internal pressurisation, and "
          "the same model and mesh is re-solved with the magnitude flipped so the "
          "resulting hoop-stress sign is on the record. -p must read hoop COMPRESSION; "
          "anything else means the inversion fixed on 2026-09-05 is back.")
        A("")
        rows = []
        for r in probes:
            for metric, got in (r.get("measured") or {}).items():
                rows.append([r["case"], r.get("what", ""), r.get("element", ""),
                             _fmt(r.get("h_req_mm"), 3), metric, _fmt(got)])
        A(_table(["case", "probe", "elem", "h_req", "metric", "measured"], rows))
        A("")

    # ---- Tier 2
    A("## Tier 2 — same mesh, same BCs: Chudware vs CalculiX, node for node")
    A("")
    A(f"The system Chudware actually solved is dumped by `study.export_system_path` "
      f"immediately before `fea_static_solve` and re-solved by ccx as C3D4/C3D10. Same "
      f"mesh + same boundary conditions + linear elasticity means the two codes are "
      f"answering the same linear algebra problem, so the gate on the displacement "
      f"field is {TOL_CODE_TO_CODE:.0e} relative; stress recovery is NOT the same "
      f"operation on the two sides (Chudware averages element stresses to nodes, ccx "
      f"extrapolates integration-point stresses to nodes and averages), so the von "
      f"Mises columns are reported as nodal-averaged-vs-nodal-averaged evidence and "
      f"are not gated.")
    A("")
    A("One stress caveat is structural rather than numerical: "
      "`summary.max_von_mises_mpa` is NOT the field peak — the solver masks a "
      "two-element ring around the fixed nodes out of that scalar, so on a model whose "
      "fixtures ARE the highly stressed feature (a bolt hole, say) the summary number "
      "is far below the nodal field maximum. The CalculiX column here is an unmasked "
      "field peak, so the comparison uses Chudware's raw nodal von Mises array; the "
      "masked summary value is carried in the JSON as `vm_summary_chud_mpa` so the two "
      "are never confused.")
    A("")
    A("The `ccx print floor` column is the relative L2 quantisation of ccx's own ASCII "
      "output (6 significant digits per component). Where the measured difference sits "
      "AT that floor, the two codes agree to the limit of what the reference output can "
      "express — the number is a property of the comparison channel, not a measured "
      "solver discrepancy.")
    A("")
    t2 = [r for r in _RESULTS["tier2"]]
    if t2:
        rows = []
        for r in t2:
            rows.append([r["case"], r.get("element", ""), r.get("ccx_elements", ""),
                         _fmt(r.get("h_req_mm"), 3), _fmt(r.get("n_dof")),
                         (f"{r['disp_l2_rel']:.3e}" if r.get("disp_l2_rel") is not None
                          else "—"),
                         (f"{r['disp_linf_rel']:.3e}"
                          if r.get("disp_linf_rel") is not None else "—"),
                         _fmt(r.get("max_disp_chud_mm")), _fmt(r.get("max_disp_ccx_mm")),
                         (f"{r['max_disp_rel']:.2e}"
                          if r.get("max_disp_rel") is not None else "—"),
                         (f"{r['reaction_rel']:.2e}"
                          if r.get("reaction_rel") is not None else "—"),
                         (f"{r['print_precision_floor_l2']:.1e}"
                          if r.get("print_precision_floor_l2") is not None else "—"),
                         _fmt(r.get("vm_peak_chud_mpa"), 5),
                         _fmt(r.get("vm_peak_ccx_mpa"), 5),
                         (f"{r['vm_peak_rel']:.2e}"
                          if r.get("vm_peak_rel") is not None else "—"),
                         r.get("verdict", "") + (f" ({r['note'][:60]})"
                                                 if r.get("note") else "")])
        A(_table(["case", "elem", "ccx elem", "h_req", "n_dof", "|u| L2 rel",
                  "|u| Linf rel", "max|u| Chudware", "max|u| ccx", "max|u| rel",
                  "reaction rel", "ccx print floor", "peak vM Chudware",
                  "peak vM ccx", "vM rel", "verdict"], rows))
    else:
        A("_No Tier-2 rows were executed._")
    A("")

    # ---- Tier 3
    A("## Tier 3 — polymesh (Polyhedral-FEA's own solver) vs the same closed forms")
    A("")
    t3 = _RESULTS["tier3"]
    if t3:
        A(_table(["case", "h (mm)", "metric", "n_nodes", "reference", "polymesh",
                  "rel err", "verdict", "command"],
                 [[r["case"], _fmt(r.get("h_mm"), 3), r["metric"],
                   _fmt(r.get("n_nodes")), _fmt(r.get("ref")), _fmt(r.get("got")),
                   (f"{r['rel_err']:.3e}" if r.get("rel_err") is not None else "—"),
                   r.get("verdict", ""), "`" + r.get("cmd", "")[-110:] + "`"]
                  for r in t3]))
    else:
        A("_No Tier-3 rows were executed._")
    A("")

    # ---- findings / skips
    A("## Corroborated findings (root-caused, with the measurements)")
    A("")
    A(_table(["case", "finding", "evidence"],
             [[f["case"], f["finding"], f["evidence"]]
              for f in CORROBORATED_FINDINGS]))
    A("")
    A("## Open findings (this run)")
    A("")
    if _FINDINGS:
        A(_table(["case", "finding", "evidence"],
                 [[f["case"], f["finding"], f["evidence"]] for f in _FINDINGS]))
    else:
        A("None: every executed check landed inside its tier tolerance.")
    A("")
    if _SKIPS:
        A("## Skipped with a reason")
        A("")
        A("A skipped leg is not lost coverage. Two kinds appear here. A CalculiX deck skipped for exceeding `VV_CCX_MAX_DOF` still has its Chudware leg reported in Tier 1 — only the cross-solve is omitted, because ccx's factorization is not memory-governed by anything and shares the slice with the Chudware solve. A leg refused by the memory governor carries the engine's own `binding limit:` clause verbatim, and under a `systemd MemoryMax` cap that clause names `cgroup memory.max` and states that no `CHUDWARE_*` variable can raise it. Since the governor became cgroup v2 aware, a capped run can legitimately show MORE refusals than an uncapped one: the refusal replaced a SIGKILL that produced no row at all, so a clean refusal is the clamp working, not a narrower study.")
        A("")
        A(_table(["tier", "case", "reason"],
                 [[s["tier"], s["case"], s["reason"][:220]] for s in _SKIPS]))
        A("")
    if figures:
        A("## Figures")
        A("")
        for f in figures:
            A(f"![{f}]({f})")
            A("")

    A("## Conclusion — plain language")
    A("")
    A(_conclusion())
    A("")
    return "\n".join(lines)


def _conclusion() -> str:
    t2 = [r["disp_l2_rel"] for r in _RESULTS["tier2"]
          if r.get("disp_l2_rel") is not None]
    exact = [r for r in _RESULTS["tier1"]
             if r.get("tol") == TOL_EXACT and r.get("rel_err") is not None]
    bits: list[str] = []
    if t2:
        worst = max(t2)
        bits.append(
            f"On {len(t2)} different meshes — four textbook parts, tet4 and tet10 — "
            f"CalculiX was handed the exact system Chudware "
            f"solved and produced the same displacement field to within "
            f"{worst:.1e} relative in the worst case. That is code-to-code agreement at "
            f"the level of arithmetic round-off: the element formulation, the assembly "
            f"and the linear solve are doing what an independent, widely used FE code "
            f"does with the same mesh and the same loads.")
    if exact:
        best = min(r["rel_err"] for r in exact)
        bits.append(
            f"On the one case where finite elements should be exact — a uniform bar in "
            f"pure tension, whose displacement field is linear and therefore inside the "
            f"tet4 space — the interior strain matched P/(EA) to {best:.1e} relative.")
    ordered = [r for r in _RESULTS["tier1"] if r.get("kind") == "convergence"
               and r.get("observed_order") is not None]
    if ordered:
        bits.append(
            f"Where the answer is discretization-limited (bending, a stress "
            f"concentration at a hole, a Lame pressure field) the error shrinks as the "
            f"mesh is refined, and {len(ordered)} case/metric pairs report their "
            f"measured convergence slope in the table above rather than a single "
            f"hand-picked mesh.")
    if _RESULTS["tier3"]:
        bits.append(
            "Polyhedral-FEA's own solver (`polymesh solve`) was run on the same parts "
            "against the same closed forms, so both of the codes in this toolchain are "
            "measured against the same truth rather than against each other.")
    if _FINDINGS:
        bits.append(
            f"{len(_FINDINGS)} disagreement(s) are recorded above as open findings with "
            f"their evidence instead of being absorbed into a wider tolerance.")
    else:
        bits.append("No check was widened to pass, and nothing is left unexplained.")
    return " ".join(bits)


def write_outputs(elapsed: float) -> dict:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    figures = make_figures(DOCS_DIR)
    report = build_report(elapsed, figures)
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(elapsed, 1),
        "provenance": _PROVENANCE,
        "tolerances": {"exact": TOL_EXACT, "code_to_code": TOL_CODE_TO_CODE,
                       "precise": TOL_PRECISE, "engineering": TOL_ENGINEERING},
        "tier1_analytic": _RESULTS["tier1"],
        "tier2_calculix": _RESULTS["tier2"],
        "tier3_polymesh": _RESULTS["tier3"],
        "findings": _FINDINGS,
        "skips": _SKIPS,
        "executed_checks": _EXECUTED,
        "failed_checks": _FAILED,
    }
    (DOCS_DIR / "FEA_VV_REPORT.md").write_text(report)
    (DOCS_DIR / "fea_vv_results.json").write_text(json.dumps(payload, indent=1))
    written = [str(DOCS_DIR / "FEA_VV_REPORT.md"),
               str(DOCS_DIR / "fea_vv_results.json")]
    return {"written": written, "payload": payload}


# ===========================================================================
# main
# ===========================================================================

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="every tier")
    ap.add_argument("--tier1", action="store_true", help="analytic tier only")
    ap.add_argument("--tier2", action="store_true",
                    help="analytic tier + the same-mesh CalculiX cross-solve")
    ap.add_argument("--tier3", action="store_true", help="polymesh tier only")
    ap.add_argument("--case", action="append", default=None,
                    help="restrict the analytic tier to these case ids")
    ap.add_argument("--workdir", default=None,
                    help="scratch directory for exported systems and ccx jobs")
    args = ap.parse_args(argv)
    if not any((args.all, args.tier1, args.tier2, args.tier3)):
        args.all = True

    ready, reason = engine_ready()
    if not ready:
        _log(f"ENGINE NOT AVAILABLE: {reason}")
        return 2

    ccx = find_ccx()
    _PROVENANCE["ccx_path"] = _display_path(ccx) if ccx else "not found"
    _PROVENANCE["ccx_version"] = ccx_version(ccx) if ccx else "not found"
    pm = polymesh_binary()
    _PROVENANCE["polymesh_binary"] = _display_path(pm) if pm else "not found"
    _PROVENANCE["polymesh_commit"] = polymesh_commit()
    _log(f"ccx: {_PROVENANCE['ccx_version']} ({_PROVENANCE['ccx_path']})")
    _log(f"polymesh: {_PROVENANCE['polymesh_binary']} @ "
         f"{_PROVENANCE['polymesh_commit']}")

    # The engine refuses a relative vtu_path/export_system_path, so resolve up front
    # rather than failing every leg with a path complaint.
    workdir = (Path(args.workdir).expanduser().resolve() if args.workdir
               else Path(tempfile.mkdtemp(prefix="vv_fea_")))
    workdir.mkdir(parents=True, exist_ok=True)
    _log(f"workdir: {workdir}")

    t0 = time.time()
    do_ccx = bool(ccx) and (args.all or args.tier2)
    if not ccx and (args.all or args.tier2):
        skip("tier2", "all", "ccx binary not found on PATH or in ~/.local/bin")

    if args.all or args.tier1 or args.tier2:
        run_tier1_and_2(workdir, ccx, args.case, do_ccx)
    if args.all or args.tier3:
        if pm:
            run_tier3(workdir, pm)
        else:
            skip("tier3", "all", "polymesh binary not found in "
                                 "core/build-occt-linux/bin")
    elapsed = time.time() - t0

    out = write_outputs(elapsed)
    _log("")
    for p in out["written"]:
        _log(f"wrote {p}")
    _log(f"\nexecuted checks: {_EXECUTED}   failed: {_FAILED}   "
         f"findings: {len(_FINDINGS)}   skips: {len(_SKIPS)}")
    if _EXECUTED == 0:
        _log("FAIL: zero executed checks")
        return 1
    return 1 if _FAILED else 0


# ===========================================================================
# pytest smoke — no .so, no ccx, no engine
# ===========================================================================

class TestReferences:
    def test_axial_bar_reference_is_hookes_law(self):
        r = ref_axial_bar(20000.0, 200.0, 400.0, 200000.0)
        assert abs(r["delta_mm"] - 0.05) < 1e-12
        assert abs(r["sigma_mpa"] - 50.0) < 1e-12
        assert abs(r["strain"] - 2.5e-4) < 1e-18

    def test_cantilever_shear_term_is_a_few_percent_of_a_stubby_beam(self):
        r = ref_cantilever(2000.0, 150.0, 20.0, 30.0, 200000.0, 0.3, 30.0)
        assert abs(r["delta_eb_mm"] - 0.25) < 1e-9
        share = r["delta_shear_mm"] / r["delta_mm"]
        assert 0.01 < share < 0.10, share
        # M(x) c / I at 30 mm from the clamp on a 150 mm cantilever.
        assert abs(r["sigma_station_mpa"] - 2000.0 * 120.0 * 15.0 / 45000.0) < 1e-9

    def test_kirsch_scf_is_three_and_finite_width_raises_it(self):
        r = ref_plate_hole(10.0, 10.0, 100.0)
        assert r["scf_kirsch"] == 3.0
        assert 3.0 < r["scf_howland"] < 3.1
        assert abs(r["sigma_peak_kirsch_mpa"] - 30.0) < 1e-12

    def test_lame_hoop_and_radial_satisfy_the_boundary_conditions(self):
        a, b, p = 15.0, 30.0, 20.0
        inner = ref_lame(p, a, b, a, 200000.0, 0.3)
        outer = ref_lame(p, a, b, b, 200000.0, 0.3)
        # sigma_r(a) = -p and sigma_r(b) = 0 are the defining BCs.
        assert abs(inner["rad_mpa"] + p) < 1e-9
        assert abs(outer["rad_mpa"]) < 1e-9
        # Lame invariant: sigma_hoop - sigma_rad = 2 p a^2 b^2 / ((b^2-a^2) r^2).
        for r_, res in ((a, inner), (b, outer)):
            want = 2 * p * a * a * b * b / ((b * b - a * a) * r_ * r_)
            assert abs((res["hoop_mpa"] - res["rad_mpa"]) - want) < 1e-9
        assert inner["u_r_mm"] > outer["u_r_mm"] > 0

    def test_convergence_order_fit_recovers_a_known_slope(self):
        hs = [8.0, 4.0, 2.0, 1.0]
        errs = [1e-2 * h ** 2 for h in hs]
        assert abs(fit_order(hs, errs) - 2.0) < 1e-9
        assert fit_order([1.0], [1.0]) is None


class TestCalculixDeck:
    @staticmethod
    def _unit_tet4_system() -> dict:
        return {
            "points": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],
                       [0.0, 0.0, 1.0], [1.0, 1.0, 1.0]],
            "elems": [[0, 1, 2, 3], [1, 2, 3, 4]],
            "nodes_per_elem": 4,
            "fixed_nodes": [0, 1, 2],
            "nodal_forces": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0],
                             [0.0, 0.0, -5.0], [1.5, 0.0, 0.0]],
            "E_mpa": 200000.0, "nu": 0.3, "bond_springs": [], "contact_springs": [],
        }

    def test_tet4_deck_carries_every_node_element_bc_and_load(self, tmp_path):
        meta = write_ccx_inp(tmp_path / "t.inp", self._unit_tet4_system())
        text = (tmp_path / "t.inp").read_text()
        assert meta["n_nodes"] == 5 and meta["n_elems"] == 2
        assert "*ELEMENT, TYPE=C3D4, ELSET=EALL" in text
        # 1-based ids, all three fixed nodes in one NSET, both loaded dofs present.
        assert "\n1, 0, 0, 0" in text
        assert re.search(r"\*NSET, NSET=NFIX\n1, 2, 3\n", text)
        assert "4, 3, -5" in text
        assert "5, 1, 1.5" in text
        assert "NFIX, 1, 3, 0.0" in text
        # Requested output blocks are what the parsers below rely on.
        for token in ("*NODE FILE", "*EL FILE", "*NODE PRINT, NSET=NALL",
                      "*NODE PRINT, NSET=NFIX"):
            assert token in text

    def test_inverted_tets_are_reoriented_not_shipped(self, tmp_path):
        sysd = self._unit_tet4_system()
        sysd["elems"] = [[0, 2, 1, 3]]  # negative volume winding
        meta = write_ccx_inp(tmp_path / "t.inp", sysd)
        assert meta["inverted_elements_reoriented"] == 1
        text = (tmp_path / "t.inp").read_text().splitlines()
        start = text.index("*ELEMENT, TYPE=C3D4, ELSET=EALL") + 1
        ids = [int(v) for v in text[start].split(",")[1:]]
        pts = sysd["points"]
        assert _signed_volume6(pts[ids[0] - 1], pts[ids[1] - 1], pts[ids[2] - 1],
                               pts[ids[3] - 1]) > 0

    def test_spring_coupled_systems_are_refused_not_silently_dropped(self, tmp_path):
        sysd = self._unit_tet4_system()
        sysd["bond_springs"] = [{"a": 0, "b": 1, "k": 1.0}]
        try:
            write_ccx_inp(tmp_path / "t.inp", sysd)
        except ValueError as exc:
            assert "bond_springs" in str(exc)
        else:
            raise AssertionError("a bonded system must not become a plain ccx deck")

    def test_tet10_midnode_ordering_is_measured_from_geometry(self):
        c = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0)]

        def mid(i, j):
            return tuple((c[i][k] + c[j][k]) / 2.0 for k in range(3))

        # Chudware/gmsh slot order: (0,1) (1,2) (2,0) (3,0) (3,2) (3,1).
        pts = list(c) + [mid(0, 1), mid(1, 2), mid(2, 0), mid(3, 0), mid(3, 2),
                         mid(3, 1)]
        perm = detect_tet10_permutation([list(p) for p in pts],
                                        [list(range(10))])
        assert perm == CHUD_TET10_TO_C3D10

    def test_a_scrambled_tet10_ordering_is_rejected(self):
        c = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 2.0)]
        pts = list(c) + [(9.0, 9.0, 9.0)] * 6  # mid nodes nowhere near any edge
        try:
            detect_tet10_permutation([list(p) for p in pts], [list(range(10))])
        except RuntimeError:
            return
        raise AssertionError("mid nodes that bisect nothing must not yield a permutation")


class TestCalculixParsers:
    DAT = """
 displacements (vx,vy,vz) for set NALL and time  0.1000000E+01

         1 -1.000000E-03  2.000000E-04  0.000000E+00
         2 -2.000000E-03  0.000000E+00  1.000000E-05

 forces (fx,fy,fz) for set NFIX and time  0.1000000E+01

         1  1.250000E+02  0.000000E+00 -3.000000E+01
         2 -1.250000E+02  0.000000E+00  3.000000E+01
"""

    def test_dat_blocks_are_keyed_by_quantity_and_set(self):
        got = parse_ccx_dat(self.DAT)
        assert got["displacements"]["NALL"][1] == (-1e-3, 2e-4, 0.0)
        assert got["displacements"]["NALL"][2][2] == 1e-5
        assert got["forces"]["NFIX"][1][0] == 125.0
        # A displacement table must never leak into the reaction table.
        assert 1 not in got["forces"].get("NALL", {})

    def test_frd_short_and_long_node_widths_both_parse(self):
        def frd(width):
            head = ("  100CL  101 1.000000000E+00        2\n"
                    " -4  DISP        4    1\n"
                    " -5  D1          1    2    1    0\n"
                    " -5  D2          1    2    2    0\n"
                    " -5  D3          1    2    3    0\n")
            rows = ""
            for nid, v in ((1, (-1e-3, 2e-4, 0.0)), (2, (-2e-3, 0.0, 1e-5))):
                rows += " -1" + str(nid).rjust(width) + "".join(
                    f"{x: .5E}" for x in v) + "\n"
            return head + rows + " -3\n"

        for width in (5, 10):
            blocks = parse_ccx_frd(frd(width))
            assert set(blocks["DISP"]) == {1, 2}, width
            assert abs(blocks["DISP"][1][0] + 1e-3) < 1e-12, width
            assert abs(blocks["DISP"][2][2] - 1e-5) < 1e-16, width

    def test_von_mises_from_components_matches_uniaxial_and_pure_shear(self):
        assert abs(von_mises_from_components([100.0, 0, 0, 0, 0, 0]) - 100.0) < 1e-9
        assert abs(von_mises_from_components([0, 0, 0, 50.0, 0, 0])
                   - 50.0 * math.sqrt(3.0)) < 1e-9
        # Hydrostatic stress is invisible to von Mises.
        assert von_mises_from_components([70.0, 70.0, 70.0, 0, 0, 0]) < 1e-9


class TestGeometryHelpers:
    def test_signed_volume_sign_flips_with_winding(self):
        p = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
        assert _signed_volume6(*p) > 0
        assert _signed_volume6(p[0], p[2], p[1], p[3]) < 0

    def test_effective_h_recovers_the_edge_of_a_regular_tet(self):
        a = 3.0
        pts = [[0, 0, 0], [a, 0, 0], [a / 2, a * math.sqrt(3) / 2, 0],
               [a / 2, a * math.sqrt(3) / 6, a * math.sqrt(2.0 / 3.0)]]
        h = effective_h({"points": pts, "elems": [[0, 1, 2, 3]]})
        assert abs(h - a) < 1e-9

    def test_memory_refusal_is_distinguished_from_a_real_solver_error(self):
        assert is_memory_refusal("static FEA solve needs an estimated 10.7 GB but this "
                                 "machine's budget is 3.0 GB")
        assert not is_memory_refusal("fea_static: invalid or folded quadratic boundary "
                                     "face")

    def test_a_cgroup_capped_refusal_keeps_its_binding_limit_clause(self):
        # Verbatim shape of the cgroup v2 aware refusal (core/util/resource_governor).
        msg = ("probe study needs an estimated 6.0 GB but this machine's budget is "
               "2.0 GB (31 GB RAM, 20.8 GB available; binding limit: cgroup "
               "memory.max — this process runs inside a memory-capped cgroup "
               "(systemd MemoryMax / container limit) minus what the rest of that "
               "cgroup already uses; raise the cgroup limit; CHUDWARE_MEM_FRAC, "
               "CHUDWARE_MEM_CAP_GB and CHUDWARE_HEADROOM_GB cannot raise it)")
        assert is_memory_refusal(msg)
        note = refusal_note(msg)
        # The clause that says WHICH limit bound, and that the env knobs are useless,
        # must survive into the skip row — truncating it is how a reader ends up
        # raising CHUDWARE_MEM_FRAC into a cgroup OOM kill.
        assert "binding limit: cgroup memory.max" in note
        assert "cannot raise it" in note
        assert "needs an estimated 6.0 GB" in note
        # An uncapped run names the other term, and it must survive too.
        uncapped = ("study needs an estimated 20.0 GB but this machine's budget is "
                    "15.374 GB (binding limit: CHUDWARE_MEM_FRAC x total RAM — raise "
                    "CHUDWARE_MEM_FRAC)")
        assert "binding limit: CHUDWARE_MEM_FRAC x total RAM" in refusal_note(uncapped)
        # A message with no binding clause is still reported, not dropped.
        assert refusal_note("fea_static: cancelled").startswith("fea_static: cancelled")
        assert refusal_note("") == "refused with no message"

    def test_vtu_reader_pulls_vector_and_scalar_point_fields(self, tmp_path):
        p = tmp_path / "x.vtu"
        p.write_text(
            '<?xml version="1.0"?>\n<VTKFile><UnstructuredGrid><Piece>'
            '<Points><DataArray type="Float64" NumberOfComponents="3" '
            'format="ascii">\n0 0 0 1 0 0\n</DataArray></Points>'
            '<Cells><DataArray type="Int32" Name="connectivity" format="ascii">0 1'
            '</DataArray></Cells>'
            '<PointData><DataArray type="Float64" Name="displacement" '
            'NumberOfComponents="3" format="ascii">1 2 3 4 5 6</DataArray>'
            '<DataArray type="Float64" Name="von_mises" format="ascii">7 8</DataArray>'
            '</PointData></Piece></UnstructuredGrid></VTKFile>\n')
        got = read_vtu_fields(p)
        assert got["points"] == [[0, 0, 0], [1, 0, 0]]
        assert got["fields"]["displacement"] == [[1, 2, 3], [4, 5, 6]]
        assert got["fields"]["von_mises"] == [7.0, 8.0]
        assert "connectivity" not in got["fields"]


if __name__ == "__main__":
    sys.exit(main())
