import ctypes, json, os, glob

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
    if out.value:
        dll.aicad_free(out)
    return rc, s

provider = {
    "name": "openai",
    "model": "google/gemma-4-e4b",
    "base_url": "http://localhost:1234/v1",
    "api_key": "",
    "reasoning_effort": "high",
    "context_tokens": 32000,  # >16k: pre-change this would bump local onto the heavy machine path
}

rc, s = call("doc_new", {"kind": "part"})
doc = json.loads(s).get("doc_id") if s else None
print("doc_id=", doc)

req = "a knurled hex standoff, 22mm tall, M3 clearance bore"
rc, s = call("agent_run", {"request": req, "doc_id": doc, "provider": provider})
print("agent_run rc=", rc)
try:
    res = json.loads(s)
    print("FINAL status=", res.get("status"), "stop_reason=", res.get("stop_reason"),
          "engaged=", res.get("engaged"), "  <-- MUST be 'micro' (limited path)",
          "run_id=", res.get("run_id"), "revision=", res.get("revision"))
except Exception as e:
    print("parse err", e, s[:500])
    res = {}

# Replay the per-stage events to see exactly where/why it died.
run_id = res.get("run_id")
runs = os.path.join(os.environ["APPDATA"], "AI-CAD", "cache", "runs")
d = os.path.join(runs, run_id) if run_id else max(glob.glob(os.path.join(runs, "run-*")), key=os.path.getmtime)
print("\n=== events", os.path.basename(d), "===")
for line in open(os.path.join(d, "events.jsonl"), encoding="utf-8"):
    e = json.loads(line)
    k = e.get("kind")
    if k == "agent_output":
        print(f"[{e['stage']} a{e['attempt']}] {e['duration_ms']}ms parsed_ok={e['parsed_ok']} "
              f"parse_error={str(e.get('parse_error'))[:120]}")
    elif k == "stage_result":
        print(f"   -> {e['verdict']}: {str(e.get('reason'))[:140]}")
    elif k == "run_finished":
        print("FINISHED:", e.get("status"), e.get("stop_reason"))

# Dump the raw first-stage reply so we can see what gemma actually returned.
for f in ("attempt_intake_1.txt", "attempt_model_1.txt"):
    p = os.path.join(d, f)
    if os.path.exists(p):
        txt = open(p, encoding="utf-8", errors="replace").read()
        print(f"\n=== {f} (len {len(txt)}) ===\n{txt[:1200]}")
