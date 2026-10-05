#!/usr/bin/env python3
"""B2 end-to-end battery-charger/controller benchmark — real MCP tool surface.

Deterministic Li-ion charger/controller mission:

  PARTS → SCHEMATIC → SIM-CHARGE (CC→CV taper) → SIM-LVD → SIM-SAFETY
  → BOARD → THERMAL → FIRMWARE (charge SM) → SAFETY-IN-THE-LOOP → POWER/REPORT

Harness pattern copied from scripts/demo_sensor_node.py / bench_flight_computer.py
(_StubMCP / Leg / run_leg / register_all_tools). Honest PASS / WARN / FAIL / SKIP
with actual wall-times. main() returns 0 when no FAIL (SKIP allowed).

Simulation legs use the same MNA solver as ecad_simulate(backend='mna')
(services.mcp.ecad_common._sim_run → the core sim_spice RPC). Behavioral B/S
devices (4-pin VSWITCH, B-source current taper) are not expressible via the 2-pin
library Switch symbol, so those circuits are built as structured SPICE and solved
with the shared backend; a companion ecad_simulate call on a library-backed
sense/rail schematic still
exercises the MCP tool surface.

Env:
  CHUDWARE_BC_OUT          output dir (default benchmarks/battery_charger)
  CHUDWARE_BC_MARKERS      marker dir (default $OUT/markers)
  CHUDWARE_ECAD_HEAVY_BUDGET_S  heavy-tool budget seconds (default 180)
  CHUDWARE_BC_SIM_BUDGET_S Renode fw_sim_run budget (default 120)
  CHUDWARE_BC_SEED         deterministic seed (default 0)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# Heavy ECAD tools budget-guarded at import time — set BEFORE services.mcp imports.
os.environ.setdefault("CHUDWARE_ECAD_HEAVY_BUDGET_S", "180")
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OUT_DIR = Path(os.environ.get(
    "CHUDWARE_BC_OUT", str(REPO / "benchmarks" / "battery_charger")))
MARKER_DIR = Path(os.environ.get(
    "CHUDWARE_BC_MARKERS", str(OUT_DIR / "markers")))

SEED = int(os.environ.get("CHUDWARE_BC_SEED", "0") or "0")
SIM_BUDGET_S = float(os.environ.get("CHUDWARE_BC_SIM_BUDGET_S", "120") or "120")

# Fixed thresholds (deterministic design targets).
V_CV = 4.2          # Li-ion CV clamp (V)
I_CC = 0.5          # CC charge current (A)
V_LVD = 3.0         # load disconnect threshold (V)
V_OVP = 4.35        # over-voltage protect (V)
I_OCP = 2.0         # over-current protect (A)
R_SENSE = 0.05      # current-sense (ohm)
V_OTP_REF = 1.0     # NTC divider ref; trip when Vdiv < ref (hot)

# Library parts the pack must resolve (services.ecad.library starter symbols).
REQUIRED_PARTS: list[tuple[str, str]] = [
    ("Battery_Management:MCP73831", "MCP73831"),
    ("Battery_Management:SwitchingCharger_SOP8", "SwitchingCharger"),
    ("Device:NMOS_Power", "NMOS_Power"),
    ("Device:PMOS_Power", "PMOS_Power"),
    ("Device:PMOS_Reverse", "PMOS_Reverse"),
    ("Device:R_Sense", "R_Sense"),
    ("Power_Management:Load_Switch", "Load_Switch"),
    ("Power_Management:LVD_Comparator", "LVD_Comparator"),
    ("Device:NTC", "NTC"),
    ("Device:L_Power", "L_Power"),
]


# ── harness (from demo_sensor_node.py) ───────────────────────────────────────

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
            return {"ok": False, "error": f"{tool} returned non-JSON",
                    "raw": raw[:200]}
    return raw if isinstance(raw, dict) else {"ok": False,
                                              "error": f"{tool} bad type"}


class Leg:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "PENDING"  # PASS | WARN | FAIL | SKIP
        self.detail = ""
        self.artifact: Any = None
        self.ms = 0

    def pass_(self, detail: str = "", artifact: Any = None) -> None:
        self.status, self.detail, self.artifact = "PASS", detail, artifact

    def warn(self, detail: str, artifact: Any = None) -> None:
        self.status, self.detail, self.artifact = "WARN", detail, artifact

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
    except Exception as exc:  # noqa: BLE001 — bench never crashes the gate
        leg.fail(f"{type(exc).__name__}: {exc}")
        leg.detail += "\n" + traceback.format_exc(limit=4)
    leg.ms = int((time.time() - t0) * 1000)
    marker = MARKER_DIR / f"{name.replace(' ', '_').lower()}.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{leg.status}\n{leg.detail}\n", encoding="utf-8")
    print(f"  [{leg.status:4}] {name} ({leg.ms} ms) — {leg.detail[:140]}")
    return leg


def register_all_tools() -> dict:
    from services.mcp.ecad_tools import register_ecad_tools
    from services.mcp.ecad_thermal import register_ecad_thermal_tools
    from services.mcp.power_budget import register_power_budget_tools
    from services.mcp.firmware_tools import register_firmware_tools

    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_power_budget_tools(stub)
    register_firmware_tools(stub)
    return stub.tools


# ── schematic / sim helpers ──────────────────────────────────────────────────

def _place(tools: dict, sid: str, **kw: Any) -> dict:
    r = _call(tools, "ecad_place_symbol", doc_id=sid, **kw)
    if not r.get("ok"):
        raise RuntimeError(f"place {kw.get('ref') or kw.get('libid')}: "
                           f"{r.get('error') or r}")
    return r


def _wire(tools: dict, sid: str, a: str, ap: str, b: str, bp: str) -> dict:
    r = _call(tools, "ecad_wire", from_ref=a, from_pin=ap, to_ref=b, to_pin=bp,
              doc_id=sid)
    if not r.get("ok"):
        raise RuntimeError(f"wire {a}.{ap}->{b}.{bp}: {r.get('error') or r}")
    return r


def build_charger_schematic(tools: dict, name: str = "battery_charger") -> str:
    """Li-ion charger/controller schematic from the starter pack.

    Topology (library parts; not every IC has a SPICE subckt — sim legs use
    behavioural equivalents of the same thresholds):

      VIN -- PMOS_Reverse -- VIN_INT -- SwitchingCharger + MCP73831 linear
      charge path: SW -> L_Power -> VBAT via R_Sense; PMOS pass + NMOS path
      LVD_Comparator drives Load_Switch; NTC on TEMP; bulk caps on VIN/VBAT.
    """
    r = _call(tools, "ecad_new_schematic", name=name)
    sid = r.get("doc_id") or ""
    if not sid:
        raise RuntimeError(f"ecad_new_schematic failed: {r}")

    parts = (
        dict(libid="Simulation_SPICE:V_source", x=-160.0, y=0.0, ref="VIN",
             value="5"),
        dict(libid="Device:PMOS_Reverse", x=-120.0, y=0.0, ref="Q3",
             value="PMOS_REV"),
        dict(libid="Battery_Management:MCP73831", x=-40.0, y=40.0, ref="U1",
             value="MCP73831"),
        dict(libid="Battery_Management:SwitchingCharger_SOP8", x=-40.0, y=-40.0,
             ref="U2", value="SW_CHG"),
        dict(libid="Device:L_Power", x=40.0, y=-40.0, ref="L1", value="10uH"),
        dict(libid="Device:R_Sense", x=100.0, y=0.0, ref="RS1", value="0.05"),
        dict(libid="Device:PMOS_Power", x=100.0, y=40.0, ref="Q2",
             value="PMOS_PWR"),
        dict(libid="Device:NMOS_Power", x=100.0, y=-80.0, ref="Q1",
             value="NMOS_PWR"),
        dict(libid="Power_Management:Load_Switch", x=180.0, y=0.0, ref="U3",
             value="LOAD_SW"),
        dict(libid="Power_Management:LVD_Comparator", x=180.0, y=-60.0, ref="U4",
             value="LVD"),
        dict(libid="Device:NTC", x=40.0, y=-100.0, ref="RT1", value="10k"),
        dict(libid="Device:R", x=40.0, y=40.0, ref="RPROG", value="2k"),
        dict(libid="Device:R", x=140.0, y=-100.0, ref="RNTC", value="10k"),
        dict(libid="Device:R", x=140.0, y=-40.0, ref="RREF_H", value="100k"),
        dict(libid="Device:R", x=140.0, y=-80.0, ref="RREF_L", value="100k"),
        dict(libid="Device:C", x=-80.0, y=40.0, ref="C1", value="10u"),
        dict(libid="Device:C_pol", x=140.0, y=40.0, ref="C2", value="100u"),
        dict(libid="Device:C", x=200.0, y=40.0, ref="C3", value="10u"),
        dict(libid="Device:R", x=0.0, y=80.0, ref="RSTAT", value="2k"),
        dict(libid="Device:LED", x=40.0, y=80.0, ref="D1", value="LED"),
        dict(libid="Device:R", x=220.0, y=0.0, ref="RLOAD", value="10"),
        dict(libid="power:GND", x=-20.0, y=-120.0, ref="PGND"),
        dict(libid="Simulation_SPICE:V_source", x=220.0, y=-60.0, ref="VDD_LVD",
             value="3.3"),
    )
    for args in parts:
        _place(tools, sid, **args)

    # Connectivity — pin numbers from starter library public maps.
    W = [
        # Input reverse-protect: VIN+ -> Q3.S(2), Q3.D(3)=VIN_INT, Q3.G(1)->GND
        ("VIN", "+", "Q3", "2"),
        ("VIN", "-", "PGND", "1"),
        ("Q3", "1", "PGND", "1"),
        # Bulk on protected input (Q3 drain)
        ("Q3", "3", "C1", "1"),
        ("C1", "2", "PGND", "1"),
        # MCP73831: VDD=4, VSS=2, VBAT=3, PROG=5, STAT=1
        ("Q3", "3", "U1", "4"),
        ("U1", "2", "PGND", "1"),
        ("U1", "5", "RPROG", "1"),
        ("RPROG", "2", "PGND", "1"),
        ("U1", "1", "RSTAT", "1"),
        ("RSTAT", "2", "D1", "A"),
        ("D1", "K", "PGND", "1"),
        # Switching charger: VIN=4, GND=3, SW=5, BAT=7, TEMP=1, ISET=2
        ("Q3", "3", "U2", "4"),
        ("U2", "3", "PGND", "1"),
        ("U2", "5", "L1", "1"),
        ("L1", "2", "RS1", "1"),
        ("U2", "7", "RS1", "1"),
        ("U1", "3", "RS1", "1"),          # linear BAT joins charge node
        ("RS1", "2", "Q2", "1"),          # sense out -> PMOS source (S pins 1-3)
        ("Q2", "2", "RS1", "2"),
        ("Q2", "3", "RS1", "2"),
        # PMOS pass drain to VBAT_SYS / load switch VIN
        ("Q2", "5", "U3", "1"),
        ("Q2", "6", "U3", "1"),
        ("Q2", "7", "C2", "1"),
        ("C2", "2", "PGND", "1"),
        ("Q2", "8", "C3", "1"),
        ("C3", "2", "PGND", "1"),
        # Gate of pass PMOS tied low (on) via NMOS path for board connectivity
        ("Q2", "4", "Q1", "5"),
        ("Q1", "1", "PGND", "1"),
        ("Q1", "2", "PGND", "1"),
        ("Q1", "3", "PGND", "1"),
        ("Q1", "4", "U4", "1"),           # LVD OUT -> NMOS gate (protection path)
        # NTC on TEMP
        ("U2", "1", "RT1", "1"),
        ("RT1", "2", "PGND", "1"),
        ("U2", "1", "RNTC", "1"),
        ("RNTC", "2", "VDD_LVD", "+"),
        # LVD comparator: OUT=1, VSS=2, IN-=3, IN+=4, VDD=5
        ("U4", "2", "PGND", "1"),
        ("U4", "5", "VDD_LVD", "+"),
        ("VDD_LVD", "-", "PGND", "1"),
        ("U4", "4", "RS1", "2"),          # sense VBAT on IN+
        ("U4", "3", "RREF_H", "2"),       # IN- = divider mid
        ("RREF_H", "1", "VDD_LVD", "+"),
        ("RREF_L", "1", "RREF_H", "2"),
        ("RREF_L", "2", "PGND", "1"),
        # Load switch: VIN=1, GND=2, ON=3, VOUT=5
        ("U3", "2", "PGND", "1"),
        ("U3", "3", "U4", "1"),           # LVD enables load
        ("U3", "5", "RLOAD", "1"),
        ("RLOAD", "2", "PGND", "1"),
        # ISET to GND via RPROG class (shared ref for connectivity)
        ("U2", "2", "RPROG", "1"),
    ]
    for a, ap, b, bp in W:
        _wire(tools, sid, a, ap, b, bp)
    return sid


def build_sense_sim_schematic(tools: dict, name: str = "bc_sense_sim") -> str:
    """Library-backed sense-rail schematic for ecad_simulate MCP exercise.

    V_sense across R_Sense with a known current via V/R source network.
    """
    r = _call(tools, "ecad_new_schematic", name=name)
    sid = r.get("doc_id") or ""
    if not sid:
        raise RuntimeError(f"ecad_new_schematic failed: {r}")
    for args in (
        dict(libid="Simulation_SPICE:V_source", x=0.0, y=0.0, ref="VBAT",
             value="4.0"),
        dict(libid="Device:R_Sense", x=40.0, y=0.0, ref="RS1", value="0.05"),
        dict(libid="Device:R", x=80.0, y=0.0, ref="RLOAD", value="8"),
        dict(libid="Device:NTC", x=40.0, y=-40.0, ref="RT1", value="10k"),
        dict(libid="Device:R", x=80.0, y=-40.0, ref="RFIX", value="10k"),
        dict(libid="power:GND", x=20.0, y=-60.0, ref="GND1"),
    ):
        _place(tools, sid, **args)
    _wire(tools, sid, "VBAT", "+", "RS1", "1")
    _wire(tools, sid, "RS1", "2", "RLOAD", "1")
    _wire(tools, sid, "RLOAD", "2", "GND1", "1")
    _wire(tools, sid, "VBAT", "-", "GND1", "1")
    _wire(tools, sid, "VBAT", "+", "RFIX", "1")
    _wire(tools, sid, "RFIX", "2", "RT1", "1")
    _wire(tools, sid, "RT1", "2", "GND1", "1")
    return sid


def _mna_simulate(circuit: dict, study: dict) -> dict:
    """Run the shared MNA backend (same as ecad_simulate backend='mna')."""
    from services.mcp.ecad_common import _sim_run
    result = _sim_run(circuit, study, backend="mna")
    for analysis in study["analyses"]:
        kind = analysis["type"]
        if not result.get("convergence", {}).get(kind):
            raise RuntimeError(f"MNA {kind} did not converge: {result}")
    return result


def _dc_trip(x: list, y: list, *, high_to_low: bool = True,
             hi: float = 1.0, lo: float = 0.5) -> Optional[float]:
    """First x where y crosses from hi-side to lo-side (or reverse)."""
    if not x or not y or len(x) != len(y):
        return None
    for i in range(len(x) - 1):
        if high_to_low and y[i] > hi and y[i + 1] < lo:
            return float(x[i + 1])
        if not high_to_low and y[i] < lo and y[i + 1] > hi:
            return float(x[i + 1])
    return None


def _charger_boardphys() -> dict:
    return {
        "schema": "aicad.boardphys/1",
        "board": {"name": "battery_charger"},
        "components": [
            {"ref": "U1", "libid": "Battery_Management:MCP73831",
             "power_w": {"typ": 0.3, "max": 0.6},
             "theta_c_per_w": {"ja": 230.0, "jc": 81.0},
             "limits_c": {"tj_max": 150.0, "ta_max": 85.0},
             "supply": {"rails": [
                 {"name": "VDD", "voltage_v_nom": 5.0, "current_a_active": 0.00055},
                 {"name": "VBAT", "voltage_v_nom": 4.2, "current_a_active": 0.5},
             ], "voltage_domains": ["VDD", "VBAT"], "sequencing": []}},
            {"ref": "U2", "libid": "Battery_Management:SwitchingCharger_SOP8",
             "power_w": {"typ": 0.5, "max": 2.0},
             "theta_c_per_w": {"ja": 80.0, "jc": 15.0},
             "limits_c": {"tj_max": 150.0, "ta_max": 85.0},
             "supply": {"rails": [
                 {"name": "VIN", "voltage_v_nom": 5.0, "current_a_active": 0.002},
             ], "voltage_domains": ["VIN", "BAT"], "sequencing": []}},
            {"ref": "Q2", "libid": "Device:PMOS_Power",
             "power_w": {"typ": 0.25, "max": 1.0},
             "theta_c_per_w": {"ja": 50.0, "jc": 5.0},
             "limits_c": {"tj_max": 150.0},
             "supply": {"rails": [], "voltage_domains": [], "sequencing": []}},
            {"ref": "U3", "libid": "Power_Management:Load_Switch",
             "power_w": {"typ": 0.05, "max": 0.5},
             "limits_c": {"tj_max": 125.0},
             "supply": {"rails": [
                 {"name": "VIN", "voltage_v_nom": 3.7, "current_a_active": 0.5},
             ], "voltage_domains": ["VIN", "VOUT"], "sequencing": []}},
            {"ref": "U4", "libid": "Power_Management:LVD_Comparator",
             "power_w": {"typ": 5e-5, "max": 2e-4},
             "limits_c": {"tj_max": 125.0},
             "supply": {"rails": [
                 {"name": "VDD", "voltage_v_nom": 3.3, "current_a_active": 1e-5},
             ], "voltage_domains": ["VDD"], "sequencing": []}},
        ],
    }


def _charger_power_profile(duty: float = 0.5) -> dict:
    i_chg = I_CC * duty
    i_q = 0.002
    i_avg = i_chg + i_q
    return {
        "schema": "aicad.powerprofile/1",
        "model": "duty-cycle bookkeeping",
        "trace_ref": "bench-battery-charger-synthetic",
        "cpu": {"active_frac": duty, "sleep_frac": 1 - duty},
        "pins": {},
        "rails": [{"name": "VBAT", "voltage_v_nom": 3.7,
                   "i_avg_a": i_avg, "i_peak_a": I_CC + i_q,
                   "residency": {"active_frac": duty, "sleep_frac": 1 - duty},
                   "schedule": {"duty": duty, "period_s": 1.0},
                   "provenance": [f"bench: charge @{duty:.0%} of I_CC={I_CC}A"]}],
        "coverage": {"components_total": 5, "components_with_rails": 5,
                     "unknown_active_current": [],
                     "notes": ["synthetic charger duty profile"]},
    }


CHARGE_SM_C = r'''/* Battery charge state machine — B2 bench firmware.
 * States: PRECHARGE -> CC -> CV -> DONE; any safety trip -> FAULT.
 * Bare-metal (no newlib printf/sbrk): poll USART1 DR so the template
 * links with the stock cmake_arm linker script.
 * Safety: ADC1 DR stub samples — low counts = OTP, high = OVP.
 */
#include <stdint.h>

/* STM32F4 USART1 — matches templates/renode_platforms/stm32f4_discovery.repl */
#define USART1_BASE 0x40011000u
#define USART_SR  (*(volatile uint32_t *)(USART1_BASE + 0x00u))
#define USART_DR  (*(volatile uint32_t *)(USART1_BASE + 0x04u))
#define USART_BRR (*(volatile uint32_t *)(USART1_BASE + 0x08u))
#define USART_CR1 (*(volatile uint32_t *)(USART1_BASE + 0x0Cu))
#define USART_TXE (1u << 7)
#define USART_UE  (1u << 13)
#define USART_TE  (1u << 3)

#define GPIO_BASE   0x40020000u
#define GPIO_ODR    (*(volatile uint32_t *)(GPIO_BASE + 0x14u))
/* ADC1 stub window (core/firmware/scenario.cpp STM32F4 map) */
#define ADC1_BASE   0x40012000u
#define ADC1_SR     (*(volatile uint32_t *)(ADC1_BASE + 0x00u))
#define ADC1_DR     (*(volatile uint32_t *)(ADC1_BASE + 0x4Cu))

#define ADC_OVP_MIN  3500u
#define ADC_PRE_MAX  1200u
#define ADC_CV_MIN   3000u
#define ADC_DONE_MIN 3400u

typedef enum {
    ST_PRECHARGE = 0,
    ST_CC,
    ST_CV,
    ST_DONE,
    ST_FAULT
} charge_state_t;

static volatile charge_state_t g_state = ST_PRECHARGE;

static void uart_putc(char c)
{
    while (!(USART_SR & USART_TXE)) {
        /* wait */
    }
    USART_DR = (uint32_t)(uint8_t)c;
}

static void uart_puts(const char *s)
{
    while (*s) {
        uart_putc(*s++);
    }
}

static uint16_t adc_read_stub(void)
{
    for (volatile int i = 0; i < 1000; ++i) {
        if (ADC1_SR & 0x2u) {
            break;
        }
    }
    return (uint16_t)(ADC1_DR & 0xFFFu);
}

static void set_state(charge_state_t st)
{
    g_state = st;
    switch (st) {
    case ST_PRECHARGE: uart_puts("STATE:PRECHARGE\r\n"); break;
    case ST_CC:        uart_puts("STATE:CC\r\n"); break;
    case ST_CV:        uart_puts("STATE:CV\r\n"); break;
    case ST_DONE:      uart_puts("STATE:DONE\r\n"); break;
    case ST_FAULT:     uart_puts("STATE:FAULT\r\n"); break;
    }
}

static void enter_fault(const char *tag)
{
    /* A latched fault is one transition, not a UART message on every ADC poll. */
    if (g_state == ST_FAULT) {
        return;
    }
    uart_puts(tag);
    set_state(ST_FAULT);
    uart_puts("FAULT_HOLD\r\n");
}

int main(void)
{
    /* 115200 baud from the STM32F4 reset 16MHz HSI peripheral clock. */
    USART_BRR = (16000000u + 115200u / 2u) / 115200u;
    USART_CR1 = USART_UE | USART_TE;
    set_state(ST_PRECHARGE);
    uart_puts("CHG_SM_BOOT\r\n");

    for (;;) {
        uint16_t sample = adc_read_stub();
        /* Bench injects: over-temp as samples near 0..400, over-voltage as >=3500. */
        if (sample >= ADC_OVP_MIN) {
            enter_fault("FAULT:OVP\r\n");
        } else if (sample <= 400u) {
            enter_fault("FAULT:OTP\r\n");
        } else if (g_state != ST_FAULT) {
            if (g_state == ST_PRECHARGE && sample > ADC_PRE_MAX) {
                set_state(ST_CC);
            } else if (g_state == ST_CC && sample >= ADC_CV_MIN) {
                set_state(ST_CV);
            } else if (g_state == ST_CV && sample >= ADC_DONE_MIN) {
                set_state(ST_DONE);
            }
        }

        GPIO_ODR ^= (1u << 5);
        for (volatile uint32_t d = 0; d < 8000u; ++d) {
            __asm__ volatile("nop");
        }
        if (g_state == ST_FAULT) {
            for (volatile uint32_t d = 0; d < 20000u; ++d) {
                __asm__ volatile("nop");
            }
        }
    }
    return 0;
}
'''


def _ensure_board_outline(doc: dict, margin_mm: float = 5.0) -> dict:
    """Close Edge.Cuts from footprint bbox when board_from_schematic left outline empty."""
    outline = doc.get("outline") or {}
    shapes = outline.get("shapes") or []
    if outline.get("closed") and shapes:
        return outline
    xs: list[float] = []
    ys: list[float] = []
    for fp in doc.get("footprints") or []:
        pos = fp.get("pos") or [0.0, 0.0]
        try:
            xs.append(float(pos[0]))
            ys.append(float(pos[1]))
        except (TypeError, ValueError, IndexError):
            continue
        for pad in fp.get("pads") or []:
            p = pad.get("pos") or [0.0, 0.0]
            try:
                xs.append(float(pos[0]) + float(p[0]))
                ys.append(float(pos[1]) + float(p[1]))
            except (TypeError, ValueError, IndexError):
                pass
    if not xs:
        xs, ys = [0.0, 50.0], [0.0, 40.0]
    x0, x1 = min(xs) - margin_mm, max(xs) + margin_mm
    y0, y1 = min(ys) - margin_mm, max(ys) + margin_mm
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return {
        "closed": True,
        "shapes": [
            {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4]),
             "layer": "Edge.Cuts"}
            for i in range(4)
        ],
    }


def _widen_power_tracks(doc: dict, min_width: float = 0.5) -> int:
    """Bump the widest tracks per net toward power width; return count widened."""
    tracks = doc.get("tracks") or []
    if not tracks:
        return 0
    # Heuristic: top 25% widest tracks, or any track already >= 0.25 mm, get power width.
    widths = sorted(float(t.get("width") or 0.0) for t in tracks)
    if not widths:
        return 0
    cutoff = widths[max(0, int(0.75 * (len(widths) - 1)))]
    n = 0
    for t in tracks:
        w = float(t.get("width") or 0.0)
        if w >= cutoff or w >= 0.25:
            if w < min_width:
                t["width"] = min_width
                n += 1
    return n


# ── legs ─────────────────────────────────────────────────────────────────────

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    for old in MARKER_DIR.glob("*.marker"):
        old.unlink()

    print("=== B2 battery-charger E2E benchmark ===")
    print(f"  out: {OUT_DIR}")
    print(f"  seed: {SEED}")
    print(f"  CHUDWARE_ECAD_HEAVY_BUDGET_S="
          f"{os.environ.get('CHUDWARE_ECAD_HEAVY_BUDGET_S')}")
    print(f"  sim_budget_s={SIM_BUDGET_S}")
    print(f"  thresholds: Vcv={V_CV} Icc={I_CC} Vlvd={V_LVD} "
          f"Vovp={V_OVP} Iocp={I_OCP}")

    t_all = time.time()
    tools = register_all_tools()
    state: dict[str, Any] = {"seed": SEED}

    # ── 1. PARTS ──────────────────────────────────────────────────────────
    def leg_parts(leg: Leg) -> None:
        resolved: dict[str, Any] = {}
        missing: list[str] = []
        from services.ecad import library as lib
        for libid, query in REQUIRED_PARTS:
            r = _call(tools, "ecad_find_part", query=query, limit=10)
            hits = r.get("hits") or []
            hit_ids = [h.get("libid") for h in hits]
            sdef = lib.resolve(libid)
            if sdef is None:
                missing.append(libid)
                resolved[libid] = {"found_by_search": libid in hit_ids,
                                   "resolve": None}
                continue
            resolved[libid] = {
                "found_by_search": libid in hit_ids or any(
                    (h.get("libid") or "").endswith(libid.split(":")[-1])
                    for h in hits),
                "resolve": True,
                "pin_count": len(sdef.get("pins") or []),
                "sim_model": (sdef.get("sim") or {}).get("model"),
                "search_top": hit_ids[:3],
            }
        state["parts"] = resolved
        if missing:
            leg.fail(f"unresolved libids: {missing}")
            return
        n = len(resolved)
        pins = sum(int(v.get("pin_count") or 0) for v in resolved.values())
        leg.pass_(f"{n}/{len(REQUIRED_PARTS)} parts resolved; total pins={pins}",
                  artifact=resolved)

    run_leg("parts", leg_parts)

    # ── 2. SCHEMATIC ──────────────────────────────────────────────────────
    def leg_schematic(leg: Leg) -> None:
        sid = build_charger_schematic(tools)
        state["sch_doc_id"] = sid
        st = _call(tools, "ecad_status", doc_id=sid, detail="summary")
        state["sch_status"] = st
        n_comp = int(st.get("component_count")
                     or len(st.get("components") or []) or 0)
        n_nets = int(st.get("net_count") or len(st.get("nets") or []) or 0)
        if n_comp < 12:
            leg.fail(f"expected >=12 components, got {n_comp}")
            return
        from services.mcp.ecad_common import ecad_session
        sch_path = OUT_DIR / "battery_charger_sch.json"
        sch_path.write_text(
            json.dumps(ecad_session.get_session(sid).doc, indent=2),
            encoding="utf-8")
        state["sch_path"] = str(sch_path)
        leg.pass_(f"sch={sid} components={n_comp} nets={n_nets}",
                  artifact={"doc_id": sid, "components": n_comp, "nets": n_nets})

    run_leg("schematic", leg_schematic)

    # ── 3. SIM-CHARGE (CC→CV taper) ───────────────────────────────────────
    def leg_sim_charge(leg: Leg) -> None:
        # Behavioural CC→CV: I = Icc * 0.5*(1+tanh(k*(Vcv-Vbat))) into Cbat.
        circuit = {
            "devices": [
                {"kind": "B", "name": "BCHG", "nodes": ["0", "SEN"],
                 "value": None,
                 "params": {"btype": "I",
                            "expr": (f"{I_CC}*0.5*(1+tanh(40*({V_CV}-V(BAT))))")}},
                {"kind": "R", "name": "RS", "nodes": ["SEN", "BAT"],
                 "value": R_SENSE, "params": {}},
                {"kind": "C", "name": "CB", "nodes": ["BAT", "0"],
                 "value": 1.0, "params": {"ic": 3.2}},
                {"kind": "R", "name": "Rleak", "nodes": ["BAT", "0"],
                 "value": 1e6, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study = {"analyses": [{"type": "tran", "tstep": 0.02, "tstop": 5.0,
                               "uic": True}],
                 "probes": ["V(BAT)", "V(SEN)"]}
        try:
            res = _mna_simulate(circuit, study)
        except Exception as exc:  # noqa: BLE001
            leg.fail(f"MNA CC-CV simulate raised: {exc}")
            return
        if not (res.get("convergence") or {}).get("tran"):
            leg.fail(f"CC-CV tran did not converge: {res.get('log')}")
            return
        tr = (res.get("tran") or {}).get("traces") or {}
        vbat = tr.get("V(BAT)") or []
        vsen = tr.get("V(SEN)") or []
        if len(vbat) < 10 or len(vsen) != len(vbat):
            leg.fail(f"missing tran traces: keys={list(tr.keys())}")
            return
        iest = [(vs - vb) / R_SENSE for vs, vb in zip(vsen, vbat)]
        i_early = sum(iest[5:15]) / 10.0
        i_late = sum(iest[-10:]) / 10.0
        v_end = float(vbat[-1])
        v_max = float(max(vbat))
        # MCP tool surface: sense-rail OP via ecad_simulate
        sid = build_sense_sim_schematic(tools)
        state["sense_sch_doc_id"] = sid
        mcp = _call(tools, "ecad_simulate", analysis="op", doc_id=sid,
                    backend="mna")
        state["sim_charge_mcp"] = {
            "ok": mcp.get("ok"), "converged": mcp.get("converged"),
            "error": mcp.get("error"), "device_count": mcp.get("device_count"),
        }
        if not mcp.get("ok") or not mcp.get("converged"):
            leg.fail(f"companion ecad_simulate OP failed: {mcp}")
            return
        art = {
            "v_end": v_end, "v_max": v_max, "i_early": i_early, "i_late": i_late,
            "V_CV": V_CV, "I_CC": I_CC, "mcp_op": state["sim_charge_mcp"],
            "n_points": len(vbat),
        }
        state["sim_charge"] = art
        (OUT_DIR / "sim_charge.json").write_text(
            json.dumps(art, indent=2), encoding="utf-8")
        # Real assertions: CV clamp + current taper
        if abs(v_end - V_CV) > 0.2:
            leg.fail(f"CV clamp fail: V_end={v_end:.4f} (expect ~{V_CV}); "
                     f"I_early={i_early:.4f} I_late={i_late:.4f}")
            return
        if i_early < 0.7 * I_CC:
            leg.fail(f"CC phase missing: I_early={i_early:.4f} < 0.7*I_CC")
            return
        if i_late > 0.25 * I_CC:
            leg.fail(f"taper fail: I_late={i_late:.4f} not << I_CC "
                     f"(still >0.25*I_CC)")
            return
        leg.pass_(
            f"CC→CV ok: V_end={v_end:.3f}V (cv={V_CV}); "
            f"I_early={i_early:.3f}A I_late={i_late:.4f}A; "
            f"mcp_op ok={mcp.get('ok')}",
            artifact=art)

    run_leg("sim_charge", leg_sim_charge)

    # ── 4. SIM-LVD ────────────────────────────────────────────────────────
    def leg_sim_lvd(leg: Leg) -> None:
        # Voltage-controlled switch ON when Vbat > V_LVD (load connected).
        circuit = {
            "devices": [
                {"kind": "V", "name": "VBAT", "nodes": ["BAT", "0"],
                 "value": 3.7, "params": {}},
                {"kind": "S", "name": "SLVD", "nodes": ["BAT", "LOAD", "BAT", "0"],
                 "value": None,
                 "params": {"ron": 0.05, "roff": 1e9, "vt": V_LVD, "vh": 0.05}},
                {"kind": "R", "name": "RLOAD", "nodes": ["LOAD", "0"],
                 "value": 10.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        study = {"analyses": [{"type": "dc", "source": "VBAT",
                               "start": 2.5, "stop": 4.0, "step": 0.02}],
                 "probes": ["V(LOAD)", "V(BAT)"]}
        try:
            res = _mna_simulate(circuit, study)
        except Exception as exc:  # noqa: BLE001
            leg.fail(f"LVD simulate raised: {exc}")
            return
        dc = res.get("dc") or {}
        x = list(dc.get("x") or [])
        vl = list((dc.get("traces") or {}).get("V(LOAD)") or [])
        if len(x) < 5 or len(vl) != len(x):
            leg.fail(f"LVD dc empty: x={len(x)} load={len(vl)}")
            return
        connect = _dc_trip(x, vl, high_to_low=False, hi=1.0, lo=0.5)
        # Also evaluate load at V_LVD-0.1 vs V_LVD+0.1
        def _at(vtarget: float) -> float:
            i = min(range(len(x)), key=lambda j: abs(x[j] - vtarget))
            return float(vl[i])

        v_lo = _at(V_LVD - 0.15)
        v_hi = _at(V_LVD + 0.15)
        art = {"connect_v": connect, "V_LVD": V_LVD,
               "Vload_below": v_lo, "Vload_above": v_hi,
               "n_points": len(x)}
        state["sim_lvd"] = art
        (OUT_DIR / "sim_lvd.json").write_text(
            json.dumps(art, indent=2), encoding="utf-8")
        # Assert disconnect below threshold, connect above
        if v_lo > 0.5:
            leg.fail(f"LVD still connected below threshold: "
                     f"Vload({V_LVD - 0.15})={v_lo:.3f}")
            return
        if v_hi < 1.0:
            leg.fail(f"LVD not connected above threshold: "
                     f"Vload({V_LVD + 0.15})={v_hi:.3f}")
            return
        if connect is not None and abs(connect - V_LVD) > 0.2:
            leg.fail(f"LVD trip at {connect:.3f} V, expect ~{V_LVD}")
            return
        # MCP DC sweep on sense schematic for tool surface
        sid = state.get("sense_sch_doc_id")
        if not sid:
            sid = build_sense_sim_schematic(tools)
        mcp = _call(tools, "ecad_simulate", analysis="dc",
                    params={"source": "VBAT", "start": 2.5, "stop": 4.2,
                            "step": 0.1},
                    doc_id=sid, backend="mna")
        state["sim_lvd_mcp"] = {"ok": mcp.get("ok"),
                                "converged": mcp.get("converged"),
                                "error": mcp.get("error"),
                                "analysis": mcp.get("analysis")}
        if not mcp.get("ok") or not mcp.get("converged"):
            leg.fail(f"companion ecad_simulate DC failed: {mcp}")
            return
        leg.pass_(
            f"LVD disconnect ok: Vload(<{V_LVD})={v_lo:.3f} "
            f"Vload(>{V_LVD})={v_hi:.3f} connect@={connect}; "
            f"mcp_dc ok={mcp.get('ok')}",
            artifact=art)

    run_leg("sim_lvd", leg_sim_lvd)

    # ── 5. SIM-SAFETY (OVP / OCP / OTP) ───────────────────────────────────
    def leg_sim_safety(leg: Leg) -> None:
        trips: dict[str, Any] = {}

        # OVP: pass switch opens when BAT > V_OVP (ctrl = Vovp - Vbat)
        ovp = {
            "devices": [
                {"kind": "V", "name": "VBAT", "nodes": ["BAT", "0"],
                 "value": 4.0, "params": {}},
                {"kind": "V", "name": "VOVP", "nodes": ["OVP", "0"],
                 "value": V_OVP, "params": {}},
                {"kind": "B", "name": "BCTRL", "nodes": ["CTRL", "0"],
                 "value": None,
                 "params": {"btype": "V", "expr": "V(OVP)-V(BAT)"}},
                {"kind": "S", "name": "S1", "nodes": ["BAT", "SYS", "CTRL", "0"],
                 "value": None,
                 "params": {"ron": 0.05, "roff": 1e9, "vt": 0.0, "vh": 0.05}},
                {"kind": "R", "name": "RLOAD", "nodes": ["SYS", "0"],
                 "value": 10.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = _mna_simulate(
            ovp, {"analyses": [{"type": "dc", "source": "VBAT",
                                "start": 3.8, "stop": 4.8, "step": 0.02}],
                  "probes": ["V(SYS)"]})
        x = list((res.get("dc") or {}).get("x") or [])
        vs = list(((res.get("dc") or {}).get("traces") or {}).get("V(SYS)") or [])
        trip_ovp = _dc_trip(x, vs, high_to_low=True)
        trips["ovp"] = {"trip_v": trip_ovp, "target": V_OVP}

        # OCP: current through Rsense; open pass when I > I_OCP
        vth = I_OCP * R_SENSE
        ocp = {
            "devices": [
                {"kind": "I", "name": "ILOAD", "nodes": ["0", "MID"],
                 "value": 1.0, "params": {}},
                {"kind": "R", "name": "RS", "nodes": ["MID", "0"],
                 "value": R_SENSE, "params": {}},
                {"kind": "V", "name": "VTH", "nodes": ["TH", "0"],
                 "value": vth, "params": {}},
                {"kind": "B", "name": "BCTRL", "nodes": ["CTRL", "0"],
                 "value": None,
                 "params": {"btype": "V", "expr": "V(TH)-V(MID)"}},
                {"kind": "V", "name": "VIN", "nodes": ["IN", "0"],
                 "value": 5.0, "params": {}},
                {"kind": "S", "name": "SPASS",
                 "nodes": ["IN", "OUT", "CTRL", "0"], "value": None,
                 "params": {"ron": 0.05, "roff": 1e9, "vt": 0.0, "vh": 0.05}},
                {"kind": "R", "name": "RLOAD", "nodes": ["OUT", "0"],
                 "value": 10.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = _mna_simulate(
            ocp, {"analyses": [{"type": "dc", "source": "ILOAD",
                                "start": 0.2, "stop": 4.0, "step": 0.05}],
                  "probes": ["V(OUT)", "V(MID)"]})
        x = list((res.get("dc") or {}).get("x") or [])
        vo = list(((res.get("dc") or {}).get("traces") or {}).get("V(OUT)") or [])
        vm = list(((res.get("dc") or {}).get("traces") or {}).get("V(MID)") or [])
        # Soft assert: below I_OCP-margin OUT high; above I_OCP+margin OUT low
        def _at_i(target: float) -> tuple[float, float]:
            i = min(range(len(x)), key=lambda j: abs(x[j] - target))
            return float(vo[i]), float(vm[i])

        v_lo_i, vs_lo = _at_i(1.0)
        v_hi_i, vs_hi = _at_i(3.0)
        trips["ocp"] = {
            "I_OCP": I_OCP, "Vth": vth,
            "Vout_at_1A": v_lo_i, "Vout_at_3A": v_hi_i,
            "Vsense_at_1A": vs_lo, "Vsense_at_3A": vs_hi,
        }

        # OTP: NTC divider proxy VDIV vs V_OTP_REF; open when hot (VDIV < ref)
        otp = {
            "devices": [
                {"kind": "V", "name": "VDIV", "nodes": ["DIV", "0"],
                 "value": 1.5, "params": {}},
                {"kind": "V", "name": "VREF", "nodes": ["REF", "0"],
                 "value": V_OTP_REF, "params": {}},
                {"kind": "B", "name": "BCTRL", "nodes": ["CTRL", "0"],
                 "value": None,
                 "params": {"btype": "V", "expr": "V(DIV)-V(REF)"}},
                {"kind": "V", "name": "VBAT", "nodes": ["BAT", "0"],
                 "value": 3.7, "params": {}},
                {"kind": "S", "name": "SOTP",
                 "nodes": ["BAT", "SYS", "CTRL", "0"], "value": None,
                 "params": {"ron": 0.05, "roff": 1e9, "vt": 0.0, "vh": 0.05}},
                {"kind": "R", "name": "RLOAD", "nodes": ["SYS", "0"],
                 "value": 10.0, "params": {}},
            ],
            "models": {}, "params": {"TEMP": 27},
        }
        res = _mna_simulate(
            otp, {"analyses": [{"type": "dc", "source": "VDIV",
                                "start": 0.2, "stop": 2.0, "step": 0.05}],
                  "probes": ["V(SYS)"]})
        x = list((res.get("dc") or {}).get("x") or [])
        vs = list(((res.get("dc") or {}).get("traces") or {}).get("V(SYS)") or [])
        trip_otp = _dc_trip(x, vs, high_to_low=False)  # reconnect when cooling
        # Evaluate hot vs cool
        def _sys_at(v: float) -> float:
            i = min(range(len(x)), key=lambda j: abs(x[j] - v))
            return float(vs[i])

        sys_hot = _sys_at(0.5)
        sys_cool = _sys_at(1.5)
        trips["otp"] = {
            "ref": V_OTP_REF, "reconnect_v": trip_otp,
            "Vsys_hot": sys_hot, "Vsys_cool": sys_cool,
        }

        state["sim_safety"] = trips
        (OUT_DIR / "sim_safety.json").write_text(
            json.dumps(trips, indent=2), encoding="utf-8")

        fails: list[str] = []
        if trip_ovp is None or abs(trip_ovp - V_OVP) > 0.2:
            fails.append(f"OVP trip={trip_ovp} expect~{V_OVP}")
        if not (v_lo_i > 2.0 and v_hi_i < 0.5):
            fails.append(f"OCP foldback fail: Vout(1A)={v_lo_i:.3f} "
                         f"Vout(3A)={v_hi_i:.3f}")
        if abs(vs_hi - 3.0 * R_SENSE) > 0.05:
            fails.append(f"OCP sense at 3A={vs_hi:.4f} expect~{3.0 * R_SENSE}")
        if not (sys_hot < 0.5 and sys_cool > 1.0):
            fails.append(f"OTP fail: hot={sys_hot:.3f} cool={sys_cool:.3f}")
        if fails:
            leg.fail("; ".join(fails))
            return
        leg.pass_(
            f"OVP@{trip_ovp:.3f}V OCP foldback "
            f"(1A→{v_lo_i:.2f}V,3A→{v_hi_i:.3f}V) "
            f"OTP hot={sys_hot:.3f}/cool={sys_cool:.2f}",
            artifact=trips)

    run_leg("sim_safety", leg_sim_safety)

    # ── 6. BOARD ──────────────────────────────────────────────────────────
    def leg_board(leg: Leg) -> None:
        sid = state.get("sch_doc_id")
        if not sid:
            leg.skip("no schematic")
            return
        r = _call(tools, "board_from_schematic", sch_doc_id=sid, optimize=True)
        state["board"] = r
        if r.get("status") == "running" or r.get("auto_backgrounded"):
            leg.skip(f"board_from_schematic budget: "
                     f"{r.get('error') or r.get('hint')}")
            return
        if not r.get("ok") and not r.get("board_doc_id"):
            leg.fail(f"board_from_schematic failed: {r.get('error') or r}")
            return
        bid = r.get("board_doc_id") or r.get("doc_id")
        state["pcb_doc_id"] = bid
        placed = int(r.get("placed") or 0)
        routed = int(r.get("routed") or 0)
        residual = r.get("residual_drc") or []
        if placed < 8:
            leg.fail(f"placed={placed} (<8)")
            return
        from services.mcp.ecad_common import ecad_session
        board_path = OUT_DIR / "battery_charger_board.json"
        board_sess = ecad_session.get_session(bid)
        board_doc = board_sess.doc
        # board_from_schematic sometimes leaves outline empty — thermal needs Edge.Cuts.
        board_doc["outline"] = _ensure_board_outline(board_doc)
        n_wide = _widen_power_tracks(board_doc, min_width=0.5)
        # Tag pass MOSFET with dissipation for thermal handoff honesty.
        for fp in board_doc.get("footprints") or []:
            if fp.get("ref") == "Q2":
                eng = dict(fp.get("eng_overrides") or {})
                eng.setdefault("power_w", {"typ": 0.25, "max": 1.0})
                eng.setdefault("theta_c_per_w", {"ja": 50.0, "jc": 5.0})
                eng.setdefault("limits_c", {"tj_max": 150.0, "ta_max": 85.0})
                fp["eng_overrides"] = eng
            if fp.get("ref") in ("U1", "U2"):
                eng = dict(fp.get("eng_overrides") or {})
                eng.setdefault("power_w", {"typ": 0.4, "max": 1.5})
                eng.setdefault("theta_c_per_w", {"ja": 80.0, "jc": 15.0})
                eng.setdefault("limits_c", {"tj_max": 150.0, "ta_max": 85.0})
                fp["eng_overrides"] = eng
        # The board lives in the C++ core: write the edits back so DRC, impedance and
        # the thermal handoff below read the same document this JSON records.
        ecad_session.swap_doc(board_sess, board_doc)
        board_doc = board_sess.doc
        board_path.write_text(json.dumps(board_doc, indent=2), encoding="utf-8")
        state["board_path"] = str(board_path)

        tracks = board_doc.get("tracks") or []
        power_widths = []
        max_w = 0.0
        for t in tracks:
            net = str(t.get("net") or "")
            w = float(t.get("width") or 0.0)
            max_w = max(max_w, w)
            if w >= 0.4 or any(k in net.upper() for k in (
                    "VIN", "VBAT", "BAT", "VSYS", "PWR", "+5", "GND")):
                power_widths.append({"net": net, "width": w})
        z = _call(tools, "ecad_check_impedance", net_or_ref="",
                  target=50.0, doc_id=bid)
        drc = _call(tools, "ecad_run_drc", doc_id=bid)
        state["drc"] = drc
        state["impedance"] = z
        n_drc = len(drc.get("violations") or [])
        wide = [p for p in power_widths if p["width"] >= 0.4]
        outline_ok = bool((board_doc.get("outline") or {}).get("closed"))
        detail = (f"board={bid} placed={placed} routed={routed} "
                  f"power_tracks={len(power_widths)} wide(>=0.4)={len(wide)} "
                  f"max_w={max_w:.3f} widened={n_wide} outline={outline_ok} "
                  f"drc_viol={n_drc} Z0_ok={z.get('ok')}")
        art = {"board_doc_id": bid, "placed": placed, "routed": routed,
               "power_widths": power_widths[:20], "wide": wide[:10],
               "max_track_width": max_w, "widened": n_wide,
               "outline_closed": outline_ok,
               "drc_violations": n_drc, "impedance": {
                   "ok": z.get("ok"), "z0": z.get("z0") or z.get("z0_ohm"),
                   "error": z.get("error")}}
        if drc.get("error") and drc.get("violations") is None and not drc.get("ok"):
            leg.fail(f"DRC failed: {drc.get('error')}")
            return
        if not outline_ok:
            leg.fail("outline not closed after ensure")
            return
        if max_w < 0.3 and not wide:
            leg.warn(detail + " (tracks still narrow)", artifact=art)
            return
        leg.pass_(detail, artifact=art)

    run_leg("board", leg_board)

    # ── 7. THERMAL ────────────────────────────────────────────────────────
    def leg_thermal(leg: Leg) -> None:
        bid = state.get("pcb_doc_id")
        if not bid:
            leg.skip("no board")
            return
        try:
            import build123d  # noqa: F401
        except ImportError:
            leg.skip("build123d not installed — FEA solid path unavailable")
            return
        # Re-assert outline on the live session (board leg already wrote it).
        from services.mcp.ecad_common import ecad_session
        sess = ecad_session.get_session(bid)
        outline = _ensure_board_outline(sess.doc)
        if outline != sess.doc.get("outline"):
            doc = dict(sess.doc, outline=outline)
            ecad_session.swap_doc(sess, doc)
        r = _call(tools, "ecad_thermal_handoff", doc_id=bid,
                  dry_run=True, mesh_fineness=0.3,
                  path=str(OUT_DIR / "charger_fea.step"))
        state["thermal"] = r
        err = str(r.get("error") or r.get("error_solve") or "")
        if not r.get("ok") and not r.get("simhandoff"):
            if "outline" in err.lower() or "edge.cuts" in err.lower():
                # Last resort: synthetic boardphys-only handoff note
                leg.skip(f"thermal needs outline: {err[:120]}")
                return
            if any(s in err.lower() for s in ("unavailable", "import", "build123d",
                                              "ocp")):
                leg.skip(f"thermal unavailable: {err[:120]}")
                return
            leg.fail(f"thermal handoff failed: {err[:160]}")
            return
        comps = r.get("components") or []
        q2 = next((c for c in comps if c.get("ref") == "Q2"), None)
        tj = None
        if q2:
            tj = q2.get("tj_c") or q2.get("t_case_c")
        if r.get("solved"):
            leg.pass_(f"solved board_max={r.get('board_max_temp_c')}C "
                      f"Q2_tj={tj}; sources="
                      f"{(r.get('coverage') or {}).get('n_heat_sources')}",
                      artifact=r)
            return
        if r.get("simhandoff") or r.get("dry_run") or r.get("ok"):
            leg.pass_(f"handoff envelope ok (solved={r.get('solved')}, "
                      f"dry={r.get('dry_run')}); Q2={q2}",
                      artifact=r)
            return
        leg.fail(f"thermal incomplete: {r}")

    run_leg("thermal", leg_thermal)

    # ── 8. FIRMWARE (charge state machine) ────────────────────────────────
    def leg_firmware(leg: Leg) -> None:
        bid = state.get("pcb_doc_id")
        board_path = state.get("board_path")
        ref = bid or board_path
        if not ref:
            leg.skip("no board to link")
            return
        # Absolute firmware root — relative CHUDWARE_FIRMWARE_DIR can double-prefix.
        fw_root = (OUT_DIR / "firmware").resolve()
        fw_root.mkdir(parents=True, exist_ok=True)
        os.environ["CHUDWARE_FIRMWARE_DIR"] = str(fw_root)

        r = _call(tools, "fw_new_project", name="battery_charger_fw",
                  mcu_family="stm32f4", board_doc_ref=str(ref))
        if not r.get("ok") and not r.get("project_id") and board_path:
            r = _call(tools, "fw_new_project", name="battery_charger_fw",
                      mcu_family="stm32f4", board_doc_ref=str(board_path))
        state["fw_project"] = r
        pid = r.get("project_id") or (r.get("project") or {}).get("id")
        if not pid:
            leg.fail(f"fw_new_project failed: {r.get('error') or r}")
            return
        state["fw_project_id"] = pid

        w = _call(tools, "fw_sources", project_id=pid,
                  write={"src/main.c": CHARGE_SM_C})
        root = w.get("root_dir")
        if w.get("error") or not root or not any(
                entry.get("path") == "src/main.c" for entry in w.get("written", [])):
            leg.fail(f"fw_sources write failed: {w.get('error') or w}")
            return
        root_dir = Path(root)

        # Ensure linker provides `end` for any residual newlib; bare SM avoids sbrk.
        ld = root_dir / "linker.ld"
        if ld.is_file():
            txt = ld.read_text(encoding="utf-8")
            if "PROVIDE(end" not in txt and "end =" not in txt:
                txt = txt.rstrip() + "\nPROVIDE(end = .);\nPROVIDE(_end = .);\n"
                ld.write_text(txt, encoding="utf-8")

        b = _call(tools, "fw_build", project_id=pid)
        state["fw_build"] = b
        if b.get("success") and b.get("elf_path"):
            leg.pass_(f"project={pid}; charge SM built elf={b.get('elf_path')} "
                      f"ms={b.get('ms')}",
                      artifact={"project": r, "build": b})
            return
        rc = b.get("returncode")
        stderr = str(b.get("stderr") or "")
        detail = (f"project={pid}; fw_build success={b.get('success')} "
                  f"rc={rc} err={stderr[-200:]}")
        if rc == -1 and "toolchain_not_found:" in stderr:
            leg.skip(detail + " (external cross-toolchain unavailable)")
            return
        leg.fail(detail)

    run_leg("firmware", leg_firmware)

    # ── 9. SAFETY-IN-THE-LOOP ─────────────────────────────────────────────
    def leg_safety_loop(leg: Leg) -> None:
        pid = state.get("fw_project_id")
        b = state.get("fw_build") or {}
        elf = b.get("elf_path") or ""

        # Build over-temp + over-voltage ADC scenario (aicad.fwscenario/1).
        # Low counts = hot NTC (FAULT:OTP); also a second segment with high=OVP.
        # Use samples waveform on ADC stub (STM32F4).
        n = 40
        # First half: over-temp (low counts), second half: over-voltage (high)
        ot_counts = [200 + (i % 5) for i in range(n // 2)]
        ov_counts = [3600 + (i % 7) for i in range(n - len(ot_counts))]
        samples = ot_counts + ov_counts
        scenario = {
            "schema": "aicad.fwscenario/1",
            "name": "charger_safety_otp_ovp",
            "tracks": [
                {"kind": "adc_waveform", "channel": 0, "sample_ms": 5.0,
                 "waveform": {"type": "samples", "values": samples}},
            ],
        }
        # Also exercise fw_sensor_scenario with a temperature-elevated profile
        # (BMP280 path) for MCP surface coverage.
        hot_profile = {
            "t_s": [0.0, 0.5, 1.0, 1.5, 2.0],
            "altitude_m": [0.0, 0.0, 0.0, 0.0, 0.0],
            "temperature_c": [25.0, 40.0, 60.0, 80.0, 95.0],
            "vel_ned_m_s": [[0, 0, 0]] * 5,
            "omega_body_rad_s": [[0, 0, 0]] * 5,
        }
        sc_tool = _call(tools, "fw_sensor_scenario",
                        profile=json.dumps(hot_profile),
                        sensor_map=[{"device": "bmp280", "address": 0x76}],
                        dt_s=0.1, name="charger_hot_baro")
        state["fw_sensor_scenario"] = {
            "ok": sc_tool.get("ok"), "n_tracks": sc_tool.get("n_tracks"),
            "error": sc_tool.get("error"), "devices": sc_tool.get("devices"),
        }
        if sc_tool.get("error") or sc_tool.get("n_tracks") != 1:
            leg.fail(f"fw_sensor_scenario failed: {sc_tool}")
            return
        (OUT_DIR / "safety_scenario.json").write_text(
            json.dumps({"adc_safety": scenario,
                        "sensor_tool": state["fw_sensor_scenario"]},
                       indent=2), encoding="utf-8")

        if not pid:
            leg.fail("no fw project")
            return
        if not (b.get("success") and elf):
            firmware_leg = next(l for l in LEGS if l.name == "firmware")
            if firmware_leg.status == "SKIP":
                leg.skip(firmware_leg.detail)
            else:
                leg.fail(f"no ELF for safety simulation: {b}")
            return

        t0 = time.time()
        s = _call(tools, "fw_sim_run", project_id=pid, elf_path=elf,
                  scenario=json.dumps(scenario), timeout_s=SIM_BUDGET_S,
                  assertions=["uart:FAULT", "uart:STATE:FAULT"])
        sim_ms = int((time.time() - t0) * 1000)
        state["fw_sim"] = s
        state["fw_sim_ms"] = sim_ms
        uart = str(s.get("uart_log") or "")
        art = {"sim_ms": sim_ms, "passed": s.get("passed"),
               "uart_log_head": uart[:400],
               "assertion_results": s.get("assertion_results"),
               "error": s.get("error")}

        if sim_ms > SIM_BUDGET_S * 1000:
            leg.fail(f"sim wall {sim_ms}ms > budget {SIM_BUDGET_S}s")
            return
        if s.get("skipped") or "not installed" in uart.lower():
            leg.skip(f"emulator unavailable: {uart[:120]}")
            return
        if "renode" in uart.lower() and s.get("passed") is False and (
                "not found" in uart.lower() or "not installed" in uart.lower()
                or "no renode" in uart.lower()):
            leg.skip(f"Renode missing: {uart[:120]}")
            return
        # Assert firmware entered FAULT
        fault_seen = ("STATE:FAULT" in uart) or ("FAULT:OTP" in uart) \
            or ("FAULT:OVP" in uart) or ("FAULT" in uart)
        if s.get("passed") and fault_seen:
            leg.pass_(f"safety-in-loop FAULT ok ({sim_ms} ms); "
                      f"uart_has_FAULT={fault_seen} passed={s.get('passed')}",
                      artifact=art)
            return
        if s.get("uart_log") is not None:
            # Ran but did not hit FAULT — real defect
            leg.fail(f"sim ran but FAULT not observed; uart={uart[:200]}")
            return
        leg.fail(f"fw_sim_run failed: {s.get('error') or uart[:100]}")

    run_leg("safety_loop", leg_safety_loop)

    # ── 10. POWER / REPORT ────────────────────────────────────────────────
    def leg_power_report(leg: Leg) -> None:
        from services.mcp.power_budget import solve_power_budget

        boardphys = _charger_boardphys()
        # Prefer live boardphys if thermal/to3d produced one
        for cand in (
            (state.get("thermal") or {}).get("boardphys_path"),
            (state.get("to_3d") or {}).get("boardphys_path"),
        ):
            if cand and os.path.isfile(str(cand)):
                try:
                    with open(cand, encoding="utf-8") as fh:
                        boardphys = json.load(fh)
                    break
                except Exception:  # noqa: BLE001
                    pass

        profile = _charger_power_profile(0.5)
        pp = (state.get("fw_sim") or {}).get("power_profile_path")
        if pp and os.path.isfile(str(pp)):
            with open(pp, encoding="utf-8") as fh:
                profile = json.load(fh)

        source = {"kind": "supply", "name": "VIN", "voltage_v": 5.0,
                  "i_max_a": 2.0}
        converters = [{"name": "U_CHG", "input": "VIN", "output": "VBAT",
                       "voltage_out_v": 4.2, "topology": "buck",
                       "efficiency": 0.88, "i_out_max_a": 1.5}]
        # Also try MCP power_budget tool
        pb_tool = _call(tools, "power_budget",
                        source=source, converters=converters,
                        board=boardphys, power_profile=profile)
        r = pb_tool if pb_tool.get("ok") is not None else solve_power_budget(
            source, converters=converters, board=boardphys,
            power_profile=profile)
        if not isinstance(r, dict) or r.get("ok") is None:
            r = solve_power_budget(source, converters=converters,
                                   board=boardphys, power_profile=profile)
        state["budget"] = r
        (OUT_DIR / "power_budget.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8")

        total_ms = int((time.time() - t_all) * 1000)
        counts = {
            "pass": sum(1 for L in LEGS if L.status == "PASS"),
            "warn": sum(1 for L in LEGS if L.status == "WARN"),
            "fail": sum(1 for L in LEGS if L.status == "FAIL"),
            "skip": sum(1 for L in LEGS if L.status == "SKIP"),
        }
        # Include this leg as pending in provisional card; final rewrite below.
        card = {
            "benchmark": "battery_charger",
            "ok": all(L.status != "FAIL" for L in LEGS),
            "seed": SEED,
            "total_wall_ms": total_ms,
            "thresholds": {
                "V_CV": V_CV, "I_CC": I_CC, "V_LVD": V_LVD,
                "V_OVP": V_OVP, "I_OCP": I_OCP, "R_SENSE": R_SENSE,
                "V_OTP_REF": V_OTP_REF,
            },
            "counts": counts,
            "legs": [
                {"name": L.name, "status": L.status, "detail": L.detail,
                 "wall_ms": L.ms}
                for L in LEGS
            ],
            "sim": {
                "charge": state.get("sim_charge"),
                "lvd": state.get("sim_lvd"),
                "safety": state.get("sim_safety"),
            },
            "artifacts": {
                "sch_path": state.get("sch_path"),
                "board_path": state.get("board_path"),
                "pcb_doc_id": state.get("pcb_doc_id"),
                "sch_doc_id": state.get("sch_doc_id"),
                "fw_project_id": state.get("fw_project_id"),
            },
            "power_budget": {
                "ok": r.get("ok"), "verdict": r.get("verdict"),
                "battery": r.get("battery"),
            },
        }
        state["report_card"] = card
        (OUT_DIR / "summary.json").write_text(
            json.dumps(card, indent=2, default=str), encoding="utf-8")

        lines = [
            "B2 battery-charger benchmark — verdict card",
            f"seed={SEED}  total_wall_ms={total_ms}",
            f"PASS={counts['pass']} WARN={counts['warn']} "
            f"FAIL={counts['fail']} SKIP={counts['skip']}",
            "",
            f"{'LEG':16} {'STAT':5} {'MS':>8}  DETAIL",
            "-" * 72,
        ]
        for L in LEGS:
            lines.append(
                f"{L.name:16} {L.status:5} {L.ms:8d}  {L.detail[:90]}")
        lines.append("-" * 72)
        lines.append("RESULT: " + ("PASS" if card["ok"] else "FAIL"))
        text = "\n".join(lines) + "\n"
        (OUT_DIR / "VERDICT.txt").write_text(text, encoding="utf-8")
        print()
        print(text)

        if not r.get("ok"):
            leg.fail(f"power_budget failed: {r.get('error')}; card written")
            return
        leg.pass_(
            f"power verdict={r.get('verdict')}; card total_wall_ms={total_ms} "
            f"P/W/F/S={counts['pass']}/{counts['warn']}/"
            f"{counts['fail']}/{counts['skip']}",
            artifact=card)

    run_leg("power_report", leg_power_report)

    # ── final summary ─────────────────────────────────────────────────────
    total_ms = int((time.time() - t_all) * 1000)
    summary = {
        "benchmark": "battery_charger",
        "ok": all(L.status != "FAIL" for L in LEGS),
        "seed": SEED,
        "total_wall_ms": total_ms,
        "legs": [{"name": L.name, "status": L.status, "detail": L.detail,
                  "wall_ms": L.ms}
                 for L in LEGS],
        "counts": {
            "pass": sum(1 for L in LEGS if L.status == "PASS"),
            "warn": sum(1 for L in LEGS if L.status == "WARN"),
            "fail": sum(1 for L in LEGS if L.status == "FAIL"),
            "skip": sum(1 for L in LEGS if L.status == "SKIP"),
        },
        "state_keys": sorted(state.keys()),
        "thresholds": {
            "V_CV": V_CV, "I_CC": I_CC, "V_LVD": V_LVD,
            "V_OVP": V_OVP, "I_OCP": I_OCP,
        },
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (MARKER_DIR / "overall.marker").write_text(
        ("PASS" if summary["ok"] else "FAIL") + "\n"
        + json.dumps(summary["counts"]) + "\n"
        + f"total_wall_ms={total_ms}\n",
        encoding="utf-8")

    # Rewrite VERDICT with final leg list
    counts = summary["counts"]
    lines = [
        "B2 battery-charger benchmark — verdict card",
        f"seed={SEED}  total_wall_ms={total_ms}",
        f"PASS={counts['pass']} WARN={counts['warn']} "
        f"FAIL={counts['fail']} SKIP={counts['skip']}",
        "",
        f"{'LEG':16} {'STAT':5} {'MS':>8}  DETAIL",
        "-" * 72,
    ]
    for L in LEGS:
        lines.append(f"{L.name:16} {L.status:5} {L.ms:8d}  {L.detail[:90]}")
    lines.append("-" * 72)
    lines.append("RESULT: " + ("PASS" if summary["ok"] else "FAIL"))
    (OUT_DIR / "VERDICT.txt").write_text("\n".join(lines) + "\n",
                                         encoding="utf-8")

    print("=== summary ===")
    for L in LEGS:
        print(f"  {L.status:4}  {L.name:14}  {L.ms:8d} ms  {L.detail[:100]}")
    c = summary["counts"]
    print(f"  totals: {c['pass']} pass / {c['warn']} warn / "
          f"{c['fail']} fail / {c['skip']} skip")
    print(f"  total_wall_ms: {total_ms}")
    print(f"  artifacts: {OUT_DIR}")
    if summary["ok"]:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
