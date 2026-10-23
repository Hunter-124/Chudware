"""ECAD -> vibration reliability screening — one call from a routed board to
a mounted-board modal solve + excitation-band flagging + a deflection-based solder-joint
risk screen, conforming to ``aicad.simhandoff/1``.

Same design contract as ``ecad_thermal``: JSON-string results with
``{ok, ..., warnings, hint, next_actions}``, engine helpers called directly, pure
helpers module-level and engine-free so tests can drive them without OCP/libchudware.

Tool:
  ecad_vibe_check   board -> fused board+components FEA solid (the thermal handoff's
                    services.ecad.threed.board_to_fea_step, reused as-is) -> mount-hole
                    detection (mounting-hole footprints / NPTH pads / user-passed
                    positions) -> fixed BCs at the mounts -> core modal solve
                    (sim_run_modal) -> natural frequencies flagged against the
                    excitation band (default 20-2000 Hz random-vibe screening) ->
                    Miles'-equation response at the worst in-band mode -> board
                    deflection estimate -> Steinberg 3-sigma deflection-limit screen
                    per component (HEURISTIC, cited — not a life prediction).

THE MASS MODEL (documented honesty): the fused FEA solid flattens component bodies to
thin caps (see services.ecad.threed._FEA_MAX_BODY_H_MM) and would otherwise carry them
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

import math
from typing import Optional, Union

from services.mcp import simhandoff as sh
from services.mcp.cad_common import coerce_json_or_repr, require_list
from services.mcp.ecad_thermal import _faceset  # shared faceset shape (tol/normal)
from services.mcp._envelopes import plain_dumps as _dumps, report_err as _err

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


# --------------------------------------------------------------------------- pure helpers


def detect_mount_holes(board: dict, user_holes: Optional[list] = None) -> list:
    """Find finite positive board mounting holes, honoring an explicit user list."""
    if not isinstance(board, dict):
        raise ValueError("board must be an object")
    if user_holes is not None:
        if not isinstance(user_holes, list):
            raise ValueError("mount_holes must be a list")
        out = []
        for i, hole in enumerate(user_holes):
            if not isinstance(hole, list) or len(hole) not in (2, 3):
                raise ValueError(
                    f"mount_holes[{i}] must be [x_mm, y_mm] or [x_mm, y_mm, drill_d_mm]")
            try:
                x, y = float(hole[0]), float(hole[1])
                diameter = float(hole[2]) if len(hole) == 3 else 3.2
            except (TypeError, ValueError) as exc:
                raise ValueError(f"mount_holes[{i}] values must be numeric mm") from exc
            if (not all(math.isfinite(v) for v in (x, y, diameter))
                    or diameter <= 0.0):
                raise ValueError(
                    f"mount_holes[{i}] coordinates must be finite and diameter > 0")
            out.append({"x_mm": x, "y_mm": y, "d_mm": diameter,
                        "ref": f"user{i + 1}", "source": "user"})
        return out

    from services.mcp.ecad_common import _pad_world as pad_world  # pad local frame -> board frame

    footprints = board.get("footprints", []) or []
    if not isinstance(footprints, list):
        raise ValueError("board footprints must be a list")
    found: list = []

    def _add(x: float, y: float, diameter: float, ref: str, source: str) -> None:
        if (not all(math.isfinite(v) for v in (x, y, diameter))
                or diameter <= 0.0):
            raise ValueError(f"{ref} mounting-hole geometry must be finite and positive")
        for prior in found:
            if abs(prior["x_mm"] - x) < 0.1 and abs(prior["y_mm"] - y) < 0.1:
                return
        found.append({"x_mm": round(x, 4), "y_mm": round(y, 4),
                      "d_mm": round(diameter, 4), "ref": ref, "source": source})

    for footprint in footprints:
        if not isinstance(footprint, dict):
            raise ValueError("board footprints must contain objects")
        ref = str(footprint.get("ref") or footprint.get("id") or "?")
        fpid = str(footprint.get("fpid") or "").lower()
        named = any(
            key in fpid for key in ("mountinghole", "mounting_hole", "mtg", "mount"))
        pads = footprint.get("pads", []) or []
        if not isinstance(pads, list):
            raise ValueError(f"{ref} pads must be a list")
        for pad in pads:
            if not isinstance(pad, dict):
                raise ValueError(f"{ref} pads must contain objects")
            drill = pad.get("drill") or {}
            if not isinstance(drill, dict):
                raise ValueError(f"{ref} pad drill must be an object")
            try:
                diameter = float(drill.get("diameter", 0.0) or 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{ref} drill diameter must be numeric mm") from exc
            if not math.isfinite(diameter) or diameter < 0.0:
                raise ValueError(f"{ref} drill diameter must be finite and >= 0 mm")
            if diameter == 0.0:
                continue
            wx, wy = [float(v) for v in pad_world(footprint, pad)]
            ptype = pad.get("type", "smd")
            netted = bool(pad.get("net"))
            if named:
                _add(wx, wy, diameter, ref, "mounting_hole_footprint")
            elif ptype == "np_tht" and diameter >= _NPTH_MIN_DRILL_MM:
                _add(wx, wy, diameter, ref, "npth_pad")
            elif (ptype == "tht" and not netted
                  and diameter >= _UNNETTED_THT_MIN_DRILL_MM):
                _add(wx, wy, diameter, ref, "unnetted_tht_pad")
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
    """Return an explicit or finite positive package-volume mass estimate in grams."""
    if not isinstance(rec, dict):
        raise ValueError("component mass records must be objects")
    for key in ("mass_g", "mass_grams"):
        value = rec.get(key)
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0.0):
            raise ValueError(f"{rec.get('ref') or '?'} {key} must be finite and > 0 g")
        return float(value)
    body = rec.get("body") or {}
    fea = rec.get("fea") or {}
    if not isinstance(body, dict) or not isinstance(fea, dict):
        raise ValueError(f"{rec.get('ref') or '?'} body/fea metadata must be objects")
    raw_h = fea.get("h_full_mm") or body.get("h_mm")
    if raw_h is None:
        return None
    try:
        height = float(raw_h)
        if body.get("shape") == "cylinder" and body.get("d_mm") is not None:
            diameter = float(body["d_mm"])
            dims = (height, diameter)
            volume_mm3 = math.pi * (diameter / 2.0) ** 2 * height
        elif body.get("l_mm") is not None and body.get("w_mm") is not None:
            length, width = float(body["l_mm"]), float(body["w_mm"])
            dims = (height, length, width)
            volume_mm3 = length * width * height
        elif fea.get("top_area_mm2") is not None:
            area = float(fea["top_area_mm2"])
            dims = (height, area)
            volume_mm3 = area * height
        else:
            return None
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{rec.get('ref') or '?'} package dimensions must be numeric") from exc
    if not all(math.isfinite(v) and v > 0.0 for v in dims):
        raise ValueError(
            f"{rec.get('ref') or '?'} package dimensions must be finite and > 0 mm")
    mass = volume_mm3 * _COMPONENT_RHO_G_CM3 / 1000.0
    if not math.isfinite(mass) or mass <= 0.0:
        raise ValueError(f"{rec.get('ref') or '?'} derived component mass is invalid")
    return mass


def build_mass_model(built: dict) -> dict:
    """Build a finite, mass-conserving effective density for the fused solid."""
    try:
        board_geo = built["board"]
        material = built["material"]
        components = built["components"]
        summary = built["summary"]
        rho_kg_m3 = float(material["rho_kg_m3"])
        outline_area = float(board_geo["outline_area_mm2"])
        thickness = float(board_geo["thickness_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("FEA mass metadata is malformed") from exc
    if not isinstance(components, list) or not isinstance(summary, dict):
        raise ValueError("FEA components/summary mass metadata is malformed")
    if (not all(math.isfinite(v) and v > 0.0
                for v in (rho_kg_m3, outline_area, thickness))):
        raise ValueError("board density, area, and thickness must be finite and > 0")
    sub_vol_mm3 = outline_area * thickness
    board_mass_g = rho_kg_m3 * sub_vol_mm3 * 1e-6
    comp_masses = []
    unmassed = []
    total = board_mass_g
    for rec in components:
        ref = rec.get("ref") or rec.get("id") or "?"
        mass = component_mass_g(rec)
        if mass is None:
            unmassed.append(ref)
            continue
        comp_masses.append({"ref": ref, "mass_g": round(mass, 4)})
        total += mass
    try:
        fused_vol = float(summary.get("board_volume") or sub_vol_mm3)
    except (TypeError, ValueError) as exc:
        raise ValueError("fused board volume must be numeric") from exc
    if not math.isfinite(fused_vol) or fused_vol <= 0.0:
        raise ValueError("fused board volume must be finite and > 0 mm³")
    density = total / fused_vol * 1000.0
    if not math.isfinite(total) or total <= 0.0 or not math.isfinite(density) or density <= 0:
        raise ValueError("derived total mass/density must be finite and > 0")
    return {
        "board_mass_g": round(board_mass_g, 3),
        "component_masses": comp_masses,
        "unmassed_refs": unmassed,
        "total_mass_g": round(total, 3),
        "fused_volume_mm3": round(fused_vol, 2),
        "density_g_cm3": round(density, 5),
        "assumptions": [
            f"component masses = package dims x {_COMPONENT_RHO_G_CM3} g/cm3 "
            "mold-compound density assumption (explicit eng mass metadata wins when "
            "present)",
            "effective density scaled so the fused FEA solid carries the estimated "
            "TOTAL board+component mass; spatial distribution remains approximate",
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
    """Steinberg relative position factor, floored at 0.1 near a clamped edge."""
    try:
        x, y = float(x), float(y)
        values = [float(v) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise ValueError("component position and board bbox must be numeric") from exc
    if (len(values) != 4 or not all(math.isfinite(v) for v in [x, y] + values)
            or values[2] <= values[0] or values[3] <= values[1]):
        raise ValueError("component position and board bbox must be finite/non-degenerate")
    x0, y0, x1, y1 = values
    a = x1 - x0
    b = y1 - y0
    r = math.sin(math.pi * min(max((x - x0) / a, 0.0), 1.0)) * \
        math.sin(math.pi * min(max((y - y0) / b, 0.0), 1.0))
    return max(r, 0.1)


def steinberg_screen(components: list, board_geo: dict, z3_mm: float) -> dict:
    """Screen finite component geometry against the Steinberg 3-sigma limit."""
    if not isinstance(components, list) or not isinstance(board_geo, dict):
        raise ValueError("components and board geometry must be structured")
    try:
        bbox = [float(v) for v in board_geo["bbox_mm"]]
        thickness = float(board_geo["thickness_mm"])
        z3_mm = float(z3_mm)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Steinberg board/deflection inputs must be numeric") from exc
    if (len(bbox) != 4 or not all(math.isfinite(v) for v in bbox + [thickness, z3_mm])
            or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or thickness <= 0.0
            or z3_mm <= 0.0):
        raise ValueError(
            "Steinberg board dimensions and 3-sigma deflection must be finite and > 0")
    x0, y0, x1, y1 = bbox
    h_in = thickness / _MM_PER_IN
    table = []
    n_fail = 0
    n_unchecked = 0
    for rec in components:
        if not isinstance(rec, dict):
            raise ValueError("Steinberg component records must be objects")
        ref = rec.get("ref") or rec.get("id") or "?"
        fea = rec.get("fea") or {}
        body = rec.get("body") or {}
        if not isinstance(fea, dict) or not isinstance(body, dict):
            raise ValueError(f"{ref} body/FEA metadata must be objects")
        entry: dict = {"ref": ref, "C": steinberg_c(rec), "L_mm": None, "r": None,
                       "z_allow_3sigma_mm": None, "z_est_3sigma_mm": round(z3_mm, 5),
                       "margin": None, "pass": None}
        try:
            if body.get("shape") == "cylinder" and body.get("d_mm") is not None:
                length_mm = float(body["d_mm"])
            elif body.get("l_mm") is not None and body.get("w_mm") is not None:
                length_mm = max(float(body["l_mm"]), float(body["w_mm"]))
            elif fea.get("rect_mm") is not None:
                rect = [float(v) for v in fea["rect_mm"]]
                if len(rect) != 4:
                    raise ValueError
                length_mm = max(rect[2] - rect[0], rect[3] - rect[1])
            else:
                entry["basis"] = "no body dims — UNCHECKED"
                n_unchecked += 1
                table.append(entry)
                continue
            pos = rec.get("pos_mm") or [(x0 + x1) / 2.0, (y0 + y1) / 2.0]
            if not isinstance(pos, list) or len(pos) != 2:
                raise ValueError
            pos_x, pos_y = float(pos[0]), float(pos[1])
            angle = float(rec.get("angle_deg") or 0.0) % 180.0
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{ref} Steinberg geometry must be numeric") from exc
        if (not all(math.isfinite(v) for v in (length_mm, pos_x, pos_y, angle))
                or length_mm <= 0.0):
            raise ValueError(f"{ref} Steinberg geometry must be finite and positive")
        along_x = angle < 45.0 or angle >= 135.0
        if (body.get("l_mm") is not None and body.get("w_mm") is not None
                and float(body["w_mm"]) > float(body["l_mm"])):
            along_x = not along_x
        b_mm = (x1 - x0) if along_x else (y1 - y0)
        factor = position_factor(pos_x, pos_y, bbox)
        c = entry["C"]
        z_allow_in = 0.00022 * (b_mm / _MM_PER_IN) / (
            c * h_in * factor * math.sqrt(length_mm / _MM_PER_IN))
        z_allow_mm = z_allow_in * _MM_PER_IN
        margin = z_allow_mm / z3_mm
        if not math.isfinite(z_allow_mm) or z_allow_mm <= 0.0 or not math.isfinite(margin):
            raise ValueError(f"{ref} derived Steinberg limit is invalid")
        passed = margin >= 1.0
        if not passed:
            n_fail += 1
        entry.update({"L_mm": round(length_mm, 3), "B_mm": round(b_mm, 3),
                      "r": round(factor, 4), "z_allow_3sigma_mm": round(z_allow_mm, 5),
                      "margin": round(margin, 3), "pass": passed})
        table.append(entry)
    return {"table": table, "n_fail": n_fail, "n_unchecked": n_unchecked,
            "citation": STEINBERG_CITATION, "heuristic": True}


def band_split(freqs: list, lo: float, hi: float) -> tuple:
    """Validate modal frequencies and tag rigid/elastic modes inside the band."""
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError) as exc:
        raise ValueError("modal band limits must be numeric") from exc
    if not math.isfinite(lo) or not math.isfinite(hi) or not 0.0 < lo < hi:
        raise ValueError("modal band must satisfy finite 0 < lo < hi")
    if not isinstance(freqs, list):
        raise ValueError("modal frequencies must be a list")
    modes = []
    in_band = []
    previous = -1.0
    for i, raw in enumerate(freqs):
        try:
            freq = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"mode {i + 1} frequency must be numeric") from exc
        if not math.isfinite(freq) or freq < 0.0 or freq < previous:
            raise ValueError("modal frequencies must be finite, non-negative, and sorted")
        previous = freq
        rigid = freq < 0.5
        inside = (not rigid) and lo <= freq <= hi
        modes.append({"mode": i + 1, "freq_hz": round(freq, 2),
                      "rigid_body": rigid, "in_band": inside})
        if inside:
            in_band.append(freq)
    return modes, in_band


def deflection_3sigma_mm(grms: float, freq_hz: float) -> float:
    """Steinberg dynamic deflection: 3 * 248.9 * Grms / f², in millimetres."""
    try:
        grms, freq_hz = float(grms), float(freq_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError("Grms and frequency must be numeric") from exc
    if (not math.isfinite(grms) or grms <= 0.0
            or not math.isfinite(freq_hz) or freq_hz <= 0.0):
        raise ValueError("Grms and frequency must be finite and > 0")
    value = 3.0 * _DEFLECT_MM_G_F2 * grms / (freq_hz * freq_hz)
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("derived 3-sigma deflection must be finite and > 0")
    return value


# --------------------------------------------------------------------------- registration


def register_ecad_reliability_tools(mcp) -> None:
    """Register ecad_vibe_check on ``mcp`` (FastMCP)."""

    # Same module identity as the rest of the server (see piping_tools note on the
    # double-import hazard).
    from services.mcp.ecad_tools import _get_pcb, _default_out_dir
    from services.mcp.ecad_common import OpError

    @mcp.tool()
    def ecad_vibe_check(doc_id: str = "",
                        band_lo_hz: float = _DEFAULT_BAND_HZ[0],
                        band_hi_hz: float = _DEFAULT_BAND_HZ[1],
                        psd_breakpoints: Optional[Union[list, str]] = None,
                        mount_holes: Optional[Union[list, str]] = None,
                        zeta: float = 0.05,
                        n_modes: int = 8,
                        mesh_fineness: float = 0.35,
                        dry_run: bool = False,
                        path: str = "") -> str:
        """Vibration reliability screen of the ACTIVE routed board — modal solve of the
        mounted board+components solid, excitation-band resonance flagging, and a
        Steinberg deflection-based solder-joint risk screen. One call, zero manual BCs.

        Pipeline:
        1. Reuses the thermal-handoff fused board+components FEA solid
           (services.ecad.threed.board_to_fea_step). Component masses come from package
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
           (participation-scaled, services.sim.vibration). psd_breakpoints
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
        # LIVE CRASH class: a stringified/python-repr list ("[[3,3],[37,3]]") used to
        # sail past `Optional[list]` untouched, then get iterated CHARACTER BY
        # CHARACTER below (`for p in psd_breakpoints` / detect_mount_holes' `for h in
        # user_holes`) — float('[') then crashed with a message naming neither this
        # argument nor the shape it wanted. Coerce up front so a real string error
        # names the argument, and a genuine list/repr-list is read correctly.
        try:
            if isinstance(psd_breakpoints, str) and psd_breakpoints.strip():
                psd_breakpoints = require_list(
                    psd_breakpoints, "psd_breakpoints",
                    "a list of [f_hz, g2_per_hz] pairs")
            if isinstance(mount_holes, str) and mount_holes.strip():
                mount_holes = require_list(
                    mount_holes, "mount_holes",
                    "a list of [x_mm, y_mm] or [x_mm, y_mm, drill_d_mm] points")
        except (TypeError, ValueError) as exc:
            return _err(str(exc))
        if psd_breakpoints is not None and not isinstance(psd_breakpoints, list):
            return _err("psd_breakpoints must be a list or null")
        if mount_holes is not None and not isinstance(mount_holes, list):
            return _err("mount_holes must be a list or null")
        try:
            lo, hi = float(band_lo_hz), float(band_hi_hz)
            zeta = float(zeta)
            mesh_fineness = float(mesh_fineness)
        except (TypeError, ValueError):
            return _err("band limits, zeta, and mesh_fineness must be numeric")
        if not math.isfinite(lo) or not math.isfinite(hi) or not 0.0 < lo < hi:
            return _err(f"bad excitation band [{lo}, {hi}] Hz — need finite 0 < lo < hi")
        if not math.isfinite(zeta) or not 0.0 < zeta <= 1.0:
            return _err("zeta must be a finite damping ratio in (0, 1]")
        if not math.isfinite(mesh_fineness) or not 0.0 < mesh_fineness <= 1.0:
            return _err("mesh_fineness must be finite and in (0, 1]")
        if (isinstance(n_modes, bool) or not isinstance(n_modes, int)
                or not 1 <= n_modes <= 100):
            return _err("n_modes must be an integer in [1, 100]")

        if psd_breakpoints is None:
            psd = [[lo, _DEFAULT_PSD_G2_HZ], [hi, _DEFAULT_PSD_G2_HZ]]
            psd_note = (f"DEFAULT flat {_DEFAULT_PSD_G2_HZ} g^2/Hz over "
                        f"{lo:g}-{hi:g} Hz (NAVMAT P-9492-class screening level, "
                        "simplified flat profile — pass psd_breakpoints for the real "
                        "qualification spectrum)")
            warnings.append(f"random-vibe input is a default screen: {psd_note}")
        else:
            if len(psd_breakpoints) < 2:
                return _err("psd_breakpoints needs at least two points")
            psd = []
            previous = -1.0
            for index, point in enumerate(psd_breakpoints):
                if not isinstance(point, list) or len(point) != 2:
                    return _err(
                        f"psd_breakpoints[{index}] must be [f_hz, g2_per_hz]")
                try:
                    frequency, density = float(point[0]), float(point[1])
                except (TypeError, ValueError):
                    return _err(f"psd_breakpoints[{index}] values must be numeric")
                if (not math.isfinite(frequency) or frequency <= previous
                        or frequency <= 0.0 or not math.isfinite(density)
                        or density <= 0.0):
                    return _err("PSD frequencies must be finite, positive, strictly "
                                "increasing; densities must be finite and > 0 g²/Hz")
                psd.append([frequency, density])
                previous = frequency
            psd_note = "caller-supplied PSD"

        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="vibe check needs an open routed board.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"])
        board_doc = session.doc

        # ── mount holes -> fixed BCs ─────────────────────────────────────────
        try:
            holes = detect_mount_holes(board_doc, mount_holes)
        except ImportError as e:
            return _err(f"3D kernel unavailable (ImportError: {e})",
                        hint="OCP/build123d not installed.", available=False)
        except ValueError as e:
            return _err(f"invalid mounting-hole setup: {e}")
        if not holes:
            return _err(
                "no mounting holes found — a free-floating board has no mounted modes",
                hint="add mounting-hole footprints / NPTH pads to the layout, or pass "
                     "mount_holes=[[x_mm, y_mm], ...] (optionally [x, y, drill_d_mm]).",
                next_actions=["ecad_vibe_check(mount_holes=[[3,3],[37,3],[37,27],[3,27]])"])

        out_path = path or str(_default_out_dir(board_doc.get("name", "board"))
                               / "board_vibe.step")

        # ── FEA solid (thermal handoff, reused) + mass-corrected material ───────────
        try:
            from services.ecad.threed import board_to_fea_step, BoardGeometryError  # STAYS: no C++ counterpart
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

        try:
            board_geo = built["board"]
            components = built["components"]
            built_summary = built["summary"]
            built_assumptions = built["assumptions"]
            if (not isinstance(built_summary, dict)
                    or not isinstance(built_summary.get("warnings", []), list)
                    or not isinstance(built_assumptions, list)):
                raise ValueError("FEA builder returned malformed summary/assumptions")
            if not isinstance(board_geo, dict) or not isinstance(components, list):
                raise ValueError("FEA builder returned malformed board/components")
            bbox = [float(v) for v in board_geo["bbox_mm"]]
            if (len(bbox) != 4 or not all(math.isfinite(v) for v in bbox)
                    or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]):
                raise ValueError("board bbox must be finite and non-degenerate")
            outside = [
                hole["ref"] for hole in holes
                if not (bbox[0] <= hole["x_mm"] <= bbox[2]
                        and bbox[1] <= hole["y_mm"] <= bbox[3])
            ]
            if outside:
                raise ValueError("mount holes outside the board bbox: " + ", ".join(outside))
            mass_model = build_mass_model(built)
            material = structural_material(mass_model)
            fixtures = mount_fixture_facesets(holes, board_geo)
            warnings.extend(built_summary.get("warnings", []))
        except (KeyError, TypeError, ValueError) as exc:
            return _err(f"invalid structural setup: {exc}")

        assumptions = [
            f"structural material: isotropic FR4+Cu laminate screen, E = "
            f"{_FR4_E_MPA / 1000:g} GPa, nu = {_FR4_NU} (in-plane laminate nominals)",
            f"modal damping zeta = {zeta:g} (assumed, not measured)",
            "mount fixtures clamp a "
            f"{_MOUNT_LAND_MM} mm land annulus on both faces at each hole "
            "(drill bores are omitted from the FEA solid)",
        ] + mass_model["assumptions"] + [
            assumption for assumption in built_assumptions
            if isinstance(assumption, str)
            and ("fused" in assumption or "flattened" in assumption)]

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
                                      derived_by="services.ecad.threed.board_to_fea_step",
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
            from services.mcp import core_bridge as cb
        except Exception as e:  # noqa: BLE001
            cb = None
            solve_err: Optional[str] = f"core bridge import failed: {e}"
        if cb is not None:
            study = {"step_path": built["step_path"], "material": material,
                     "fixtures": fixtures, "mesh_order": 2,
                     "fineness": mesh_fineness}
            try:
                res = cb.call("sim_run_modal", {"study": study, "n_modes": n_modes})
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": str(e)}
            if not isinstance(res, dict):
                solve_err = "modal solver returned a non-object response"
            else:
                solve_err = None if res.get("ok") else res.get(
                    "error", "modal solve did not converge")
        if cb is None or solve_err:
            _VIBE["last"] = None
            reason = f"FEA leg unavailable: {solve_err}" if cb is None else \
                f"modal solve failed: {solve_err}"
            return _dumps(sh.attach({
                **base, "ok": False, "solved": False, "dry_run": False,
                "available": cb is not None, "error": reason, "error_solve": reason,
                "hint": ("the core mesher/solver is unavailable" if cb is None else
                         "the modal solver failed or did not converge"),
                "next_actions": [
                    "set CHUDWARE_LIBCHUDWARE to a sim-enabled build and retry"
                    if cb is None else
                    "check mount fixtures/material/mesh_fineness and retry"
                ],
            }, envelope))

        try:
            raw_freqs = res.get("freqs_hz")
            if not isinstance(raw_freqs, list):
                raise ValueError("freqs_hz must be a list")
            freqs = [float(freq) for freq in raw_freqs]
            modes, in_band = band_split(freqs, lo, hi)
        except (TypeError, ValueError) as exc:
            _VIBE["last"] = None
            return _dumps(sh.attach({
                **base, "ok": False, "solved": False, "dry_run": False,
                "error": f"invalid modal solver result: {exc}",
            }, envelope))
        elastic = [freq for freq in freqs if freq >= 0.5]
        if not elastic:
            _VIBE["last"] = None
            return _dumps(sh.attach({
                **base, "ok": False, "solved": False, "dry_run": False,
                "error": "modal solver returned no elastic modes",
                "modes": modes,
            }, envelope))
        f1 = elastic[0]
        band_verdict = (
            f"FLAG: {len(in_band)} natural frequenc"
            f"{'y' if len(in_band) == 1 else 'ies'} inside the {lo:g}-{hi:g} Hz "
            f"excitation band ({', '.join(f'{f:.0f}' for f in in_band)} Hz) — "
            "resonant amplification expected under this environment"
            if in_band else
            f"CLEAR: no natural frequency inside the {lo:g}-{hi:g} Hz excitation band "
            f"(first elastic mode {f1:.0f} Hz)")

        # ── response at the worst in-band mode + deflection screen ─────────
        deflect: dict = {"available": False}
        response: dict = {"available": False}
        response_error = None
        try:
            from services.sim.vibration import miles_response  # STAYS: no C++ counterpart
            shapes = res.get("mode_shapes")
            n_nodes = res.get("n_nodes")
            if (not isinstance(shapes, list) or not shapes
                    or isinstance(n_nodes, bool) or not isinstance(n_nodes, int)
                    or n_nodes <= 0):
                raise ValueError("modal solve returned no valid mode shapes/n_nodes")
            rand = miles_response(
                freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                total_mass_kg=mass_model["total_mass_g"] / 1000.0,
                excitation_dir=[0.0, 0.0, 1.0],
                psd_breakpoints=psd, zeta=zeta)
            per_mode = []
            for mode in rand["per_mode"]:
                converted = {
                    key: (float(value) if isinstance(value, (int, float)) else value)
                    for key, value in mode.items()
                }
                if (not isinstance(converted.get("freq_hz"), (int, float))
                        or not math.isfinite(float(converted["freq_hz"]))
                        or converted["freq_hz"] <= 0.0
                        or not isinstance(converted.get("grms"), (int, float))
                        or not math.isfinite(float(converted["grms"]))
                        or converted["grms"] < 0.0):
                    raise ValueError(
                        "random-response frequency must be > 0 and Grms must be >= 0")
                per_mode.append(converted)
            pm_in = [mode for mode in per_mode if lo <= mode["freq_hz"] <= hi]
            pool = pm_in or per_mode
            if not pool:
                raise ValueError("random response returned no participating modes")
            worst = max(pool, key=lambda mode: mode["grms"])
            total_grms = float(rand["total_grms"])
            quality = float(rand["Q"])
            if (not math.isfinite(total_grms) or total_grms <= 0.0
                    or not math.isfinite(quality) or quality <= 0.0):
                raise ValueError("random-response total Grms/Q must be finite and > 0")
            response = {
                "available": True,
                "worst_mode": worst,
                "worst_mode_in_band": bool(pm_in),
                "per_mode": per_mode,
                "total_grms": round(total_grms, 4),
                "zeta": zeta, "Q": quality,
                "citation": rand["citation"],
                "limitations": rand["limitations"],
            }
            z3 = deflection_3sigma_mm(worst["grms"], worst["freq_hz"])
            screen = steinberg_screen(components, board_geo, z3)
            n_ok = sum(1 for entry in screen["table"] if entry["pass"] is True)
            screen_complete = bool(screen["table"]) and screen["n_unchecked"] == 0
            screen_pass = screen_complete and screen["n_fail"] == 0
            verdict = (
                f"{'PASS' if screen_pass else 'INCOMPLETE/FAIL'} (HEURISTIC): "
                f"3-sigma board deflection {z3:.4f} mm at the worst "
                f"{'in-band ' if pm_in else ''}mode "
                f"({worst['freq_hz']:.0f} Hz, {worst['grms']:.2f} Grms) — "
                f"{n_ok} component(s) within the Steinberg deflection limit, "
                f"{screen['n_fail']} over, {screen['n_unchecked']} unchecked. "
                f"{screen['citation']}")
            deflect = {
                "available": True, "complete": screen_complete,
                "z_3sigma_mm": round(z3, 5),
                "at_freq_hz": round(worst["freq_hz"], 2),
                "mode_grms": round(worst["grms"], 4),
                **screen, "verdict": verdict, "pass": screen_pass,
            }
            if not pm_in:
                deflect["note"] = (
                    "no mode inside the excitation band — screened at the worst "
                    "overall mode (conservatively informative)")
        except (ImportError, KeyError, TypeError, ValueError) as exc:
            response_error = f"response/deflection unavailable: {type(exc).__name__}: {exc}"
            warnings.append(response_error)

        complete = bool(response.get("available") and deflect.get("complete"))
        passed = bool(complete and not in_band and deflect.get("pass"))
        result = sh.attach({
            **base, "ok": complete, "solved": True, "dry_run": False,
            "complete": complete, "pass": passed,
            "solver": "C++ core modal (tet10) + Miles'-equation modal response",
            "n_nodes": res.get("n_nodes"), "n_dof": res.get("n_dof"),
            "modes": modes, "f1_hz": round(f1, 2),
            "band": {"lo_hz": lo, "hi_hz": hi,
                     "in_band_freqs_hz": [round(freq, 2) for freq in in_band],
                     "pass": not in_band, "verdict": band_verdict},
            "response": response, "deflection_screen": deflect,
            "warnings": warnings,
            "hint": (f"solved: f1 = {f1:.0f} Hz; {band_verdict.split(':')[0]} in band; "
                     + (f"deflection screen "
                        f"{'PASS' if deflect.get('pass') else 'INCOMPLETE/FAIL'} "
                        f"(z3 = {deflect.get('z_3sigma_mm')} mm)"
                        if deflect.get("available") else
                        "deflection screen unavailable")),
            "next_actions": (
                ["repair missing response/mode-shape/component-limit data and re-run"]
                if not complete else
                ["stiffen the board (thicker laminate / extra mount) or move heavy "
                 "parts toward the mounts, then re-run ecad_vibe_check()"]
                if not passed else
                ["margins OK at the screening level — re-run with the real "
                 "qualification psd_breakpoints before sign-off"]),
        }, envelope)
        if not complete:
            result["error"] = response_error or (
                "deflection screen incomplete because component geometry was unchecked")
        _VIBE["last"] = result if complete else None
        return _dumps(result)
