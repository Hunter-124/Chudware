"""Read-only harness electrical and signal-integrity tools.

Harness references are resolved through the document study store; these tools do not
persist analyses. Results retain the analysis basis, assumptions and unknowns.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from services.harness import cable_models, electrical, model, wire_data

_MAX_PAGE = 50
_MAX_BYTES = 65536


def _response(fn, offset: int = 0, limit: int = 25) -> str:
    """Return a bounded page of every top-level result list, with total counts."""
    try:
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or
                isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE):
            raise model.HarnessError("bad_pagination", "offset must be nonnegative and limit must be 1..50")
        result = fn()
        if not isinstance(result, dict):
            result = {"links": result}
        result = dict(result)
        result.setdefault("ok", True)
        pages = {}
        for key, value in list(result.items()):
            if isinstance(value, list):
                result[key] = value[offset:offset + limit]
                pages[key] = {"offset": offset, "returned": len(result[key]), "total": len(value),
                              "next_offset": offset + limit if offset + limit < len(value) else None}
        if pages:
            result["pagination"] = pages
        text = json.dumps(result, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise model.HarnessError("output_too_large", "Result exceeds the response size limit",
                                     hint="Reduce limit or request fewer wires or links.")
        return text
    except model.HarnessError as exc:
        error = exc.to_dict()
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        error = {"ok": False, "code": "bad_argument", "error": str(exc),
                 "hint": "Check the supplied harness and electrical inputs."}
    except Exception:
        error = {"ok": False, "code": "analysis_error", "error": "Electrical analysis could not be completed",
                 "hint": "Check the document and analysis inputs, then retry."}
    # Even errors can contain user-controlled study names or input values.
    text = json.dumps(error, default=str, allow_nan=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > _MAX_BYTES:
        return json.dumps({"ok": False, "code": "error_too_large", "error": "Error details exceeded the response size limit"})
    return text


def register_harness_electrical_tools(mcp) -> None:
    @mcp.tool(risk="read")
    def harness_electrical_check(doc_id: str, harness_id: Optional[str] = None,
                                 loads: Optional[list[dict]] = None, ambient_c: Optional[float] = None,
                                 rails: Optional[dict] = None, power_budget: Optional[dict] = None,
                                 fuses: Optional[list[dict]] = None, wire_specs: Optional[dict] = None,
                                 max_drop_pct: Optional[float] = None, emissivity: Optional[float] = None,
                                 count_unloaded_wires: bool = False, warn_margin_c: Optional[float] = None,
                                 warn_utilisation: Optional[float] = None, find_fixes: bool = True,
                                 offset: int = 0, limit: int = 25) -> str:
        """Check saved harness current, voltage drop, thermal/ampacity and protection.

        Supply loads per wire or net and available ratings, return paths and fuse data.
        wire_specs maps wire IDs (or '*' for defaults) to physical properties.
        Set insulation to PVC (polyvinyl chloride), PTFE (TFE or
        polytetrafluoroethylene), FEP, ETFE (Tefzel), XLPE (cross-linked or irradiated
        polyethylene), silicone (silicone rubber), or TPE (thermoplastic elastomer).
        Generic \"Teflon\" is ambiguous. Material values are cited screening examples,
        not the actual wire's properties or rating; prefer its datasheet. Optional
        insulation_thermal_conductivity_w_mk, emissivity and temp_rating_c override
        material values on the wire or in wire_specs. Supply insulation_od_mm and
        conductor size; the global emissivity overrides the surface and is required for
        mixed bundle surfaces. FEP, ETFE and TPE emissivity uses a generic cable-surface
        estimate, not a material-specific measurement. TPE conductivity uses a TPU
        example as a low-confidence screening assumption; actual values depend on
        formulation. Missing conductivity or emissivity leaves thermal analysis
        unevaluated; missing temperature rating prevents a rating verdict.
        The estimate uses equivalent solid copper diameter, cylindrical insulation
        conduction and a shared bundle surface with still-air horizontal-cylinder
        convection and radiation. Bundle void/contact conduction and external heating
        are omitted; for ribbon cable the circular envelope is not its actual surface
        area. This is an estimate, not a certified rating. A wire_specs ampacity
        object can provide current_a, ambient_c, max_conductors and source. The
        optional nasa_pressure_psia selects NASA MSFC-STD-3012A Appendix A screening
        only for its supported gauges, insulation ratings, ambient and bundle scope;
        it is a spaceflight requirement, not general chassis certification. Missing
        evidence remains incomplete; assumptions, limitations and citations accompany
        results. Top-level result lists are independently paged.
        """
        return _response(lambda: electrical.analyze(
            model.load_harness(doc_id, harness_id), loads=loads, ambient_c=ambient_c,
            rails=rails, power_budget=power_budget, fuses=fuses, wire_specs=wire_specs,
            max_drop_pct=max_drop_pct, emissivity=emissivity,
            count_unloaded_wires=count_unloaded_wires, warn_margin_c=warn_margin_c,
            warn_utilisation=warn_utilisation, find_fixes=find_fixes), offset, limit)

    @mcp.tool(risk="read")
    def harness_bus_check(bus: str, params: dict, doc_id: Optional[str] = None,
                          harness_id: Optional[str] = None, nets: Optional[list[str]] = None,
                          wires: Optional[list[str]] = None, cable: Optional[dict] = None,
                          links: Optional[list[dict]] = None,
                          offset: int = 0, limit: int = 25) -> str:
        """Check i2c, can, can_fd, rs485, uart or spi bus signal integrity.

        For a saved harness, supply doc_id and select wires by ID or nets by name.
        Selected wires must be series sections of one signal path, not parallel bus
        conductors. Alternatively supply explicit links with length_m and cable_model.
        I2C inputs include pullup_ohm, supply_v, sink_current_ma, vol_v,
        max_rise_time_ns, device_capacitance_pf, and mode (standard or fast) or
        max_capacitance_pf. CAN/RS485 require termination_ohms,
        termination_tolerance_fraction, stub_lengths_m, max_stub_delay_ns and, for
        nonzero stubs, stub_delay_ns_per_m. Propagation screening also requires
        bitrate_hz, sample_point and loop_delay_ns. RS485 bias additionally requires
        bias_pullup_ohm, bias_pulldown_ohm, bias_load_ohm, supply_v and min_bias_v.
        UART/SPI use max_delay_ns. Missing evidence is unknown, not a pass.
        """
        from services.harness.bus_electrical import analyze_bus, harness_to_analysis_links as convert

        def run() -> dict:
            if doc_id:
                if links is not None:
                    raise model.HarnessError("bad_argument", "Use either saved harness wires or explicit links, not both")
                if bool(nets) == bool(wires):
                    raise model.HarnessError("bad_argument", "Select either nonempty nets or nonempty wires")
                harness = model.load_harness(doc_id, harness_id)
                selected = wires
                if nets:
                    if len(set(nets)) != len(nets):
                        raise model.HarnessError("bad_argument", "nets must not contain duplicates")
                    present = {w.get("net") for w in harness["wires"]}
                    missing = [net for net in nets if net not in present]
                    if missing:
                        raise model.HarnessError("unknown_net", "One or more nets are absent from the harness",
                                                 nets=missing)
                    selected = [w["id"] for w in harness["wires"] if w.get("net") in nets]
                chosen_links = convert(harness, selected, cable)
                result = analyze_bus(bus, params, chosen_links)
                result["links"] = chosen_links
                result["harness_id"] = harness.get("id")
                return result
            if harness_id is not None or nets is not None or wires is not None or cable is not None:
                raise model.HarnessError("bad_argument", "Harness selectors and cable require doc_id")
            return analyze_bus(bus, params, links)
        return _response(run, offset, limit)

    @mcp.tool(risk="read")
    def harness_cable_model(spec: dict, doc_id: Optional[str] = None,
                            harness_id: Optional[str] = None, wire_id: Optional[str] = None,
                            f_hz: Optional[float] = None, offset: int = 0, limit: int = 25) -> str:
        """Estimate per-length cable R/L/C, impedance and delay with cited basis.

        spec accepts kind, conductor_diameter_mm, spacing_mm, insulation or er/er_eff,
        twist_lay_mm, dielectric_od_mm, shield_r_ohm_per_m and datasheet values.
        Optionally supply doc_id and wire_id together to resolve its conductor and geometry.
        For ribbon cable, provide its actual electrical construction or datasheet;
        ribbon kind alone does not establish spacing or dielectric properties.
        Dielectric bounds and missing evidence are retained rather than treated as exact.
        """
        def run() -> dict:
            wire: dict[str, Any] = {}
            conductor = None
            if bool(doc_id) != bool(wire_id):
                raise model.HarnessError("bad_argument", "doc_id and wire_id must be supplied together")
            if harness_id is not None and not doc_id:
                raise model.HarnessError("bad_argument", "harness_id requires doc_id and wire_id")
            if doc_id:
                wire = model.get_wire(model.load_harness(doc_id, harness_id), wire_id)
                conductor = wire_data.resolve_conductor(
                    gauge_awg=wire.get("gauge_awg"), csa_mm2=wire.get("csa_mm2"),
                    conductor_class=wire.get("conductor_class"),
                    r20_ohm_per_km=wire.get("r20_ohm_per_km"), stranded=wire.get("stranded"))
            return cable_models.evaluate_cable(spec, conductor=conductor, wire=wire, f_hz=f_hz)
        return _response(run, offset, limit)

    @mcp.tool(risk="read")
    def harness_to_bus_links(doc_id: str, templates: list[dict], harness_id: Optional[str] = None,
                             offset: int = 0, limit: int = 25) -> str:
        """Convert saved harness routes to firmware simulation bus_links.

        Supply complete firmware link templates with explicit endpoints and topology.
        Lossless-tline signal segments supply from, to, wire_id and cable; lumped-RC
        signals supply harness_wires entries with wire_id and cable. Routed lengths must
        be positive and the necessary per-metre electrical values exact, not bounds.
        CAN lumped-RC sections require cable.datasheet.loop_resistance_ohm_per_km.
        """
        from services.harness.bus_electrical import harness_to_bus_links as convert
        return _response(lambda: {"bus_links": convert(model.load_harness(doc_id, harness_id), templates)},
                         offset, limit)
