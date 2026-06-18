"""cpp_orch_run_parity.py — P53b END-TO-END parity gate: the C++ run_design
(core/agent/run.cpp) vs the REAL Python oracle (agent.orchestrator.run.run_design)
driven against a LIVE document session.

For each scenario we script a RoleScriptedProvider's by-role replies, then drive:
  - Python : agent.orchestrator.run.run_design(request, DocSession, provider, ...)
             with _render_review monkeypatched to None (vision rendering is HOST
             display tooling, deferred to P55 — documented out-of-scope; this makes
             the Python review stats-only, exactly like the C++ review).
  - C++    : open a C++ doc session with the SAME blank part, then
             agent_run_design{request, doc_id, provider:{name:"mock",replies:{by_role}},
             options:{run_id:"fixed", prompts_dir}}.
Then assert (a) same final {status, stop_reason, revision}; (b) the resulting FEATURE
TREE identical (feature ids/types/params, float-normalized); (c) the NORMALIZED event
stream identical (volatile run_id/duration_ms/prompt_hash/raw_path stripped; prompt_file
is a per-engine absolute path of the SAME class as raw_path and is normalized too).

Run under the runtime venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_orch_run_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatches (0 = full parity). DO NOT weaken assertions.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
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


# --------------------------------------------------------------------------- ops shapes
# Known-good op batches (cross-checked against tools/cpp_session_parity.py). We use
# sketch+extrude exclusively: the resulting tree digest carries only single-value params
# ("depth=N"), so the digest is INSENSITIVE to object-key ordering — the one place a
# sorted-map C++ json could diverge from a Python insertion-ordered dict.
def _line(x1, y1, x2, y2):
    return {"op": "sketch_add_entity", "sketch": "$0",
            "entity": {"type": "line", "construction": False, "p1": [x1, y1], "p2": [x2, y2]}}


def rect_extrude_ops(depth, sketch_name="Sketch1", boss_name="Boss1"):
    return [
        {"op": "add_feature", "feature": {"type": "sketch", "name": sketch_name,
                                          "params": {"entities": [], "constraints": []},
                                          "refs": {"plane": "plane.top"}}},
        _line(0, 0, 40, 0), _line(40, 0, 40, 30), _line(40, 30, 0, 30), _line(0, 30, 0, 0),
        {"op": "add_feature", "feature": {
            "type": "extrude", "name": boss_name,
            "params": {"mode": "boss", "end": "blind", "depth": {"value": depth, "expr": None}},
            "refs": {"profile": [{"kind": "region", "sketch": "$0",
                                  "centroid": [20, 15], "area": 1200.0, "hint_index": 0}]}}},
    ]


def bad_ops():
    # An UNKNOWN op -> fails dry_run validation on BOTH engines with a BYTE-IDENTICAL
    # message: "op[0]: unknown op 'frobnicate'". (Most invalid-op messages diverge in
    # wording between engine.doc.ops and core/doc/session.cpp — a PRE-EXISTING op-error
    # text divergence outside P53b's scope; the unknown-op message is identical, so it
    # cleanly exercises the dry-run retry + verbatim error-feedback composition that
    # P53b's doc_dry_run_ops owns.)
    return [{"op": "frobnicate", "bogus": 1}]


def fence(obj) -> str:
    return "```json\n" + _json.dumps(obj) + "\n```"


def pyblock(code: str) -> str:
    return "```python\n" + code + "\n```"


# --------------------------------------------------------------------------- normalization
# Volatile fields stripped before the structural stream compare:
#  - run_id / duration_ms / prompt_hash / raw_path : per-run/timing/path noise (as P53a).
#  - prompt_file : a per-engine ABSOLUTE prompt path (same class as raw_path); both engines
#    read agent/prompts but format the path string independently.
#  - doc_id : the engine-assigned OPAQUE session identifier (C++ "doc-cppNNN" vs Python uuid
#    hex). It is NOT a behavior — it is the analogue of run_id for the session. The actual
#    session CONTENT (the resulting feature tree) is compared directly + must be identical.
_VOLATILE = {"run_id", "duration_ms", "prompt_hash", "raw_path", "prompt_file", "doc_id"}


def normalize_events(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        out.append({k: v for k, v in e.items() if k not in _VOLATILE})
    return out


def round_floats(x, nd=6):
    if isinstance(x, bool):
        return x
    if isinstance(x, float):
        return round(x, nd)
    if isinstance(x, dict):
        return {k: round_floats(v, nd) for k, v in x.items()}
    if isinstance(x, list):
        return [round_floats(v, nd) for v in x]
    return x


def feature_summary(tree: dict) -> list:
    """Order-independent, float-normalized per-feature view: (id, type, params, refs)."""
    out = []
    for f in (tree or {}).get("features", []):
        if not isinstance(f, dict):
            continue
        out.append({"id": f.get("id"), "type": f.get("type"),
                    "params": round_floats(f.get("params") or {}),
                    "refs": round_floats(f.get("refs") or {})})
    return out


# --------------------------------------------------------------------------- engines
def run_python(request, replies, run_id):
    """Drive the REAL run_design against a live Python DocSession + scripted provider."""
    from agent.orchestrator import run as R
    from agent.providers.mock_provider import RoleScriptedProvider
    from engine.doc import session as docsess

    # Neutralize display: rendering is HOST tooling (P55), out of scope. Stats-only review.
    R._render_review = lambda session: None  # type: ignore[assignment]

    sess = docsess.new_session("part")
    provider = RoleScriptedProvider({k: list(v) for k, v in replies.items()})

    captured: list[dict] = []
    res = R.run_design(request, sess, provider, emit=captured.append,
                       options={"run_id": run_id})
    tree = sess.doc
    return ({"status": res.status, "stop_reason": res.stop_reason, "revision": res.revision},
            tree, captured)


def run_cpp(core: CppCore, request, replies, run_id):
    rc, newr = core.call("doc_new", {"kind": "part"})
    doc_id = newr["doc_id"]
    rc, data = core.call("agent_run_design", {
        "request": request, "doc_id": doc_id,
        "provider": {"name": "mock", "replies": replies},
        "options": {"run_id": run_id, "prompts_dir": PROMPTS_DIR},
    })
    result = {"status": data.get("status"), "stop_reason": data.get("stop_reason"),
              "revision": data.get("revision")}
    events = data.get("events") or []
    rc, tr = core.call("doc_tree", {"doc_id": doc_id})
    return result, tr.get("tree", {}), events


# --------------------------------------------------------------------------- scenarios
def scenarios():
    SPEC = fence({"object": "bracket", "units": "mm"})
    MEM = fence({"summary": "built a 2-part bracket"})

    # 1) HAPPY PATH: 2-part plan -> 2 model steps (both valid sketch+extrude), review done.
    happy = {
        "intake": [SPEC],
        "planner": [fence({"name": "Bracket",
                           "parts": [{"name": "base", "purpose": "plate"},
                                     {"name": "post", "purpose": "riser"}]})],
        "modeler": [fence(rect_extrude_ops(10.0, "S1", "B1")),
                    fence(rect_extrude_ops(20.0, "S2", "B2"))],
        "reviewer": [fence({"done": True})],
        "memory": [MEM],
    }

    # 2) INVALID-THEN-VALID model ops: 1st reply fails dry_run, 2nd applies.
    inv_valid = {
        "intake": [SPEC],
        "planner": [fence({"junk": True})],  # degrade -> single default step
        "modeler": [fence(bad_ops()), fence(rect_extrude_ops(12.0, "S1", "B1"))],
        "reviewer": [fence({"done": True})],
        "memory": [MEM],
    }

    # 3) ESCAPE HATCH: unparseable ops twice, then (attempt>2) a build123d script.
    escape = {
        "intake": [SPEC],
        "planner": [fence({"junk": True})],
        "modeler": ["no ops here, just prose",
                    "still no ops",
                    pyblock("result = Box(10, 10, 10)")],
        "reviewer": [fence({"done": True})],
        "memory": [MEM],
    }

    # 4) REVIEW -> FIX: review NOT done w/ issue, fixer applies a valid ops patch, review done.
    review_fix = {
        "intake": [SPEC],
        "planner": [fence({"junk": True})],
        "modeler": [fence(rect_extrude_ops(10.0, "S1", "B1"))],
        "reviewer": [fence({"done": False, "issues": ["too short"]}), fence({"done": True})],
        "fixer": [fence(rect_extrude_ops(8.0, "S2", "B2"))],
        "memory": [MEM],
    }

    # 5) PLAN DEGRADE: planner junk -> single default step; modeler valid.
    degrade = {
        "intake": [SPEC],
        "planner": [fence({"nope": 1})],
        "modeler": [fence(rect_extrude_ops(15.0, "S1", "B1"))],
        "reviewer": [fence({"done": True})],
        "memory": [MEM],
    }

    return [
        ("1-happy-path", "make a bracket", happy),
        ("2-invalid-then-valid", "make a plate", inv_valid),
        ("3-escape-hatch", "make a cube", escape),
        ("4-review-fix", "make a bracket", review_fix),
        ("5-plan-degrade", "make a widget", degrade),
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", default=str(REPO / "core" / "build-occt" / "bin" / "Release" /
                                         "libaicad.dll"), type=Path)
    args = ap.parse_args()

    if not args.dll.exists():
        print(f"[orch-run-parity] libaicad.dll not found at {args.dll}")
        return 2
    core = CppCore(args.dll)
    print(f"[orch-run-parity] libaicad ABI={core.abi()}")

    for name, request, replies in scenarios():
        run_id = "fixed-" + name
        # Each engine consumes its own deep copy of the scripted replies (RoleScriptedProvider
        # pops from its queue; the C++ side parses fresh JSON).
        py_result, py_tree, py_events = run_python(request, _deep(replies), run_id)
        c_result, c_tree, c_events = run_cpp(core, request, _deep(replies), run_id)

        check(f"({name}) result", c_result, py_result)
        check(f"({name}) feature tree", feature_summary(c_tree), feature_summary(py_tree))
        check(f"({name}) event stream",
              normalize_events(c_events), normalize_events(py_events))
        check(f"({name}) event count", len(c_events), len(py_events))

    print(f"\n  checks passed: {len(_oks)}")
    if _fails:
        print(f"  MISMATCHES: {len(_fails)}\n")
        for f in _fails:
            print("  FAIL " + f)
    else:
        print("  ALL MATCH — full end-to-end parity (5 scenarios: result + tree + events)")
    return len(_fails)


def _deep(d):
    return _json.loads(_json.dumps(d))


if __name__ == "__main__":
    sys.exit(main())
