"""cpp_agent_support_parity.py — P52 parity gate: C++ agent SUPPORT layer vs the
Python oracle (agent/orchestrator/ops_emit.py, events.py, roles.py, agent/config.py).

These are the PURE string/file-IO functions ported in P52: ops/script/revert
extraction, the offline guard + provider resolution, run-event persistence, and
role-prompt resolution. Each section drives the C++ DLL diagnostic handlers and
compares to the REAL Python module byte-for-byte (objects parsed, key order
ignored for the jsonl).

Run under the runtime venv (imports the engine + agent packages):

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" \
        tools\cpp_agent_support_parity.py --dll core\build-occt\bin\Release\libaicad.dll

Exit code = number of mismatches (0 = full parity). DO NOT weaken assertions.
"""

from __future__ import annotations

import argparse
import os
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


# --------------------------------------------------------------------------- (a) extract_ops
def gate_extract_ops(cpp: CppCore) -> None:
    from agent.orchestrator import ops_emit

    corpus = [
        '```json\n[{"op":"add_feature"}]\n```',
        '```\n{"ops": [{"op":"x"}]}\n```',
        'prose before\n[{"op":"y"}, {"op":"z"}]\ntrailing',
        '```json\n["has ] bracket", "esc \\" quote"]\n```',
        '```json\n[[1,2],[3,4]]\n```',
        '```json\n{not valid json}\n```',
        'just prose, no ops here at all.',
        '```json\n[1]\n```\n```json\n[2]\n```',
        '```JSON\n[{"op":"upper"}]\n```',
        '```json\n{"ops": "not a list"}\n```\n[42]',
        '',
        '```json\n42\n```',
    ]
    for i, reply in enumerate(corpus):
        py_ops, py_err = ops_emit.extract_ops(reply)
        rc, data = cpp.call("agent_extract_ops", {"reply": reply})
        c_ops = data.get("ops")
        c_err = data.get("error")
        check(f"(a) extract_ops[{i}].ops", c_ops, py_ops)
        check(f"(a) extract_ops[{i}].error", c_err, py_err)


# --------------------------------------------------------------------------- (b) revert+script
def gate_revert_script(cpp: CppCore) -> None:
    from agent.orchestrator import ops_emit

    revert_corpus = [
        '```json\n{"revert": true}\n```',
        '{"revert": true}',
        '{"revert": false}',
        'no verdict here',
        '```json\n{"revert": 1}\n```',  # truthy-but-not-True -> False
        'keep it {"revert": true} inline',
    ]
    for i, reply in enumerate(revert_corpus):
        py = ops_emit.extract_revert(reply)
        rc, data = cpp.call("agent_extract_revert", {"reply": reply})
        check(f"(b) extract_revert[{i}]", data.get("revert"), py)

    script_corpus = [
        '```python\nresult = Box(1,1,1)\n```',
        '```py\nparts = [a, b]\n```',
        '```\nassembly = foo()\n```',  # bare scriptish fence
        '```text\nresult = 1\n```',    # any-tag fallback
        'no script here',
        '```python\nx = 1\n```',        # not scriptish
        '```python\n   result   =   Box()\n```',  # whitespace around =
    ]
    for i, reply in enumerate(script_corpus):
        py = ops_emit.extract_script(reply)
        rc, data = cpp.call("agent_extract_script", {"reply": reply})
        check(f"(b) extract_script[{i}]", data.get("script"), py)


# --------------------------------------------------------------------------- (c) first_balanced
def gate_first_balanced(cpp: CppCore) -> None:
    from agent.orchestrator import ops_emit

    cases = [
        ('foo [1, 2, 3] bar', '[', ']'),
        ('[a, [b, c], d]', '[', ']'),
        ('["a ] in string", 2]', '[', ']'),
        ('["esc \\" quote ] here"]', '[', ']'),
        ('unbalanced [1, 2', '[', ']'),
        ('no brackets', '[', ']'),
        ('{"k": {"n": 1}}', '{', '}'),
        ('{"s": "} brace"}', '{', '}'),
    ]
    for i, (text, o, c) in enumerate(cases):
        py = ops_emit._first_balanced(text, o, c)
        rc, data = cpp.call("agent_first_balanced", {"text": text, "open": o, "close": c})
        check(f"(c) first_balanced[{i}]", data.get("span"), py)


# --------------------------------------------------------------------------- (d) script_escape_op
def gate_script_escape(cpp: CppCore) -> None:
    from agent.orchestrator import ops_emit

    for i, (code, name) in enumerate([("result = Box(1,1,1)", "Script"),
                                      ("parts = []", "Custom")]):
        py = ops_emit.script_escape_op(code, name)
        p = {"code": code, "name": name}
        rc, data = cpp.call("agent_script_escape_op", p)
        check(f"(d) script_escape_op[{i}]", data, py)


# --------------------------------------------------------------------------- (e) host_is_loopback
def gate_host_loopback(cpp: CppCore) -> None:
    from agent import config

    urls = [
        "http://localhost:1234/v1",
        "http://127.0.0.1:8080",
        "https://api.openai.com",
        "http://[::1]:5000",
        "",
        "http://0.0.0.0:1/",
        "http://example.com",
    ]
    for i, u in enumerate(urls):
        py = config._host_is_loopback(u)
        rc, data = cpp.call("agent_host_is_loopback", {"base_url": u})
        check(f"(e) host_is_loopback[{i}={u!r}]", data.get("loopback"), py)


# --------------------------------------------------------------------------- (f) provider_offline_safe
class _StubProvider:
    def __init__(self, name, base_url=None, offline_safe=False):
        self.name = name
        self.base_url = base_url
        self.offline_safe = offline_safe


def gate_offline_safe(cpp: CppCore) -> None:
    from agent import config

    stubs = [
        _StubProvider("mock"),
        _StubProvider("anthropic"),
        _StubProvider("openai", base_url="http://localhost:1234/v1"),
        _StubProvider("openai", base_url=None),
        _StubProvider("openai", base_url="https://api.openai.com/v1"),
        _StubProvider("llamacpp", base_url="http://127.0.0.1:1234/v1"),
        _StubProvider("llamacpp", base_url="http://remote.example.com:1234/v1"),
        _StubProvider("llamacpp", base_url=None),
        _StubProvider("weird", base_url="http://localhost:9/v1"),
        _StubProvider("weird", base_url="http://evil.com/v1"),
        _StubProvider("anything", offline_safe=True),
    ]
    for i, s in enumerate(stubs):
        py = config.provider_offline_safe(s)
        p = {"name": s.name}
        if s.base_url is not None:
            p["base_url"] = s.base_url
        if s.offline_safe:
            p["offline_safe"] = s.offline_safe
        rc, data = cpp.call("agent_offline_safe", {"provider": p})
        check(f"(f) offline_safe[{i}={s.name},{s.base_url}]", data.get("safe"), py)


# --------------------------------------------------------------------------- (g) resolve_provider
def gate_resolve_provider(cpp: CppCore) -> None:
    """Two halves (see docstring):

    1. SAFE descriptors — compared to a HAND-AUTHORED expected descriptor DERIVED
       from agent/config.py::_resolve_provider's documented logic (calling the
       real _resolve_provider would construct an OpenAI() client / probe a server).
    2. OFFLINE REFUSALS — the security-critical path; gated against the REAL
       _resolve_provider RAISING OfflineModeError (it raises BEFORE constructing
       any client, so it is side-effect-free for these combos).
    """
    from agent import config

    # --- 1. SAFE descriptors (no network construction triggered) ----------------
    DLM = config.DEFAULT_LOCAL_MODEL
    DLB = config.DEFAULT_LOCAL_BASE_URL

    def desc(name, model, base_url, vision, api_key, cfg):
        d = {"name": name, "model": model, "base_url": base_url,
             "supports_vision": vision, "api_key": api_key}
        d["offline_safe"] = config.provider_offline_safe(_StubProvider(
            name, base_url=base_url))
        return d

    # (name, cfg, env, expected_descriptor)
    safe_cases = [
        # empty name -> openai-local default
        (None, {}, {"OPENAI_API_KEY": "envkey"},
         {"name": "openai", "model": DLM, "base_url": DLB,
          "supports_vision": True, "api_key": "envkey", "offline_safe": True}),
        (None, {"model": "m1", "openai": {"base_url": "http://localhost:1/v1",
                                          "vision": False},
                "api_key": "cfgkey"}, {},
         {"name": "openai", "model": "m1", "base_url": "http://localhost:1/v1",
          "supports_vision": False, "api_key": "cfgkey", "offline_safe": True}),
        (None, {}, {},
         {"name": "openai", "model": DLM, "base_url": DLB,
          "supports_vision": True, "api_key": "lm-studio", "offline_safe": True}),
        # mock
        ("mock", {}, {},
         {"name": "mock", "model": None, "base_url": None,
          "supports_vision": False, "api_key": None, "offline_safe": True}),
        # anthropic (NOT offline)
        ("anthropic", {"api_key": "sk-ant"}, {},
         {"name": "anthropic", "model": "claude-opus-4-8", "base_url": None,
          "supports_vision": True, "api_key": "sk-ant", "offline_safe": False}),
        ("anthropic", {"model": "claude-x"}, {"ANTHROPIC_API_KEY": "envk"},
         {"name": "anthropic", "model": "claude-x", "base_url": None,
          "supports_vision": True, "api_key": "envk", "offline_safe": False}),
        # openai cloud (no base_url)
        ("openai", {"model": "gpt-4o-mini"}, {"OPENAI_API_KEY": "ek"},
         {"name": "openai", "model": "gpt-4o-mini", "base_url": None,
          "supports_vision": True, "api_key": "ek", "offline_safe": False}),
        # openai with loopback base_url
        ("openai", {"openai": {"base_url": "http://localhost:8/v1", "vision": False}},
         {"OPENAI_API_KEY": "ek"},
         {"name": "openai", "model": "gpt-4o", "base_url": "http://localhost:8/v1",
          "supports_vision": False, "api_key": "ek", "offline_safe": True}),
        # llamacpp
        ("llamacpp", {"model": "ggufm"}, {},
         {"name": "llamacpp", "model": "ggufm", "base_url": DLB,
          "supports_vision": False, "api_key": None, "offline_safe": True}),
        ("llama.cpp", {}, {},
         {"name": "llamacpp", "model": DLM, "base_url": DLB,
          "supports_vision": False, "api_key": None, "offline_safe": True}),
        # unknown -> error
        ("bogus", {}, {}, {"error": "Unknown provider: 'bogus'"}),
    ]
    for i, (name, cfg, env, expected) in enumerate(safe_cases):
        p = {"cfg": cfg, "env": env}
        if name is not None:
            p["name"] = name
        rc, data = cpp.call("agent_resolve_provider", p)
        check(f"(g1) resolve_provider[{i}]", data, expected)

    # --- 2. OFFLINE REFUSALS — gate against the REAL _resolve_provider raising ----
    refusal_cases = [
        ("anthropic", {"offline_mode": True}, {}),
        ("openai", {"offline_mode": True}, {}),  # no loopback base_url -> refused
        ("llamacpp", {"offline_mode": True,
                      "llamacpp": {"base_url": "http://remote.example.com:1234/v1"}}, {}),
    ]
    for i, (name, cfg, env) in enumerate(refusal_cases):
        # Real oracle: must RAISE OfflineModeError (side-effect-free, pre-construct).
        raised = False
        try:
            config._resolve_provider(name, cfg)
        except config.OfflineModeError:
            raised = True
        except Exception as exc:  # pragma: no cover - should not happen
            _fails.append(f"(g2) resolve_provider refusal[{i}] oracle raised {exc!r}")
            continue
        p = {"name": name, "cfg": cfg, "env": env}
        rc, data = cpp.call("agent_resolve_provider", p)
        c_refused = "error" in data and "offline mode is enabled" in data.get("error", "")
        check(f"(g2) resolve_provider refusal[{i}={name}] oracle-raised", raised, True)
        check(f"(g2) resolve_provider refusal[{i}={name}] cpp-refused", c_refused, True)


# --------------------------------------------------------------------------- (h) cap_raw
def gate_cap_raw(cpp: CppCore) -> None:
    from agent.orchestrator import events

    cap = events.RAW_CAP
    for i, n in enumerate([cap - 1, cap, cap + 1]):
        text = "x" * n
        py = events.cap_raw(text)
        rc, data = cpp.call("agent_cap_raw", {"text": text})
        check(f"(h) cap_raw[len={n}]", data.get("capped"), py)
    # falsy / empty
    rc, data = cpp.call("agent_cap_raw", {"text": ""})
    check("(h) cap_raw[empty]", data.get("capped"), events.cap_raw(""))


# --------------------------------------------------------------------------- (i) events
def gate_events(cpp: CppCore) -> None:
    from agent.orchestrator import events

    big_raw = "Z" * (events.RAW_CAP + 100)
    seq = [
        ("intake_start", {"req": "make a box", "n": 1}),
        ("model_reply", {"raw": events.cap_raw(big_raw), "stage": "modeler"}),
        ("done", {"ok": True, "bodies": 3}),
    ]

    # C++ side: one batch (one truncate, three emits) to a fresh run id.
    cpp_run = "cpp-" + uuid.uuid4().hex
    rc, _ = cpp.call("agent_run_events_batch",
                     {"run_id": cpp_run,
                      "events": [{"kind": k, "data": d} for k, d in seq]})
    rc, read = cpp.call("agent_run_events_read", {"run_id": cpp_run})
    c_events = read if isinstance(read, list) else []

    # Python side: a DIFFERENT run id, same kinds/data; read its events.jsonl.
    py_run = "py-" + uuid.uuid4().hex
    re = events.RunEvents(py_run)
    for k, d in seq:
        re.emit(k, **d)
    import json as _json
    py_events = []
    with open(events.run_dir(py_run) / "events.jsonl", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                py_events.append(_json.loads(line))

    check("(i) events count", len(c_events), len(py_events))
    for i, (ce, pe) in enumerate(zip(c_events, py_events)):
        # Each event must carry its OWN run_id (kind first, then run_id, then data).
        check(f"(i) event[{i}].run_id present", ce.get("run_id"), cpp_run)
        # Compare the rest field-by-field (key ORDER ignored — dict ==); the run_id
        # differs by design (two different run ids), so drop it before comparing.
        ce2 = {k: v for k, v in ce.items() if k != "run_id"}
        pe2 = {k: v for k, v in pe.items() if k != "run_id"}
        check(f"(i) event[{i}] parsed-equal", ce2, pe2)
    # The model_reply event must carry the capped (<=16KB+marker) raw.
    if len(c_events) >= 2:
        capped = c_events[1].get("raw", "")
        check("(i) event raw capped", len(capped) <= events.RAW_CAP + 64, True)
        check("(i) event raw == python", c_events[1].get("raw"), py_events[1].get("raw"))


# --------------------------------------------------------------------------- (j) roles
def gate_roles(cpp: CppCore) -> None:
    from agent.orchestrator import roles

    for role in roles.ROLES:
        py_text, py_source = roles.load_prompt(role)
        rc, data = cpp.call("agent_load_prompt", {"role": role, "prompts_dir": PROMPTS_DIR})
        check(f"(j) load_prompt[{role}].text", data.get("text"), py_text)
        check(f"(j) load_prompt[{role}].source-basename",
              os.path.basename(data.get("source", "")), os.path.basename(py_source))
        # prompt_hash parity over the resolved text.
        rc, hd = cpp.call("agent_prompt_hash", {"text": py_text})
        check(f"(j) prompt_hash[{role}]", hd.get("hash"), roles.prompt_hash(py_text))

    # load_cheatsheet for several feature types (incl. an alias + an unknown).
    for ft in ["box", "cylinder", "sphere", "cone", "sketch", "extrude",
               "fillet", "linear_pattern", "circular_pattern", "nope", ""]:
        py = roles.load_cheatsheet(ft)
        rc, data = cpp.call("agent_load_cheatsheet",
                            {"feature_type": ft, "prompts_dir": PROMPTS_DIR})
        check(f"(j) load_cheatsheet[{ft!r}]", data.get("text"), py)

    # get_prompts_v2 structure (role/overridden/text; path basenames).
    py_v2 = roles.get_prompts_v2()
    rc, data = cpp.call("agent_get_prompts_v2", {"prompts_dir": PROMPTS_DIR})
    c_v2 = data if isinstance(data, list) else []
    check("(j) get_prompts_v2 count", len(c_v2), len(py_v2))
    for i, (cr, pr) in enumerate(zip(c_v2, py_v2)):
        check(f"(j) get_prompts_v2[{i}].role", cr.get("role"), pr["role"])
        check(f"(j) get_prompts_v2[{i}].overridden", cr.get("overridden"), pr["overridden"])
        check(f"(j) get_prompts_v2[{i}].text", cr.get("text"), pr["text"])
        check(f"(j) get_prompts_v2[{i}].path-basename",
              os.path.basename(cr.get("path", "")), os.path.basename(pr["path"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, type=Path)
    args = ap.parse_args()

    cpp = CppCore(args.dll)
    print(f"ABI: {cpp.abi()}")

    gate_extract_ops(cpp)
    gate_revert_script(cpp)
    gate_first_balanced(cpp)
    gate_script_escape(cpp)
    gate_host_loopback(cpp)
    gate_offline_safe(cpp)
    gate_resolve_provider(cpp)
    gate_cap_raw(cpp)
    gate_events(cpp)
    gate_roles(cpp)

    print(f"\n  checks passed: {len(_oks)}")
    if _fails:
        print(f"  MISMATCHES: {len(_fails)}\n")
        for f in _fails:
            print("  FAIL " + f)
    else:
        print("  ALL MATCH (a-j) — full parity")
    return len(_fails)


if __name__ == "__main__":
    sys.exit(main())
