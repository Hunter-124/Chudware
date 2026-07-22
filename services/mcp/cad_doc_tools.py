"""CAD document-lifecycle MCP tools (T-RF8 split of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP  # noqa: F401 — register signature
from typing import Any
from typing import Optional
from typing import Union
import json
import os

from .cad_common import (
    _CUR,
    _DOC_ALIASES,
    _JOBS,
    _JOB_LOCK,
    _LAST_DELIVER_REV,
    _LAST_VOLUME,
    _autosave,
    _doc,
    _doc_revision,
    _dumps,
    _err,
    _face_hist,
    _publish_current,
    _resolve_alias,
    _resolve_appearance,
    _sanitize_output_path,
    _solve_sync_or_handoff,
    _start_job,
    _tmp,
    _unpublish,
    _write_diff,
    cb,
    recover_autosaved,
)

from .cad_guide import (
    cad_guide_text,
)

def register_cad_doc_tools(mcp: FastMCP) -> None:
    """CAD document-lifecycle MCP tools (T-RF8 split of cad_tools.py): new_part, add_features, get_part, undo/redo, save/close/recover, configurations, material, engine_profile, cad_guide."""


    # ---------------------------------------------------------------- knowledge

    @mcp.tool()
    def cad_guide(topic: str = "overview") -> str:
        """Return the engine's modeling/simulation knowledge so you can drive it well. CALL FIRST.

        This is the same closed op vocabulary + worked examples the in-app agent's modeler is
        taught. topic="overview" (default) gives the workflow + full op cheatsheet (primitives,
        sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell/draft, holes, bolt_circle,
        patterns/mirror, edits) and the plain-English geometry-query predicates. Other topics:
        "assembly" (new_assembly/asm_add_component/asm_mate/solve_mates — put saved parts
        together with mates so they actually fit; seating patterns + validation loop),
        "simulation" (FEA/CFD how-to), "motion" (simulate_motion — drive a mate to animate an
        assembly + swept interference, kinematics), "review" (how to read the annotated render), "gsl" (the
        progressive TEXT read of a part's geometry — describe_part/describe_sketch/
        describe_scene/hover/query_adjacency — including the non-vision self-review path),
        "narration" (how to use describe_toolpaths and describe_results to get compact
        text summaries of CAM jobs and FEA/CFD results instead of raw JSON dumps),
        "parts" (standard-parts library: search→get→place workflow, standard-before-custom
        bias, parts_taxonomy category browse + faceted part_search, domain routing,
        editability guarantee, sourcing), "reverse"
        (reverse-engineering an imported STEP/mesh into an editable feature tree),
        "requirements" (req_create/update/status/report — design-brief CRUD),
        "dag" (dag_plan/run/status/approve/abort/replan — multi-step mission orchestration),
        "verify" (verify_battery — done-check before declaring a design finished),
        "interaction" (ask_user elicitation seam — batch unknowns into one call),
        "sourcing" (part_source need→ranked candidates, artifact plans (place / scrape+import /
        make_standard_part), vendors_health login etiquette, cam_tool_import cutting tools,
        bom_rollup/bom_price + request_quote),
        "cost" (cam_cost_estimate/request_quote — made-part cost model),
        "spec" (match_part_number/spec_sheet_match — MPN resolution + datasheet extraction),
        "piping" (piping_solve/report/... — 1-D hydraulic solve + CFD handoff),
        "hydraulic" (hydraulic_new/add_component/... — ISO 1219 circuit design),
        "firmware" (fw_new_project/build/sim_run/flash/debug — embedded compile loop),
        "structural" (struct_analyze/check/estimate — PyNite member forces + code check),
        "mechanical" (gear_check/bearing_life — AGMA gear tooth stress + ISO 281 bearing life),
        "regen" (regen_diagnose — diagnose + propose fix-ops on rebuild failure),
        "code_compliance" (code_check — building-code compliance with RAG citations),
        "serving" (serving_status/set_canary/promote/rollback — LoRA adapter routing),
        "design_review" (design_review — one-call geometry battery narrated verdict),
        "doc_diff" (doc_diff_tool — semantic feature-level diff between two revisions),
        "plm" (project-vault versioning stub — T-R2 fills this),
        "tolerance_stackup" (stackup_analyze — 1D worst-case + RSS tolerance stack-up),
        "sketch3d" (sketch3d_new/add_curves/get/as_sweep_guide + render_sketch/
        sketch_preview_text/doc_sketch_contact_sheet — 3D free-space sketch curves
        usable as sweep spines/guide curves, and 2D sketch preview before extruding),
        or a single feature's cheatsheet by name ("hole", "fillet", "revolve", "loft",
        "sweep", "shell", "chamfer", "pattern", "mirror", "sketch", "airfoil",
        "drawing", "hardware", ...).
        """
        return cad_guide_text(topic)



    # ---------------------------------------------------------------- diagnostics

    @mcp.tool()
    def engine_profile(action: str = "status", hz: int = 0, mode: str = "modeling",
                       top: int = 40, min_samples: int = 0,
                       write_folded: str = "", write_speedscope: str = "") -> str:
        """Profile THIS engine process (the one your modeling RPCs run in) at runtime.

        The desktop app's profiler window only sees the GUI's own engine — a DIFFERENT process
        from this MCP server, where subagent modeling actually happens. This tool drives the
        engine's built-in sampling profiler + lock-contention meters in-process, so a heavy
        multi-subagent run can be measured without a restart. All modeling in one engine
        serialises under a single global lock; the `contention` action is what reveals time
        spent BLOCKED on that lock (which the CPU sampler cannot see).

        action:
          "start"       begin sampling. hz sets the rate (default ~1kHz); mode "modeling"
                        (default) samples only modeling ops, "all" samples every RPC.
          "stop"        stop sampling and return the symbolized report.
          "report"      return the report WITHOUT stopping (modules + hot frames + folded stacks).
          "status"      (default) is the sampler running? how many samples so far?
          "contention"  return g_mtx lock-wait/hold/queue-depth stats since last reset — the
                        primary multi-subagent bottleneck signal (%wall blocked, top callers).
          "reset_contention"  zero the contention counters (call before a measured run).
          "phases"      per-study simulation phase timings (always-on, no start needed):
                        every mesher / FEA static / modal / thermal / fatigue / LBM /
                        flow / overlay run as {domain, total_ms, phases:[{name,ms,pct}]}
                        — mesh vs assemble vs factorize vs post, per study. The same
                        breakdown also rides along on "stop"/"report" under "phases".
          "reset_phases"  clear the phase registry (call before a measured run).

        top/min_samples/write_folded/write_speedscope tune the sampling report (see PROFILER.md).
        """
        try:
            a = (action or "status").strip().lower()
            if a == "start":
                p = {"mode": mode}
                if hz and hz > 0:
                    p["hz"] = int(hz)
                return _dumps(cb.call("prof_start", p))
            if a == "stop":
                return _dumps(cb.call("prof_stop", {"top": top, "min_samples": min_samples,
                                                    "write_folded": write_folded or None,
                                                    "write_speedscope": write_speedscope or None}))
            if a == "report":
                return _dumps(cb.call("prof_report", {"top": top, "min_samples": min_samples,
                                                      "write_folded": write_folded or None,
                                                      "write_speedscope": write_speedscope or None}))
            if a == "status":
                return _dumps(cb.call("prof_status", {}))
            if a == "contention":
                return _dumps(cb.call("prof_contention", {}))
            if a == "reset_contention":
                return _dumps(cb.call("prof_contention", {"reset": True}))
            if a == "phases":
                return _dumps(cb.call("prof_phases", {}))
            if a == "reset_phases":
                return _dumps(cb.call("prof_phases", {"reset": True}))
            return _err(ValueError(
                f"unknown action {action!r}; use start|stop|report|status|contention|"
                f"reset_contention|phases|reset_phases"))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- modeling

    @mcp.tool()
    def new_part(name: str = "part") -> str:
        """Create a new, empty editable part document and make it current. Returns its doc_id.

        Follow with add_features(...) to build geometry. Each part is a real parametric feature
        tree (undo/redo, editable parameters), not a one-shot mesh.
        """
        try:
            r = cb.call("doc_new", {"kind": "part", "name": name})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            # Seed the write-diff baseline: an empty part has zero volume, so the FIRST
            # add_features reports a real volume delta ("0.0 -> X (+X)") instead of degrading
            # to the volume-only "volume now X" branch of _write_diff.
            _LAST_VOLUME[did] = 0.0
            return _dumps({"ok": True, "doc_id": did, "name": name,
                           "revision": r.get("revision", 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_features(ops: list, doc_id: Optional[str] = None, background: bool = False) -> str:
        """Apply a list of document ops to build/edit the part (the core modeling tool).

        `ops` is a JSON array of ops from the CLOSED vocabulary (see OP VOCABULARY below
        and cad_guide()) — e.g.
          [{"op":"add_feature","feature":{"type":"box","name":"Block",
              "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
                        "height":{"value":20,"expr":null}}}},
           {"op":"add_feature","feature":{"type":"fillet","name":"Round",
              "params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}]
        Dimensions are objects {"value":<n>,"expr":null}. Geometry is addressed in plain words
        via `refs` (e.g. {"face":{"of":"face","side":"top"}}). Ops in one call may reference an
        earlier op in the SAME array as "$<index>" (0-based); existing tree features by id ("f1").

        OP VOCABULARY (closed — do NOT invent names):
          ops (field op=…): add_feature, edit_feature, delete_feature, reorder_feature,
            set_suppressed, set_folder, rename, set_units, set_material, set_appearance,
            set_rollback, eq_set, eq_delete, sketch_add_entity, sketch_add_constraint,
            sketch_edit_entity, sketch_delete_entity, sketch_delete_constraint, sketch_set_dim,
            sketch_toggle_construction, sketch_convert, sketch_break_external_ref, sketch_trim,
            sketch_extend, sketch_offset, sketch_fillet, sketch_chamfer, sketch_mirror_entities,
            sketch_pattern_entities, sketch_move, sketch_rotate, asm_insert_component,
            asm_edit_component, asm_delete_component, asm_add_mate, asm_edit_mate,
            asm_delete_mate, asm_set_explode, copy_feature, paste_feature,
            paste_feature_as_derived, sketch_paste_entities
          feature types (ONLY under op=add_feature): box, cylinder, sphere, cone, torus,
            sketch, extrude, revolve, loft, sweep, helix, fillet, chamfer, shell, draft,
            hole, bolt_circle, linear_pattern, circular_pattern, mirror, transform, boolean,
            rib, split, thread, text, import, script, plane, axis, point, csys, sketch3d, …
          WRONG: {"op":"box",…} / {"op":"sketch",…} / {"op":"extrude",…} / {"op":"cylinder",…}
          RIGHT: {"op":"add_feature","feature":{"type":"box"|"cylinder"|"sketch"|…, …}}
          Live list: engine doc_list_ops (sourced from core/doc/op_registry — single source).
          Call cad_guide() for worked examples and full param shapes.

        Returns revision, body_count, volume_mm3, and per-feature status (ok / error / warning)
        so you can immediately tell a dropped cut, a failed reference, or a stray extra body.
        VERIFY LOOP (after every mutation): call render_part() or render_views() to SEE the
        result, then measure_part()/query_geometry()/inspect_feature(target) on the changed
        region, and before declaring done run verify_battery(doc_id). On a validation error
        the whole batch is rejected — read `error`, fix the ops, call again. A rebuild-failure
        response may also carry `regen_diagnosis`: an IP-18 fix-preview card (diagnosis +
        editable fix_ops + verifier verdict) — record your verdict with regen_resolve(card, action).

        SKETCH MUTATIONS: any batch that includes sketch_add_entity / sketch_add_constraint /
        sketch_set_dim / sketch_fillet / … auto-appends a compact ASCII + NL readback
        (`ascii_block`, `sketch_summary`, `findings`) for each touched sketch. READ that
        readback before the next sketch edit. Before declaring a sketch done, call
        render_sketch_ascii(sketch) + sketch_health(sketch) (or check_sketch) and require dof 0.

        HEAVY OPS: a costly op (a global fillet/chamfer/shell/boolean on a many-edged body) is
        run on a worker thread and AUTO-HANDED-OFF to the background if it overruns the call
        budget, so it can never block past the MCP transport timeout (which would otherwise drop
        the whole session). If that happens you get {job_id, status:"running"} — poll get_job().
        All open docs are autosaved BEFORE the op, so nothing is lost even on a hard timeout.
        Pass background=True to hand off immediately for an op you already know is heavy.
        """
        try:
            d = _doc(doc_id)

            def _apply() -> dict:
                # Transactional batch: snapshot first so a partial failure rolls the WHOLE batch
                # back (the engine otherwise leaves failed features in the tree). All-or-nothing.
                ckpt = None
                try:
                    ckpt = cb.call("doc_checkpoint", {"doc_id": d}).get("checkpoint")
                except Exception:  # noqa: BLE001
                    ckpt = None

                # GSL Stage 1a — snapshot the PRE-op topology for the write-diff (W2).
                before_hist = _face_hist(d)
                before_vol = _LAST_VOLUME.get(d)

                # T-W12 auto-preflight: dry-run on shadow clone before mutating.
                # Freedom rule: fail verdict never blocks — we proceed regardless;
                # if verdict==fail we log a preflight_overridden event after apply.
                _pf_result: dict = {}
                try:
                    from services.mcp.preflight_tools import run_auto_preflight
                    _pf_result = run_auto_preflight(d, "add_features", ops)
                except Exception:  # noqa: BLE001
                    pass

                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
                fs = (r.get("rebuild") or {}).get("feature_status", [])
                feats = [{"id": s.get("id"), "ok": s.get("ok"),
                          "error": s.get("error"), "warning": s.get("warning")} for s in fs]
                bodies = (r.get("tess") or {}).get("bodies", [])
                ok = all(s.get("ok") for s in fs) if fs else True

                if not ok and ckpt:
                    # Roll the tree back to the pre-batch revision; report WHY each feature failed.
                    reverted = False
                    try:
                        cb.call("doc_revert", {"doc_id": d, "checkpoint": ckpt})
                        reverted = True
                    except Exception:  # noqa: BLE001
                        reverted = False
                    fail = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                            if not s.get("ok")]
                    _publish_current(d)  # mirror the rolled-back (or current) state to the app
                    fail_result = {
                        "ok": False,
                        "doc_id": d,
                        "rolled_back": reverted,
                        "error": "batch rejected — no features were kept; fix and resend"
                                 if reverted else "batch failed and rollback failed",
                        "failed_features": fail,
                        "features": feats,
                    }
                    # T-BL22: rebuild failure -> IP-18 regen_diagnose fix-preview card
                    # (T-H11 seam). Diagnoses the FAILED tree from the ops response (the
                    # doc itself was already rolled back above), logs the model call as an
                    # IP-18 correction trace, and attaches the accept/edit/reject card to
                    # the failure response for the copilot/caller to surface. Best-effort:
                    # any error (no provider, no tree) degrades to the plain failure dict.
                    try:
                        failed_tree = r.get("tree") or (r.get("rebuild") or {}).get("tree")
                        if failed_tree:
                            from services.mcp.regen_tools import diagnose_failed_document
                            engine_err = "; ".join(
                                str(f.get("error")) for f in fail if f.get("error")) or None
                            diag = diagnose_failed_document(
                                {"revision": r.get("revision", 0), "tree": failed_tree,
                                 "owner_map": r.get("owner_map") or {}},
                                engine_error=engine_err)
                            if diag.get("ok"):
                                card = dict(diag["card"])
                                # strip in-process handles (not JSON-serializable intent)
                                card.pop("_corrupted_document", None)
                                card.pop("_fixed_document", None)
                                fail_result["regen_diagnosis"] = card
                    except Exception:  # noqa: BLE001 — diagnosis must never mask the failure
                        pass
                    return fail_result

                out = {
                    "ok": ok,
                    "doc_id": d,
                    "revision": r.get("revision"),
                    "body_count": len(bodies),
                    "volume_mm3": r.get("volume"),
                    "features": feats,
                }
                # GSL Stage 1a — SEMANTIC write-confirmation (W2): say what the op actually did
                # ("added 2 faces (+2 cylinder); 2 new bore/hole(s); volume 24000→23435") or warn
                # "no geometry change" so the model isn't left diffing a bare volume number.
                vol_after = r.get("volume")
                if ok:
                    try:
                        out["changes"] = _write_diff(before_hist, _face_hist(d), before_vol,
                                                     vol_after)
                    except Exception:  # noqa: BLE001
                        out["changes"] = {
                            "summary": "geometry changed but write-diff unavailable",
                            "faces_delta": None,
                        }
                    if isinstance(vol_after, (int, float)):
                        _LAST_VOLUME[d] = vol_after
                    # B3 — best-effort AUTO-WARNING: if this batch added feature(s), check the
                    # new batch against the rest of the tree for boss/cut overlap or a swallowed
                    # boss (features filter keeps this bounded to new-batch x existing pairs, not
                    # a full-tree scan). Never fails the batch — degrades to no warning.
                    added_ids = [fid for i, op in enumerate(ops)
                                 if isinstance(op, dict) and op.get("op") == "add_feature"
                                 for fid in [(r.get("id_map") or {}).get(f"${i}")] if fid]
                    if added_ids:
                        try:
                            ov = cb.call("eval_feature_overlap",
                                         {"doc_id": d, "features": added_ids})
                            if ov.get("pairs") or ov.get("dead_features"):
                                out["overlap_warnings"] = {
                                    "summary": ov.get("summary"),
                                    "pairs": ov.get("pairs", []),
                                    "dead_features": ov.get("dead_features", []),
                                }
                        except Exception:  # noqa: BLE001
                            pass
                # Push the updated part to the desktop app's viewport (if it's open) so the user
                # watches it build. Best-effort; only when something actually got built.
                if ok and bodies:
                    pub = _publish_current(d)
                    if pub:
                        out["shown_in_app"] = True
                    _autosave(d)  # persist so a server restart can recover this doc
                # T-W12: attach preflight advisory items and log override event.
                if _pf_result:
                    adv_items = _pf_result.get("advisory_items") or []
                    if adv_items:
                        out["preflight_advisory"] = adv_items
                    pf_verdict = _pf_result.get("verdict", "skipped")
                    if pf_verdict == "fail":
                        # Applied despite failed preflight — log audit trigger (§11).
                        try:
                            from services.mcp.preflight_tools import log_preflight_override
                            log_preflight_override(d, "add_features", _pf_result)
                        except Exception:  # noqa: BLE001
                            pass
                # Auto-readback for 2D sketch mutations (compact ASCII + NL summary).
                # Best-effort: never fails the batch if the live sketch load is unavailable.
                if ok:
                    try:
                        from mcp_server.ascii2d import (  # noqa: PLC0415
                            sketch_ids_from_ops, live_sketch_readback,
                        )
                        sids = sketch_ids_from_ops(ops)
                        # Also pick up newly-created sketch features referenced as $N
                        # once id_map has resolved them for sketch-bearing add_feature.
                        id_map = r.get("id_map") or {}
                        for i, op in enumerate(ops or []):
                            if not isinstance(op, dict):
                                continue
                            if op.get("op") != "add_feature":
                                continue
                            feat = op.get("feature") or {}
                            if (feat.get("type") or "").lower() != "sketch":
                                continue
                            fid = id_map.get(f"${i}")
                            if isinstance(fid, str) and fid not in sids:
                                sids.append(fid)
                        if sids:
                            readbacks = []
                            for sid in sids[:4]:
                                rb = live_sketch_readback(d, sid)
                                if rb:
                                    readbacks.append(rb)
                            if readbacks:
                                out["sketch_readbacks"] = readbacks
                                # Flatten first for models that expect top-level ascii/summary.
                                primary = readbacks[0]
                                out["ascii"] = primary.get("ascii")
                                out["ascii_block"] = primary.get("ascii_block")
                                out["sketch_summary"] = primary.get("summary")
                                out["ascii_readback"] = primary
                                out["readback_hint"] = primary.get("hint")
                                if primary.get("findings") is not None:
                                    out.setdefault("findings", primary.get("findings"))
                    except Exception:  # noqa: BLE001
                        pass
                return out

            if background:
                jid = _start_job("add_features", _apply)
                return _dumps({"ok": True, "doc_id": d, "job_id": jid, "status": "running",
                               "note": f"applying {len(ops)} op(s) in the background — "
                                       f"poll get_job('{jid}')."})
            # Run on a worker thread with auto-handoff: a fast batch returns inline exactly as
            # before; a heavy one that overruns the budget keeps running and returns a job_id so
            # the transport call returns in time and the session survives.
            raw = _solve_sync_or_handoff("add_features", _apply)
            # mega-8 F7: enrich thin unknown-op errors with closest + cad_guide
            # (C++ path sometimes returns only "unknown op 'cylinder'").
            try:
                from services.mcp import op_vocab as _ov  # noqa: PLC0415
                enriched = _ov.enrich_unknown_op_error(raw)
                if isinstance(enriched, dict):
                    return _dumps(enriched)
                if isinstance(enriched, str):
                    return enriched
            except Exception:  # noqa: BLE001
                pass
            return _dumps(raw) if not isinstance(raw, str) else raw
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def get_part(doc_id: Optional[str] = None, summary: bool = False) -> str:
        """Return the feature tree (features, parameters, ids) + revision for editing.

        summary=True returns a LIGHTWEIGHT tree — feature id/name/type/suppressed + parameters,
        with heavy embedded geometry (the import features' base64 BREP blobs) and any oversized
        string value ELIDED. Use it on big assemblies/imported parts where the full tree blows
        the tool token limit (a 600k-char part lists in a few KB). The full (default) form
        carries the complete params verbatim for round-trip editing.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_tree", {"doc_id": d})
            if not summary:
                return _dumps(r)

            def _trim(value: Any) -> Any:
                # Elide base64 geometry blobs / very long strings that bloat the tree but carry
                # no design intent the caller would read in a summary.
                if isinstance(value, str) and len(value) > 200:
                    return f"<{len(value)} chars elided>"
                if isinstance(value, dict):
                    return {k: ("<elided>" if k in ("embedded_brep",) else _trim(v))
                            for k, v in value.items()}
                if isinstance(value, list):
                    return [_trim(v) for v in value] if len(value) <= 64 else \
                        f"<list of {len(value)} elided>"
                return value

            tree = r.get("tree") or {}
            feats = tree.get("features") or []
            out_feats = []
            for f in feats:
                if not isinstance(f, dict):
                    continue
                params = {k: ("<elided>" if k == "embedded_brep" else _trim(v))
                          for k, v in (f.get("params") or {}).items()}
                out_feats.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "suppressed": bool(f.get("suppressed", False)),
                    "params": params,
                })
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "summary": True, "feature_count": len(out_feats),
                           "features": out_feats})
        except Exception as e:  # noqa: BLE001
            return _err(e)






    @mcp.tool()
    def set_material(material: Union[str, dict, list],
                     body: Optional[Union[str, int]] = None,
                     doc_id: Optional[str] = None) -> str:
        """Set a body's MATERIAL APPEARANCE — colour + finish — so it renders like the real material.

        material: a preset name (aluminum, steel, stainless, brass, copper, gold, plastic, abs,
        black) OR an explicit dict {"color": [r,g,b] 0..1, "metallic"?: 0..1, "roughness"?: 0..1},
        OR a bare [r,g,b] list for a plain colour.
        body: which body — "b1", "b2", ... (1-based) or an int ordinal. Default: ALL bodies.

        Persists on the document (round-trips through the native .step+) and is picked up by
        render_part. NOTE: this is the VISUAL material; to set the material for SIMULATION
        (density/stiffness) pass `material=` to run_fea_static / run_cfd_flow instead.
        """
        try:
            d = _doc(doc_id)
            ap = _resolve_appearance(material)
            # Resolve target body keys. None -> all bodies (an empty op batch returns the tess
            # body list without mutating the doc); int -> "b<n>"; str -> the key as given.
            if body is None:
                info = cb.call("doc_apply_ops", {"doc_id": d, "ops": []})
                n = len(info.get("tess", {}).get("bodies", [])) or 1
                targets = [f"b{i + 1}" for i in range(n)]
            elif isinstance(body, int):
                targets = [f"b{body}"]
            else:
                targets = [str(body)]
            ops = [{"op": "set_appearance", "id": k, **ap} for k in targets]
            r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
            return _dumps({"ok": True, "doc_id": d, "bodies": targets,
                           "appearance": ap, "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def undo(doc_id: Optional[str] = None) -> str:
        """Undo the last edit on the current part.

        Multi-user note: from mcp/agent connections the daemon rejects this with
        code ``undo_denied`` (agents use doc_checkpoint/doc_revert or PLM rollback).
        Set env ``AICAD_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after undo, call render_part()/render_views() (and inspect_feature on the
        affected region) to confirm the rolled-back state, then verify_battery before done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_undo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def redo(doc_id: Optional[str] = None) -> str:
        """Redo the last undone edit on the current part.

        Multi-user note: from mcp/agent connections the daemon rejects this with
        code ``undo_denied`` (agents use doc_checkpoint/doc_revert or PLM rollback).
        Set env ``AICAD_UNDO_AGENTS=1`` only as a deliberate kill-switch.

        VERIFY LOOP: after redo, call render_part()/render_views() to SEE the restored state
        and verify_battery before declaring done.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_redo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def show_in_app(doc_id: Optional[str] = None) -> str:
        """Push the current part to the OPEN AI-CAD desktop app's 3D viewport so the user can see it.

        Modeling tools already do this automatically after each change; call this to force a refresh
        (e.g. after switching the current part). No-op effect if the desktop app isn't running — the
        part is written to the handoff folder and shown whenever the app next opens.
        """
        try:
            d = _doc(doc_id)
            pub = _publish_current(d)
            if not pub:
                return _dumps({"ok": False, "error": "could not publish (no geometry yet?)"})
            return _dumps({"ok": True, "doc_id": d, **pub})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def list_parts() -> str:
        """List all open part/drawing documents in this engine session."""
        try:
            return _dumps(cb.call("doc_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def close_part(doc_id: Optional[str] = None, discard: bool = False) -> str:
        """Close a part document and free it. Clears the current part if it was current.

        SAVE DISCIPLINE: docs you (the AI) create are AI-owned. The desktop app never warns
        the user about YOUR unsaved docs — so YOU are responsible for saving deliverables
        before closing. If the doc has unsaved changes this returns {"ok": false,
        "code": "unsaved_changes"} WITHOUT closing; call save_part(path=...) to deliver it,
        or close_part(discard=True) to intentionally drop scratch work.

        If another client (e.g. the user, who adopted this doc by editing it) is still
        editing, the daemon degrades the close to a detach and returns {"detached": true} —
        your live view drops but their doc lives on.
        """
        try:
            d = _doc(doc_id)
            if not discard:
                # "Undelivered work" = mutated since the last save_part/export_part. Revision is
                # the reliable signal (the engine `dirty` flag is cleared by the MCP autosave's
                # doc_save to a temp path, so it would false-negative here).
                name = d
                for e in cb.call("doc_list", {}).get("docs", []):
                    if isinstance(e, dict) and e.get("doc_id") == d:
                        name = e.get("name") or d
                        break
                rev = _doc_revision(d)
                delivered = _LAST_DELIVER_REV.get(d)
                if rev is not None and rev > 0 and delivered != rev:
                    return _dumps({
                        "ok": False, "code": "unsaved_changes", "doc_id": d, "dirty": True,
                        "message": f"{name} has unsaved changes — call save_part(path=...) "
                                   f"to deliver it, or close_part(discard=True) to drop it",
                    })
            r = cb.call("doc_close", {"doc_id": d})
            if r.get("detached"):
                # Another client is editing it: our view detached, the doc lives on.
                _unpublish(d)
                if _CUR["doc_id"] == d:
                    _CUR["doc_id"] = None
                _LAST_DELIVER_REV.pop(d, None)
                return _dumps({"ok": True, "detached": True, "doc_id": d,
                               "note": r.get("note")})
            _unpublish(d)  # drop its mirror tab in the app
            if _CUR["doc_id"] == d:
                _CUR["doc_id"] = None
            # Prune recovery aliases that resolve to this now-closed doc. The bridge
            # outlives daemon restarts (which reset the doc-id counter), so a stale
            # old_id -> d alias could later misroute an agent's call to a REUSED d.
            for k in [k for k in _DOC_ALIASES if k == d or _resolve_alias(k) == d]:
                _DOC_ALIASES.pop(k, None)
            _LAST_DELIVER_REV.pop(d, None)
            return _dumps({"ok": True, "closed": r.get("closed", True), "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def save_part(path: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Save the part as AI-CAD's NATIVE .step+ — the editable, reopenable format. PREFER THIS.

        .step+ is the only format that round-trips the full PARAMETRIC feature tree (so the user
        can keep editing parameters/features), its SIMULATION STUDIES (FEA/CFD setups + results),
        and any associated 2D drawings — all in one file the desktop app opens fully editable. Use
        this to deliver a part the user will keep working on. (export_part writes flat interchange
        or mesh files: a plain STEP/STL has geometry only, no feature history or studies.)

        path : where to write. If omitted, a temp .step+ is written. A ".step+" extension is added
               if missing. Returns the written path and bytes so you can confirm a real save.

        Save every deliverable BEFORE your session ends: the user is never warned about your
        unsaved docs, so anything you don't save (and don't close_part(discard=True)) is only
        recoverable from a fallback crash-recovery tier — not a delivery mechanism.
        """
        try:
            d = _doc(doc_id)
            # K3: canonicalise the caller-supplied path so any '..' traversal sequences are
            # fully resolved before we pass them to the engine.  This means the caller sees
            # exactly where the file will be written (no hidden navigation).
            out = _sanitize_output_path(path) if path else _tmp(".step+")
            if not out.lower().endswith(".step+"):
                out += ".step+"
            r = cb.call("doc_save", {"doc_id": d, "path": out})
            written = r.get("path", out)
            ok = os.path.exists(written)
            if ok:
                # Delivered: record the revision so close_part won't warn until further edits.
                rev = _doc_revision(d)
                if rev is not None:
                    _LAST_DELIVER_REV[d] = rev
            return _dumps({"ok": ok, "format": "step+", "native": True, "doc_id": d,
                           "path": written,
                           "bytes": (os.path.getsize(written) if ok else 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def get_job(job_id: str) -> str:
        """Poll a background job started by run_cfd_flow/run_fea_*(background=True).

        Returns {status: "running"|"done"|"error"}; when done, the solve `result` is included.
        Polling a long solve this way never trips the per-call transport timeout that would
        otherwise drop the whole session.
        """
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return _dumps({"ok": False, "error": f"unknown job_id {job_id!r}"})
            snap = dict(job)
        out = {"ok": True, "job_id": job_id, "status": snap["status"], "kind": snap["kind"]}
        if snap["status"] == "running" and snap.get("progress"):
            progress = dict(snap["progress"])  # live {pct, stage, text} for long OpenFOAM runs
            # LBM reports progress via a file the engine writes each checkpoint — fold it in so the
            # built-in CFD path gets an ETA-style readout like OpenFOAM (report footnote).
            pf = progress.pop("progress_file", None)
            if pf:
                try:
                    with open(pf, encoding="utf-8") as fh:
                        progress.update(json.load(fh))
                except (OSError, ValueError):
                    pass  # not written yet / mid-write — keep the seed
            out["progress"] = progress
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] == "error":
            out["ok"] = False
            out["error"] = snap["error"]
        return _dumps(out)



    @mcp.tool()
    def recover_parts(all: bool = False) -> str:
        """Reopen auto-saved parts after a server restart (recover a dropped session).

        Every successful add_features auto-persists the part to a recoverable .step+; if the
        server was restarted (e.g. a client timeout killed it) the in-memory docs are gone — call
        this to reload them (each gets a fresh doc_id). list_parts() then shows them.

        By default this recovers ONLY the most-recent prior session's parts (the one that was
        dropped) — not the entire autosave history, so you don't drag in unrelated old docs.
        Pass all=True to reach every autosaved part. In daemon mode this also reopens parts the
        daemon retired (unclosed docs from ended sessions — they were saved and closed instead of
        lingering forever; nothing reopens them except this call or the GUI's recovery prompt).
        Each entry returns its fresh `doc_id` plus the saved `name` and `original_doc_id` so you
        can re-map old handles. Stale autosaves (older than AI_CAD_AUTOSAVE_MAX_AGE_DAYS,
        default 7) are pruned automatically.
        """
        try:
            recovered = recover_autosaved(scope="all" if all else "recent",
                                          include_orphaned=True)
            return _dumps({"ok": True, "recovered": recovered, "count": len(recovered),
                           "scope": "all" if all else "recent"})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    # ---------------------------------------------------------------- configurations

    @mcp.tool()
    def list_configurations(doc_id: Optional[str] = None) -> str:
        """List the part's configurations (parametric variants) and which one is active.

        A configuration is a named variant of the SAME feature tree with its own parameter
        overrides and feature suppressions — e.g. "Long" / "Short" of one bracket, or a family of
        sizes. Returns {configurations:[names], active, detail:[{name, param_overrides,
        suppressions}]}. A fresh part has just the implicit "Default". Build variants with
        add_configuration + set_config_override / set_config_suppression, switch with
        activate_configuration; measure_part/render_part then reflect the active variant.
        """
        try:
            return _dumps(cb.call("config_list", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def add_configuration(name: str, copy_from: Optional[str] = None,
                          doc_id: Optional[str] = None) -> str:
        """Add a new configuration (a named parametric variant) to the current part.

        name      : the new configuration's name (must be unique).
        copy_from : an existing configuration name to seed its overrides/suppressions from
                    (omit to start empty = identical to the base part). Then tailor it with
                    set_config_override / set_config_suppression, and activate_configuration to
                    make it current. The new config is created but NOT activated.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_add", {"doc_id": d, "name": name, "copy_from": copy_from})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "added": name,
                           "active": (r.get("tree") or {}).get("active_configuration"),
                           "configurations": [c.get("name") for c in
                                              (r.get("tree") or {}).get("configurations", [])]})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def activate_configuration(name: str, doc_id: Optional[str] = None) -> str:
        """Switch the active configuration. The part rebuilds to that variant's parameters.

        After this, measure_part / render_part / export_part reflect the chosen variant. Returns
        the new active configuration name and the rebuilt body count/volume so you can confirm the
        switch took (e.g. a suppressed feature drops a body, an override changes the volume).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_activate", {"doc_id": d, "name": name})
            bodies = (r.get("tess") or {}).get("bodies", [])
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "active": name,
                           "body_count": len(bodies),
                           "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_override(config: str, feature_id: str, param: str,
                            value: Optional[Union[float, list]] = None, clear: bool = False,
                            doc_id: Optional[str] = None) -> str:
        """Override one feature parameter in a configuration (the parametric-variant edit).

        config     : the configuration name to edit (see list_configurations).
        feature_id : the feature's id from get_part (e.g. "f1").
        param      : the parameter name on that feature (e.g. "length", "radius", "translate").
        value      : the override value for this configuration only — a plain NUMBER for a scalar
                     param (length/radius/angle…) OR a LIST for a vector param, e.g.
                     translate:[x,y,z] on a `transform` feature (so one assembly file can hold a
                     Default and a STOWED config that differ only in a placement vector).
        clear      : pass True (and omit value) to REMOVE the override so the config falls back to
                     the base value.
        The base/Default config and other configurations are untouched. Activate the configuration
        (or it may already be active) and measure_part to see the effect.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "name": config, "feature_id": feature_id,
                            "param": param}
            if clear or value is None:
                params["clear"] = True
            elif isinstance(value, (list, tuple)):
                # Vector/array param (e.g. transform.translate): the engine reads it as a raw
                # array (opt_vec3), so the override must be the bare list — NOT wrapped in a
                # {value, expr} scalar object. Coerce numeric components to float.
                params["value"] = [float(x) if isinstance(x, (int, float)) else x for x in value]
            else:
                # Scalar params are {value, expr} objects in the engine; wrap a bare number.
                params["value"] = {"value": float(value), "expr": None}
            cb.call("config_set_override", params)
            _publish_current(d)
            _autosave(d)
            echoed = None
            if not (clear or value is None):
                echoed = (params["value"] if isinstance(value, (list, tuple)) else float(value))
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "param": param,
                           "value": echoed,
                           "cleared": bool(clear or value is None)})
        except Exception as e:  # noqa: BLE001
            return _err(e)



    @mcp.tool()
    def set_config_suppression(config: str, feature_id: str, suppressed: bool = True,
                               doc_id: Optional[str] = None) -> str:
        """Suppress (or un-suppress) a feature in a configuration — turn a feature ON/OFF per variant.

        config     : the configuration name (see list_configurations).
        feature_id : the feature id from get_part (e.g. "f3" — a fillet, a hole, a boss).
        suppressed : True to remove that feature in this configuration; False to bring it back.
        Lets one variant include a feature (a mounting boss, a chamfer) that another omits, without
        forking the feature tree. Activate the configuration + render_part to confirm.
        """
        try:
            d = _doc(doc_id)
            cb.call("config_set_suppression", {"doc_id": d, "name": config,
                                               "feature_id": feature_id,
                                               "suppressed": bool(suppressed)})
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "suppressed": bool(suppressed)})
        except Exception as e:  # noqa: BLE001
            return _err(e)
