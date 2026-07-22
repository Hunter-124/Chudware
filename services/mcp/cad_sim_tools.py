"""CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Optional
from typing import Union
import math

from .cad_common import (
    CoreError,
    _autosave_all_open,
    _default_conductivity,
    _default_specific_heat,
    _doc,
    _dumps,
    _err,
    _first_temperature,
    _merge_face_sets,
    _persist_study,
    _resolve_face_points,
    _resolve_material,
    _solve_sync_or_handoff,
    _start_job,
    _tmp,
    cb,
)

def register_cad_sim_tools(mcp: FastMCP) -> None:
    """CAD simulation MCP tools (T-RF8 split of cad_tools.py): run_fea_* + run_cfd_*."""


    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(fixtures: list, loads: list, material: Union[str, dict] = "steel",
                       mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                       background: bool = False) -> str:
        """Linear-static stress FEA on the current part. Returns max stress, displacement, and FoS.

        fixtures : list of face queries held fixed, e.g. ["bottom"] or
                   [{"of":"face","side":"bottom","pick":"largest"}].
        loads    : list of {face query + a force or pressure}, e.g.
                   [{"face":"top","force":[0,0,-500]}]   # newtons, world XYZ
                   [{"face":"top","pressure":0.5}]       # MPa, inward
        material : a name ("steel","aluminum","titanium","stainless","brass","abs","pla") or an
                   explicit dict {E_mpa, nu, density_g_cm3, yield_mpa, ...}.
        mesh_fineness : 0..1 (default 0.5). Second-order (tet10) elements are used for accuracy.
        background : if True, return a job_id immediately and solve on a worker thread — poll
                     get_job(job_id). Use it for fine meshes that may exceed the call timeout.

        Returns {max_von_mises_mpa, max_disp_mm, min_fos}. min_fos = yield / max stress; > 1 holds.
        Tip: query_geometry(...) first to confirm your fixture/load faces resolve.
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})

            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error":
                               "no fixture faces resolved — the face queries you provided "
                               "didn't match any surface points. Static FEA requires at least "
                               "one fixed face. Use query_geometry() to confirm the face query."})

            load_specs: list = []
            for ld in loads:
                if not isinstance(ld, dict) or "face" not in ld:
                    raise ValueError(f"each load needs a 'face' query: {ld!r}")
                faces = _resolve_face_points(d, ld["face"])
                # Merge multiple face-point sets for this load (keeps face_normal/surface
                # hints exactly when they stay valid — see _merge_face_sets).
                merged = _merge_face_sets(faces)
                spec: dict = {"faces": merged}
                if "pressure" in ld:
                    spec["type"] = "pressure"
                    spec["magnitude"] = float(ld["pressure"])
                elif "force" in ld:
                    spec["type"] = "force"
                    spec["vector"] = [float(x) for x in ld["force"]]
                else:
                    raise ValueError(f"load needs 'force':[x,y,z] or 'pressure':<MPa>: {ld!r}")
                load_specs.append(spec)

            study = {
                "step_path": step,
                "material": mat,
                "fixtures": fixture_sets,
                "loads": load_specs,
                "mesh_order": 2,        # tet10 captures bending
                "fineness": mesh_fineness,
            }

            def _solve() -> dict:
                res = cb.call("sim_run_static", {"study": study})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d, "error": res.get("error", "solve failed")}
                # Persist setup+results into the doc so they save inside the .step+.
                _persist_study(d, {
                    "type": "fea_static",
                    "name": f"Static stress ({mat.get('name', material)})",
                    "engine": "cpp",
                    "material": mat,
                    "fixtures": fixtures,
                    "loads": loads,
                    "mesh_fineness": mesh_fineness,
                    "results": res.get("summary", {}),
                    "n_dof": res.get("n_dof"),
                })
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "summary": res.get("summary", {}), "n_dof": res.get("n_dof"),
                        "saved_to_study": True}

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_static", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_static", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_modal(fixtures: list, material: Union[str, dict] = "steel", n_modes: int = 6,
                      mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                      background: bool = False) -> str:
        """Modal FEA — the natural frequencies (Hz) of the held part. Returns the first n_modes.

        fixtures : list of face queries held fixed (e.g. ["bottom"]). An unconstrained part has
                   rigid-body modes near 0 Hz, so fix it like it is mounted.
        material : name or explicit dict (density drives the mode frequencies).
        background : if True, return a job_id and solve on a worker thread — poll get_job(job_id).
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            # F6 (free-free modal): when fixtures=[] the caller is requesting an unconstrained
            # solve — the C++ fea_modal_solve handles empty fixed_nodes by computing the full
            # 6 rigid-body modes near 0 Hz followed by the elastic modes. Only reject when the
            # caller DID supply fixture queries but NONE of them resolved to real face points
            # (which is almost certainly a geometry-query mistake, not intentional free-free).
            if fixtures and not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved — "
                               "the face queries you provided didn't match any surface points. "
                               "Use query_geometry() to find the right face query, or pass "
                               "fixtures=[] for a free-free (unconstrained) solve."})
            # 2nd-order (tet10) elements: quadratic bending accuracy — no spurious split on
            # a square section and no low-frequency bias the way tet4 had (Bug N-1).
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "mesh_order": 2, "fineness": mesh_fineness}

            def _solve() -> dict:
                # Free-free (no fixtures): the 0 Hz rigid-body cluster is 6-fold
                # degenerate. FIXED IN THE ENGINE 2026-07-12 (fea_modal.cpp
                # over-requests + widens the Krylov subspace + verifies the rigid
                # count internally), so this caller-side over-request + verify is
                # now a redundant SAFETY NET — kept because it is cheap and turns
                # any regression into a visible warning note instead of silently
                # wrong mode numbering.
                free_free = not fixture_sets
                solve_n = max(n_modes + 6, 14) if free_free else n_modes
                res = cb.call("sim_run_modal", {"study": study, "n_modes": solve_n})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                freqs = list(res.get("freqs_hz", []))
                note = None
                if free_free:
                    def _n_rigid(fs: list) -> int:
                        return sum(1 for f in fs if f < 0.5)  # rigid ~0 Hz, elastic >> 0.5 Hz
                    for _ in range(2):
                        if _n_rigid(freqs) >= 6:
                            break
                        solve_n += 6
                        retry = cb.call("sim_run_modal",
                                        {"study": study, "n_modes": solve_n})
                        if not retry.get("ok"):
                            break
                        res, freqs = retry, list(retry.get("freqs_hz", []))
                    if _n_rigid(freqs) < 6:
                        note = (f"warning: free-free solve resolved only "
                                f"{_n_rigid(freqs)}/6 rigid-body modes — the mode list "
                                "may be missing rigid modes (eigensolver degenerate-"
                                "cluster limitation); elastic frequencies are unaffected")
                    freqs = freqs[:n_modes]
                _persist_study(d, {
                    "type": "fea_modal",
                    "name": f"Modal ({mat.get('name', material)})",
                    "engine": "cpp",
                    "material": mat,
                    "fixtures": fixtures,
                    "n_modes": n_modes,
                    "mesh_fineness": mesh_fineness,
                    "results": {"freqs_hz": freqs, "n_modes": len(freqs)},
                })
                out = {"ok": True, "doc_id": d, "material": mat.get("name", material),
                       "freqs_hz": freqs, "n_modes": len(freqs),
                       "saved_to_study": True}
                if note:
                    out["note"] = note
                return out

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_modal", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_modal", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_frequency_response(fixtures: list, excitation_dir: Optional[list] = None,
                                   sweep_hz: Optional[list] = None, zeta: float = 0.02,
                                   base_accel_g: float = 1.0,
                                   psd_breakpoints: Optional[list] = None,
                                   vibration_spec: Optional[dict] = None,
                                   n_modes: int = 6,
                                   material: Union[str, dict] = "steel",
                                   mesh_fineness: float = 0.5,
                                   doc_id: Optional[str] = None,
                                   background: bool = False) -> str:
        """Harmonic + random (PSD) vibration response of the held part vs a base excitation.

        Extends run_fea_modal into a frequency-response solve by modal superposition:
        each mode becomes an SDOF oscillator (participation-factor weighted); harmonic
        response uses the classical transmissibility sweep, random response uses Miles'
        equation per mode (Steinberg 2000 Eq. 6.6). Modes combine by SRSS.

        fixtures       : face queries held fixed (base/support) — same syntax as run_fea_modal.
        excitation_dir : base-excitation unit direction [dx,dy,dz]; default [0,0,1] (Z-up axis).
        sweep_hz       : harmonic sweep points; default 200 log-spaced points, 10–2000 Hz.
        zeta           : modal damping ratio (default 0.02 = 2% — an assumption, echoed back).
        base_accel_g   : harmonic base input amplitude in g (default 1.0 = unit sweep).
        psd_breakpoints: [[f_hz, psd_g2_per_hz], ...] random-vibration input (log-log
                         interpolated). OMITTED → the random field returns
                         {ok:false, reason:"no_psd_provided"} — no default profile is assumed.
        vibration_spec : {"max_peak_accel_g": g} and/or {"max_grms": g} → pass/fail `verdict`
                         (the DAG verify-node end condition). Omitted → verdict is null.

        Returns {ok, freqs_hz, harmonic:{sweep_hz, accel_g, peak_accel_g, peak_freq_hz,
        per_mode, citation, limitations}, random:{per_mode, total_grms, ...} | degraded,
        verdict, damping_assumption}. Response is acceleration amplification (g) —
        physical displacement/stress are NOT recovered (unit-normalized mode shapes).
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            ex_dir = [float(x) for x in (excitation_dir or [0.0, 0.0, 1.0])]
            if sweep_hz is not None:
                sweep = [float(f) for f in sweep_hz]
            else:  # default: 200 log-spaced points across the common vibe-test band
                sweep = [10.0 * (2000.0 / 10.0) ** (i / 199.0) for i in range(200)]
            psd = None
            if psd_breakpoints is not None:
                psd = [(float(p[0]), float(p[1])) for p in psd_breakpoints]
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if fixtures and not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved — "
                               "the face queries you provided didn't match any surface "
                               "points. Use query_geometry() to find the right face query."})
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "mesh_order": 2, "fineness": mesh_fineness}

            def _solve() -> dict:
                res = cb.call("sim_run_modal", {"study": study, "n_modes": n_modes})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                freqs = res.get("freqs_hz", [])
                shapes = res.get("mode_shapes", [])
                n_nodes = int(res.get("n_nodes", 0))
                if not freqs or not shapes or n_nodes <= 0:
                    return {"ok": False, "doc_id": d, "error":
                            "modal solve returned no mode shapes — cannot build the "
                            "frequency response (need freqs_hz + mode_shapes + n_nodes)"}
                # Total mass from the solid at the study material's density (eval_mass
                # returns mass_g = volume_mm3 * density; kg = g / 1000).
                dens = float(mat.get("density_g_cm3", 0.0))
                meas = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": dens})
                total_mass_kg = float((meas or {}).get("mass_g") or 0.0) / 1000.0
                if total_mass_kg <= 0.0:
                    return {"ok": False, "doc_id": d, "error":
                            "could not determine part mass (volume or density missing) — "
                            "set a material with density_g_cm3 and ensure the part has "
                            "solid volume"}
                # Lazy import — keeps server module load independent of numpy availability.
                from engine_oracle.sim.vibration import (
                    evaluate_vibration_spec, harmonic_response, miles_response)
                harm = harmonic_response(
                    freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                    total_mass_kg=total_mass_kg, excitation_dir=ex_dir,
                    sweep_hz=sweep, zeta=zeta, base_accel_g=base_accel_g)
                if psd is not None:
                    rand: dict = miles_response(
                        freqs_hz=freqs, mode_shapes_flat=shapes, n_nodes=n_nodes,
                        total_mass_kg=total_mass_kg, excitation_dir=ex_dir,
                        psd_breakpoints=psd, zeta=zeta)
                    rand["ok"] = True
                else:  # honest degrade — NEVER assume a default vibration profile
                    rand = {"ok": False, "reason": "no_psd_provided",
                            "hint": "Provide psd_breakpoints=[[f_hz, g2_per_hz], ...] "
                                    "(e.g. from the qualification spec) for random-"
                                    "vibration GRMS via Miles' equation."}
                verdict = evaluate_vibration_spec(
                    vibration_spec, harm, rand if rand.get("ok") else None)
                _persist_study(d, {
                    "type": "fea_frequency_response",
                    "name": f"Frequency response ({mat.get('name', material)})",
                    "engine": "cpp+py_modal_superposition",
                    "material": mat, "fixtures": fixtures, "n_modes": n_modes,
                    "mesh_fineness": mesh_fineness,
                    "results": {"freqs_hz": freqs,
                                "peak_accel_g": harm.get("peak_accel_g"),
                                "peak_freq_hz": harm.get("peak_freq_hz"),
                                "total_grms": rand.get("total_grms"),
                                "verdict": verdict},
                })
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "freqs_hz": freqs, "harmonic": harm, "random": rand,
                        "verdict": verdict,
                        "damping_assumption": f"zeta={zeta} (Q={1.0 / (2.0 * zeta):g}) — "
                                              "assumed modal damping, not measured",
                        "saved_to_study": True}

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_frequency_response", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_frequency_response", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_cfd_flow(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                     cells: int = 96, n_steps: int = 2000, doc_id: Optional[str] = None,
                     background: bool = False) -> str:
        """Wind-tunnel CFD over the current part/assembly (LBM). Returns drag, lift, Cd, Reynolds.

        CONVENTIONS (AI-CAD is Z-up): freestream is along +X; LIFT is the +Z (up) force; SPAN is Y.
        +aoa pitches the nose up (rotation about Y), so a cambered wing makes +lift. `reynolds` and
        `cd` use ref_length_mm = the part's LONGEST span (NOT chord) — for an airfoil, simulate the
        wing alone or read `ref_length_mm` and renormalise to chord yourself.

        velocity : free-stream speed (m/s).   aoa : angle of attack (degrees).
        fluid    : "air" or "water".          cells : grid pitch = domain's longest span / cells
                   (higher = finer + slower; practical 48-200).  n_steps : LBM iterations (~500-4000).
        background : if True, return a job_id immediately and solve on a worker thread (recommended
                     for cells>=120 or n_steps>=2500, which can exceed the call timeout) — then poll
                     get_job(job_id).

        RESOLUTION MATTERS: the result reports `voxel_mm`, `min_feature_mm`, and
        `min_feature_voxels` (how many cells span the thinnest part dimension). If a thin wing/tail
        edge spans < ~8 voxels it is UNDER-RESOLVED — lift especially reads near zero — and a
        `warning` is returned; raise `cells` or simulate the thin part on its own. Also reported:
        `converged` + `residual` (force settle over the last fifth of the run); a non-converged run
        is under-iterated, not physically zero.

        ⚠ ABSOLUTE Cd IS UNCALIBRATED. This is a coarse voxel LBM: the staircased surface
        over-counts force (more so on curved/angled faces), so the magnitude of drag_n/cd can be
        off by an order of magnitude and is resolution-dependent. Use it to COMPARE configurations
        and read TRENDS (does +aoa add lift? does a fillet cut drag? A vs B?), which it now gets
        right (drag scales as V^2, Cd is velocity-independent). For a TRUSTWORTHY absolute Cd/Cl,
        use run_cfd_accurate() (OpenFOAM RANS — validated, but minutes per run). Treat these LBM
        numbers as a fast directional estimate, not a certified coefficient.
        """
        try:
            d = _doc(doc_id)
            # Live-progress side-channel: the engine writes {pct,step,n_steps} to this file as the
            # LBM iterates; get_job reads it so a background run reports progress like OpenFOAM.
            prog_path = _tmp(".json")
            prog: dict = {"pct": 0, "stage": "lbm_solve", "progress_file": prog_path}

            def _solve() -> dict:
                res = cb.call("sim_run_flow", {"doc_id": d, "velocity": velocity, "aoa": aoa,
                                               "fluid": fluid, "cells": cells, "n_steps": n_steps,
                                               "progress_path": prog_path})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "flow sim failed")}
                out = {"ok": True, "doc_id": d}
                for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                          "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                          "converged", "residual", "voxel_mm", "min_feature_mm",
                          "min_feature_voxels", "steps_run", "diverged", "warning"):
                    if k in res:
                        out[k] = res[k]
                _persist_study(d, {
                    "type": "cfd_flow",
                    "name": f"CFD wind tunnel ({fluid}, {velocity} m/s, aoa {aoa})",
                    "engine": "cpp-lbm",
                    "params": {"velocity": velocity, "aoa": aoa, "fluid": fluid,
                               "cells": cells, "n_steps": n_steps},
                    "results": {k: out[k] for k in out if k not in ("ok", "doc_id")},
                })
                out["saved_to_study"] = True
                return out

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("cfd_flow", _solve, progress=prog)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("cfd_flow", _solve, progress=prog))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_cfd_accurate(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                         preset: str = "coarse", doc_id: Optional[str] = None) -> str:
        """HIGH-FIDELITY external-aero CFD via OpenFOAM (steady RANS, k-omega SST + prism layers).

        Use this when you need a TRUSTWORTHY absolute Cd/Cl, not the coarse directional estimate
        from run_cfd_flow (the built-in LBM). This is the validated path — sphere/cube land within
        ~10-15% of experiment (regression gate: tools/foam_validation.py). It body-fits a
        snappyHexMesh with boundary layers and solves to convergence, so it takes MINUTES and
        requires OpenFOAM installed — detected automatically on Linux (sourced env / system install /
        user-space conda env / AICAD_OPENFOAM root), native Windows blueCFD, or WSL openfoam2412.
        All OpenFOAM invocations are subprocesses (GPL isolation — never linked). It ALWAYS runs in
        the background: returns a job_id immediately; poll get_job(job_id) (which reports live
        meshing/solve progress) for the result.

        CONVENTIONS match run_cfd_flow (Z-up): freestream +X, lift +Z, +aoa pitches nose up.
        velocity : m/s.   aoa : degrees.   fluid : "air" | "water".
        preset   : "coarse" (fastest, ~600 iters) | "medium" | "fine" | "xfine" (finest/slowest).
                   Start coarse to sanity-check, refine if you need the digits.

        Result (via get_job): {cd, cl, ld (lift/drag), drag_n, lift_n, reynolds, ref_area_mm2,
        vmax_mps, converged, iters, notes}. If `converged` is false, raise the preset / iterations.
        """
        try:
            from engine.sim.foam_runner import capabilities
            caps = capabilities()
            if not caps.get("openfoam"):
                import os as _os
                how = ("`python -m tools.provision_openfoam --install` (user-space conda-forge "
                       "env, no sudo), or unpack an openfoam.com tarball under ~/OpenFOAM/, or "
                       "set AICAD_OPENFOAM to an install root"
                       if _os.name == "posix" else
                       "`python -m tools.provision_openfoam --install` (WSL openfoam2412) or "
                       "install blueCFD-Core natively and set %AICAD_OPENFOAM%")
                return _dumps({"ok": False, "skipped": True, "reason": "openfoam_not_installed",
                               "error": "OpenFOAM is not available on this machine. Provision it "
                               f"with {how}. The coarse LBM run_cfd_flow still works."})
            if preset not in ("coarse", "medium", "fine", "xfine"):
                return _dumps({"ok": False, "error": f"unknown preset {preset!r} — use "
                               "coarse|medium|fine|xfine"})
            d = _doc(doc_id)
            # OpenFOAM meshes a watertight STL surface (build_case handles mm -> m).
            stl = _tmp(".stl")
            cb.call("export_file", {"doc_id": d, "path": stl, "format": "stl"})
            fluid_dict = ({"nu": 1.0e-6, "rho": 1000.0} if fluid == "water"
                          else {"nu": 1.5e-5, "rho": 1.225})

            prog: dict = {"pct": 0, "stage": "start", "text": "queued"}

            def _on_progress(ev: dict) -> None:
                prog.update(pct=ev.get("pct", 0), stage=ev.get("stage", ""),
                            text=ev.get("text", ""))

            def _solve() -> dict:
                from engine.sim.openfoam import run_openfoam_tunnel
                r = run_openfoam_tunnel(stl, direction=(1.0, 0.0, 0.0),
                                        speed_mps=float(velocity), preset=preset,
                                        aoa_deg=float(aoa), fluid=fluid_dict,
                                        on_progress=_on_progress)
                return {"ok": True, "doc_id": d,
                        "solver": f"OpenFOAM RANS (k-omega SST, {caps.get('openfoam_kind')})",
                        "preset": preset, "cd": r.cd, "cl": r.cl, "ld": r.ld,
                        "drag_n": r.drag_n, "lift_n": r.lift_n, "side_n": r.side_n,
                        "reynolds": r.reynolds, "ref_area_mm2": r.ref_area_mm2,
                        "vmax_mps": r.vmax_mps, "converged": (not r.diverged),
                        "iters": r.steps_run, "notes": r.solver_notes,
                        "velocity_ms": float(velocity), "aoa_deg": float(aoa), "fluid": fluid}

            jid = _start_job("cfd_openfoam", _solve, progress=prog)
            return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                           "solver": caps.get("openfoam_version"), "preset": preset,
                           "note": "OpenFOAM RANS runs for minutes — poll get_job(job_id); it "
                                   "reports live meshing/solve progress."})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ================================================================
    # T-BL04 — fatigue / thermal / buckling / contact FEA
    # Fatigue routes to the C++ `sim_run_fatigue` solver (same posture as
    # run_fea_static→sim_run_static). Thermal/buckling/contact route to a
    # CalculiX (ccx) subprocess — GPL-2.0, subprocess-only, PATH-discovered,
    # never linked/bundled (contracts/licenses.md) — and degrade HONESTLY
    # (skipped:true) when ccx is absent. See services/mcp/fea_advanced.py.
    # ================================================================

    def _part_bbox_dims(d: str) -> tuple:
        """Return (L, b, h) = the part's world bounding-box size (mm), sorted so
        L is the longest span (the slender axis for beam/column proxies)."""
        r = cb.call("asm_world_bodies", {"doc_id": d, "explode": False})
        bodies = r.get("bodies", [])
        if not bodies:
            raise ValueError("no bodies in the document — build geometry first")
        # Union of all body bboxes.
        lo = [float("inf")] * 3
        hi = [float("-inf")] * 3
        for bd in bodies:
            bb = bd.get("bbox", {})
            mn, mx = bb.get("min"), bb.get("max")
            if not (mn and mx):
                continue
            for i in range(3):
                lo[i] = min(lo[i], float(mn[i]))
                hi[i] = max(hi[i], float(mx[i]))
        size = sorted((hi[i] - lo[i] for i in range(3)), reverse=True)
        if size[0] <= 0:
            raise ValueError("degenerate bounding box")
        return size[0], size[1] if size[1] > 0 else size[0], \
            size[2] if size[2] > 0 else size[0]



    @mcp.tool()
    def run_fea_fatigue(fixtures: list, loads: list, material: Union[str, dict] = "steel",
                        load_type: str = "reversed", load_ratio: float = -1.0,
                        surface: str = "machined", reliability_pct: float = 50.0,
                        temperature_c: float = 20.0, theory: str = "goodman",
                        mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                        background: bool = False) -> str:
        """High-cycle fatigue life (S-N) on the current part — reuses the linear-static
        stress field, then applies mean-stress theory + Marin correction + Basquin life.

        Runs the SAME static solve as run_fea_static (fixtures/loads/material), then the
        engine (`sim_run_fatigue`) evaluates every mean-stress theory (Goodman/Gerber/
        Soderberg/ASME/Morrow) node-by-node and returns each theory's max equivalent
        fully-reversed amplitude sa_eq. This tool then applies the Marin endurance
        correction + reliability + the Shigley S-N life line (mirrors the desktop app's
        fatigue math exactly) to report cycles-to-failure + a factor of safety.

        fixtures/loads : SAME plain-English face queries as run_fea_static.
        material       : name or dict (needs ultimate_mpa + endurance_mpa; the built-in
                         materials all carry them).
        load_type      : "reversed" (R=-1, default) | "zero_based" (R=0) | "ratio" (use
                         load_ratio) — the cyclic load spectrum on the static result.
        surface        : "ground"|"machined"|"hot_rolled"|"as_forged" (Marin ka).
        reliability_pct: design reliability for ke (50/90/99/99.9...).
        theory         : which mean-stress theory to report at top level (default goodman).

        Returns {ok, regime: infinite|finite|static_overload, cycles, fos_endurance,
        se_corrected_mpa, theories{...}}. regime="infinite" ⇒ >1e6 cycles (runout).
        Degrades like run_fea_static when the C++ FEA mesher backend is unavailable.
        """
        try:
            from . import fea_advanced as fa
            d = _doc(doc_id)
            mat = _resolve_material(material)
            Sut = float(mat.get("ultimate_mpa") or 0.0)
            Se_base = float(mat.get("endurance_mpa") or 0.0)
            if Sut <= 0:
                return _dumps({"ok": False, "error":
                               f"material {mat.get('name', material)!r} has no ultimate_mpa — "
                               "fatigue needs ultimate + endurance strength."})
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved — use "
                               "query_geometry() to confirm the face query."})
            load_specs: list = []
            for ld in loads:
                if not isinstance(ld, dict) or "face" not in ld:
                    raise ValueError(f"each load needs a 'face' query: {ld!r}")
                faces = _resolve_face_points(d, ld["face"])
                merged = _merge_face_sets(faces)
                spec: dict = {"faces": merged}
                if "pressure" in ld:
                    spec["type"] = "pressure"; spec["magnitude"] = float(ld["pressure"])
                elif "force" in ld:
                    spec["type"] = "force"; spec["vector"] = [float(x) for x in ld["force"]]
                else:
                    raise ValueError(f"load needs 'force' or 'pressure': {ld!r}")
                load_specs.append(spec)

            surf_map = {"ground": 0, "machined": 1, "hot_rolled": 2, "as_forged": 3}
            lt_map = {"reversed": {"lmin": -1.0, "lmax": 1.0},
                      "zero_based": {"lmin": 0.0, "lmax": 1.0},
                      "ratio": {"lmin": float(load_ratio), "lmax": 1.0}}
            corr = fa.corrected_endurance(Se_base, Sut, surface=surf_map.get(surface, 1),
                                          load_case=0, temperature_c=temperature_c,
                                          reliability_pct=reliability_pct)
            fat = {"ultimate_mpa": Sut, "yield_mpa": float(mat.get("yield_mpa") or 0.0),
                   "se_corrected_mpa": corr["se_corrected_mpa"],
                   "se_base_mpa": corr["se_base_mpa"]}
            fat.update(lt_map.get(load_type, lt_map["reversed"]))
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "loads": load_specs, "mesh_order": 2, "fineness": mesh_fineness,
                     "fatigue": fat}

            def _solve() -> dict:
                try:
                    res = cb.call("sim_run_fatigue", {"study": study})
                except CoreError as ce:
                    if "unknown method" in str(ce):
                        return {"ok": False, "skipped": True, "doc_id": d,
                                "backend": "cpp fea",
                                "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                                          "available in this build (mesher not compiled). Fatigue "
                                          "reuses the static stress field — it needs the same FEA "
                                          "backend as run_fea_static."}
                    raise
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "fatigue solve failed")}
                theories = res.get("summary", {}).get("theories", {})
                life = fa.fatigue_summarize(theories, corr["se_corrected_mpa"], Sut,
                                            theory=theory)
                life.update({"ok": True, "doc_id": d, "marin": corr,
                             "material": mat.get("name", material)})
                _persist_study(d, {
                    "type": "fea_fatigue",
                    "name": f"Fatigue ({mat.get('name', material)}, {theory})",
                    "engine": "cpp",
                    "material": mat, "fixtures": fixtures, "loads": loads,
                    "results": {"theories": life["theories"], "regime": life["regime"],
                                "cycles": life["cycles"], "fos_endurance": life["fos_endurance"]},
                })
                life["saved_to_study"] = True
                return life

            _autosave_all_open()
            if background:
                jid = _start_job("fea_fatigue", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d})
            return _dumps(_solve_sync_or_handoff("fea_fatigue", _solve))
        except CoreError as e:
            # C++ FEA backend absent (mesher not built) → honest degrade, same as static.
            if "unknown method" in str(e):
                return _dumps({"ok": False, "skipped": True,
                               "reason": "the C++ FEA solver backend (sim_run_fatigue) is not "
                                         "available in this build (mesher not compiled). Fatigue "
                                         "reuses the static stress field — it needs the same FEA "
                                         "backend as run_fea_static.", "backend": "cpp fea"})
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_thermal(hot_faces: list, cold_faces: list,
                        material: Union[str, dict] = "steel",
                        conductivity_w_mk: Optional[float] = None,
                        transient: Optional[dict] = None,
                        loads: Optional[list] = None,
                        specific_heat_j_kgk: Optional[float] = None,
                        mesh_fineness: float = 0.5,
                        doc_id: Optional[str] = None) -> str:
        """Thermal (heat-conduction) FEA — steady-state via CalculiX, TRANSIENT via the
        C++ core solver on the real part mesh.

        STEADY (default, transient=None): solves ∇·(k∇T)=0 with the given faces held at
        fixed temperatures (Dirichlet) via a ccx subprocess. Because arbitrary CAD meshing
        needs the FEA mesher, this ccx path solves on a PRISMATIC BOX PROXY sized to the
        part's bounding box with a 1-D conduction setup (hot end vs cold end) — an
        engineering estimate honest for bar/plate-like parts. CalculiX is GPL-2.0 →
        subprocess-only, PATH-discovered; absent ccx ⇒ {ok:false, skipped:true}.

        TRANSIENT (T-FIN18, pass a `transient` dict): solves C dT/dt + K T = f(t) with
        implicit backward Euler on the REAL tet mesh of the part (C++ core, same solver
        family as run_fea_static — no box proxy, no ccx needed).
          transient : {"t_end_s": <s>, "dt_s": <s, optional — defaults t_end/200>,
                       "t_init_c": <°C, default 20>, "frame_times_s": [<s>, ...],
                       "capacitance": "lumped"|"consistent" (default lumped)}
          hot_faces/cold_faces : Dirichlet faces held at their temperature for the whole
                       run, e.g. [{"face": "+x", "temperature": 100}]. May be EMPTY for
                       transient (a flux-only adiabatic heat-up is well-posed).
          loads     : optional time-varying heat loads, each
                       {"face": <query>, "flux_w_m2": <q>, "schedule": {...}}  or
                       {"face": <query>, "h_w_m2k": <h>, "t_amb_c": <°C>}   (convection)
                      schedule (per load, optional — omitted = always on):
                       {"duty": 0.25, "period_s": 1.0}          duty-cycle shorthand —
                          the direct mapping for an aicad.powerprofile/1 per-rail duty
                          profile (rail duty d, period p)
                       {"points": [[t_s, scale], ...], "period_s": <opt>}  piecewise
                          sample-and-hold (a residency timeline maps verbatim)
          specific_heat_j_kgk : cp override; else material dict, else a typical value.
        Returns frame times + per-frame max temperature, the PEAK temperature over the
        whole run (max-temp envelope) and when it occurred. Full nodal frames stay in
        the study result inside the doc.

        conductivity_w_mk : thermal conductivity override (W/m·K); else from material
                            (falls back to a typical value for the named material).
        """
        try:
            if transient is not None:
                d = _doc(doc_id)
                mat = _resolve_material(material)
                k_cond = (float(conductivity_w_mk) if conductivity_w_mk is not None
                          else float(mat.get("conductivity_w_mk")
                                     or _default_conductivity(mat, material)))
                if not isinstance(transient, dict) or not transient.get("t_end_s"):
                    return _dumps({"ok": False, "error":
                                   "transient needs a dict with t_end_s (seconds), e.g. "
                                   "{\"t_end_s\": 60, \"dt_s\": 0.5}"})
                cp = (float(specific_heat_j_kgk) if specific_heat_j_kgk is not None
                      else float(mat.get("cp_j_kgk") or _default_specific_heat(mat, material)))
                step = _tmp(".step")
                cb.call("doc_export_step", {"doc_id": d, "path": step})

                def _faceset(q):
                    merged = _merge_face_sets(_resolve_face_points(d, q))
                    return merged if merged["points"] else None

                fixtures = []
                for f in (list(hot_faces or []) + list(cold_faces or [])):
                    if not isinstance(f, dict) or "temperature" not in f or "face" not in f:
                        raise ValueError("each hot/cold face needs {'face': <query>, "
                                         f"'temperature': <°C>}}: {f!r}")
                    fs = _faceset(f["face"])
                    if fs is None:
                        return _dumps({"ok": False, "error":
                                       f"face query {f['face']!r} resolved to no faces — "
                                       "use query_geometry() to confirm"})
                    fs["temp_c"] = float(f["temperature"])
                    fixtures.append(fs)

                load_specs = []
                for ld in (loads or []):
                    if not isinstance(ld, dict) or "face" not in ld:
                        raise ValueError(f"each thermal load needs a 'face' query: {ld!r}")
                    fs = _faceset(ld["face"])
                    if fs is None:
                        return _dumps({"ok": False, "error":
                                       f"load face query {ld['face']!r} resolved to no faces"})
                    if "flux_w_m2" in ld:
                        spec = {"faces": fs, "type": "flux",
                                "magnitude": float(ld["flux_w_m2"])}
                        if isinstance(ld.get("schedule"), dict):
                            spec["schedule"] = ld["schedule"]
                    elif "h_w_m2k" in ld:
                        spec = {"faces": fs, "type": "convection",
                                "h": float(ld["h_w_m2k"]),
                                "t_amb_c": float(ld.get("t_amb_c", 20.0))}
                    else:
                        raise ValueError("thermal load needs 'flux_w_m2' (+ optional "
                                         f"'schedule') or 'h_w_m2k'/'t_amb_c': {ld!r}")
                    load_specs.append(spec)

                study = {"step_path": step,
                         "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                         "fixtures": fixtures, "loads": load_specs,
                         "fineness": mesh_fineness,
                         "transient": {
                             "t_end_s": float(transient["t_end_s"]),
                             "dt_s": float(transient.get("dt_s") or 0.0),
                             "t_init_c": float(transient.get("t_init_c", 20.0)),
                             "frame_times_s": [float(t) for t in
                                               transient.get("frame_times_s", [])],
                             "capacitance": transient.get("capacitance", "lumped"),
                         }}
                res = cb.call("sim_run_thermal", {"study": study})
                if not res.get("ok"):
                    return _dumps({"ok": False, "doc_id": d,
                                   "error": res.get("error", "transient thermal solve failed")})
                frames = res.get("frames") or []
                frame_summ = [{"t_s": fr.get("t_s"),
                               "max_temp_c": round(max(fr.get("temperature") or [0.0]), 3)}
                              for fr in frames]
                out = {"ok": True, "doc_id": d, "transient": True,
                       "solver": "C++ core transient conduction (backward Euler, "
                                 f"{study['transient']['capacitance']} capacitance)",
                       "material": mat.get("name", material),
                       "conductivity_w_mk": k_cond, "specific_heat_j_kgk": cp,
                       "summary": res.get("summary", {}),
                       "n_steps": res.get("n_steps"), "n_nodes": res.get("n_nodes"),
                       "n_elems": res.get("n_elems"),
                       "frames": frame_summ,
                       "solver_notes": res.get("solver_notes", []),
                       "note": "summary.max_temp_c is the PEAK over the whole run "
                               "(max-temp envelope), at summary.max_temp_time_s; "
                               "per-frame nodal fields persisted in the doc study."}
                _persist_study(d, {"type": "fea_thermal_transient",
                                   "name": f"Transient thermal ({mat.get('name', material)})",
                                   "engine": "cpp",
                                   "material": {**mat, "k_w_mk": k_cond, "cp_j_kgk": cp},
                                   "transient": study["transient"],
                                   "results": {"summary": res.get("summary", {}),
                                               "frames": frames,
                                               "envelope_max": res.get("envelope_max")}})
                return _dumps(out)

            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("thermal steady-state"))
            d = _doc(doc_id)
            mat = _resolve_material(material)
            k_cond = (float(conductivity_w_mk) if conductivity_w_mk is not None
                      else float(mat.get("conductivity_w_mk") or _default_conductivity(mat, material)))
            T_hot = _first_temperature(hot_faces, "hot_faces")
            T_cold = _first_temperature(cold_faces, "cold_faces")
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_thermal_bar_inp(L, b, h, k_cond, T_hot, T_cold)
            run = fa.run_ccx(gen["inp"], job="thermal")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx thermal run failed"),
                               "backend": run.get("backend")})
            mid_T = fa.parse_dat_nodal(run["dat_path"], "NT", gen["mid_nodes"], component=1)
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *HEAT TRANSFER, STEADY STATE (box proxy)",
                   "conductivity_w_mk": k_cond, "T_hot_c": T_hot, "T_cold_c": T_cold,
                   "mid_plane_T_c": (round(mid_T, 3) if mid_T is not None else None),
                   "expected_mid_T_c": round(gen["expected_mid_T"], 3),
                   "note": "box-proxy 1-D conduction estimate; ccx GPL-2.0 subprocess."}
            _persist_study(d, {"type": "fea_thermal", "name": f"Thermal ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(out)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_buckling(fixtures: list, load: list, material: Union[str, dict] = "steel",
                         n_modes: int = 4, doc_id: Optional[str] = None) -> str:
        """Linear (eigenvalue) buckling via CalculiX (ccx subprocess) — critical load
        factor for slender members / thin panels.

        Returns the buckling load FACTORS λ (the applied load × λ = the critical load
        Pcr). λ<1 means the part buckles BELOW the applied load — unsafe. Like the
        thermal tool this solves a BOX PROXY sized to the part bounding box (a
        fixed-free column under axial compression), an Euler-grade estimate honest for
        column/strut-like parts.

        fixtures : face(s) held fixed (the mounted/clamped end), e.g. ["bottom"].
        load     : the compressive load as a single {face + force}, e.g.
                   [{"face":"top","force":[0,0,-1000]}] — magnitude is what λ scales.
        n_modes  : number of buckling modes to return (lowest first).

        CalculiX GPL-2.0 → subprocess-only. Absent ccx ⇒ {ok:false, skipped:true}.
        """
        try:
            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("linear buckling"))
            d = _doc(doc_id)
            mat = _resolve_material(material)
            E = float(mat["E_mpa"]); nu = float(mat.get("nu", 0.3))
            P = 1000.0
            if load and isinstance(load[0], dict) and "force" in load[0]:
                vec = load[0]["force"]
                P = max(1.0, math.sqrt(sum(float(x) ** 2 for x in vec)))
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_buckling_column_inp(L, b, h, E, nu, P, nz=4)
            run = fa.run_ccx(gen["inp"], job="buckle")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx buckling run failed"),
                               "backend": run.get("backend")})
            factors = fa.parse_buckle_factors(run["dat_path"])[:n_modes]
            crit = [round(f * P, 3) for f in factors]
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *BUCKLE (box proxy, fixed-free column)",
                   "applied_load_n": P, "load_factors": [round(f, 4) for f in factors],
                   "critical_loads_n": crit,
                   "euler_reference_pcr_n": round(gen["expected_Pcr"], 3),
                   "note": "box-proxy Euler buckling estimate; ccx GPL-2.0 subprocess. "
                           "load_factor<1 ⇒ buckles below the applied load."}
            _persist_study(d, {"type": "fea_buckling",
                               "name": f"Buckling ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(out)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def run_fea_contact(pressure_mpa: float = 1.0, material: Union[str, dict] = "steel",
                        doc_id: Optional[str] = None) -> str:
        """Nonlinear (large-deflection) contact FEA via CalculiX (ccx subprocess) — for
        press-fits, snap-fits, stacked parts touching under load.

        Solves a *STATIC, NLGEOM step with a *CONTACT PAIR transmitting load across a
        touching interface. This ccx path uses a TWO-BLOCK PROXY (an upper block pressed
        onto a lower fixed block) sized to the part bounding box — a validated
        conservation demonstration that the contact interface transmits the applied load
        into the fixed support (base reaction ≈ applied load).

        pressure_mpa : downward pressure on the upper block's top face.

        CalculiX GPL-2.0 → subprocess-only. Absent ccx ⇒ {ok:false, skipped:true}.
        """
        try:
            from . import fea_advanced as fa
            if fa.ccx_path() is None:
                return _dumps(fa._ccx_absent_result("nonlinear contact"))
            d = _doc(doc_id)
            mat = _resolve_material(material)
            E = float(mat["E_mpa"]); nu = float(mat.get("nu", 0.3))
            L, b, h = _part_bbox_dims(d)
            gen = fa.build_contact_block_inp(L, b, h, E, nu, float(pressure_mpa))
            run = fa.run_ccx(gen["inp"], job="contact")
            if run.get("skipped"):
                return _dumps(run)
            if not run.get("ok") or not run.get("dat_path"):
                return _dumps({"ok": False, "error": run.get("stderr", "")[-400:]
                               or run.get("error", "ccx contact run failed"),
                               "backend": run.get("backend")})
            rf = fa.parse_dat_nodal(run["dat_path"], "RF", gen["base_nodes"], component=3)
            reaction = abs(rf * len(gen["base_nodes"])) if rf is not None else None
            out = {"ok": True, "doc_id": d, "backend": run["backend"],
                   "solver": "CalculiX *STATIC,NLGEOM + *CONTACT PAIR (two-block proxy)",
                   "applied_load_n": round(gen["applied_load_n"], 3),
                   "base_reaction_n": (round(reaction, 3) if reaction is not None else None),
                   "note": "box-proxy contact conservation check; base reaction should "
                           "equal the applied load. ccx GPL-2.0 subprocess."}
            _persist_study(d, {"type": "fea_contact",
                               "name": f"Contact ({mat.get('name', material)})",
                               "engine": "calculix", "results": out})
            return _dumps(out)
        except CoreError as e:
            return _err(e)
        except Exception as e:  # noqa: BLE001
            return _err(e)
