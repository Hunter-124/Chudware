"""Deterministic reproduction of the carried-forward ECAD bugs (E-1/E-2/E-3 + DFM polish).

Bypasses the LLM provider entirely (provider=None -> recipe path) so the run is fast and
self-contained. Run with the engine venv python from the repo root.

Zero-Python cutover (T-RF20): the ECAD session is the C++ core via the ecad_common
facade; sync/autoroute/DFM are ``ecad_update_from_sch`` / ``ecad_autoroute`` /
``ecad_dfm`` RPCs; the MNA leg is ecad_common._sim_run (C++ sim_spice).
"""
import sys, json, os, tempfile

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from services.ecad_design import orchestrator as orch
from services.mcp.ecad_common import ecad_session, netlist_mod, _sim_run
from services.mcp import core_bridge as cb


def banner(t):
    print("\n" + "=" * 70 + f"\n{t}\n" + "=" * 70)


# ---- E-1 / E-2: RC low-pass AC self-check -------------------------------------
banner("E-1/E-2: rc_lowpass design -> AC sweep self-check")
run = orch.run_design("an RC low-pass filter with a 1kHz cutoff", provider=None)
print("ok:", run.ok, "stop_reason:", run.stop_reason, "recipe:", run.recipe_used)
print("erc:", run.erc)
print("sim:", json.dumps(run.sim, default=str)[:500])
sch_id = run.doc_id

# ---- E-1 standalone: a fresh ecad_simulate('ac') on the SAME schematic --------
banner("E-1 standalone: build netlist -> spice -> AC, does V(OUT) read ~0.707 at fc?")
from services.sim.spice import netlist_to_spice_circuit

sess = ecad_session.get_session(sch_id)
nl = netlist_mod.build_netlist(sess.doc)
orch._remap_pins_by_name(nl)
circ, warns = netlist_to_spice_circuit(nl)
vdevs = [d for d in circ["devices"] if d.get("kind") == "V"]
print("V devices + their params.ac:", [(d["name"], (d.get("params") or {}).get("ac")) for d in vdevs])
study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 20, "fstart": 10.0, "fstop": 1e5}],
         "probes": ["V(OUT)"]}
res = _sim_run(circ, study)
ac = res.get("ac", {})
print("ac keys:", list(ac.keys()))
fs = ac.get("x_f", [])
mag = ac.get("mag", {})
print("mag keys:", list(mag.keys()))
if fs and mag.get("V(OUT)"):
    # |H| at ~1kHz
    voa = mag["V(OUT)"]
    # find nearest to 1000 Hz
    idx = min(range(len(fs)), key=lambda i: abs(fs[i] - 1000.0))
    print(f"|H(1kHz)| ~= {voa[idx]:.4f} (expect 0.707)")
    print(f"|H({fs[0]:.1f}Hz)|={voa[0]:.4f}  |H({fs[-1]:.1f}Hz)|={voa[-1]:.4f}")
else:
    print("!! no AC magnitude trace for V(OUT) -- E-1 NOT fixed")

# ---- E-3: board flow makes an Edge.Cuts outline + to_3d --------------------
banner("E-3: make_board flow -> outline present -> board_to_step")
pcb = ecad_session.new_session("pcb", "E3_board")
sync = cb.call("ecad_update_from_sch", {"board_doc_id": pcb.doc_id, "sch_doc_id": sch_id})
pcb = ecad_session.get_session(pcb.doc_id)
print("footprints after sync:", len(pcb.doc.get("footprints", [])))
try:
    ar = cb.call("ecad_autoroute", {"doc_id": pcb.doc_id, "opts": {}})
    print("autoroute report:", {k: ar.get("report", {}).get(k) for k in ("routed", "failed")})
except Exception as e:
    print("autoroute err:", type(e).__name__, e)

# Replicate the MCP make_board step 3.5: auto outline if none
from services.mcp.ecad_tools import _default_outline_shapes, _outline_bbox
pcb = ecad_session.get_session(pcb.doc_id)
print("outline before:", (pcb.doc.get("outline", {}) or {}).get("shapes"))
if not (pcb.doc.get("outline", {}) or {}).get("shapes"):
    shapes = _default_outline_shapes(pcb.doc)
    ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
print("outline after:", json.dumps((pcb.doc.get("outline", {}) or {}).get("shapes"))[:300])
print("bbox:", _outline_bbox(pcb.doc))

try:
    from services.ecad.threed import board_to_step
    out = os.path.join(tempfile.gettempdir(), "e3_board.step")
    r = board_to_step(pcb.doc, out)
    print("board_to_step OK ->", out, "exists:", os.path.exists(out),
          "vol:", r.get("summary", {}).get("board_volume"))
except Exception as e:
    import traceback; traceback.print_exc()
    print("!! board_to_step FAILED:", type(e).__name__, e)

# ---- DFM: acid_trap on 90-degree router joints + zero-width silk ----------
banner("DFM: acid_trap false-positives on 90deg L-bends + zero-width silk")
dfm_v = cb.call("ecad_dfm", {"doc_id": pcb.doc_id}).get("violations", [])
from collections import Counter
c = Counter(v["rule"] for v in dfm_v)
print("DFM rule counts:", dict(c))
for v in dfm_v[:12]:
    print(f"  [{v['severity']}] {v['rule']}: {v['message']}")
