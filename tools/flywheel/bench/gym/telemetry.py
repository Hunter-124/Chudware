"""Gym -> Pillar 16 telemetry wiring (T-W27 DoD step 6).

Every gym cycle is training signal. This writes an ``events.jsonl`` per cycle in the
telemetry wire format (contracts/telemetry.md §3/§5): a leading session-envelope record
``{consent, origin, app_version, os}`` followed by one ``gym.cell`` event per cell
carrying the challenge/tier/seed, the independent grading verdict, and the taxonomy
classes with their evidence refs. The existing flywheel exporter (tools/flywheel/export.py)
iterates run dirs by this exact shape, so a gym cycle is exportable as SFT/correction
signal with no exporter change.

Origin is ``team`` (dogfood corpus, per the Pillar 16 team-dogfood decision) → default
consent ``full``; if the resolved app config has ``offline_enabled`` the tier is
hard-forced to ``off`` at the emitter (mirrors export.py `_offline_enabled`). We do not
re-resolve config here (the gym is a dev tool); we stamp ``full``/``team`` and let the
exporter re-verify, which is the contract's audit model.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any


def _session_envelope() -> dict[str, Any]:
    return {
        "consent": "full",          # team dogfood default; exporter re-verifies
        "origin": "team",
        "app_version": "gym-T-W27",
        "os": platform.system().lower(),
    }


def write_cycle_telemetry(report: dict[str, Any], out_dir: Path) -> Path:
    """Write a gym cycle's events.jsonl (envelope + per-cell training-signal events).

    Reads the assembled report dict (from report.build_report). Returns the path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"

    lines: list[str] = [json.dumps(_session_envelope())]

    # One training-signal event per cell. We reconstruct per-cell rows from the report's
    # taxonomy evidence + pass matrix so the record is self-contained (no run-dir crawl).
    cycle = report["cycle"]
    mode = report["execution_mode"]
    # Flatten taxonomy evidence back to (challenge,tier,seed) -> [classes].
    per_cell_classes: dict[tuple, list[dict]] = {}
    for fc, evs in report["taxonomy"]["evidence"].items():
        for e in evs:
            key = (e["challenge"], e["tier"], e["seed"])
            per_cell_classes.setdefault(key, []).append(
                {"failure_class": fc, "detail": e["detail"],
                 "evidence_seqs": e["evidence_seqs"]})

    for key, cls in sorted(per_cell_classes.items(), key=lambda kv: str(kv[0])):
        ch, tier, seed = key
        lines.append(json.dumps({
            "type": "gym.cell",
            "cycle": cycle,
            "mode": mode,
            "challenge": ch, "tier": tier, "seed": seed,
            "failure_classes": cls,
            # a training-signal flag: mock cells are for harness/classifier proof, not
            # model-behaviour SFT — the exporter can filter on this.
            "training_usable": mode == "real",
        }))

    # Cells that fired NO failure class still matter (clean passes = positive signal).
    # Emit a coverage row so the exporter sees the full matrix, not just failures.
    lines.append(json.dumps({
        "type": "gym.cycle_summary",
        "cycle": cycle, "mode": mode,
        "pass_rates": report["pass_rates"],
        "taxonomy_counts": report["taxonomy"]["counts"],
        "n_cells": report["n_cells"],
        "training_usable": mode == "real",
    }))

    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_path
