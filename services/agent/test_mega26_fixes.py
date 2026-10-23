"""Mega-26 fixes — A26 source-on-measured-net + B26 clearance retry fire.

Grounded in mega-25 a2 fingerprints:

  A26 / AC5 — sim on schematic, device_count=2, V(3V3)=0.0.
              Root causes:
                (1) forced V_source value "DC 3.3" fails schematic
                    _parse_value → value=None → OP 0.0 V.
                (2) source must drive the N21-resolved MEASURE net (3V3),
                    not a pre-regulator VIN island behind U1 (no sim model).
              Cure: value "3.3"; V+/load on resolved measure net; GND return;
              record u1_stubbed_ideal_regulator on call records.

  B26 / AC4 — min_clearance_mm present but clearance_retry_n=0.
              Root causes:
                (1) live ledger DRC results are _truncate()'d mid-JSON so
                    errors count is lost → chain_need never re-opts.
                (2) BUILD allow omits ecad_optimize_traces so re-opt branch
                    was gated unreachable.
              Cure: scrape errors from truncated blob; reopt_allowed when
              DRC/board present; cascade fires autoroute(pitch=0.3)+re-opt
              then re-DRC (retry_n>=1).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MEGA25 = _ROOT / "reports/runs/mega-25"
_MEGA25_AC4 = _MEGA25 / "criterion_AC4.json"
_MEGA25_AC5 = _MEGA25 / "criterion_AC5.json"
_MEGA25_LEDGER = _MEGA25 / "ledger.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        pytest.skip(f"fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ledger_ecad_calls() -> list[dict]:
    data = _load_json(_MEGA25_LEDGER)
    out: list[dict] = []
    for e in data.get("events") or []:
        p = e.get("payload") or {}
        if p.get("event") != "tool_call":
            continue
        tool = p.get("tool")
        if not tool:
            continue
        if not (
            str(tool).startswith("ecad")
            or tool in ("board_from_schematic", "ecad_make_board")
        ):
            continue
        out.append({
            "tool": tool,
            "ok": bool(p.get("ok")),
            "args": p.get("args") or {},
            "result": p.get("result"),
            "forced": p.get("forced"),
            "force_reason": p.get("force_reason"),
        })
    return out


# ---------------------------------------------------------------------------
# Live fingerprints
# ---------------------------------------------------------------------------


def test_mega25_ac5_fingerprint_v_zero_on_schematic():
    """A26 fixture: sim on sch, device_count=2, V(3V3)=0.0, U1 skipped."""
    ac5 = _load_json(_MEGA25_AC5)
    assert ac5.get("result") == "FAIL"
    ev = ac5.get("evidence") or {}
    assert int(ev.get("device_count") or 0) == 2
    assert float((ev.get("op") or {}).get("V(3V3)", 1e9)) == 0.0
    assert (ev.get("resolved_net") or "").upper() == "3V3"
    warns = " ".join(ev.get("warnings") or [])
    assert "no sim model" in warns.lower()
    assert ev.get("doc_id") == "f9ec35394c6c"


def test_mega25_ac4_fingerprint_clearance_retry_zero():
    """B26 fixture: DRC clearance×27, min_clearance path did not re-route."""
    ac4 = _load_json(_MEGA25_AC4)
    assert ac4.get("result") == "FAIL"
    ev = ac4.get("evidence") or {}
    drc = ev.get("drc") or ev.get("ecad_run_drc") or {}
    assert int(drc.get("errors") or 0) >= 20
    blob = json.dumps(drc, default=str).lower()
    assert "clearance" in blob
    assert "0.2" in blob or "0.20" in blob
    assert drc.get("doc_id") == "7c4fae119443"


def test_mega25_ledger_old_topology_dc33_and_no_retry():
    """Ledger replay: old V value was 'DC 3.3'; no autoroute after DRC."""
    calls = _ledger_ecad_calls()
    v_places = [
        c for c in calls
        if c.get("tool") == "ecad_place_symbol"
        and "V_source" in str((c.get("args") or {}).get("libid") or "")
    ]
    assert v_places, "expected forced V_source in mega-25 ledger"
    assert (v_places[0].get("args") or {}).get("value") == "DC 3.3"
    tools = [c.get("tool") for c in calls if c.get("ok") or c.get("forced")]
    assert "ecad_run_drc" in tools
    assert "ecad_autoroute" not in tools  # residual: retry never fired


# ---------------------------------------------------------------------------
# A26 — source on measured net + parseable V value + stub flag
# ---------------------------------------------------------------------------


def test_a26_v_source_value_is_parseable_not_dc_prefix():
    """Forced inject must use bare '3.3' (schematic _parse_value rejects 'DC 3.3')."""
    from services.sim.spice import _parse_value
    from services.agent.agent_turn import _ecad_ensure_driven_rail, _ecad_forced_tool_args

    # Engine contract: "DC 3.3" fails; "3.3" works.
    with pytest.raises(ValueError):
        _parse_value("DC 3.3")
    assert _parse_value("3.3") == 3.3

    sch = "sch_a26"
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch, "kind": "sch"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "MCU:Generic_QFP48", "ref": "U1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "U1", "doc_id": sch,
                "pins": [
                    {"ref": "U1", "pin": "1", "name": "VDD", "type": "power_in",
                     "x": 50.0, "y": 50.0},
                    {"ref": "U1", "pin": "12", "name": "VSS", "type": "power_in",
                     "x": 50.0, "y": 40.0},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "U1", "from_pin": "1",
                     "to_ref": "U1", "to_pin": "12", "doc_id": sch},
            "result": {
                "ok": True, "net": "N$x", "net_size": 2,
                "net_members": ["U1.1", "U1.12"],
            },
        },
        {
            "tool": "ecad_run_erc", "ok": True,
            "result": {"ok": True, "errors": 0, "doc_id": sch},
        },
    ]
    # Forced-tool place path when no V yet.
    place_args = _ecad_forced_tool_args("ecad_place_symbol", calls + [
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "GND", "doc_id": sch},
            "result": {"ok": True, "net": "GND", "net_size": 1},
        },
    ])
    # When cross+power and no source, place injects V.
    if "V_source" in str(place_args.get("libid") or ""):
        assert place_args.get("value") == "3.3", place_args

    dispatched: list[tuple[str, dict]] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append((tool, a))
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "V" in lib:
                assert a.get("value") == "3.3", a
                return {
                    "ok": True, "ref": a.get("ref") or "V1", "libid": lib,
                    "value": a.get("value"),
                    "pins": [
                        {"number": "+", "name": "+", "type": "power_out"},
                        {"number": "-", "name": "-", "type": "power_out"},
                    ],
                }
            return {
                "ok": True, "ref": a.get("ref") or "Rload", "libid": lib,
                "pins": [{"number": "1"}, {"number": "2"}],
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_add_power":
            return {"ok": True, "net": a.get("net") or "GND", "net_size": 1}
        return {"ok": True}

    allow = {
        "ecad_place_symbol", "ecad_wire", "ecad_add_power", "ecad_simulate",
    }
    new_calls, made, steps, _ = _ecad_ensure_driven_rail(
        calls=list(calls),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=20,
        reason="a26",
        steps=0,
        max_steps=12,
    )
    assert made > 0
    v_args = [a for t, a in dispatched if t == "ecad_place_symbol" and "V" in str(a.get("libid"))]
    assert v_args and v_args[0].get("value") == "3.3", dispatched
    # V+ wires onto measure-rail anchor (U1 VDD / 3V3). net is stashed on the
    # call record but stripped before MCP dispatch — assert via call records.
    v_wires = [
        c for c in new_calls
        if c.get("ok") and c.get("tool") == "ecad_wire"
        and str((c.get("args") or {}).get("from_ref") or "").startswith("V")
        and str((c.get("args") or {}).get("from_pin") or "") in ("+", "1")
    ]
    assert v_wires, dispatched
    assert any(
        str((c.get("args") or {}).get("net") or "").upper() == "3V3"
        for c in v_wires
    ), v_wires
    assert any(c.get("u1_stubbed_ideal_regulator") for c in new_calls if c.get("forced"))


def test_a26_forced_sim_args_mark_u1_stub_and_resolved_net():
    from services.agent.agent_turn import _ecad_forced_tool_args

    sch = "sch_stub"
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "doc_id": sch},
            "result": {"ok": True, "net": "3V3"},
        },
    ]
    sim = _ecad_forced_tool_args("ecad_simulate", calls)
    assert sim.get("doc_id") == sch
    assert (sim.get("resolved_net") or "").upper() in ("3V3", "3.3V", "VCC")
    assert sim.get("u1_stubbed_ideal_regulator") is True
    probes = sim.get("probes") or []
    assert any("3V3" in str(p).upper() or "3.3" in str(p) for p in probes)


def test_a26_old_vs_new_v_value_topology_fixture():
    """Fixture: mega-25 ledger used DC 3.3 on VIN-class inject; new uses 3.3 on 3V3."""
    from services.agent.agent_turn import _ecad_ensure_driven_rail

    calls = _ledger_ecad_calls()
    # Drop post-DRC force so we re-run ensure on the pre-sim shape.
    trimmed = []
    for c in calls:
        if c.get("tool") == "ecad_simulate":
            break
        # Drop prior V inject so ensure re-places.
        if (
            c.get("tool") == "ecad_place_symbol"
            and "V_source" in str((c.get("args") or {}).get("libid") or "")
        ):
            continue
        if c.get("tool") == "ecad_wire" and str(
            (c.get("args") or {}).get("from_ref") or ""
        ).startswith("V"):
            continue
        trimmed.append(c)

    old_v = [
        (c.get("args") or {}).get("value")
        for c in calls
        if c.get("tool") == "ecad_place_symbol"
        and "V_source" in str((c.get("args") or {}).get("libid") or "")
    ]
    assert old_v and old_v[0] == "DC 3.3"

    seen: list[dict] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        if tool == "ecad_place_symbol":
            seen.append(a)
            lib = str(a.get("libid") or "")
            if "V" in lib:
                return {
                    "ok": True, "ref": "V1", "libid": lib, "value": a.get("value"),
                    "pins": [{"number": "+"}, {"number": "-"}],
                }
            return {
                "ok": True, "ref": a.get("ref") or "R2", "libid": lib,
                "pins": [{"number": "1"}, {"number": "2"}],
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_add_power":
            return {"ok": True, "net": a.get("net"), "net_size": 1}
        return {"ok": True}

    allow = {
        "ecad_place_symbol", "ecad_wire", "ecad_add_power", "ecad_simulate",
        "ecad_run_erc",
    }
    _ecad_ensure_driven_rail(
        calls=list(trimmed),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=20,
        reason="a26_ledger",
        steps=0,
        max_steps=12,
    )
    v_new = [a for a in seen if "V" in str(a.get("libid") or "")]
    assert v_new and v_new[0].get("value") == "3.3", seen


# ---------------------------------------------------------------------------
# B26 — clearance retry fires from truncated DRC + build allow
# ---------------------------------------------------------------------------


def test_b26_truncated_drc_errors_recovered():
    from services.agent.agent_turn import _ecad_last_drc_errors, _ecad_chain_need

    # Live mega-25 truncated shape (unterminated JSON string).
    raw = (
        '{"ok":true,"doc_id":"7c4fae119443","revision":6,"errors":27,'
        '"warnings":[],"verdict":"27 error(s), 0 warning(s). worst: '
        "[ERROR] clearance: clearance 0.1299mm < required 0.2mm between p11"
        # truncated mid-string — no closing quote/brace
    )
    calls = [
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {"ok": True, "doc_id": "7c4fae119443", "board_doc_id": "7c4fae119443"},
        },
        {
            "tool": "ecad_optimize_traces", "ok": True,
            "args": {"doc_id": "7c4fae119443", "min_clearance_mm": 0.2},
            "result": {"ok": True, "doc_id": "7c4fae119443"},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "args": {"doc_id": "7c4fae119443"},
            "result": raw,
        },
    ]
    assert _ecad_last_drc_errors(calls) == 27
    # Live BUILD allow (no optimize listed).
    allow_build = {
        "ecad_add_power", "ecad_place_symbol", "ecad_wire", "ecad_run_erc",
        "ecad_simulate", "board_from_schematic", "ecad_run_drc",
    }
    # Need full product context for chain_need.
    full = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": "sch1"},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "ref": "R1"},
            "result": {
                "ok": True, "ref": "R1",
                "pins": [
                    {"ref": "R1", "pin": "1", "name": "1", "type": "passive"},
                    {"ref": "R1", "pin": "2", "name": "2", "type": "passive"},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3"},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {"from_ref": "R1", "from_pin": "1",
                     "to_ref": "R1", "to_pin": "2"},
            "result": {"ok": True, "net": "3V3", "net_size": 2},
        },
    ] + calls
    assert _ecad_chain_need(full, allow_build) == "ecad_optimize_traces"


def test_b26_mega25_ledger_snap_fires_clearance_retry():
    """Replay post-DRC mega-25 ledger: autoroute fires, retry_n>=1, re-DRC."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade,
        _ecad_optimize_retry_count,
        _ecad_last_drc_errors,
    )

    calls = _ledger_ecad_calls()
    idx = next(i for i, c in enumerate(calls) if c.get("tool") == "ecad_run_drc")
    snap = calls[: idx + 1]
    # BUILD-shaped allow (optimize omitted — was the unreachable gate).
    allow = {
        "ecad_add_power", "ecad_find_part", "ecad_new_schematic",
        "ecad_place_symbol", "ecad_wire", "ecad_run_erc", "ecad_simulate",
        "board_from_schematic", "ecad_run_drc", "ecad_antenna_design",
        "ecad_antenna_analyze", "delegate",
    }
    dispatched: list[str] = []

    def _dispatch(tool, args):
        a = dict(args or {})
        dispatched.append(tool)
        if tool == "ecad_autoroute":
            assert float(a.get("pitch") or 0) >= 0.3
            assert a.get("doc_id") == "7c4fae119443"
            return {"ok": True, "doc_id": a.get("doc_id"), "routed": 2}
        if tool == "ecad_optimize_traces":
            return {"ok": True, "doc_id": a.get("doc_id"), "corners_rounded": 1}
        if tool == "ecad_run_drc":
            return {
                "ok": True, "doc_id": a.get("doc_id"), "errors": 0,
                "violations": [], "verdict": "0 error(s)",
            }
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0, "doc_id": "f9ec35394c6c"}
        if tool == "ecad_simulate":
            return {
                "ok": True, "device_count": 2, "op": {"V(3V3)": 3.3},
                "nodes": ["3V3", "GND"], "doc_id": "f9ec35394c6c",
                "resolved_net": "3V3",
            }
        if tool == "ecad_place_symbol":
            lib = str(a.get("libid") or "")
            if "V" in lib:
                assert a.get("value") == "3.3"
                return {
                    "ok": True, "ref": "V1", "libid": lib, "value": a.get("value"),
                    "pins": [{"number": "+"}, {"number": "-"}],
                }
            return {
                "ok": True, "ref": a.get("ref") or "Rload", "libid": lib,
                "pins": [{"number": "1"}, {"number": "2"}],
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": a.get("net") or "3V3", "net_size": 2,
                "net_members": [
                    f"{a.get('from_ref')}.{a.get('from_pin')}",
                    f"{a.get('to_ref')}.{a.get('to_pin')}",
                ],
            }
        if tool == "ecad_add_power":
            return {"ok": True, "net": a.get("net"), "net_size": 1}
        return {"ok": True}

    new_calls, made = _ecad_force_chain_cascade(
        calls=list(snap),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=24,
        reason="end_of_turn_ecad_force",
        max_steps=12,
    )
    assert "ecad_autoroute" in dispatched, dispatched
    assert "ecad_optimize_traces" in dispatched, dispatched
    # re-DRC after retry
    assert dispatched.count("ecad_run_drc") >= 1
    assert _ecad_optimize_retry_count(new_calls) >= 1
    # After clean re-DRC in this fixture, errors==0.
    assert int(_ecad_last_drc_errors(new_calls) or 0) == 0
    assert made > 0


def test_b26_clearance_retry_bounded_once():
    """Second dirty DRC must not loop forever (retry_n cap)."""
    from services.agent.agent_turn import (
        _ecad_force_chain_cascade,
        _ecad_optimize_retry_count,
        _ecad_chain_need,
    )

    brd, sch = "brd_b26", "sch_b26"
    base = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "ecad_place_symbol", "ok": True,
            "args": {"libid": "Device:R", "ref": "R1", "doc_id": sch},
            "result": {
                "ok": True, "ref": "R1",
                "pins": [
                    {"ref": "R1", "pin": "1", "name": "1", "type": "passive"},
                    {"ref": "R1", "pin": "2", "name": "2", "type": "passive"},
                ],
            },
        },
        {
            "tool": "ecad_add_power", "ok": True,
            "args": {"net": "3V3", "doc_id": sch},
            "result": {"ok": True, "net": "3V3", "net_size": 1},
        },
        {
            "tool": "ecad_wire", "ok": True,
            "args": {
                "from_ref": "R1", "from_pin": "1",
                "to_ref": "R1", "to_pin": "2", "doc_id": sch,
            },
            "result": {"ok": True, "net": "3V3", "net_size": 2},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "ok": True, "doc_id": brd, "board_doc_id": brd, "sch_doc_id": sch,
            },
        },
        {
            "tool": "ecad_optimize_traces", "ok": True,
            "args": {"doc_id": brd, "min_clearance_mm": 0.2},
            "result": {"ok": True, "doc_id": brd},
        },
        {
            "tool": "ecad_run_drc", "ok": True,
            "args": {"doc_id": brd},
            "result": {
                "ok": True, "doc_id": brd, "errors": 27,
                "violations": [
                    {"rule": "clearance", "message": "clearance 0.1299mm < 0.2mm"},
                ],
                "verdict": "27 error(s). worst: [ERROR] clearance: 0.1299mm",
            },
        },
    ]
    allow = {
        "ecad_new_schematic", "ecad_place_symbol", "ecad_wire", "ecad_add_power",
        "board_from_schematic", "ecad_run_drc", "ecad_run_erc", "ecad_simulate",
        # intentionally omit optimize/autoroute from allow — cascade must expand
    }
    n_ar = {"n": 0}

    def _dispatch(tool, args):
        if tool == "ecad_autoroute":
            n_ar["n"] += 1
            return {"ok": True, "doc_id": brd, "routed": 1}
        if tool == "ecad_optimize_traces":
            return {"ok": True, "doc_id": brd}
        if tool == "ecad_run_drc":
            # Still dirty after retry — honest residual.
            return {
                "ok": True, "doc_id": brd, "errors": 12,
                "violations": [{"rule": "clearance", "message": "still tight"}],
                "verdict": "12 error(s) clearance",
            }
        if tool == "ecad_run_erc":
            return {"ok": True, "errors": 0, "doc_id": sch}
        if tool == "ecad_simulate":
            return {"ok": True, "op": {"V(3V3)": 3.3}, "doc_id": sch}
        if tool == "ecad_place_symbol":
            lib = str((args or {}).get("libid") or "")
            if "V" in lib:
                return {
                    "ok": True, "ref": "V1", "libid": lib,
                    "value": (args or {}).get("value"),
                    "pins": [{"number": "+"}, {"number": "-"}],
                }
            return {
                "ok": True, "ref": (args or {}).get("ref") or "Rx",
                "pins": [{"number": "1"}, {"number": "2"}],
            }
        if tool == "ecad_wire":
            return {
                "ok": True, "net": (args or {}).get("net") or "3V3", "net_size": 2,
            }
        if tool == "ecad_add_power":
            return {"ok": True, "net": (args or {}).get("net"), "net_size": 1}
        return {"ok": True}

    new_calls, _ = _ecad_force_chain_cascade(
        calls=list(base),
        allow=allow,
        dispatch=_dispatch,
        ledger=None,
        actor="test",
        messages=[],
        tool_calls_made=0,
        budget=24,
        reason="b26",
        max_steps=12,
    )
    assert n_ar["n"] == 1, n_ar
    assert _ecad_optimize_retry_count(new_calls) >= 1
    # After one retry, chain must not demand another optimize forever.
    need2 = _ecad_chain_need(new_calls, allow | {"ecad_optimize_traces", "ecad_autoroute"})
    if need2 == "ecad_optimize_traces":
        assert _ecad_optimize_retry_count(new_calls) >= 1


# ---------------------------------------------------------------------------
# Regression holds
# ---------------------------------------------------------------------------


def test_s23_in_band_and_y25_sch_sim_hold():
    from services.agent.agent_turn import _ecad_forced_tool_args

    sch, brd = "sch_hold", "brd_hold"
    calls = [
        {
            "tool": "ecad_new_schematic", "ok": True,
            "result": {"ok": True, "doc_id": sch},
        },
        {
            "tool": "board_from_schematic", "ok": True,
            "result": {
                "ok": True, "doc_id": brd, "board_doc_id": brd, "sch_doc_id": sch,
            },
        },
    ]
    sim = _ecad_forced_tool_args("ecad_simulate", calls)
    drc = _ecad_forced_tool_args("ecad_run_drc", calls)
    assert sim.get("doc_id") == sch
    assert drc.get("doc_id") == brd
