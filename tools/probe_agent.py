import ctypes, json, os, sys

dist = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "AI-CAD")
os.add_dll_directory(dist)
os.environ.setdefault("AI_CAD_PROMPTS_DIR", os.path.join(dist, "app", "agent", "prompts"))

dll = ctypes.CDLL(os.path.join(dist, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]

def call(method, params):
    out = ctypes.c_char_p()
    rc = dll.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    s = out.value.decode() if out.value else ""
    # free
    if out.value:
        dll.aicad_free(out)
    return rc, s

print("PROMPTS_DIR=", os.environ.get("AI_CAD_PROMPTS_DIR"))
rc, s = call("doc_new", {"kind": "part"})
print("doc_new rc=", rc, s[:200])
doc = json.loads(s).get("doc_id") if s else None
print("doc_id=", doc)

rc, s = call("agent_run", {"request": "a bracket with a filleted top edge", "doc_id": doc})
print("agent_run rc=", rc)
print("agent_run out=", s[:2000])

# tail: final status + get_prompts_v2 shape
try:
    res = json.loads(s)
    print("FINAL status=", res.get("status"), "stop_reason=", res.get("stop_reason"), "engaged=", res.get("engaged"))
except Exception as e:
    print("parse err", e)
rc, s = call("get_prompts_v2", {})
print("get_prompts_v2 rc=", rc)
try:
    pr = json.loads(s)
    arr = pr.get("prompts")
    print("prompts type=", type(arr).__name__, "n=", len(arr) if isinstance(arr,list) else None)
    if isinstance(arr, list) and arr:
        r0 = arr[0]
        print("rec0 keys=", list(r0.keys()))
        for k in ("role","path","override_path","overridden","text"):
            print("   ", k, "=", repr(r0.get(k))[:60], "(null!)" if r0.get(k) is None else "")
except Exception as e:
    print("prompts parse err", e, s[:300])
