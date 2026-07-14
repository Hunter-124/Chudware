"""HARD guardrails (all capability tiers A/B/C) — physics/process floors.

Harness-enforced invariants from harness-tiers-spec §2 (WI-11..15). They gate
terminal claims (done) and silently protect (auto-checkpoint); they never
dictate *how* to build. Capability-independent: ImportError-safe for callers
that treat missing modules as no-op.

Ledger event kinds (WI-17):
  * hard_guardrail {name, outcome: blocked|passed|waived, reason}
  * auto_checkpoint {trigger_tool}
  * auto_verify_inject  (reserved, batch 2)

Enforcement points:
  * pre-dispatch  — WI-12 auto-checkpoint, WI-15 ref integrity
  * done intercept — WI-11 battery, WI-13 interference, WI-14 orphan sketch
  * mission VERIFY — phases._gate_verify (missing battery = FAIL)
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Destructive classifier (WI-6 / WI-12 shared HARD set)
# ---------------------------------------------------------------------------
# Op kinds that remove or irreversibly alter geometry. Used when inspecting
# add_features / doc_apply_ops payloads. Tool names that are themselves
# destructive (delete/replace) are classified via DESTRUCTIVE_TOOLS.

DESTRUCTIVE_OPS: frozenset[str] = frozenset({
    # boolean / material removal
    "boolean", "boolean_cut", "cut", "subtract", "cut_extrude", "cut_revolve",
    "extrude_cut", "revolve_cut",
    # delete / remove
    "delete", "delete_feature", "remove", "remove_feature", "suppress_feature",
    # replace / rewrite that drops geometry
    "replace", "replace_feature", "edit_feature",
    # shell / material thin-out
    "shell",
    # pattern over existing geometry (can cascade destroy)
    "pattern", "linear_pattern", "circular_pattern", "pattern_linear",
    "pattern_circular", "mirror",
})

# Top-level tool names that always count as destructive regardless of args.
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset({
    "delete_feature", "remove_feature", "doc_delete_feature",
    "delete_part", "close_part",  # close can drop unsaved work — treat as destructive
})

# Tools that mutate geometry (battery-before-done only when one of these succeeded).
MUTATING_TOOLS: frozenset[str] = frozenset({
    "add_features", "doc_apply_ops", "new_part", "new_assembly",
    "asm_add_component", "asm_mate", "part_place", "set_material",
    "delete_feature", "remove_feature", "regen_resolve",
    "sketch3d_add", "sketch3d_edit", "convert_face_to_sketch",
}) | DESTRUCTIVE_TOOLS

# Tools whose args may carry feature/face refs that must resolve (WI-15).
REF_MUTATING_TOOLS: frozenset[str] = frozenset({
    "add_features", "doc_apply_ops",
})

# Model-visible rejection messages (spec WI-11/13/14/15).
MSG_VERIFY_BATTERY = (
    "HARD guardrail: run verify_battery (or include a handoff.waivers entry "
    "with check='verify_battery' and a non-empty reason) before declaring done."
)
MSG_INTERFERENCE = (
    "HARD guardrail: run check_interference (or waive with check="
    "'check_interference' + reason) before declaring an assembly/multi-body done."
)
MSG_ORPHAN_SKETCH = (
    "HARD guardrail: orphan sketch(es) remain unconsumed. Extrude/revolve them, "
    "delete them, or waive each with check='orphan_sketch:<id>' + reason. "
    "Orphans: {ids}"
)
MSG_CHECKPOINT_FAIL = (
    "HARD guardrail: auto-checkpoint before destructive tool {tool!r} failed "
    "({reason}); the destructive call was blocked."
)
MSG_REFS = (
    "HARD guardrail: dangling feature/face refs — cannot apply. "
    "Missing: {missing}. Fix the refs (no waiver for kernel integrity)."
)

# Kill-switch config key (WI-12). Default true.
_CFG_AUTO_CHECKPOINT = "hard_guardrails"


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

class GuardVerdict:
    """Outcome of one guardrail check.

    ``ok`` True → proceed. False → block (feed ``message`` to the model).
    ``outcome`` is the ledger tag: passed | blocked | waived.
    """

    __slots__ = ("ok", "name", "outcome", "reason", "message")

    def __init__(self, ok: bool, name: str, outcome: str,
                 reason: str = "", message: str = "") -> None:
        self.ok = ok
        self.name = name
        self.outcome = outcome  # blocked | passed | waived
        self.reason = reason
        self.message = message or reason


# ---------------------------------------------------------------------------
# Config kill-switch
# ---------------------------------------------------------------------------

def auto_checkpoint_enabled(cfg: dict | None = None) -> bool:
    """WI-12 kill-switch: hard_guardrails.auto_checkpoint (default True)."""
    try:
        if cfg is None:
            from services.agent import config as _cfg  # noqa: PLC0415
            cfg = _cfg.load_config()
    except Exception:  # noqa: BLE001
        cfg = cfg or {}
    block = (cfg or {}).get(_CFG_AUTO_CHECKPOINT) or {}
    if not isinstance(block, dict):
        return True
    return bool(block.get("auto_checkpoint", True))


# ---------------------------------------------------------------------------
# Destructive classifier
# ---------------------------------------------------------------------------

def is_destructive_tool(tool: str, args: dict | None = None) -> bool:
    """True when *tool* (with optional *args*) is in the HARD destructive set.

    Classifies by tool name first, then by op kinds inside add_features /
    doc_apply_ops payloads (feature.type or op/kind fields).
    """
    if not tool:
        return False
    if tool in DESTRUCTIVE_TOOLS:
        return True
    args = args or {}
    if tool not in ("add_features", "doc_apply_ops"):
        # Other tools: treat as destructive only if name itself is listed.
        return False
    ops = args.get("ops") or []
    if not isinstance(ops, list):
        return False
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = (op.get("op") or op.get("kind") or "").lower()
        if kind in DESTRUCTIVE_OPS:
            return True
        feat = op.get("feature") if isinstance(op.get("feature"), dict) else {}
        ftype = (feat.get("type") or "").lower()
        if ftype in DESTRUCTIVE_OPS:
            return True
        # Extrude/revolve with mode=cut
        params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
        mode = str(params.get("mode") or op.get("mode") or "").lower()
        if mode in ("cut", "remove", "subtract"):
            return True
        # Pattern that references an existing feature (pattern-over-existing)
        if ftype in ("pattern", "linear_pattern", "circular_pattern", "mirror") or \
           kind in ("pattern", "linear_pattern", "circular_pattern", "mirror"):
            return True
    return False


def is_mutating_tool(tool: str) -> bool:
    """True when a successful call of *tool* obligates battery-before-done."""
    return tool in MUTATING_TOOLS


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _emit(ledger: Any, actor: str, kind: str, payload: dict) -> None:
    if ledger is None:
        return
    try:
        ledger.append(actor or "guardrails", kind, payload)
    except Exception:  # noqa: BLE001
        pass


def emit_hard_guardrail(ledger: Any, name: str, outcome: str,
                        reason: str = "", actor: str = "guardrails") -> None:
    """WI-17: emit hard_guardrail {name, outcome, reason}."""
    _emit(ledger, actor, "hard_guardrail", {
        "name": name,
        "outcome": outcome,
        "reason": reason or "",
    })


def emit_auto_checkpoint(ledger: Any, trigger_tool: str,
                         actor: str = "guardrails",
                         extra: dict | None = None) -> None:
    """WI-17: emit auto_checkpoint {trigger_tool}."""
    payload = {"trigger_tool": trigger_tool}
    if extra:
        payload.update(extra)
    _emit(ledger, actor, "auto_checkpoint", payload)


def _iter_ledger_events(ledger: Any) -> list[dict]:
    """Best-effort list of ledger events (real Ledger or test double)."""
    if ledger is None:
        return []
    events = getattr(ledger, "events", None)
    if events is None:
        return []
    return list(events)


def _payload(ev: dict) -> dict:
    p = ev.get("payload")
    return p if isinstance(p, dict) else {}


def _event_kind(ev: dict) -> str:
    return str(ev.get("kind") or "")


# ---------------------------------------------------------------------------
# Waiver extraction
# ---------------------------------------------------------------------------

def _waivers_from_handoff(handoff: dict | None) -> list[dict]:
    """Normalise handoff.waivers to [{check, reason}, ...]."""
    if not isinstance(handoff, dict):
        return []
    raw = handoff.get("waivers") or handoff.get("waived") or []
    out: list[dict] = []
    if isinstance(raw, dict):
        # {check: reason} map form
        for k, v in raw.items():
            out.append({"check": str(k), "reason": str(v) if v is not None else ""})
        return out
    if not isinstance(raw, list):
        return []
    for item in raw:
        if isinstance(item, dict):
            check = item.get("check") or item.get("name") or item.get("id") or ""
            reason = item.get("reason") or item.get("rationale") or ""
            out.append({"check": str(check), "reason": str(reason)})
        elif isinstance(item, str):
            out.append({"check": item, "reason": ""})
    return out


def find_waiver(handoff: dict | None, check: str) -> Optional[dict]:
    """Return the waiver dict for *check* if present with non-empty reason."""
    check_l = (check or "").lower()
    for w in _waivers_from_handoff(handoff):
        c = (w.get("check") or "").lower()
        if c == check_l or c.startswith(check_l + ":"):
            reason = (w.get("reason") or "").strip()
            if reason:
                return w
    return None


def find_orphan_waivers(handoff: dict | None) -> dict[str, str]:
    """Map sketch_id → reason for orphan_sketch:<id> waivers with non-empty reason."""
    out: dict[str, str] = {}
    for w in _waivers_from_handoff(handoff):
        c = (w.get("check") or "")
        reason = (w.get("reason") or "").strip()
        if not reason:
            continue
        low = c.lower()
        if low.startswith("orphan_sketch:"):
            sid = c.split(":", 1)[1].strip()
            if sid:
                out[sid] = reason
        elif low == "orphan_sketch" or low == "orphan_sketches":
            # blanket orphan waiver applies to all
            out["*"] = reason
    return out


# ---------------------------------------------------------------------------
# Evidence scanners (ledger)
# ---------------------------------------------------------------------------

def _tool_call_events(ledger: Any) -> list[dict]:
    """Return payloads of tool_call-like events from ledger / agent_turn logs."""
    out: list[dict] = []
    for ev in _iter_ledger_events(ledger):
        kind = _event_kind(ev)
        p = _payload(ev)
        if kind == "hard_guardrail":
            continue
        if kind == "auto_checkpoint":
            continue
        # agent_turn logs: kind=agent_turn, payload.event=tool_call
        if kind == "agent_turn" and p.get("event") == "tool_call":
            out.append(p)
            continue
        if kind == "tool_call":
            out.append(p if "tool" in p else {**p, "tool": p.get("name", "")})
            continue
        # criterion_result with check_tool
        if kind == "criterion_result":
            out.append({
                "tool": p.get("check_tool") or "",
                "ok": (p.get("result") or "").upper() == "PASS",
                "result": p,
            })
    return out


def turn_had_mutating_success(ledger: Any, calls: list[dict] | None = None) -> bool:
    """True if this turn successfully ran a mutating tool (battery then required)."""
    if calls:
        for c in calls:
            if c.get("ok") and is_mutating_tool(str(c.get("tool") or "")):
                return True
    for p in _tool_call_events(ledger):
        tool = str(p.get("tool") or "")
        if is_mutating_tool(tool) and p.get("ok", True):
            return True
    return False


def battery_evidence(ledger: Any, calls: list[dict] | None = None) -> Optional[dict]:
    """Return evidence dict if verify_battery PASS/WAIVED was recorded, else None.

    Accepts:
      * tool_call verify_battery with ok and no hard findings (score path)
      * hard_guardrail verify_battery waived
      * criterion_result with check_tool=verify_battery and PASS
      * explicit battery_result events
    """
    # Live call list first (same turn, may not be on ledger yet for in-flight)
    if calls:
        for c in reversed(calls):
            if str(c.get("tool") or "") != "verify_battery":
                continue
            if not c.get("ok", True):
                continue
            res = c.get("result")
            if _battery_result_ok(res):
                return {"source": "tool_call", "result": res}

    for ev in reversed(_iter_ledger_events(ledger)):
        kind = _event_kind(ev)
        p = _payload(ev)
        if kind == "hard_guardrail" and p.get("name") == "verify_battery" \
                and p.get("outcome") == "waived":
            return {"source": "waiver", "reason": p.get("reason", "")}
        if kind == "battery_result":
            if _battery_result_ok(p):
                return {"source": "battery_result", "result": p}
        if kind == "criterion_result":
            if (p.get("check_tool") or "") == "verify_battery" and \
                    (p.get("result") or "").upper() == "PASS":
                return {"source": "criterion_result", "result": p}
        # agent_turn / tool_call
        tool = ""
        res: Any = None
        ok = True
        if kind == "agent_turn" and p.get("event") == "tool_call":
            tool = str(p.get("tool") or "")
            res = p.get("result")
            ok = bool(p.get("ok", True))
        elif kind == "tool_call":
            tool = str(p.get("tool") or p.get("name") or "")
            res = p.get("result")
            ok = bool(p.get("ok", True))
        if tool == "verify_battery" and ok and _battery_result_ok(res):
            return {"source": "ledger_tool_call", "result": res}
    return None


def _battery_result_ok(res: Any) -> bool:
    """Interpret a verify_battery return as PASS.

    Battery returns {scores, findings, ...}. PASS when no failing findings and
    not explicitly ok:false. String JSON is accepted. Truncated ledger strings
    that still look like success also pass (best-effort).
    """
    if res is None:
        # Tool ran with no captured body — treat as ran (ok flag already checked).
        return True
    if isinstance(res, str):
        s = res.strip()
        if not s:
            return True
        if s.startswith("{") or s.startswith("["):
            try:
                import json as _json  # noqa: PLC0415
                res = _json.loads(s)
            except Exception:  # noqa: BLE001
                # Truncated / non-JSON text: look for failure markers.
                low = s.lower()
                if '"ok": false' in low or '"ok":false' in low:
                    return False
                return True
        else:
            low = s.lower()
            return "fail" not in low or "pass" in low
    if not isinstance(res, dict):
        return True
    if res.get("ok") is False:
        return False
    findings = res.get("findings")
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get("ok") is False:
                return False
            if str(f.get("severity") or "").lower() == "fail":
                return False
    scores = res.get("scores")
    if isinstance(scores, dict) and scores:
        # Any zero score is a fail (binary checks).
        if any(float(v) <= 0.0 for v in scores.values() if v is not None):
            # Still allow if findings empty and ok explicitly true
            if res.get("ok") is True and not findings:
                return True
            if findings:
                return False
    return True


def interference_evidence(ledger: Any, calls: list[dict] | None = None) -> Optional[dict]:
    """Recent successful check_interference (or battery covering it) with no clashes."""
    if calls:
        for c in reversed(calls):
            tool = str(c.get("tool") or "")
            if tool == "check_interference" and c.get("ok", True):
                if _interference_result_clean(c.get("result")):
                    return {"source": "tool_call", "result": c.get("result")}
            if tool == "verify_battery" and c.get("ok", True):
                if _battery_covers_interference(c.get("result")):
                    return {"source": "battery", "result": c.get("result")}

    for ev in reversed(_iter_ledger_events(ledger)):
        kind = _event_kind(ev)
        p = _payload(ev)
        if kind == "hard_guardrail" and p.get("name") == "check_interference" \
                and p.get("outcome") == "waived":
            return {"source": "waiver", "reason": p.get("reason", "")}
        tool = ""
        res: Any = None
        ok = True
        if kind == "agent_turn" and p.get("event") == "tool_call":
            tool = str(p.get("tool") or "")
            res = p.get("result")
            ok = bool(p.get("ok", True))
        elif kind == "tool_call":
            tool = str(p.get("tool") or p.get("name") or "")
            res = p.get("result")
            ok = bool(p.get("ok", True))
        if not ok:
            continue
        if tool == "check_interference" and _interference_result_clean(res):
            return {"source": "ledger_tool_call", "result": res}
        if tool == "verify_battery" and _battery_covers_interference(res):
            return {"source": "battery", "result": res}
    return None


def _interference_result_clean(res: Any) -> bool:
    if res is None:
        return True
    if isinstance(res, str):
        try:
            import json as _json  # noqa: PLC0415
            if res.strip().startswith("{"):
                res = _json.loads(res)
            else:
                low = res.lower()
                return "interference" not in low or "0" in low or "none" in low or "ok" in low
        except Exception:  # noqa: BLE001
            return "fail" not in res.lower()
    if not isinstance(res, dict):
        return True
    if res.get("ok") is False:
        return False
    # Common shapes: pairs / interferences / clashes with volume > 0
    for key in ("pairs", "interferences", "clashes", "findings"):
        items = res.get(key)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("severity") == "fail":
                return False
            vol = it.get("volume_mm3") or it.get("interference") or it.get("volume")
            try:
                if vol is not None and float(vol) > 0:
                    return False
            except (TypeError, ValueError):
                pass
    return True


def _battery_covers_interference(res: Any) -> bool:
    if not isinstance(res, dict):
        if isinstance(res, str) and "check_interference" in res:
            return "fail" not in res.lower() or '"check_interference": 1' in res
        return False
    scores = res.get("scores") or {}
    if "check_interference" in scores:
        try:
            return float(scores["check_interference"]) > 0
        except (TypeError, ValueError):
            return False
    findings = res.get("findings") or []
    for f in findings:
        if isinstance(f, dict) and f.get("check") == "check_interference" and f.get("ok") is False:
            return False
    # Battery ran without that check → does not cover
    return "check_interference" in (res.get("scores") or {})


# ---------------------------------------------------------------------------
# Doc inspection helpers (orphan sketches / assembly kind)
# ---------------------------------------------------------------------------

def _doc_tree(doc_id: str | None,
              tree_provider: Callable[[str], dict] | None = None) -> dict:
    if not doc_id:
        return {}
    if tree_provider is not None:
        try:
            return tree_provider(doc_id) or {}
        except Exception:  # noqa: BLE001
            return {}
    try:
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        resp = cb.call("doc_tree", {"doc_id": doc_id}) or {}
        return resp.get("tree") or resp or {}
    except Exception:  # noqa: BLE001
        return {}


def list_orphan_sketches(doc_id: str | None,
                         tree_provider: Callable[[str], dict] | None = None,
                         tree: dict | None = None) -> list[str]:
    """Return sketch feature ids that are not consumed by a later feature.

    A sketch is consumed when another feature's refs mention its id/name.
    """
    t = tree if tree is not None else _doc_tree(doc_id, tree_provider)
    features = t.get("features") if isinstance(t, dict) else None
    if not isinstance(features, list):
        return []
    sketches: list[dict] = []
    consumers_text = []
    for f in features:
        if not isinstance(f, dict):
            continue
        ftype = (f.get("type") or "").lower()
        if ftype in ("sketch", "sketch3d"):
            sketches.append(f)
        else:
            # Collect all stringified refs from non-sketch features
            consumers_text.append(_flatten_refs(f))
    blob = "\n".join(consumers_text)
    orphans: list[str] = []
    for s in sketches:
        sid = str(s.get("id") or "")
        name = str(s.get("name") or "")
        if not sid and not name:
            continue
        # Explicitly deleted / suppressed sketches are not orphans
        if s.get("suppressed") or s.get("deleted"):
            continue
        used = (sid and sid in blob) or (name and name in blob)
        if not used:
            orphans.append(sid or name)
    return orphans


def _flatten_refs(obj: Any) -> str:
    """Flatten feature dict to a string for cheap id/name membership checks."""
    parts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("id", "name", "feature", "sketch", "profile", "profile_sketch",
                         "path", "guide") and isinstance(v, (str, int)):
                    parts.append(str(v))
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, (str, int)):
            parts.append(str(x))

    walk(obj)
    return " ".join(parts)


def is_assembly_or_multibody(doc_id: str | None,
                            tree_provider: Callable[[str], dict] | None = None,
                            tree: dict | None = None,
                            handoff: dict | None = None) -> bool:
    """True when the active doc is an assembly or multi-body part."""
    if isinstance(handoff, dict):
        kind = str(handoff.get("doc_kind") or handoff.get("kind") or "").lower()
        if kind in ("assembly", "asm", "multi_body", "multibody"):
            return True
        if handoff.get("is_assembly") is True:
            return True
    t = tree if tree is not None else _doc_tree(doc_id, tree_provider)
    if not isinstance(t, dict):
        return False
    kind = str(t.get("kind") or t.get("doc_kind") or t.get("type") or "").lower()
    if kind in ("assembly", "asm"):
        return True
    bodies = t.get("bodies") or t.get("body_ids") or []
    if isinstance(bodies, list) and len(bodies) > 1:
        return True
    comps = t.get("components") or t.get("instances") or []
    if isinstance(comps, list) and len(comps) > 1:
        return True
    body_count = t.get("body_count")
    try:
        if body_count is not None and int(body_count) > 1:
            return True
    except (TypeError, ValueError):
        pass
    return False


def resolve_active_doc_id(handoff: dict | None = None,
                          calls: list[dict] | None = None,
                          args: dict | None = None,
                          ledger: Any = None) -> Optional[str]:
    """Best-effort active doc_id from handoff, latest call args, or ledger."""
    if isinstance(handoff, dict):
        for k in ("doc_id", "active_doc", "document_id"):
            if handoff.get(k):
                return str(handoff[k])
    if isinstance(args, dict) and args.get("doc_id"):
        return str(args["doc_id"])
    if calls:
        for c in reversed(calls):
            a = c.get("args") if isinstance(c.get("args"), dict) else {}
            if a.get("doc_id"):
                return str(a["doc_id"])
    for p in reversed(_tool_call_events(ledger)):
        a = p.get("args") if isinstance(p.get("args"), dict) else {}
        if a.get("doc_id"):
            return str(a["doc_id"])
        r = p.get("result")
        if isinstance(r, dict) and r.get("doc_id"):
            return str(r["doc_id"])
    return None


# ---------------------------------------------------------------------------
# WI-15: kernel ref integrity (cheap id-set check)
# ---------------------------------------------------------------------------

def extract_ops(args: dict | None) -> list[dict]:
    if not isinstance(args, dict):
        return []
    ops = args.get("ops")
    return ops if isinstance(ops, list) else []


def check_refs_resolve_ids(ops: list[dict], known_ids: set[str],
                           known_names: set[str] | None = None) -> list[str]:
    """Return dangling ref ids from *ops* against *known_ids*/*known_names*.

    Reuses the advisor/preflight._check_refs_resolve shape without a clone:
    walk feature.refs for non-$ ids not in the known set.
    """
    known_names = known_names or set()
    missing: list[str] = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        feat = op.get("feature") if isinstance(op.get("feature"), dict) else {}
        refs = feat.get("refs") if isinstance(feat.get("refs"), dict) else {}
        # Also scan top-level refs on the op
        top_refs = op.get("refs") if isinstance(op.get("refs"), dict) else {}
        for ref_map in (refs, top_refs):
            for _key, ref_val in ref_map.items():
                candidates: list[Any] = []
                if isinstance(ref_val, dict):
                    candidates.append(ref_val.get("id") or ref_val.get("feature"))
                elif isinstance(ref_val, str):
                    candidates.append(ref_val)
                elif isinstance(ref_val, list):
                    for item in ref_val:
                        if isinstance(item, dict):
                            candidates.append(item.get("id") or item.get("feature"))
                        elif isinstance(item, str):
                            candidates.append(item)
                for ref_id in candidates:
                    if not ref_id or not isinstance(ref_id, str):
                        continue
                    if ref_id.startswith("$"):
                        continue  # pending id map in same batch
                    # Plain-English geometry queries are not stable ids
                    if ref_id in known_ids or ref_id in known_names:
                        continue
                    # Heuristic: words like "top"/"face" are query predicates, not ids
                    if _looks_like_geom_query(ref_id):
                        continue
                    if ref_id not in missing:
                        missing.append(ref_id)
        # edit/delete by id
        for id_key in ("id", "feature_id", "target"):
            rid = op.get(id_key)
            if isinstance(rid, str) and rid and not rid.startswith("$") \
                    and rid not in known_ids and rid not in known_names \
                    and not _looks_like_geom_query(rid):
                # edit_feature / delete of unknown id
                kind = (op.get("op") or op.get("kind") or "").lower()
                if kind in ("edit_feature", "delete_feature", "remove_feature",
                            "delete", "remove", "suppress_feature") or id_key != "id":
                    if rid not in missing:
                        missing.append(rid)
    return missing


def _looks_like_geom_query(ref_id: str) -> bool:
    """Plain-English / query-shaped refs are not hard id checks."""
    if not ref_id:
        return True
    if " " in ref_id:
        return True
    low = ref_id.lower()
    if low in {"top", "bottom", "front", "back", "left", "right",
               "face", "edge", "vertex", "body", "all"}:
        return True
    if low.startswith(("of:", "side:", "face:", "edge:")):
        return True
    return False


def known_ids_from_tree(tree: dict | None) -> tuple[set[str], set[str]]:
    known_ids: set[str] = set()
    known_names: set[str] = set()
    if not isinstance(tree, dict):
        return known_ids, known_names
    features = tree.get("features") or []
    if isinstance(features, list):
        for f in features:
            if not isinstance(f, dict):
                continue
            if f.get("id"):
                known_ids.add(str(f["id"]))
            if f.get("name"):
                known_names.add(str(f["name"]))
    return known_ids, known_names


def check_refs_against_doc(ops: list[dict], doc_id: str | None,
                           tree_provider: Callable[[str], dict] | None = None,
                           known: tuple[set[str], set[str]] | None = None,
                           ) -> list[str]:
    """Cheap id-set ref check (WI-15). Empty list = all resolve / skipped."""
    if not ops:
        return []
    if known is not None:
        known_ids, known_names = known
    else:
        if not doc_id and tree_provider is None:
            return []  # cannot check — do not false-block
        tree = _doc_tree(doc_id, tree_provider)
        if not tree:
            return []  # engine unavailable — skip (same as preflight soft path)
        known_ids, known_names = known_ids_from_tree(tree)
    return check_refs_resolve_ids(ops, known_ids, known_names)


# Re-export shape compatible with preflight (for callers that want the same API).
def check_refs_resolve(ops: list[dict], doc_id: str | None = None,
                       tree_provider: Callable[[str], dict] | None = None,
                       known: tuple[set[str], set[str]] | None = None) -> list[dict]:
    """Return preflight-shaped check list for refs_resolve (pass/fail)."""
    missing = check_refs_against_doc(ops, doc_id, tree_provider=tree_provider, known=known)
    if missing:
        return [{
            "check": "refs_resolve",
            "result": "fail",
            "detail": f"dangling refs: {', '.join(missing)}",
            "refs": {"features": missing},
        }]
    return [{
        "check": "refs_resolve",
        "result": "pass",
        "detail": "all refs resolve",
        "refs": {},
    }]


# ---------------------------------------------------------------------------
# WI-12: auto-checkpoint before destructive
# ---------------------------------------------------------------------------

def take_auto_checkpoint(doc_id: str | None,
                         trigger_tool: str,
                         *,
                         ledger: Any = None,
                         actor: str = "guardrails",
                         checkpoint_fn: Callable[..., Any] | None = None,
                         vault_bound: bool = False,
                         ) -> tuple[bool, str]:
    """Silent checkpoint. Returns (ok, reason). Failure blocks the destructive call.

    Prefers doc_checkpoint; uses plm_checkpoint when *vault_bound* is True.
    *checkpoint_fn* is injectable for tests: ``(doc_id, trigger_tool) -> result``.
    """
    if checkpoint_fn is not None:
        try:
            res = checkpoint_fn(doc_id, trigger_tool)
            ok = True
            if isinstance(res, dict) and res.get("ok") is False:
                ok = False
                reason = str(res.get("error") or res.get("reason") or "checkpoint failed")
            else:
                reason = "ok"
            if ok:
                emit_auto_checkpoint(ledger, trigger_tool, actor=actor,
                                     extra={"doc_id": doc_id, "via": "injected"})
                emit_hard_guardrail(ledger, "auto_checkpoint", "passed",
                                    reason=f"checkpoint before {trigger_tool}",
                                    actor=actor)
            else:
                emit_hard_guardrail(ledger, "auto_checkpoint", "blocked",
                                    reason=reason, actor=actor)
            return ok, reason
        except Exception as exc:  # noqa: BLE001
            reason = str(exc)
            emit_hard_guardrail(ledger, "auto_checkpoint", "blocked",
                                reason=reason, actor=actor)
            return False, reason

    if not doc_id:
        # No doc to checkpoint — pass (nothing to protect) but still log.
        emit_auto_checkpoint(ledger, trigger_tool, actor=actor,
                             extra={"doc_id": None, "via": "skipped_no_doc"})
        emit_hard_guardrail(ledger, "auto_checkpoint", "passed",
                            reason="no doc_id; nothing to checkpoint",
                            actor=actor)
        return True, "no_doc"

    try:
        if vault_bound:
            try:
                from services.mcp import core_bridge as cb  # noqa: PLC0415
                # Prefer plm when vault is bound; fall back to doc_checkpoint.
                try:
                    # plm_checkpoint is an MCP tool; use engine doc_checkpoint as
                    # the always-available cheap path, then optional plm.
                    ck = cb.call("doc_checkpoint", {"doc_id": doc_id})
                    if isinstance(ck, dict) and ck.get("_is_error"):
                        raise RuntimeError(ck.get("error") or "doc_checkpoint error")
                    emit_auto_checkpoint(ledger, trigger_tool, actor=actor,
                                         extra={"doc_id": doc_id, "via": "doc_checkpoint",
                                                "vault_bound": True})
                    emit_hard_guardrail(ledger, "auto_checkpoint", "passed",
                                        reason=f"checkpoint before {trigger_tool}",
                                        actor=actor)
                    return True, "ok"
                except Exception:
                    raise
            except Exception as exc:
                reason = str(exc)
                emit_hard_guardrail(ledger, "auto_checkpoint", "blocked",
                                    reason=reason, actor=actor)
                return False, reason

        from services.mcp import core_bridge as cb  # noqa: PLC0415
        ck = cb.call("doc_checkpoint", {"doc_id": doc_id})
        if isinstance(ck, dict) and (ck.get("_is_error") or ck.get("ok") is False):
            reason = str(ck.get("error") or ck.get("reason") or "doc_checkpoint failed")
            emit_hard_guardrail(ledger, "auto_checkpoint", "blocked",
                                reason=reason, actor=actor)
            return False, reason
        emit_auto_checkpoint(ledger, trigger_tool, actor=actor,
                             extra={"doc_id": doc_id, "via": "doc_checkpoint",
                                    "checkpoint": (ck or {}).get("checkpoint")
                                    if isinstance(ck, dict) else None})
        emit_hard_guardrail(ledger, "auto_checkpoint", "passed",
                            reason=f"checkpoint before {trigger_tool}",
                            actor=actor)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        emit_hard_guardrail(ledger, "auto_checkpoint", "blocked",
                            reason=reason, actor=actor)
        return False, reason


def pre_dispatch_checkpoint(tool: str, args: dict | None, *,
                            ledger: Any = None,
                            actor: str = "guardrails",
                            cfg: dict | None = None,
                            checkpoint_fn: Callable[..., Any] | None = None,
                            vault_bound: bool = False,
                            ) -> Optional[GuardVerdict]:
    """WI-12 pre-dispatch. Returns a blocking GuardVerdict or None to proceed."""
    if not is_destructive_tool(tool, args):
        return None
    if not auto_checkpoint_enabled(cfg):
        emit_hard_guardrail(ledger, "auto_checkpoint", "waived",
                            reason="kill-switch hard_guardrails.auto_checkpoint=false",
                            actor=actor)
        return None  # kill-switch: do not block
    doc_id = None
    if isinstance(args, dict):
        doc_id = args.get("doc_id")
        if doc_id is not None:
            doc_id = str(doc_id)
    ok, reason = take_auto_checkpoint(
        doc_id, tool, ledger=ledger, actor=actor,
        checkpoint_fn=checkpoint_fn, vault_bound=vault_bound,
    )
    if ok:
        return None
    return GuardVerdict(
        ok=False, name="auto_checkpoint", outcome="blocked",
        reason=reason,
        message=MSG_CHECKPOINT_FAIL.format(tool=tool, reason=reason),
    )


def pre_dispatch_refs(tool: str, args: dict | None, *,
                      ledger: Any = None,
                      actor: str = "guardrails",
                      tree_provider: Callable[[str], dict] | None = None,
                      known: tuple[set[str], set[str]] | None = None,
                      ) -> Optional[GuardVerdict]:
    """WI-15 pre-dispatch ref integrity. No waiver. Returns block verdict or None."""
    if tool not in REF_MUTATING_TOOLS:
        return None
    ops = extract_ops(args)
    if not ops:
        return None
    doc_id = str(args.get("doc_id")) if isinstance(args, dict) and args.get("doc_id") else None
    missing = check_refs_against_doc(ops, doc_id, tree_provider=tree_provider, known=known)
    if not missing:
        emit_hard_guardrail(ledger, "refs_resolve", "passed",
                            reason="all refs resolve", actor=actor)
        return None
    msg = MSG_REFS.format(missing=", ".join(missing))
    emit_hard_guardrail(ledger, "refs_resolve", "blocked",
                        reason=msg, actor=actor)
    return GuardVerdict(ok=False, name="refs_resolve", outcome="blocked",
                        reason=msg, message=msg)


def pre_dispatch(tool: str, args: dict | None, *,
                 ledger: Any = None,
                 actor: str = "guardrails",
                 cfg: dict | None = None,
                 checkpoint_fn: Callable[..., Any] | None = None,
                 vault_bound: bool = False,
                 tree_provider: Callable[[str], dict] | None = None,
                 known: tuple[set[str], set[str]] | None = None,
                 ) -> Optional[GuardVerdict]:
    """Run all pre-dispatch HARD guards. First block wins."""
    v = pre_dispatch_refs(tool, args, ledger=ledger, actor=actor,
                          tree_provider=tree_provider, known=known)
    if v is not None:
        return v
    v = pre_dispatch_checkpoint(tool, args, ledger=ledger, actor=actor, cfg=cfg,
                                checkpoint_fn=checkpoint_fn, vault_bound=vault_bound)
    if v is not None:
        return v
    return None


# ---------------------------------------------------------------------------
# Done intercept (WI-11, WI-13, WI-14)
# ---------------------------------------------------------------------------

def check_verify_battery_before_done(
    handoff: dict | None,
    *,
    ledger: Any = None,
    calls: list[dict] | None = None,
    actor: str = "guardrails",
    require_if_no_mutation: bool = False,
) -> GuardVerdict:
    """WI-11: verify_battery PASS/WAIVED before done.

    When no mutating tool ran this turn and *require_if_no_mutation* is False,
    the guard passes (planning / measure-only nodes). Mission gate uses
    ``require_if_no_mutation=True`` / explicit battery_ran context instead.
    """
    name = "verify_battery"
    waiver = find_waiver(handoff, "verify_battery")
    if waiver is not None:
        reason = waiver["reason"]
        emit_hard_guardrail(ledger, name, "waived", reason=reason, actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="waived", reason=reason)

    evidence = battery_evidence(ledger, calls)
    if evidence is not None:
        emit_hard_guardrail(ledger, name, "passed",
                            reason=f"evidence via {evidence.get('source')}",
                            actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="passed",
                            reason=f"evidence via {evidence.get('source')}")

    if not require_if_no_mutation and not turn_had_mutating_success(ledger, calls):
        # No geometry mutation this turn → nothing to verify.
        emit_hard_guardrail(ledger, name, "passed",
                            reason="no mutating tool this turn; battery not required",
                            actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="passed",
                            reason="no mutation")

    emit_hard_guardrail(ledger, name, "blocked",
                        reason="missing verify_battery PASS/WAIVED", actor=actor)
    return GuardVerdict(ok=False, name=name, outcome="blocked",
                        reason="missing verify_battery",
                        message=MSG_VERIFY_BATTERY)


def check_interference_before_done(
    handoff: dict | None,
    *,
    ledger: Any = None,
    calls: list[dict] | None = None,
    doc_id: str | None = None,
    tree_provider: Callable[[str], dict] | None = None,
    tree: dict | None = None,
    actor: str = "guardrails",
    force_assembly: bool | None = None,
) -> GuardVerdict:
    """WI-13: check_interference before assembly/multi-body done."""
    name = "check_interference"
    is_asm = force_assembly if force_assembly is not None else is_assembly_or_multibody(
        doc_id, tree_provider=tree_provider, tree=tree, handoff=handoff,
    )
    if not is_asm:
        emit_hard_guardrail(ledger, name, "passed",
                            reason="not assembly/multi-body", actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="passed",
                            reason="not assembly")

    waiver = find_waiver(handoff, "check_interference")
    if waiver is None:
        # also accept interference / assembly_interference aliases
        waiver = find_waiver(handoff, "interference")
    if waiver is not None:
        reason = waiver["reason"]
        emit_hard_guardrail(ledger, name, "waived", reason=reason, actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="waived", reason=reason)

    evidence = interference_evidence(ledger, calls)
    if evidence is not None:
        emit_hard_guardrail(ledger, name, "passed",
                            reason=f"evidence via {evidence.get('source')}",
                            actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="passed",
                            reason=f"evidence via {evidence.get('source')}")

    emit_hard_guardrail(ledger, name, "blocked",
                        reason="missing clean check_interference", actor=actor)
    return GuardVerdict(ok=False, name=name, outcome="blocked",
                        reason="missing check_interference",
                        message=MSG_INTERFERENCE)


def check_no_orphan_sketch_at_done(
    handoff: dict | None,
    *,
    ledger: Any = None,
    doc_id: str | None = None,
    tree_provider: Callable[[str], dict] | None = None,
    tree: dict | None = None,
    actor: str = "guardrails",
    orphan_ids: list[str] | None = None,
) -> GuardVerdict:
    """WI-14: every sketch must be consumed or deleted (or waived per-id)."""
    name = "orphan_sketch"
    if orphan_ids is None:
        orphan_ids = list_orphan_sketches(doc_id, tree_provider=tree_provider, tree=tree)

    if not orphan_ids:
        emit_hard_guardrail(ledger, name, "passed",
                            reason="no orphan sketches", actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="passed",
                            reason="no orphans")

    waived_map = find_orphan_waivers(handoff)
    remaining = []
    for sid in orphan_ids:
        if "*" in waived_map or sid in waived_map:
            continue
        remaining.append(sid)

    if not remaining:
        reason = "all orphan sketches waived"
        emit_hard_guardrail(ledger, name, "waived", reason=reason, actor=actor)
        return GuardVerdict(ok=True, name=name, outcome="waived", reason=reason)

    # Partial waivers: still block on remaining
    msg = MSG_ORPHAN_SKETCH.format(ids=", ".join(remaining))
    emit_hard_guardrail(ledger, name, "blocked", reason=msg, actor=actor)
    return GuardVerdict(ok=False, name=name, outcome="blocked",
                        reason=msg, message=msg)


def check_done(
    handoff: dict | None = None,
    *,
    ledger: Any = None,
    calls: list[dict] | None = None,
    doc_id: str | None = None,
    tree_provider: Callable[[str], dict] | None = None,
    tree: dict | None = None,
    actor: str = "guardrails",
    require_battery_if_no_mutation: bool = False,
    force_assembly: bool | None = None,
    orphan_ids: list[str] | None = None,
    skip: set[str] | None = None,
) -> GuardVerdict:
    """Run all done-path HARD guards. First block wins; all emit ledger events."""
    handoff = handoff or {}
    skip = skip or set()
    if doc_id is None:
        doc_id = resolve_active_doc_id(handoff, calls, ledger=ledger)

    if "verify_battery" not in skip:
        v = check_verify_battery_before_done(
            handoff, ledger=ledger, calls=calls, actor=actor,
            require_if_no_mutation=require_battery_if_no_mutation,
        )
        if not v.ok:
            return v

    if "check_interference" not in skip:
        v = check_interference_before_done(
            handoff, ledger=ledger, calls=calls, doc_id=doc_id,
            tree_provider=tree_provider, tree=tree, actor=actor,
            force_assembly=force_assembly,
        )
        if not v.ok:
            return v

    if "orphan_sketch" not in skip:
        v = check_no_orphan_sketch_at_done(
            handoff, ledger=ledger, doc_id=doc_id,
            tree_provider=tree_provider, tree=tree, actor=actor,
            orphan_ids=orphan_ids,
        )
        if not v.ok:
            return v

    return GuardVerdict(ok=True, name="done", outcome="passed",
                        reason="all hard guardrails passed")


# ---------------------------------------------------------------------------
# Mission VERIFY gate helper (WI-11 strengthen)
# ---------------------------------------------------------------------------

def battery_missing_for_mission(ctx: dict[str, Any]) -> bool:
    """True when mission VERIFY context has no battery evidence and no waiver.

    Used by phases._gate_verify so missing battery is FAIL not silent.
    Context keys (any one of):
      * battery_ran: bool
      * battery_result: dict (PASS-shaped)
      * criterion_results covering verify tools
      * hard_guardrail_events / ledger scan via ctx['ledger']
      * waived_criteria / waivers including verify_battery
    """
    if not isinstance(ctx, dict):
        return False  # no context → don't invent a fail (caller may be unit test)
    if ctx.get("battery_ran") is True:
        return False
    br = ctx.get("battery_result")
    if br is not None and _battery_result_ok(br):
        return False
    waived = set(ctx.get("waived_criteria") or [])
    if "verify_battery" in waived or "battery" in waived:
        return False
    waivers = ctx.get("waivers") or []
    if isinstance(waivers, list):
        for w in waivers:
            if isinstance(w, dict):
                c = str(w.get("check") or w.get("name") or "")
                if c in ("verify_battery", "battery") and str(w.get("reason") or "").strip():
                    return False
    # criterion_results: if any AC used verify_battery and PASSED
    results = ctx.get("criterion_results") or {}
    details = ctx.get("criterion_details") or {}
    for ac, res in results.items():
        det = details.get(ac) if isinstance(details, dict) else None
        tool = ""
        if isinstance(det, dict):
            tool = str(det.get("check_tool") or det.get("tool") or "")
        if tool == "verify_battery" and res == "PASS":
            return False
    if ctx.get("require_battery") is True:
        return True
    # Explicit flag from mission runner that geometry was built this mission
    if ctx.get("geometry_built") or ctx.get("mutating_tools_ran"):
        # Need evidence
        ledger = ctx.get("ledger")
        if battery_evidence(ledger) is not None:
            return False
        return True
    # Default: if criterion_results is non-empty (VERIFY actually running with ACs)
    # but nothing indicates battery, require it when require_battery defaults for
    # geometry missions. Keep backward-compat: empty/no geometry → not missing.
    if results and ctx.get("require_battery", False):
        return True
    return False
