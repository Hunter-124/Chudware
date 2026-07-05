"""One-shot CLI shim over sim_advisor for non-Python callers (T-SIM3 GUI wiring).

The ImGui GUI (ui/imgui/src/ui/sim_manager.cpp) shells out to this module at
Run-dispatch time (precheck) and at result-arrival time (postcheck). Contract:

    python3 -m services.agent.advisor.sim_advisor_cli
        stdin:  {"mode": "precheck",  "study": {...}, "doc_geom": {...}?,
                 "ledger_run_id": "r_..."?, "study_id": "study-..."?}
             |  {"mode": "postcheck", "study": {...}, "precheck_items": [...]?,
                 "ledger_run_id": "r_..."?, "study_id": "study-..."?}
        stdout: {"ok": true, "items": [...]}                       (precheck)
             |  {"ok": true, "verdict": ..., "narration": ..., ...} (postcheck)
        exit 0 on success, non-zero + {"ok": false, "error": ...} on failure.

Pure passthrough — all policy/logic lives in sim_advisor.py (T-W9); this file
only decodes/encodes JSON so the C++ side never re-implements advisor rules.
The ``_anchor`` passthrough key is kept in precheck output so the caller can
round-trip it back into postcheck (`precheck_items`) unchanged.

Ledger (inspectability rule): when the caller supplies ``ledger_run_id`` (the GUI
passes AI_CAD_MISSION_RUN_ID when a mission context exists), one ``artifact``
event (kind ``study`` — contracts/advisor.md §2 shape) is appended to that run's
design ledger recording the advisory outcome. Ledger failures never fail the
advisory call (best-effort, mirrors Ledger._save()).
"""
from __future__ import annotations

import json
import pathlib
import sys

# Allow both `python -m services.agent.advisor.sim_advisor_cli` (repo cwd) and a
# direct-path invocation from the GUI (which knows only the repo root).
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from services.agent.advisor import sim_advisor  # noqa: E402


def _emit_ledger_event(req: dict, mode: str, note: str) -> None:
    """Best-effort ledger artifact event when a mission context exists (§2 shape)."""
    run_id = req.get("ledger_run_id")
    if not run_id:
        return
    try:
        from services.agent.ledger import Ledger  # noqa: PLC0415
        study_id = req.get("study_id") or "study-unknown"
        Ledger(run_id).append(
            actor="gui_sim",
            kind="artifact",
            payload={"kind": "study", "id": study_id, "path": "",
                     "stage": "sim", "note": f"advisor {mode}: {note}"},
        )
    except Exception:  # noqa: BLE001 — ledger must never fail the advisory
        pass


def main() -> int:
    try:
        req = json.load(sys.stdin)
        mode = req.get("mode")
        study = req.get("study") or {}
        if mode == "precheck":
            items = sim_advisor.precheck(study, req.get("doc_geom"))
            out = {"ok": True, "items": items,
                   "block_enabled": sim_advisor.PRECHECK_BLOCK_ENABLED}
            n_blockers = sum(1 for it in items if it.get("severity") == "blocker")
            n_actions = sum(1 for it in items if it.get("severity") == "action")
            _emit_ledger_event(req, mode,
                               f"{len(items)} item(s), {n_blockers} blocker(s), "
                               f"{n_actions} action(s)")
        elif mode == "postcheck":
            res = sim_advisor.postcheck(study, req.get("precheck_items"),
                                        req.get("doc_geom"))
            res["ok"] = True
            res["narration"] = sim_advisor.narrate_postcheck(res)
            out = res
            _emit_ledger_event(
                req, mode,
                f"verdict {res.get('verdict')} "
                f"({res.get('mat_class')}, SF={res.get('sf_used')})")
        else:
            raise ValueError(f"unknown mode {mode!r} (want precheck|postcheck)")
    except Exception as exc:  # noqa: BLE001 — single stderr surface for the GUI
        json.dump({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sys.stdout)
        return 1
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
