"""cpp_agent_e2e.py - end-to-end smoke of the C++ agent across the THREE user workflows:
make a part (agent_run), edit it (agent_run on the same doc), and answer a question
(agent_answer_question). Drives libaicad.dll against the live local LLM (config.json).

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_agent_e2e.py \
        --dll core\build-occt\bin\Release\libaicad.dll
"""
from __future__ import annotations
import argparse, ctypes, json, os, sys
from pathlib import Path


class CppCore:
    def __init__(self, dll_path: Path):
        dll_path = dll_path.resolve()
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(dll_path.parent))
        self.lib = ctypes.CDLL(str(dll_path))
        self.lib.aicad_call.restype = ctypes.c_int
        self.lib.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
        self.lib.aicad_free.argtypes = [ctypes.c_char_p]

    def call(self, method, params):
        out = ctypes.c_char_p()
        self.lib.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
        d = json.loads(out.value.decode()); self.lib.aicad_free(out); return d


def vol(core, did):
    m = core.call("eval_mass", {"doc_id": did})
    return round(m["mass_g"] / 7.85 * 1000, 1) if m.get("mass_g") else None


def feats(core, did):
    t = core.call("doc_tree", {"doc_id": did}).get("tree", {})
    fl = t.get("features", []) if isinstance(t, dict) else []
    return [(f.get("id"), f.get("type")) for f in fl]


def run_design(core, did, req, label):
    print(f"\n{'='*70}\n[{label}] {req}\n{'='*70}")
    res = core.call("agent_run", {"request": req, "doc_id": did})
    print(f"  engaged={res.get('engaged')} status={res.get('status')} "
          f"stop={res.get('stop_reason')} rev={res.get('revision')}")
    # per-step verdicts
    for ev in res.get("events", []):
        if ev.get("kind") == "stage_result" and ev.get("stage") == "execute":
            print(f"    step {ev.get('step_index')}: {ev.get('verdict')} - {ev.get('reason','')[:70]}")
    print(f"  -> features: {feats(core, did)}")
    print(f"  -> volume_mm3: {vol(core, did)}")
    return res


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, type=Path)
    a = ap.parse_args(argv)
    core = CppCore(a.dll)

    did = core.call("doc_new", {"kind": "part", "name": "e2e"})["doc_id"]
    print("doc:", did)

    # 1) MAKE A PART
    run_design(core, did, "a round flange 40mm in diameter and 8mm thick with a 12mm bore "
                          "through the center", "MAKE PART")
    v_part = vol(core, did)

    # 2) MAKE AN EDIT (same doc - exercises the edit bias / looks_like_edit path)
    run_design(core, did, "add a 1mm chamfer around the top outer edge of the flange", "EDIT")
    v_edit = vol(core, did)

    # 3) HELP THE USER (question/answer path)
    q = "What is the outer diameter of this part and how big is the central bore?"
    print(f"\n{'='*70}\n[HELP] {q}\n{'='*70}")
    ans = core.call("agent_answer_question", {"question": q, "doc_id": did})
    print("  answer:", (ans.get("answer") or "")[:600])

    # crude pass/fail signals
    print(f"\n{'#'*70}\nSUMMARY")
    print(f"  part built : {'OK' if v_part else 'FAIL'} (vol={v_part})")
    print(f"  edit applied: {'changed' if v_edit != v_part else 'NO-CHANGE'} (vol {v_part} -> {v_edit})")
    print(f"  help answered: {'OK' if ans.get('answer') else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
