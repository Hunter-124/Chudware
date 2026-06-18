"""cpp_orch_machine_parity.py — P53a parity gate: the C++ orchestrator state
machine + DesignState/context assembly vs the REAL Python oracle
(agent/orchestrator/machine.py + state.py).

GATE A (the important one) — the deterministic state machine. For a battery of
SCRIPTS exercising every control-flow path, drive BOTH:
  - Python  : agent.orchestrator.machine.run_machine with scripted Action
              callables popping the same outcomes + a RunEvents capturing events.
  - C++      : libaicad.dll agent_run_machine {script, plan_steps, budgets?,
              cancel_after?} which builds the identical scripted actions.
Then assert (a) final {status, stop_reason} equal and (b) the NORMALIZED event
stream equal (volatile fields run_id/duration_ms/prompt_hash/raw_path stripped).

GATE B — state/context pure functions: err_sig, digest_tree, digest_params,
looks_like_edit, feature_status_errors, est_tokens, and build_context for every
role (system + body byte-equal), including the trim/drop-under-pressure path.

For the spec/plan json.dumps(...,separators=(",",": "),indent=0) byte-equality:
the C++ side replicates Python's exact formatting (py_json_dumps_indent0), so the
build_context body is compared byte-for-byte. The fixtures use int/string-only
spec/plan so float repr never enters; if a float spec is ever needed the gate
would fall back to structural comparison (NOT exercised here).

Run under the runtime venv (imports engine + agent packages):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_orch_machine_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatches (0 = full parity). DO NOT weaken assertions.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402

PROMPTS_DIR = str(REPO / "agent" / "prompts")

_fails: list[str] = []
_oks: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        _oks.append(label)
    else:
        _fails.append(f"{label}\n    C++ : {got!r}\n    PY  : {want!r}")


# --------------------------------------------------------------------------- event normalization
_VOLATILE = {"run_id", "duration_ms", "prompt_hash", "raw_path"}


def normalize_events(events: list[dict]) -> list[dict]:
    """Strip volatile fields so the two streams compare structurally + in order."""
    out = []
    for e in events:
        out.append({k: v for k, v in e.items() if k not in _VOLATILE})
    return out


# --------------------------------------------------------------------------- Python machine driver
def run_python_machine(script: dict, plan_steps: list, budgets: dict | None,
                       cancel_after: int | None) -> tuple[dict, list[dict]]:
    """Drive the REAL run_machine with scripted actions + a capturing RunEvents."""
    from agent.orchestrator import machine as M
    from agent.orchestrator.events import RunEvents
    from agent.orchestrator.state import DesignState

    captured: list[dict] = []

    class CapturingEvents(RunEvents):
        def emit(self, kind, **data):
            ev = super().emit(kind, **data)
            captured.append(ev)
            return ev

    invoked = {"n": 0}

    def make_action(stage: str):
        queue = list(script.get(stage, []))
        cursor = {"i": 0}

        def action(state: DesignState, attempt: int) -> M.StageOutcome:
            invoked["n"] += 1
            if cursor["i"] < len(queue):
                spec = queue[cursor["i"]]
                cursor["i"] += 1
            elif queue:
                spec = queue[-1]
            else:
                spec = {"verdict": "pass"}
            o = M.StageOutcome(
                verdict=spec.get("verdict", "pass"),
                reason=spec.get("reason", ""),
                error=spec.get("error"),
                done=spec.get("done", False),
                raw=spec.get("raw", ""),
                parsed_ok=spec.get("parsed_ok", True),
                parse_error=spec.get("parse_error"),
                ops_count=spec.get("ops_count"),
                duration_ms=spec.get("duration_ms", 0),
                no_progress=spec.get("no_progress", False),
            )
            state._last_no_progress = o.no_progress
            return o

        return action

    actions = M.StageActions(
        intake=make_action("intake"),
        plan=make_action("plan"),
        model=make_action("model"),
        review=make_action("review"),
        fix=make_action("fix"),
        memory=make_action("memory"),
        plan_steps=lambda state: list(plan_steps),
    )

    control = M.RunControl()
    if cancel_after is not None:
        class _Ctl(M.RunControl):
            @property
            def cancelled(self):  # type: ignore[override]
                return invoked["n"] >= cancel_after

            @cancelled.setter
            def cancelled(self, v):
                pass
        control = _Ctl()

    state = DesignState(request="test request")
    run_id = "pymach-" + uuid.uuid4().hex
    ev = CapturingEvents(run_id)
    result = M.run_machine(state, actions, ev, budgets=budgets, control=control)
    return result, captured


# --------------------------------------------------------------------------- GATE A scenarios
def gate_machine(cpp: CppCore) -> None:
    P = {"verdict": "pass"}
    R = {"verdict": "retry"}
    F = {"verdict": "fail"}
    S = {"verdict": "skip"}

    def err(sig, **kw):
        d = {"verdict": "retry", "error": sig}
        d.update(kw)
        return d

    scenarios = [
        # 1. happy path: intake pass, plan pass, 2 steps, model passx2, review pass, memory pass.
        ("1-happy",
         {"intake": [P], "plan": [P], "model": [P, P], "review": [P], "memory": [P]},
         ["step A", "step B"], None, None),
        # 2. intake retry -> budget exhausted (non-fatal) then proceeds.
        ("2-intake-exhaust",
         {"intake": [R], "plan": [P], "model": [P], "review": [P], "memory": [P]},
         ["only"], {"intake": 1}, None),
        # 3. plan retry x2 -> degrade; plan_steps single step.
        ("3-plan-degrade",
         {"intake": [P], "plan": [R, R], "model": [P], "review": [P], "memory": [P]},
         [""], {"plan": 2}, None),
        # 4. model retry until escape -> model fail -> {failed, model_failed}.
        ("4-model-fail",
         {"intake": [P], "plan": [P], "model": [R, R, R, R], "review": [P], "memory": [P]},
         ["s1"], {"model": 4}, None),
        # 5. identical-failure: model returns same err sig 3x -> stage fail.
        ("5-identical-fail",
         {"intake": [P], "plan": [P],
          "model": [err("ValueError: boom"), err("ValueError: boom"), err("ValueError: boom")],
          "review": [P], "memory": [P]},
         ["s1"], {"model": 4}, None),
        # 6. review retry -> fix pass -> review pass (2 rounds).
        ("6-review-fix-pass",
         {"intake": [P], "plan": [P], "model": [P], "review": [R, P], "fix": [P], "memory": [P]},
         ["s1"], {"review": 2, "fix": 4}, None),
        # 7. review retry -> fix fail -> break.
        ("7-review-fix-fail",
         {"intake": [P], "plan": [P], "model": [P], "review": [R, R],
          "fix": [R, R, R, R], "memory": [P]},
         ["s1"], {"review": 2, "fix": 4}, None),
        # 8. stagnation: 3 no_progress model steps -> break the loop.
        ("8-stagnation",
         {"intake": [P], "plan": [P],
          "model": [dict(verdict="pass", no_progress=True)] * 5,
          "review": [P], "memory": [P]},
         ["s1", "s2", "s3", "s4", "s5"], None, None),
        # 9. global cap: >40 forced calls (many short-budget steps) -> {failed, max_iters} or fail.
        ("9-global-cap",
         {"intake": [P], "plan": [P],
          "model": [P] * 60, "review": [P], "memory": [P]},
         [f"s{i}" for i in range(60)], {"model": 1}, None),
        # 10. cancel_after k -> {cancelled, cancelled}.
        ("10-cancel",
         {"intake": [P], "plan": [P], "model": [P, P], "review": [P], "memory": [P]},
         ["s1", "s2"], None, 2),
    ]

    for name, script, steps, budgets, cancel_after in scenarios:
        py_result, py_events = run_python_machine(script, steps, budgets, cancel_after)
        params = {"script": script, "plan_steps": steps}
        if budgets is not None:
            params["budgets"] = budgets
        if cancel_after is not None:
            params["cancel_after"] = cancel_after
        rc, data = cpp.call("agent_run_machine", params)
        c_result = {"status": data.get("status"), "stop_reason": data.get("stop_reason")}
        c_events = data.get("events") or []

        check(f"(A {name}) result",
              c_result, {"status": py_result["status"], "stop_reason": py_result["stop_reason"]})
        check(f"(A {name}) event stream",
              normalize_events(c_events), normalize_events(py_events))
        check(f"(A {name}) event count",
              len(c_events), len(py_events))


# --------------------------------------------------------------------------- GATE B: err_sig
def gate_err_sig(cpp: CppCore) -> None:
    from agent.orchestrator.machine import err_sig

    corpus = [
        "Traceback (most recent call last):\n  File x\n    foo()\nValueError: bad input",
        "spec failed\nvalidation error: width must be > 0",
        "RuntimeError: kernel exploded",
        "  \n   \n  ",
        "",
        "my.module.CustomError: nested",
        "just a plain message",
        "Warning: not an error\nKeyError\nmore text after",
        "line1\nValueError: first\nline3\nTypeError: last",
        "HTTPError(500)\ntrailing line",
    ]
    for i, e in enumerate(corpus):
        py = err_sig(e)
        rc, data = cpp.call("agent_err_sig", {"err": e})
        check(f"(B err_sig[{i}])", data.get("sig"), py)
    # None case (empty).
    py = err_sig(None)
    rc, data = cpp.call("agent_err_sig", {"err": ""})
    check("(B err_sig[None])", data.get("sig"), py)


# --------------------------------------------------------------------------- GATE B: digest_tree
def gate_digest_tree(cpp: CppCore) -> None:
    from agent.orchestrator.state import digest_tree, _digest_params

    trees = [
        {"features": []},
        {},
        {"features": [
            {"id": "f1", "type": "box", "name": "Box1",
             "params": {"width": {"value": 50}, "height": {"value": 20, "expr": None},
                        "depth": 10, "flag": True}},
            {"id": "f2", "type": "fillet", "name": "Fillet1", "suppressed": True,
             "params": {"radius": {"value": 3}}},
            {"id": "f3", "type": "hole", "name": ""},  # empty name -> rstrip
            "not a dict",
            {"id": "f4", "type": "extrude", "name": "Ex",
             "params": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7}},  # >6 params
        ]},
        # >40 features -> truncation marker.
        {"features": [{"id": f"f{i}", "type": "box", "name": f"B{i}"} for i in range(45)]},
    ]
    for i, t in enumerate(trees):
        py = digest_tree(t)
        rc, data = cpp.call("agent_digest_tree", {"tree": t})
        check(f"(B digest_tree[{i}])", data.get("digest"), py)

    param_cases = [
        {"width": {"value": 50}, "h": {"value": 20, "expr": None}, "d": 10},
        {"a": 1.5, "b": {"value": 2.0}, "name": "ignored-str", "flag": True},
        {},
        {"x": "string-skipped", "y": [1, 2], "z": {"no_value_key": 1}},
    ]
    for i, p in enumerate(param_cases):
        py = _digest_params(p)
        rc, data = cpp.call("agent_digest_params", {"params": p})
        check(f"(B digest_params[{i}])", data.get("s"), py)


# --------------------------------------------------------------------------- GATE B: looks_like_edit
def gate_looks_like_edit(cpp: CppCore) -> None:
    from agent.orchestrator.state import looks_like_edit

    corpus = [
        "make the plate 60mm wide",
        "change the radius",
        "create a new box",  # not an edit
        "MAKE IT BIGGER",
        "build a bracket",   # not an edit
        "remove the fillet",
        "set width to 5",
        "a fancy widget",    # 'widen'? no. not an edit
        "widen the slot",
        "",
        "instead use a cylinder",
        "scale it up",
    ]
    for i, r in enumerate(corpus):
        py = looks_like_edit(r)
        rc, data = cpp.call("agent_looks_like_edit", {"request": r})
        check(f"(B looks_like_edit[{i}={r!r}])", data.get("edit"), py)


# --------------------------------------------------------------------------- GATE B: feature_status_errors
def gate_feature_status_errors(cpp: CppCore) -> None:
    from agent.orchestrator.state import feature_status_errors

    cases = [
        [],
        [{"id": "f1", "ok": True}, {"id": "f2", "ok": False, "error": "boom"}],
        [{"id": "f3", "ok": False, "error": ""},  # error falsy -> skipped
         {"ok": False, "error": "no id here"},
         {"id": "f5", "ok": True, "error": "ok-so-skipped"}],
    ]
    for i, c in enumerate(cases):
        py = feature_status_errors(c)
        rc, data = cpp.call("agent_feature_status_errors", {"feature_status": c})
        check(f"(B feature_status_errors[{i}])", data.get("errors"), py)


# --------------------------------------------------------------------------- GATE B: est_tokens
def gate_est_tokens(cpp: CppCore) -> None:
    from agent.orchestrator.state import est_tokens

    for i, t in enumerate(["", "a", "abcd", "abcde", "x" * 100, "x" * 101]):
        py = est_tokens(t)
        rc, data = cpp.call("agent_est_tokens", {"text": t})
        check(f"(B est_tokens[len={len(t)}])", data.get("n"), py)


# --------------------------------------------------------------------------- GATE B: build_context
def _state_dict(**kw) -> dict:
    base = {
        "request": kw.get("request", ""),
        "doc_id": kw.get("doc_id", ""),
        "revision": kw.get("revision", 0),
        "spec": kw.get("spec"),
        "plan": kw.get("plan"),
        "tree_digest": kw.get("tree_digest", ""),
        "last_errors": kw.get("last_errors", []),
        "last_issues": kw.get("last_issues", []),
        "memory": kw.get("memory", {}),
        "current_step": kw.get("current_step", ""),
    }
    return base


def gate_build_context(cpp: CppCore) -> None:
    from agent.orchestrator.state import build_context, DesignState

    # A fully-populated state — int/string-only spec+plan for byte-exact json.dumps.
    spec = {"name": "bracket", "units": "mm", "dims": {"width": 60, "height": 20},
            "features": ["plate", "hole"], "notes": "café ☃"}
    plan = {"name": "Bracket", "parts": [{"name": "base", "qty": 1},
                                         {"name": "rib", "qty": 2}], "order": [1, 2, 3]}
    tree_digest = "f1 box Box1 (width=60, height=20)\nf2 hole Hole1 (dia=5)"
    memory = {"summary": "prior part", "decisions": ["use mm", "z-up"],
              "open_issues": ["thin wall", "", None, "sharp edge"]}

    base = _state_dict(
        request="make the plate 60mm wide",
        spec=spec, plan=plan, tree_digest=tree_digest,
        last_errors=["e1: failed", "e2: also failed"],
        last_issues=["wall too thin"],
        memory=memory, current_step="add the plate",
    )

    roles = ["intake", "planner", "modeler", "reviewer", "fixer", "memory"]
    for role in roles:
        # Build the Python DesignState from the same dict.
        ds = DesignState(
            request=base["request"], doc_id=base["doc_id"], revision=base["revision"],
            spec=base["spec"], plan=base["plan"], tree_digest=base["tree_digest"],
            last_errors=list(base["last_errors"]), last_issues=list(base["last_issues"]),
            memory=dict(base["memory"]), current_step=base["current_step"],
        )
        py_system, py_msgs, py_chars, py_tok = build_context(ds, role)
        py_body = py_msgs[0]["content"][0]["text"]

        rc, data = cpp.call("agent_build_context",
                            {"state": base, "role": role, "prompts_dir": PROMPTS_DIR})
        check(f"(B build_context[{role}].system)", data.get("system"), py_system)
        check(f"(B build_context[{role}].body)", data.get("body"), py_body)
        check(f"(B build_context[{role}].chars)", data.get("context_chars"), py_chars)
        check(f"(B build_context[{role}].tokens)", data.get("context_tokens_est"), py_tok)

    # Edit-bias absent when no features.
    nofeat = _state_dict(request="make it bigger", tree_digest="")
    ds2 = DesignState(request="make it bigger")
    py_system, py_msgs, _c, _t = build_context(ds2, "modeler")
    rc, data = cpp.call("agent_build_context",
                        {"state": nofeat, "role": "modeler", "prompts_dir": PROMPTS_DIR})
    check("(B build_context[modeler-nobias].body)",
          data.get("body"), py_msgs[0]["content"][0]["text"])

    # --- TRIM / drop-under-pressure path: huge memory + plan exceed 6000 tokens.
    big = "x" * 40000  # ~10k tokens -> forces dropping opt: blocks.
    big_mem = {"summary": big, "decisions": [big]}
    big_plan = {"name": "P", "parts": [{"name": big}]}
    trim_state = _state_dict(
        request="make a box " + big,  # request itself big to push past cap even after drops
        plan=big_plan, tree_digest="f1 box B (w=10)",
        memory=big_mem, current_step="step",
    )
    for role in ["modeler", "fixer"]:
        ds3 = DesignState(
            request=trim_state["request"], plan=trim_state["plan"],
            tree_digest=trim_state["tree_digest"], memory=trim_state["memory"],
            current_step=trim_state["current_step"],
        )
        py_system, py_msgs, py_chars, py_tok = build_context(ds3, role)
        py_body = py_msgs[0]["content"][0]["text"]
        rc, data = cpp.call("agent_build_context",
                            {"state": trim_state, "role": role, "prompts_dir": PROMPTS_DIR})
        check(f"(B build_context[trim-{role}].body)", data.get("body"), py_body)
        check(f"(B build_context[trim-{role}].tokens)",
              data.get("context_tokens_est"), py_tok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, type=Path)
    args = ap.parse_args()

    cpp = CppCore(args.dll)
    print(f"ABI: {cpp.abi()}")

    gate_machine(cpp)
    gate_err_sig(cpp)
    gate_digest_tree(cpp)
    gate_looks_like_edit(cpp)
    gate_feature_status_errors(cpp)
    gate_est_tokens(cpp)
    gate_build_context(cpp)

    print(f"\n  checks passed: {len(_oks)}")
    if _fails:
        print(f"  MISMATCHES: {len(_fails)}\n")
        for f in _fails:
            print("  FAIL " + f)
    else:
        print("  ALL MATCH — full parity (GATE A machine + GATE B state/context)")
    return len(_fails)


if __name__ == "__main__":
    sys.exit(main())
