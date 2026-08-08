"""tool_guidance.em — curated options for the EM-solver + PCB-antenna tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "em_status": {
        "prompt_note": (
            "Capability probe before requesting full-wave work. builtin-fdfd eigenmode and "
            "analytic-uniform driven/time-domain are expected built-ins; inspect solvers/defaults. "
            "openEMS installed=false means full-wave is unavailable, never a successful fallback."
        ),
    },
    "em_eigenmode": {
        "prompt_note": (
            "Requires standard='WR-90' or waveguide JSON {shape,a_mm,b_mm,eps_r?}; default "
            "solver is builtin-fdfd. Frequencies are GHz in the result. Refine resolution and "
            "require TE10 error_vs_analytic_pct to settle before quoting cutoff; detail=True "
            "includes field data."
        ),
        "timeout_s": 300,
    },
    "em_driven": {
        "prompt_note": (
            "Uniform-guide default is analytic-uniform: ideal matched S21 propagation, not "
            "evidence for a discontinuity or antenna. Frequencies are GHz, length is mm. "
            "Call em_status before solver='openems'; absent openEMS returns ok:false/skipped. "
            "detail=True returns full S-parameter arrays."
        ),
        "timeout_s": 600,
    },
    "em_timedomain": {
        "prompt_note": (
            "Uniform-guide pulse delay/dispersion, not an S-parameter sweep. length is mm and "
            "f0_ghz/bw_ghz are GHz; zero values auto-derive a band. Default is analytic "
            "dispersive IFFT. Check em_status before openEMS; detail=True returns waveforms."
        ),
        "timeout_s": 600,
    },
    "ecad_antenna_design": {
        "prompt_note": (
            "Design first, then validate via ecad_antenna_analyze with the same "
            "params. meander_turns only affects ifa/pifa, velocity_factor only "
            "monopole; include_geometry=False returns dims only."
        ),
    },
    "ecad_antenna_analyze": {
        "prompt_note": (
            "Always returns a closed-form resonance estimate. want_fullwave=True additionally "
            "uses openEMS only when installed; otherwise fullwave.skipped is evidence of no "
            "full-wave result, not PASS. Frequencies are Hz. Refine n_freq/max_timesteps and "
            "check the full-wave block before trusting S11."
        ),
        "timeout_s": 600,
    },
}
