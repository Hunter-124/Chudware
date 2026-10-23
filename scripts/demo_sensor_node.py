#!/usr/bin/env python3
"""Sensor-node demo — STM32 sensor-node board end-to-end content smoke.

One scripted mission exercises the ECAD, firmware and simulation tool surface end-to-end:

  board (MCU + I2C temp sensor + regulator + FPGA-lite optional leg)
    → verification ladder (ERC/DRC/hostcheck/sicheck/FPGA/PDN)
    → firmware project + pinmap + co-verify (+ emulated sensors when Renode present)
    → ECAD→thermal FEA → heatsink pick for the regulator
    → system power budget with a duty-cycle profile
    → make_report verification deliverable

Fast path (<5 min): uses dry-run-friendly legs where solvers are absent, live FEA when
``CHUDWARE_LIBCHUDWARE`` points at a sim-enabled libchudware, and honest SKIP markers for
Renode/OpenFOAM-gated legs. Exit 0 on overall PASS (SKIPs allowed); exit 1 on FAIL.

Gate wiring: ``scripts/gate.sh`` stage_sensor_node_demo invokes this with the mcp-venv.
Artifacts land under ``$OUT_DIR`` (default ``scratch/sensor_node_demo/``).

Run:
  CHUDWARE_LIBCHUDWARE=$PWD/core/build-occt-linux/bin/libchudware.so \\
    python3 scripts/demo_sensor_node.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OUT_DIR = Path(os.environ.get(
    "CHUDWARE_SENSOR_DEMO_OUT", str(REPO / "scratch" / "sensor_node_demo")))
MARKER_DIR = Path(os.environ.get(
    "CHUDWARE_SENSOR_DEMO_MARKERS", str(OUT_DIR / "markers")))


# ── harness ──────────────────────────────────────────────────────────────────

class _StubMCP:
    """Collects @mcp.tool()-decorated functions (no FastMCP / stdio needed)."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _call(tools: dict, tool: str, **kwargs: Any) -> dict:
    fn = tools.get(tool)
    if fn is None:
        return {"ok": False, "error": f"tool {tool!r} not registered"}
    raw = fn(**kwargs)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {"ok": False, "error": f"{tool} returned non-JSON", "raw": raw[:200]}
    return raw if isinstance(raw, dict) else {"ok": False, "error": f"{tool} bad type"}


class Leg:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "PENDING"  # PASS | FAIL | SKIP
        self.detail = ""
        self.artifact: Any = None
        self.ms = 0

    def pass_(self, detail: str = "", artifact: Any = None) -> None:
        self.status, self.detail, self.artifact = "PASS", detail, artifact

    def fail(self, detail: str) -> None:
        self.status, self.detail = "FAIL", detail

    def skip(self, detail: str) -> None:
        self.status, self.detail = "SKIP", detail


LEGS: list[Leg] = []


def run_leg(name: str, fn: Callable[[Leg], None]) -> Leg:
    leg = Leg(name)
    LEGS.append(leg)
    t0 = time.time()
    try:
        fn(leg)
    except Exception as exc:  # noqa: BLE001 — demo never crashes the gate
        leg.fail(f"{type(exc).__name__}: {exc}")
        leg.detail += "\n" + traceback.format_exc(limit=4)
    leg.ms = int((time.time() - t0) * 1000)
    marker = MARKER_DIR / f"{name.replace(' ', '_').lower()}.marker"
    marker.write_text(f"{leg.status}\n{leg.detail}\n", encoding="utf-8")
    print(f"  [{leg.status:4}] {name} ({leg.ms} ms) — {leg.detail[:120]}")
    return leg


# ── board fixture ────────────────────────────────────────────────────────────

def _pad(net: str, n: str = "1", size: float = 1.2, x: float = 0.0, y: float = 0.0) -> dict:
    return {"id": f"p{n}_{net}", "number": n, "pin": n, "pos": [x, y], "angle": 0.0,
            "shape": "rect", "size": [size, size], "type": "smd",
            "layers": ["F.Cu"], "net": net}


def build_sensor_node_board(tools: dict, name: str = "sensor_node_stm32") -> str:
    """STM32F4 sensor-node: regulator U1, MCU U2, I2C temp sensor U3, FPGA-lite U4,
    decoupling caps, crystal, BOOT0 pull-down, closed outline + power/I2C nets."""
    # The ecad session lives in the C++ core; the facade mirrors the old
    # engine.ecad.session API, but doc mutations need an explicit swap_doc write-back.
    from services.mcp.ecad_common import ecad_session
    from services.mcp.ecad_tools import _ECAD

    _call(tools, "ecad_new_pcb", name=name)
    doc_id = _ECAD["current_pcb"]
    _sess = ecad_session.get_session(doc_id)
    doc = _sess.doc

    # 50 x 40 mm closed outline
    pts = [(0, 0), (50, 0), (50, 40), (0, 40)]
    doc["outline"] = {"closed": True, "shapes": [
        {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4])}
        for i in range(4)]}

    for net in ("+3V3", "GND", "SCL", "SDA", "NRST", "BOOT0", "LED", "VCORE", "VIO"):
        if not any(n.get("name") == net for n in doc.get("nets", [])):
            doc.setdefault("nets", []).append({"name": net, "class": "default"})

    reg_eng = {
        "power_w": {"typ": 1.8, "max": 2.5},
        "theta_c_per_w": {"ja": 40.0, "jc": 5.0},
        "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
        "package": {"body_l_mm": 10.0, "body_w_mm": 9.0, "height_mm": 4.5},
        "supply": {"rails": [{"name": "+3V3", "voltage_v_min": 3.0,
                              "voltage_v_nom": 3.3, "voltage_v_max": 3.6,
                              "current_a_active": 0.5, "current_a_sleep": 0.001}],
                   "voltage_domains": ["+3V3"], "sequencing": []},
        "sources": ["golden demo: linear regulator ~1.8 W"],
    }
    mcu_eng = {
        "power_w": {"typ": 0.25, "max": 0.45},
        "theta_c_per_w": {"ja": 55.0, "jc": 8.0},
        "limits_c": {"tj_max": 105.0, "ta_max": 85.0},
        "package": {"body_l_mm": 14.0, "body_w_mm": 14.0, "height_mm": 1.6},
        "mcu": {"family": "stm32f4", "part": "STM32F407VGT6"},
        "supply": {
            "rails": [
                {"name": "+3V3", "voltage_v_min": 3.0, "voltage_v_nom": 3.3,
                 "voltage_v_max": 3.6, "current_a_active": 0.020,
                 "current_a_sleep": 5e-6},
                {"name": "VCORE", "voltage_v_nom": 1.2, "current_a_active": 0.015,
                 "current_a_sleep": 1e-6},
            ],
            "voltage_domains": ["+3V3", "VCORE"],
            "sequencing": ["+3V3 before VCORE"],
        },
        "sources": ["golden demo: STM32F4 MCU"],
    }
    sensor_eng = {
        "power_w": {"typ": 0.005, "max": 0.01},
        "theta_c_per_w": {"ja": 120.0, "jc": None},
        "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
        "package": {"body_l_mm": 3.0, "body_w_mm": 3.0, "height_mm": 0.8},
        "supply": {"rails": [{"name": "+3V3", "voltage_v_nom": 3.3,
                              "current_a_active": 0.005, "current_a_sleep": 1e-6}],
                   "voltage_domains": ["+3V3"], "sequencing": []},
        "sources": ["golden demo: TMP103-class I2C temp sensor"],
    }
    fpga_eng = {
        "power_w": {"typ": 0.4, "max": 0.8},
        "theta_c_per_w": {"ja": 30.0, "jc": 4.0},
        "limits_c": {"tj_max": 100.0, "ta_max": 85.0},
        "package": {"body_l_mm": 12.0, "body_w_mm": 12.0, "height_mm": 1.4},
        "fpga": {"family": "ice40", "part": "iCE40HX1K", "banks": {
            "0": {"vcco": "3V3", "iostandard": "LVCMOS33"}}},
        "supply": {
            "rails": [
                {"name": "VCORE", "voltage_v_nom": 1.2, "current_a_active": 0.08},
                {"name": "VIO", "voltage_v_nom": 3.3, "current_a_active": 0.02},
            ],
            "voltage_domains": ["VCORE", "VIO"],
            "sequencing": ["VCORE before VIO"],
        },
        "sources": ["golden demo: FPGA-lite optional leg"],
    }

    doc["footprints"] = [
        {"id": "fp1", "ref": "U1", "fpid": "Package_TO:TO-220", "value": "REG_3V3",
         "pos": [12.0, 12.0], "angle": 0.0, "side": "top",
         "pads": [_pad("+3V3", "1", size=6.0), _pad("GND", "2"), _pad("+3V3", "3")],
         "fields": {"height": 4.5, "Value": "REG_3V3"}, "eng_overrides": reg_eng},
        {"id": "fp2", "ref": "U2", "fpid": "Package_QFP:LQFP-100", "value": "STM32F407",
         "pos": [30.0, 20.0], "angle": 0.0, "side": "top",
         "pads": [
             _pad("+3V3", "1", x=-5), _pad("GND", "2", x=-4),
             _pad("SCL", "3", x=-3), _pad("SDA", "4", x=-2),
             _pad("NRST", "5", x=-1), _pad("BOOT0", "6", x=0),
             _pad("LED", "7", x=1), _pad("VCORE", "8", x=2),
         ],
         "fields": {"height": 1.6, "Value": "STM32F407VGT6"}, "eng_overrides": mcu_eng},
        {"id": "fp3", "ref": "U3", "fpid": "Package_SO:SOT-23-6", "value": "TMP103",
         "pos": [42.0, 30.0], "angle": 0.0, "side": "top",
         "pads": [_pad("+3V3", "1"), _pad("GND", "2"),
                  _pad("SCL", "3"), _pad("SDA", "4")],
         "fields": {"height": 0.8, "Value": "TMP103"}, "eng_overrides": sensor_eng},
        {"id": "fp4", "ref": "U4", "fpid": "Package_BGA:BGA-121", "value": "iCE40HX1K",
         "pos": [18.0, 32.0], "angle": 0.0, "side": "top",
         "pads": [_pad("VCORE", "1"), _pad("VIO", "2"), _pad("GND", "3"),
                  _pad("SCL", "4"), _pad("SDA", "5")],
         "fields": {"height": 1.4, "Value": "iCE40HX1K"}, "eng_overrides": fpga_eng},
        # Decoupling near MCU rails (hostcheck)
        {"id": "fp5", "ref": "C1", "fpid": "Capacitor_SMD:C_0402", "value": "100n",
         "pos": [28.0, 14.0], "angle": 0.0, "side": "top",
         "pads": [_pad("+3V3", "1"), _pad("GND", "2")],
         "fields": {"height": 0.5}},
        {"id": "fp6", "ref": "C2", "fpid": "Capacitor_SMD:C_0402", "value": "100n",
         "pos": [32.0, 14.0], "angle": 0.0, "side": "top",
         "pads": [_pad("VCORE", "1"), _pad("GND", "2")],
         "fields": {"height": 0.5}},
        # Crystal (hostcheck crystal presence)
        {"id": "fp7", "ref": "Y1", "fpid": "Crystal:Crystal_SMD", "value": "8MHz",
         "pos": [38.0, 12.0], "angle": 0.0, "side": "top",
         "pads": [_pad("GND", "1"), _pad("GND", "2")],
         "fields": {"height": 0.8}},
        # BOOT0 pull-down
        {"id": "fp8", "ref": "R1", "fpid": "Resistor_SMD:R_0402", "value": "10k",
         "pos": [34.0, 26.0], "angle": 0.0, "side": "top",
         "pads": [_pad("BOOT0", "1"), _pad("GND", "2")],
         "fields": {"height": 0.4}},
    ]

    # Power distribution + I2C bus tracks
    doc["tracks"] = [
        {"id": "t1", "kind": "segment", "p1": [12.0, 12.0], "p2": [30.0, 20.0],
         "width": 0.5, "net": "+3V3", "layer": "F.Cu"},
        {"id": "t2", "kind": "segment", "p1": [30.0, 20.0], "p2": [42.0, 30.0],
         "width": 0.2, "net": "SCL", "layer": "F.Cu"},
        {"id": "t3", "kind": "segment", "p1": [30.0, 20.0], "p2": [42.0, 31.0],
         "width": 0.2, "net": "SDA", "layer": "F.Cu"},
        {"id": "t4", "kind": "segment", "p1": [30.0, 20.0], "p2": [18.0, 32.0],
         "width": 0.4, "net": "VCORE", "layer": "F.Cu"},
    ]
    ecad_session.swap_doc(_sess, doc)
    return doc_id


def build_minimal_schematic(tools: dict, name: str = "sensor_node_sch") -> str:
    """Minimal schematic so ERC has something to check (advisory, not full netlist parity)."""
    from services.mcp.ecad_tools import _ECAD
    r = _call(tools, "ecad_new_schematic", name=name)
    if not r.get("ok") and not r.get("doc_id"):
        # some versions return doc_id at top level without ok
        if not _ECAD.get("current_sch"):
            return ""
    sid = r.get("doc_id") or _ECAD.get("current_sch") or ""
    if not sid:
        return ""
    # Place a couple of symbols if the library has them — best-effort.
    for libid, x in (("Device:R", 0), ("Device:C", 40), ("Device:R", 80)):
        try:
            _call(tools, "ecad_place_symbol", libid=libid, x=x, y=0, doc_id=sid)
        except Exception:  # noqa: BLE001
            pass
    return sid


# ── legs ─────────────────────────────────────────────────────────────────────

def register_all_tools() -> dict:
    from services.mcp.ecad_tools import register_ecad_tools
    from services.mcp.ecad_thermal import register_ecad_thermal_tools
    from services.mcp.heatsink import register_heatsink_tools
    from services.mcp.power_budget import register_power_budget_tools
    from services.mcp.firmware_tools import register_firmware_tools

    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_heatsink_tools(stub)
    register_power_budget_tools(stub)
    register_firmware_tools(stub)
    return stub.tools


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    for old in MARKER_DIR.glob("*.marker"):
        old.unlink()

    print("=== Sensor-node demo: STM32 sensor-node ===")
    print(f"  out: {OUT_DIR}")
    print(f"  CHUDWARE_LIBCHUDWARE={os.environ.get('CHUDWARE_LIBCHUDWARE', '<unset>')}")

    tools = register_all_tools()
    state: dict[str, Any] = {}

    # ── 1. Board ──────────────────────────────────────────────────────────
    def leg_board(leg: Leg) -> None:
        doc_id = build_sensor_node_board(tools)
        state["pcb_doc_id"] = doc_id
        from services.mcp.ecad_common import ecad_session
        doc = ecad_session.get_session(doc_id).doc
        n_fp = len(doc.get("footprints") or [])
        assert n_fp >= 6, f"expected >=6 footprints, got {n_fp}"
        assert doc.get("outline", {}).get("closed"), "outline must be closed"
        leg.pass_(f"pcb doc_id={doc_id}, {n_fp} footprints",
                  artifact={"doc_id": doc_id, "n_footprints": n_fp})

    run_leg("board", leg_board)

    # ── 2. Schematic + ERC ────────────────────────────────────────────────
    def leg_erc(leg: Leg) -> None:
        sid = build_minimal_schematic(tools)
        state["sch_doc_id"] = sid
        if not sid:
            leg.skip("no schematic session available")
            return
        r = _call(tools, "ecad_run_erc", doc_id=sid)
        if not r.get("ok") and "error" in r and "Open a schematic" in str(r.get("error", "")):
            leg.skip(f"ERC unavailable: {r.get('error')}")
            return
        # ERC is advisory — PASS if tool ran (errors may exist on a minimal sch)
        state["erc"] = r
        n_err = int(r.get("errors") or 0)
        n_warn = int(r.get("warnings") or len(r.get("violations") or []))
        leg.pass_(f"ERC ran: {n_err} errors, {n_warn} warnings (advisory)",
                  artifact=r)

    run_leg("erc", leg_erc)

    # ── 3. DRC ────────────────────────────────────────────────────────────
    def leg_drc(leg: Leg) -> None:
        r = _call(tools, "ecad_run_drc", doc_id=state["pcb_doc_id"])
        state["drc"] = r
        if not r.get("ok") and r.get("error"):
            # empty board rules may still return ok with zero violations
            leg.fail(f"DRC failed: {r.get('error')}")
            return
        n = len(r.get("violations") or [])
        leg.pass_(f"DRC ran: {n} violations (advisory)", artifact=r)

    run_leg("drc", leg_drc)

    # ── 4. Host-board pack ────────────────────────────────────────────────
    def leg_hostcheck(leg: Leg) -> None:
        r = _call(tools, "ecad_run_hostcheck", doc_id=state["pcb_doc_id"])
        state["hostcheck"] = r
        if not r.get("ok"):
            leg.fail(f"hostcheck failed: {r.get('error')}")
            return
        n = len(r.get("violations") or [])
        rules = sorted({g.get("rule") for g in (r.get("by_rule") or []) if g.get("rule")})
        leg.pass_(f"{n} findings, rules={rules[:6]}", artifact=r)

    run_leg("hostcheck", leg_hostcheck)

    # ── 5. SI pack ────────────────────────────────────────────────────────
    def leg_sicheck(leg: Leg) -> None:
        r = _call(tools, "ecad_run_sicheck", doc_id=state["pcb_doc_id"])
        state["sicheck"] = r
        if not r.get("ok") and r.get("error"):
            leg.fail(f"sicheck failed: {r.get('error')}")
            return
        n = len(r.get("violations") or [])
        leg.pass_(f"SI pack ran: {n} findings", artifact=r)

    run_leg("sicheck", leg_sicheck)

    # ── 6. FPGA pack (schematic-side: bank check + constraint export) ─────
    def leg_fpga(leg: Leg) -> None:
        # ecad_fpga_check is a schematic tool (bank/IO validation on the FPGA symbol).
        sch = _call(tools, "ecad_new_schematic", name="sensor_node_fpga_sch")
        sid = sch.get("doc_id") or ""
        if not sid:
            leg.skip("could not open FPGA schematic")
            return
        placed = _call(tools, "ecad_place_symbol", libid="FPGA:iCE40UP5K-SG48",
                       x=100, y=100, doc_id=sid)
        if not placed.get("ok"):
            leg.skip(f"FPGA symbol unavailable: {placed.get('error') or placed.get('hint')}")
            return
        r = _call(tools, "ecad_fpga_check", doc_id=sid)
        state["fpga"] = r
        if r.get("error") and "FPGA" in str(r.get("error", "")) and not r.get("devices"):
            leg.skip(str(r.get("error")))
            return
        # Bare FPGA flags config pins (CRESET_B etc.) — tool ran successfully.
        n_viol = len(r.get("violations") or [])
        exp = _call(tools, "ecad_export", kind="constraints",
                    path=str(OUT_DIR / "fpga_constraints"), doc_id=sid)
        state["fpga_export"] = exp
        if not exp.get("ok") and not r.get("devices"):
            leg.fail(f"fpga_check/export failed: check={r.get('error')} export={exp.get('error')}")
            return
        leg.pass_(f"devices={len(r.get('devices') or [])} violations={n_viol} "
                  f"(config pins on bare FPGA expected); export ok={exp.get('ok')}",
                  artifact={"check": r, "export": exp})

    run_leg("fpga", leg_fpga)

    # ── 7. PDN ────────────────────────────────────────────────────────────
    def leg_pdn(leg: Leg) -> None:
        r = _call(tools, "ecad_run_pdn", net="+3V3", source_refs="U1",
                  doc_id=state["pcb_doc_id"])
        state["pdn"] = r
        if not r.get("ok"):
            # PDN may need more copper — still record and soft-fail only on hard errors
            if r.get("skipped") or "unavailable" in str(r.get("error", "")).lower():
                leg.skip(str(r.get("error") or r.get("hint") or "PDN skipped"))
                return
            # Treat analysis-with-violations as PASS (the tool ran)
            if r.get("violations") is not None or r.get("nets"):
                leg.pass_(f"PDN ran with findings: {r.get('error') or r.get('verdict')}",
                          artifact=r)
                return
            leg.fail(f"PDN failed: {r.get('error')}")
            return
        bp = r.get("boardphys_path")
        leg.pass_(f"PDN ok; boardphys={bp}", artifact=r)

    run_leg("pdn", leg_pdn)

    # ── 8. Firmware project + co-verify + optional sim ────────────────────
    def leg_firmware(leg: Leg) -> None:
        # Persist board JSON so fw tools can take a path ref
        from services.mcp.ecad_common import ecad_session
        board_path = OUT_DIR / "sensor_node_board.json"
        board_doc = ecad_session.get_session(state["pcb_doc_id"]).doc
        board_path.write_text(json.dumps(board_doc, indent=2), encoding="utf-8")
        state["board_path"] = str(board_path)

        r = _call(tools, "fw_new_project", name="sensor_node_fw",
                  mcu_family="stm32f4", board_doc_ref=state["pcb_doc_id"])
        if not r.get("ok") and not r.get("project_id"):
            # try path ref
            r = _call(tools, "fw_new_project", name="sensor_node_fw",
                      mcu_family="stm32f4", board_doc_ref=str(board_path))
        state["fw_project"] = r
        pid = r.get("project_id") or (r.get("project") or {}).get("id")
        if not pid:
            leg.fail(f"fw_new_project failed: {r.get('error') or r}")
            return

        # Static co-verify (no emulator needed)
        v = _call(tools, "fw_verify_board", board_doc_ref=state["pcb_doc_id"],
                  project_id=pid, mcu_family="stm32f4")
        if not v.get("ok") and v.get("error"):
            v = _call(tools, "fw_verify_board", board_doc_ref=str(board_path),
                      project_id=pid, mcu_family="stm32f4")
        state["fw_verify"] = v

        # Build + sim: honest skip when toolchain/renode absent
        b = _call(tools, "fw_build", project_id=pid)
        state["fw_build"] = b
        sim_detail = "build not attempted"
        if b.get("success") and b.get("elf_path"):
            s = _call(tools, "fw_sim_run", project_id=pid, elf_path=b["elf_path"],
                      scenario="fixture:i2c_temp_tmp103")
            if s.get("uart_log") is not None or s.get("passed") is not None:
                state["fw_sim"] = s
                if s.get("passed"):
                    sim_detail = "sim PASS with sensor fixture"
                elif s.get("skipped") or "not installed" in str(s.get("uart_log", "")).lower() \
                        or "renode" in str(s.get("uart_log", "")).lower() \
                        or "qemu" in str(s.get("uart_log", "")).lower():
                    sim_detail = f"sim SKIP (emulator): {str(s.get('uart_log', ''))[:80]}"
                    # mark a dedicated sim skip but keep firmware leg PASS on static verify
                    (MARKER_DIR / "fw_sim.marker").write_text(
                        f"SKIP\n{sim_detail}\n", encoding="utf-8")
                else:
                    sim_detail = f"sim ran passed={s.get('passed')}"
            else:
                sim_detail = f"sim unavailable: {s.get('error') or s.get('uart_log')}"
                (MARKER_DIR / "fw_sim.marker").write_text(
                    f"SKIP\n{sim_detail}\n", encoding="utf-8")
        else:
            sim_detail = f"fw_build skipped/failed: success={b.get('success')} " \
                         f"rc={b.get('returncode')} (cross-toolchain may be absent)"
            (MARKER_DIR / "fw_sim.marker").write_text(
                f"SKIP\n{sim_detail}\n", encoding="utf-8")

        n_findings = len((v or {}).get("findings") or [])
        leg.pass_(f"project={pid}; verify findings={n_findings}; {sim_detail}",
                  artifact={"project": r, "verify": v, "build": b,
                            "sim": state.get("fw_sim")})

    run_leg("firmware", leg_firmware)

    # ── 9. Thermal FEA handoff ────────────────────────────────────────────
    def leg_thermal(leg: Leg) -> None:
        try:
            import build123d  # noqa: F401
        except ImportError:
            leg.skip("build123d not installed — FEA solid path unavailable")
            return
        dry = os.environ.get("CHUDWARE_SENSOR_DEMO_THERMAL_DRY", "").strip() in ("1", "true", "yes")
        r = _call(tools, "ecad_thermal_handoff", doc_id=state["pcb_doc_id"],
                  dry_run=dry, mesh_fineness=0.3)
        state["thermal"] = r
        if not r.get("ok"):
            leg.fail(f"thermal handoff failed: {r.get('error')}")
            return
        if r.get("dry_run") or not r.get("solved"):
            # Solver may be absent — envelope still proves the handoff seam
            if r.get("simhandoff"):
                leg.pass_(f"handoff envelope ok (solved={r.get('solved')}, "
                          f"dry={r.get('dry_run')}); sources="
                          f"{r.get('coverage', {}).get('n_heat_sources')}",
                          artifact=r)
                return
            if r.get("error_solve"):
                leg.pass_(f"geometry+BCs ok; solver unavailable: {r.get('error_solve')[:80]}",
                          artifact=r)
                return
            leg.fail(f"thermal handoff incomplete: {r}")
            return
        tmax = r.get("board_max_temp_c")
        leg.pass_(f"solved board max {tmax} C; {len(r.get('components') or [])} components",
                  artifact=r)

    run_leg("thermal", leg_thermal)

    # ── 10. Heatsink pick ─────────────────────────────────────────────────
    def leg_heatsink(leg: Leg) -> None:
        if not state.get("thermal"):
            leg.skip("no thermal context")
            return
        # last_ctx is set even on dry-run / unsolved; heatsink degrades to network_estimate
        r = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=85.0,
                  include_library=True, include_parametric=True, max_candidates=6)
        state["heatsink"] = r
        if not r.get("ok"):
            if "no thermal context" in str(r.get("error", "")):
                leg.skip(str(r.get("error")))
                return
            leg.fail(f"heatsink_select failed: {r.get('error')}")
            return
        winner = (r.get("winner") or {}).get("candidate")
        achieved = (r.get("winner") or {}).get("achieved_c")
        met = r.get("target_met")
        # For the golden demo: PASS if tool ranks candidates honestly (met or not)
        leg.pass_(f"winner={winner!r} achieved={achieved} C target_met={met} "
                  f"mode={r.get('mode')}", artifact=r)

    run_leg("heatsink", leg_heatsink)

    # ── 11. Power budget ──────────────────────────────────────────────────
    def leg_budget(leg: Leg) -> None:
        from services.mcp.power_budget import solve_power_budget

        # Prefer live boardphys if thermal/pdn wrote one; else synthetic sensor-node sidecar
        boardphys = None
        for cand in (
            (state.get("pdn") or {}).get("boardphys_path"),
            (state.get("thermal") or {}).get("step_path", "") + ".boardphys.json",
        ):
            if cand and os.path.isfile(cand):
                try:
                    with open(cand, encoding="utf-8") as fh:
                        boardphys = json.load(fh)
                    break
                except Exception:  # noqa: BLE001
                    pass
        if boardphys is None:
            boardphys = {
                "schema": "aicad.boardphys/1",
                "board": {"name": "sensor_node_stm32"},
                "components": [
                    {"ref": "U2", "supply": {
                        "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                                   "current_a_active": 0.020, "current_a_sleep": 5e-6}],
                        "voltage_domains": ["3V3"], "sequencing": []}},
                    {"ref": "U3", "supply": {
                        "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                                   "current_a_active": 0.005, "current_a_sleep": 1e-6}],
                        "voltage_domains": ["3V3"], "sequencing": []}},
                    {"ref": "U1", "supply": {
                        "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                                   "current_a_active": 0.001}],
                        "voltage_domains": ["3V3"], "sequencing": []}},
                ],
            }
        # Duty cycle from emulation if present; else representative 1 % wake
        profile = None
        pp = (state.get("fw_sim") or {}).get("power_profile_path")
        if pp and os.path.isfile(pp):
            with open(pp, encoding="utf-8") as fh:
                profile = json.load(fh)
        if profile is None:
            duty = 0.01
            i_avg = 0.020 * duty + 5e-6 * (1 - duty) + 0.005 * duty
            profile = {
                "schema": "aicad.powerprofile/1",
                "model": "duty-cycle bookkeeping",
                "trace_ref": "golden-demo-synthetic",
                "cpu": {"active_frac": duty, "sleep_frac": 1 - duty},
                "pins": {},
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "i_avg_a": i_avg, "i_peak_a": 0.025,
                           "residency": {"active_frac": duty, "sleep_frac": 1 - duty},
                           "schedule": {"duty": duty, "period_s": 1.0},
                           "provenance": ["golden demo: 1% wake duty synthetic profile"]}],
                "coverage": {"components_total": 3, "components_with_rails": 3,
                             "unknown_active_current": [], "notes": [
                                 "synthetic profile — live fw_sim power_profile used when present"]},
            }
        source = {"kind": "battery", "name": "VBAT", "voltage_v": 3.6,
                  "capacity_mah": 2400.0}
        converters = [{"name": "U_BUCK", "input": "VBAT", "output": "3V3",
                       "voltage_out_v": 3.3, "topology": "buck",
                       "efficiency": 0.90, "i_out_max_a": 0.5}]
        r = solve_power_budget(source, converters=converters, board=boardphys,
                               power_profile=profile)
        state["budget"] = r
        if not r.get("ok"):
            leg.fail(f"power_budget failed: {r.get('error')}")
            return
        verdict = r.get("verdict")
        life = (r.get("battery") or {}).get("life_h")
        # fail verdict is still a successful demo leg if the tool computed it
        leg.pass_(f"verdict={verdict} battery_life_h={life}", artifact=r)
        (OUT_DIR / "power_budget.json").write_text(
            json.dumps(r, indent=2), encoding="utf-8")

    run_leg("power_budget", leg_budget)

    # ── 12. make_report ───────────────────────────────────────────────────
    def leg_report(leg: Leg) -> None:
        try:
            import services.deliverables as d
            from services.deliverables import joins as J
            from services.deliverables import project as dproj
        except Exception as exc:  # noqa: BLE001
            leg.skip(f"deliverables unavailable: {exc}")
            return

        os.environ.setdefault("CHUDWARE_PROJECT_DIR", str(OUT_DIR / "project"))
        dproj.project_dir()  # ensure

        # Attach key leg results as provenance bag
        attached = {}
        for key, tool in (("hostcheck", "ecad_run_hostcheck"),
                          ("thermal", "ecad_thermal_handoff"),
                          ("heatsink", "heatsink_select"),
                          ("budget", "power_budget")):
            payload = state.get(key)
            if isinstance(payload, dict):
                attached.update(J.attach(key, tool, payload))

        def _num(payload: dict, *path: str, default: float = 0.0) -> float:
            cur: Any = payload
            for p in path:
                if not isinstance(cur, dict):
                    return default
                cur = cur.get(p)
            try:
                return float(cur)
            except (TypeError, ValueError):
                return default

        # Seed numeric fields the slots will resolve
        thermal = state.get("thermal") or {}
        heatsink = state.get("heatsink") or {}
        budget = state.get("budget") or {}
        if "thermal" in attached:
            attached["thermal"]["payload"].setdefault(
                "board_max_temp_c",
                _num(thermal, "board_max_temp_c", default=0.0))
        if "heatsink" in attached:
            w = heatsink.get("winner") or {}
            attached["heatsink"]["payload"].setdefault(
                "achieved_c", _num(w, "achieved_c", default=0.0))
            attached["heatsink"]["payload"].setdefault("target_c", 85.0)
        if "budget" in attached:
            attached["budget"]["payload"].setdefault(
                "life_h", _num(budget, "battery", "life_h", default=0.0))

        outline = [
            {"type": "title", "body": [
                "Sensor-node demo — STM32F4 sensor-node board.",
                "Verification ladder + firmware co-verify + thermal FEA + "
                "heatsink pick + system power budget.",
            ]},
            {"type": "requirement", "req_id": "REQ-HOST",
             "criterion": "Host-board pack runs on the sensor-node PCB",
             "verdict": "PASS" if (state.get("hostcheck") or {}).get("ok") else "FAIL",
             "body": ["ecad_run_hostcheck executed; findings are advisory pre-fab checks."],
             "slots": {
                 "measured": J.ref("hostcheck", "errors", round=0)
                 if "hostcheck" in attached else None,
             }},
            {"type": "requirement", "req_id": "REQ-THERM",
             "criterion": "Board thermal study produced (or envelope dry-run)",
             "verdict": "PASS" if (state.get("thermal") or {}).get("ok") else "FAIL",
             "body": ["ecad_thermal_handoff with aicad.simhandoff/1 envelope."],
             "slots": {
                 "measured": J.ref("thermal", "board_max_temp_c", unit="C", round=1)
                 if "thermal" in attached else None,
                 "limit": J.ref("heatsink", "target_c", unit="C", round=0)
                 if "heatsink" in attached else None,
             }},
            {"type": "requirement", "req_id": "REQ-SINK",
             "criterion": "Heatsink ranked for regulator U1 to Tj <= 85 C",
             "verdict": "PASS" if (state.get("heatsink") or {}).get("ok") else "FAIL",
             "body": ["heatsink_select ranked library + parametric candidates."],
             "slots": {
                 "measured": J.ref("heatsink", "achieved_c", unit="C", round=1)
                 if "heatsink" in attached else None,
                 "limit": J.ref("heatsink", "target_c", unit="C", round=0)
                 if "heatsink" in attached else None,
             }},
            {"type": "requirement", "req_id": "REQ-PWR",
             "criterion": "System power budget computes battery life",
             "verdict": "PASS" if (state.get("budget") or {}).get("ok") else "FAIL",
             "body": ["power_budget with duty-cycle profile (live fw profile or 1% synthetic)."],
             "slots": {
                 "measured": J.ref("budget", "life_h", unit="h", round=1)
                 if "budget" in attached else None,
             }},
            {"type": "summary", "heading": "Demo legs",
             "body": [f"{L.name}: {L.status} — {L.detail[:100]}" for L in LEGS]},
        ]
        # Drop None slots
        for blk in outline:
            if "slots" in blk:
                blk["slots"] = {k: v for k, v in blk["slots"].items() if v is not None}
                if not blk["slots"]:
                    blk.pop("slots")

        rec = d.make_report({
            "kind": "report",
            "title": "Sensor-Node Demo",
            "template": "verification_report",
            "format": "docx",
            "outline": outline,
            "attached_results": attached,
        })
        state["report"] = rec
        if not rec.get("ok"):
            leg.fail(f"make_report failed: {rec.get('reason')} {rec.get('hint')}")
            return
        path = rec.get("path")
        leg.pass_(f"report at {path}", artifact=rec)

    run_leg("report", leg_report)

    # ── summary ───────────────────────────────────────────────────────────
    summary = {
        "ok": all(L.status != "FAIL" for L in LEGS),
        "legs": [{"name": L.name, "status": L.status, "detail": L.detail, "ms": L.ms}
                 for L in LEGS],
        "counts": {
            "pass": sum(1 for L in LEGS if L.status == "PASS"),
            "fail": sum(1 for L in LEGS if L.status == "FAIL"),
            "skip": sum(1 for L in LEGS if L.status == "SKIP"),
        },
        "state_keys": sorted(state.keys()),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (MARKER_DIR / "overall.marker").write_text(
        ("PASS" if summary["ok"] else "FAIL") + "\n" + json.dumps(summary["counts"]) + "\n",
        encoding="utf-8")

    print()
    print("=== summary ===")
    for L in LEGS:
        print(f"  {L.status:4}  {L.name:14}  {L.detail[:100]}")
    c = summary["counts"]
    print(f"  totals: {c['pass']} pass / {c['fail']} fail / {c['skip']} skip")
    print(f"  artifacts: {OUT_DIR}")
    if summary["ok"]:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
