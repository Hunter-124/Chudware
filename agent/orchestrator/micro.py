"""C35 MICRO-ORCHESTRATION PROFILE — a tiny-context path for ultra-quantized local models.

The standard orchestrator (machine.py + run.py) assumes a model that can hold a ~6k-token role
context and emit a multi-feature ops batch. The configured default ships an ultra-quantized local
model (gemma-4-e4b, 4k-8k context, weak instruction following). This profile trades the big single
modeler call for a chain of MINUSCULE, fully self-contained calls:

  DECOMPOSER  (one tiny call)  -> ordered JSON list of single-feature steps (hard cap 12)
  EXECUTOR    (one call/step)  -> ONE doc-op JSON array for that step, applied immediately;
                                  on an op error, ONE retry with only the error appended
  VERIFIER    (pure python)    -> resolved params vs the step intent (dims, volume direction)

Every prompt is asserted under :data:`EXEC_PROMPT_BUDGET_TOKENS` (the doc summary is truncated
FIRST). Progress streams through the same :class:`~agent.orchestrator.events.RunEvents` used by the
v2 machine, so the Copilot pane shows "step k/N" inline.

Engagement (see :func:`should_engage`): auto when the provider is LOCAL and (config "micro_mode"
is true OR the model context is known-small). Config key ``micro_mode`` default ``"auto"``; set
``true``/``false`` to force on/off.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from ..providers.base import Provider, text_part, user
from . import ops_emit, roles
from .events import RunEvents
from .state import est_tokens

# Per-call budgets (token estimate = chars // 4, matching state.est_tokens / the events contract).
DECOMPOSE_SYSTEM_BUDGET_TOKENS = 400      # the decomposer system prompt must stay <=400 tokens
EXEC_PROMPT_BUDGET_TOKENS = 2000          # total per-executor-call budget (system + user)
DOC_SUMMARY_BUDGET_TOKENS = 300           # the compact doc summary cap (trimmed first)
MAX_STEPS = 12                            # hard cap on decomposed steps


# --------------------------------------------------------------------------- engagement


def should_engage(provider: Provider, cfg: dict | None = None) -> bool:
    """True if the micro profile should drive this run.

    Auto-engages when the provider is LOCAL (loopback OpenAI-compatible / mock) AND either the
    config forces ``micro_mode: true`` OR the model context is known-small. ``micro_mode`` defaults
    to ``"auto"``; ``true``/``false`` force the decision regardless of provider locality.
    """
    cfg = cfg or {}
    mode = cfg.get("micro_mode", "auto")
    if mode is True or (isinstance(mode, str) and mode.lower() == "true"):
        return True
    if mode is False or (isinstance(mode, str) and mode.lower() == "false"):
        return False
    # auto: local provider + (small context). The local OpenAI-compatible/mock providers are the
    # small-model path; cloud providers (anthropic, openai-cloud) use the full orchestrator.
    return _provider_is_local(provider) and _context_is_small(provider, cfg)


def _provider_is_local(provider: Provider) -> bool:
    from .. import config as agent_config

    name = getattr(provider, "name", "")
    if name in ("mock", "mock-roles"):
        return True
    if name == "openai":
        return agent_config._host_is_loopback(getattr(provider, "base_url", None))
    return False


def _context_is_small(provider: Provider, cfg: dict) -> bool:
    """Treat the context as small unless we KNOW it is large.

    Local quantized models default to small. An explicit ``context_tokens`` config > 16000 (or a
    provider attribute) opts out. The default-local gemma-4-e4b is small, so auto engages.
    """
    ctx = cfg.get("context_tokens") or getattr(provider, "context_tokens", None)
    if isinstance(ctx, (int, float)) and ctx > 16000:
        return False
    return True


# --------------------------------------------------------------------------- robust JSON extraction


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def extract_json_array(reply: str) -> Optional[list]:
    """Pull a JSON array from *reply* — fenced ```json block first, then a bare ``[...]``.

    Mirrors loop.py's fence-then-braces pattern (here braces are brackets for an array).
    """
    for m in _FENCE_RE.finditer(reply or ""):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return data["steps"]
    bare = ops_emit._first_balanced(reply or "", "[", "]")
    if bare:
        try:
            data = json.loads(bare)
            if isinstance(data, list):
                return data
        except Exception:  # noqa: BLE001
            pass
    return None


# --------------------------------------------------------------------------- step model


@dataclass
class Step:
    type: str
    desc: str
    params: dict = field(default_factory=dict)


def normalize_steps(raw: Optional[list]) -> list[Step]:
    """Coerce the decomposer's raw list into Step objects, hard-capped at MAX_STEPS."""
    steps: list[Step] = []
    for item in (raw or [])[:MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        ftype = str(item.get("type") or "").strip().lower()
        if not ftype:
            continue
        desc = str(item.get("desc") or item.get("description") or "").strip()
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        steps.append(Step(type=ftype, desc=desc, params=params))
    return steps


# --------------------------------------------------------------------------- compact doc summary


def summarize_doc(tree: dict, last_result=None, budget_tokens: int = DOC_SUMMARY_BUDGET_TOKENS) -> str:
    """A <=``budget_tokens`` compact tree: one line per feature (type, name, key dims) + a total
    volume line when available. Truncated to fit the budget (this is the FIRST thing trimmed)."""
    feats = (tree or {}).get("features", []) or []
    lines: list[str] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        dims = _key_dims(f.get("params") or {})
        sup = " [supp]" if f.get("suppressed") else ""
        line = f"{f.get('id', '?')} {f.get('type', '?')} {f.get('name', '')}{sup}".rstrip()
        if dims:
            line += f" ({dims})"
        lines.append(line)
    body = "\n".join(lines) if lines else "(empty — no features yet)"
    vol = total_volume(last_result)
    head = "CURRENT DOC:"
    if vol is not None:
        head += f" volume={vol:.0f} mm^3"
    text = head + "\n" + body
    # Truncate to the budget (chars), keeping the head + as many feature lines as fit.
    max_chars = budget_tokens * 4
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[doc truncated]"
    return text


def _key_dims(params: dict) -> str:
    """A short 'k=v' summary of a feature's key dimension params (dim objects + scalars)."""
    out: list[str] = []
    for k, v in list(params.items())[:6]:
        if isinstance(v, dict) and "value" in v:
            out.append(f"{k}={v['value']}")
        elif isinstance(v, (int, float, str)) and k in (
                "radius", "length", "width", "height", "depth", "count", "count1",
                "count2", "angle", "thickness", "mode", "end", "style", "standard"):
            out.append(f"{k}={v}")
    return ", ".join(out)


def total_volume(last_result) -> Optional[float]:
    """Total solid volume across the doc's built bodies (None if unavailable)."""
    if last_result is None:
        return None
    try:
        bodies = list(getattr(last_result, "bodies", []) or [])
        if not bodies:
            return None
        return float(sum(b.shape.volume for b in bodies))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- prompt assembly


def build_decompose_prompt() -> tuple[str, list[dict]]:
    """(system, messages) for the decomposer. System asserted <= DECOMPOSE_SYSTEM_BUDGET_TOKENS."""
    system, _src = roles.load_prompt("decomposer")
    assert est_tokens(system) <= DECOMPOSE_SYSTEM_BUDGET_TOKENS, (
        f"decomposer system prompt {est_tokens(system)} tok > "
        f"{DECOMPOSE_SYSTEM_BUDGET_TOKENS} budget")
    # The request is provided to the runner; the user message is built by the caller.
    return system, []


def build_executor_prompt(step: Step, doc_summary: str, request: str,
                          error_feedback: str = "") -> tuple[str, list[dict]]:
    """Build the TINY self-contained executor prompt for one step.

    Returns ``(system, messages)``. The system is the executor role prompt + ONLY this step's
    feature-type cheat-sheet. The user message carries the doc summary, the request, the step, and
    (on retry) the prior op error. The TOTAL (system + user) is asserted under
    :data:`EXEC_PROMPT_BUDGET_TOKENS`; on overflow the doc summary is truncated FIRST.
    """
    base_system, _src = roles.load_prompt("executor")
    cheat = roles.load_cheatsheet(step.type)
    system = base_system.rstrip() + ("\n\n---\nCHEAT-SHEET for this step:\n" + cheat if cheat else "")

    step_text = (f"STEP TO DO NOW:\ntype: {step.type}\n{step.desc}"
                 + (f"\nparams: {json.dumps(step.params)}" if step.params else ""))
    parts = [f"REQUEST (overall): {request}", doc_summary, step_text]
    if error_feedback:
        parts.append("PREVIOUS ATTEMPT FAILED — fix this and re-emit the op array:\n"
                     + error_feedback)
    body = "\n\n".join(parts)

    # Budget assertion + remediation: truncate the doc summary FIRST until it fits.
    sys_tok = est_tokens(system)
    while sys_tok + est_tokens(body) > EXEC_PROMPT_BUDGET_TOKENS and doc_summary:
        # Halve the doc summary (chars) and rebuild the body.
        doc_summary = doc_summary[: max(40, len(doc_summary) // 2)].rstrip() + "\n...[doc trimmed]"
        parts[1] = doc_summary
        body = "\n\n".join(parts)
        if "[doc trimmed]" in doc_summary and len(doc_summary) <= 60:
            break
    assert sys_tok + est_tokens(body) <= EXEC_PROMPT_BUDGET_TOKENS, (
        f"executor prompt {sys_tok + est_tokens(body)} tok > {EXEC_PROMPT_BUDGET_TOKENS} "
        f"budget even after trimming the doc summary")
    return system, [user([text_part(body)])]


# --------------------------------------------------------------------------- per-step verify


@dataclass
class StepStatus:
    index: int
    type: str
    applied: bool
    ok: bool                       # all features compiled
    verified: bool                 # verifier checks passed (dims + volume direction)
    notes: str = ""
    volume_before: Optional[float] = None
    volume_after: Optional[float] = None


def verify_step(step: Step, tree: dict, feature_status: list[dict],
                vol_before: Optional[float], vol_after: Optional[float]) -> tuple[bool, str]:
    """Pure-python check of the applied step against its intent. Returns ``(verified, notes)``.

    Checks (no model call in v1):
      * the step's feature compiled (its row in feature_status is ok);
      * volume moved the right way — a boss/add step should grow it, a cut/hole/shell should
        shrink it (when both volumes are known);
      * the newest feature's key dims roughly match the step params (informational).
    """
    notes: list[str] = []

    # 1) compile status of the newest feature(s).
    bad = [s for s in (feature_status or []) if not s.get("ok")]
    if bad:
        return False, "feature did not compile: " + "; ".join(
            f"{s.get('id')}: {s.get('error')}" for s in bad[:3])

    # 2) volume direction.
    if vol_before is not None and vol_after is not None:
        removes = step.type in ("shell",) or _is_cut(step)
        adds = step.type in ("box", "cylinder", "sphere", "cone", "extrude", "revolve",
                             "linear_pattern", "circular_pattern") and not removes
        delta = vol_after - vol_before
        if removes and delta >= -1e-6 and vol_before > 0:
            notes.append(f"expected volume to DROP (cut/shell) but it did not (Δ={delta:.0f})")
            return False, "; ".join(notes)
        if adds and vol_before > 0 and delta <= 1e-6:
            notes.append(f"expected volume to GROW (boss) but it did not (Δ={delta:.0f})")
            return False, "; ".join(notes)
        notes.append(f"Δvol={delta:.0f}")

    # 3) dims sanity (informational; never fails the step alone).
    feats = (tree or {}).get("features", []) or []
    if feats:
        last = feats[-1]
        if last.get("type") == step.type:
            notes.append(f"added {last.get('type')} {last.get('id')}")
    return True, "; ".join(notes) if notes else "ok"


def _is_cut(step: Step) -> bool:
    mode = str(step.params.get("mode", "")).lower()
    if mode == "cut":
        return True
    return step.type == "hole" or "cut" in (step.desc or "").lower()


# --------------------------------------------------------------------------- the runner


def _provider_call(provider: Provider, system: str, messages: list[dict],
                   max_tokens: int = 1024) -> tuple[str, int]:
    """One bounded provider call; returns ``(reply, duration_ms)`` (never raises)."""
    t0 = time.perf_counter()
    try:
        reply, _thinking = provider.complete_ex(system, messages, max_tokens=max_tokens)
    except Exception as e:  # noqa: BLE001
        reply = ""
        _ = e
    return reply or "", int((time.perf_counter() - t0) * 1000)


def run_micro_design(request, doc_session, provider, emit=None, options=None):
    """Drive the MICRO-ORCHESTRATION profile against a live ``doc_session``.

    Signature mirrors :func:`agent.orchestrator.run.run_design` so the worker can dispatch to either
    based on :func:`should_engage`. Returns a ``RunResult`` (imported lazily to avoid a cycle).
    """
    import uuid

    from .run import RunResult  # local import: run.py imports state which imports roles; avoid cycle

    options = options or {}
    run_id = options.get("run_id") or uuid.uuid4().hex[:12]
    control = options.get("control")
    events = RunEvents(run_id, emit)

    # Offline-mode defense in depth (same guard as run_design).
    from .. import config as agent_config
    agent_config.assert_offline_safe(provider)

    def _tree() -> dict:
        return getattr(doc_session, "doc", {}) or {}

    def _cancelled() -> bool:
        return bool(control and getattr(control, "cancelled", False))

    events.emit("run_started", request=request, doc_id=getattr(doc_session, "doc_id", ""),
                stages=["decompose", "execute", "verify"], profile="micro")

    # ---- DECOMPOSE ----
    events.emit("stage_started", stage="decompose", role="decomposer", attempt=1, budget=1)
    dsys, _ = build_decompose_prompt()
    dsys = f"ROLE: decomposer\n{dsys}"
    duser = [user([text_part(f"REQUEST:\n{request}\n\nProduce the step list json now.")])]
    events.emit("agent_call", stage="decompose", role="decomposer", attempt=1,
                context_tokens_est=est_tokens(dsys) + est_tokens(duser[0]["content"][0]["text"]))
    reply, dur = _provider_call(provider, dsys, duser, max_tokens=1024)
    sidecar = events.write_sidecar("decompose", 1, reply)
    steps = normalize_steps(extract_json_array(reply))
    events.emit("agent_output", stage="decompose", role="decomposer", attempt=1,
                raw=reply[:4000], raw_path=sidecar, parsed_ok=bool(steps),
                ops_count=len(steps), duration_ms=dur)
    if not steps:
        events.emit("stage_result", stage="decompose", attempt=1, verdict="fail",
                    reason="no parseable step list")
        events.emit("run_finished", status="failed", stop_reason="decompose_failed",
                    revision=getattr(doc_session, "revision", 0))
        return RunResult(run_id=run_id, status="failed", stop_reason="decompose_failed",
                         revision=getattr(doc_session, "revision", 0),
                         doc_id=getattr(doc_session, "doc_id", ""))
    events.emit("stage_result", stage="decompose", attempt=1, verdict="pass",
                reason=f"{len(steps)} step(s)", step_count=len(steps))

    # ---- EXECUTE + VERIFY per step ----
    from engine.doc import session as docsess

    statuses: list[StepStatus] = []
    n = len(steps)
    for i, step in enumerate(steps):
        if _cancelled():
            events.emit("run_finished", status="cancelled", stop_reason="cancelled",
                        revision=getattr(doc_session, "revision", 0))
            return RunResult(run_id=run_id, status="cancelled", stop_reason="cancelled",
                             revision=getattr(doc_session, "revision", 0),
                             doc_id=getattr(doc_session, "doc_id", ""))
        vol_before = total_volume(getattr(doc_session, "last_result", None))
        # Inline activity: "step k/N".
        events.emit("stage_started", stage="execute", role="executor", attempt=1, budget=2,
                    step_index=i, step_total=n, step_type=step.type, step_desc=step.desc,
                    text=f"Step {i + 1}/{n}: {step.type} — {step.desc}")

        applied = False
        ok = False
        last_err = ""
        # ONE retry: attempt 1 fresh, attempt 2 with the prior error appended.
        for attempt in (1, 2):
            error_feedback = last_err if attempt == 2 else ""
            doc_summary = summarize_doc(_tree(), getattr(doc_session, "last_result", None))
            system, messages = build_executor_prompt(step, doc_summary, request, error_feedback)
            system = f"ROLE: executor\n{system}"
            events.emit("agent_call", stage="execute", role="executor", attempt=attempt,
                        step_index=i,
                        context_tokens_est=est_tokens(system)
                        + est_tokens(messages[0]["content"][0]["text"]))
            reply, dur = _provider_call(provider, system, messages, max_tokens=1024)
            sc = events.write_sidecar(f"execute_{i}", attempt, reply)
            ops, perr = ops_emit.extract_ops(reply)
            if ops is None:
                last_err = f"no ops array in reply ({perr})"
                events.emit("agent_output", stage="execute", role="executor", attempt=attempt,
                            step_index=i, raw=reply[:4000], raw_path=sc, parsed_ok=False,
                            parse_error=perr, duration_ms=dur)
                continue
            if not ops:
                events.emit("agent_output", stage="execute", role="executor", attempt=attempt,
                            step_index=i, raw=reply[:4000], raw_path=sc, parsed_ok=True,
                            ops_count=0, duration_ms=dur)
                applied = True  # nothing to do for this step; treat as a no-op pass
                ok = True
                break
            valid, verr = ops_emit.dry_run(doc_session.doc, ops)
            events.emit("agent_output", stage="execute", role="executor", attempt=attempt,
                        step_index=i, raw=reply[:4000], raw_path=sc, parsed_ok=True,
                        ops_count=len(ops), duration_ms=dur,
                        parse_error=None if valid else verr)
            if not valid:
                last_err = f"ops failed validation: {verr}"
                continue
            try:
                resp = docsess.apply_ops(doc_session, doc_session.revision, ops)
            except Exception as e:  # noqa: BLE001
                last_err = f"apply failed: {e}"
                continue
            applied = True
            fs = resp.get("rebuild", {}).get("feature_status", [])
            ok = all(s.get("ok") for s in fs)
            break

        vol_after = total_volume(getattr(doc_session, "last_result", None))
        if applied:
            verified, notes = verify_step(step, _tree(), _step_feature_status(doc_session),
                                          vol_before, vol_after)
        else:
            verified, notes = False, last_err or "executor produced no valid ops"
        statuses.append(StepStatus(index=i, type=step.type, applied=applied, ok=ok,
                                   verified=verified, notes=notes,
                                   volume_before=vol_before, volume_after=vol_after))
        events.emit("stage_result", stage="execute", attempt=1,
                    verdict="pass" if (applied and ok) else "fail",
                    reason=notes, step_index=i, step_total=n,
                    verified=verified, applied=applied,
                    text=f"Step {i + 1}/{n} {'done' if applied and ok else 'failed'}: {notes}")

    # ---- finish ----
    applied_ok = sum(1 for s in statuses if s.applied and s.ok)
    status = "done" if applied_ok > 0 else "failed"
    stop_reason = "completed" if status == "done" else "all_steps_failed"
    revision = getattr(doc_session, "revision", 0)
    events.emit("run_finished", status=status, stop_reason=stop_reason, revision=revision,
                steps_applied=applied_ok, steps_total=n)
    return RunResult(run_id=run_id, status=status, stop_reason=stop_reason,
                     revision=revision, doc_id=getattr(doc_session, "doc_id", ""))


def _step_feature_status(doc_session) -> list[dict]:
    """The latest feature_status rows from the session's last build (for the verifier)."""
    res = getattr(doc_session, "last_result", None)
    fs = getattr(res, "feature_status", None)
    return list(fs) if fs else []
