"""Audit: which library footprints put silk over pads, and does acid_trap fire on 90deg joints.

Zero-Python cutover (T-RF20): the ECAD session is the C++ core via the ecad_common
facade and DFM is the ``ecad_dfm`` RPC (engine.ecad.dfm is deleted).
"""
from collections import Counter

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from mcp_server.ecad_common import ecad_session
from services.ecad.library.starter import _make_footprints
from services.mcp import core_bridge as cb


def _apply(sess, ops):
    return ecad_session.apply_ops(sess, sess.revision, ops)


def _dfm(sess):
    return cb.call("ecad_dfm", {"doc_id": sess.doc_id}).get("violations", [])


print("=== silk_over_pad audit across ALL library footprints ===")
fps = _make_footprints()
bad = []
for fpid in sorted(fps):
    pcb = ecad_session.new_session("pcb", "audit")
    try:
        _apply(pcb, [{"op": "pcb_add_footprint", "fpid": fpid, "pos": [0, 0], "ref": "X1"}])
    except Exception as e:
        print(f"  (place failed {fpid}: {e})"); continue
    v = _dfm(pcb)
    c = Counter(x["rule"] for x in v)
    sop = c.get("silk_over_pad", 0)
    smw = c.get("silk_min_width", 0)
    if sop or smw:
        bad.append((fpid, sop, smw))
        print(f"  {fpid}: silk_over_pad={sop} silk_min_width={smw}")
if not bad:
    print("  (clean — no footprint puts silk over a pad)")

print("\n=== acid_trap on a 90deg L-bend (route on the default GND net) ===")
pcb = ecad_session.new_session("pcb", "acid")
# fresh board has GND net; route an L-bend + an acute (45deg) bend on GND
_apply(pcb, [
    {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "width": 0.25, "p1": [0, 0], "p2": [5, 0]},
    {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "width": 0.25, "p1": [5, 0], "p2": [5, 5]},   # 90deg
    {"op": "pcb_add_track", "net": "GND", "layer": "F.Cu", "width": 0.25, "p1": [5, 5], "p2": [8, 6]},   # ~71deg from vertical
])
v = _dfm(pcb)
for x in v:
    if x["rule"] == "acid_trap":
        print(f"  [{x['severity']}] {x['message']}")
print("  acid_trap count:", sum(1 for x in v if x["rule"] == "acid_trap"),
      "(the 90deg L-bend at (5,0) should NOT be flagged; only the acute joint should)")
