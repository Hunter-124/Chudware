"""Live bug-finding battery for the plain-language geometry vocabulary + INTENT features.

Fires natural-language modelling requests at the local LM Studio model through the
STAGED libaicad.dll (real agent_run), then for each request verifies BOTH:
  - DISCOVERY: did the model reach for the intended plain-language feature?
  - GEOMETRY:  did it actually build the right solid? (volume + body count via a forced
               rebuild — "built without error" is NOT enough; a dropped cut fuses
               silently, a stray tool leaves 2 bodies. We assert the geometry.)

This is the empirical "iron out the bugs" harness. Engine failures, discovery misses,
and GEOMETRY failures are reported separately.

Usage:
  python tools/vocab_battery.py                       # core cases
  python tools/vocab_battery.py --model google/gemma-4-26b-a4b
  python tools/vocab_battery.py --hard                 # harder multi-step parts
  python tools/vocab_battery.py --only fillet,hole
"""
import ctypes, json, os, sys, time

DIST = r"C:\Users\nigga\Desktop\AI-opencad\dist\AI-CAD"
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
        return rc, {"_raw": s[:600]}


MODEL = "google/gemma-4-e4b"
if "--model" in sys.argv:
    MODEL = sys.argv[sys.argv.index("--model") + 1]
ONLY = None
if "--only" in sys.argv:
    ONLY = set(sys.argv[sys.argv.index("--only") + 1].split(","))
HARD = "--hard" in sys.argv

PROVIDER = {"name": "openai", "model": MODEL,
            "base_url": "http://localhost:1234/v1", "api_key": "lm-studio",
            "supports_vision": False}


def has_type(feats, *types):
    return any(f.get("type") in types for f in feats)


def n_bodies(resp):
    tess = resp.get("tess")
    if isinstance(tess, dict) and isinstance(tess.get("bodies"), list):
        return len(tess["bodies"])
    return None


# Each case: (tag, request, discovery_pred(feats)->(hit,note), geom_pred(vol,nb,feats)->(ok,note)|None)
def one_solid(vol, nb, fs):
    return (nb == 1 and (vol or 0) > 0,
            f"one solid body, vol>0 (got bodies={nb}, vol={round(vol or 0,1)})")


CORE = [
    ("fillet", "Make a 40 by 30 by 20 mm block and round its top edges by 3 mm.",
     lambda fs: (has_type(fs, "fillet"), "fillet present"), one_solid),
    ("chamfer", "Make a 30 mm cube and put a 2 mm chamfer on its top edges.",
     lambda fs: (has_type(fs, "chamfer"), "chamfer present"), one_solid),
    ("shell", "Make a 50x50x40 mm box and shell it out to 2 mm wall, removing the top face.",
     lambda fs: (has_type(fs, "shell"), "shell present"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 50 * 50 * 40, f"hollow (vol {round(v,0)} < solid 100000)")),
    ("hole", "Make a 40x40x20 mm plate and drill one 8 mm hole through the centre of the top face.",
     lambda fs: (has_type(fs, "hole", "cylinder"), "hole or cut-cylinder"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 40 * 40 * 20, f"drilled (vol {round(v,0)} < solid 32000)")),
    ("bolt_circle", "Make a 60 mm diameter, 10 mm thick disc with six 5 mm bolt holes on a 40 mm bolt circle.",
     lambda fs: (has_type(fs, "bolt_circle"), "bolt_circle intent feature"), one_solid),
    ("sketch_on_face", "Make a 40x40x10 mm base, then add a 12 mm diameter, 15 mm tall cylindrical boss centred on its top face.",
     lambda fs: (has_type(fs, "cylinder", "extrude"), "boss present"),
     lambda v, nb, fs: (nb == 1 and v > 40 * 40 * 10, f"boss added (vol {round(v,0)} > base 16000)")),
    ("revolve", "Model a simple vase about 60 mm tall as a solid of revolution.",
     lambda fs: (has_type(fs, "revolve"), "revolve used"), one_solid),
    ("tube_outer", "Make a tube: 40 mm outer diameter, 28 mm inner bore, 50 mm tall. "
                   "Put a 2 mm chamfer on the outer top edge only.",
     lambda fs: (has_type(fs, "chamfer"), "chamfer present"),
     lambda v, nb, fs: (nb == 1 and v > 0, f"one tube body (bodies={nb})")),
    ("largest_face", "Make a 60 by 40 by 20 mm block and drill an 8 mm hole through the centre of its largest face.",
     lambda fs: (has_type(fs, "hole", "cylinder"), "hole present"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 60 * 40 * 20, f"drilled (vol {round(v,0)} < 48000)")),
]

HARD_CASES = [
    ("shaft_both_ends", "Make a 20 mm diameter, 80 mm long shaft and put a 1.5 mm chamfer on BOTH ends.",
     lambda fs: (has_type(fs, "chamfer"), "chamfer present"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 3.14159 * 100 * 80, f"chamfered shaft (vol {round(v,0)})")),
    ("hole_row", "Make a 100x20x5 mm bar with five 4 mm holes evenly spaced along its length.",
     lambda fs: (has_type(fs, "hole", "linear_pattern", "extrude", "cylinder"), "holes present"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 100 * 20 * 5, f"drilled bar (vol {round(v,0)} < 10000)")),
    ("counterbore", "Make a 50x50x15 mm plate with one counterbored hole through its centre for an M6 cap screw.",
     lambda fs: (has_type(fs, "hole"), "hole intent feature"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 50 * 50 * 15, f"counterbored (vol {round(v,0)})")),
    ("mirror_boss", "Make a 80x40x10 mm plate with two identical 10 mm bosses, one near each short end, "
                    "made symmetric by mirroring.",
     lambda fs: (has_type(fs, "mirror", "extrude", "cylinder", "linear_pattern"), "boss feature present"),
     lambda v, nb, fs: (nb == 1 and v > 80 * 40 * 10, f"plate+bosses (vol {round(v,0)} > base 32000)")),
    ("bracket", "Make an L-bracket: two 60x40 mm flanges 5 mm thick meeting at a right angle, "
                "with a 6 mm mounting hole in each flange.",
     lambda fs: (len(fs) >= 2, "multi-feature bracket"),
     lambda v, nb, fs: (nb == 1 and v > 0, f"one bracket body (bodies={nb})")),
    ("loft", "Loft a 30 mm circle at the bottom to a 20 mm square 40 mm above it.",
     lambda fs: (has_type(fs, "loft"), "loft used"), one_solid),
]

EDGE_CASES = [
    ("hole_side", "Make a 40x40x40 mm cube and drill a 10 mm hole straight through its front face.",
     lambda fs: (has_type(fs, "hole", "cylinder"), "hole on a side face"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 40 * 40 * 40, f"drilled side (vol {round(v,0)} < 64000)")),
    ("fillet_then_shell", "Make a 50x50x30 mm box, round its four vertical edges by 5 mm, "
                          "then shell it to a 2 mm wall opening the top.",
     lambda fs: (has_type(fs, "fillet") and has_type(fs, "shell"), "fillet + shell both present"),
     lambda v, nb, fs: (nb == 1 and 0 < v < 50 * 50 * 30, f"rounded+hollow (vol {round(v,0)})")),
    ("sweep_pipe", "Make a pipe by sweeping a 6 mm diameter circle along an L-shaped path "
                   "(40 mm then a right-angle turn for 30 mm).",
     lambda fs: (has_type(fs, "sweep"), "sweep used"), one_solid),
    ("pattern_row", "Make a 120x25x8 mm bar with one 6 mm boss, then linear-pattern that boss "
                    "into a row of four evenly along the length.",
     lambda fs: (has_type(fs, "linear_pattern"), "linear_pattern used"),
     lambda v, nb, fs: (nb == 1 and v > 120 * 25 * 8, f"bar+4 bosses (vol {round(v,0)} > 24000)")),
    ("mirror_sketch", "Make a 60x40x10 mm plate, add a 10 mm boss near one end with a sketch+extrude, "
                      "then mirror it across the centre plane to the other end.",
     lambda fs: (has_type(fs, "mirror"), "mirror used"),
     lambda v, nb, fs: (nb == 1 and v > 60 * 40 * 10, f"plate+mirrored bosses (vol {round(v,0)} > 24000)")),
    ("rib_gusset", "Make an L-bracket from two 50x30x4 mm flanges and add a triangular rib "
                   "reinforcing the inside corner.",
     lambda fs: (len(fs) >= 2, "bracket + rib attempt"),
     lambda v, nb, fs: (nb == 1 and v > 0, f"one body (bodies={nb})")),
]

EDGE = "--edge" in sys.argv
CASES = CORE + (HARD_CASES if HARD else []) + (EDGE_CASES if EDGE else [])


def run_case(tag, req, disc, geom):
    _, r = call("doc_new", {"kind": "part"})
    doc = r.get("doc_id")
    t0 = time.time()
    rc, resp = call("agent_run", {"request": req, "doc_id": doc, "provider": PROVIDER})
    dt = time.time() - t0
    # Forced rebuild gives us volume + tessellated body count (real geometry, not just tree).
    _, rb = call("doc_rebuild", {"doc_id": doc})
    vol = rb.get("volume") or resp.get("volume") or 0
    nb = n_bodies(rb) if n_bodies(rb) is not None else n_bodies(resp)
    _, tr = call("doc_tree", {"doc_id": doc})
    tree = tr.get("tree", {})
    feats = tree.get("features", []) if isinstance(tree, dict) else []
    # Per-feature compile status lives in rebuild.feature_status (NOT tree.features[].status).
    ferr = []
    fstatus = (rb.get("rebuild", {}) or {}).get("feature_status") or rb.get("feature_status") or []
    name_by_id = {f.get("id"): f.get("name") for f in feats}
    for s in fstatus:
        if isinstance(s, dict) and s.get("ok") is False:
            ferr.append(f"{name_by_id.get(s.get('id'), s.get('id'))}: {s.get('error')}")
    hit, dnote = disc(feats) if feats else (False, "no features")
    gok, gnote = (geom(vol, nb, feats) if (geom and feats) else (True, "n/a"))
    print(f"\n=== [{tag}] ({dt:.0f}s) status={resp.get('status')} bodies={nb} vol={round(vol,1)}")
    for f in feats:
        print(f"      - {f.get('type'):15} {str(f.get('name'))[:16]:16} refs={json.dumps(f.get('refs', {}))[:80]}")
    for e in ferr:
        print("    FEATURE ERROR:", e[:200])
    print(f"    discovery: {'HIT ' if hit else 'MISS'} ({dnote})")
    print(f"    geometry : {'OK  ' if gok else 'BAD '} ({gnote})")
    return {"tag": tag, "status": resp.get("status"), "n": len(feats),
            "ferr": ferr, "hit": hit, "gok": gok}


print(f"MODEL = {MODEL}{'  [HARD]' if HARD else ''}")
print("=" * 78)
results = []
for c in CASES:
    tag = c[0]
    if ONLY and tag not in ONLY:
        continue
    try:
        results.append(run_case(*c))
    except Exception as e:
        print(f"\n=== [{tag}] HARNESS EXCEPTION: {e}")
        results.append({"tag": tag, "status": "exc", "ferr": [str(e)], "hit": False, "gok": False})

print("\n" + "=" * 78 + "\nSUMMARY")
for r in results:
    ok = not r.get("ferr") and r.get("hit") and r.get("gok")
    print(f"  {'OK ' if ok else '** '}{r['tag']:16} status={r.get('status')} feats={r.get('n')} "
          f"engine_err={len(r.get('ferr', []))} disc={'hit' if r.get('hit') else 'MISS'} "
          f"geom={'ok' if r.get('gok') else 'BAD'}")
ef = sum(1 for r in results if r.get("ferr"))
dm = sum(1 for r in results if not r.get("hit"))
gm = sum(1 for r in results if not r.get("gok"))
print(f"\nengine failures: {ef}   discovery misses: {dm}   GEOMETRY failures: {gm}   / {len(results)}")
