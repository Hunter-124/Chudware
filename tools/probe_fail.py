"""Dump the FULL agent_run response (events, rejected ops, errors) for the cases that
failed or missed in the battery, so we can see exactly what the model emitted and why."""
import ctypes, json, os, sys

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "AI-CAD")
os.add_dll_directory(DIST)
os.environ["AI_CAD_PROMPTS_DIR"] = os.path.join(DIST, "app", "agent", "prompts")
dll = ctypes.CDLL(os.path.join(DIST, "libaicad.dll"))
dll.aicad_call.restype = ctypes.c_int
dll.aicad_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_char_p)]
dll.aicad_free.argtypes = [ctypes.c_char_p]


def call(method, params):
    out = ctypes.c_char_p()
    rc = dll.aicad_call(method.encode(), json.dumps(params).encode(), ctypes.byref(out))
    s = out.value.decode() if out.value else ""
    if out.value:
        dll.aicad_free(out)
    try:
        return rc, (json.loads(s) if s else {})
    except Exception:
        return rc, {"_raw": s[:2000]}


MODEL = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "google/gemma-4-e4b"
PROVIDER = {"name": "openai", "model": MODEL, "base_url": "http://localhost:1234/v1",
            "api_key": "lm-studio", "supports_vision": False}

REQS = {
    "revolve": "Model a simple vase about 60 mm tall as a solid of revolution.",
    "bolt_circle": "Make a 60 mm diameter, 10 mm thick disc with six 5 mm bolt holes on a 40 mm bolt circle.",
}
which = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "revolve"
req = REQS.get(which, which)

print(f"MODEL={MODEL}\nREQUEST: {req}\n" + "=" * 78)
_, r = call("doc_new", {"kind": "part"})
doc = r.get("doc_id")
rc, resp = call("agent_run", {"request": req, "doc_id": doc, "provider": PROVIDER})
print("rc=", rc, "status=", resp.get("status"), "stop=", resp.get("stop_reason"),
      "engaged=", resp.get("engaged"))
if resp.get("error"):
    print("ERROR:", resp.get("error"))
# Show whatever the response carries about steps / ops / events.
for key in ("plan", "steps", "ops", "applied_ops", "rejected", "errors", "last_errors",
            "messages", "events", "timeline", "notes"):
    if key in resp and resp[key]:
        print(f"\n--- {key} ---")
        print(json.dumps(resp[key], indent=1)[:3000])
print("\n--- full top-level keys ---")
print(list(resp.keys()))
# resulting tree
_, tr = call("doc_tree", {"doc_id": doc})
tree = tr.get("tree", {})
feats = tree.get("features", []) if isinstance(tree, dict) else []
print(f"\nRESULT FEATURES ({len(feats)}):")
for f in feats:
    print("  -", f.get("type"), f.get("name"), json.dumps(f.get("refs", {}))[:120])
