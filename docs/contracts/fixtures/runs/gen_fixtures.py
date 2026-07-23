#!/usr/bin/env python3
"""Generate committed fixture run directories for the T-W17 transparency panels.

These run dirs are the *replayable troubleshooting mode*: the Agent Inspector,
Watch Grid, and Settings panels render from them with **no live backend** — this
is also how the CHUDWARE_SHOT scenarios capture headlessly.

Layout produced under docs/contracts/fixtures/runs/:

  r_demo_bracket/                 (populated run — inspector + watch)
    events.jsonl                  aicad.ledger event log (one JSON event/line)
    advisories.jsonl              aicad.advisory/1 blocks per seq
    preflight/pf_*.json           aicad.preflight/1 records
    audits/aud_*.json             aicad.audit/1 records
    delegation.json               aicad.delegation/1 tree
    failures/fc_*.json            aicad.failure/1 cards
    workspace_status.json         aicad.workspace status (docs+owners+renders)
    renders/<doc>.png             latest render artifact per doc (real PNGs)
  r_empty/                        (empty run — inspector empty state)
    events.jsonl                  (just a run.started, no turns)
    workspace_status.json         (no docs)

Schemas: contracts/advisor.md §2 §3 §10 §11 §12 §5; workspace shape from
work/tasks/T-W16.md (per-doc streams + workspace_status).
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# Render PNGs — simple deterministic part thumbnails (stand in for real render
# artifacts; the watch grid only needs a loadable PNG per doc).
# --------------------------------------------------------------------------

def _part_png(path: Path, label: str, rgb: tuple[int, int, int]) -> None:
    W, H = 320, 240
    img = Image.new("RGB", (W, H), (24, 27, 33))
    d = ImageDraw.Draw(img)
    # faux isometric box
    cx, cy = W // 2, H // 2
    fr, fg, fb = rgb
    top = [(cx, cy - 70), (cx + 90, cy - 25), (cx, cy + 20), (cx - 90, cy - 25)]
    left = [(cx - 90, cy - 25), (cx, cy + 20), (cx, cy + 90), (cx - 90, cy + 45)]
    right = [(cx + 90, cy - 25), (cx, cy + 20), (cx, cy + 90), (cx + 90, cy + 45)]
    d.polygon(top, fill=(min(fr + 40, 255), min(fg + 40, 255), min(fb + 40, 255)))
    d.polygon(left, fill=(int(fr * 0.6), int(fg * 0.6), int(fb * 0.6)))
    d.polygon(right, fill=rgb)
    d.text((10, H - 22), label, fill=(210, 214, 222))
    img.save(path)


# --------------------------------------------------------------------------
# Populated run: r_demo_bracket
# --------------------------------------------------------------------------

def gen_populated() -> None:
    run = HERE / "r_demo_bracket"
    (run / "renders").mkdir(parents=True, exist_ok=True)
    (run / "preflight").mkdir(exist_ok=True)
    (run / "audits").mkdir(exist_ok=True)
    (run / "failures").mkdir(exist_ok=True)

    ts = "2026-07-04T09:12:{:02d}Z"

    # ---- ledger event log (events.jsonl) -----------------------------------
    # Each line: {seq, ts, actor, kind, payload}. Turn boundaries are marked by
    # 'turn.start'/'turn.end' with the verbatim injected context on turn.start.
    events = []
    seq = 0

    def ev(actor, kind, payload):
        nonlocal seq
        events.append({"seq": seq, "ts": ts.format(seq % 60), "actor": actor,
                       "kind": kind, "payload": payload})
        seq += 1

    ev("human", "run.started", {"run_id": "r_demo_bracket",
        "brief": {"title": "Bracket for 25mm shaft",
                  "summary": "Motor mount bracket, bore fits a 25 mm shaft H7/g6"}})

    # Turn 1 — orchestrator plans (verbatim ledger render injected).
    ev("orchestrator", "turn.start", {
        "turn": 1, "stage": "layout",
        "injected": {
            "ledger_render": ("[BRIEF] Bracket for 25mm shaft\n"
                              "  Motor mount bracket, bore fits a 25 mm shaft H7/g6\n"
                              "[REQUIREMENTS]\n  R1 bore fits 25mm shaft (sliding) [open]\n"),
            "advisory": None,
            "playbook_step": "layout: establish datums and primary references first",
        }})
    ev("orchestrator", "tool.call", {"turn": 1, "tool": "add_features",
        "args_digest": "datum plane + base sketch", "preflight": "pf_0001"})
    ev("orchestrator", "decision", {"turn": 1, "id": "D1", "stage": "layout",
        "text": "Base plate 60x40x8, boss centered", "rationale": "clears shaft + bolt pattern"})
    ev("orchestrator", "turn.end", {"turn": 1, "status": "ok"})

    # Turn 2 — feature_modeler bores the hole; advisory fires (interference).
    ev("feature_modeler", "turn.start", {
        "turn": 2, "stage": "feature",
        "injected": {
            "ledger_render": ("[BRIEF] Bracket for 25mm shaft\n"
                              "[DIMENSIONS]\n  DIM1 bore_diameter 24.98 mm (proposed)\n"),
            "advisory": ("[ADVISORY seq=14 stage=feature severity=action]\n"
                         "  [action] interference: bore d24.98 vs shaft d25.00 — 340 mm3 overlap\n"
                         "    fix: edit_feature_param (feature=F7, param=d, value=25.02) "
                         "— ISO 286 H7/g6 on d25 ⇒ hole 25.000..25.021 [table]\n"
                         "[/ADVISORY]"),
            "playbook_step": "feature: honor ADVISORY numbers as ground truth",
        }})
    ev("feature_modeler", "tool.call", {"turn": 2, "tool": "add_features",
        "args_digest": "bore F7 d24.98", "preflight": "pf_0002"})
    ev("foreman", "advisory", {"turn": 2, "seq": 14, "stage": "feature",
        "severity": "action", "ref": "advisories.jsonl#14"})
    ev("feature_modeler", "tool.call", {"turn": 2, "tool": "edit_feature_param",
        "args_digest": "F7.d = 25.02 (applied advisory fix)", "preflight": "pf_0003"})
    ev("feature_modeler", "dimension", {"turn": 2, "id": "DIM1", "name": "bore_diameter",
        "value": 25.02, "unit": "mm", "status": "confirmed",
        "driver": "sliding fit on shaft S1 (H7/g6)"})
    ev("feature_modeler", "turn.end", {"turn": 2, "status": "ok"})

    # Turn 3 — a rebuild failure the fixer retries (failure card).
    ev("fixer", "turn.start", {
        "turn": 3, "stage": "feature",
        "injected": {
            "ledger_render": "[FAILURE fc_0001] fillet r=5 exceeds wall 3.2 on F9\n",
            "advisory": None,
            "playbook_step": "fixer: one candidate fix per attempt; record predicted vs measured",
        }})
    ev("foreman", "failure", {"turn": 3, "ref": "failures/fc_0001.json"})
    ev("fixer", "tool.call", {"turn": 3, "tool": "edit_feature_param",
        "args_digest": "F9 fillet r 5 -> 2.5", "preflight": "pf_0004"})
    ev("fixer", "turn.end", {"turn": 3, "status": "ok"})

    # Turn 4 — overseer audit at stage exit.
    ev("overseer", "audit", {"turn": 4, "ref": "audits/aud_0001.json"})
    ev("overseer", "turn.end", {"turn": 4, "status": "ok"})

    ev("orchestrator", "run.done", {"status": "done", "turns": 4})

    (run / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    # ---- advisories.jsonl --------------------------------------------------
    adv = {
        "schema": "aicad.advisory/1", "seq": 14, "stage": "feature",
        "items": [{
            "id": "adv_0001", "severity": "action", "check": "interference",
            "finding": "bodies bracket/shaft interfere 340 mm3 near [12,0,5]; bore d24.98 vs shaft d25.00",
            "refs": {"doc_id": "doc_bracket", "bodies": ["bracket", "shaft"], "features": ["F7"]},
            "numbers": {"overlap_mm3": 340, "bore_d": 24.98, "shaft_d": 25.0},
            "candidate_fixes": [{
                "action": "edit_feature_param",
                "params": {"feature": "F7", "param": "d", "value": 25.02},
                "basis": "ISO 286 H7/g6 sliding fit on d25 -> hole 25.000..25.021",
                "predicted_effect": "clearance 0.007..0.041 mm", "confidence": "table"}],
            "helper_tools": ["fit_calc", "measure_between"],
            "source": {"kind": "kernel", "detail": "doc_eval_interference"}}],
    }
    (run / "advisories.jsonl").write_text(json.dumps(adv) + "\n", encoding="utf-8")

    # ---- preflight/pf_*.json -----------------------------------------------
    preflights = {
        "pf_0001": {"verdict": "pass", "tool": "add_features",
                    "checks": [{"check": "refs_resolve", "result": "pass", "detail": "datum + base sketch resolve"}]},
        "pf_0002": {"verdict": "warn", "tool": "add_features",
                    "checks": [{"check": "downstream_impact", "result": "warn",
                                "detail": "bore d24.98 will interfere with shaft d25.00", "refs": {"features": ["F7"]}}]},
        "pf_0003": {"verdict": "pass", "tool": "edit_feature_param",
                    "checks": [{"check": "params_sane", "result": "pass", "detail": "d=25.02 within stock"}]},
        "pf_0004": {"verdict": "fail", "tool": "edit_feature_param",
                    "checks": [{"check": "rebuild_predict", "result": "fail",
                                "detail": "fillet r=5 exceeds adjacent wall 3.2 on F9", "refs": {"features": ["F9"]}}]},
    }
    for pid, body in preflights.items():
        rec = {"schema": "aicad.preflight/1", "id": pid,
               "target": {"doc_id": "doc_bracket", "tool": body["tool"], "ops_hash": pid},
               "verdict": body["verdict"], "checks": body["checks"],
               "predicted": {"feature_status": [], "dof": None, "interference_delta": None},
               "downstream": [], "advisory_items": []}
        (run / "preflight" / f"{pid}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")

    # ---- audits/aud_0001.json ----------------------------------------------
    audit = {
        "schema": "aicad.audit/1", "id": "aud_0001", "trigger": "stage_exit",
        "scope": {"run_id": "r_demo_bracket", "stage": "feature", "roles": ["feature_modeler", "fixer"], "buildgraph_nodes": []},
        "verdicts": [
            {"class": "ok", "finding": "bore dimension traces to H7/g6 fit calc; provenance sound",
             "evidence": {"ledger_events": [8, 9], "advisories": ["adv_0001"], "handoffs": [], "preflight": ["pf_0003"]},
             "required_action": {"kind": "none", "detail": "", "dispatch_role": None}, "severity": "info"},
            {"class": "bad_fix", "finding": "fillet reduced to 2.5 without confirming aesthetic radius requirement",
             "evidence": {"ledger_events": [15], "advisories": [], "handoffs": [], "preflight": ["pf_0004"]},
             "required_action": {"kind": "ask_user", "detail": "confirm min fillet radius", "dispatch_role": None},
             "severity": "action"}],
        "model": {"provider": "anthropic", "id": "claude-sonnet"}, "tokens": 1840,
    }
    (run / "audits" / "aud_0001.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")

    # ---- delegation.json ---------------------------------------------------
    delegation = {
        "schema": "aicad.delegation/1", "depth_cap": 3,
        "nodes": [
            {"id": "dg_root", "parent": None, "role": "orchestrator", "brief": "Build bracket for 25mm shaft",
             "requested_by": "human", "approval": {"policy": "auto", "approved_by": "auto", "ts": ts.format(1)},
             "model": {"provider": "anthropic", "id": "claude-sonnet", "tier": "frontier"},
             "budget": {"tokens_granted": 40000, "tokens_spent": 18200, "attempts": 1},
             "docs": [{"doc_id": "doc_bracket", "lease": "write"}], "status": "running"},
            {"id": "dg_feat", "parent": "dg_root", "role": "feature_modeler", "brief": "Bore F7 to fit 25mm shaft",
             "requested_by": "orchestrator", "approval": {"policy": "auto", "approved_by": "auto", "ts": ts.format(20)},
             "model": {"provider": "llamacpp", "id": "qwen2.5-coder-7b", "tier": "local"},
             "budget": {"tokens_granted": 12000, "tokens_spent": 6100, "attempts": 1},
             "docs": [{"doc_id": "doc_bracket", "lease": "write"}], "status": "done"},
            {"id": "dg_fix", "parent": "dg_root", "role": "fixer", "brief": "Resolve fillet rebuild failure on F9",
             "requested_by": "orchestrator", "approval": {"policy": "overseer", "approved_by": "overseer", "ts": ts.format(35)},
             "model": {"provider": "anthropic", "id": "claude-sonnet", "tier": "strong"},
             "budget": {"tokens_granted": 8000, "tokens_spent": 2400, "attempts": 2},
             "docs": [{"doc_id": "doc_bracket", "lease": "write"}], "status": "done"}],
    }
    (run / "delegation.json").write_text(json.dumps(delegation, indent=2), encoding="utf-8")

    # ---- failures/fc_0001.json ---------------------------------------------
    failure = {
        "schema": "aicad.failure/1", "id": "fc_0001", "class": "rebuild", "stage": "feature",
        "what": "fillet r=5 exceeds adjacent wall 3.2 on F9",
        "where": {"doc_id": "doc_bracket", "features": ["F9"], "bodies": ["bracket"], "entities": [], "study": None},
        "why": {"kind": "deterministic", "detail": "requested radius > half local wall thickness"},
        "candidate_fixes": [{"action": "edit_feature_param", "params": {"feature": "F9", "param": "r", "value": 2.5},
                             "basis": "max stable fillet ~= 0.78 x wall", "predicted_effect": "rebuild succeeds", "confidence": "kernel"}],
        "helper_tools": ["wall_thickness"],
        "attempts": [
            {"n": 1, "fix_applied": {"param": "r", "value": 3.0}, "result": "same", "delta": {"status": "still failing"}},
            {"n": 2, "fix_applied": {"param": "r", "value": 2.5}, "result": "improved", "delta": {"status": "rebuild ok"}}],
        "budget": {"max_attempts": 3, "on_exhaust": "ask_user"},
    }
    (run / "failures" / "fc_0001.json").write_text(json.dumps(failure, indent=2), encoding="utf-8")

    # ---- renders + workspace_status (watch grid, 3 docs) -------------------
    _part_png(run / "renders" / "doc_bracket.png", "bracket", (94, 132, 214))
    _part_png(run / "renders" / "doc_shaft.png", "shaft", (196, 152, 84))
    _part_png(run / "renders" / "doc_bushing.png", "bushing", (120, 190, 128))

    workspace = {
        "schema": "aicad.workspace/1", "run_id": "r_demo_bracket",
        "docs": [
            {"doc_id": "doc_bracket", "title": "Bracket", "owner": "feature_modeler",
             "model": "qwen2.5-coder-7b", "node_status": "building",
             "latest_render": "renders/doc_bracket.png", "lease": "write"},
            {"doc_id": "doc_shaft", "title": "Shaft S1", "owner": "assembly_fit_engineer",
             "model": "claude-sonnet", "node_status": "done",
             "latest_render": "renders/doc_shaft.png", "lease": "read"},
            {"doc_id": "doc_bushing", "title": "Bushing", "owner": "feature_modeler",
             "model": "qwen2.5-coder-7b", "node_status": "failed",
             "latest_render": "renders/doc_bushing.png", "lease": "write"}],
    }
    (run / "workspace_status.json").write_text(json.dumps(workspace, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Empty run: r_empty
# --------------------------------------------------------------------------

def gen_empty() -> None:
    run = HERE / "r_empty"
    run.mkdir(parents=True, exist_ok=True)
    ev = {"seq": 0, "ts": "2026-07-04T10:00:00Z", "actor": "human",
          "kind": "run.started",
          "payload": {"run_id": "r_empty", "brief": {"title": "Empty run", "summary": "no turns yet"}}}
    (run / "events.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
    (run / "workspace_status.json").write_text(
        json.dumps({"schema": "aicad.workspace/1", "run_id": "r_empty", "docs": []}, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    gen_populated()
    gen_empty()
    print("fixtures written under", HERE)
