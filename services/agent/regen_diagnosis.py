"""IP-18 regen-error diagnosis: frontier-backed fix-op proposals for docs that fail to rebuild.

When a rebuild fails (dangling refs after a topology-splitting edit, a suppressed parent,
an impossible parameter, a broken selector, a circular equation) the model is handed the
failure signal + the feature tree + the valid vocabulary, and returns a list of **doc-ops**
that repair the document. The ops are ordinary editable document mutations — never opaque
scripts — so R1 editability parity holds (the user can inspect/edit/reject every proposed
change).

Two things make this the FRONTIER-FIRST ship of IP-18:

1. The failure signal is derived **structurally from the corrupted document itself** (or the
   real engine feature_status error in the live flow) — never from any bench label. Peeking
   at ground-truth (mutation_class / target) would invalidate the regen_fix bench.
2. Every provider call is routed through ``LoggingProvider(insertion_point="IP-18")``
   so each real failure->fix becomes a correction trace for the later SFT (Pillar 16). The
   migration to a local model is a LATER, bench-gated phase; this module ships the frontier
   path and the logging seam.

The module is provider-agnostic and offline-safe: with no provider it does nothing; with a
MockProvider it is fully unit-testable without credentials or the C++ core.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .providers.base import Provider, text_part, user

_LOG = logging.getLogger(__name__)

# Selector kinds the doc schema accepts (kept in lockstep with the regen_fix bench detector).
VALID_SELECTOR_KINDS = ("face", "edge", "vertex", "body", "datum", "region", "plane")

# The doc-op vocabulary the model may return. Each maps to an editable document mutation.
FIX_OPS = ("repoint_ref", "set_selector_kind", "unsuppress", "set_param", "set_equation")


# ---------------------------------------------------------------------------
# Tree access helpers (a corpus doc is {revision, tree, owner_map}; a bare part
# doc is the tree itself). Everything below operates on the tree.
# ---------------------------------------------------------------------------

def _tree(document: dict) -> dict:
    return document.get("tree", document)


def _features(document: dict) -> list[dict]:
    return _tree(document).get("features", [])


def _feature_ids(document: dict) -> set[str]:
    return {f["id"] for f in _features(document)}


def _globals(document: dict) -> list[dict]:
    return _tree(document).get("equations", {}).get("globals", [])


def _iter_selectors(node: Any):
    """Yield every selector dict (has a string ``kind``) anywhere under ``node``."""
    if isinstance(node, dict):
        if isinstance(node.get("kind"), str):
            yield node
        for v in node.values():
            yield from _iter_selectors(v)
    elif isinstance(node, list):
        for it in node:
            yield from _iter_selectors(it)


def _iter_numeric_params(node: Any):
    """Yield every {'value': number, 'expr': ...} param dict under ``node``."""
    if isinstance(node, dict):
        if isinstance(node.get("value"), (int, float)) and "expr" in node:
            yield node
        for v in node.values():
            yield from _iter_numeric_params(v)
    elif isinstance(node, list):
        for it in node:
            yield from _iter_numeric_params(it)


def _dep_ids(feat: dict) -> set[str]:
    """Feature ids ``feat`` depends on: selector owner_feature/sketch keys + a pattern
    ``features`` list of plain ids. Matches the corpus mutator / bench detector."""
    ids: set[str] = set()
    refs = feat.get("refs", {})
    for sel in _iter_selectors(refs):
        for key in ("owner_feature", "sketch"):
            if isinstance(sel.get(key), str):
                ids.add(sel[key])
    fl = refs.get("features")
    if isinstance(fl, list):
        ids.update(x for x in fl if isinstance(x, str))
    return ids


def _equation_cycle(globals_: list[dict]) -> Optional[tuple[str, str]]:
    """Return (name_a, name_b) of one trivial cycle (``a.expr == b`` and ``b.expr == a``
    reachable), or None. Only the 'expr is exactly another global name' edges matter for
    the corpus's circular_equation class."""
    names = {g["name"] for g in globals_}
    expr = {g["name"]: (g.get("expr") or "") for g in globals_}

    def reaches(start: str, target: str, seen: set[str]) -> bool:
        nxt = expr.get(start, "")
        if nxt not in names:
            return False
        if nxt == target:
            return True
        if nxt in seen:
            return False
        seen.add(nxt)
        return reaches(nxt, target, seen)

    for n in sorted(names):
        if reaches(n, n, set()):
            # the immediate next hop from n is the partner in the cycle
            return (n, expr.get(n, ""))
    return None


# ---------------------------------------------------------------------------
# Structural failure detection — the diagnosis INPUT (no label peeking)
# ---------------------------------------------------------------------------

@dataclass
class Failure:
    """One structural or native rebuild failure, with the vocabulary a fix needs."""
    kind: str                       # dangling_ref | bad_selector | suppressed_parent |
                                    # param_out_of_range | circular_equation | native_rebuild
    feature: Optional[str]          # feature id involved (None for equation cycles)
    detail: str                     # human-readable, prompt-ready
    options: dict[str, Any] = field(default_factory=dict)  # valid ids/kinds to choose from

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def detect_failures(document: dict) -> list[Failure]:
    """Derive the structural rebuild-failure signal from the document alone.

    This is the OFFLINE failure signal. ``diagnostic_failures`` adds the live engine's
    feature_status evidence without claiming to infer geometry failures structurally.
    Deterministic and label-free so the bench stays honest.
    """
    fids = _feature_ids(document)
    valid_ids = sorted(fids)
    out: list[Failure] = []

    cyc = _equation_cycle(_globals(document))
    if cyc is not None:
        a, b = cyc
        out.append(Failure(
            kind="circular_equation", feature=None,
            detail=f"global equation '{a}' and '{b}' reference each other (a cycle).",
            options={"cycle": [a, b], "globals": [g["name"] for g in _globals(document)]},
        ))

    for feat in _features(document):
        fid = feat.get("id")
        for sel in _iter_selectors(feat.get("refs", {})):
            for key in ("owner_feature", "sketch"):
                ref = sel.get(key)
                if isinstance(ref, str) and ref not in fids:
                    out.append(Failure(
                        kind="dangling_ref", feature=fid,
                        detail=f"feature '{fid}' selector {key} points at missing id '{ref}'.",
                        options={"from": ref, "valid_feature_ids": valid_ids},
                    ))
            kind = sel.get("kind")
            if isinstance(kind, str) and kind not in VALID_SELECTOR_KINDS:
                out.append(Failure(
                    kind="bad_selector", feature=fid,
                    detail=f"feature '{fid}' selector kind '{kind}' is not a valid kind.",
                    options={"bad_kind": kind, "valid_kinds": list(VALID_SELECTOR_KINDS)},
                ))
        fl = feat.get("refs", {}).get("features")
        if isinstance(fl, list):
            for ref in fl:
                if isinstance(ref, str) and ref not in fids:
                    out.append(Failure(
                        kind="dangling_ref", feature=fid,
                        detail=f"feature '{fid}' pattern references missing id '{ref}'.",
                        options={"from": ref, "valid_feature_ids": valid_ids},
                    ))
        if feat.get("suppressed"):
            dependents = [o["id"] for o in _features(document)
                          if o.get("id") != fid and fid in _dep_ids(o)]
            if dependents:
                out.append(Failure(
                    kind="suppressed_parent", feature=fid,
                    detail=(f"feature '{fid}' is suppressed but {dependents} still depend on it."),
                    options={"dependents": dependents},
                ))
        for p in _iter_numeric_params(feat.get("params", {})):
            v = p["value"]
            if v < 0 or abs(v) > 1e5:
                out.append(Failure(
                    kind="param_out_of_range", feature=fid,
                    detail=f"feature '{fid}' has an impossible numeric parameter value {v}.",
                    options={"value": v},
                ))
    return out


def diagnostic_failures(document: dict) -> list[Failure]:
    """Keep native feature failures alongside the offline structural findings."""
    failures = detect_failures(document)
    features = {f.get("id"): f for f in _features(document)}
    for status in (document.get("rebuild") or {}).get("feature_status", []):
        if status.get("ok", True):
            continue
        fid = status.get("id")
        feature = features.get(fid, {})
        failures.append(Failure(
            kind="native_rebuild", feature=fid,
            detail=str(status.get("error") or "engine reported feature rebuild failure"),
            options={"name": feature.get("name"), "type": feature.get("type"),
                     "params": feature.get("params", {}), "refs": feature.get("refs", {})},
        ))
    return failures


# ---------------------------------------------------------------------------
# Fix-op application — the diagnosis OUTPUT (editable doc-ops, never scripts)
# ---------------------------------------------------------------------------

def apply_fix_ops(document: dict, ops: list[dict]) -> tuple[dict, int]:
    """Apply a list of doc-ops, returning (new_document, n_applied).

    Each op is a small, inspectable document mutation. Unknown or non-applicable ops are
    skipped (they simply don't count toward n_applied) so a malformed model reply degrades
    to 'no fix' rather than corrupting the doc further.
    """
    import copy
    doc = copy.deepcopy(document)
    tree = _tree(doc)
    feats = {f["id"]: f for f in tree.get("features", [])}
    applied = 0

    for op in ops:
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        if name == "repoint_ref":
            feat = feats.get(op.get("feature"))
            frm, to = op.get("from"), op.get("to")
            if feat is None or not isinstance(to, str):
                continue
            if _repoint(feat, frm, to):
                applied += 1
        elif name == "set_selector_kind":
            feat = feats.get(op.get("feature"))
            new_kind = op.get("kind")
            if feat is None or new_kind not in VALID_SELECTOR_KINDS:
                continue
            old = op.get("old_kind")
            if _set_kind(feat, old, new_kind):
                applied += 1
        elif name == "unsuppress":
            feat = feats.get(op.get("feature"))
            if feat is not None and feat.get("suppressed"):
                feat["suppressed"] = False
                applied += 1
        elif name == "set_param":
            feat = feats.get(op.get("feature"))
            if feat is not None and "value" in op:
                if _set_param(feat, op.get("param"), op["value"], op.get("expr", "__keep__")):
                    applied += 1
        elif name == "set_equation":
            if _set_equation(tree, op.get("global"), op.get("expr")):
                applied += 1
    return doc, applied


def _repoint(feat: dict, frm: Optional[str], to: str) -> bool:
    """Repoint owner_feature/sketch selectors (and pattern feature ids) equal to ``frm`` -> ``to``.
    If ``frm`` is None, repoint any selector target that is itself missing is left to the
    caller; here None means 'repoint the first dangling-looking selector'."""
    changed = False
    for sel in _iter_selectors(feat.get("refs", {})):
        for key in ("owner_feature", "sketch"):
            cur = sel.get(key)
            if isinstance(cur, str) and (cur == frm or frm is None):
                sel[key] = to
                changed = True
                if frm is None:
                    return changed
    fl = feat.get("refs", {}).get("features")
    if isinstance(fl, list):
        for i, ref in enumerate(fl):
            if isinstance(ref, str) and (ref == frm or frm is None):
                fl[i] = to
                changed = True
                if frm is None:
                    return changed
    return changed


def _set_kind(feat: dict, old: Optional[str], new_kind: str) -> bool:
    changed = False
    for sel in _iter_selectors(feat.get("refs", {})):
        k = sel.get("kind")
        if isinstance(k, str) and (k == old or old is None or k not in VALID_SELECTOR_KINDS):
            sel["kind"] = new_kind
            changed = True
    return changed


def _set_param(feat: dict, param_name: Optional[str], value: float, expr: Any) -> bool:
    """Set a numeric parameter's value (and optionally its expr). ``param_name`` matches the
    key under ``params`` whose dict holds the value; None sets the first out-of-range one."""
    params = feat.get("params", {})
    # direct-key match first (params[name] == {value, expr})
    if isinstance(param_name, str) and isinstance(params.get(param_name), dict) \
            and "value" in params[param_name]:
        params[param_name]["value"] = value
        if expr != "__keep__":
            params[param_name]["expr"] = expr
        return True
    # else target an out-of-range param (the actual failure); fall back to the first numeric
    numeric = list(_iter_numeric_params(params))
    out_of_range = [p for p in numeric if p["value"] < 0 or abs(p["value"]) > 1e5]
    target = out_of_range or (numeric if param_name is None else [])
    if not target:
        return False
    p = target[0]
    p["value"] = value
    if expr != "__keep__":
        p["expr"] = expr
    return True


def _set_equation(tree: dict, name: Optional[str], expr: Any) -> bool:
    if not isinstance(name, str) or expr is None:
        return False
    for g in tree.get("equations", {}).get("globals", []):
        if g.get("name") == name:
            g["expr"] = str(expr)
            return True
    return False


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def _sidecar_dir() -> Path:
    """Telemetry sidecar dir resolved WITHOUT services.paths (whose Linux XDG branch is a known
    gap in this env) so LoggingProvider construction never crashes at the IP-18 call site."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "Chudware" / "telemetry" / "sidecars" / "regen_fix"


SYSTEM_PROMPT = (
    "You are a CAD regeneration-repair assistant. A parametric document failed to rebuild. "
    "You are given the structural failure(s) and the feature tree. Propose the MINIMAL set of "
    "document edits that make it rebuild while preserving the designer's intent — never change "
    "geometry that the failure did not touch. Respond with ONLY a JSON object "
    '{"diagnosis": "<one sentence>", "fix_ops": [ ... ]}. Each fix op is one of:\n'
    '  {"op":"repoint_ref","feature":"<fid>","from":"<missing_id>","to":"<valid_fid>"}\n'
    '  {"op":"set_selector_kind","feature":"<fid>","old_kind":"<bad>","kind":"<valid>"}\n'
    '  {"op":"unsuppress","feature":"<fid>"}\n'
    '  {"op":"set_param","feature":"<fid>","param":"<name>","value":<number>}\n'
    '  {"op":"set_equation","global":"<name>","expr":"<value-or-expression>"}\n'
    "Use only feature ids and selector kinds that appear in the provided vocabulary. "
    "Do not invent features. Prefer the fewest ops."
)


def build_diagnosis_prompt(document: dict, failures: list[Failure],
                           engine_error: Optional[str] = None) -> str:
    tree = _tree(document)
    feature_summary = [
        {"id": f.get("id"), "name": f.get("name"), "type": f.get("type"),
         "suppressed": bool(f.get("suppressed")),
         "params": f.get("params", {}), "refs": f.get("refs", {})}
        for f in tree.get("features", [])
    ]
    payload = {
        "failures": [f.to_dict() for f in failures],
        "features": feature_summary,
        "globals": [{"name": g["name"], "expr": g.get("expr")} for g in _globals(document)],
        "valid_selector_kinds": list(VALID_SELECTOR_KINDS),
    }
    if engine_error:
        payload["engine_error"] = engine_error
    return "Rebuild failed. Repair it.\n" + json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# The diagnosis call (frontier, logged through LoggingProvider)
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    diagnosis: str
    fix_ops: list[dict]
    fixed_document: dict
    n_applied: int
    failures: list[dict]
    raw_reply: str = ""
    artifact_id: Optional[str] = None   # the IP-18 ai_artifact this fix belongs to (for action
    run_id: Optional[str] = None        # events to correlate against; from the logged emit)
    council_record: Optional[dict] = None  # fix-stage council record (None if skipped)


def _parse_reply(reply: str) -> tuple[str, list[dict]]:
    """Extract {diagnosis, fix_ops} from a model reply, tolerating prose/fences around the JSON."""
    text = reply.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return "", []
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return "", []
    ops = obj.get("fix_ops", [])
    return (obj.get("diagnosis", "") or ""), (ops if isinstance(ops, list) else [])


def diagnose(document: dict, provider: Provider, *,
             engine_error: Optional[str] = None,
             insertion_point: str = "IP-18",
             on_event: Optional[Callable[[dict], None]] = None,
             consent_fn: Optional[Callable[[], str]] = None,
             run_id: Optional[str] = None,
             wrap_logging: bool = True,
             max_tokens: int = 2000,
             council_tier: Optional[str] = None,
             council_providers: Optional[list] = None,
             ledger: Optional[Any] = None,
             registry: Optional[Any] = None) -> Diagnosis:
    """Diagnose a failed-rebuild ``document`` and return a candidate fix.

    ``provider`` is an explicitly bound Provider: the tool-free compiled-runtime adapter
    online, or an offline-safe provider offline. Unless ``wrap_logging`` is False,
    LoggingProvider records the IP-18 correction trace without owning model requests.

    When ``council_tier`` is provided, a council is convened at the ``fix`` stage
    after diagnosis to deliberate on the proposed fix_ops — giving multi-angle validation
    before the fix is applied.  The council policy (breadth / diverse-model selection) is
    resolved from ``council_policy.yaml`` for (stage="fix", tier=council_tier).
    When the policy is disabled for this tier (e.g. frontier minimal dial), the council
    call is skipped silently.  Council results are attached to the returned Diagnosis as
    ``council_record`` (None if skipped).
    """
    failures = diagnostic_failures(document)
    prompt = build_diagnosis_prompt(document, failures, engine_error=engine_error)

    # Capture the logged IP-18 ai_artifact's id/run_id so a downstream accept/reject action
    # (regen_card.emit_action) can correlate against it in the exporter.
    captured: dict = {"artifact_id": None, "run_id": run_id}

    def _sink(ev: dict) -> None:
        if ev.get("kind") == "ai_artifact" and captured["artifact_id"] is None:
            captured["artifact_id"] = ev.get("artifact_id")
            captured["run_id"] = ev.get("run_id")
        if on_event is not None:
            on_event(ev)

    call_provider = provider
    if wrap_logging:
        try:
            from .providers.logging_provider import LoggingProvider
            call_provider = LoggingProvider(
                provider, on_event=_sink, insertion_point=insertion_point,
                suppress_emit=False, consent_fn=consent_fn, run_id=run_id,
                sidecar_dir=_sidecar_dir(),
            )
        except Exception:  # logging must never break the diagnosis
            call_provider = provider

    reply = call_provider.complete(SYSTEM_PROMPT, [user([text_part(prompt)])], max_tokens=max_tokens)
    diagnosis_text, fix_ops = _parse_reply(reply)
    fixed_doc, n_applied = apply_fix_ops(document, fix_ops)

    # Optional fix-stage council — deliberates on proposed fix_ops.
    council_record: Optional[dict] = None
    if council_tier is not None:
        council_record = _maybe_convene_fix_council(
            diagnosis_text=diagnosis_text,
            fix_ops=fix_ops,
            failures=[f.to_dict() for f in failures],
            provider=provider,
            tier=council_tier,
            providers=council_providers,
            ledger=ledger,
            registry=registry,
        )

    return Diagnosis(
        diagnosis=diagnosis_text, fix_ops=fix_ops, fixed_document=fixed_doc,
        n_applied=n_applied, failures=[f.to_dict() for f in failures], raw_reply=reply,
        artifact_id=captured["artifact_id"], run_id=captured["run_id"],
        council_record=council_record,
    )


def _maybe_convene_fix_council(
    *,
    diagnosis_text: str,
    fix_ops: list[dict],
    failures: list[dict],
    provider: Provider,
    tier: str,
    providers: Optional[list] = None,
    ledger: Optional[Any] = None,
    registry: Optional[Any] = None,
) -> Optional[dict]:
    """Convene a fix-stage council when the policy is enabled for this tier.

    Returns None only when policy disables the council. Once execution starts,
    failures return an explicit incomplete record and are logged with traceback.
    """
    try:
        from .councils import council_convene, resolve_policy
        policy = resolve_policy("fix", tier)
        if not policy.enabled:
            return None

        topic = (
            f"Error diagnosis review: {len(failures)} failure(s) detected. "
            f"Proposed fix: {fix_ops[0].get('op', 'unknown') if fix_ops else 'none'}. "
            "Deliberate on whether the proposed fix_ops are correct and complete."
        )
        inputs: dict = {
            "diagnosis": diagnosis_text[:800],   # cap at 800 chars to fit all tiers
            "fix_ops": fix_ops[:5],              # first 5 ops; enough context for seats
            "failures": failures[:3],            # first 3 failures
        }
        return council_convene(
            kind="failure_review",
            topic=topic,
            inputs=inputs,
            provider=provider,
            ledger=ledger,
            registry=registry,
            providers=providers,
            stage="fix",
            tier=tier,
        )
    except Exception as exc:  # noqa: BLE001 — diagnosis survives, failure stays visible
        _LOG.exception("Fix-stage council failed after execution began")
        partial = getattr(exc, "council_record", None)
        if not isinstance(partial, dict):
            partial = {
                "schema": "aicad.council/1",
                "id": f"cn_{uuid.uuid4().hex[:10]}",
                "kind": "failure_review",
                "topic": "Error diagnosis review",
                "inputs": {
                    "diagnosis": diagnosis_text[:800],
                    "fix_ops": fix_ops[:5],
                    "failures": failures[:3],
                },
                "seats": [],
                "rounds": 2,
                "positions": [],
                "synthesis": {
                    "decision": "",
                    "rationale": "",
                    "rejected_alternatives": [],
                },
                "dissents": [],
                "chair": "department_manager",
                "seat_outcomes": [],
                "diagnostics": {},
            }
        partial["status"] = "incomplete"
        partial["complete"] = False
        partial.setdefault("seat_outcomes", [])
        partial.setdefault("diagnostics", {})["council_exception"] = {
            "status": "failed",
            "outcome_kind": "internal_error",
            "executed": False,
            "error": str(exc),
        }
        if ledger is not None:
            try:
                ledger.append("council", "artifact", {
                    "id": partial["id"],
                    "kind": "council",
                    "council": partial,
                })
            except Exception as persist_exc:  # noqa: BLE001 — keep diagnosis alive
                _LOG.exception("Could not persist incomplete fix-stage council")
                partial["diagnostics"]["persistence_error"] = {
                    "status": "failed",
                    "outcome_kind": "internal_error",
                    "executed": False,
                    "error": str(persist_exc),
                }
        return partial
