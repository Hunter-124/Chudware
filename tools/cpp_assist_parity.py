"""cpp_assist_parity.py — P54b parity gate: C++ in-app LLM assists vs the Python
oracle (agent/ask.py + agent/pm_assist.py).

These are the two provider-driven, pure-logic assists ported in P54b:
  ASK   — read-only Q&A about the open part (build_doc_context / build_ask_prompt /
          answer_question).
  PM    — PropertyManager "Select with AI" JSON action plan (_extract_json /
          _coerce_patch_value / _validate_plan / _heuristic_plan /
          build_pm_assist_prompt / assist_property_manager).

Each section drives the C++ DLL diagnostic handlers and compares to the REAL
Python module byte-for-byte. build_doc_context is driven via a tiny stub session
object (so the real Python functions run); the SAME doc/doc_id/revision/
feature_status are handed to the C++ handler.

Run under the runtime venv (imports the engine + agent packages):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_assist_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatches (0 = full parity). DO NOT weaken assertions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402

_fails: list[str] = []
_oks: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        _oks.append(label)
    else:
        _fails.append(f"{label}\n    C++ : {got!r}\n    PY  : {want!r}")


# --------------------------------------------------------------------------- stubs
class _StubResult:
    def __init__(self, feature_status):
        self.feature_status = feature_status


class _StubSession:
    """Minimal stand-in for the worker session the oracle reads."""

    def __init__(self, doc, doc_id="?", revision=0, feature_status=None):
        self.doc = doc
        self.doc_id = doc_id
        self.revision = revision
        self.last_result = _StubResult(feature_status if feature_status is not None else [])


class _ScriptedProvider:
    """A one-shot provider returning a fixed reply (ask/pm single call)."""

    def __init__(self, reply):
        self._reply = reply

    def complete(self, system, messages, max_tokens=8000):
        return self._reply


class _ErrProvider:
    def __init__(self, msg):
        self._msg = msg

    def complete(self, system, messages, max_tokens=8000):
        raise RuntimeError(self._msg)


# --------------------------------------------------------------------------- doc fixtures
def _doc_with_features():
    return {
        "name": "Bracket",
        "units": {"system": "IPS"},
        "features": [
            {"id": "f1", "type": "sketch", "name": "Base Sketch"},
            {"id": "f2", "type": "extrude", "name": "Boss",
             "params": {"depth": {"value": 25, "expr": "d"}, "draft": 3, "merge": True,
                        "label": "main", "scale": 1.5}},
            {"id": "f3", "type": "fillet", "name": "Round", "suppressed": True,
             "params": {"radius": {"value": 2.0}}},
        ],
    }


def _doc_empty():
    return {"name": "Empty", "units": {"system": "MMGS"}, "features": []}


def _doc_many():
    feats = [{"id": f"f{i}", "type": "box", "name": f"B{i}"} for i in range(70)]
    return {"name": "Many", "features": feats}


def _doc_huge():
    # A doc whose context body exceeds 8000 chars (long names force truncation).
    feats = [{"id": f"f{i}", "type": "extrude", "name": "X" * 200,
              "params": {"depth": {"value": i}}} for i in range(60)]
    return {"name": "Huge", "features": feats}


# --------------------------------------------------------------------------- ASK gate
def gate_ask(cpp: CppCore) -> None:
    import agent.ask as ask

    cases = [
        ("none", None, "?", 0, []),
        ("empty", _doc_empty(), "doc-empty", 0, []),
        ("feats-clean", _doc_with_features(), "doc-7", 4,
         [{"id": "f1", "ok": True}, {"id": "f2", "ok": True}, {"id": "f3", "ok": True}]),
        ("feats-errs", _doc_with_features(), "doc-7", 4,
         [{"id": "f1", "ok": True},
          {"id": "f2", "ok": False, "error": "bad sketch"},
          {"id": "f3", "ok": False, "error": "deg"},
          {"id": "f4", "ok": False, "error": "e4"},
          {"id": "f5", "ok": False, "error": "e5"},
          {"id": "f6", "ok": False, "error": "e6"}]),
        ("feats-notbuilt", _doc_with_features(), "doc-7", 7, []),
        ("many", _doc_many(), "doc-many", 1, []),
        ("huge", _doc_huge(), "doc-huge", 2, []),
    ]

    for tag, doc, doc_id, rev, fs in cases:
        sess = _StubSession(doc, doc_id=doc_id, revision=rev, feature_status=fs)
        py_ctx = ask.build_doc_context(sess)
        rc, data = cpp.call("agent_build_doc_context",
                            {"doc": doc, "doc_id": doc_id, "revision": rev,
                             "feature_status": fs})
        check(f"(ask) build_doc_context[{tag}]", data.get("text"), py_ctx)

        # build_ask_prompt — system + body.
        q = "How deep is the boss?"
        py_sys, py_msgs = ask.build_ask_prompt(q, sess)
        py_body = py_msgs[0]["content"][0]["text"]
        rc, data = cpp.call("agent_build_ask_prompt",
                            {"question": q, "doc": doc, "doc_id": doc_id,
                             "revision": rev, "feature_status": fs})
        check(f"(ask) build_ask_prompt[{tag}].system", data.get("system"), py_sys)
        check(f"(ask) build_ask_prompt[{tag}].body", data.get("body"), py_body)

    # answer_question — scripted reply (with surrounding whitespace) + empty fallback.
    doc = _doc_with_features()
    sess = _StubSession(doc, doc_id="doc-7", revision=4, feature_status=[{"id": "f1", "ok": True}])
    for tag, reply in [("normal", "  The boss is 25mm deep.\n"),
                       ("empty", "   \n  "),
                       ("blank", "")]:
        py = ask.answer_question("how deep?", sess, provider=_ScriptedProvider(reply))
        rc, data = cpp.call("agent_answer_question",
                            {"question": "how deep?", "doc": doc, "doc_id": "doc-7",
                             "revision": 4, "feature_status": [{"id": "f1", "ok": True}],
                             "provider": {"name": "mock", "replies": [reply]}})
        check(f"(ask) answer_question[{tag}]", data.get("answer"), py)


# --------------------------------------------------------------------------- PM: extract_json
def gate_pm_extract(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    corpus = [
        '```json\n{"answer": "hi", "patches": []}\n```',
        '```\n{"a": 1}\n```',
        'prose before {"k": "v", "n": 2} prose after',
        'not json at all',
        '',
        '[1, 2, 3]',            # valid json but not a dict -> {}
        '{"x": 1}',             # bare dict
        '```JSON\n{"upper": true}\n```',  # case-insensitive fence
        '  {"trim": 1}  ',      # leading/trailing ws
        '{"nested": {"a": [1, {"b": 2}]}}',
        'garbage {"first": 1} middle {"second": 2} end',  # first { to last }
        '```json\n{"бокс": "круг"}\n```',  # non-ascii keys/values
    ]
    for i, reply in enumerate(corpus):
        py = pm._extract_json(reply)
        rc, data = cpp.call("agent_pm_extract_json", {"reply": reply})
        check(f"(pm) extract_json[{i}]", data.get("plan"), py)


# --------------------------------------------------------------------------- PM: coerce
def gate_pm_coerce(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    cases = [
        # dim
        ({"kind": "dim"}, 5),
        ({"kind": "dim"}, 5.0),
        ({"kind": "dim"}, "10mm"),
        ({"kind": "dim"}, True),                  # bool is an int subclass in py
        ({"kind": "dim"}, {"expr": "a+b"}),
        ({"kind": "dim"}, {"expr": 5}),           # expr not str -> fall through
        ({"kind": "dim"}, {"value": 7}),
        ({"kind": "dim"}, {"value": "x"}),
        ({"kind": "dim"}, {"value": [1]}),        # value not scalar -> None
        ({"kind": "dim"}, [1, 2]),                # list -> None
        ({"kind": "dim"}, None),
        # combo / datum_ref
        ({"kind": "combo", "options": ["a", "b"]}, "a"),
        ({"kind": "combo", "options": ["a", "b"]}, "z"),
        ({"kind": "combo", "options": ["a", "b"]}, 1),
        ({"kind": "datum_ref", "options": ["Plane1"]}, "Plane1"),
        ({"kind": "datum_ref", "options": ["Plane1"]}, "Plane2"),
        ({"kind": "datum_ref"}, "Plane1"),        # no options -> None
        # check
        ({"kind": "check"}, True),
        ({"kind": "check"}, False),
        ({"kind": "check"}, 1),
        ({"kind": "check"}, "true"),
        # int
        ({"kind": "int"}, 4),
        ({"kind": "int"}, 4.0),
        ({"kind": "int"}, 4.5),
        ({"kind": "int"}, True),                  # bool -> None
        ({"kind": "int"}, "4"),
        # selector/feature_well/other -> None
        ({"kind": "selector"}, "anything"),
        ({"kind": "feature_well"}, 5),
        ({"kind": "weird"}, 5),
        ({}, 5),
    ]
    for i, (field, value) in enumerate(cases):
        py = pm._coerce_patch_value(field, value)
        rc, data = cpp.call("agent_pm_coerce", {"field": field, "value": value})
        check(f"(pm) coerce[{i}]", data.get("value"), py)


# --------------------------------------------------------------------------- PM: validate_plan
def _extrude_schema():
    return {
        "type": "extrude",
        "fields": [
            {"key": "depth", "kind": "dim"},
            {"key": "draft_angle", "kind": "dim"},
            {"key": "end", "kind": "combo", "options": ["blind", "up_to_reference"]},
            {"key": "flip", "kind": "check"},
            {"key": "count", "kind": "int"},
            {"key": "plane", "kind": "datum_ref", "options": ["Front", "Top"]},
            {"key": "end_ref", "kind": "selector"},
            {"key": "tool_well", "kind": "feature_well"},
            {"key": "hidden_dim", "kind": "dim", "visible": False},
            {"key": "", "kind": "dim"},          # empty key -> skipped
            "not a dict",                          # skipped
        ],
    }


def gate_pm_validate(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    schema = _extrude_schema()
    current = {"fields": {"end_ref": {"count": 0}, "tool_well": {"count": 2}}}

    plans = [
        # patches to visible non-selector fields (+ a selector patch that's rejected)
        ("good-patches", {
            "answer": "  do it  ",
            "patches": [
                {"key": "depth", "value": 12},
                {"key": "end", "value": "blind"},
                {"key": "flip", "value": True},
                {"key": "count", "value": 3.0},
                {"key": "plane", "value": "Front"},
                {"key": "end_ref", "value": "anything"},   # selector -> rejected
                {"key": "hidden_dim", "value": 9},          # not visible -> rejected
                {"key": "nope", "value": 1},                # unknown -> rejected
                {"key": "depth", "value": [1]},             # bad value -> rejected
            ],
        }),
        # activate_well valid
        ("act-valid", {"activate_well": "end_ref"}),
        # activate_well invalid + empty-selector fallback (no patches/questions)
        ("act-fallback", {"activate_well": "not_a_selector"}),
        # reveal/highlights filtering
        ("reveal-hl", {"reveal_fields": ["depth", "hidden_dim", "ghost"],
                       "highlights": ["end", "tool_well", "x"]}),
        # questions cleaning
        ("questions", {"questions": [
            {"prompt": " Which end? ", "choices": ["a", " b ", "", "c", "d"]},  # ->3 choices
            {"question": "two?", "choices": ["x", "y"]},
            {"prompt": "too few", "choices": ["only"]},   # dropped
            {"prompt": "", "choices": ["a", "b"]},        # dropped (no prompt)
            {"prompt": "fourth", "choices": ["a", "b"]},  # beyond first 3 -> dropped
        ]}),
        # synthesized answer (no answer, has patches)
        ("synth-patch", {"patches": [{"key": "depth", "value": 5}]}),
        # synthesized answer (empty everything) + empty-selector fallback well
        ("synth-empty", {}),
        # synthesized answer with questions present (suppresses well fallback)
        ("synth-q", {"questions": [{"prompt": "p?", "choices": ["a", "b"]}]}),
    ]
    for tag, plan in plans:
        py = pm._validate_plan(plan, schema, current)
        rc, data = cpp.call("agent_pm_validate_plan",
                            {"plan": plan, "form_schema": schema, "current": current})
        check(f"(pm) validate_plan[{tag}]", data.get("validated"), py)


# --------------------------------------------------------------------------- PM: heuristic
def gate_pm_heuristic(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    schema = _extrude_schema()
    current = {"fields": {"end_ref": {"count": 0}, "tool_well": {"count": 2}}}
    non_extrude = {
        "type": "fillet",
        "fields": [{"key": "radius", "kind": "dim"},
                   {"key": "edges", "kind": "selector"}],
    }
    cur2 = {"fields": {"edges": {"count": 0}}}

    cases = [
        ("up-to", schema, current, "Extrude up to the top face"),
        ("offset", schema, current, "offset from reference please"),
        ("from-ref", schema, current, "start from reference plane"),
        ("none-extrude", schema, current, "just make it deep"),  # empties fallback
        ("non-extrude", non_extrude, cur2, "up to something"),    # empties fallback
        ("empty-intent", schema, current, ""),
    ]
    for tag, fs, cur, intent in cases:
        py = pm._heuristic_plan(fs, cur, intent)
        rc, data = cpp.call("agent_pm_heuristic_plan",
                            {"form_schema": fs, "current": cur, "intent": intent})
        check(f"(pm) heuristic_plan[{tag}]", data.get("plan"), py)


# --------------------------------------------------------------------------- PM: build_prompt
def gate_pm_build_prompt(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    doc = _doc_with_features()
    sess = _StubSession(doc, doc_id="doc-pm", revision=2,
                        feature_status=[{"id": "f1", "ok": True}])
    # A non-ASCII doc name proves ensure_ascii=False (literal UTF-8 in the body).
    doc_na = dict(doc)
    doc_na["name"] = "Brückè — ✓"
    sess_na = _StubSession(doc_na, doc_id="doc-pm", revision=2,
                           feature_status=[{"id": "f1", "ok": True}])

    schema = _extrude_schema()
    current = {"fields": {"end_ref": {"count": 0}}}
    params = {"form_schema": schema, "current": current,
              "intent": "up to the face", "selected_refs": [{"ref": "edge1"}]}

    for tag, s in [("ascii", sess), ("nonascii", sess_na)]:
        py_sys, py_msgs = pm.build_pm_assist_prompt(params, s)
        py_body = py_msgs[0]["content"][0]["text"]
        rc, data = cpp.call("agent_pm_build_prompt",
                            {"params": params, "doc": s.doc, "doc_id": s.doc_id,
                             "revision": s.revision,
                             "feature_status": s.last_result.feature_status})
        check(f"(pm) build_prompt[{tag}].system", data.get("system"), py_sys)
        check(f"(pm) build_prompt[{tag}].body", data.get("body"), py_body)


# --------------------------------------------------------------------------- PM: assist e2e
def gate_pm_assist(cpp: CppCore) -> None:
    import agent.pm_assist as pm

    doc = _doc_with_features()
    sess = _StubSession(doc, doc_id="doc-pm", revision=2,
                        feature_status=[{"id": "f1", "ok": True}])
    schema = _extrude_schema()
    current = {"fields": {"end_ref": {"count": 0}, "tool_well": {"count": 2}}}
    params = {"form_schema": schema, "current": current,
              "intent": "extrude up to face", "selected_refs": []}

    # (i) a good JSON plan from the provider.
    good = ('```json\n{"answer": "filled", "patches": [{"key": "depth", "value": 8}],'
            ' "activate_well": "end_ref"}\n```')
    py = pm.assist_property_manager(params, sess, provider=_ScriptedProvider(good))
    rc, data = cpp.call("agent_pm_assist",
                        {"params": params, "doc": doc, "doc_id": "doc-pm", "revision": 2,
                         "feature_status": [{"id": "f1", "ok": True}],
                         "provider": {"name": "mock", "replies": [good]}})
    check("(pm) assist[good-json]", data.get("validated"), py)

    # (ii) junk reply -> heuristic fallback (intent "extrude up to face").
    junk = "I cannot help with that, here is some prose."
    py = pm.assist_property_manager(params, sess, provider=_ScriptedProvider(junk))
    rc, data = cpp.call("agent_pm_assist",
                        {"params": params, "doc": doc, "doc_id": "doc-pm", "revision": 2,
                         "feature_status": [{"id": "f1", "ok": True}],
                         "provider": {"name": "mock", "replies": [junk]}})
    check("(pm) assist[junk-fallback]", data.get("validated"), py)

    # (iii) provider RAISES -> heuristic fallback + " Provider fallback: {err}".
    py = pm.assist_property_manager(params, sess, provider=_ErrProvider("boom"))
    rc, data = cpp.call("agent_pm_assist",
                        {"params": params, "doc": doc, "doc_id": "doc-pm", "revision": 2,
                         "feature_status": [{"id": "f1", "ok": True}],
                         "provider": {"error": "boom"}})
    check("(pm) assist[provider-error]", data.get("validated"), py)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, type=Path)
    args = ap.parse_args()

    cpp = CppCore(args.dll)
    print(f"ABI: {cpp.abi()}")

    gate_ask(cpp)
    gate_pm_extract(cpp)
    gate_pm_coerce(cpp)
    gate_pm_validate(cpp)
    gate_pm_heuristic(cpp)
    gate_pm_build_prompt(cpp)
    gate_pm_assist(cpp)

    print(f"\n  checks passed: {len(_oks)}")
    if _fails:
        print(f"  MISMATCHES: {len(_fails)}\n")
        for f in _fails:
            print("  FAIL " + f)
    else:
        print("  ALL MATCH (ask + pm) — full parity")
    return len(_fails)


if __name__ == "__main__":
    sys.exit(main())
