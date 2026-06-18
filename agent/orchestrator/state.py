"""DesignState — the shared, compact source every agent call assembles its FRESH context from.

Compaction by construction: instead of accumulating a growing conversation (the legacy loop's
model), each stage builds a bounded message list from this state. ``build_context(state, role)``
returns ``(system_text, messages, context_chars, context_tokens_est)`` where the estimated token
count (len/4, the same heuristic the events contract uses) is kept under :data:`CONTEXT_TOKEN_CAP`
so 4-8k-context local models never overflow. The assembler trims the lowest-priority pieces
(memory, then plan detail, then the tree digest) until it fits.

The state holds only digests, never raw model transcripts: the request, the normalized spec, the
plan, the live doc id + revision, a one-line-per-feature tree digest, the last render path, the
last errors/issues, a per-stage attempt ledger, and loaded memory.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..providers.base import text_part, user
from . import roles as _roles

# Per-call context budget (PLAN §5-M9: <=6k est tokens). Estimate = chars / 4.
CONTEXT_TOKEN_CAP = 6000
_CHARS_PER_TOKEN = 4


def est_tokens(text: str) -> int:
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


@dataclass
class AttemptLedger:
    """Per-stage attempt counts + the running error/issue signatures (for identical-failure stop)."""
    attempts: dict[str, int] = field(default_factory=dict)
    last_sigs: dict[str, str] = field(default_factory=dict)
    sig_streak: dict[str, int] = field(default_factory=dict)

    def bump(self, stage: str) -> int:
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        return self.attempts[stage]

    def count(self, stage: str) -> int:
        return self.attempts.get(stage, 0)

    def note_sig(self, stage: str, sig: str) -> int:
        """Record an error signature for *stage*; return the consecutive-identical streak."""
        if sig and sig == self.last_sigs.get(stage):
            self.sig_streak[stage] = self.sig_streak.get(stage, 0) + 1
        else:
            self.sig_streak[stage] = 1
        self.last_sigs[stage] = sig
        return self.sig_streak[stage]


@dataclass
class DesignState:
    request: str
    doc_id: str = ""
    revision: int = 0
    spec: Optional[dict] = None
    plan: Optional[dict] = None
    tree_digest: str = ""                 # one line per feature: "f1 box Box1 [ok]"
    last_render: Optional[str] = None
    last_errors: list[str] = field(default_factory=list)
    last_issues: list[str] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    ledger: AttemptLedger = field(default_factory=AttemptLedger)
    # Carried for the current MODEL step (set by the machine before assembling a modeler context).
    current_step: str = ""

    def update_tree(self, tree: dict) -> None:
        self.tree_digest = digest_tree(tree)

    def update_revision(self, revision: int) -> None:
        self.revision = revision


# --------------------------------------------------------------------------- digests


def _digest_params(params: dict, limit: int = 6) -> str:
    """A short ``k=v`` summary of a feature's key params (dim objects + plain scalars)."""
    out: list[str] = []
    for k, v in list((params or {}).items())[:limit]:
        if isinstance(v, dict) and "value" in v:
            out.append(f"{k}={v['value']}")
        elif isinstance(v, (int, float)):
            out.append(f"{k}={v}")
    return ", ".join(out)


def digest_tree(tree: dict, max_lines: int = 40) -> str:
    """One compact line per feature: ``<id> <type> <name> (key params)`` (+ a [supp] marker).

    Key params are included (Wave D) so an edit-shaped request can be steered to ``edit_feature``
    on the right feature/param instead of duplicating geometry with ``add_feature``.
    """
    feats = (tree or {}).get("features", []) or []
    lines: list[str] = []
    for f in feats[:max_lines]:
        if not isinstance(f, dict):
            continue
        sup = " [supp]" if f.get("suppressed") else ""
        line = f"{f.get('id', '?')} {f.get('type', '?')} {f.get('name', '')}{sup}".rstrip()
        dims = _digest_params(f.get("params") or {})
        if dims:
            line += f" ({dims})"
        lines.append(line)
    if len(feats) > max_lines:
        lines.append(f"... (+{len(feats) - max_lines} more features)")
    return "\n".join(lines) if lines else "(empty — no features yet)"


# ----------------------------------------------------------------- edit bias (Wave D)
# When the document ALREADY HAS features and the request reads like a modification, the
# ops-emitting roles get an explicit steer: edit_feature on the existing feature, not a new
# add_feature duplicate. The heuristic is a cheap word list — a false positive only adds one
# short instruction block; a false negative just means no extra steer.

_EDIT_WORDS = (
    "make ", "change", "set ", "resize", "increase", "decrease", "widen", "narrow",
    "shorten", "lengthen", "thicker", "thinner", "wider", "taller", "shorter", "deeper",
    "shallower", "bigger", "smaller", "larger", "edit", "modify", "update", "adjust",
    "rename", "instead", "should be", "reduce", "enlarge", "scale", "move ",
    "remove", "delete",
)

EDIT_BIAS_TEXT = (
    "EDIT BIAS: the document ALREADY HAS the features listed above (ids, names and current "
    "params shown). This request modifies something that exists — PREFER `edit_feature` on the "
    "existing feature's id over `add_feature`. Example: \"make the plate 60mm wide\" => "
    '[{"op":"edit_feature","id":"<the plate feature id>","params":{"width":{"value":60,'
    '"expr":null}}}] — do NOT add a second plate. Use `delete_feature` with the feature id for '
    "removals. Only use add_feature for genuinely NEW geometry."
)


def looks_like_edit(request: str) -> bool:
    """True when *request* reads like a modification of existing geometry (cheap word list)."""
    r = f" {(request or '').lower()} "
    return any(w in r for w in _EDIT_WORDS)


def _has_features(state: "DesignState") -> bool:
    return bool(state.tree_digest) and not state.tree_digest.startswith("(empty")


def feature_status_errors(feature_status: list[dict]) -> list[str]:
    """Extract human-readable errors from a rebuild's feature_status list."""
    out: list[str] = []
    for s in feature_status or []:
        if not s.get("ok") and s.get("error"):
            out.append(f"{s.get('id', '?')}: {s['error']}")
    return out


def _memory_text(memory: dict) -> str:
    if not memory:
        return ""
    parts: list[str] = []
    decisions = memory.get("decisions") or []
    if decisions:
        parts.append("Prior decisions: " + "; ".join(str(d) for d in decisions[:8]))
    issues = memory.get("open_issues") or []
    issues = [i for i in issues if i]
    if issues:
        parts.append("Known open issues: " + "; ".join(str(i) for i in issues[:4]))
    summary = memory.get("summary")
    if summary:
        parts.insert(0, f"Memory: {summary}")
    return "\n".join(parts)


def _spec_text(spec: dict) -> str:
    return "SPEC:\n" + json.dumps(spec, separators=(",", ": "), indent=0)


def _plan_text(plan: dict, compact: bool = False) -> str:
    if compact:
        names = ", ".join(p.get("name", "?") for p in plan.get("parts", []))
        return f"PLAN: {plan.get('name', '')} — parts: {names}"
    return "PLAN:\n" + json.dumps(plan, separators=(",", ": "), indent=0)


# --------------------------------------------------------------------------- context assembly


def build_context(state: DesignState, role: str) -> tuple[str, list[dict], int, int]:
    """Assemble the FRESH bounded context for *role*.

    Returns ``(system_text, messages, context_chars, context_tokens_est)``. The system text is the
    role's resolved prompt (override-aware, cheatsheet-appended for ops roles). The single user
    message carries only the digests this role needs; lower-priority pieces are dropped until the
    total estimate is under :data:`CONTEXT_TOKEN_CAP`.
    """
    system, _src = _roles.load_prompt(role)

    # Priority-ordered blocks (most important last so trimming drops the cheap context first).
    # Each block is (key, text); we keep the request + role-essential blocks, shedding the rest.
    blocks = _role_blocks(state, role)

    # Trim from the FRONT of the optional list (memory/plan-detail) until system+user fits the cap.
    sys_tokens = est_tokens(system)
    while blocks:
        body = "\n\n".join(t for _k, t in blocks if t)
        total = sys_tokens + est_tokens(body)
        if total <= CONTEXT_TOKEN_CAP:
            break
        # Drop the lowest-priority present block (first entry flagged optional).
        opt_idx = next((i for i, (k, _t) in enumerate(blocks) if k.startswith("opt:")), None)
        if opt_idx is None:
            # Only essential blocks left and still over cap → hard-truncate the largest essential.
            largest = max(range(len(blocks)), key=lambda i: len(blocks[i][1]))
            k, t = blocks[largest]
            keep = max(200, (CONTEXT_TOKEN_CAP - sys_tokens) * _CHARS_PER_TOKEN - 200)
            blocks[largest] = (k, t[:keep] + "\n...[truncated]")
            body = "\n\n".join(t for _k, t in blocks if t)
            break
        blocks.pop(opt_idx)

    body = "\n\n".join(t for _k, t in blocks if t)
    messages = [user([text_part(body)])]
    chars = len(system) + len(body)
    return system, messages, chars, est_tokens(system) + est_tokens(body)


def _role_blocks(state: DesignState, role: str) -> list[tuple[str, str]]:
    """Role-specific ordered context blocks. ``opt:`` prefix marks droppable-under-pressure."""
    req = f"REQUEST:\n{state.request}"
    mem = _memory_text(state.memory)
    tree = f"CURRENT FEATURE TREE:\n{state.tree_digest}" if state.tree_digest else ""
    errs = ("ERRORS:\n" + "\n".join(state.last_errors)) if state.last_errors else ""
    issues = ("REVIEW ISSUES:\n" + "\n".join(state.last_issues)) if state.last_issues else ""
    # Wave D: explicit edit_feature steer for the ops-emitting roles on edit-shaped requests.
    bias = EDIT_BIAS_TEXT if _has_features(state) and looks_like_edit(state.request) else ""

    if role == "intake":
        blocks = [("opt:mem", mem), ("req", req)]
    elif role == "planner":
        spec = _spec_text(state.spec) if state.spec else ""
        blocks = [("opt:mem", mem), ("req", req), ("spec", spec)]
    elif role == "modeler":
        plan = _plan_text(state.plan) if state.plan else ""
        step = f"STEP TO IMPLEMENT NOW:\n{state.current_step}" if state.current_step else ""
        blocks = [("opt:mem", mem), ("opt:plan", plan), ("req", req),
                  ("tree", tree), ("bias", bias), ("step", step), ("errors", errs)]
    elif role == "reviewer":
        plan = _plan_text(state.plan, compact=True) if state.plan else ""
        blocks = [("opt:mem", mem), ("opt:plan", plan), ("req", req), ("tree", tree)]
    elif role == "fixer":
        plan = _plan_text(state.plan, compact=True) if state.plan else ""
        prob = errs or issues
        blocks = [("opt:mem", mem), ("opt:plan", plan), ("req", req),
                  ("tree", tree), ("bias", bias), ("problem", prob)]
    elif role == "memory":
        plan = _plan_text(state.plan, compact=True) if state.plan else ""
        blocks = [("opt:plan", plan), ("req", req), ("tree", tree)]
    else:  # pragma: no cover - guarded by roles.ROLES
        blocks = [("req", req)]
    return [(k, t) for k, t in blocks if t]
