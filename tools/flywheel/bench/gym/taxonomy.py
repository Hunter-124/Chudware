"""Failure taxonomy classifier.

Classifies what went wrong on a design-gym challenge by REPLAYING the ledger events
(schema ``{seq, ts, actor, kind, payload}`` — see services/agent/ledger.py) plus the
gym's own acceptance-criteria grading and the validator/battery outputs the run
recorded. Every classification carries EVIDENCE REFS (concrete event seq numbers + the
kinds/fields that triggered it) so a reviewer can replay exactly why a class fired — no
vibes.

The eight classes:

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
  execution_stall     the run produced tool activity but NOT ONE tool call succeeded —
                      the mission stalled before any geometry existed (pre-BUILD).
                      Evidence: the errored tool-call event seqs. When this fires,
                      incoherent_geometry is suppressed: with zero successful
                      executions there is no geometry to be incoherent (classifying
                      these runs as incoherent_geometry would yield EMPTY evidence
                      seqs).

Tool-call reading: real agent turns record tool calls as ``agent_turn``
events with ``payload.event == "tool_call"`` (services/agent/agent_turn._log), while
scripted gym turns use ``kind == "tool_call"`` directly. The classifier normalises
BOTH shapes (``_tool_call_events``) so real-run evidence is visible to the detectors.

A single run can exhibit several classes; ``classify_ledger`` returns all that fire.
The classifier is deterministic. Terminal infrastructure outcomes also invalidate a
provisional graded pass so a real cell cannot report success after harness failure.
"""
from __future__ import annotations

import json
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
    # Harness outcomes share the classification envelope so reports retain their
    # evidence, but are intentionally not part of the eight model-failure classes.
    POLICY_REFUSAL = "policy_refusal"
    GUARD_BLOCK = "guard_block"
    HARNESS_FAULT = "harness_fault"


_OUTCOME_CLASSES = frozenset({
    FailureClass.POLICY_REFUSAL, FailureClass.GUARD_BLOCK,
    FailureClass.HARNESS_FAULT,
})
TAXONOMY: tuple[str, ...] = tuple(
    fc.value for fc in FailureClass if fc not in _OUTCOME_CLASSES
)


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
    """Every tool call in the ledger as ``(event, call_payload)`` pairs.

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

# Keep this vocabulary identical to services.agent.agent_turn.TOOL_OUTCOME_KINDS
# without importing that dispatch-heavy module into the offline gym classifier.
TOOL_OUTCOME_KINDS = frozenset({
    "ok", "policy_refusal", "guard_block", "validation_error",
    "timeout_unknown", "internal_error", "tool_error",
})
_POLICY_REFUSAL_STATUSES = frozenset({
    "call_cap_reached", "loop_guard_refused", "tool_disabled",
    "tool_not_allowed",
})
_GUARD_BLOCK_STATUSES = frozenset({"guardrail_blocked", "guard_block"})
_TERMINAL_INFRASTRUCTURE_STATUSES = frozenset({
    "reply_truncated", "guardrail_internal_error",
})


def _result_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def normalize_tool_outcome(p: dict) -> tuple[str, bool]:
    """Return the agent-turn outcome vocabulary's ``(kind, executed)`` pair.

    New ledgers carry both fields directly. Older and scripted ledgers are
    normalised from their result payload/status so every taxonomy detector uses
    the same decision and no detector has to parse error prose independently.
    """
    result = _result_dict(p.get("result"))
    explicit_kind = p.get("outcome_kind")
    status = str(p.get("status") or result.get("status") or "")
    explicit_executed = p.get("executed")
    if not isinstance(explicit_executed, bool):
        explicit_executed = result.get("executed")
    if not isinstance(explicit_executed, bool):
        explicit_executed = None

    if explicit_kind in TOOL_OUTCOME_KINDS:
        if explicit_executed is None:
            explicit_executed = explicit_kind not in {
                "policy_refusal", "guard_block", "validation_error",
            }
        return str(explicit_kind), explicit_executed
    if (p.get("policy_refusal") or result.get("policy_refusal")
            or status in _POLICY_REFUSAL_STATUSES):
        return "policy_refusal", False
    if status in _GUARD_BLOCK_STATUSES:
        return "guard_block", False
    if status in _TERMINAL_INFRASTRUCTURE_STATUSES:
        return "internal_error", False
    if p.get("timed_out") or result.get("timed_out"):
        return "timeout_unknown", True if explicit_executed is None else explicit_executed
    if p.get("internal_error") or result.get("internal_error"):
        return "internal_error", True if explicit_executed is None else explicit_executed
    if p.get("preflight") == "reject":
        return "validation_error", False
    if p.get("ok") is False or p.get("error"):
        return "tool_error", True if explicit_executed is None else explicit_executed
    return "ok", True if explicit_executed is None else explicit_executed


def _is_refusal_outcome(p: dict) -> bool:
    return normalize_tool_outcome(p)[0] in {"policy_refusal", "guard_block"}


def _terminal_infrastructure_status(p: dict) -> str | None:
    values = (p.get("status"), p.get("stop_reason"), p.get("reason"))
    for value in values:
        if value in _TERMINAL_INFRASTRUCTURE_STATUSES:
            return str(value)
    event = str(p.get("event") or "")
    if event == "reply_truncated_fatal":
        return "reply_truncated"
    if event.endswith("guardrail_internal_error"):
        return "guardrail_internal_error"
    return None


def _call_succeeded(p: dict) -> bool:
    """True only for the shared outcome taxonomy's explicit success kind."""
    return normalize_tool_outcome(p)[0] == "ok"


# Ref-id patterns the pipeline uses: DL1 / AC2 / DIM7 / doc ids / feature ids.
_REF_RE = re.compile(r"\b(DL\d+|AC\d+|DIM\d+|FN\d+|FT\d+|G\d+)\b")


def _mint_ids(kind: str | None, p: dict) -> set[str]:
    """Ids a SINGLE event's payload mints/declares (manifest ids excluded — those
    are known from run start, not minted mid-ledger; see _known_ids)."""
    minted: set[str] = set()
    if kind == "artifact" and p.get("kind") == "manifest":
        for dl in p.get("deliverables", []) or []:
            if dl.get("id"):
                minted.add(dl["id"])
            for ac in dl.get("acceptance_criteria", []) or []:
                if ac.get("id"):
                    minted.add(ac["id"])
    if kind == "dimension" and p.get("id"):
        minted.add(p["id"])
    # ladder rung artifacts mint FN/FT/G ids
    for coll_key in ("functions", "features", "elements", "datums"):
        for item in p.get(coll_key, []) or []:
            if isinstance(item, dict) and item.get("id"):
                minted.add(item["id"])
    # explicit mint list (agent_fn may declare minted ids for a turn)
    for mid in p.get("mints", []) or []:
        minted.add(mid)
    return minted


def _known_ids(events: list[dict], manifest: dict | None = None) -> set[str]:
    """Every DL/AC/DIM/FN/FT/G id the ledger has actually MINTED (declared),
    ACROSS THE WHOLE LEDGER (unordered — see _detect_context_loss for the ordered,
    per-event view an id-cited-before-it-was-minted check needs).

    REAL cells don't replay the manifest as an artifact event (the mission
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
        known |= _mint_ids(e.get("kind"), e.get("payload") or {})
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
    """Fire when executable tool attempts were made but none succeeded."""
    if graded.get("passed"):
        return []
    if any(_terminal_infrastructure_status(e.get("payload") or {})
           for e in events):
        return []
    calls = _tool_call_events(events)
    turns = [e for e in events if e.get("kind") == "agent_turn"]
    if calls:
        attempted = [
            (e, p) for e, p in calls
            if normalize_tool_outcome(p)[0] not in {
                "policy_refusal", "guard_block", "internal_error",
                "timeout_unknown",
            }
        ]
        if not attempted or any(_call_succeeded(p) for _, p in attempted):
            return []
        seqs = [e.get("seq") for e, _ in attempted]
        ev = [_ev_ref(e, f"tool {p.get('tool','?')} failed: "
                          f"{str(p.get('error') or p.get('result'))[:60]}")
              for e, p in attempted[:8]]
        return [Classification(
            FailureClass.EXECUTION_STALL.value,
            f"all {len(attempted)} executable tool calls failed — nothing ran "
            "successfully (pre-BUILD stall)", seqs, ev)]
    if turns:
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
        outcome_kind, _executed = normalize_tool_outcome(p)
        if outcome_kind not in {"validation_error", "tool_error", "ok"}:
            continue
        # (a) preflight rejected the call / the tool ran and errored.  A pure
        # RESOLUTION failure ("could not be resolved") is a dispatch-coverage gap
        # (harness), not model misuse — it feeds execution_stall instead.
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
        #
        # Scoping decision (P1 audit): this rule depends on for_ac/verifies — fields
        # ONLY the scripted gym emits (runner.py replay_turns_into_ledger); real
        # tool_call payloads (agent_turn._log) never tag a call with the criterion
        # it claims to verify, so there is no real signal to adapt this rule to.
        # Kept scripted-only rather than deleted per YAGNI — ~15 gym challenge
        # fixtures under tools/flywheel/bench/gym/challenges/*.py drive misuse
        # detection through for_ac (e.g. c1/c4/c5/c6/c7/c9's misuse variants). It
        # correctly never fires on real-shaped payloads (no false positive)
        # instead of guessing which criterion an untagged real call verified.
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
    # Manifest ids are known from the start of the run (real cells don't
    # replay a manifest artifact event). Ledger-MINTED ids are ORDERED: an id only
    # counts as known for events AFTER the event that minted it — citing an id
    # before it exists in the ledger IS the failure this detector exists to catch,
    # and a whole-ledger union (the old behaviour) missed exactly that case.
    known: set[str] = set()
    for dl in (manifest or {}).get("deliverables", []) or []:
        if dl.get("id"):
            known.add(dl["id"])
        for ac in dl.get("acceptance_criteria", []) or []:
            if ac.get("id"):
                known.add(ac["id"])
    for e in sorted(events, key=lambda ev: ev.get("seq", 0)):
        p = e.get("payload") or {}
        kind = e.get("kind")
        # Only inspect free-text/ref fields a role authored (not the manifest artifact).
        if not (kind == "artifact" and p.get("kind") == "manifest"):
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
        known |= _mint_ids(kind, p)
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


def _grind_target(p: dict) -> str:
    """The repeated-action TARGET grind groups by.

    Scripted-gym turns carry an explicit build-graph ``node`` (e.g.
    ``n_verify_DL1``) — genuinely per-deliverable. Real payloads carry no such
    id: real tool_call events (agent_turn._log) have none at all, and real
    decision events (ledger.log_decision) carry a ``stage`` that is a workflow
    phase SHARED by every deliverable in that phase — keying on it collapsed
    unrelated deliverables at the same stage into one grind bucket. Fall back to
    the call's own tool+args signature (or its decision id) so two different real
    deliverables making different calls never collapse into a single '?' bucket."""
    node = p.get("node") or p.get("for_dl")
    if node:
        return str(node)
    tool = p.get("tool")
    if tool:
        return f"{tool}|{_stable(p.get('args'))}"
    return str(p.get("id") or p.get("stage") or "?")


def _detect_grind(events: list[dict], grind_threshold: int = 3) -> list[Classification]:
    out: list[Classification] = []
    # Group tool_call / decision events by (target, action fingerprint).
    # Tool calls are read through the normaliser so real agent-turn calls
    # (kind=agent_turn, payload.event=tool_call) count toward grind too.
    seen: dict[tuple, list[dict]] = {}
    pairs = [
        (e, p) for e, p in _tool_call_events(events)
        if normalize_tool_outcome(p)[0] not in {
            "policy_refusal", "guard_block", "internal_error", "timeout_unknown",
        }
    ] + [
        (e, e.get("payload") or {}) for e in _by_kind(events, "decision")]
    pairs.sort(key=lambda ep: ep[0].get("seq", 0))
    for e, p in pairs:
        target = _grind_target(p)
        fp = p.get("hypothesis") or p.get("fingerprint")
        if fp is None:
            # fingerprint the action: same tool + same args string == no changed hypothesis
            fp = f"{p.get('tool','')}|{_stable(p.get('args'))}|{p.get('text','')[:40]}"
        seen.setdefault((target, fp), []).append(e)
    for (target, fp), group in seen.items():
        if len(group) >= grind_threshold:
            seqs = [g.get("seq") for g in group]
            out.append(Classification(
                FailureClass.GRIND.value,
                f"{len(group)} attempts on {target!r} with no changed hypothesis",
                seqs, [_ev_ref(group[0], f"{len(group)} identical attempts (anti-grind)")]))
    return out


def _advisory_ids(p: dict) -> set[str]:
    """Every identifier a payload's advisory represents.

    Scripted-gym advisories carry a top-level ``topic``/``id`` (runner.py
    replay_turns_into_ledger). Real advisories are advisor.engine.build_advisory's
    ``{schema, seq, stage, items, user_override, failure_cards}`` shape — there is
    NO top-level topic/id, only per-item ids (``items[].id``, minted by
    engine._finding_to_item). Collect both so either shape is matched by id.
    Returns an EMPTY set when the payload carries nothing identifiable — the
    caller must never treat two empty sets as a match. That was the source of the
    old false-positive: ``pa.get("id") == pe.get("contradicts_advisory")`` compared
    None == None, and virtually every real advisory (no top-level id) matched."""
    ids: set[str] = set()
    for key in ("topic", "id"):
        v = p.get(key)
        if v:
            ids.add(str(v))
    for item in p.get("items", []) or []:
        if isinstance(item, dict) and item.get("id"):
            ids.add(str(item["id"]))
    return ids


def _detect_advisory_ignored(events: list[dict]) -> list[Classification]:
    out: list[Classification] = []
    advisories = _by_kind(events, "advisory")
    # "replan"/"dispute" are the scripted-gym dispute kinds; "advisory_dispute" is
    # the real one (services/mcp/advisor_tools.advisory_dispute -> ledger kind
    # "advisory_dispute", payload carries "advisory_id").
    disputes = _by_kind(events, "replan", "dispute", "advisory_dispute")
    disputed_ids: set[str] = set()
    for d in disputes:
        pd = d.get("payload") or {}
        for key in ("topic", "id", "advisory_id"):
            v = pd.get(key)
            if v:
                disputed_ids.add(str(v))
    for a in advisories:
        pa = a.get("payload") or {}
        ids = _advisory_ids(pa)
        if not ids:
            # nothing identifiable on this advisory — never guess a match.
            continue
        # a later action contradicts the advisory (payload carries what it warned against)
        contra = None
        for e in events:
            if e.get("seq", -1) <= a.get("seq", -1):
                continue
            pe = e.get("payload") or {}
            contradicts = pe.get("contradicts_advisory")
            topic = pe.get("topic")
            if (contradicts and str(contradicts) in ids) or (
                    topic and str(topic) in ids and pe.get("action") == "proceed"):
                contra = e
                break
        if contra is not None and not (ids & disputed_ids):
            label = sorted(ids)[0]
            out.append(Classification(
                FailureClass.ADVISORY_IGNORED.value,
                f"advisory {label!r} contradicted without dispute",
                [a.get("seq"), contra.get("seq")],
                [_ev_ref(a, "advisory shown"), _ev_ref(contra, "later action contradicts it")]))
    return out


def _stable(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        return str(obj)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def _detect_harness_outcomes(events: list[dict], graded: dict) -> list[Classification]:
    """Preserve policy and infrastructure outcomes without calling them failures."""
    out: list[Classification] = []
    for e, p in _tool_call_events(events):
        kind, executed = normalize_tool_outcome(p)
        if kind == "policy_refusal":
            failure_class = FailureClass.POLICY_REFUSAL
        elif kind == "guard_block":
            failure_class = FailureClass.GUARD_BLOCK
        elif kind in {"internal_error", "timeout_unknown"}:
            failure_class = FailureClass.HARNESS_FAULT
        else:
            continue
        status = str(p.get("status") or _result_dict(p.get("result")).get("status")
                     or kind)
        out.append(Classification(
            failure_class.value,
            f"{status}: outcome={kind}, executed={str(executed).lower()}",
            [e.get("seq")], [_ev_ref(e, f"tool outcome {kind}; executed={executed}")],
        ))

    terminal_statuses: list[str] = []
    for e in events:
        status = _terminal_infrastructure_status(e.get("payload") or {})
        if status is None:
            continue
        terminal_statuses.append(status)
        out.append(Classification(
            FailureClass.HARNESS_FAULT.value,
            f"terminal infrastructure outcome: {status}",
            [e.get("seq")], [_ev_ref(e, f"terminal infrastructure outcome {status}")],
        ))
    if terminal_statuses:
        # run_cell reads this same grading object after classification. A provider
        # truncation or harness-internal guardrail fault is never a passing cell,
        # even when independent AC grading happened to observe acceptable geometry.
        graded["passed"] = False
        graded["infrastructure_outcomes"] = list(dict.fromkeys(terminal_statuses))
    return out


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
    results += _detect_harness_outcomes(events, graded)
    results += _detect_wrong_question(events, manifest, graded)
    # A pre-BUILD stall (no successful execution at all) is execution_stall, and
    # SUPPRESSES incoherent_geometry — with nothing ever executed there is no
    # geometry to be incoherent, and incoherent_geometry would carry empty
    # evidence seqs.
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
        if _is_refusal_outcome(p):
            continue
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
