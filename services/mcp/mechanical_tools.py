"""Mechanical engineering MCP tools — AGMA gear tooth stress + ISO 281 bearing life.

  gear_check(...)   -> AGMA bending + contact stress, safety factors, disclaimer
  bearing_life(...) -> ISO 281 L10 life from load + speed + dynamic load rating (C)

Both tools expose deterministic closed-form engineering checks — no solver required.
Dynamic load rating C is a required explicit input: the parts catalog (bearing:ISO 15:*)
carries geometry (bore/od/width) only, not C. Supply C from the bearing's own datasheet.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP
from services.mcp import core_bridge as cb
from services.mcp.cad_tools import _dumps, _err


def register_mechanical_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    def gear_check(
        Wt: float,
        module: float,
        face_width: float,
        pitch_diameter: float,
        J: float,
        I: float,
        Ko: float = 1.0,
        Kv: float = 1.0,
        Ks: float = 1.0,
        Km: float = 1.0,
        KB: float = 1.0,
        Cf: float = 1.0,
        Cp: float = 191.0,
        allowable_bending_stress: Optional[float] = None,
        allowable_contact_stress: Optional[float] = None,
    ) -> str:
        """AGMA spur-gear tooth bending and contact (pitting) stress check.

        Implements AGMA 2001 bending (Eq. 14-15 Shigley's) and contact (Eq. 14-16) stress
        equations. Safety factors reported when allowable stresses are supplied.

        Args:
            Wt: Tangential (transmitted) load at pitch cylinder, N.
            module: Transverse module (= pitch diameter / number of teeth), mm.
            face_width: Gear face width, mm.
            pitch_diameter: Pitch diameter of the pinion, mm.
            J: Bending-strength geometry factor (AGMA 908; supply from charts or textbook).
            I: Contact-strength geometry factor (AGMA 908; supply from charts or textbook).
            Ko: Overload factor (>=1.0; default 1.0 = smooth power source).
            Kv: Dynamic factor (>=1.0; accounts for pitch-line velocity error).
            Ks: Size factor (default 1.0; enter from AGMA table for large gears).
            Km: Load-distribution factor (>=1.0; default 1.0 for narrow, well-aligned).
            KB: Rim-thickness factor (bending equation only; default 1.0 = solid gear).
            Cf: Surface condition factor (contact equation only; default 1.0 normal finish).
            Cp: Elastic coefficient, MPa^0.5 (default 191.0 = steel/steel, Shigley's Table 14-8).
            allowable_bending_stress: S_t (MPa) -- optional. Required for SF_bending.
            allowable_contact_stress: S_c (MPa) -- optional. Required for SF_contact.

        Returns JSON {ok, sigma_bending_MPa, sigma_contact_MPa, SF_bending, SF_contact, disclaimer}.
        SF values are null when allowable stresses are not supplied.

        Reference: Budynas & Nisbett, Shigley's Mechanical Engineering Design, 10th ed., Ch. 14.
        AGMA 2001-D04 (formulas are publicly described in open textbooks; this is a clean-room
        implementation from those published equations -- not a copy of the standard).

        Preliminary estimate only -- see disclaimer in response.
        """
        # The C++ core (core/mechanical/mechanical.cpp) returns the full
        # {ok, sigma_*, SF_*, disclaimer} envelope — same shape the Python wrapper emitted.
        try:
            return _dumps(cb.call("gear_check", {
                "Wt": Wt, "module": module, "face_width": face_width,
                "pitch_diameter": pitch_diameter, "J": J, "I": I,
                "Ko": Ko, "Kv": Kv, "Ks": Ks, "Km": Km, "KB": KB, "Cf": Cf, "Cp": Cp,
                "allowable_bending_stress": allowable_bending_stress,
                "allowable_contact_stress": allowable_contact_stress,
            }))
        except Exception as e:
            return _err(e)

    @mcp.tool()
    def bearing_life(
        C: float,
        Fr: float,
        Fa: float = 0.0,
        n_rpm: float = 1000.0,
        bearing_type: str = "ball",
        X: float = 1.0,
        Y: float = 0.0,
        bearing_designation: str = "",
    ) -> str:
        """ISO 281 rolling-element bearing L10 fatigue life estimate.

        Computes L10 life from dynamic load rating, applied load, and shaft speed.

        Args:
            C: Dynamic load rating, N. REQUIRED. Supply from bearing manufacturer datasheet
               or catalog specification. The parts catalog (bearing:ISO 15:*) carries
               geometry (bore/od/width) only -- it does NOT carry C. Do not fabricate a rating.
            Fr: Radial load, N.
            Fa: Axial load, N (default 0).
            n_rpm: Shaft speed, rpm.
            bearing_type: "ball" (p=3, default) or "roller" (p=10/3).
            X: Radial load factor (default 1.0 for pure radial).
            Y: Axial load factor (default 0.0 for pure radial).
            bearing_designation: Optional bearing designation (e.g. "6205") for reference only.
                                  Geometry is looked up for context, but C is NOT taken from
                                  the catalog -- always pass C explicitly.

        Returns JSON {ok, L10_million_rev, L10h_hours, P_equiv_N, p_exponent,
                      bearing_designation, disclaimer}.

        Reference: ISO 281:2007 (life equation); Shigley's Mechanical Engineering Design,
        10th ed., Ch. 11 (Budynas & Nisbett). Formula is published in open textbooks;
        this is a clean-room implementation.

        Preliminary estimate only -- see disclaimer in response.
        """
        # The C++ core (core/mechanical/mechanical.cpp) returns the full
        # {ok, L10_*, P_equiv_N, disclaimer} envelope — same shape as the Python wrapper.
        try:
            return _dumps(cb.call("bearing_life", {
                "C": C, "Fr": Fr, "Fa": Fa, "n_rpm": n_rpm,
                "bearing_type": bearing_type, "X": X, "Y": Y,
                "bearing_designation": bearing_designation,
            }))
        except Exception as e:
            return _err(e)
