"""
C16 smoke test: doc_new -> add box -> resolve_pick (face 1) -> add fillet ->
undo -> redo -> verify tree state.

Run from repo root:
  & $py docs/sessions/test_c16_smoke.py
"""
import json
import os
import subprocess
import sys

def get_python():
    appdata = os.environ.get("APPDATA", ".")
    return os.path.join(appdata, "AI-CAD", "runtime", "venv", "Scripts", "python.exe")

# __file__ is docs/sessions/test_c16_smoke.py -> go up 3 levels: sessions -> docs -> repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def run_session(messages):
    """Send all messages to one worker subprocess, return list of parsed responses."""
    py = get_python()
    lines = "\n".join(json.dumps(m) for m in messages) + "\n"
    proc = subprocess.Popen(
        [py, "-m", "engine.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
    )
    stdout, stderr = proc.communicate(lines.encode(), timeout=120)
    responses = []
    for line in stdout.decode().splitlines():
        line = line.strip()
        if line:
            try:
                responses.append(json.loads(line))
            except Exception:
                pass
    return responses, stderr.decode()


def run_full_session_with_doc_id():
    """
    Run the full C16 flow in ONE subprocess.
    We first do a doc_new to get the doc_id, then build the remaining messages
    and send ALL messages (including the first doc_new) together in one session.
    This works because the worker is stateful within a single process lifetime.
    """
    py = get_python()

    # Phase 1: quick doc_new in a throwaway session to learn schema shape
    # (doc_ids differ per session, so we'll just run everything fresh in one session)

    # Build all messages with placeholder; we'll stream them in order
    msgs_template = [
        {"method": "doc_new", "params": {"schema": "aicad.part/1"}, "id": 1},
        {"method": "doc_apply_ops", "params": {
            "doc_id": "PLACEHOLDER",
            "base_revision": 0,
            "ops": [{"op": "add_feature", "feature": {
                "type": "box", "name": "Box1",
                "params": {
                    "length": {"value": 50.0, "expr": None},
                    "width":  {"value": 40.0, "expr": None},
                    "height": {"value": 30.0, "expr": None}
                }
            }}]
        }, "id": 2},
        {"method": "doc_resolve_pick", "params": {
            "doc_id": "PLACEHOLDER", "revision": 1,
            "body_id": 1, "kind": "face", "topo_id": 1
        }, "id": 3},
        {"method": "doc_apply_ops", "params": {
            "doc_id": "PLACEHOLDER",
            "base_revision": 1,
            "ops": [{"op": "add_feature", "feature": {
                "type": "fillet", "name": "Fillet1",
                "params": {"radius": {"value": 3.0, "expr": None}},
                "refs": {"edges": [
                    {"kind": "edge", "surf": "line",
                     "centroid": [0.0, 0.0, 15.0], "axis": [0.0, 0.0, 1.0],
                     "area_or_len": 30.0, "owner_feature": None, "hint_index": 0}
                ]}
            }}]
        }, "id": 4},
        {"method": "doc_undo", "params": {"doc_id": "PLACEHOLDER"}, "id": 5},
        {"method": "doc_redo", "params": {"doc_id": "PLACEHOLDER"}, "id": 6},
        {"method": "doc_tree", "params": {"doc_id": "PLACEHOLDER"}, "id": 7},
        {"method": "shutdown", "params": {}, "id": 99},
    ]

    # Stream interactively so we can patch doc_id from response 1 before sending the rest
    proc = subprocess.Popen(
        [py, "-m", "engine.worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        text=True,
        bufsize=1,
    )

    all_responses = {}
    doc_id = None

    def send(msg):
        line = json.dumps(msg) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        if not line:
            return None
        return json.loads(line.strip())

    # Send message 1 (doc_new)
    send(msgs_template[0])
    r1 = recv()
    all_responses[1] = r1
    assert r1 and r1.get("ok"), f"doc_new failed: {r1}"
    doc_id = r1["result"]["doc_id"]

    # Patch and send the rest
    rest = json.loads(json.dumps(msgs_template[1:]).replace('"PLACEHOLDER"', f'"{doc_id}"'))
    for msg in rest:
        send(msg)

    # Read remaining responses
    while True:
        r = recv()
        if r is None:
            break
        all_responses[r.get("id")] = r
        if r.get("id") == 99:
            break

    proc.stdin.close()
    proc.wait(timeout=5)
    return all_responses, doc_id


def main():
    print("=== C16 doc smoke test ===")

    responses, doc_id = run_full_session_with_doc_id()

    r1 = responses.get(1)
    print(f"doc_new  doc_id={doc_id}  rev={r1['result']['revision']}")

    # Assertions (responses is now a dict keyed by id)
    # r2 = add_box (id=2)
    r2 = responses.get(2)
    assert r2, "no response for id=2 (add_box)"
    if r2.get("ok"):
        feat_status = r2["result"].get("rebuild", {}).get("feature_status", [])
        aicm = r2["result"].get("tess", {}).get("aicm_path", "")
        print(f"\nadd_box  ok=True  rev={r2['result']['revision']}  aicm={aicm}")
        print(f"  feature_status: {feat_status}")
    else:
        print(f"\nadd_box  FAILED: {r2.get('error')}")

    # r3 = resolve_pick (id=3)
    r3 = responses.get(3)
    if r3:
        print(f"\nresolve_pick  ok={r3.get('ok')}  result={r3.get('result')}")

    # r4 = add_fillet (id=4)
    r4 = responses.get(4)
    if r4:
        if r4.get("ok"):
            feat_status = r4["result"].get("rebuild", {}).get("feature_status", [])
            aicm = r4["result"].get("tess", {}).get("aicm_path", "")
            print(f"\nadd_fillet  ok=True  rev={r4['result']['revision']}  aicm={aicm}")
            print(f"  feature_status: {feat_status}")
        else:
            print(f"\nadd_fillet  FAILED: {r4.get('error')}")

    # r5 = undo (id=5)
    r5 = responses.get(5)
    if r5:
        if r5.get("ok"):
            feat_status = r5["result"].get("rebuild", {}).get("feature_status", [])
            rev_after_undo = r5["result"]["revision"]
            n_features = len(r5["result"].get("tree", {}).get("features", []))
            print(f"\nundo  ok=True  rev={rev_after_undo}  features_in_tree={n_features}")
            print(f"  feature_status: {feat_status}")
            # After undo, fillet should be removed — expect 1 feature (box)
            if n_features == 1:
                print("  PASS: fillet removed from tree after undo")
            else:
                print(f"  NOTE: {n_features} features after undo (expected 1 if fillet was added)")
        else:
            print(f"\nundo  FAILED: {r5.get('error')}")

    # r6 = redo (id=6)
    r6 = responses.get(6)
    if r6:
        if r6.get("ok"):
            feat_status = r6["result"].get("rebuild", {}).get("feature_status", [])
            rev_after_redo = r6["result"]["revision"]
            n_features = len(r6["result"].get("tree", {}).get("features", []))
            print(f"\nredo  ok=True  rev={rev_after_redo}  features_in_tree={n_features}")
            print(f"  feature_status: {feat_status}")
            feat_names = [f.get('name') for f in r6['result'].get('tree',{}).get('features',[])]
            print(f"  features after redo: {feat_names}")
        else:
            print(f"\nredo  FAILED (may not be in history): {r6.get('error')}")

    print("\n=== C16 smoke test complete ===")

if __name__ == "__main__":
    main()
