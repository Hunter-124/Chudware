"""Capability truth for the permanently offline robot planner."""
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
        "plan": "offline reachability, collision and trajectory planning is implemented",
        "generate": "non-executable trajectory artifact generation is implemented",
        "simulate": "deterministic ideal mock-controller replay is implemented",
    }
    unsupported = {
        "post": "vendor-native executable robot programs are deliberately not emitted",
        "parse": "capability advertisement is capped at plan/generate/simulate for P4",
        "monitor": "robot subsystem defines no observation producer",
        "transport": "no device transport exists in the robot subsystem",
        "execute": "no vendor controller qualification or command path exists",
        "safety_integration": "cell integrator owns safety assessment; no safety method is implemented",
    }
    levels = [
        CapabilityTruth(level, "verified", verified[level]) if level in verified
        else CapabilityTruth(level, "unsupported", unsupported[level])
        for level in LEVELS
    ]
    return {
        "subject_id": "robot:offline",
        "max_advertised": "simulate",
        "device_command_owner": None,
        "levels": levels,
    }
