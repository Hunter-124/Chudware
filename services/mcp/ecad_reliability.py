"""ECAD -> vibration reliability screening (T-FIN21) — one call from a routed board to
a mounted-board modal solve + excitation-band flagging + a deflection-based solder-joint
risk screen, conforming to ``aicad.simhandoff/1``.

Same design contract as ``ecad_thermal`` (T-FIN16): JSON-string results with
``{ok, ..., warnings, hint, next_actions}``, engine helpers called directly, pure
helpers module-level and engine-free so tests can drive them without OCP/libaicad.

Tool:
  ecad_vibe_check   board -> fused board+components FEA solid (T-FIN16's
                    engine.ecad.threed.board_to_fea_step, reused as-is) -> mount-hole
                    detection (mounting-hole footprints / NPTH pads / user-passed
                    positions) -> fixed BCs at the mounts -> core modal solve
                    (sim_run_modal) -> natural frequencies flagged against the
                    excitation band (default 20-2000 Hz random-vibe screening) ->
                    Miles'-equation response at the worst in-band mode -> board
                    deflection estimate -> Steinberg 3-sigma deflection-limit screen
                    per component (HEURISTIC, cited — not a life prediction).

THE MASS MODEL (documented honesty): the fused FEA solid flattens component bodies to
thin caps (see engine.ecad.threed._FEA_MAX_BODY_H_MM) and would otherwise carry them
at the board's homogenized density. We therefore estimate the REAL masses — board =
homogenized rho x substrate volume; each component = full package dims x a
mold-compound density assumption (or explicit eng mass metadata where present) — and
scale ONE effective density so the fused solid carries the estimated total mass. Mass
is globally correct; its spatial distribution is approximate (heavy parts still count
extra where their fused caps sit, but at the shared density). Every number and
assumption is echoed in ``mass_model`` / ``assumptions``.

THE DEFLECTION SCREEN (heuristic, cited): Steinberg's classic 3-sigma single-amplitude
deflection limit for ~20e6 random-vibration stress cycles,

    Z_allow(in) = 0.00022 * B / (C * h * r * sqrt(L))     [inputs in inches]

with B = board edge length parallel to the component, h = board thickness, L =
component length, r = relative position factor (1.0 at the board center) and C = a
component-packaging constant. The estimated board deflection comes from the classic
Z(in) = 9.8 * G / f^2 relation at the worst in-band mode's Miles'-equation GRMS
response (x3 for 3-sigma). THIS IS A SCREENING HEURISTIC — it flags solder-joint
fatigue RISK; it is NOT a life prediction (no cycle counting, no joint geometry).

CONFORMANCE: the tool embeds a validated ``aicad.simhandoff/1`` envelope under the
reserved ``simhandoff`` key (coupling ``ecad_to_structural_fea``: one
``fixed_support`` condition PER mount hole + the base-excitation PSD condition).
"""

from __future__ import annotations

import json
import math
from typing import Any, Optional

from services.mcp import simhandoff as sh
from services.mcp.ecad_thermal import _faceset  # shared faceset shape (tol/normal)

# --------------------------------------------------------------------------- constants

# Steinberg, D. S. (2000). "Vibration Analysis for Electronic Equipment", 3rd ed.,
# John Wiley & Sons. Deflection-limit criterion (ch. 8, commonly quoted as the
# 0.00022*B/(C*h*r*sqrt(L)) allowable for ~20e6 random-vibe 3-sigma stress cycles)
# and the Z = 9.8*G/f^2 (inches) dynamic single-amplitude deflection relation.
STEINBERG_CITATION = (
    "Steinberg, D. S. (2000). Vibration Analysis for Electronic Equipment, 3rd ed., "
    "Wiley — Z_allow = 0.00022*B/(C*h*r*sqrt(L)) 3-sigma deflection limit "
    "(~20e6 random-vibration stress cycles) with Z = 9.8*G/f^2 dynamic deflection. "
    "HEURISTIC screen, not a life prediction.")

# Z(in) = 9.8 * G / f^2  ->  mm: 9.8 in * 25.4 mm/in.
_DEFLECT_MM_G_F2 = 9.8 * 25.4

_MM_PER_IN = 25.4

# Steinberg component-packaging constants C (larger C = stiffer/more fatigue-critical
# attachment = LOWER allowable deflection). Keys matched case-insensitively against
# the footprint fpid/ref; first hit wins, default 1.0 (standard DIP-class through-hole
# / leaded SMT). Published Steinberg-class values; a screening table, not a standard.
_STEINBERG_C: list = [
    ("lccc", 2.25), ("qfn", 2.25), ("dfn", 2.25),   # leadless ceramic / leadless SMT
    ("bga", 1.75),                                   # ball grid array
    ("pga", 1.26),                                   # pin grid array / side-brazed
    ("axial", 0.75),                                 # axial-leaded through-hole
]
_STEINBERG_C_DEFAULT = 1.0

# Structural FR4+Cu laminate assumptions (isotropic screen values; echoed in output).
_FR4_E_MPA = 18000.0        # in-plane laminate modulus, ~17-24 GPa family
_FR4_NU = 0.136
# Average component body density when no explicit mass metadata exists (mold compound
# / mixed package average) — the "density assumption" the task names.
_COMPONENT_RHO_G_CM3 = 1.9

# Default random-vibe screening excitation: flat 0.04 g^2/Hz across the band —
# NAVMAT P-9492-class screening LEVEL simplified to a flat profile (the real spec has
# 3 dB/oct slopes below 80 / above 350 Hz). A default screen, loudly echoed; pass
# psd_breakpoints for the actual qualification spectrum.
_DEFAULT_PSD_G2_HZ = 0.04
_DEFAULT_BAND_HZ = (20.0, 2000.0)

# Fixture patch radius beyond the hole radius (screw head / standoff land).
_MOUNT_LAND_MM = 1.5
# Mount-hole detection thresholds.
_NPTH_MIN_DRILL_MM = 1.5      # NPTH pads at/above this are treated as mounting holes
_UNNETTED_THT_MIN_DRILL_MM = 2.0  # unconnected plated holes this large = mounts

_VIBE: dict = {"last": None}   # session state (parity with ecad_thermal)


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", next_actions: Optional[list] = None,
         **extra: Any) -> str:
    out: dict = {"ok": False, "error": reason, "warnings": [],
                 "hint": hint or reason, "next_actions": next_actions or []}
    out.update(extra)
    return _dumps(out)


# --------------------------------------------------------------------------- pure helpers


def detect_mount_holes(board: dict, user_holes: Optional[list] = None) -> list:
    """Find board mounting holes -> [{x_mm, y_mm, d_mm, ref, source}].

    Priority:
      * ``user_holes`` — [[x, y], ...] or [[x, y, drill_d], ...] used verbatim
        (default drill 3.2 mm = M3 clearance) with source "user".
      * footprints whose fpid or ref names a mounting hole ("mountinghole", "mtg",
        "mount") — every drilled pad they carry.
      * NPTH (np_tht) pads with drill >= 1.5 mm anywhere on the board.
      * un-netted plated (tht) pads with drill >= 2.0 mm (screw holes drawn as
        plated pads).
    Deduplicated by position (0.1 mm). Empty list = nothing detected (caller errors).
    """
    if user_holes:
        out = []
        for i, h in enumerate(user_holes):
            x, y = float(h[0]), float(h[1])
            d = float(h[2]) if len(h) > 2 and h[2] else 3.2
            out.append({"x_mm": x, "y_mm": y, "d_mm": d,
                        "ref": f"user{i + 1}", "source": "user"})
        return out

    from engine.ecad.schema import pad_world  # pad local frame -> board frame

    found: list = []

    def _add(x: float, y: float, d: float, ref: str, source: str) -> None:
        for f in found:
            if abs(f["x_mm"] - x) < 0.1 and abs(f["y_mm"] - y) < 0.1:
                return
        found.append({"x_mm": round(x, 4), "y_mm": round(y, 4),
                      "d_mm": round(d, 4), "ref": ref, "source": source})

    for fp in board.get("footprints", []) or []:
        ref = str(fp.get("ref") or fp.get("id") or "?")
        fpid = str(fp.get("fpid") or "").lower()
        named = any(k in fpid for k in ("mountinghole", "mounting_hole", "mtg", "mount"))
        for pad in fp.get("pads", []) or []:
            drill = pad.get("drill") or {}
            dia = float(drill.get("diameter", 0.0) or 0.0)
            if dia <= 0.0:
                continue
            wx, wy = pad_world(fp, pad)
            ptype = pad.get("type", "smd")
            netted = bool(pad.get("net"))
            if named:
                _add(wx, wy, dia, ref, "mounting_hole_footprint")
            elif ptype == "np_tht" and dia >= _NPTH_MIN_DRILL_MM:
                _add(wx, wy, dia, ref, "npth_pad")
            elif ptype == "tht" and not netted and dia >= _UNNETTED_THT_MIN_DRILL_MM:
                _add(wx, wy, dia, ref, "unnetted_tht_pad")
    return found


def _disc_grid(x: float, y: float, r: float, z: float, max_step: float = 0.6) -> list:
    """Sample points covering a disc of radius r at (x, y, z)."""
    n = max(3, int(math.ceil(2.0 * r / max_step)) + 1)
    pts = [[round(x, 4), round(y, 4), round(z, 4)]]
    for i in range(n):
        px = x - r + 2.0 * r * i / (n - 1)
        for j in range(n):
            py = y - r + 2.0 * r * j / (n - 1)
            if (px - x) ** 2 + (py - y) ** 2 <= r * r:
                pts.append([round(px, 4), round(py, 4), round(z, 4)])
    return pts


def mount_fixture_facesets(holes: list, board_geo: dict) -> list:
    """Fixed-BC facesets: one land patch per hole on BOTH board faces (the FEA solid
    omits the drill bore, so we clamp the annular land a screw head / standoff grips)."""
    fixtures = []
    for h in holes:
        r = h["d_mm"] / 2.0 + _MOUNT_LAND_MM
        for z, nrm in ((board_geo["z_top_mm"], [0.0, 0.0, 1.0]),
                       (board_geo["z_bot_mm"], [0.0, 0.0, -1.0])):
            fixtures.append(_faceset(_disc_grid(h["x_mm"], h["y_mm"], r, z), nrm))
    return fixtures


def component_mass_g(rec: dict) -> Optional[float]:
    """Estimated component mass (g): explicit eng metadata mass when present, else
    full package dims x the mold-compound density assumption. None when the body was
    never placed (no dims to estimate from)."""
    for key in ("mass_g", "mass_grams"):
        v = rec.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    body = rec.get("body") or {}
    fea = rec.get("fea") or {}
    h = fea.get("h_full_mm") or body.get("h_mm")
    if not h:
        return None
    if body.get("shape") == "cylinder" and body.get("d_mm"):
        vol_mm3 = math.pi * (float(body["d_mm"]) / 2.0) ** 2 * float(h)
    elif body.get("l_mm") and body.get("w_mm"):
        vol_mm3 = float(body["l_mm"]) * float(body["w_mm"]) * float(h)
    elif fea.get("top_area_mm2"):
        vol_mm3 = float(fea["top_area_mm2"]) * float(h)
    else:
        return None
    return vol_mm3 * _COMPONENT_RHO_G_CM3 / 1000.0  # mm3 * g/cm3 / 1000 = g


def build_mass_model(built: dict) -> dict:
    """Mass-corrected effective density for the fused solid.

    board mass = homogenized rho x substrate volume; component masses via
    :func:`component_mass_g`; effective density = total mass / fused solid volume.
    Returns {board_mass_g, component_masses:[{ref, mass_g}], total_mass_g,
    fused_volume_mm3, density_g_cm3, assumptions:[...]}.
    """
    board_geo = built["board"]
    rho_kg_m3 = float(built["material"]["rho_kg_m3"])
    sub_vol_mm3 = float(board_geo["outline_area_mm2"]) * float(board_geo["thickness_mm"])
    board_mass_g = rho_kg_m3 * sub_vol_mm3 * 1e-6  # kg/m3 * mm3 * 1e-9 m3/mm3 * 1e3 g/kg
    comp_masses = []
    unmassed = []
    total = board_mass_g
    for rec in built["components"]:
        ref = rec.get("ref") or rec.get("id") or "?"
        m = component_mass_g(rec)
        if m is None:
            unmassed.append(ref)
            continue
        comp_masses.append({"ref": ref, "mass_g": round(m, 4)})
        total += m
    fused_vol = float(built["summary"].get("board_volume") or 0.0) or sub_vol_mm3
    dens = total / fused_vol * 1000.0  # g/mm3 -> g/cm3 (x1000 mm3 per cm3)
    return {
        "board_mass_g": round(board_mass_g, 3),
        "component_masses": comp_masses,
        "unmassed_refs": unmassed,
        "total_mass_g": round(total, 3),
        "fused_volume_mm3": round(fused_vol, 2),
        "density_g_cm3": round(dens, 5),
        "assumptions": [
            f"component masses = package dims x {_COMPONENT_RHO_G_CM3} g/cm3 "
            "mold-compound density assumption (explicit eng mass metadata wins when "
            "present)",
            "effective density scaled so the fused FEA solid (flattened component "
            "caps) carries the estimated TOTAL board+component mass — mass is "
            "globally correct, its spatial distribution approximate",
        ] + ([f"{len(unmassed)} component(s) have no body dims and contribute NO "
              f"mass: {', '.join(unmassed)}"] if unmassed else []),
    }


def structural_material(mass_model: dict) -> dict:
    """Isotropic FR4+Cu structural screen material with the mass-corrected density."""
    return {"name": "FR4+Cu laminate (structural screen)",
            "E_mpa": _FR4_E_MPA, "nu": _FR4_NU,
            "density_g_cm3": mass_model["density_g_cm3"]}


def steinberg_c(rec: dict) -> float:
    """Component-packaging constant C from fpid/ref keywords (default 1.0)."""
    hay = (str(rec.get("fpid") or "") + " " + str(rec.get("libid") or "")).lower()
    for key, c in _STEINBERG_C:
        if key in hay:
            return c
    return _STEINBERG_C_DEFAULT


def position_factor(x: float, y: float, bbox: list) -> float:
    """Steinberg relative position factor r: sin(pi*X/a)*sin(pi*Y/b) — 1.0 at the
    board center, 0.707 at an edge-centered half point, 0.5 at a quarter point.
    Floored at 0.1 (a component ON a clamped edge barely deflects; avoid div/0)."""
    x0, y0, x1, y1 = [float(v) for v in bbox]
    a = max(x1 - x0, 1e-6)
    b = max(y1 - y0, 1e-6)
    r = math.sin(math.pi * min(max((x - x0) / a, 0.0), 1.0)) * \
        math.sin(math.pi * min(max((y - y0) / b, 0.0), 1.0))
    return max(r, 0.1)


def steinberg_screen(components: list, board_geo: dict, z3_mm: float) -> dict:
    """Per-component Steinberg 3-sigma deflection-limit screen.

    ``z3_mm`` is the estimated 3-sigma board deflection (single amplitude, mm). Per
    component: Z_allow = 0.00022*B/(C*h*r*sqrt(L)) (inch inputs, converted), margin =
    Z_allow / z3_mm; pass when margin > 1. Components without body dims are UNCHECKED
    (honesty), never assumed OK. Returns {table, n_fail, n_unchecked, citation,
    heuristic: True}.
    """
    x0, y0, x1, y1 = [float(v) for v in board_geo["bbox_mm"]]
    h_in = float(board_geo["thickness_mm"]) / _MM_PER_IN
    table = []
    n_fail = 0
    n_unchecked = 0
    for rec in components:
        ref = rec.get("ref") or rec.get("id") or "?"
        fea = rec.get("fea") or {}
        body = rec.get("body") or {}
        entry: dict = {"ref": ref, "C": steinberg_c(rec), "L_mm": None, "r": None,
                       "z_allow_3sigma_mm": None, "z_est_3sigma_mm": round(z3_mm, 5),
                       "margin": None, "pass": None}
        # Component length L: longest package dim (worst case), else pad-extent rect.
        if body.get("shape") == "cylinder" and body.get("d_mm"):
            length_mm = float(body["d_mm"])
        elif body.get("l_mm") and body.get("w_mm"):
            length_mm = max(float(body["l_mm"]), float(body["w_mm"]))
        elif fea.get("rect_mm"):
            r_ = fea["rect_mm"]
            length_mm = max(r_[2] - r_[0], r_[3] - r_[1])
        else:
            entry["basis"] = "no body dims — UNCHECKED"
            n_unchecked += 1
            table.append(entry)
            continue
        pos = rec.get("pos_mm") or [(x0 + x1) / 2.0, (y0 + y1) / 2.0]
        # B: board edge length parallel to the component's long axis. Rotation is
        # binned to the nearest axis (screening resolution).
        ang = float(rec.get("angle_deg") or 0.0) % 180.0
        along_x = ang < 45.0 or ang >= 135.0
        if body.get("l_mm") and body.get("w_mm") and float(body["w_mm"]) > float(body["l_mm"]):
            along_x = not along_x  # long axis is the footprint's w
        b_mm = (x1 - x0) if along_x else (y1 - y0)
        r = position_factor(float(pos[0]), float(pos[1]), board_geo["bbox_mm"])
        c = entry["C"]
        z_allow_in = 0.00022 * (b_mm / _MM_PER_IN) / (
            c * h_in * r * math.sqrt(max(length_mm, 1e-6) / _MM_PER_IN))
        z_allow_mm = z_allow_in * _MM_PER_IN
        margin = float(z_allow_mm / z3_mm) if z3_mm > 0 else float("inf")
        ok = bool(margin > 1.0)
        if not ok:
            n_fail += 1
        entry.update({"L_mm": round(length_mm, 3), "B_mm": round(b_mm, 3),
                      "r": round(r, 4), "z_allow_3sigma_mm": round(z_allow_mm, 5),
                      "margin": round(margin, 3) if math.isfinite(margin) else None,
                      "pass": ok})
        table.append(entry)
    return {"table": table, "n_fail": n_fail, "n_unchecked": n_unchecked,
            "citation": STEINBERG_CITATION, "heuristic": True}


def band_split(freqs: list, lo: float, hi: float) -> tuple:
    """(modes_table, in_band_freqs): every elastic mode tagged in/out of band.
    Rigid-body modes (< 0.5 Hz) are tagged but excluded from the band verdict."""
    modes = []
    in_band = []
    for i, f in enumerate(freqs):
        f = float(f)
        rigid = f < 0.5
        inside = (not rigid) and lo <= f <= hi
        modes.append({"mode": i + 1, "freq_hz": round(f, 2),
                      "rigid_body": rigid, "in_band": inside})
        if inside:
            in_band.append(f)
    return modes, in_band


def deflection_3sigma_mm(grms: float, freq_hz: float) -> float:
    """Steinberg dynamic deflection: Z_rms(mm) = 248.9 * Grms / f^2, x3 for 3-sigma."""
    if freq_hz <= 0:
        return 0.0
    return 3.0 * _DEFLECT_MM_G_F2 * grms / (freq_hz * freq_hz)


# --------------------------------------------------------------------------- registration


def register_ecad_reliability_tools(mcp) -> None:
    """Register ecad_vibe_check on ``mcp`` (FastMCP)."""

    # Same module identity as the rest of the server (see piping_tools note on the
    # double-import hazard).
    from mcp_server.ecad_tools import _get_pcb, _default_out_dir
    from engine.ecad.ops import OpError

    @mcp.tool()
    def ecad_vibe_check(doc_id: str = "",
                        band_lo_hz: float = _DEFAULT_BAND_HZ[0],
                        band_hi_hz: float = _DEFAULT_BAND_HZ[1],
                        psd_breakpoints: Optional[list] = None,
                        mount_holes: Optional[list] = None,
                        zeta: float = 0.05,
                        n_modes: int = 8,
                        mesh_fineness: float = 0.35,
                        dry_run: bool = False,
                        path: str = "") -> str:
        """Vibration reliability screen of the ACTIVE routed board — modal solve of the
        mounted board+components solid, excitation-band resonance flagging, and a
        Steinberg deflection-based solder-joint risk screen. One call, zero manual BCs.

        Pipeline:
        1. Reuses the T-FIN16 fused board+components FEA solid
           (engine.ecad.threed.board_to_fea_step). Component masses come from package
           dims x a mold-compound density assumption (eng mass metadata wins where
           present); ONE effective density is scaled so the solid carries the total
           estimated mass (mass_model lists every number).
        2. Mount holes: mounting-hole footprints / NPTH pads / large un-netted plated
           holes — or pass mount_holes=[[x,y],[x,y,drill_d],...] explicitly. Each hole
           becomes a fixed patch (screw-land annulus) on both board faces.
        3. Core modal solve (sim_run_modal, tet10) -> modes table; every natural
           frequency inside [band_lo_hz, band_hi_hz] is FLAGGED (default 20-2000 Hz —
           the common random-vibe screening band).
        4. Random response at the worst in-band mode via Miles' equation
           (participation-scaled, engine.sim.vibration). psd_breakpoints
           [[f_hz, g2_per_hz], ...] sets the input; DEFAULT = flat 0.04 g^2/Hz across
           the band (NAVMAT P-9492-class screening level, simplified flat — echoed in
           assumptions; pass the real qualification spectrum for a spec-true screen).
        5. Board 3-sigma deflection estimate (Z = 9.8*G/f^2) screened per component
           against Steinberg's 0.00022*B/(C*h*r*sqrt(L)) 3-sigma deflection limit —
           a documented, cited HEURISTIC for solder-joint fatigue risk, NOT a life
           prediction.

        zeta : modal damping ratio assumption (default 0.05 — bare PCB assemblies
               typically 0.02-0.10; echoed back, never hidden).
        dry_run=True stops before meshing/solving: geometry, mount fixtures, mass
        model and the aicad.simhandoff/1 envelope only.

        Returns {ok, mount_holes, mass_model, modes, band, response,
        deflection_screen (with citation), warnings, simhandoff}.
        """
        warnings: list = []
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="vibe check needs an open routed board.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"])
        board_doc = session.doc

        lo, hi = float(band_lo_hz), float(band_hi_hz)
        if not (0.0 < lo < hi):
            return _err(f"bad excitation band [{lo}, {hi}] Hz — need 0 < lo < hi")

        # ── mount holes -> fixed BCs ─────────────────────────────────────────
        try:
            holes = detect_mount_holes(board_doc, mount_holes)
        except ImportError as e:
            return _err(f"3D kernel unavailable (ImportError: {e})",
                        hint="OCP/build123d not installed.", available=False)
        if not holes:
            return _err(
                "no mounting holes found — a free-floating board has no mounted modes",
                hint="add mounting-hole footprints / NPTH pads to the layout, or pass "
                     "mount_holes=[[x_mm, y_mm], ...] (optionally [x, y, drill_d_mm]).",
                next_actions=["ecad_vibe_check(mount_holes=[[3,3],[37,3],[37,27],[3,27]])"])

        out_path = path or str(_default_out_dir(board_doc.get("name", "board"))
                               / "board_vibe.step")

        # ── FEA solid (T-FIN16, reused) + mass-corrected material ───────────
        try:
            from engine.ecad.threed import board_to_fea_step, BoardGeometryError
        except ImportError as e:
            return _err(f"3D kernel unavailable (ImportError: {e})",
                        hint="OCP/build123d not installed — the FEA solid cannot be built.",
                        next_actions=["pip install build123d"], available=False)
        try:
            built = board_to_fea_step(board_doc, out_path)
        except BoardGeometryError as e:
            return _err(f"board geometry failed: {e}",
                        hint="the board needs a closed Edge.Cuts outline.",
                        next_actions=["ecad_run_drc()", "ecad_status()"])
        except Exception as e:  # noqa: BLE001
            return _err(f"FEA solid build failed: {type(e).__name__}: {e}")

        board_geo = built["board"]
        components = built["components"]
        warnings.extend(built["summary"].get("warnings", []))

        mass_model = build_mass_model(built)
        material = structural_material(mass_model)
        fixtures = mount_fixture_facesets(holes, board_geo)

        # ── excitation input ────────────────────────────────────────────────
        if psd_breakpoints is not None:
            psd = [[float(p[0]), float(p[1])] for p in psd_breakpoints]
            psd_note = "caller-supplied PSD"
        else:
            psd = [[lo, _DEFAULT_PSD_G2_HZ], [hi, _DEFAULT_PSD_G2_HZ]]
            psd_note = (f"DEFAULT flat {_DEFAULT_PSD_G2_HZ} g^2/Hz over "
                        f"{lo:g}-{hi:g} Hz (NAVMAT P-9492-class screening level, "
                        "simplified flat profile — pass psd_breakpoints for the real "
                        "qualification spectrum)")
            warnings.append(f"random-vibe input is a default screen: {psd_note}")

        assumptions = [
            f"structural material: isotropic FR4+Cu laminate screen, E = "
            f"{_FR4_E_MPA / 1000:g} GPa, nu = {_FR4_NU} (in-plane laminate nominals)",
            f"modal damping zeta = {zeta:g} (assumed, not measured)",
            "mount fixtures clamp a "
            f"{_MOUNT_LAND_MM} mm land annulus on both faces at each hole "
            "(drill bores are omitted from the FEA solid)",
        ] + mass_model["assumptions"] + [
            a for a in built["assumptions"] if "fused" in a or "flattened" in a]

        # ── simhandoff envelope ─────────────────────────────────────────────
        conditions = [
            sh.condition(f"mount_{h['ref']}_{i + 1}", "fixed_support",
                         {"x_mm": h["x_mm"], "y_mm": h["y_mm"], "d_mm": h["d_mm"]},
                         "mm", f"mount-hole detection ({h['source']})",
                         applied_to=f"board faces at hole {h['ref']}")
            for i, h in enumerate(holes)]
        conditions.append(sh.condition(
            "base_excitation_psd", "base_excitation",
            {"psd_breakpoints": psd, "band_hz": [lo, hi], "zeta": zeta},
            "g^2/Hz", psd_note, applied_to="mount fixtures (Z base excitation)"))
        envelope = sh.build_handoff(
            "ecad_to_structural_fea",
            sh.study_ref("ecad", "ecad_vibe_check", doc_id=session.doc_id),
            sh.study_ref("structural_fea", "run_fea_modal"),
            geometry=[sh.geometry_ref("board", "step", path=built["step_path"],
                                      derived_by="engine.ecad.threed.board_to_fea_step",
                                      meta={"fused_single_solid": True})],
            conditions=conditions,
            warnings=warnings or None,
            meta={"material": material, "mass_model": mass_model,
                  "band_hz": [lo, hi], "assumptions": assumptions})

        base = {
            "ok": True, "doc_id": session.doc_id,
            "step_path": built["step_path"],
            "mount_holes": holes,
            "mass_model": mass_model,
            "material": material,
            "band_hz": [lo, hi],
            "psd_input": {"breakpoints": psd, "note": psd_note},
            "assumptions": assumptions,
            "warnings": warnings,
        }

        if dry_run:
            _VIBE["last"] = None
            return _dumps(sh.attach({
                **base, "solved": False, "dry_run": True,
                "n_fixtures": len(fixtures),
                "hint": f"dry run: {len(holes)} mount hole(s) -> {len(fixtures)} fixed "
                        "patches; geometry + mass model + envelope only (no solve).",
                "next_actions": ["ecad_vibe_check(dry_run=False)  # solve"],
            }, envelope))

        # ── modal solve via the core solver ─────────────────────────────────
        try:
            from mcp_server import core_bridge as cb
        except Exception as e:  # noqa: BLE001
            cb = None
            solve_err: Optional[str] = f"core bridge import failed: {e}"
        if cb is not None:
            study = {"step_path": built["step_path"], "material": material,
                     "fixtures": fixtures, "mesh_order": 2,
                     "fineness": float(mesh_fineness)}
            try:
                res = cb.call("sim_run_modal", {"study": study,
                                                "n_modes": int(n_modes)})
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)}
            solve_err = None if res.get("ok") else res.get("error", "modal solve failed")
        if cb is None or solve_err:
            _VIBE["last"] = None
            return _dumps(sh.attach({
                **base, "solved": False, "dry_run": False,
                "error_solve": f"FEA leg unavailable: {solve_err}",
                "hint": "geometry + fixtures + envelope are valid; the mesher/solver "
                        "(libaicad with Netgen) is not available in this environment.",
                "next_actions": ["set AI_CAD_LIBAICAD to a sim-enabled libaicad build "
                                 "and retry"],
            }, envelope))

        freqs = [float(f) for f in res.get("freqs_hz", [])]
        modes, in_band = band_split(freqs, lo, hi)
        elastic = [f for f in freqs if f >= 0.5]
        f1 = elastic[0] if elastic else None
        band_verdict = (
            f"FLAG: {len(in_band)} natural frequenc"
            f"{'y' if len(in_band) == 1 else 'ies'} inside the {lo:g}-{hi:g} Hz "
            f"excitation band ({', '.join(f'{f:.0f}' for f in in_band)} Hz) — "
            "resonant amplification expected under this environment"
            if in_band else
            f"CLEAR: no natural frequency inside the {lo:g}-{hi:g} Hz excitation band "
            f"(first elastic mode {f1:.0f} Hz)" if f1 else
            "CLEAR: no elastic modes returned")

        # ── response at the worst in-band mode + deflection screen ─────────
        deflect: dict = {"available": False}
        response: dict = {"available": False}
        try:
            from engine_oracle.sim.vibration import miles_response
            shapes = res.get("mode_shapes") or []
            n_nodes = int(res.get("n_nodes", 0))
            if shapes and n_nodes > 0:
                rand = miles_response(
                    freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                    total_mass_kg=mass_model["total_mass_g"] / 1000.0,
                    excitation_dir=[0.0, 0.0, 1.0], psd_breakpoints=psd, zeta=zeta)
                # Coerce numpy scalars (participation math) to JSON-safe floats.
                per_mode = [{k: (float(v) if isinstance(v, (int, float)) else v)
                             for k, v in m.items()} for m in rand["per_mode"]]
                pm_in = [m for m in per_mode if lo <= m["freq_hz"] <= hi]
                pool = pm_in or per_mode
                worst = max(pool, key=lambda m: m["grms"]) if pool else None
                response = {
                    "available": True,
                    "worst_mode": worst,
                    "worst_mode_in_band": bool(pm_in),
                    "per_mode": per_mode,
                    "total_grms": round(float(rand["total_grms"]), 4),
                    "zeta": zeta, "Q": float(rand["Q"]),
                    "citation": rand["citation"],
                    "limitations": rand["limitations"],
                }
                if worst is not None:
                    z3 = deflection_3sigma_mm(worst["grms"], worst["freq_hz"])
                    screen = steinberg_screen(components, board_geo, z3)
                    n_ok = sum(1 for e in screen["table"] if e["pass"])
                    verdict = (
                        f"{'FAIL' if screen['n_fail'] else 'PASS'} (HEURISTIC): "
                        f"3-sigma board deflection {z3:.4f} mm at the worst "
                        f"{'in-band ' if pm_in else ''}mode "
                        f"({worst['freq_hz']:.0f} Hz, {worst['grms']:.2f} Grms) — "
                        f"{n_ok} component(s) within the Steinberg deflection limit, "
                        f"{screen['n_fail']} over, {screen['n_unchecked']} unchecked. "
                        f"{screen['citation']}")
                    deflect = {"available": True,
                               "z_3sigma_mm": round(z3, 5),
                               "at_freq_hz": round(worst["freq_hz"], 2),
                               "mode_grms": round(worst["grms"], 4),
                               **screen, "verdict": verdict,
                               "pass": screen["n_fail"] == 0}
                    if not pm_in:
                        deflect["note"] = ("no mode inside the excitation band — "
                                           "screened at the worst overall mode "
                                           "(conservatively informative)")
            else:
                warnings.append("modal solve returned no mode shapes — response + "
                                "deflection screen unavailable (modes table only)")
        except ImportError as e:
            warnings.append(f"engine.sim.vibration unavailable ({e}) — response + "
                            "deflection screen skipped")

        result = sh.attach({
            **base, "solved": True, "dry_run": False,
            "solver": "C++ core modal (tet10) + Miles'-equation modal response",
            "n_nodes": res.get("n_nodes"), "n_dof": res.get("n_dof"),
            "modes": modes,
            "f1_hz": round(f1, 2) if f1 else None,
            "band": {"lo_hz": lo, "hi_hz": hi,
                     "in_band_freqs_hz": [round(f, 2) for f in in_band],
                     "verdict": band_verdict},
            "response": response,
            "deflection_screen": deflect,
            "warnings": warnings,
            "hint": (f"solved: f1 = {f1:.0f} Hz; {band_verdict.split(':')[0]} in band; "
                     + (f"deflection screen {'PASS' if deflect.get('pass') else 'FAIL'} "
                        f"(z3 = {deflect.get('z_3sigma_mm')} mm)" if deflect.get("available")
                        else "deflection screen unavailable")) if f1 else band_verdict,
            "next_actions": (
                ["stiffen the board (thicker laminate / extra mount) or move heavy "
                 "parts toward the mounts, then re-run ecad_vibe_check()"]
                if (in_band or (deflect.get("available") and not deflect.get("pass")))
                else ["margins OK at the screening level — re-run with the real "
                      "qualification psd_breakpoints before sign-off"]),
        }, envelope)
        _VIBE["last"] = result
        return _dumps(result)
