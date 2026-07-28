"""tool_guidance.em — curated options for the EM-solver + PCB-antenna tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "em_status": {
        "prompt_note": (
            "EM preflight: call before planning a full-wave run — reports whether "
            "openEMS is installed and the default solver per analysis. Never fails."
        ),
    },
    "em_eigenmode": {
        "prompt_note": (
            "Needs standard='WR-90' or a waveguide JSON dict — empty cross-section "
            "errors. fdfd runtime grows with resolution (40..200 sane); follow with "
            "em_driven for S-params."
        ),
        "timeout_s": 300,
    },
    "em_driven": {
        "prompt_note": (
            "Analytic-uniform default is an ideal matched straight section — it shows "
            "no mismatch/discontinuities. solver='openems' for real full-wave (check "
            "em_status first); detail=True for full arrays."
        ),
        "timeout_s": 600,
    },
    "em_timedomain": {
        "prompt_note": (
            "For pulse delay/dispersion through a guide, not S-params (that's "
            "em_driven). f0_ghz/bw_ghz=0 auto-derives the band from cutoff; raw "
            "waveforms only with detail=True."
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
            "Default want_fullwave=True runs a real FDTD S11 sweep when openEMS is "
            "installed (minutes) — pass False for a fast analytic estimate; check "
            "em_status first."
        ),
        "timeout_s": 600,
    },
}
