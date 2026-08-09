"""Capability truth for file-only PnP interchange and simulation."""
from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("plan", "generate", "post", "parse", "simulate", "monitor", "transport", "execute", "safety_integration")


@dataclass(frozen=True)
class CapabilityTruth:
    level: str
    truth: str
    why: str
    evidence_ref: tuple[str, ...] = ()


def capability_report() -> dict[str, object]:
    verified = {
        "plan": "board, panel and placement planning is implemented offline",
        "generate": "OpenPnP-shaped file generation is implemented",
        "simulate": "deterministic virtual-null placement simulation is implemented",
    }
    unsupported = {
        "post": "PnP interchange emits jobs, not machine-native executable programs",
        "parse": "capability advertisement is capped at plan/generate/simulate for P4",
        "monitor": "PnP subsystem defines no observation producer",
        "transport": "no device transport exists in the PnP subsystem",
        "execute": "no physical placement driver or actuation path exists",
        "safety_integration": "safety integration is outside this file-only simulation subsystem",
    }
    levels = [
        CapabilityTruth(level, "verified", verified[level]) if level in verified
        else CapabilityTruth(level, "unsupported", unsupported[level])
        for level in LEVELS
    ]
    return {"subject_id": "pnp:offline", "max_advertised": "simulate", "levels": levels}
