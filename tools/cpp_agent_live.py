"""cpp_agent_live.py — drive the C++ core agent (libaicad.dll) end-to-end against the
live local LLM (LM Studio). Creates a doc, runs agent_run (provider resolved from
config.json, offline-aware), then reports the feature tree + mass props + STEP export.

  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_agent_live.py \
        --dll core\build-occt\bin\Release\libaicad.dll "a 40mm cube with a 12mm hole through the center"
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
        self.lib.aicad_free.restype = None
        self.lib.aicad_free.argtypes = [ctypes.c_char_p]
        self.lib.aicad_abi_version.restype = ctypes.c_char_p

    def abi(self) -> str:
        return self.lib.aicad_abi_version().decode()

    def call(self, method: str, params: dict) -> tuple[int, dict]:
        out = ctypes.c_char_p()
        rc = self.lib.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
        data = {}
        if out.value:
            try:
                data = json.loads(out.value.decode())
            finally:
                self.lib.aicad_free(out)
        return rc, data


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, type=Path)
    ap.add_argument("request", nargs="+")
    a = ap.parse_args(argv)
    req = " ".join(a.request)

    core = CppCore(a.dll)
    print("ABI:", core.abi())

    rc, doc = core.call("doc_new", {"kind": "part", "name": "cpp_live"})
    doc_id = doc.get("doc_id") or doc.get("id")
    print("doc_new rc=%d ->" % rc, json.dumps(doc)[:200])
    if not doc_id:
        print("NO DOC ID; abort"); return 2

    print("\n=== agent_run (provider auto-resolved from config.json) ===")
    print("request:", req)
    rc, res = core.call("agent_run", {"request": req, "doc_id": doc_id})
    events = res.pop("events", [])
    print("agent_run rc=%d" % rc)
    print("result:", json.dumps(res, indent=2)[:1500])
    print("\n--- %d events ---" % len(events))
    for ev in events:
        t = ev.get("type") or ev.get("event") or "?"
        # show compact one-liners
        keys = {k: v for k, v in ev.items() if k not in ("type", "event")}
        s = json.dumps(keys)
        print(" ", t, ":", s[:240])

    print("\n=== doc_tree ===")
    rc, tree = core.call("doc_tree", {"doc_id": doc_id})
    print(json.dumps(tree, indent=2)[:1200])

    print("\n=== eval_mass ===")
    rc, mass = core.call("eval_mass", {"doc_id": doc_id})
    print("rc=%d ->" % rc, json.dumps(mass)[:400])

    out_step = str(Path(os.environ["APPDATA"]) / "AI-CAD" / "cache" / "cpp_live.step")
    rc, exp = core.call("doc_export_step", {"doc_id": doc_id, "path": out_step})
    print("\n=== doc_export_step rc=%d ->" % rc, json.dumps(exp)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
