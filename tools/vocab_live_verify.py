"""Live end-to-end check of the plain-language geometry vocabulary against the STAGED
libaicad.dll (the one the app loads) — and an optional live run on the local LM Studio
model. Part A drives the real doc_apply_ops RPC (which also runs validate.cpp), proving
the staged DLL accepts: word-query fillet, sketch-on-a-named-face, relational `at`,
query-target hole, and the new bolt_circle feature. Part B asks Gemma to use them.
"""
import ctypes, json, os, sys

DIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", "AI-CAD")
os.add_dll_directory(DIST)
os.environ.setdefault("AI_CAD_PROMPTS_DIR", os.path.join(DIST, "app", "agent", "prompts"))

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
        return rc, json.loads(s) if s else {}
    except Exception:
        return rc, {"_raw": s[:400]}


def dim(v):
    return {"value": v, "expr": None}


def new_doc():
    _, r = call("doc_new", {"kind": "part"})
    return r.get("doc_id")


def feat(ftype, name, params=None, refs=None, **extra):
    f = {"type": ftype, "name": name, "params": params or {}, "refs": refs or {}}
    f.update(extra)
    return {"op": "add_feature", "feature": f}


def apply(doc, ops):
    return call("doc_apply_ops", {"doc_id": doc, "ops": ops})


def statuses_ok(resp):
    """Pull per-feature statuses out of whatever shape the response uses."""
    tree = resp.get("tree", resp)
    feats = (tree.get("features") if isinstance(tree, dict) else None) or []
    fs = resp.get("feature_status") or resp.get("statuses") or []
    return feats, fs


def scenario(title, ops):
    doc = new_doc()
    rc, resp = apply(doc, ops)
    err = resp.get("error")
    # collect any per-feature error text
    bad = []
    for s in (resp.get("feature_status") or resp.get("statuses") or []):
        if isinstance(s, dict) and s.get("ok") is False:
            bad.append(s.get("error", "?"))
    ok = (rc == 0) and not err and not bad
    print(f"[{'PASS' if ok else 'FAIL'}] {title}")
    if err:
        print("        error:", err)
    for b in bad:
        print("        feature error:", b)
    if not ok and "_raw" in resp:
        print("        raw:", resp["_raw"])
    return ok


print("PROMPTS_DIR =", os.environ.get("AI_CAD_PROMPTS_DIR"))
print("=" * 72)
print("PART A — plain-language vocabulary through the staged DLL (doc_apply_ops)")
print("=" * 72)

results = []

# 1) word-query fillet: round the TOP edges of a block.
results.append(scenario(
    "fillet the top edges (side:'top')",
    [feat("box", "Block", {"length": dim(40), "width": dim(30), "height": dim(20)}),
     feat("fillet", "TopRound", {"radius": dim(3)}, {"edges": {"of": "edge", "side": "top"}})]))

# 2) sketch ON the top face (word query) + boss extrude.
results.append(scenario(
    "sketch on the top face + boss",
    [feat("box", "Block", {"length": dim(40), "width": dim(30), "height": dim(20)}),
     feat("sketch", "OnTop", {"entities": [
            {"id": "e1", "type": "circle", "construction": False, "center": [0, 0], "radius": 6}],
          "constraints": []},
          {"plane": {"of": "face", "side": "top"}}),
     feat("extrude", "Boss", {"mode": "boss", "end": "blind", "depth": dim(5)},
          {"profile": [{"kind": "region", "sketch": "$1", "centroid": [0, 0], "hint_index": 0}]})]))

# 3) bolt_circle intent feature: 6x M4 on a 30mm circle on the top face.
results.append(scenario(
    "bolt_circle (6x on top face)",
    [feat("box", "Plate", {"length": dim(60), "width": dim(60), "height": dim(10)}),
     feat("bolt_circle", "Bolts", {"diameter": dim(4), "count": 6, "pcd": dim(30),
                                    "end": "through_all"},
          {"face": {"of": "face", "side": "top"}})]))

# 4) hole with a word-query face + a centre position (no sketch needed).
results.append(scenario(
    "hole on the top face (word query)",
    [feat("box", "Block", {"length": dim(40), "width": dim(40), "height": dim(20)}),
     feat("hole", "Bore", {"style": "simple", "standard": "custom",
                            "diameter": dim(8), "end": "through_all"},
          {"face": {"of": "face", "side": "top"}})]))

# 5) relational placement: a cut cylinder centred on the top face.
results.append(scenario(
    "relational at (cut on top face)",
    [feat("box", "Block", {"length": dim(40), "width": dim(30), "height": dim(20)}),
     feat("cylinder", "Bore", {"radius": dim(3), "height": dim(60)},
          refs=None, mode="cut", at={"of": "face", "side": "top"})]))

print("-" * 72)
print(f"PART A: {sum(results)}/{len(results)} scenarios passed")

# ---------------------------------------------------------------- PART B (live Gemma)
if "--gemma" in sys.argv:
    print("=" * 72)
    print("PART B — live run on LM Studio google/gemma-4-e4b")
    print("=" * 72)
    provider = {"name": "openai", "model": "google/gemma-4-e4b",
                "base_url": "http://localhost:1234/v1", "api_key": "lm-studio",
                "supports_vision": False}
    doc = new_doc()
    req = sys.argv[sys.argv.index("--gemma") + 1] if len(sys.argv) > sys.argv.index("--gemma") + 1 \
        else "Make a 40 by 30 by 20 mm block and round its top edges by 3 mm."
    print("request:", req)
    rc, resp = call("agent_run", {"request": req, "doc_id": doc, "provider": provider})
    print("agent_run rc =", rc)
    print("status =", resp.get("status"), "| stop_reason =", resp.get("stop_reason"),
          "| engaged =", resp.get("engaged"))
    if resp.get("error"):
        print("error:", resp["error"])
    # dump the ops the model emitted, and the resulting feature tree
    _, tree = call("doc_tree", {"doc_id": doc})
    t = tree.get("tree", {})
    feats = t.get("features", []) if isinstance(t, dict) else []
    print(f"resulting features ({len(feats)}):")
    for f in feats:
        print("   -", f.get("type"), f.get("name"),
              "| refs:", json.dumps(f.get("refs", {}))[:120])
