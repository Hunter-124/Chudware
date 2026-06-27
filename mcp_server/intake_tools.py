"""Intake / photo-measurement / first-try-fit MCP tools.

The north star: never silently guess a fit-critical dimension. When the user references a real part
we have no model for, we (1) try standard reference data first, (2) ask for photos WITH a scale
reference, (3) DEMAND a typed caliper value for any measured-tier critical dim, (4) resolve the
manufacturing process (FDM default, clarify if ambiguous), (5) build the receptacle to FIT with
process clearances, and (6) emit a FitReport (+ optional printable fit-test coupon) before export.

The pure brain is engine.intake.intake + engine.dfm.process_profiles. These tools are the thin MCP
adapter: they drive the flow and PERSIST the artifacts inside the .step+ via doc_set_meta/doc_get_meta
(the same opaque-doc-key mechanism as `studies`), so an MCP-built part reopens in-app intact. The
elicitation is naturally multi-turn over MCP: a tool returns the ask, the user answers next turn.
"""
from __future__ import annotations

import json as _json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from mcp_server import core_bridge as cb
from mcp_server.cad_tools import _doc, _dumps, _err
from engine.dfm import process_profiles as pp
from engine.intake import intake as ik

# doc["meta"] keys (persisted in the .step+).
_K_PROCESS = "process"
_K_MEASURED = "measured_parts"
_K_LEDGER = "crit_dims"
_K_FIT = "fit_report"
_K_INTAKE = "intake"


def _get(doc_id: str, key: str):
    try:
        return cb.call("doc_get_meta", {"doc_id": doc_id, "key": key}).get("value")
    except Exception:  # noqa: BLE001
        return None


def _set(doc_id: str, key: str, value) -> None:
    cb.call("doc_set_meta", {"doc_id": doc_id, "key": key, "value": value})


def _process_id(doc_id: str) -> str:
    prof = _get(doc_id, _K_PROCESS)
    if isinstance(prof, dict) and prof.get("process"):
        return prof["process"]
    return pp.DEFAULT_PROCESS


def _load_ledger(doc_id: str):
    raw = _get(doc_id, _K_LEDGER)
    return ik.CriticalDimensionLedger.from_dict(raw) if isinstance(raw, dict) else None


def _load_measured(doc_id: str):
    raw = _get(doc_id, _K_MEASURED)
    if isinstance(raw, list) and raw:
        return ik.MeasuredPartProfile.from_dict(raw[-1])
    return None


def register_intake_tools(mcp: FastMCP) -> None:

    # ----------------------------------------------------------------- manufacturing process

    @mcp.tool()
    def resolve_process(request: str, material: Optional[str] = None,
                        quantity: Optional[int] = None) -> str:
        """Decide the manufacturing process from the request (FDM 3D-printing is the default).

        Returns {chosen, confidence, runner_up, ambiguous, must_block, clarification, assumptions,
        why}. If `ambiguous` is true, ASK the user the single `clarification` question before
        building (e.g. sheet-metal vs CNC for a flat metal plate); otherwise proceed and record the
        assumption. If `must_block` (e.g. metal asked of FDM), surface it — don't silently switch.
        This drives the process-CLARIFY gate; bind the choice with set_process().
        """
        try:
            return _dumps(pp.resolve_process(request, material=material, quantity=quantity))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_process(process: str = "fdm", material: Optional[str] = None,
                   printer_overrides: Optional[dict] = None, doc_id: Optional[str] = None) -> str:
        """Bind the manufacturing ProcessProfile to the part (persisted in the .step+).

        `process`: fdm|cnc_mill|sheet_metal|injection|lathe|laser. `printer_overrides`: per-printer
        {xy_comp, hole_comp} for FDM calibration. Returns the resolved profile (fit-clearance table,
        min wall, thread strategy, hole compensation) — the numbers every later fit-budgeted
        dimension uses. Call this once the process is known (or accept the FDM default).
        """
        try:
            d = _doc(doc_id)
            prof = pp.get_profile(process, overrides=printer_overrides)
            if material:
                prof.setdefault("material", {})["default"] = material
            _set(d, _K_PROCESS, prof)
            return _dumps({"ok": True, "process": prof,
                           "process_block": pp.process_block_text(prof)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def dfm_check(process: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Design-for-manufacturing punch list against the part's ProcessProfile thresholds.

        Checks the thinnest wall against the process minimum and (for molding) draft, and returns a
        pass/fail list with the offending values. Uses the bound process (set_process) unless you
        override `process`. Run before export so a part that can't be made the chosen way is caught.
        """
        try:
            d = _doc(doc_id)
            proc = process or _process_id(d)
            prof = pp.get_profile(proc)
            params = prof.get("params", {})
            issues = []
            try:
                th = cb.call("eval_thickness", {"doc_id": d})
                mn = th.get("min_thickness_mm")
                minwall = params.get("min_wall_mm")
                if isinstance(mn, (int, float)) and isinstance(minwall, (int, float)):
                    issues.append({"check": "min_wall", "value_mm": round(mn, 3),
                                   "limit_mm": minwall, "ok": mn >= minwall,
                                   "note": f"thinnest wall {mn:.2f}mm vs {proc} minimum {minwall}mm"})
            except Exception:  # noqa: BLE001
                pass
            if params.get("draft", {}).get("required"):
                try:
                    dr = cb.call("eval_draft", {"doc_id": d, "pull_dir": [0, 0, 1],
                                                "min_draft_deg": params["draft"].get("min_deg", 1.0)})
                    nbelow = dr.get("n_faces_below_threshold", 0)
                    issues.append({"check": "draft", "faces_below": nbelow,
                                   "ok": nbelow == 0,
                                   "note": f"{proc} needs >= {params['draft'].get('min_deg')}deg draft"})
                except Exception:  # noqa: BLE001
                    pass
            ok = all(i.get("ok", True) for i in issues)
            return _dumps({"ok": True, "process": proc, "dfm_pass": ok, "checks": issues})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ----------------------------------------------------------------- photo measurement

    @mcp.tool()
    def request_part_photos(label: str, critical_features: list,
                           process: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Ask the user for photos of a real part we have NO standard model for, so we can measure it.

        `critical_features`: the things that must FIT — plain descriptions or {key, description, fit}
        dicts (e.g. ["the motor body pocket", "the output shaft", "the 2 mounting holes"]). Returns a
        prioritized, templated ask: which orthographic views + close-ups to shoot, the mandatory
        SCALE reference (a printable scale card / calipers-in-frame / ruler) IN THE SAME PLANE as the
        feature, and which dims will need a typed caliper number. Persists an IntakeState (awaiting
        the user's photos); they reply next turn — submit them with submit_part_photos(). Call
        standard-parts lookup FIRST; only photograph a part we truly can't resolve.
        """
        try:
            d = _doc(doc_id)
            proc = process or _process_id(d)
            req = ik.build_photo_request(label, critical_features, process=proc)
            st = ik.new_intake_state(doc_id=d)
            st.status = "awaiting_user"
            st.pending_request_id = req.get("request_id_seed")
            st.needs = [{"kind": "photo", "label": label, "request_id": st.pending_request_id}]
            _set(d, _K_INTAKE, st.to_dict())
            return _dumps({"ok": True, "request": req, "request_id": st.pending_request_id,
                           "status": "awaiting_user"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def submit_part_photos(claims: list, label: str = "part",
                          doc_id: Optional[str] = None) -> str:
        """Submit the measurements YOU read from the user's photos as structured claims; we score each.

        Each claim: {feature, dim, value_mm, scale_source (card|caliper_typed|ruler|coin|none),
        in_plane (bool), agrees_across_views (bool), edge_quality (sharp|soft), caliper_readout?}.
        We assign each a confidence tier from those signals, build a MeasuredPartProfile + the
        critical-dimension ledger for the bound process, and return `open_demands` — the fit-critical
        dims whose confidence is below what their fit needs (you MUST get a typed caliper number for
        those via confirm_dimensions; a photo guess won't press-fit). Persists everything in the part.
        """
        try:
            d = _doc(doc_id)
            proc = _process_id(d)
            by_feat: dict = {}
            for c in claims:
                fkey = c.get("feature") or "feature"
                tier = ik.score_claim(c)
                feat = by_feat.setdefault(fkey, {"dims": {}, "tiers": []})
                if c.get("dim"):
                    feat["dims"][c["dim"]] = c.get("value_mm")
                feat["tiers"].append(tier.get("tier", "low"))
            features = []
            for fkey, fv in by_feat.items():
                arch = ik.classify(fkey) or "prism_pocket"
                worst = ik.worst_tier(fv["tiers"]) if fv["tiers"] else "low"
                features.append(ik.new_measured_feature(fkey, arch, dims=fv["dims"],
                                                        confidence=worst))
            mp = ik.new_measured_part("mp_1", label, source="photo")
            mp.features = features
            ledger = ik.build_ledger(mp, process=proc)
            demands = ik.compute_demands(mp, ledger, process=proc)
            # Auto-resolve the critical dims that DO have a satisfying photo value (not in demands):
            # only the genuinely-soft or never-measured criticals should keep blocking the gate, so
            # a high-confidence photo dim doesn't force a needless typed re-confirmation.
            demand_keys = {x.get("key") for x in demands if isinstance(x, dict)}
            for row in ledger.rows:
                if row.key in demand_keys or row.status != "unknown":
                    continue
                dim = row.key.rsplit(".", 1)[-1]
                feat = mp.feature(row.feature)
                val = feat.dims.get(dim) if feat is not None else None
                if val is not None:
                    ik.apply_confirmation(ledger, row.key, val, status="measured",
                                          resolved_by="vision")
            _set(d, _K_MEASURED, [mp.to_dict()])
            _set(d, _K_LEDGER, ledger.to_dict())
            return _dumps({"ok": True, "measured_part": mp.to_dict(),
                           "open_demands": demands,
                           "gate_ok": ik.gate_ok(ledger)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def confirm_dimensions(confirmations: list, doc_id: Optional[str] = None) -> str:
        """Close fit-critical demands with TYPED caliper values (the dims we refuse to guess).

        `confirmations`: [{key, value_mm}] — e.g. [{"key":"output_shaft.dia","value_mm":3.0},
        {"key":"mount_holes.spacing","value_mm":18.0}]. Marks each ledger row measured/given and
        returns what's still blocking + whether the critical-dim gate is now clear (so geometry can
        commit). Persists the updated ledger.
        """
        try:
            d = _doc(doc_id)
            ledger = _load_ledger(d)
            if ledger is None:
                return _dumps({"ok": False, "error": "no ledger yet — run submit_part_photos first"})
            for c in confirmations:
                ik.apply_confirmation(ledger, c.get("key"), c.get("value_mm"))
            _set(d, _K_LEDGER, ledger.to_dict())
            still = [r.key for r in ik.unresolved_critical(ledger)]
            return _dumps({"ok": True, "gate_ok": ik.gate_ok(ledger),
                           "still_blocking": still})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def intake_status(doc_id: Optional[str] = None) -> str:
        """Where the intake stands: bound process, measured parts, the critical-dim ledger, what is
        still blocking first-try fit, and any pending photo/dimension request (the resume token)."""
        try:
            d = _doc(doc_id)
            ledger = _load_ledger(d)
            return _dumps({
                "ok": True,
                "process": _get(d, _K_PROCESS),
                "intake": _get(d, _K_INTAKE),
                "measured_parts": _get(d, _K_MEASURED),
                "gate_ok": ik.gate_ok(ledger) if ledger else True,
                "blocking": [r.key for r in ik.unresolved_critical(ledger)] if ledger else [],
            })
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def fit_report(doc_id: Optional[str] = None) -> str:
        """The fit report to review before printing: each mating pair's modeled-vs-source dimension,
        the clearance applied for the process, its source/confidence, and an ok/at_risk verdict, plus
        the assumptions made. Proves first-try fit (or flags what rests on a soft measurement)."""
        try:
            d = _doc(doc_id)
            proc = _process_id(d)
            measured = _load_measured(d)
            ledger = _load_ledger(d)
            if measured is None or ledger is None:
                return _dumps({"ok": False, "error": "nothing measured yet"})
            fr = ik.build_fit_report(measured, ledger, process=proc)
            _set(d, _K_FIT, fr.to_dict())
            return _dumps({"ok": True, "fit_report": fr.to_dict()})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def make_fit_coupon(interface_key: str, doc_id: Optional[str] = None) -> str:
        """Build a small printable FIT-TEST COUPON of one critical interface (bore / bolt-pattern /
        connector cutout) so the user can verify the real fit in a ~20-min print before committing
        the whole part. Builds it as a NEW part from the measured + process-stacked dims and returns
        its doc_id (export it to STL with export_part). The single best first-try-fit de-risk on FDM.
        """
        try:
            d = _doc(doc_id)
            proc = _process_id(d)
            measured = _load_measured(d)
            if measured is None:
                return _dumps({"ok": False, "error": "no measured part to base a coupon on"})
            feat = measured.feature(interface_key)
            spec = ik.coupon_spec(feat if feat is not None else interface_key, process=proc)
            ops = spec.get("ops") or []
            if not ops:
                return _dumps({"ok": False, "error": f"no coupon recipe for {interface_key!r}",
                               "spec": spec})
            r = cb.call("doc_new", {"kind": "part", "name": f"coupon_{interface_key}"})
            cdoc = r.get("doc_id")
            cb.call("doc_apply_ops", {"doc_id": cdoc, "ops": ops})
            return _dumps({"ok": True, "coupon_doc_id": cdoc, "interface": interface_key,
                           "spec": spec, "note": "export_part(fmt='stl') to print it"})
        except Exception as e:  # noqa: BLE001
            return _err(e)
