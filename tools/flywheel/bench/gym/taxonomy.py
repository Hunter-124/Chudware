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
  execution_stall     (T-W32, foreman-sanctioned 2026-07-05) the run produced tool
                      activity but NOT ONE tool call succeeded — the mission stalled
                      before any geometry existed (pre-BUILD). Evidence: the errored
                      tool-call event seqs. When this fires, incoherent_geometry is
                      suppressed: with zero successful executions there is no geometry
                      to be incoherent (the old behaviour classified these runs as
                      incoherent_geometry with EMPTY evidence seqs — see
                      cycle-1-real-r3).

Tool-call reading (T-W32): real agent turns record tool calls as ``agent_turn``
events with ``payload.event == "tool_call"`` (services/agent/agent_turn._log), while
scripted gym turns use ``kind == "tool_call"`` directly. The classifier normalises
BOTH shapes (``_tool_call_events``) so real-run evidence is visible to the detectors.

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
    EXECUTION_STALL = "execution_stall"


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


def _tool_call_events(events: Iterable[dict]) -> list[tuple[dict, dict]]:
    """Every tool call in the ledger as ``(event, call_payload)`` pairs (T-W32).

    Normalises the two shapes tool calls land in:
      * scripted gym turns:   ``kind == "tool_call"``, payload IS the call;
      * real agent turns:     ``kind == "agent_turn"`` with
        ``payload.event == "tool_call"`` (agent_turn._log) — the call fields
        (tool/args/ok/error/result) sit alongside the ``event`` tag.
    """
    out: list[tuple[dict, dict]] = []
    for e in events:
        p = e.get("payload") or {}
        if e.get("kind") == "tool_call":
            out.append((e, p))
        elif e.get("kind") == "agent_turn" and p.get("event") == "tool_call":
            out.append((e, p))
    return out


def _call_succeeded(p: dict) -> bool:
    """A call payload counts as successful unless it carries an explicit failure
    signal (``ok: false`` or an ``error``). Scripted turns without an ok field are
    successful by construction."""
    if p.get("ok") is False:
        return False
    if p.get("error"):
        return False
    return True


# Ref-id patterns the pipeline uses: DL1 / AC2 / DIM7 / doc ids / feature ids.
_REF_RE = re.compile(r"\b(DL\d+|AC\d+|DIM\d+|FN\d+|FT\d+|G\d+)\b")


def _known_ids(events: list[dict], manifest: dict | None = None) -> set[str]:
    """Every DL/AC/DIM/FN/FT/G id the ledger has actually MINTED (declared).

    T-W32: REAL cells don't replay the manifest as an artifact event (the mission
    pipeline records the manifest artifact without its deliverables payload), so
    ids the MANIFEST declares (DL/AC) are also known when the caller passes it —
    otherwise every real run citing its own deliverable id was a context_loss
    false positive."""
    known: set[str] = set()
    for dl in (manifest or {}).get("deliverables", []) or []:
        if dl.get("id"):
            known.add(dl["id"])
        for ac in dl.get("acceptance_criteria", []) or []:
            if ac.get("id"):
                known.add(ac["id"])
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


def _detect_execution_stall(events: list[dict], graded: dict) -> list[Classification]:
    """Fire when the run had tool activity but not ONE call succeeded (T-W32).

    A pre-BUILD stall: the model tried to execute (or turned without executing)
    and nothing ever ran — resolution failures, rejected kwargs, provider errors.
    Only fires on a non-passing run (a graded PASS with odd events is not a stall).
    """
    if graded.get("passed"):
        return []
    calls = _tool_call_events(events)
    turns = [e for e in events if e.get("kind") == "agent_turn"]
    if calls:
        if any(_call_succeeded(p) for _, p in calls):
            return []
        failed_calls = [(e, p) for e, p in calls if not _call_succeeded(p)]
        seqs = [e.get("seq") for e, _ in failed_calls]
        ev = [_ev_ref(e, f"tool {p.get('tool','?')} failed: "
                          f"{str(p.get('error') or p.get('result'))[:60]}")
              for e, p in failed_calls[:8]]
        return [Classification(
            FailureClass.EXECUTION_STALL.value,
            f"all {len(failed_calls)} tool calls failed — nothing ever executed "
            "(pre-BUILD stall)", seqs, ev)]
    if turns:
        # agent turns happened but the model never emitted a single tool call.
        seqs = [e.get("seq") for e in turns[:8]]
        ev = [_ev_ref(turns[0], "agent turns produced no tool call at all")]
        return [Classification(
            FailureClass.EXECUTION_STALL.value,
            f"{len(turns)} agent-turn events with zero tool calls — nothing ever "
            "executed (pre-BUILD stall)", seqs, ev)]
    return []


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

    for e, p in _tool_call_events(events):
        # (a) preflight rejected the call / the tool ran and errored.  A pure
        # RESOLUTION failure ("could not be resolved") is a dispatch-coverage gap
        # (harness), not model misuse — it feeds execution_stall instead (T-W32).
        err = str(p.get("error") or "")
        if p.get("preflight") == "reject" or (err and "could not be resolved" not in err):
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


def _detect_context_loss(events: list[dict],
                         manifest: dict | None = None) -> list[Classification]:
    out: list[Classification] = []
    known = _known_ids(events, manifest)
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
    # T-W32: tool calls are read through the normaliser so real agent-turn calls
    # (kind=agent_turn, payload.event=tool_call) count toward grind too.
    seen: dict[tuple, list[dict]] = {}
    pairs = _tool_call_events(events) + [
        (e, e.get("payload") or {}) for e in _by_kind(events, "decision")]
    pairs.sort(key=lambda ep: ep[0].get("seq", 0))
    for e, p in pairs:
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
    # T-W32 (foreman-sanctioned): a pre-BUILD stall (no successful execution at
    # all) is execution_stall, and SUPPRESSES incoherent_geometry — with nothing
    # ever executed there is no geometry to be incoherent, and the old routing
    # produced incoherent_geometry with empty evidence seqs.
    stall = _detect_execution_stall(events, graded)
    results += stall
    if not stall:
        results += _detect_incoherent_geometry(events, graded)
    results += _detect_tool_misuse(events, manifest)
    results += _detect_context_loss(events, manifest)
    results += _detect_abstraction_skip(events)
    results += _detect_grind(events, grind_threshold)
    results += _detect_advisory_ignored(events)
    return results


# ---------------------------------------------------------------------------
# Tool-misuse fingerprints (WI-11 instrumentation)
# ---------------------------------------------------------------------------

# Regex buckets for error-class tagging of failed tool_calls.
_MISUSE_BUCKETS: list[tuple[str, "re.Pattern[str]"]] = [
    ("bad-arg", re.compile(r"bad.?arg|invalid.?arg|unexpected.?kw|type.?error|missing.?required|got an unexpected", re.I)),
    ("wrong-face-ref", re.compile(r"face|edge|ref(erence)?|unknown.?feature|no such|does not exist|dangling", re.I)),
    ("order-violation", re.compile(r"order|before|after|sequence|prerequisite|not yet|out of order", re.I)),
    ("verify-skipped", re.compile(r"verify.?skip|battery.?required|must run verify|verify_battery", re.I)),
    ("orphan-sketch", re.compile(r"orphan.?sketch|dangling.?sketch|unconsumed.?sketch", re.I)),
    ("no-calc-before-dim", re.compile(r"calc.?before|fit_calc|scratchpad|no.?prior.?calc", re.I)),
]


def misuse_error_class(error: str | None) -> str:
    """Map a tool error string to a misuse fingerprint bucket (or 'other')."""
    text = str(error or "")
    if not text:
        return "other"
    for name, pat in _MISUSE_BUCKETS:
        if pat.search(text):
            return name
    return "other"


def misuse_fingerprints(ledger: dict | None = None, *,
                        events: list[dict] | None = None) -> list[dict]:
    """Cluster (tool, error-class) from failed tool_calls for instrumentation.

    Returns a list of ``{tool, error_class, count, fingerprint}`` sorted by count desc.
    """
    from collections import Counter
    if events is None:
        events = _events(ledger or {})
    counts: Counter = Counter()
    for _e, p in _tool_call_events(events):
        if _call_succeeded(p):
            continue
        tool = str(p.get("tool") or "?")
        err = str(p.get("error") or p.get("result") or "")
        # Also accept explicit error_class tags from hard_guardrail / inject.
        cls = p.get("error_class") or misuse_error_class(err)
        counts[(tool, str(cls))] += 1
    out = []
    for (tool, cls), n in counts.most_common():
        out.append({
            "tool": tool,
            "error_class": cls,
            "count": n,
            "fingerprint": f"{tool}:{cls}",
        })
    return out
