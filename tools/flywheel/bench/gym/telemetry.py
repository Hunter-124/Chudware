"""Gym telemetry wiring: one ``events.jsonl`` per gym cycle.

Writes the telemetry wire format (contracts/telemetry.md §3/§5): a leading
session-envelope record ``{consent, origin, app_version, os}`` followed by one
``gym.cell`` event per cell carrying the challenge/tier/seed, the independent grading
verdict, and the taxonomy classes with their evidence refs, then a
``gym.cycle_summary`` record.

Origin is ``team`` (internal development runs) → default consent ``full``; if the resolved
app config has ``offline_enabled`` the tier is hard-forced to ``off`` at the emitter.
We do not re-resolve config here (the gym is a dev tool); we stamp ``full``/``team`` and
leave re-verification to any downstream consumer, which is the contract's audit model.
"""
from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any


def _session_envelope() -> dict[str, Any]:
    return {
        "consent": "full",          # team development default; consumers re-verify
        "origin": "team",
        "app_version": "gym",
        "os": platform.system().lower(),
    }


def write_cycle_telemetry(report: dict[str, Any], out_dir: Path) -> Path:
    """Write a gym cycle's events.jsonl (envelope + per-cell training-signal events).

    Reads the assembled report dict (from report.build_report). Returns the path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"

    lines: list[str] = [json.dumps(_session_envelope())]

    # One event per actual cell. Cell mode is execution truth; the cycle summary may
    # be mixed and must never promote a MOCK cell into training data.
    cycle = report["cycle"]
    cycle_mode = report["execution_mode"]
    cells = report.get("cells") or []
    for cell in sorted(
        cells,
        key=lambda item: str((
            item.get("challenge"), item.get("division"), item.get("tier"), item.get("seed"),
        )),
    ):
        mode = str(cell.get("mode") or "mock").lower()
        classes = []
        for classification in cell.get("classifications") or []:
            if not isinstance(classification, dict):
                continue
            classes.append({
                "failure_class": classification.get("failure_class"),
                "detail": classification.get("detail"),
                "evidence_seqs": classification.get("evidence_seqs") or [],
            })
        lines.append(json.dumps({
            "type": "gym.cell",
            "cycle": cycle,
            "mode": mode,
            "challenge": cell.get("challenge"),
            "division": cell.get("division") or "local",
            "tier": cell.get("tier"),
            "model": cell.get("model"),
            "seed": cell.get("seed"),
            "run_id": cell.get("run_id"),
            "passed": bool(cell.get("passed")),
            "failure_classes": classes,
            "training_usable": mode == "real",
        }))

    lines.append(json.dumps({
        "type": "gym.cycle_summary",
        "cycle": cycle, "mode": cycle_mode,
        "pass_rates": report["pass_rates"],
        "taxonomy_counts": report["taxonomy"]["counts"],
        "n_cells": report["n_cells"],
        "training_usable": cycle_mode == "real",
    }))

    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return events_path
