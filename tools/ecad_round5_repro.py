"""Deterministic reproduction of the carried-forward ECAD bugs (E-1/E-2/E-3 + DFM polish).

Bypasses the LLM provider entirely (provider=None -> recipe path) so the run is fast and
self-contained. Run with the engine venv python from the repo root.
"""
import sys, json, os, tempfile

from services.ecad_design import orchestrator as orch
from engine.ecad import session as ecad_session
from engine.ecad import dfm as dfm_mod
from engine.ecad import rpc as ecad_rpc


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
from engine.ecad import netlist as netlist_mod
from engine.sim.spice import netlist_to_spice_circuit
from engine.sim.circuit import simulate as sim_run

sess = ecad_session.get_session(sch_id)
nl = netlist_mod.build_netlist(sess.doc)
orch._remap_pins_by_name(nl)
circ, warns = netlist_to_spice_circuit(nl)
vdevs = [d for d in circ["devices"] if d.get("kind") == "V"]
print("V devices + their params.ac:", [(d["name"], (d.get("params") or {}).get("ac")) for d in vdevs])
study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 20, "fstart": 10.0, "fstop": 1e5}],
         "probes": ["V(OUT)"]}
res = sim_run(circ, study)
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
sync = ecad_rpc._ecad_update_from_sch({"board_doc_id": pcb.doc_id, "sch_doc_id": sch_id}, lambda _d: None)
print("footprints after sync:", len(pcb.doc.get("footprints", [])))
try:
    ar = ecad_rpc._ecad_autoroute({"doc_id": pcb.doc_id, "opts": {}}, lambda _d: None)
    print("autoroute report:", {k: ar.get("report", {}).get(k) for k in ("routed", "failed")})
except Exception as e:
    print("autoroute err:", type(e).__name__, e)

# Replicate the MCP make_board step 3.5: auto outline if none
from mcp_server.ecad_tools import _default_outline_shapes, _outline_bbox
from engine.ecad import session as _s
def _apply(sess, ops):
    return _s.apply_ops(sess, sess.revision, ops)
print("outline before:", (pcb.doc.get("outline", {}) or {}).get("shapes"))
if not (pcb.doc.get("outline", {}) or {}).get("shapes"):
    shapes = _default_outline_shapes(pcb.doc)
    _apply(pcb, [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
print("outline after:", json.dumps((pcb.doc.get("outline", {}) or {}).get("shapes"))[:300])
print("bbox:", _outline_bbox(pcb.doc))

try:
    from engine.ecad.threed import board_to_step
    out = os.path.join(tempfile.gettempdir(), "e3_board.step")
    r = board_to_step(pcb.doc, out)
    print("board_to_step OK ->", out, "exists:", os.path.exists(out),
          "vol:", r.get("summary", {}).get("board_volume"))
except Exception as e:
    import traceback; traceback.print_exc()
    print("!! board_to_step FAILED:", type(e).__name__, e)

# ---- DFM: acid_trap on 90-degree router joints + zero-width silk ----------
banner("DFM: acid_trap false-positives on 90deg L-bends + zero-width silk")
dfm_v = dfm_mod.run_dfm(pcb.doc)
from collections import Counter
c = Counter(v["rule"] for v in dfm_v)
print("DFM rule counts:", dict(c))
for v in dfm_v[:12]:
    print(f"  [{v['severity']}] {v['rule']}: {v['message']}")
