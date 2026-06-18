"""cpp_netlist_parity.py — Python-vs-C++ SPICE NETLIST PARSER parity.

Parses a set of representative SPICE deck texts with BOTH:
  - Python ORACLE  : engine.sim.spice.parse_spice(deck) -> Circuit dict
  - C++  CANDIDATE : libaicad.dll aicad_call("sim_netlist_parse", {"netlist": deck})

and deep-compares the two Circuit dicts (dict key order ignored; lists ordered;
floats compared with a tiny relative tolerance). Covers value suffixes (1k/1u/1meg),
a .model diode + diode card, V sources with PULSE and SIN/AC, .subckt + X instance
(nested + twice-instantiated), and .param usage.

For two decks it then runs BOTH parsed Circuit dicts through the simulator
(engine.sim.circuit.simulate Python vs core.call("sim_spice", ...) on the
C++-parsed dict) to confirm end-to-end text->result parity.

Run under the venv:
  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_netlist_parity.py

Exit code = number of mismatched checks (0 = full parity).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.cpp_parity import CppCore  # noqa: E402

RTOL = 1e-9


def _num_eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        fa, fb = float(a), float(b)
        if math.isnan(fa) and math.isnan(fb):
            return True
        return abs(fa - fb) <= RTOL * max(abs(fa), abs(fb), 1e-30)
    return False


def _deep_eq(a, b, path: str, diffs: list[str]) -> None:
    """Recursively compare two Circuit-dict fragments; record human-readable diffs."""
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = set(a.keys()), set(b.keys())
        if ka != kb:
            only_a = ka - kb
            only_b = kb - ka
            if only_a:
                diffs.append(f"{path}: keys only in py: {sorted(only_a)}")
            if only_b:
                diffs.append(f"{path}: keys only in cpp: {sorted(only_b)}")
        for k in ka & kb:
            _deep_eq(a[k], b[k], f"{path}.{k}", diffs)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            diffs.append(f"{path}: list len {len(a)}!={len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            _deep_eq(x, y, f"{path}[{i}]", diffs)
        return
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if not _num_eq(a, b):
            diffs.append(f"{path}: {a!r}!={b!r}")
        return
    if a != b:
        diffs.append(f"{path}: {a!r}!={b!r}")


# --------------------------------------------------------------------------- decks
DECKS: list[tuple[str, str]] = [
    ("rlc suffixes", """\
* RLC with value suffixes (1k, 1u, 1meg)
V1 in 0 DC 5
R1 in mid 1k
C1 mid out 1u
L1 out 0 1meg
R2 out 0 4.7k
.end
"""),
    ("diode .model + card", """\
Vs a 0 5
R1 a b 1k
D1 b 0 D1N4148
.model D1N4148 D(IS=2.52n N=1.752 RS=0.568)
.end
"""),
    ("V pulse source", """\
V1 in 0 PULSE(0 5 0 1n 1n 1u 2u)
R1 in 0 50
.end
"""),
    ("V sin + ac", """\
V1 in 0 SIN(0 1 1k 0 0) AC 1 0
R1 in 0 1k
.end
"""),
    ("subckt + X instance", """\
.subckt MYDIV a b
Rp a b 1k
Rn b 0 2k
.ends MYDIV
V1 in 0 10
X1 in out MYDIV
RL out 0 3k
.end
"""),
    ("subckt twice + nested", """\
.subckt LEG a b
Rs a b 1k
.ends LEG
.subckt PAIR p q
X1 p mid LEG
X2 mid q LEG
.ends PAIR
V1 in 0 12
XA in out PAIR
XB out gnd LEG
.end
"""),
    (".param usage", """\
.param RVAL=2200 GAINX=10
V1 in 0 3
R1 in 0 4.7k
.end
"""),
    ("opamp builtin X", """\
V1 in 0 0.2 AC 1 0
RIN in inv 1k
RF inv out 10k
X1 0 inv out OPAMP
.end
"""),
]


def main() -> int:
    dll = REPO / "core" / "build-occt" / "bin" / "Release" / "libaicad.dll"
    if not dll.exists():
        print(f"[netlist-parity] libaicad.dll not found at {dll}")
        return 2
    core = CppCore(dll)
    print(f"[netlist-parity] libaicad ABI={core.abi()}")

    from engine.sim.spice import parse_spice
    from engine.sim.circuit import simulate

    checks = 0
    mism = 0

    parsed_py: dict[str, dict] = {}
    parsed_cpp: dict[str, dict] = {}

    for label, deck in DECKS:
        checks += 1
        py_circ = parse_spice(deck)
        rc, cpp_circ = core.call("sim_netlist_parse", {"netlist": deck})
        parsed_py[label] = py_circ
        parsed_cpp[label] = cpp_circ
        diffs: list[str] = []
        if rc != 0:
            diffs.append(f"cpp rc={rc} {cpp_circ.get('error','')[:50]}")
        else:
            _deep_eq(py_circ, cpp_circ, "circuit", diffs)
        if diffs:
            print(f"  {label:<22} DIFF: {'; '.join(diffs)[:120]}")
            mism += 1
        else:
            nd = len(py_circ.get("nodes", []))
            ndev = len(py_circ.get("devices", []))
            print(f"  {label:<22} MATCH  ({nd} nodes, {ndev} devices)")

    # ---- End-to-end text->result parity on two decks. Parse each deck with the
    # Python oracle AND the C++ parser, then run the Python parsed dict through the
    # Python simulator vs the C++ parsed dict through the C++ sim_spice. Confirms the
    # whole text->Circuit->result pipeline matches.
    e2e = [
        ("e2e divider op", """\
V1 in 0 10
R1 in out 1k
R2 out 0 2k
.end
""", {"analyses": [{"type": "op"}], "probes": ["V(out)", "I(V1)"]}),
        ("e2e rc tran", """\
V1 in 0 PULSE(0 5 0 1n 1n 1 0)
R1 in out 1k
C1 out 0 1u
.end
""", {"analyses": [{"type": "tran", "tstep": 1e-6 * 1000 / 200, "tstop": 5e-3,
                    "uic": True}], "probes": ["V(out)"]}),
    ]
    import numpy as np
    for label, deck, study in e2e:
        checks += 1
        diffs: list[str] = []
        py_circ = parse_spice(deck)
        rc, cpp_circ = core.call("sim_netlist_parse", {"netlist": deck})
        if rc != 0:
            diffs.append(f"cpp parse rc={rc}")
        ro = simulate(py_circ, study)
        rc2, rcpp = core.call("sim_spice", {"circuit": cpp_circ, "study": study})
        if "op" in {a["type"] for a in study["analyses"]}:
            if not (ro.get("convergence", {}).get("op") and rcpp.get("convergence", {}).get("op")):
                diffs.append("op did not converge")
            po, co = ro.get("op", {}), rcpp.get("op", {})
            for pr in study["probes"]:
                pv, cv = po.get(pr), co.get(pr)
                if pv is None or cv is None:
                    diffs.append(f"{pr} missing py={pv} cpp={cv}")
                elif abs(float(pv) - float(cv)) / max(abs(float(pv)), 1e-9) > 1e-6:
                    diffs.append(f"{pr} {pv:.6g}!={cv:.6g}")
        else:  # tran
            if not (ro.get("convergence", {}).get("tran") and rcpp.get("convergence", {}).get("tran")):
                diffs.append("tran did not converge")
            tp = np.asarray(ro["tran"]["x_t"], dtype=np.float64)
            tc = np.asarray(rcpp.get("tran", {}).get("x_t", []), dtype=np.float64)
            if tp.shape != tc.shape:
                diffs.append(f"time grid {tp.shape}!={tc.shape}")
            else:
                for pr in study["probes"]:
                    vp = np.asarray(ro["tran"]["traces"][pr], dtype=np.float64)
                    vc = np.asarray(rcpp["tran"]["traces"][pr], dtype=np.float64)
                    scale = max(float(np.max(np.abs(vp))), 1e-9)
                    dmax = float(np.max(np.abs(vp - vc)))
                    if dmax / scale > 1e-6:
                        diffs.append(f"{pr} max|d|={dmax:.2e}")
        if diffs:
            print(f"  {label:<22} DIFF: {'; '.join(diffs)[:120]}")
            mism += 1
        else:
            print(f"  {label:<22} MATCH  (text->result parity)")

    print(f"\n[netlist-parity] {checks - mism}/{checks} cases at full parity")
    return mism


if __name__ == "__main__":
    raise SystemExit(main())
