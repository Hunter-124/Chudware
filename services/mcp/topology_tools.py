"""Topology optimization MCP tools: native capability discovery and the durable run.

Two tools, one native route. ``topology_capabilities`` forwards the native
``topology_capabilities`` RPC — the only method x physics matrix (no catalog here).
``run_topology_optimization`` takes the SAME canonical study the GUI edits, persists it as
``type:"topology"`` in the document BEFORE the solve starts, resolves its face selectors
and exports the design domain ONCE in this (parent) process, and starts the native
``sim_run_topology`` RPC as a durable ``core_rpc`` job in its own process group. Progress,
cancel and results go through the existing ``get_job`` / ``wait_job`` / ``sim_status`` /
``sim_cancel`` / ``sim_results`` tools; the registered collector runs exactly once per job
(services.mcp.cad_common.collect_durable), writes the run's evidence into its project
directory, runs DFAM on the real exported surface, and appends a compact RunRecord
(host/src/ui/sim_topology.h) to the same study, so the GUI opens what the model ran. A
failed or cancelled job goes through the registered terminal hook under the same
exactly-once claim: its study's last_job becomes "failed"/"cancelled" and the run is
retained with only what the solver actually wrote.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Union

from mcp.server.fastmcp import FastMCP

from services.sim import jobs as simjobs
from services.sim import topology as topo

from .cad_common import (
    DurableCollectRetry,
    cb,
    coerce_json_or_repr,
    register_durable_collector,
    register_durable_terminal,
    solve_durable_or_handoff,
    _doc,
    _dumps,
    _err,
    _resolve_material_name,
)
from .cad_sim_tools import _require_sim_geometry, _sim_face_points, _started

_KIND = "topology"
_RPC = "sim_run_topology"


# ---------------------------------------------------------------- engine seams

def _capabilities() -> dict:
    """The native matrix, or a named refusal when this engine build cannot answer."""
    try:
        reply = cb.call("topology_capabilities", {})
    except Exception as exc:  # noqa: BLE001 — every failure means "cannot offer anything"
        raise topo.TopologyInputError(
            "capabilities_unavailable",
            f"the native topology_capabilities RPC is unavailable in this engine build "
            f"({exc}); no topology method can be offered or run.") from None
    return topo.capability_matrix(reply)


def _named_material(name: str) -> dict:
    try:
        return _resolve_material_name(name)
    except ValueError as exc:
        raise topo.TopologyInputError("missing_material", str(exc), "study.material") from None


def _resolver(doc_id: str):
    """selector -> doc_face_points facesets, for every selector dialect a study may hold."""

    def resolve(selector) -> list:
        if isinstance(selector, dict) and "points" in selector:
            return [dict(selector)]  # an already-resolved faceset passes through unchanged
        try:
            if isinstance(selector, dict) and selector.get("kind") == "face":
                # A GUI face selector is exactly what doc_face_points takes; the
                # viewport-only `_pick` identity stays in the persisted study.
                picked = {k: v for k, v in selector.items() if not str(k).startswith("_")}
                faceset = cb.call("doc_face_points", {"doc_id": doc_id, "selector": picked})
                if not isinstance(faceset, dict) or not faceset.get("points"):
                    raise ValueError("the selected face carries no surface points; re-pick it")
                return [faceset]
            return _sim_face_points(doc_id, selector)
        except topo.TopologyInputError:
            raise
        except Exception as exc:  # noqa: BLE001 — named at the selector that failed
            raise topo.TopologyInputError(
                "missing_selection",
                f"face selector {topo.selector_text(selector)!r} did not resolve: {exc}",
                topo.selector_text(selector)) from None

    return resolve


def _studies(doc_id: str) -> list:
    reply = cb.call("sim_study_list", {"doc_id": doc_id})
    studies = reply.get("studies") if isinstance(reply, dict) else None
    return [s for s in (studies or []) if isinstance(s, dict)]


def _save_study(doc_id: str, study: dict) -> str:
    """Write the study (edit by id, else add) and return its id; failures propagate."""
    if study.get("id"):
        cb.call("sim_study_edit", {"doc_id": doc_id, "study_id": study["id"], "study": study})
        return str(study["id"])
    reply = cb.call("sim_study_add", {"doc_id": doc_id, "study": study})
    sid = reply.get("study_id") if isinstance(reply, dict) else None
    if not sid:
        raise RuntimeError(f"sim_study_add returned no study_id: {reply!r}")
    study["id"] = sid
    return str(sid)


def _mark_last_job(doc_id: str, study_id: str, run_id: str, **changes) -> None:
    """Update last_job of the CURRENT stored study, only while it still names this run.

    A run that already reached a final state (collected/failed/cancelled, possibly by a
    concurrent poll) is never moved back.
    """
    current = next((s for s in _studies(doc_id) if s.get("id") == study_id), None)
    if current is None:
        return
    last = current.get("last_job") if isinstance(current.get("last_job"), dict) else {}
    if last.get("run_id") != run_id or last.get("state") in topo.FINAL_JOB_STATES:
        return
    current["last_job"] = {**last, **changes}
    cb.call("sim_study_edit", {"doc_id": doc_id, "study_id": study_id, "study": current})


# ---------------------------------------------------------------- setup

def _target_study(doc_id: str, raw: Optional[dict], study_id: Optional[str],
                  name: Optional[str]) -> tuple[dict, Optional[dict]]:
    """(merged raw study, persisted study it updates or None)."""
    wanted_id = study_id or (raw or {}).get("id")
    studies = _studies(doc_id)
    existing = None
    if wanted_id:
        existing = next((s for s in studies if s.get("id") == wanted_id), None)
        if existing is None:
            raise topo.TopologyInputError(
                "invalid_input",
                f"document {doc_id} has no study {wanted_id!r}; omit study_id to create one, "
                "or pass the id of an existing topology study.", "study_id")
    elif raw is None:
        raise topo.TopologyInputError(
            "invalid_input",
            "pass study=<canonical topology study> or study_id=<existing topology study id>.",
            "study")
    else:
        lookup = name or raw.get("name")
        existing = next((s for s in studies if s.get("type") == _KIND
                         and lookup and s.get("name") == lookup), None)
    if existing is not None and existing.get("type") != _KIND:
        raise topo.TopologyInputError(
            "invalid_input",
            f"study {existing.get('id')!r} is a {existing.get('type')!r} study; a topology "
            "run never converts another study. Create a new topology study instead.",
            "study_id")
    if existing is not None and raw is not None:
        merged = topo.merge_study(existing, raw)
    else:
        merged = dict(existing if raw is None else raw)
    if name:
        merged["name"] = name
    if existing is not None:
        merged["id"] = existing["id"]
    return merged, existing


def _run(study, doc_id, study_id, name, background, notes: list) -> dict:
    if not isinstance(background, bool):
        raise topo.TopologyInputError("invalid_input", "background must be true or false",
                                      "background")
    d = _doc(doc_id)
    raw = coerce_json_or_repr(study) if study is not None else None
    if raw is not None and not isinstance(raw, dict):
        raise topo.TopologyInputError(
            "invalid_input", f"study must be an object, got {type(raw).__name__}", "study")
    merged, _existing = _target_study(d, raw, study_id, name)
    canonical = topo.normalize_study(merged, notes)
    try:
        _require_sim_geometry(d)
    except ValueError as exc:
        raise topo.TopologyInputError("missing_geometry", str(exc), "doc_id") from None
    caps = _capabilities()
    combo = topo.select_combination(caps, canonical["topology"])
    topo.apply_print_process(canonical, combo, notes)
    settings = topo.run_settings(canonical["topology"], combo)
    topo.require_topology_settings(canonical, combo)
    physics = settings["physics"]
    topo.require_run_inputs(canonical, physics)
    material = topo.run_material(canonical.get("material"), physics, _named_material, notes)
    # Every selector resolves against the live document HERE, before anything is written:
    # the child shares no document table and a bad pick must fail this call, not the job.
    envelope = topo.build_envelope(canonical, settings=settings, material=material,
                                   resolve=_resolver(d), step_path="", result_dir="",
                                   notes=notes)

    run_id = topo.new_run_id()
    run_dir = topo.allocate_run_dir(canonical["name"], run_id,
                                    result_dir=canonical["topology"].get("result_dir"))
    step = run_dir / "source.step"
    cb.call("doc_export_step", {"doc_id": d, "path": str(step)})
    if not step.is_file() or step.stat().st_size == 0:
        raise topo.TopologyInputError(
            "missing_geometry", f"exporting the design domain produced no STEP at {step}",
            "doc_id")
    envelope["step_path"] = str(step)
    envelope["topology"]["result_dir"] = str(run_dir)
    started = time.time()
    source = topo.file_record(step)
    inputs = topo.write_json_new(run_dir, "inputs.json", envelope)
    snapshot = {k: v for k, v in canonical.items() if k not in ("runs", "last_job")}
    setup = topo.write_json_new(run_dir, "setup.json", {
        "run_id": run_id, "doc_id": d, "submitted_utc": started, "study": snapshot,
        "settings": settings, "capability": combo, "material": material, "notes": notes})
    label = (f"{settings['method']} {physics}"
             + (f" vf={settings['volume_fraction']:g}"
                if isinstance(settings.get("volume_fraction"), (int, float)) else ""))
    canonical["last_job"] = {"run_id": run_id, "state": "submitting",
                             "submitted_utc": started, "artifact_dir": str(run_dir)}
    try:
        sid = _save_study(d, canonical)
    except Exception as exc:  # noqa: BLE001 — an invisible study would break GUI parity
        raise topo.TopologyInputError(
            "invalid_input",
            f"the topology study could not be saved into document {d} ({exc}); nothing was "
            "started, so the GUI never shows a run it cannot open.", "doc_id") from None

    meta = {"analysis": _KIND, "doc": d, "study_id": sid, "study_name": canonical["name"],
            "run_id": run_id, "run_dir": str(run_dir),
            "project_root": str(topo.project_jail()), "label": label,
            "started_utc": started, "settings": settings, "source": source,
            "inputs": inputs, "setup": setup, "notes": list(notes),
            "print_process": canonical.get("print_process"),
            "material_key": (canonical["material"] if isinstance(canonical.get("material"), str)
                             else (canonical.get("material") or {}).get("key")),
            "formulation": combo.get("formulation"), "volume_phase": combo.get("volume_phase")}
    payload = {"method": _RPC, "params": {"study": envelope}}
    common = {
        "study_id": sid, "study_name": canonical["name"], "run_id": run_id,
        "artifact_dir": str(run_dir), "method": settings["method"], "physics": physics,
        "objective": settings.get("objective") or combo["objectives"][0],
        "formulation": combo.get("formulation"),
        "volume_phase": combo.get("volume_phase")
        or ("fluid" if physics in topo.FLOW_PHYSICS else "solid"),
        "paths": [source["path"], inputs["path"], setup["path"]],
    }
    try:
        if background:
            job = simjobs.start(_KIND, "core_rpc", payload, meta)
        else:
            reply = solve_durable_or_handoff(_KIND, "core_rpc", payload, meta)
    except ValueError as exc:
        _mark_last_job(d, sid, run_id, state="refused", error=str(exc))
        raise topo.TopologyInputError(
            "invalid_input", f"the solve refused to start: {exc}", "study") from None
    if background:
        _mark_last_job(d, sid, run_id, state="running", job_id=job["job_id"])
        out = _started(job, d)
        out.update(common)
        out["next_actions"] = [f"wait_job('{job['job_id']}')", f"get_job('{job['job_id']}')",
                               f"sim_cancel('{job['job_id']}')"]
    else:
        out = dict(reply)
        # A failed/cancelled solve was already persisted into the study, exactly once, by
        # the terminal hook below (collect_durable); only a handoff still needs marking.
        if out.get("auto_backgrounded"):
            _mark_last_job(d, sid, run_id, state="running", job_id=out.get("job_id"))
        for key, value in common.items():
            if key == "paths":
                out["paths"] = list(out.get("paths") or []) + value
            else:
                out.setdefault(key, value)
    if notes:
        out["notes"] = list(dict.fromkeys(list(out.get("notes") or []) + notes))
    return out


# ---------------------------------------------------------------- collect (exactly once)
# Finished, failed and cancelled jobs all end in the SAME study through one path: the
# evidence is written into the run directory once (and recorded in the job spool), then
# one RunRecord is appended. A document write that fails retryably raises
# DurableCollectRetry, so collect_durable releases its claim without caching and the next
# poll appends the same record from the same evidence.

def _append_run(doc_id: str, study_id: str, record: dict,
                state: str) -> tuple[bool, Optional[str], dict, bool]:
    """(saved, reason, info, retryable). A deleted study is final, never re-created."""
    try:
        studies = _studies(doc_id)
    except Exception as exc:  # noqa: BLE001 — the run itself is retained on disk
        return False, (f"document {doc_id} is not reachable from this process ({exc}); the "
                       f"run is retained in {record['artifact_dir']}"), {}, True
    current = next((s for s in studies if s.get("id") == study_id), None)
    if current is None:
        return False, ("the study was deleted from the document; the run is retained in "
                       f"{record['artifact_dir']} and the study is not re-created"), {}, False
    if current.get("type") != _KIND:
        return False, f"study {study_id} no longer holds a topology study", {}, False
    updated, info = topo.apply_run_record(current, record, state=state)
    if not info["duplicate"]:
        try:
            cb.call("sim_study_edit", {"doc_id": doc_id, "study_id": study_id,
                                       "study": updated})
        except Exception as exc:  # noqa: BLE001
            return False, f"sim_study_edit failed: {exc}", info, True
    return True, None, info, False


def _last_progress(job_id: str) -> Optional[dict]:
    """The worker's last published progress scalars (phase, iteration, ...), if any."""
    snap = simjobs.partial(job_id)
    if not snap:
        return None
    out = {k: v for k, v in snap.items()
           if not str(k).startswith("_") and k not in ("partial", "converged", "job_id")
           and not isinstance(v, (dict, list))}
    return out or None


def _print_handoff(result: dict, artifacts: dict, settings: dict, meta: dict) -> Optional[dict]:
    surface = topo.surface_artifact(artifacts)
    if surface is None or (result.get("manufacturing") or {}).get("geometry_valid") is not True:
        return None
    key, entry = surface
    path = entry["path"]
    build, source = topo.effective_build_direction(result, settings)
    handoff = {"surface_path": path, "surface_artifact": key, "build_dir": build,
               "build_dir_source": source, "slice_part": {"path": path, "build_dir": build},
               "import_part": {"path": path, "as_solid": True}}
    if meta.get("print_process"):
        handoff["print_process"] = meta["print_process"]
    material = {"pla": "PLA", "petg": "PETG", "abs": "ABS", "asa": "ASA", "tpu": "TPU"}.get(
        str(meta.get("material_key") or "").lower())
    if material:
        handoff["material"] = material
        handoff["slice_part"]["material"] = material
    return handoff


def _write_evidence(st: dict, raw: Optional[dict], state: str) -> dict:
    """Write this run's evidence into its directory ONCE: {result, paths, notes}.

    ``raw`` is the solver's reply (``state`` done), its failure reply, or None when the
    worker failed before replying or was killed — then only the durable files the native
    run wrote before stopping are registered.
    """
    job_id = str(st.get("job_id"))
    stashed = simjobs.collect_evidence(job_id)
    if stashed is not None:
        return stashed
    meta = st.get("meta") or {}
    settings = meta.get("settings") or {}
    notes = list(meta.get("notes") or [])
    run_dir = Path(meta.get("run_dir") or "")
    root = meta.get("project_root")
    usable = bool(meta.get("run_dir")) and run_dir.is_dir() and bool(root) and topo.contained(
        run_dir, root)
    finished = state == simjobs.DONE
    recovered_entries: dict = {}
    if finished:
        result = dict(raw or {})
    else:
        result = topo.unfinished_result(raw, state=state,
                                        error=str(st.get("error") or f"solve {state}"),
                                        settings=settings)
        if raw is None and usable:
            recovered_entries, recovered, rec_notes = topo.recover_native_run(run_dir, settings)
            notes.extend(rec_notes)
            if recovered:
                result["recovered"] = recovered
        progress = _last_progress(job_id)
        if progress:
            result["last_progress"] = progress
    if usable:
        compact, bulk = topo.externalize_bulk(result, run_dir)
    else:
        compact = {k: v for k, v in result.items() if k not in topo.BULK_RESULT_KEYS}
        bulk = {}
        notes.append(f"run directory {run_dir} is missing or outside the project; nothing "
                     "was written and every artifact is reported missing")
    overlay = compact.pop("overlay_aicm", None)
    reported = dict(compact.get("artifacts") or {}) if isinstance(
        compact.get("artifacts"), dict) else {}
    reported.update(recovered_entries)
    if usable:
        artifacts, art_notes = topo.verify_artifacts(reported, run_dir, overlay)
    else:
        artifacts = {key: {**entry, "missing": True}
                     for key, entry in reported.items() if isinstance(entry, dict)}
        art_notes = []
    artifacts.update(bulk)
    notes.extend(art_notes)
    compact["artifacts"] = artifacts
    if finished:
        process = topo.print_process(meta["print_process"]) if meta.get("print_process") else None
        compact["manufacturing"] = topo.dfam_report(compact, artifacts, settings, process)
        compact["topology"] = {**settings, **(compact.get("topology") or {})}
        handoff = _print_handoff(compact, artifacts, settings, meta)
        if handoff is not None:
            compact["print_handoff"] = handoff
    source = meta.get("source") or {}
    inputs = meta.get("inputs") or {}
    provenance = {k: v for k, v in (compact.get("provenance") or {}).items()
                  if not isinstance(v, (dict, list))}
    provenance.update(job_id=job_id, run_id=meta.get("run_id"),
                      source_step_sha256=source.get("sha256"),
                      inputs_sha256=inputs.get("sha256"), artifact_dir=str(run_dir))
    compact["provenance"] = provenance
    paths: list = []
    if usable:
        prov = topo.write_json_new(run_dir, "provenance.json", {
            "job_id": job_id, "run_id": meta.get("run_id"), "doc_id": meta.get("doc"),
            "study_id": meta.get("study_id"), "job_state": state,
            "collected_utc": time.time(), "submitted_utc": meta.get("started_utc"),
            "source_step": source, "inputs": inputs, "setup": meta.get("setup"),
            "artifacts": artifacts, "engine": ((raw or {}).get("provenance") or {}),
            "notes": notes})
        compact["artifacts"]["provenance"] = {**prov, "format": "json",
                                              "label": "run provenance (hashes)"}
        result_file = topo.write_json_new(run_dir, "result.json", compact)
        paths = [result_file["path"], prov["path"]]
        paths += [e["path"] for e in artifacts.values()
                  if not e.get("missing") and e.get("path") not in paths]
        if source.get("path"):
            paths.append(source["path"])
    evidence = {"result": compact, "paths": paths, "notes": notes}
    if not simjobs.save_collect_evidence(job_id, evidence):
        evidence["notes"] = notes + ["the job spool could not record this run's evidence; "
                                     "a retried collect would write a second copy"]
    return evidence


def _collect(st: dict, raw: Optional[dict], state: str) -> dict:
    meta = st.get("meta") or {}
    job_id = st.get("job_id")
    run_dir = str(meta.get("run_dir") or "")
    evidence = _write_evidence(st, raw, state)
    compact = evidence["result"]
    notes = list(evidence["notes"])
    record = topo.run_record(
        str(meta.get("run_id")), label=str(meta.get("label") or meta.get("run_id")),
        started_utc=meta.get("started_utc"), settings=meta.get("settings") or {},
        result=compact, job_id=str(job_id), artifact_dir=run_dir)
    final = "collected" if state == simjobs.DONE else state
    saved, reason, info, retryable = _append_run(str(meta.get("doc")),
                                                 str(meta.get("study_id")), record, final)
    oc = topo.outcome(compact)
    out = {"ok": compact.get("ok") is True, "job_id": job_id, "study_id": meta.get("study_id"),
           "study_name": meta.get("study_name"), "run_id": meta.get("run_id"),
           "saved_to_study": saved, "outcome": oc, "converged": oc["converged"],
           "feasible": oc["feasible"], "termination": oc["termination"],
           "print_ready": compact["manufacturing"]["print_ready"] is True,
           "artifact_dir": run_dir, "paths": list(evidence["paths"]), "result": compact}
    if state == simjobs.DONE and "print_handoff" in compact:
        out["print_handoff"] = compact["print_handoff"]
    if state != simjobs.DONE:
        out.update(state=state, error=compact.get("error"))
    if reason:
        out["study_not_saved_reason"] = reason
    if info.get("duplicate"):
        out["duplicate_collection"] = True
    if info.get("superseded_by"):
        out["superseded_by_run"] = info["superseded_by"]
        notes.append(f"a newer run ({info['superseded_by']}) was submitted for this study; "
                     "this run is retained but does not replace the study's latest result")
    if notes:
        out["notes"] = notes
    if retryable:
        raise DurableCollectRetry(out)
    return out


def _collect_topology(st: dict, raw: dict) -> dict:
    """COLLECT stage: evidence into the run directory, DFAM, RunRecord into the study."""
    return _collect(st, raw, simjobs.DONE)


def _collect_unfinished_topology(st: dict, reply: Optional[dict]) -> dict:
    """Terminal hook: a failed or cancelled run, retained once with what the solver wrote."""
    return _collect(st, reply, str(st.get("state")))


register_durable_collector(_KIND, _collect_topology)
register_durable_terminal(_KIND, _collect_unfinished_topology)


# ---------------------------------------------------------------- tools

def register_topology_tools(mcp: FastMCP) -> None:
    """Topology optimization: native capability matrix + durable study run."""

    @mcp.tool()
    def topology_capabilities() -> str:
        """Read-only. The native topology-optimization capability matrix — call FIRST.

        Returns `capabilities` exactly as the engine's topology_capabilities RPC reports
        it: methods (simp | ramp | beso | level_set), physics (structural | thermal | modal |
        buckling | flow | thermomechanical | cht) and one `combinations` row per pair with
        `supported`, `reason` (why not), `formulation`, `volume_phase` ("solid" budgets
        material; "fluid" budgets channels for flow/cht — rho=1 is always solid),
        `objectives` (first = default), `limits` (optional response-limit keys) and
        `parameters` (accepted topology{} keys) plus solver `defaults`. Only listed,
        supported pairs can run; nothing else is offered. `boundaries` lists which study
        sections each physics consumes, including build_plate. `print_processes` lists
        shared print presets (id, label, process, description, topology). When the engine
        build has no topology solver this returns ok:false, code
        "capabilities_unavailable" — do not substitute a guess.
        """
        try:
            caps = _capabilities()
        except topo.TopologyInputError as exc:
            return _dumps({**exc.payload(), "available": False})
        except Exception as e:  # noqa: BLE001
            return _err(e)
        pairs = sorted(f"{c.get('method')}x{c.get('physics')}" for c in caps["combinations"]
                       if isinstance(c, dict) and c.get("supported") is True
                       and c.get("objectives"))
        boundaries = {
            p: ([*(["fixtures", "loads", "load_cases"] if p in topo.STRUCTURAL_PHYSICS else []),
                 *(["thermal"] if p in topo.THERMAL_PHYSICS else []),
                 *(["flow"] if p in topo.FLOW_PHYSICS else []),
                 "preserve", "exclude", "build_plate"])
            for p in topo.PHYSICS}
        return _dumps({"ok": True, "available": True, "capabilities": caps,
                       "supported_pairs": pairs, "boundaries": boundaries,
                       "print_processes": [
                           {key: process[key] for key in
                            ("id", "label", "process", "description", "topology")}
                           for process in topo.print_processes()],
                       "material_required": dict(topo.REQUIRED_MATERIAL),
                       "units": "geometry mm; force N; pressure/stress MPa; thermal W, C, "
                                "W/mK, W/m2K; flow m/s, Pa, Pa*s, kg/m3; density g/cm3"})

    @mcp.tool()
    def run_topology_optimization(
        study: Union[dict, str, None] = None,
        doc_id: Optional[str] = None,
        study_id: Optional[str] = None,
        name: Optional[str] = None,
        background: bool = True,
    ) -> str:
        """Mutating. Run topology optimization on the active part as a persisted, GUI-visible study.

        Call topology_capabilities first: only a supported method x physics pair runs, and
        topology{} may hold only that pair's `parameters`/`limits`. The study is the same
        canonical object the Simulation tab edits; it is saved into the document as
        type "topology" BEFORE the solve, and the finished run is appended to its `runs`.

        Arguments
          study: canonical topology study (below). Omit it with study_id to re-run a study
            exactly as stored (e.g. one the user set up in the GUI). With both, the given
            keys overlay the stored study (topology{} merges key by key); unknown keys,
            prior runs and GUI selector metadata are preserved.
          doc_id: document (default: the active part). study_id / name: target an existing
            topology study; a same-named topology study is updated, never duplicated.
          background: true (default) returns a job id at once; poll wait_job/get_job,
            stop with sim_cancel. false waits up to the sync budget, then hands back the id.

        Study shape (units: mm, N, MPa, W, C, W/mK, W/m2K, m/s, Pa, Pa*s, g/cm3)
          {name, material: "<library key>" | {key?, E_mpa, nu, density_g_cm3, yield_mpa?,
             k_w_mk?, alpha_per_k?, cp_j_kgk?}, mesh_order: 1 (tet4 only),
           mesh_target_mm?, mesh_fineness? (0..1), mesh_min_mm?,
           print_process?: "<print_processes id>", build_plate?: {faces:[sel], label?},
           topology: {physics, method, objective?, volume_fraction, max_iterations?,
             filter_radius_mm?, penalty?, ... limits such as stress_limit_mpa,
             symmetry_axes?, build_direction?, overhang_angle_deg?, min_member_mm?,
             result_dir? (under the project only; each run gets its own new subfolder)},
           preserve / exclude: [{faces:[sel], thickness_mm?} | {box_min_mm:[3], box_max_mm:[3]}]}
          A face selector (sel) is a face query ("top", {"of":"face","side":"-x"}), a face
          sid from describe_part, or a GUI face selector; it is resolved on the live part.
          print_process fills absent topology parameters offered by the chosen pair;
          explicit settings win, filled keys are saved and reported in notes. build_plate
          selects the planar, lowest bed face; its inward normal supplies the build direction.
          Overhang control needs build_plate or build_direction; its default angle is 45 deg.

        Boundaries by physics (sections a physics does not use stay in the study, unsent):
          structural       fixtures + loads (+ load_cases); objective compliance, or mass
                           with explicit response limits
          modal            fixtures; objective frequency (material density_g_cm3 required)
          buckling         fixtures + non-zero loads (the pre-stress); buckling_factor
          thermal          thermal{fixtures, loads}; thermal_compliance (k_w_mk)
          thermomechanical fixtures (+ loads) AND thermal{...}; compliance includes thermal
                           expansion (k_w_mk, alpha_per_k)
          flow             flow{viscosity_pa_s, density_kg_m3, inlets, outlets, walls?};
                           dissipation; volume_fraction budgets FLUID
          cht              flow{... fluid_k_w_mk, fluid_cp_j_kgk} AND thermal{loads};
                           mean_temperature; volume_fraction budgets FLUID
          fixtures: [{faces:[sel], dofs?:[0,1,2]}]   (dofs omitted = all; [2] = Z only)
          loads: [{faces:[sel], type:"force", vec:[fx,fy,fz]} | {faces, type:"pressure",
                  magnitude: MPa inward}]
          load_cases: [{name, weight>0, fixtures?:[...], loads:[...]}]
          thermal: {reference_temperature_c?, fixtures:[{faces, temperature_c}],
                    loads:[{faces, type:"flux", magnitude_w_m2} | {faces, type:"power",
                    power_w} | {faces, type:"convection", h_w_m2k, ambient_c}]}
          flow: {viscosity_pa_s, density_kg_m3, inlets:[{faces, velocity_m_s:[3],
                 temperature_c?}], outlets:[{faces, pressure_pa}], walls?:[{faces}],
                 fluid_k_w_mk?, fluid_cp_j_kgk?, brinkman_max?, reference_length_mm?}

        Structural example
          run_topology_optimization(study={"name": "Bracket topology",
            "material": "6061-T6", "mesh_order": 1, "mesh_target_mm": 3,
            "topology": {"physics": "structural", "method": "simp",
                         "objective": "compliance", "volume_fraction": 0.35,
                         "filter_radius_mm": 4, "max_iterations": 120},
            "fixtures": [{"faces": ["-x"]}, {"faces": ["bottom"], "dofs": [2]}],
            "loads": [{"faces": ["+x"], "type": "force", "vec": [0, 0, -800]}],
            "preserve": [{"faces": ["+x"], "thickness_mm": 3}]})

        FDM example (PLA bracket; choose bed and load faces on the actual part)
          run_topology_optimization(study={"name": "Printed PLA bracket",
            "material": "pla", "print_process": "fdm_0.4",
            "build_plate": {"faces": ["bottom"]}, "mesh_target_mm": 1,
            "topology": {"physics": "structural", "method": "simp",
                         "objective": "compliance", "volume_fraction": 0.3},
            "fixtures": [{"faces": ["-x"]}],
            "loads": [{"faces": ["+x"], "type": "force", "vec": [0, 0, -50]}]})

        Results (get_job/wait_job when done): `ok` = the computation finished, NOT a
        certified design; `converged` and `feasible` are separate (an iteration limit or a
        violated constraint is a finished, unconverged/infeasible run — report it as such).
        `result` follows the native result (summary, history, constraints, manufacturing,
        artifacts with sha256); density/field arrays are files in `artifact_dir`, which lives
        under the project (never the job spool) and is never overwritten.
        `manufacturing.dfam` is the DFAM check of the exported surface (run only when the
        solver validated the thresholded geometry). `print_ready` is always false here:
        only slice_part's passing G-code check makes a part print-ready, and this tool does
        not slice; `print_ready_reason` says what passed and what is missing. Failures name
        the missing selection, material, geometry or unsupported combination (`code`, `field`).
        With validated geometry and an intact surface, `print_handoff` gives the preferred
        print surface path, effective build_dir/source and slice_part arguments. Add the
        user's printer: slice_part(printer=<user's printer>, **print_handoff["slice_part"]).
        Continue in CAD with import_part(path=print_handoff["surface_path"], as_solid=True)
        (also supplied as print_handoff.import_part); the original feature history stays editable.
        """
        notes: list = []
        try:
            return _dumps(_run(study, doc_id, study_id, name, background, notes))
        except topo.TopologyInputError as exc:
            out = exc.payload()
            if notes:
                out["notes"] = notes
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)
