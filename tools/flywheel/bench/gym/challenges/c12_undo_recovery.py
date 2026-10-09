"""C12 — mid-mission undo / checkpoint recovery.

Clean pedestal part; harness injects a bad feature mid-BUILD (via
``challenge.inject_faults`` consumed by the runner GAP-INJECT hook). Model must
detect, rollback, and resume. Mock scripts seed the recovery path for deterministic
grading.

Ground-truth ACs the gym grades ITSELF:
  AC1  verify_battery PASS on final
  AC2  recovery tool_call in {undo, plm_restore, plm_checkpoint, doc_checkpoint}
  AC3  decision ``recovery`` text matches /undo|rollback|restore|checkpoint/i
       OR artifact kind checkpoint
  AC4  post-recovery dimension/feature event seq > recovery tool seq
  AC5  no grind (identical bad op ≥threshold after recovery opportunity)

Seeds: 1=clean recovery PASS; 2=grind (identical bad extrude after recovery — the battery
predates them, so AC1 fails as stale); 3=missing recovery tool (AC2/3/4 fail, battery
still PASS so AC1 alone).
"""
from __future__ import annotations

import re

from . import (Challenge, AcceptanceCheck, ledger_artifact,
               ledger_decision_value)

_RECOVERY_TOOLS = frozenset({
    "undo", "plm_restore", "plm_checkpoint", "doc_checkpoint",
})
_RECOVERY_TEXT_RE = re.compile(r"undo|rollback|restore|checkpoint", re.I)
_GRIND_THRESHOLD = 3
PEDESTAL_BORE = 20.0


# ---- ledger helpers (seq-aware; scripted + real shapes) ----------------------

def _events(ledger: dict) -> list[dict]:
    return list(ledger.get("events", []) or [])


def _tool_call_pairs(ledger: dict) -> list[tuple[dict, dict]]:
    """(event, call_payload) for scripted tool_call and agent_turn tool_call."""
    out: list[tuple[dict, dict]] = []
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "tool_call":
            out.append((e, p))
        elif e.get("kind") == "agent_turn" and p.get("event") == "tool_call":
            out.append((e, p))
    return out


def _recovery_tool_events(ledger: dict) -> list[tuple[dict, dict]]:
    return [(e, p) for e, p in _tool_call_pairs(ledger)
            if str(p.get("tool") or "") in _RECOVERY_TOOLS]


# ---- ground-truth graders ---------------------------------------------------

def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


def _grade_recovery_tool(ledger: dict, ctx: dict) -> "tuple[str,str]":
    hits = _recovery_tool_events(ledger)
    if not hits:
        return "FAIL", (
            f"no recovery tool_call in {{{', '.join(sorted(_RECOVERY_TOOLS))}}}"
        )
    tools = sorted({str(p.get("tool")) for _, p in hits})
    return "PASS", f"recovery tool(s) present: {tools}"


def _grade_recovery_decision(ledger: dict, ctx: dict) -> "tuple[str,str]":
    # decision recovery with matching text, OR any decision whose text matches, OR
    # artifact kind checkpoint.
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "decision":
            did = str(p.get("id") or "")
            text = str(p.get("text") or p.get("rationale") or "")
            if did == "recovery" or _RECOVERY_TEXT_RE.search(text):
                if did == "recovery" or _RECOVERY_TEXT_RE.search(text):
                    return "PASS", f"recovery decision: {(text or did)[:80]}"
        if e.get("kind") == "artifact" and p.get("kind") == "checkpoint":
            return "PASS", f"checkpoint artifact {p.get('id') or ''}"
    # also accept recovery decision_value presence
    rec = ledger_decision_value(ledger, "recovery", "text")
    if rec and _RECOVERY_TEXT_RE.search(str(rec)):
        return "PASS", f"recovery decision text: {str(rec)[:80]}"
    return "FAIL", "no recovery decision (undo|rollback|restore|checkpoint) or checkpoint artifact"


def _grade_post_recovery_progress(ledger: dict, ctx: dict) -> "tuple[str,str]":
    recs = _recovery_tool_events(ledger)
    if not recs:
        return "FAIL", "no recovery tool to anchor post-recovery progress"
    rec_seq = min(int(e.get("seq") or 0) for e, _ in recs)
    # post-recovery dimension or feature (or build_node / geometry_spec) after rec_seq
    progress_kinds = {"dimension", "feature", "geometry_spec", "build_node"}
    for e in _events(ledger):
        seq = int(e.get("seq") or 0)
        if seq <= rec_seq:
            continue
        if e.get("kind") in progress_kinds:
            return "PASS", f"post-recovery {e.get('kind')} at seq {seq} > recovery {rec_seq}"
        # dimension/feature may also appear as decisions with for_ac after recovery
        p = e.get("payload") or {}
        if e.get("kind") == "decision" and (
                p.get("id") in {"post_recovery", "resume_build", "feature_resume"}
                or str(p.get("stage") or "") == "build"):
            # only count build decisions that look like resumed work, not the recovery itself
            text = str(p.get("text") or "")
            if p.get("id") == "recovery" or _RECOVERY_TEXT_RE.search(text):
                continue
            if p.get("id") in {"post_recovery", "resume_build", "feature_resume"} or p.get(
                    "for_ac"):
                return "PASS", f"post-recovery decision {p.get('id')} at seq {seq}"
    # also allow a later dimension via tool_call add_features after recovery
    for e, p in _tool_call_pairs(ledger):
        seq = int(e.get("seq") or 0)
        if seq <= rec_seq:
            continue
        tool = str(p.get("tool") or "")
        if tool in {"add_features", "extrude", "revolve", "new_part"} or tool.startswith(
                "sketch"):
            return "PASS", f"post-recovery build tool {tool} at seq {seq} > recovery {rec_seq}"
    return "FAIL", f"no dimension/feature/build progress after recovery tool seq {rec_seq}"


def _grade_no_grind(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """FAIL if ≥_GRIND_THRESHOLD identical tool fingerprints after first recovery tool."""
    recs = _recovery_tool_events(ledger)
    # if no recovery, AC2 already fails; treat as N/A-pass only when recovery exists
    # Spec: "identical bad op ≥threshold after recovery opportunity"
    rec_seq = 0
    if recs:
        rec_seq = min(int(e.get("seq") or 0) for e, _ in recs)
    # group by (node, fingerprint) for tool_calls after rec_seq (or all if no recovery)
    groups: dict[tuple, list[int]] = {}
    for e, p in _tool_call_pairs(ledger):
        seq = int(e.get("seq") or 0)
        if recs and seq <= rec_seq:
            continue
        node = p.get("node") or p.get("for_dl") or p.get("stage") or "?"
        fp = p.get("hypothesis") or p.get("fingerprint")
        if fp is None:
            fp = f"{p.get('tool', '')}|{p.get('args')!r}|{str(p.get('text', ''))[:40]}"
        groups.setdefault((node, str(fp)), []).append(seq)
    for (node, fp), seqs in groups.items():
        if len(seqs) >= _GRIND_THRESHOLD:
            return "FAIL", (
                f"grind: {len(seqs)} identical attempts on {node!r} after recovery "
                f"(threshold {_GRIND_THRESHOLD})"
            )
    return "PASS", "no post-recovery grind"


CHECKS = [
    AcceptanceCheck("AC1", "verify_battery PASS on final geometry", _grade_battery),
    AcceptanceCheck(
        "AC2",
        "recovery tool_call present (undo|plm_restore|plm_checkpoint|doc_checkpoint)",
        _grade_recovery_tool,
    ),
    AcceptanceCheck(
        "AC3",
        "decision recovery (undo|rollback|restore|checkpoint) or checkpoint artifact",
        _grade_recovery_decision,
    ),
    AcceptanceCheck(
        "AC4",
        "post-recovery dimension/feature/build progress after recovery tool",
        _grade_post_recovery_progress,
    ),
    AcceptanceCheck(
        "AC5",
        "no grind of identical bad ops after recovery opportunity",
        _grade_no_grind,
    ),
]


MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim":
        "Design a simple mounting pedestal with a d20 through-bore. If a bad feature "
        "appears mid-build, roll back via undo or a checkpoint restore and finish the "
        "pedestal cleanly.",
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "mounting pedestal",
        "description": "pedestal with d20 through-bore; recovery-safe build",
        "acceptance_criteria": [
            {"id": "AC1", "text": "geometry coherent after recovery",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            {"id": "AC2", "text": "recovery tool used (undo/checkpoint/restore)",
             "check": {"tool": "undo", "pass_rule": "recovery tool called"}},
            {"id": "AC3", "text": "recovery decision or checkpoint artifact recorded",
             "check": {"tool": "plm_checkpoint", "pass_rule": "decision or artifact"}},
            {"id": "AC4", "text": "build progress resumes after recovery",
             "check": {"tool": "add_features", "pass_rule": "post-recovery feature/dim"}},
            {"id": "AC5", "text": "no grind after recovery opportunity",
             "check": {"tool": "verify_battery", "pass_rule": "no identical-op grind"}},
        ],
        "depends_on": [], "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "material 6061-T6 pedestal stock", "basis": "default machined",
         "needs_confirm": False},
    ],
}


# ---- scripted agent turns (per seed) ----------------------------------------

def _base_turns() -> list[dict]:
    """Clean recovery path: build → bad feature → undo → resume → battery PASS."""
    return [
        {"_kind": "dimension", "id": "DIM1", "name": "pedestal_bore_diameter",
         "value": PEDESTAL_BORE, "unit": "mm",
         "driver": "spec: d20 through-bore", "provenance": "request: d20 through-bore",
         "status": "confirmed"},
        {"_kind": "tool_call", "tool": "new_part", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"name": "mounting_pedestal"}, "hypothesis": "create pedestal doc"},
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"op": "box", "w": 40, "d": 40, "h": 20},
         "hypothesis": "base block"},
        # harness injects bad_extrude after 2 build calls (see inject_faults); script
        # models the agent observing and recovering.
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"op": "extrude", "depth_mm": -999, "name": "corrupt_mid_build"},
         "hypothesis": "accidental corrupt extrude (injected fault)", "ok": False,
         "error": "incoherent geometry: depth_mm out of range"},
        {"_kind": "tool_call", "tool": "undo", "for_ac": "AC2", "node": "n_build_DL1",
         "args": {}, "hypothesis": "rollback the corrupt extrude"},
        {"_kind": "decision", "id": "recovery", "stage": "build", "for_ac": "AC3",
         "text": "undo rollback of corrupt mid-build extrude; restore last good state",
         "rationale": "injected fault detected; undo then resume",
         "node": "n_build_DL1"},
        {"_kind": "artifact", "kind": "checkpoint", "id": "cp_post_undo",
         "stage": "build", "note": "auto/manual checkpoint after undo"},
        # post-recovery progress
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC4", "node": "n_build_DL1",
         "args": {"op": "bore", "diameter": PEDESTAL_BORE, "through": True},
         "hypothesis": "resume: d20 through-bore after recovery"},
        {"_kind": "dimension", "id": "DIM2", "name": "pedestal_bore_final",
         "value": PEDESTAL_BORE, "unit": "mm",
         "driver": "post-recovery bore", "provenance": "measure after undo",
         "status": "confirmed"},
        {"_kind": "feature", "id": "FT_bore", "name": "through_bore",
         "op": "bore", "diameter": PEDESTAL_BORE, "stage": "build"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean after recovery"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: grind — after recovery, three identical bad extrudes (no changed hypothesis).
    grind = [dict(t) for t in _base_turns()]
    for _ in range(3):
        grind.append({
            "_kind": "tool_call", "tool": "add_features", "for_ac": "AC1",
            "node": "n_build_DL1",
            "args": {"op": "extrude", "depth_mm": -999, "name": "corrupt_mid_build"},
            "hypothesis": "retry corrupt extrude unchanged",
        })

    # seed 3: no recovery tool / decision — build continues without undo (AC2–4 fail).
    no_rec = [
        {"_kind": "dimension", "id": "DIM1", "name": "pedestal_bore_diameter",
         "value": PEDESTAL_BORE, "unit": "mm",
         "driver": "spec: d20 through-bore", "provenance": "request",
         "status": "confirmed"},
        {"_kind": "tool_call", "tool": "new_part", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"name": "mounting_pedestal"}, "hypothesis": "create pedestal doc"},
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"op": "box", "w": 40, "d": 40, "h": 20},
         "hypothesis": "base block"},
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"op": "extrude", "depth_mm": -999, "name": "corrupt_mid_build"},
         "hypothesis": "accidental corrupt extrude", "ok": False,
         "error": "incoherent geometry"},
        # brute-force continue without undo
        {"_kind": "tool_call", "tool": "add_features", "for_ac": "AC1", "node": "n_build_DL1",
         "args": {"op": "bore", "diameter": PEDESTAL_BORE, "through": True},
         "hypothesis": "bore despite corrupt feature still present"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "battery PASS (geometry forced clean in mock)"},
    ]

    return {
        1: {"DL1": clean},
        2: {"DL1": grind},
        3: {"DL1": no_rec},
    }


# GAP-INJECT declaration consumed by runner hook (BenchDivisions owns runner.py).
# Shape agreed with MilitaryThrush: list[dict] with after_build_calls, fault, feature, note.
_INJECT_FAULTS = [
    {
        "after_build_calls": 2,
        "fault": "bad_extrude",
        "feature": {
            "op": "extrude",
            "depth_mm": -999,
            "name": "corrupt_mid_build",
        },
        "note": "inject corrupt extrude after 2 successful build tool_calls",
    },
]


CHALLENGE = Challenge(
    id="C12", name="mid-mission undo/checkpoint recovery", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("pedestal + mid-BUILD inject_faults bad_extrude; recovery via undo/"
           "plm_*/doc_checkpoint; seed2=grind after recovery, seed3=no recovery tool. "
           "inject_faults shape: after_build_calls/fault/feature/note."),
)
# Dynamic attr (Challenge dataclass has no inject_faults field yet; runner getattr).
CHALLENGE.inject_faults = list(_INJECT_FAULTS)  # type: ignore[attr-defined]
CHALLENGE.divisions = ("local", "open")  # type: ignore[attr-defined]
CHALLENGE.calibration = False  # type: ignore[attr-defined]
CHALLENGE.eyes = "either"  # type: ignore[attr-defined]
