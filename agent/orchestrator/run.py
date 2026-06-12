"""``run_design`` — wire the machine + roles + ops application to a LIVE document session.

This is the engine-half entry point the worker's ``agent_run_v2`` calls. It builds a
:class:`~agent.orchestrator.state.DesignState` from the open ``engine.doc.session.DocSession`,
constructs the per-stage :class:`~agent.orchestrator.machine.StageActions` (each runs a provider
call over a FRESH bounded context, parses the reply, and applies the effect to the session), runs
the machine, and returns a :class:`RunResult`.

Ops application path (doc_ops.md): the modeler/fixer emit a JSON ops array → dry-run validate
against the live doc → ``session.apply_ops(base_revision)`` transactional apply. On invalid ops,
the validator error is fed back verbatim for a bounded retry; after 2 parse/validate failures the
ESCAPE HATCH fires — the modeler is asked for a build123d script and we emit one
``add_feature{type:"script"}`` op (the proven floor).

REVIEW renders the doc's last-good bodies via ``engine.render.render_grid`` over the session's AICM/
STL export and feeds the image only to vision-capable providers; otherwise it reviews stats-only
(the legacy loop's degraded mode).

MEMORY: the memory role's output is stored on the session as a duck-typed attribute
(``session.ai_memory``) so a stepplus save can embed ``ai/memory.json`` later, AND written to
``cache/runs/<run_id>/memory.json`` so it survives regardless. (We deliberately do NOT edit
``engine/doc/session.py`` — C20 owns it this wave.)
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..providers.base import Provider
from . import ops_emit, roles
from .events import RunEvents, run_dir
from .machine import RunControl, StageActions, StageOutcome, run_machine
from .state import DesignState, build_context, digest_tree, feature_status_errors

EmitFn = Callable[[dict], None]

# How many parse/validate failures before the modeler escape hatch (script feature) fires.
_ESCAPE_AFTER = 2


@dataclass
class RunResult:
    run_id: str
    status: str            # "done" | "failed" | "cancelled"
    stop_reason: str
    revision: int
    doc_id: str = ""


# --------------------------------------------------------------------------- memory persistence


def _memory_path(run_id: str) -> Path:
    return run_dir(run_id) / "memory.json"


def _load_memory(session) -> dict:
    """Load prior memory: a duck-typed ``session.ai_memory`` first, then any preserved
    ``ai/memory.json`` extra-entry on the open .step+ doc."""
    mem = getattr(session, "ai_memory", None)
    if isinstance(mem, dict) and mem:
        return mem
    try:
        raw = (getattr(session, "extra_entries", {}) or {}).get("ai/memory.json")
        if raw:
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _store_memory(session, run_id: str, mem: dict) -> None:
    """Persist memory both on the session (duck-typed, for a later stepplus embed) and to disk."""
    try:
        setattr(session, "ai_memory", mem)
        # Also stage it as the ai/memory.json extra-entry so a doc_save embeds it (stepplus passes
        # unknown entries through verbatim). This does NOT require editing session.py.
        ee = getattr(session, "extra_entries", None)
        if isinstance(ee, dict):
            ee["ai/memory.json"] = json.dumps(mem, indent=2).encode("utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        _memory_path(run_id).write_text(json.dumps(mem, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- json helpers


def _extract_json_obj(reply: str) -> Optional[dict]:
    """Parse the first fenced ```json object (or a bare top-level ``{...}``) into a dict."""
    import re
    for m in re.finditer(r"```(?:json|JSON)?\s*\n(.*?)```", reply or "", re.DOTALL):
        try:
            d = json.loads(m.group(1).strip())
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            continue
    blob = ops_emit._first_balanced(reply or "", "{", "}")
    if blob:
        try:
            d = json.loads(blob)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            pass
    return None


def _inputs_digest(state: DesignState) -> str:
    return f"rev{state.revision} feats={len(state.tree_digest.splitlines())}"


# --------------------------------------------------------------------------- the runner


def run_design(request: str, doc_session, provider: Provider,
               emit: Optional[EmitFn] = None, options: Optional[dict] = None) -> RunResult:
    """Run the deterministic orchestrator against a live ``doc_session``. Returns a RunResult.

    ``options`` (all optional): ``budgets`` (per-stage overrides), ``control`` (a RunControl for
    cooperative cancel), ``run_id`` (override the generated id).
    """
    options = options or {}
    run_id = options.get("run_id") or uuid.uuid4().hex[:12]
    budgets = options.get("budgets")
    control: RunControl = options.get("control") or RunControl()

    # OFFLINE MODE defense-in-depth (C35): refuse to run if the resolved provider is not
    # local-only while offline mode is set. Raises OfflineModeError naming the setting.
    from .. import config as agent_config
    agent_config.assert_offline_safe(provider)

    events = RunEvents(run_id, emit)

    state = DesignState(request=request,
                        doc_id=getattr(doc_session, "doc_id", ""),
                        revision=getattr(doc_session, "revision", 0))
    state.memory = _load_memory(doc_session)
    state.update_tree(getattr(doc_session, "doc", {}) or {})

    stages = ["intake", "plan", "model", "review", "fix", "memory"]
    events.emit("run_started", request=request, doc_id=state.doc_id, stages=stages)

    actions = _build_actions(doc_session, provider, state, run_id)
    result = run_machine(state, actions, events, budgets=budgets, control=control)

    revision = getattr(doc_session, "revision", state.revision)
    events.emit("run_finished", status=result["status"],
                stop_reason=result["stop_reason"], revision=revision)

    return RunResult(run_id=run_id, status=result["status"],
                     stop_reason=result["stop_reason"], revision=revision,
                     doc_id=state.doc_id)


def _agent_call(provider: Provider, state: DesignState, role: str) -> tuple[str, int, dict]:
    """Run one provider call for *role* over a fresh bounded context.

    Returns ``(reply, duration_ms, meta)`` where meta carries the event fields (prompt_file/hash,
    context sizes, inputs_digest).
    """
    system, messages, chars, tok = build_context(state, role)
    # Deterministic role marker: providers (and the test mock's router) read the role from
    # this first line instead of sniffing prompt prose. Harmless context for real models.
    system = f"ROLE: {role}\n{system}"
    _text, source = roles.load_prompt(role)
    meta = {
        "prompt_file": source,
        "prompt_hash": roles.prompt_hash(system),
        "context_chars": chars,
        "context_tokens_est": tok,
        "inputs_digest": _inputs_digest(state),
    }
    t0 = time.perf_counter()
    try:
        reply, _thinking = provider.complete_ex(system, messages, max_tokens=2048)
    except Exception as e:  # noqa: BLE001
        reply = ""
        meta["call_error"] = f"{type(e).__name__}: {e}"
    dur = int((time.perf_counter() - t0) * 1000)
    return reply or "", dur, meta


def _build_actions(session, provider: Provider, state: DesignState, run_id: str) -> StageActions:
    from engine.doc import session as docsess

    # ---- INTAKE ----
    def intake(st: DesignState, attempt: int) -> StageOutcome:
        reply, dur, meta = _agent_call(provider, st, "intake")
        spec = _extract_json_obj(reply)
        if spec is not None:
            st.spec = spec
            return StageOutcome(verdict="pass", reason="spec parsed", raw=reply,
                                parsed_ok=True, duration_ms=dur, **meta)
        return StageOutcome(verdict="retry", reason="spec not parseable", raw=reply,
                            parsed_ok=False, parse_error="no spec json",
                            error="intake: no spec json", duration_ms=dur, **meta)

    # ---- PLAN ----
    def plan(st: DesignState, attempt: int) -> StageOutcome:
        reply, dur, meta = _agent_call(provider, st, "planner")
        p = _extract_json_obj(reply)
        if p is not None and isinstance(p.get("parts"), list) and p["parts"]:
            st.plan = p
            return StageOutcome(verdict="pass", reason="plan parsed", raw=reply,
                                parsed_ok=True, duration_ms=dur, **meta)
        # Plan is optional: a failure DEGRADES to single-step MODEL (skip, not fail).
        return StageOutcome(verdict="retry", reason="plan not parseable", raw=reply,
                            parsed_ok=False, parse_error="no plan json",
                            duration_ms=dur, **meta)

    # ---- MODEL (with dry-run + bounded retry + script escape hatch) ----
    def model(st: DesignState, attempt: int) -> StageOutcome:
        st._last_no_progress = False  # type: ignore[attr-defined]
        # On the 3rd+ attempt (>= _ESCAPE_AFTER prior failures), ask for the escape-hatch script.
        if attempt > _ESCAPE_AFTER:
            return _model_escape(st)
        reply, dur, meta = _agent_call(provider, st, "modeler")
        ops, perr = ops_emit.extract_ops(reply)
        if ops is None:
            return StageOutcome(verdict="retry", reason=f"no ops: {perr}", raw=reply,
                                parsed_ok=False, parse_error=perr,
                                error=f"model: {perr}", duration_ms=dur, **meta)
        if not ops:
            # Empty array = nothing to add this step. Valid + no progress.
            st._last_no_progress = True  # type: ignore[attr-defined]
            return StageOutcome(verdict="pass", reason="no ops needed for step", raw=reply,
                                parsed_ok=True, ops_count=0, no_progress=True,
                                duration_ms=dur, **meta)
        ok, verr = ops_emit.dry_run(session.doc, ops)
        if not ok:
            # Feed the validator error back verbatim so the next attempt can correct it.
            st.last_errors = [f"invalid ops: {verr}"]
            return StageOutcome(verdict="retry", reason="ops failed validation", raw=reply,
                                parsed_ok=True, ops_count=len(ops),
                                error=f"model-validate: {verr}", duration_ms=dur, **meta)
        applied = _apply(st, ops)
        st.last_errors = applied["errors"]
        return StageOutcome(verdict="pass", reason=f"applied {len(ops)} ops", raw=reply,
                            parsed_ok=True, ops_count=len(ops), duration_ms=dur, **meta)

    def _model_escape(st: DesignState) -> StageOutcome:
        """Escape hatch: ask the modeler for a build123d script, wrap it in a script feature."""
        st.current_step = (st.current_step + "\n\nThe op attempts failed. Instead, output a "
                           "build123d script (assign `result`) in a ```python block that builds "
                           "this part.").strip()
        reply, dur, meta = _agent_call(provider, st, "modeler")
        code = ops_emit.extract_script(reply)
        if not code:
            return StageOutcome(verdict="fail", reason="escape hatch: no script", raw=reply,
                                parsed_ok=False, parse_error="no script",
                                error="model-escape: no script", duration_ms=dur, **meta)
        op = ops_emit.script_escape_op(code, name=(st.spec or {}).get("object", "Script") or "Script")
        ok, verr = ops_emit.dry_run(session.doc, [op])
        if not ok:
            return StageOutcome(verdict="fail", reason=f"escape script invalid: {verr}",
                                raw=reply, parsed_ok=True, ops_count=1,
                                error=f"model-escape-validate: {verr}", duration_ms=dur, **meta)
        applied = _apply(st, [op])
        st.last_errors = applied["errors"]
        return StageOutcome(verdict="pass", reason="escape hatch: script feature applied",
                            raw=reply, parsed_ok=True, ops_count=1, duration_ms=dur, **meta)

    # ---- REVIEW (vision over a render, else stats-only) ----
    def review(st: DesignState, attempt: int) -> StageOutcome:
        render_path = _render_review(session)
        st.last_render = render_path
        system, messages, chars, tok = build_context(st, "reviewer")
        _t, source = roles.load_prompt("reviewer")
        meta = {"prompt_file": source, "prompt_hash": roles.prompt_hash(system),
                "context_chars": chars, "context_tokens_est": tok,
                "inputs_digest": _inputs_digest(st)}
        # Attach the render only for vision-capable providers (legacy loop pattern).
        if render_path and getattr(provider, "supports_vision", True) and Path(render_path).exists():
            from ..providers.base import image_part
            messages[-1]["content"].append(image_part(render_path))
        t0 = time.perf_counter()
        try:
            reply, _thinking = provider.complete_ex(system, messages, max_tokens=1024)
        except Exception as e:  # noqa: BLE001
            reply = ""
            meta["call_error"] = f"{type(e).__name__}: {e}"
        dur = int((time.perf_counter() - t0) * 1000)
        verdict_obj = _extract_json_obj(reply)
        if verdict_obj is None:
            return StageOutcome(verdict="retry", reason="review verdict not parseable",
                                raw=reply, parsed_ok=False, parse_error="no verdict json",
                                duration_ms=dur, **meta)
        done = bool(verdict_obj.get("done"))
        issues = [i for i in (verdict_obj.get("issues") or []) if i]
        st.last_issues = issues
        if done or not issues:
            return StageOutcome(verdict="pass", reason="review: done", raw=reply,
                                parsed_ok=True, done=True, duration_ms=dur, **meta)
        return StageOutcome(verdict="retry", reason=f"review: {len(issues)} issue(s)",
                            raw=reply, parsed_ok=True, done=False, duration_ms=dur, **meta)

    # ---- FIX (minimal ops patch OR revert) ----
    def fix(st: DesignState, attempt: int) -> StageOutcome:
        reply, dur, meta = _agent_call(provider, st, "fixer")
        if ops_emit.extract_revert(reply):
            try:
                resp = docsess.undo(session)
                st.update_revision(resp["revision"])
                st.update_tree(resp["tree"])
                return StageOutcome(verdict="pass", reason="fix: reverted last change",
                                    raw=reply, parsed_ok=True, duration_ms=dur, **meta)
            except Exception as e:  # noqa: BLE001
                return StageOutcome(verdict="fail", reason=f"revert failed: {e}",
                                    raw=reply, error=f"fix-revert: {e}", duration_ms=dur, **meta)
        ops, perr = ops_emit.extract_ops(reply)
        if ops is None:
            return StageOutcome(verdict="retry", reason=f"fix: no ops ({perr})", raw=reply,
                                parsed_ok=False, parse_error=perr,
                                error=f"fix: {perr}", duration_ms=dur, **meta)
        if not ops:
            return StageOutcome(verdict="pass", reason="fix: no change", raw=reply,
                                parsed_ok=True, ops_count=0, duration_ms=dur, **meta)
        ok, verr = ops_emit.dry_run(session.doc, ops)
        if not ok:
            st.last_errors = [f"invalid fix ops: {verr}"]
            return StageOutcome(verdict="retry", reason="fix ops failed validation", raw=reply,
                                parsed_ok=True, ops_count=len(ops),
                                error=f"fix-validate: {verr}", duration_ms=dur, **meta)
        applied = _apply(st, ops)
        st.last_errors = applied["errors"]
        st.last_issues = []
        return StageOutcome(verdict="pass", reason=f"fix: applied {len(ops)} ops", raw=reply,
                            parsed_ok=True, ops_count=len(ops), duration_ms=dur, **meta)

    # ---- MEMORY ----
    def memory(st: DesignState, attempt: int) -> StageOutcome:
        reply, dur, meta = _agent_call(provider, st, "memory")
        mem = _extract_json_obj(reply)
        if mem is None:
            return StageOutcome(verdict="pass", reason="memory: nothing recorded", raw=reply,
                                parsed_ok=False, parse_error="no memory json",
                                duration_ms=dur, **meta)
        _store_memory(session, run_id, mem)
        st.memory = mem
        return StageOutcome(verdict="pass", reason="memory recorded", raw=reply,
                            parsed_ok=True, duration_ms=dur, **meta)

    # ---- plan_steps: one modeler step per plan part (or a single step if unplanned) ----
    def plan_steps(st: DesignState) -> list:
        if not st.plan or not st.plan.get("parts"):
            return [_default_step(st)]
        steps: list[str] = []
        for p in st.plan["parts"]:
            name = p.get("name", "part")
            purpose = p.get("purpose", "")
            dims = p.get("dimensions", {})
            feats = p.get("features", []) or []
            dim_s = ", ".join(f"{k}={v}" for k, v in dims.items()) if isinstance(dims, dict) else str(dims)
            feat_s = "; ".join(str(f) for f in feats)
            steps.append(f"Build part '{name}' ({purpose}). Dimensions: {dim_s}. "
                         f"Features: {feat_s}".strip())
        return steps

    def _apply(st: DesignState, ops: list) -> dict:
        resp = docsess.apply_ops(session, st.revision, ops)
        st.update_revision(resp["revision"])
        st.update_tree(resp["tree"])
        errs = feature_status_errors(resp.get("rebuild", {}).get("feature_status", []))
        return {"errors": errs, "resp": resp}

    return StageActions(intake=intake, plan=plan, model=model, review=review,
                        fix=fix, memory=memory, plan_steps=plan_steps)


def _default_step(state: DesignState) -> str:
    obj = (state.spec or {}).get("object") if state.spec else None
    return f"Build the requested object: {obj or state.request}"


# --------------------------------------------------------------------------- review render


def _render_review(session) -> Optional[str]:
    """Render the doc's last-good bodies to a contact sheet for the reviewer (best-effort).

    Exports the session's bodies to STL via the kernel and runs ``engine.render.render_grid``. Any
    failure (no bodies, no pyvista) just means stats-only review — never breaks the run.
    """
    try:
        from engine import render
        from engine.doc import session as docsess
        result = docsess._ensure_built(session)
        bodies = list(result.bodies)
        if not bodies:
            return None
        out_dir = run_dir(f"review_{getattr(session, 'doc_id', 'doc')}")
        stl = out_dir / "review.stl"
        from build123d import Compound, export_stl
        shape = bodies[0].shape if len(bodies) == 1 else Compound(children=[b.shape for b in bodies])
        export_stl(shape, str(stl))
        return render.render_grid(str(stl), out_dir / "review_grid.png")
    except Exception:  # noqa: BLE001
        return None
