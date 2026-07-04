"""Advanced FEA backends for T-BL04 — fatigue, thermal, buckling, and contact.

Four analysis types beyond the built-in C++ static/modal solvers:

1. **Fatigue life** — pure S-N (Basquin) post-processing on an existing
   ``run_fea_static`` result. NO external solver: it reads the peak von-Mises
   stress a static study already computed and estimates cycles-to-failure from
   the material's endurance limit + ultimate strength (Goodman mean-stress
   correction). Fully deterministic + hand-calc-validated.

2. **Thermal steady-state** — CalculiX ``*HEAT TRANSFER, STEADY STATE``.
3. **Linear buckling** — CalculiX ``*BUCKLE`` eigenvalue (critical load factor).
4. **Nonlinear / contact** — CalculiX ``*STATIC, NLGEOM`` with ``*CONTACT PAIR``.

CalculiX (``ccx``) is **GPL-2.0** → **subprocess-only, PATH-discovered, never
linked or bundled** (contracts/licenses.md, same isolation posture as
ngspice/Renode/QEMU; this generalises the T-BL20 ``bench_calculix`` seam from a
V&V-only cross-check into first-class MCP tools). When ``ccx`` is absent every
CalculiX-backed tool degrades HONESTLY — ``{"ok": False, "skipped": True,
"reason": ...}`` — and NEVER fabricates a result. The fatigue tool needs no
binary and always runs.

The ``.inp`` generators here are CLEAN-ROOM: synthesized from our own geometry
and loads in the Abaqus/CalculiX input syntax; no NAFEMS/ASME/CalculiX example
text is reproduced (same fence as T-BL01/T-BL20).

Validation: see ``scripts/vv_benchmark.py`` FAMILY 10 (thermal 1-D bar, Euler
buckling, Basquin fatigue) — each solver is hand-calc-checked against a cited
closed-form reference with an explicit tolerance tier.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# CalculiX solver discovery: PATH only. No bundled binary, no linked library.
_CCX_NAMES = ("ccx", "ccx_2.20", "ccx_2.21", "ccx_2.19", "calculix")


def ccx_path() -> Optional[str]:
    """Return the CalculiX executable on PATH, or None (subprocess-only discovery)."""
    env = os.environ.get("AI_CAD_CCX")
    if env and shutil.which(env):
        return shutil.which(env)
    for name in _CCX_NAMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def _ccx_absent_result(kind: str, extra: Optional[dict] = None) -> dict:
    """Honest §0-shape degrade when ccx is not installed. Never a fabricated solve."""
    out = {
        "ok": False,
        "skipped": True,
        "reason": (
            f"CalculiX (ccx) not found on PATH — {kind} is a subprocess-only, "
            "PATH-discovered backend (GPL-2.0 isolation per contracts/licenses.md, "
            "same posture as ngspice/Renode/QEMU). Install CalculiX (e.g. `ccx_2.20`) "
            "and put it on PATH, or set AI_CAD_CCX to its full path. The .inp generator "
            "and result parser are exercised without the binary by the unit tests."
        ),
        "backend": "calculix (ccx)",
    }
    if extra:
        out.update(extra)
    return out


def run_ccx(inp_text: str, job: str = "job", timeout: float = 300.0,
            keep_dir: bool = False) -> dict:
    """Run a CalculiX job on a clean-room ``.inp`` string in an isolated temp dir.

    Reusable seam (T-BL17 frequency-response may import this rather than build a
    rival ccx runner). Returns
    ``{ok, returncode, workdir, dat_path, frd_path, sta_path, stdout, stderr}``
    or ``{ok: False, skipped: True, ...}`` when ccx is absent. The caller parses
    the .dat / .frd. ``keep_dir=True`` leaves the temp dir for debugging.
    """
    ccx = ccx_path()
    if not ccx:
        return _ccx_absent_result("this analysis")
    workdir = tempfile.mkdtemp(prefix="aicad_ccx_")
    inp_path = os.path.join(workdir, f"{job}.inp")
    Path(inp_path).write_text(inp_text, encoding="utf-8")
    try:
        proc = subprocess.run(
            [ccx, "-i", job], cwd=workdir, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ccx timed out after {timeout:.0f}s",
                "workdir": workdir, "backend": "calculix (ccx)"}
    except OSError as exc:
        return {"ok": False, "error": f"ccx subprocess error: {exc}",
                "workdir": workdir, "backend": "calculix (ccx)"}
    dat = os.path.join(workdir, f"{job}.dat")
    frd = os.path.join(workdir, f"{job}.frd")
    sta = os.path.join(workdir, f"{job}.sta")
    ok = proc.returncode == 0
    return {
        "ok": ok, "returncode": proc.returncode, "workdir": workdir,
        "dat_path": dat if os.path.exists(dat) else None,
        "frd_path": frd if os.path.exists(frd) else None,
        "sta_path": sta if os.path.exists(sta) else None,
        "stdout": proc.stdout, "stderr": proc.stderr,
        "backend": f"calculix ({os.path.basename(ccx)})",
    }


# =====================================================================
# 1. FATIGUE LIFE — S-N life/FoS post-processing on the C++ fatigue solver
# =====================================================================
#
# The equivalent-fully-reversed amplitude sa_eq (per mean-stress theory) is
# computed by the ENGINE (core/sim/fea_fatigue.cpp `fea_run_fatigue`, RPC
# `sim_run_fatigue`) on the SAME static stress field as run_fea_static — NOT
# reimplemented here (anti-duplication). The engine contract (fea_fatigue.h)
# states the HOST owns Marin correction + reliability + S-N life + factor of
# safety; the desktop app does this in host/src/ui/sim_manager.cpp. The two
# functions below MIRROR that host math EXACTLY so the MCP fatigue tool and the
# GUI never produce divergent fatigue numbers for the same part.
#
# Surface-condition enum (matches sim_manager.cpp marin_ka): 0 ground, 1 machined
# (default), 2 hot-rolled, 3 as-forged. Load case (marin_kb/kc): 0 bending,
# 1 axial, 2 torsion.

_MARIN_KA_A = (1.58, 4.51, 57.7, 272.0)
_MARIN_KA_B = (-0.085, -0.265, -0.718, -0.995)


def marin_ka(Sut: float, surface: int) -> float:
    """Marin surface factor ka = A*Sut^b, capped at 1 (Shigley 6-19). Mirrors host."""
    i = surface if 0 <= surface <= 3 else 1
    ka = _MARIN_KA_A[i] * (Sut ** _MARIN_KA_B[i])
    return min(ka, 1.0)


def marin_kb(d_mm: float, load_case: int) -> float:
    """Marin size factor kb (axial=1; else Shigley 6-20). Mirrors host."""
    if load_case == 1:
        return 1.0
    if d_mm <= 0:
        d_mm = 7.62
    if d_mm < 2.79:
        d_mm = 2.79
    if d_mm <= 51.0:
        return (d_mm / 7.62) ** -0.107
    return 1.51 * (d_mm ** -0.157)


def marin_kc(load_case: int) -> float:
    """Marin load factor kc (0.85 axial, 0.59 torsion, else 1). Mirrors host."""
    return 0.85 if load_case == 1 else (0.59 if load_case == 2 else 1.0)


def marin_kd(T_c: float) -> float:
    """Marin temperature factor kd, interpolated (Shigley 6-21). Mirrors host."""
    Ts = (20.0, 100.0, 200.0, 300.0, 400.0, 500.0, 550.0)
    kd = (1.0, 1.020, 1.025, 0.975, 0.90, 0.825, 0.76)
    if T_c <= Ts[0]:
        return 1.0
    for i in range(1, 7):
        if T_c <= Ts[i]:
            t = (T_c - Ts[i - 1]) / (Ts[i] - Ts[i - 1])
            return kd[i - 1] + t * (kd[i] - kd[i - 1])
    return kd[6]


def reliability_ke(R_pct: float) -> float:
    """Reliability factor ke (Shigley 6-4), interpolated. Mirrors host."""
    R = (50.0, 90.0, 95.0, 99.0, 99.9, 99.99, 99.999)
    ke = (1.000, 0.897, 0.868, 0.814, 0.753, 0.702, 0.659)
    if R_pct <= R[0]:
        return 1.0
    for i in range(1, 7):
        if R_pct <= R[i]:
            t = (R_pct - R[i - 1]) / (R[i] - R[i - 1])
            return ke[i - 1] + t * (ke[i] - ke[i - 1])
    return ke[6]


def sn_life(sa_eq: float, Se: float, Sut: float) -> float:
    """Cycles to failure from an equivalent fully-reversed stress via the Shigley
    high-cycle S-N line (a=(0.9Sut)²/Se, b=-⅓·log10(0.9Sut/Se)); ≥1e6 = runout.

    EXACT mirror of host/src/ui/sim_manager.cpp `sn_life` — do not diverge."""
    if Se <= 0 or Sut <= 0 or sa_eq <= 0:
        return 1e6
    if sa_eq <= Se:
        return 1e6
    f = 0.9
    a = (f * Sut) * (f * Sut) / Se
    b = -(1.0 / 3.0) * math.log10((f * Sut) / Se)
    N = (sa_eq / a) ** (1.0 / b)
    return 1.0 if N < 1 else (1e6 if N > 1e6 else N)


def corrected_endurance(Se_base: float, Sut: float, *, surface: int = 1,
                        load_case: int = 0, diameter_mm: float = 7.62,
                        temperature_c: float = 20.0,
                        reliability_pct: float = 50.0) -> dict:
    """Apply the Marin factors + reliability to an uncorrected endurance limit Se'.
    Returns {se_corrected_mpa, ka, kb, kc, kd, ke}. Mirrors the host correction chain."""
    if Se_base <= 0:
        Se_base = min(0.5 * Sut, 700.0)   # seBase fallback (host)
    ka = marin_ka(Sut, surface)
    kb = marin_kb(diameter_mm, load_case)
    kc = marin_kc(load_case)
    kd = marin_kd(temperature_c)
    ke = reliability_ke(reliability_pct)
    Se = Se_base * ka * kb * kc * kd * ke
    return {"se_corrected_mpa": Se, "se_base_mpa": Se_base,
            "ka": ka, "kb": kb, "kc": kc, "kd": kd, "ke": ke}


def fatigue_summarize(theories: dict, Se: float, Sut: float,
                      theory: str = "goodman") -> dict:
    """Turn the engine's per-theory sa_eq (equivalent fully-reversed amplitude) into a
    life + factor-of-safety for the requested mean-stress theory. Life via `sn_life`
    (host mirror). Returns per-theory {sa_eq_mpa, cycles, regime, fos} plus the
    selected `theory` echoed at the top level.

    theories : {name: {"sa_eq_mpa": float}} exactly as `sim_run_fatigue` returns
               under summary.theories.
    """
    out: dict = {"selected_theory": theory, "se_corrected_mpa": round(Se, 3),
                 "ultimate_mpa": round(Sut, 3), "theories": {}}
    for name, tv in theories.items():
        sa_eq = float(tv.get("sa_eq_mpa", 0.0))
        N = sn_life(sa_eq, Se, Sut)
        regime = "infinite" if (sa_eq <= Se or N >= 1e6) else (
            "static_overload" if sa_eq >= Sut else "finite")
        fos = round(Se / sa_eq, 4) if sa_eq > 0 else None
        out["theories"][name] = {
            "sa_eq_mpa": round(sa_eq, 3),
            "cycles": (None if regime == "infinite" else round(N)),
            "regime": regime,
            "fos_endurance": fos,
        }
    sel = out["theories"].get(theory, {})
    out["cycles"] = sel.get("cycles")
    out["regime"] = sel.get("regime")
    out["fos_endurance"] = sel.get("fos_endurance")
    return out


# =====================================================================
# CalculiX .inp generators — clean-room hex meshers on a box (beam/plate proxy)
# =====================================================================

def _hex_box_nodes_elems(L: float, b: float, h: float,
                         nx: int, ny: int, nz: int) -> tuple[list, list, int, int, int]:
    """Structured C3D8 hex grid on an L×b×h box. Returns (node_lines, elem_lines,
    nnx, nny, nnz) as .inp text fragments. x∈[0,L], y∈[0,b], z∈[0,h]. Clean-room."""
    nnx, nny, nnz = nx + 1, ny + 1, nz + 1

    def nid(i: int, j: int, k: int) -> int:
        return 1 + i + j * nnx + k * nnx * nny

    node_lines = ["*NODE"]
    for k in range(nnz):
        for j in range(nny):
            for i in range(nnx):
                x, y, z = L * i / nx, b * j / ny, h * k / nz
                node_lines.append(f"{nid(i, j, k)}, {x:.6f}, {y:.6f}, {z:.6f}")

    elem_lines = ["*ELEMENT, TYPE=C3D8, ELSET=EALL"]
    eid = 1
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                n1, n2 = nid(i, j, k), nid(i + 1, j, k)
                n3, n4 = nid(i + 1, j + 1, k), nid(i, j + 1, k)
                n5, n6 = nid(i, j, k + 1), nid(i + 1, j, k + 1)
                n7, n8 = nid(i + 1, j + 1, k + 1), nid(i, j + 1, k + 1)
                elem_lines.append(
                    f"{eid}, {n1}, {n2}, {n3}, {n4}, {n5}, {n6}, {n7}, {n8}")
                eid += 1
    return node_lines, elem_lines, nnx, nny, nnz


def _nid_fn(nnx: int, nny: int):
    def nid(i: int, j: int, k: int) -> int:
        return 1 + i + j * nnx + k * nnx * nny
    return nid


def build_thermal_bar_inp(L: float, b: float, h: float, k_cond: float,
                          T_hot: float, T_cold: float,
                          nx: int = 40, ny: int = 3, nz: int = 3) -> dict:
    """1-D steady conduction along x: face x=0 held at T_hot, face x=L at T_cold.
    Clean-room *HEAT TRANSFER, STEADY STATE .inp. Returns {inp, hot_nodes, cold_nodes,
    mid_nodes, expected_mid_T}."""
    node_lines, elem_lines, nnx, nny, nnz = _hex_box_nodes_elems(L, b, h, nx, ny, nz)
    nid = _nid_fn(nnx, nny)
    hot = [nid(0, j, k) for k in range(nnz) for j in range(nny)]
    cold = [nid(nx, j, k) for k in range(nnz) for j in range(nny)]
    imid = nx // 2
    mid = [nid(imid, j, k) for k in range(nnz) for j in range(nny)]
    lines = list(node_lines) + list(elem_lines)
    lines += [
        "*NSET, NSET=NHOT", ", ".join(str(n) for n in hot),
        "*NSET, NSET=NCOLD", ", ".join(str(n) for n in cold),
        "*NSET, NSET=NMID", ", ".join(str(n) for n in mid),
        "*MATERIAL, NAME=MAT",
        "*CONDUCTIVITY", f"{k_cond:.6f}",
        "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
        "*STEP",
        "*HEAT TRANSFER, STEADY STATE",
        "*BOUNDARY",
        f"NHOT, 11, 11, {T_hot:.6f}",
        f"NCOLD, 11, 11, {T_cold:.6f}",
        "*NODE PRINT, NSET=NMID", "NT", "*END STEP",
    ]
    # Linear conduction → mid-plane temperature is the arithmetic mean.
    expected_mid = (T_hot + T_cold) / 2.0
    return {"inp": "\n".join(lines) + "\n", "hot_nodes": hot, "cold_nodes": cold,
            "mid_nodes": mid, "expected_mid_T": expected_mid}


def build_buckling_column_inp(L: float, b: float, h: float, E: float, nu: float,
                              P: float, nx: int = 40, ny: int = 4, nz: int = 4) -> dict:
    """Fixed-free column buckling: base x=0 encastre, axial compressive load -x on the
    x=L end. Clean-room *BUCKLE .inp. Returns {inp, expected_Pcr, tip_nodes} where
    Pcr is the Euler fixed-free critical load; ccx returns the load FACTOR λ so
    Pcr_ccx = λ * P."""
    node_lines, elem_lines, nnx, nny, nnz = _hex_box_nodes_elems(L, b, h, nx, ny, nz)
    nid = _nid_fn(nnx, nny)
    fixed = [nid(0, j, k) for k in range(nnz) for j in range(nny)]
    tip = [nid(nx, j, k) for k in range(nnz) for j in range(nny)]
    per = P / len(tip)
    lines = list(node_lines) + list(elem_lines)
    lines += [
        "*NSET, NSET=NFIX", ", ".join(str(n) for n in fixed),
        "*NSET, NSET=NTIP", ", ".join(str(n) for n in tip),
        "*MATERIAL, NAME=MAT",
        "*ELASTIC", f"{E:.1f}, {nu:.3f}",
        "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
        "*BOUNDARY", "NFIX, 1, 3, 0.0",
        "*STEP",
        "*BUCKLE", "4",
        "*CLOAD",
    ]
    lines += [f"{n}, 1, {-per:.6f}" for n in tip]
    lines += ["*END STEP"]
    # Euler fixed-free (one end clamped, one free): Pcr = π²EI / (2L)²; use the
    # SMALLER of the two cross-section second moments (weakest bending axis).
    I_min = min(b * h ** 3, h * b ** 3) / 12.0
    Pcr = (math.pi ** 2) * E * I_min / ((2.0 * L) ** 2)
    return {"inp": "\n".join(lines) + "\n", "expected_Pcr": Pcr, "tip_nodes": tip,
            "applied_P": P, "I_min": I_min}


def build_contact_block_inp(L: float, b: float, h: float, E: float, nu: float,
                            pressure: float, nx: int = 6, ny: int = 6,
                            nz: int = 4) -> dict:
    """Two stacked blocks in *NLGEOM surface-to-surface contact: lower block base
    fixed (z=0), upper block (z∈[h,2h]) pressed straight down by `pressure` on its
    top face; a *CONTACT PAIR (penalty) transmits the load across the interface at
    z=h. Clean-room Abaqus/CalculiX syntax. Returns
    {inp, top_nodes, base_nodes, applied_load_n}.

    Validation (numeric, ENGINEERING tier): by static equilibrium the vertical
    reaction summed over the fixed base must equal the applied load
    (pressure*L*b) — a conservation check that any correct contact solve satisfies.

    Contact face convention: the DEPENDENT (slave) surface is the node set on the
    upper block's bottom; the INDEPENDENT (master) surface is an element-face
    surface on the lower block's top (CalculiX wants the master as *SURFACE
    TYPE=ELEMENT for node-to-surface contact)."""
    # --- lower block: z∈[0,h], ids 1..n_per, elems 1..e_per ---
    lower_nodes, lower_elems_raw, nnx, nny, nnz = _hex_box_nodes_elems(
        L, b, h, nx, ny, nz)
    nid = _nid_fn(nnx, nny)
    n_per = nnx * nny * nnz
    e_per = nx * ny * nz
    lower_elems = [ln.replace("ELSET=EALL", "ELSET=ELOW") for ln in lower_elems_raw]

    base = [nid(i, j, 0) for j in range(nny) for i in range(nnx)]

    # Lower-block top element faces. For our C3D8 node winding the +z face is the
    # 5-6-7-8 face, which is CalculiX face label S2. The top layer of elements is
    # k=nz-1; element id of the (i,j,k) hex is 1 + i + j*nx + k*nx*ny.
    def eid_of(i: int, j: int, k: int) -> int:
        return 1 + i + j * nx + k * nx * ny
    top_elem_faces = [eid_of(i, j, nz - 1) for j in range(ny) for i in range(nx)]

    # --- upper block: z∈[h,2h], node ids +n_per, elem ids +e_per ---
    upper_nodes = ["*NODE"]
    for k in range(nnz):
        for j in range(nny):
            for i in range(nnx):
                x, y, z = L * i / nx, b * j / ny, h + h * k / nz
                upper_nodes.append(
                    f"{n_per + nid(i, j, k)}, {x:.6f}, {y:.6f}, {z:.6f}")
    upper_elems = ["*ELEMENT, TYPE=C3D8, ELSET=EUP"]
    eid = e_per + 1
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                ns = [nid(i, j, k), nid(i + 1, j, k), nid(i + 1, j + 1, k),
                      nid(i, j + 1, k), nid(i, j, k + 1), nid(i + 1, j, k + 1),
                      nid(i + 1, j + 1, k + 1), nid(i, j + 1, k + 1)]
                ns = [n_per + x for x in ns]
                upper_elems.append(f"{eid}, " + ", ".join(str(n) for n in ns))
                eid += 1
    upper_bot = [n_per + nid(i, j, 0) for j in range(nny) for i in range(nnx)]
    upper_top = [n_per + nid(i, j, nz) for j in range(nny) for i in range(nnx)]

    lines = list(lower_nodes) + lower_elems + upper_nodes + upper_elems
    lines += ["*NSET, NSET=NBASE", ", ".join(str(n) for n in base)]
    # Master (independent): element-face surface on the lower block top (S2 = 5678 face).
    lines.append("*SURFACE, NAME=SLOWTOP, TYPE=ELEMENT")
    lines += [f"{e}, S2" for e in top_elem_faces]
    # Slave (dependent): node surface on the upper block bottom.
    lines.append("*SURFACE, NAME=SUPBOT, TYPE=NODE")
    lines.append(", ".join(str(n) for n in upper_bot))
    lines += [
        "*MATERIAL, NAME=MAT",
        "*ELASTIC", f"{E:.1f}, {nu:.3f}",
        "*SOLID SECTION, ELSET=ELOW, MATERIAL=MAT",
        "*SOLID SECTION, ELSET=EUP, MATERIAL=MAT",
        "*SURFACE INTERACTION, NAME=SI",
        "*SURFACE BEHAVIOR, PRESSURE-OVERCLOSURE=LINEAR",
        f"{E * 10.0:.1f}",
        "*CONTACT PAIR, INTERACTION=SI, TYPE=NODE TO SURFACE",
        "SUPBOT, SLOWTOP",
        "*BOUNDARY", "NBASE, 1, 3, 0.0",
        "*STEP, NLGEOM",
        "*STATIC", "0.1, 1.0",
        "*CLOAD",
    ]
    per = pressure * (L * b) / len(upper_top)
    lines += [f"{n}, 3, {-per:.6f}" for n in upper_top]
    lines += ["*NODE PRINT, NSET=NBASE", "RF", "*END STEP"]
    return {"inp": "\n".join(lines) + "\n", "top_nodes": upper_top,
            "base_nodes": base, "applied_load_n": pressure * L * b}


# =====================================================================
# CalculiX .dat result parsers
# =====================================================================

# CalculiX *NODE PRINT variable → the phrase its .dat result-block header carries.
_DAT_HEADER_KEYWORD = {
    "NT": "temperature",     # *NODE PRINT NT  → "temperatures ... for set ..."
    "U": "displacement",     # *NODE PRINT U   → "displacements ..."
    "RF": "force",           # *NODE PRINT RF  → "forces (reaction) ..."
}


def parse_dat_nodal(dat_path: str, key: str, node_set: list,
                    component: int = 1) -> Optional[float]:
    """Parse a ccx *NODE PRINT .dat block for the variable `key` (CalculiX names:
    "NT" temperature, "U" displacement, "RF" reaction force). Return the averaged
    value of the given 1-based `component` column over `node_set`. None if nothing
    parsed.

    ccx labels the result block with a natural-language header ("temperatures for
    set NMID", "forces (reaction) ..."), NOT the terse variable name, so we match on
    the mapped keyword (with the raw key as a fallback)."""
    want = set(node_set)
    vals: list = []
    kw = _DAT_HEADER_KEYWORD.get(key.upper(), key.lower())
    lines = Path(dat_path).read_text().splitlines()
    i = 0
    while i < len(lines):
        header = lines[i].strip()
        hl = header.lower()
        if (kw in hl or key.upper() in header.upper()) and header:
            # ccx puts one or more blank lines between the header and the data rows;
            # scan forward through the block, skipping blanks, until data STARTS then
            # ends (a blank line after data began, or a non-numeric header line).
            j = i + 1
            got_block = False
            started = False
            while j < len(lines):
                row = lines[j]
                parts = row.split()
                is_data = bool(parts) and parts[0].lstrip("-").isdigit()
                if is_data:
                    started = True
                    try:
                        nid = int(parts[0])
                        col = component  # 1-based column after the node id
                        if nid in want and len(parts) > col:
                            vals.append(float(parts[col]))
                            got_block = True
                    except (ValueError, IndexError):
                        pass
                elif started:
                    break  # data block ended
                j += 1
            if got_block:
                i = j
                continue
        i += 1
    if not vals:
        return None
    return sum(vals) / len(vals)


def parse_buckle_factors(dat_path: str) -> list:
    """Parse the eigenvalue (buckling load factor) list from a ccx *BUCKLE .dat.

    ccx prints a 'B U C K L I N G   F A C T O R   O U T P U T' block with rows
    '<mode>  <factor>'. Return the factors in mode order (lowest first)."""
    factors: list = []
    text = Path(dat_path).read_text()
    lines = text.splitlines()
    in_block = False
    for ln in lines:
        u = ln.upper()
        if "BUCKLING FACTOR" in u.replace("  ", " ").replace("   ", " ") \
                or ("B U C K L I N G" in u):
            in_block = True
            continue
        if in_block:
            parts = ln.split()
            if len(parts) >= 2:
                try:
                    int(parts[0])
                    factors.append(float(parts[1]))
                    continue
                except ValueError:
                    if factors:
                        break
            elif factors:
                break
    return factors
