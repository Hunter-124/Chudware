"""Fatigue post-processing and CalculiX contact cross-check support.

Thermal and buckling studies use the built-in C++ solvers.

1. **Fatigue life** — pure S-N (Basquin) post-processing on an existing
   ``run_fea_static`` result. NO external solver: it reads the peak von-Mises
   stress a static study already computed and estimates cycles-to-failure from
   the material's endurance limit + ultimate strength (Goodman mean-stress
   correction). Fully deterministic + hand-calc-validated.

2. **Contact cross-check** — CalculiX ``*STATIC, NLGEOM`` with ``*CONTACT PAIR``.

CalculiX (``ccx``) is **GPL-2.0** → **subprocess-only, PATH-discovered, never
linked or bundled** (contracts/licenses.md, same isolation posture as
ngspice/Renode/QEMU; this generalises the ``bench_calculix`` seam from a
V&V-only cross-check into first-class MCP tools). When ``ccx`` is absent every
CalculiX-backed tool degrades HONESTLY — ``{"ok": False, "skipped": True,
"reason": ...}`` — and NEVER fabricates a result. The fatigue tool needs no
binary and always runs.

The ``.inp`` generators here are CLEAN-ROOM: synthesized from our own geometry
and loads in the Abaqus/CalculiX input syntax; no NAFEMS/ASME/CalculiX example
text is reproduced (same clean-room fence as the V&V benchmarks).

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


class _CcxWorkdirOwner:
    """Lifetime owner for an unretained successful CalculiX work directory."""

    def __init__(self, path: str):
        self.path = path
        self._remove = shutil.rmtree
        self._cleaned = False

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._remove(self.path, ignore_errors=True)

    def __del__(self):
        self.cleanup()


class _OwnedCcxPath(str):
    """Path whose lifetime keeps its containing CCX directory available to parsers."""

    def __new__(cls, path: str, owner: _CcxWorkdirOwner):
        value = super().__new__(cls, path)
        value._ccx_owner = owner
        return value

def _finite(value, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number, got {value!r}") from None
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return out


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    try:
        out = int(value)
        exact = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from None
    if out <= 0 or not math.isfinite(exact) or exact != out:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return out

# CalculiX solver discovery: PATH only. No bundled binary, no linked library.
_CCX_NAMES = ("ccx", "ccx_2.20", "ccx_2.21", "ccx_2.19", "calculix")

# ---------------------------------------------------------------------------
# Layered-timeout contract: the INNER solver clock must be strictly SHORTER
# than the OUTER harness clock — never equal to it.
# ---------------------------------------------------------------------------
# Two clocks bound every harness ``run_fea_*`` call, and they do NOT start
# together:
#   OUTER  services/agent/agent_turn.py::_dispatch_tool runs the tool on a
#          ThreadPoolExecutor and gives up at ``fut.result(timeout=timeout_f)``,
#          where ``timeout_f`` is the tool's services/agent/tool_guidance/sim.py
#          ``timeout_s``. That clock starts the instant the tool is DISPATCHED.
#   INNER  ``run_ccx``'s ``subprocess.run(timeout=...)`` clock starts strictly
#          LATER — after face-query resolution, ``_autosave_all_open()``, STEP
#          export, the second-order tet mesh build, and tempdir/deck setup have
#          already spent outer-clock seconds.
# Both were 300 s, i.e. EQUAL, so the outer deadline ALWAYS fired first: the
# actionable ``{"ok": False, "error": "ccx timed out after Ns"}`` branch in
# ``run_ccx`` below (which tells the model to try a coarser mesh) was
# UNREACHABLE through the harness, and a solve that needed slightly over five
# minutes could only ever surface the outer "abandoned in the background, may
# still be running" payload — which names no remedy at all.
#
# Arithmetic, naming both clocks:
#   CCX_SETUP_MARGIN_S    = 120 s — the pre-solve share of the OUTER clock that
#       the INNER clock cannot see. Clean-room deck generation itself is ~1 ms
#       (build_contact_block_inp measured 0.9 ms on the 7600X3D), so the
#       margin is sized for what actually dominates — the mesh build and the
#       autosave/STEP round trip. A cold bench mesh is 0.982 s and a 54k-DOF
#       modal assembly 2.0 s (simsuite bench measurements, same box),
#       so 120 s covers pre-solve work two orders of magnitude heavier than the
#       bench parts.
#   HARNESS_CCX_TIMEOUT_S = 420 - 120 = 300 s — the solver budget the harness
#       hands ``run_ccx``. It lands on exactly the 300 s that ``run_ccx``'s own
#       public default already gave, so this change cannot shorten a solve that
#       used to fit; it only stops the outer clock from stealing the diagnostic.
#   HARNESS_FEA_TIMEOUT_S = 420 s — the OUTER ceiling every SINGLE-SOLVE
#       ``run_fea_*`` entry in services/agent/tool_guidance/sim.py carries. A
#       multi-leg member (``run_fea_convergence`` solves the runbook's three-mesh
#       ladder under ONE dispatch) resolves its setup once and then spends one
#       inner clock per leg, so its ceiling is legs*HARNESS_CCX_TIMEOUT_S +
#       CCX_SETUP_MARGIN_S — the margin is shared, only the solver term scales
#       (sim.py's ``FEA_SOLVER_LEGS``/``fea_timeout_s``).
# The invariant to preserve is that the inner clocks a tool may burn stay
# strictly inside its outer ceiling by CCX_SETUP_MARGIN_S;
# services/mcp/test_fea_ccx_advanced.py pins the guidance table and these
# constants in lockstep so the two clocks cannot drift back to equality.
HARNESS_FEA_TIMEOUT_S: float = 420.0
CCX_SETUP_MARGIN_S: float = 120.0
HARNESS_CCX_TIMEOUT_S: float = HARNESS_FEA_TIMEOUT_S - CCX_SETUP_MARGIN_S


def ccx_path() -> Optional[str]:
    """Return the CalculiX executable on PATH, or None (subprocess-only discovery)."""
    env = os.environ.get("CHUDWARE_CCX")
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
            "and put it on PATH, or set CHUDWARE_CCX to its full path. The .inp generator "
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

    Reusable seam (frequency-response may import this rather than build a
    rival ccx runner). Returns
    ``{ok, returncode, workdir, dat_path, frd_path, sta_path, stdout, stderr}``
    or ``{ok: False, skipped: True, ...}`` when ccx is absent. The caller parses
    the .dat / .frd. By default successful result paths own the directory until
    the paths/result leave scope, so existing post-return parsers can finish and
    the directory is then removed. Errors are cleaned before return.
    ``keep_dir=True`` is the only mode that retains and reports the workdir.

    ``timeout`` stays 300 s for DIRECT callers (benchmarks, scripts/vv_benchmark.py,
    the GUI worker seam in services/sim/multiphysics_rpc.py) — none of them run
    under an outer harness deadline. Agent-harness call sites must instead pass
    ``timeout=HARNESS_CCX_TIMEOUT_S`` so the inner clock stays strictly inside
    ``HARNESS_FEA_TIMEOUT_S`` and the "timed out" branch below is reachable; see
    the layered-timeout block above.
    """
    if not isinstance(inp_text, str) or not inp_text.strip():
        raise ValueError("inp_text must be a non-empty CalculiX deck")
    if not isinstance(job, str) or not job or any(
            not (c.isalnum() or c in "_-") for c in job):
        raise ValueError("job may contain only letters, digits, '_' and '-'")
    timeout = _finite(timeout, "timeout")
    if timeout <= 0.0:
        raise ValueError("timeout must be > 0 seconds")
    if not isinstance(keep_dir, bool):
        raise ValueError("keep_dir must be a boolean")
    ccx = ccx_path()
    if not ccx:
        return _ccx_absent_result("this analysis")
    workdir = tempfile.mkdtemp(prefix="chudware_ccx_")
    owner: Optional[_CcxWorkdirOwner] = None
    try:
        inp_path = os.path.join(workdir, f"{job}.inp")
        Path(inp_path).write_text(inp_text, encoding="utf-8")
        try:
            proc = subprocess.run(
                [ccx, "-i", job], cwd=workdir, capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"ccx timed out after {timeout:.0f}s",
                    "workdir": workdir if keep_dir else None,
                    "retained": keep_dir, "backend": "calculix (ccx)"}
        except OSError as exc:
            return {"ok": False, "error": f"ccx subprocess error: {exc}",
                    "workdir": workdir if keep_dir else None,
                    "retained": keep_dir, "backend": "calculix (ccx)"}
        dat = os.path.join(workdir, f"{job}.dat")
        frd = os.path.join(workdir, f"{job}.frd")
        sta = os.path.join(workdir, f"{job}.sta")
        ok = proc.returncode == 0

        def _nonempty(path: str) -> Optional[str]:
            # ccx 2.23 exits 0 on an unparseable deck (only *WARNING "card image
            # cannot be interpreted" on stdout, "Job finished") and still touches
            # a ZERO-BYTE .dat. A 0-byte file is never a result.
            try:
                return path if os.path.getsize(path) > 0 else None
            except OSError:
                return None

        paths = [_nonempty(dat), _nonempty(frd), _nonempty(sta)]
        if not ok and not keep_dir:
            # Solver-error artifacts are diagnostics, not parseable result paths.
            paths = [None, None, None]
        elif ok and not keep_dir and any(paths):
            owner = _CcxWorkdirOwner(workdir)
            paths = [_OwnedCcxPath(path, owner) if path is not None else None
                     for path in paths]

        return {
            "ok": ok, "returncode": proc.returncode,
            "workdir": workdir if keep_dir else None,
            "retained": keep_dir,
            "dat_path": paths[0],
            "frd_path": paths[1],
            "sta_path": paths[2],
            "stdout": proc.stdout, "stderr": proc.stderr,
            "backend": f"calculix ({os.path.basename(ccx)})",
        }
    finally:
        # A successful unretained run transfers ownership to its result path(s);
        # every other unretained exit cleans synchronously, including exceptions.
        if not keep_dir and owner is None:
            shutil.rmtree(workdir, ignore_errors=True)


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
    """Marin surface factor ka = A*Sut^b, capped at 1 (Shigley 6-19)."""
    Sut = _finite(Sut, "Sut")
    if Sut <= 0.0:
        raise ValueError("Sut must be > 0 MPa")
    if surface not in range(4):
        raise ValueError("surface must be 0..3")
    ka = _MARIN_KA_A[surface] * (Sut ** _MARIN_KA_B[surface])
    return min(ka, 1.0)


def marin_kb(d_mm: float, load_case: int) -> float:
    """Marin size factor kb (axial=1; else Shigley 6-20)."""
    d_mm = _finite(d_mm, "diameter_mm")
    if load_case not in (0, 1, 2):
        raise ValueError("load_case must be 0 (bending), 1 (axial), or 2 (torsion)")
    if d_mm <= 0.0:
        raise ValueError("diameter_mm must be > 0")
    if load_case == 1:
        return 1.0
    d_eff = max(d_mm, 2.79)
    if d_eff <= 51.0:
        return (d_eff / 7.62) ** -0.107
    return 1.51 * (d_eff ** -0.157)


def marin_kc(load_case: int) -> float:
    """Marin load factor kc (0.85 axial, 0.59 torsion, else 1)."""
    if load_case not in (0, 1, 2):
        raise ValueError("load_case must be 0 (bending), 1 (axial), or 2 (torsion)")
    return 0.85 if load_case == 1 else (0.59 if load_case == 2 else 1.0)


def marin_kd(T_c: float) -> float:
    """Marin temperature factor kd, interpolated (Shigley 6-21). Mirrors host."""
    T_c = _finite(T_c, "temperature_c")
    if not 20.0 <= T_c <= 550.0:
        raise ValueError("temperature_c must be in the supported range 20..550")
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
    R_pct = _finite(R_pct, "reliability_pct")
    if not 50.0 <= R_pct <= 99.999:
        raise ValueError("reliability_pct must be in the supported range 50..99.999")
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
    sa_eq = _finite(sa_eq, "sa_eq")
    Se = _finite(Se, "Se")
    Sut = _finite(Sut, "Sut")
    if Se <= 0.0 or Sut <= 0.0:
        raise ValueError("Se and Sut must both be > 0 MPa")
    if Se >= 0.9 * Sut:
        raise ValueError("Se must be below 0.9*Sut for the Shigley high-cycle S-N line")
    if sa_eq <= 0.0:
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
    Se_base = _finite(Se_base, "Se_base")
    Sut = _finite(Sut, "Sut")
    if Se_base <= 0.0 or Sut <= 0.0:
        raise ValueError("Se_base and Sut must both be > 0 MPa")
    ka = marin_ka(Sut, surface)
    kb = marin_kb(diameter_mm, load_case)
    kc = marin_kc(load_case)
    kd = marin_kd(temperature_c)
    ke = reliability_ke(reliability_pct)
    Se = Se_base * ka * kb * kc * kd * ke
    if not math.isfinite(Se) or Se <= 0.0 or Se >= 0.9 * Sut:
        raise ValueError(
            "corrected endurance must be finite, positive, and below 0.9*Sut for "
            "the Shigley high-cycle S-N line")
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
    if not isinstance(theories, dict) or not theories:
        raise ValueError("theories must be a non-empty result mapping")
    Se = _finite(Se, "Se")
    Sut = _finite(Sut, "Sut")
    if Se <= 0.0 or Sut <= 0.0:
        raise ValueError("Se and Sut must be > 0 MPa")
    if theory not in theories:
        raise ValueError(f"requested theory {theory!r} is absent")
    out: dict = {"selected_theory": theory, "se_corrected_mpa": round(Se, 3),
                 "ultimate_mpa": round(Sut, 3), "theories": {}}
    for name, tv in theories.items():
        if not isinstance(tv, dict) or "sa_eq_mpa" not in tv:
            raise ValueError(f"theory {name!r} is missing sa_eq_mpa")
        sa_eq = _finite(tv["sa_eq_mpa"], f"theories.{name}.sa_eq_mpa")
        if sa_eq <= 0.0:
            raise ValueError(f"theory {name!r} returned non-positive equivalent stress")
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
    L, b, h = (_finite(L, "L"), _finite(b, "b"), _finite(h, "h"))
    if min(L, b, h) <= 0.0:
        raise ValueError("L, b, and h must all be > 0")
    nx, ny, nz = (_positive_int(nx, "nx"), _positive_int(ny, "ny"),
                  _positive_int(nz, "nz"))
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


def _set_lines(ids) -> list:
    """CalculiX rejects a data line with more than 16 comma-separated entries
    (splitline error 201). NSET / node-*SURFACE data therefore must be wrapped to
    <=16 ids per line — a single ", ".join() overflows the moment a face carries
    more than 16 nodes (e.g. a full end face at ny=nz=4 → 25 nodes). Return one
    formatted line per chunk of 16."""
    ids = list(ids)
    return [", ".join(str(n) for n in ids[i:i + 16]) for i in range(0, len(ids), 16)]




def build_contact_block_inp(L: float, b: float, h: float, E: float, nu: float,
                            pressure: float, nx: int = 6, ny: int = 6,
                            nz: int = 4) -> dict:
    """Two stacked blocks in *NLGEOM surface-to-surface contact: lower block base
    fixed (z=0), upper block (z∈[h,2h]) pressed straight down by `pressure` on its
    top face; a *CONTACT PAIR (penalty) transmits the load across the interface at
    z=h. Clean-room Abaqus/CalculiX syntax. Returns
    {inp, top_nodes, base_nodes, applied_load_n}.

    This is a BOX PROXY, not the caller's geometry. The MCP tool run_fea_contact no
    longer uses it — that now solves real unilateral contact on the meshed document
    through the engine (core/sim/fea_contact.cpp + fea_static_solve_contact). What
    still needs this deck is the frozen `sim_run_contact` worker RPC
    (services/sim/multiphysics_rpc.py, contracts/worker_rpc.md), whose parameters ARE
    box dimensions and which the GUI sim manager calls.

    Validation (numeric, ENGINEERING tier): by static equilibrium the vertical
    reaction summed over the fixed base must equal the applied load
    (pressure*L*b) — a conservation check that any correct contact solve satisfies.

    Contact face convention: the DEPENDENT (slave) surface is the node set on the
    upper block's bottom; the INDEPENDENT (master) surface is an element-face
    surface on the lower block's top (CalculiX wants the master as *SURFACE
    TYPE=ELEMENT for node-to-surface contact)."""
    E, nu, pressure = (_finite(E, "E"), _finite(nu, "nu"),
                       _finite(pressure, "pressure"))
    if E <= 0.0 or pressure <= 0.0:
        raise ValueError("E and pressure must both be > 0")
    if not -1.0 < nu < 0.5:
        raise ValueError("nu must be in (-1, 0.5)")
    # --- lower block: z∈[0,h], ids 1..n_per, elems 1..e_per ---
    lower_nodes, lower_elems_raw, nnx, nny, nnz = _hex_box_nodes_elems(
        L, b, h, nx, ny, nz)
    nid = _nid_fn(nnx, nny)
    n_per = nnx * nny * nnz
    e_per = nx * ny * nz
    lower_elems = [ln.replace("ELSET=EALL", "ELSET=ELOW") for ln in lower_elems_raw]

    base = [nid(i, j, 0) for j in range(nny) for i in range(nnx)]

    # Lower-block top element faces. For our C3D8 node winding the +z face is the
    # 5-6-7-8 face = CalculiX face 2 (label "S2") per the CalculiX manual's
    # hexahedral face table (P2 = nodes 5-8-7-6). The top element layer is k=nz-1;
    # element id of the (i,j,k) hex is 1 + i + j*nx + k*nx*ny.
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
    lines += ["*NSET, NSET=NBASE", *_set_lines(base)]
    # Master (independent): element-face surface on the lower block top (S2 = 5678 face).
    lines.append("*SURFACE, NAME=SLOWTOP, TYPE=ELEMENT")
    lines += [f"{e}, S2" for e in top_elem_faces]
    # Slave (dependent): node surface on the upper block bottom.
    lines.append("*SURFACE, NAME=SUPBOT, TYPE=NODE")
    # A node *SURFACE takes exactly ONE node per line (CalculiX: "only one entry per
    # line allowed" — unlike *NSET which permits 16). Do NOT wrap these at 16.
    lines += [str(n) for n in upper_bot]
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
    ]
    # The upper block is restrained ONLY in z by the contact — its in-plane (x,y)
    # translation and z-rotation are unconstrained rigid-body modes, so the nonlinear
    # solve diverges ("increment size smaller than minimum"). Pin the contact-interface
    # nodes in x,y: for a frictionless uniaxial compression coupon they don't slide, so
    # this removes the spurious modes without stiffening the z (compression) response.
    lines += ["*NSET, NSET=NUPB", *_set_lines(upper_bot)]
    lines += [
        "*BOUNDARY", "NBASE, 1, 3, 0.0", "NUPB, 1, 2, 0.0",
        "*STEP, NLGEOM, INC=100",
        # Automatic incrementation with a low floor lets ccx cut back through the
        # initial contact-engagement transient instead of failing at a fixed step.
        "*STATIC", "0.05, 1.0, 1e-6, 0.25",
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
    """Parse the last complete ccx *NODE PRINT block and average a node component.

    ``key`` uses CalculiX names ("NT" temperature, "U" displacement, "RF"
    reaction force). The requested component is 1-based after the node id.
    Returning a partial node set would corrupt conservation checks: contact multiplies
    this average by the requested node count, so one missing row otherwise invents the
    missing reactions. A missing, malformed, duplicate, or non-finite row therefore
    returns ``None`` rather than a plausible partial result.

    The LAST matching block is required because a multi-increment NLGEOM step prints
    one block per converged increment; averaging across partial-load increments
    understates the final reaction."""
    component = _positive_int(component, "component")
    if not node_set:
        return None
    ids = [_positive_int(nid, f"node_set[{i}]") for i, nid in enumerate(node_set)]
    want = set(ids)
    if len(want) != len(ids):
        raise ValueError("node_set must not contain duplicate node ids")
    vals: dict[int, float] = {}
    known_key = key.upper() in _DAT_HEADER_KEYWORD
    kw = _DAT_HEADER_KEYWORD.get(key.upper(), key.lower())
    lines = Path(dat_path).read_text().splitlines()
    i = 0
    while i < len(lines):
        header = lines[i].strip()
        hl = header.lower()
        is_result_header = (kw in hl if known_key else key.lower() in hl)
        if header and is_result_header:
            # ccx puts blank lines between the header and data. Once rows start,
            # the first non-numeric row ends this result block.
            j = i + 1
            block_vals: dict[int, float] = {}
            invalid_target_row = False
            started = False
            while j < len(lines):
                row = lines[j]
                parts = row.split()
                is_data = bool(parts) and parts[0].lstrip("-").isdigit()
                if is_data:
                    started = True
                    try:
                        nid = int(parts[0])
                    except ValueError:
                        nid = -1
                    if nid in want:
                        if nid in block_vals or len(parts) <= component:
                            invalid_target_row = True
                        else:
                            try:
                                block_vals[nid] = float(parts[component])
                            except ValueError:
                                invalid_target_row = True
                elif started:
                    break
                j += 1
            # Keep the LAST matching block even when it is incomplete, so an
            # earlier complete increment cannot masquerade as the final result.
            vals = {} if invalid_target_row else block_vals
            i = j
            continue
        i += 1
    if set(vals) != want or any(not math.isfinite(v) for v in vals.values()):
        return None
    return sum(vals.values()) / len(vals)


