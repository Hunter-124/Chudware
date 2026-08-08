"""One-shot JSON CLI over :mod:`sim_advisor` for host or harness callers.

Contract::

    python3 -m services.agent.advisor.sim_advisor_cli
        stdin:  {"mode": "precheck", "study": {...}, "doc_geom": {...}?}
             |  {"mode": "postcheck", "study": {...}, "precheck_items": [...]?}
        stdout: {"ok": true, "items": [...], "block_enabled": bool}
             |  {"ok": true, "verdict": ..., "confidence": ...,
                 "narration": ..., "items": [...]}

The shim only decodes/encodes JSON. Setup policy, finite math, convergence evidence,
and analytic anchors live in sim_advisor.py. ``_anchor`` remains in precheck output so
a caller can round-trip the same independently computed anchor into postcheck.

When ``ledger_run_id`` is supplied, a best-effort study artifact event records the
outcome. Ledger failure never changes advisor success.
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
                f"verdict {res.get('verdict')}, confidence {res.get('confidence')} "
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
