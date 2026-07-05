"""Failure taxonomy classifier (T-W27).

Classifies what went wrong on a design-gym challenge by REPLAYING the ledger events
(schema ``{seq, ts, actor, kind, payload}`` — see services/agent/ledger.py) plus the
gym's own acceptance-criteria grading and the validator/battery outputs the run
recorded. Every classification carries EVIDENCE REFS (concrete event seq numbers + the
kinds/fields that triggered it) so a reviewer can replay exactly why a class fired — no
vibes, per the task's discipline rule.

The eight classes (task spec):

  wrong_question      clarifier asked a useless question, or MISSED a critical one that
                      the manifest flagged blocking. Evidence: question events vs the
                      manifest's blocking ambiguities.
  incoherent_geometry verify_battery / an acceptance-criterion check failed. Evidence:
                      the failed AC ids + the battery/check result event.
  tool_misuse         a tool was called with the wrong tool for the criterion, or with
                      args the preflight rejected. Evidence: the tool-call event + the
                      preflight/error event.
  context_loss        a role cited a ref (doc/feature/DL/AC/dimension id) that does not
                      exist in the ledger. Evidence: the citing event + the missing ref.
  abstraction_skip    a design-cognition-ladder validator bounced (rung emitted without
                      the required prior rung / omissions checklist). Evidence: the
                      ladder validator finding event.
  grind               >=N attempts on one node/deliverable without a changed hypothesis
                      (identical action payload), i.e. the anti-grind rule tripped.
                      Evidence: the repeated event seqs.
  advisory_ignored    an advisory was injected and then contradicted by a later action
                      without a dispute/replan. Evidence: the advisory event + the
                      contradicting action.

A single run can exhibit several classes; ``classify_ledger`` returns all that fire.
The classifier is deterministic and side-effect free.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class FailureClass(str, Enum):
    WRONG_QUESTION = "wrong_question"
    INCOHERENT_GEOMETRY = "incoherent_geometry"
    TOOL_MISUSE = "tool_misuse"
    CONTEXT_LOSS = "context_loss"
    ABSTRACTION_SKIP = "abstraction_skip"
    GRIND = "grind"
    ADVISORY_IGNORED = "advisory_ignored"


TAXONOMY: tuple[str, ...] = tuple(fc.value for fc in FailureClass)


@dataclass
class Classification:
    """One fired failure class with the evidence that fired it."""
    failure_class: str
    detail: str                       # human-readable one-liner
    evidence_seqs: list[int] = field(default_factory=list)   # ledger event seq refs
    evidence: list[dict] = field(default_factory=list)       # {seq, kind, why} refs

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "detail": self.detail,
            "evidence_seqs": self.evidence_seqs,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# Ledger helpers (schema: {seq, ts, actor, kind, payload})
# ---------------------------------------------------------------------------

def _events(ledger: dict[str, Any]) -> list[dict]:
    return list(ledger.get("events", []) or [])


def _by_kind(events: Iterable[dict], *kinds: str) -> list[dict]:
    ks = set(kinds)
    return [e for e in events if e.get("kind") in ks]


def _ev_ref(e: dict, why: str) -> dict:
    return {"seq": e.get("seq"), "kind": e.get("kind"), "actor": e.get("actor"), "why": why}


# Ref-id patterns the pipeline uses: DL1 / AC2 / DIM7 / doc ids / feature ids.
_REF_RE = re.compile(r"\b(DL\d+|AC\d+|DIM\d+|FN\d+|FT\d+|G\d+)\b")


def _known_ids(events: list[dict]) -> set[str]:
    """Every DL/AC/DIM/FN/FT/G id the ledger has actually MINTED (declared)."""
    known: set[str] = set()
    for e in events:
        p = e.get("payload") or {}
        kind = e.get("kind")
        # manifest artifact carries the deliverable + criterion ids
        if kind == "artifact" and p.get("kind") == "manifest":
            for dl in p.get("deliverables", []) or []:
                if dl.get("id"):
                    known.add(dl["id"])
                for ac in dl.get("acceptance_criteria", []) or []:
                    if ac.get("id"):
                        known.add(ac["id"])
        if kind == "dimension":
            if p.get("id"):
                known.add(p["id"])
        # ladder rung artifacts mint FN/FT/G ids
        for coll_key in ("functions", "features", "elements", "datums"):
            for item in p.get(coll_key, []) or []:
                if isinstance(item, dict) and item.get("id"):
                    known.add(item["id"])
        # explicit mint list (agent_fn may declare minted ids for a turn)
        for mid in p.get("mints", []) or []:
            known.add(mid)
    return known


# ---------------------------------------------------------------------------
# Individual class detectors
# ---------------------------------------------------------------------------

def _detect_wrong_question(events: list[dict], manifest: dict | None,
                           graded: dict) -> list[Classification]:
    out: list[Classification] = []
    questions = _by_kind(events, "question")

    # (a) a question that duplicates a defaulted assumption / has no bearing → useless.
    for q in questions:
        p = q.get("payload") or {}
        if p.get("useless") is True or p.get("bearing") == "none":
            out.append(Classification(
                FailureClass.WRONG_QUESTION.value,
                f"asked a question with no bearing: {p.get('text','')[:80]!r}",
                [q.get("seq")], [_ev_ref(q, "question marked no-bearing/useless")]))

    # (b) a critical blocking ambiguity in the manifest that was NEVER asked → missed.
    if manifest:
        asked_texts = " ".join((q.get("payload") or {}).get("text", "").lower()
                               for q in questions)
        for amb in manifest.get("ambiguities", []) or []:
            if not amb.get("blocking"):
                continue
            key = _amb_keyword(amb.get("text", ""))
            if key and key not in asked_texts:
                # find the manifest artifact event as evidence anchor
                anchor = next((e for e in events if e.get("kind") == "artifact"
                               and (e.get("payload") or {}).get("kind") == "manifest"), None)
                ev = [_ev_ref(anchor, "blocking ambiguity never clarified")] if anchor else []
                seqs = [anchor.get("seq")] if anchor else []
                out.append(Classification(
                    FailureClass.WRONG_QUESTION.value,
                    f"missed a critical blocking ambiguity: {amb.get('text','')[:80]!r}",
                    seqs, ev))
    return out


def _amb_keyword(text: str) -> str:
    """A distinctive keyword from an ambiguity to test whether it was asked about."""
    t = text.lower()
    for kw in ("material", "shaft", "load", "clearance", "process", "thread",
               "diameter", "bore", "fastener", "tolerance"):
        if kw in t:
            return kw
    return ""


def _detect_incoherent_geometry(events: list[dict], graded: dict) -> list[Classification]:
    out: list[Classification] = []
    failed = [ac for ac in graded.get("criteria", []) if ac.get("result") == "FAIL"]
    if not failed:
        return out
    # anchor the evidence to the check/battery events the grading read.
    check_events = _by_kind(events, "artifact", "decision")
    battery = next((e for e in check_events
                    if "battery" in str((e.get("payload") or {}).get("id", "")).lower()
                    or (e.get("payload") or {}).get("kind") == "battery"), None)
    ev = [_ev_ref(battery, "verify_battery / AC check failed")] if battery else []
    seqs = [battery.get("seq")] if battery else []
    ids = ", ".join(ac.get("id", "?") for ac in failed)
    out.append(Classification(
        FailureClass.INCOHERENT_GEOMETRY.value,
        f"acceptance criteria failed: {ids}", seqs, ev))
    return out


def _detect_tool_misuse(events: list[dict], manifest: dict | None) -> list[Classification]:
    out: list[Classification] = []
    # Map each AC id -> its declared check tool from the manifest.
    ac_tool: dict[str, str] = {}
    if manifest:
        for dl in manifest.get("deliverables", []) or []:
            for ac in dl.get("acceptance_criteria", []) or []:
                t = (ac.get("check") or {}).get("tool")
                if ac.get("id") and t:
                    ac_tool[ac["id"]] = t

    for e in _by_kind(events, "tool_call"):
        p = e.get("payload") or {}
        # (a) preflight rejected the call.
        if p.get("preflight") == "reject" or p.get("error"):
            out.append(Classification(
                FailureClass.TOOL_MISUSE.value,
                f"tool {p.get('tool','?')} rejected/errored: {str(p.get('error') or p.get('reason'))[:60]}",
                [e.get("seq")], [_ev_ref(e, "preflight reject / tool error")]))
            continue
        # (b) wrong tool for the criterion it claims to VERIFY. Only a verification
        # call can misuse a check tool — a BUILD/mutation tool that merely serves a
        # criterion (for_ac on an add_features/pattern/hole step) is not misuse, it is
        # construction. So gate on verification intent: an explicit ``verifies`` flag,
        # or the called tool being a known check tool (i.e. the role is checking, not
        # building). This prevents false-positives on build steps tagged with for_ac.
        target_ac = p.get("for_ac")
        if not (target_ac and target_ac in ac_tool):
            continue
        is_verification = bool(p.get("verifies")) or p.get("tool") in _CHECK_TOOLS
        if is_verification and p.get("tool") != ac_tool[target_ac]:
            out.append(Classification(
                FailureClass.TOOL_MISUSE.value,
                f"used {p.get('tool')} for {target_ac} but check tool is {ac_tool[target_ac]}",
                [e.get("seq")], [_ev_ref(e, "verification used a tool != criterion's "
                                         "declared check tool")]))
    return out


# Known verification/check tools (a call using one of these is a verification intent).
# Sourced from the §16 check-tool enum + the standard battery surface.
_CHECK_TOOLS = frozenset({
    "fit_report", "verify_battery", "measure_part", "measure_between", "cam_simulate",
    "run_fea_static", "run_fea_modal", "run_cfd_flow", "review_drawing",
    "check_interference", "check_feature_overlap", "design_review", "wall_thickness",
    "draft_analysis", "check_symmetry", "inspect_section", "query_geometry",
})


def _detect_context_loss(events: list[dict]) -> list[Classification]:
    out: list[Classification] = []
    known = _known_ids(events)
    for e in events:
        p = e.get("payload") or {}
        # Only inspect free-text/ref fields a role authored (not the manifest artifact).
        if e.get("kind") == "artifact" and p.get("kind") == "manifest":
            continue
        cited = set()
        for field_name in ("text", "rationale", "summary", "cites", "refs_text"):
            val = p.get(field_name)
            if isinstance(val, str):
                cited |= set(_REF_RE.findall(val))
            elif isinstance(val, list):
                cited |= {c for c in val if isinstance(c, str) and _REF_RE.fullmatch(c)}
        missing = sorted(c for c in cited if c not in known)
        if missing:
            out.append(Classification(
                FailureClass.CONTEXT_LOSS.value,
                f"cited nonexistent ref(s): {', '.join(missing)}",
                [e.get("seq")], [_ev_ref(e, f"cites undeclared id(s) {missing}")]))
    return out


def _detect_abstraction_skip(events: list[dict]) -> list[Classification]:
    out: list[Classification] = []
    for e in events:
        p = e.get("payload") or {}
        # ladder validator bounces are recorded as decision/artifact with a bounce flag.
        is_ladder = ("ladder" in str(p.get("stage", "")).lower()
                     or "ladder" in str(p.get("id", "")).lower()
                     or p.get("kind") == "ladder_bounce")
        if is_ladder and (p.get("bounce") or p.get("validator") == "fail"):
            out.append(Classification(
                FailureClass.ABSTRACTION_SKIP.value,
                f"ladder validator bounced: {str(p.get('finding') or p.get('text'))[:80]}",
                [e.get("seq")], [_ev_ref(e, "design-cognition ladder validator bounce")]))
    return out


def _detect_grind(events: list[dict], grind_threshold: int = 3) -> list[Classification]:
    out: list[Classification] = []
    # Group tool_call / decision events by (node/deliverable, action fingerprint).
    seen: dict[tuple, list[dict]] = {}
    for e in _by_kind(events, "tool_call", "decision"):
        p = e.get("payload") or {}
        node = p.get("node") or p.get("for_dl") or p.get("stage") or "?"
        fp = p.get("hypothesis") or p.get("fingerprint")
        if fp is None:
            # fingerprint the action: same tool + same args string == no changed hypothesis
            fp = f"{p.get('tool','')}|{_stable(p.get('args'))}|{p.get('text','')[:40]}"
        seen.setdefault((node, fp), []).append(e)
    for (node, fp), group in seen.items():
        if len(group) >= grind_threshold:
            seqs = [g.get("seq") for g in group]
            out.append(Classification(
                FailureClass.GRIND.value,
                f"{len(group)} attempts on {node!r} with no changed hypothesis",
                seqs, [_ev_ref(group[0], f"{len(group)} identical attempts (anti-grind)")]))
    return out


def _detect_advisory_ignored(events: list[dict]) -> list[Classification]:
    out: list[Classification] = []
    advisories = _by_kind(events, "advisory")
    disputes = _by_kind(events, "replan", "dispute")
    disputed_topics = {(_advisory_topic((d.get("payload") or {}))) for d in disputes}
    for a in advisories:
        pa = a.get("payload") or {}
        topic = _advisory_topic(pa)
        # a later action contradicts the advisory (payload carries what it warned against)
        contra = None
        for e in events:
            if e.get("seq", -1) <= a.get("seq", -1):
                continue
            pe = e.get("payload") or {}
            if pe.get("contradicts_advisory") == pa.get("id") or (
                    topic and pe.get("topic") == topic and pe.get("action") == "proceed"):
                contra = e
                break
        if contra is not None and topic not in disputed_topics:
            out.append(Classification(
                FailureClass.ADVISORY_IGNORED.value,
                f"advisory {pa.get('id','?')} ({topic}) contradicted without dispute",
                [a.get("seq"), contra.get("seq")],
                [_ev_ref(a, "advisory shown"), _ev_ref(contra, "later action contradicts it")]))
    return out


def _advisory_topic(p: dict) -> str:
    return str(p.get("topic") or p.get("id") or p.get("text", "")[:24])


def _stable(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        return str(obj)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def classify_ledger(ledger: dict[str, Any], *, manifest: dict | None = None,
                    graded: dict | None = None,
                    grind_threshold: int = 3) -> list[Classification]:
    """Replay a ledger and return every failure class that fires, with evidence.

    Args:
        ledger:  ``{"events": [...], "state": {...}}`` as written by services/agent/ledger.
        manifest: the deliverable manifest (for wrong_question / tool_misuse checks). If
                  omitted, taken from the manifest artifact event in the ledger.
        graded:  the gym's own AC grading ``{"criteria": [{"id","result"}...], "passed": bool}``.
                 Drives incoherent_geometry (independent of the pipeline's PASS floor).
        grind_threshold: attempts-on-one-node before grind fires (default 3, matching the
                 anti-grind "two failed attempts then replan" rule → a 3rd is grind).
    """
    events = _events(ledger)
    graded = graded or {"criteria": [], "passed": True}
    if manifest is None:
        anchor = next((e for e in events if e.get("kind") == "artifact"
                       and (e.get("payload") or {}).get("kind") == "manifest"), None)
        manifest = (anchor.get("payload") if anchor else None)

    results: list[Classification] = []
    results += _detect_wrong_question(events, manifest, graded)
    results += _detect_incoherent_geometry(events, graded)
    results += _detect_tool_misuse(events, manifest)
    results += _detect_context_loss(events)
    results += _detect_abstraction_skip(events)
    results += _detect_grind(events, grind_threshold)
    results += _detect_advisory_ignored(events)
    return results
