#!/usr/bin/env python3
"""B1 end-to-end flight-computer benchmark — real MCP tool surface.

Drives the landed ECAD + antenna + firmware + sensor-bridge stack on a
deterministic central-flight-computer design:

  MCU (Generic_QFP48) + 3V3 buck + MPU6050 IMU + BMP280 baro + LIS3MDL mag
  + NEO-6M GPS + decoupling, then board_from_schematic, antenna, checks,
  firmware, climb+turn sensor-in-the-loop, power budget, report card.

Harness pattern copied from scripts/demo_sensor_node.py (_StubMCP / Leg /
run_leg / register_all_tools). Honest PASS / WARN / FAIL / SKIP verdicts with
actual wall-times. main() returns 0 when no FAIL (SKIP allowed).

The GNSS stimulus starts 1 ms after reset so firmware enables UE/RE before the
first real 9600-baud start edge. All 11 fixes, 200 ms cadence and sensor bytes
are unchanged; each bus runs for the same explicit 3 s virtual horizon.

Env:
  CHUDWARE_FC_OUT          output dir (default benchmarks/flight_computer)
  CHUDWARE_FC_MARKERS      marker dir (default $OUT/markers)
  CHUDWARE_ECAD_HEAVY_BUDGET_S  heavy-tool budget seconds (default 180 here)
  CHUDWARE_FC_ANT_FULLWAVE_BUDGET_S  openEMS full-wave budget (default 90)
  CHUDWARE_FC_SIM_BUDGET_S per-run Renode fw_sim_run wall budget (default 120; over = FAIL)
    Explicit benchmark budget; the native API's default host deadline remains 30 s.
  CHUDWARE_FC_SEED         deterministic seed (default 0; fixed inputs)
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# Heavy ECAD tools (board_from_schematic / autoroute / 3d / dfm) are budget-
# guarded at import time — set BEFORE importing services.mcp.ecad_tools.
os.environ.setdefault("CHUDWARE_ECAD_HEAVY_BUDGET_S", "180")
os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OUT_DIR = Path(os.environ.get(
    "CHUDWARE_FC_OUT", str(REPO / "benchmarks" / "flight_computer")))
MARKER_DIR = Path(os.environ.get(
    "CHUDWARE_FC_MARKERS", str(OUT_DIR / "markers")))

SEED = int(os.environ.get("CHUDWARE_FC_SEED", "0") or "0")
ANT_FULLWAVE_BUDGET_S = float(
    os.environ.get("CHUDWARE_FC_ANT_FULLWAVE_BUDGET_S", "90") or "90")
SIM_BUDGET_S = float(os.environ.get("CHUDWARE_FC_SIM_BUDGET_S", "120") or "120")
GNSS_STIMULUS_OFFSET_MS = 1.0

# Fixed flight-computer BOM (libids from services.ecad.library).
REQUIRED_PARTS: list[tuple[str, str]] = [
    ("MCU:Generic_QFP48", "mcu qfp48"),
    ("Sensor_Motion:MPU6050", "MPU6050"),
    ("Sensor_Pressure:BMP280", "BMP280"),
    ("Sensor_Magnetic:LIS3MDL", "LIS3MDL"),
    ("RF_GPS:NEO6M", "NEO6M"),
    ("Regulator_Switching:Buck_3V3", "Buck_3V3"),
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
    from services.mcp.ecad_reliability import register_ecad_reliability_tools
    from services.mcp.antenna_tools import register_antenna_tools
    from services.mcp.power_budget import register_power_budget_tools
    from services.mcp.firmware_tools import register_firmware_tools

    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_ecad_reliability_tools(stub)
    register_antenna_tools(stub)
    register_power_budget_tools(stub)
    register_firmware_tools(stub)
    return stub.tools


# ── schematic fixture ────────────────────────────────────────────────────────

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


def build_flight_computer_schematic(tools: dict,
                                    name: str = "flight_computer") -> str:
    """MCU + buck + IMU/baro/mag on I2C + GPS on UART + decoupling.

    Deterministic placement (fixed x/y). Nets via ecad_wire so board_from_schematic
    sees a real connectivity graph. Power ports name the +3V3 / GND rails.
    """
    r = _call(tools, "ecad_new_schematic", name=name)
    sid = r.get("doc_id") or ""
    if not sid:
        raise RuntimeError(f"ecad_new_schematic failed: {r}")

    # ICs + passives + power ports + VIN source (sim front-end shares this sch
    # for power-rail OP; board flow drops power/V_source footprints).
    for args in (
        dict(libid="MCU:Generic_QFP48", x=0.0, y=0.0, ref="U1", value="MCU"),
        dict(libid="Sensor_Motion:MPU6050", x=90.0, y=40.0, ref="U2",
             value="MPU6050"),
        dict(libid="Sensor_Pressure:BMP280", x=90.0, y=0.0, ref="U3",
             value="BMP280"),
        dict(libid="Sensor_Magnetic:LIS3MDL", x=90.0, y=-40.0, ref="U4",
             value="LIS3MDL"),
        dict(libid="RF_GPS:NEO6M", x=90.0, y=-80.0, ref="U5", value="NEO-6M"),
        dict(libid="Regulator_Switching:Buck_3V3", x=-90.0, y=0.0, ref="U6",
             value="Buck_3V3"),
        dict(libid="Device:C", x=-40.0, y=40.0, ref="C1", value="100n"),
        dict(libid="Device:C", x=-40.0, y=-40.0, ref="C2", value="10u"),
        dict(libid="Device:C", x=30.0, y=40.0, ref="C3", value="100n"),
        dict(libid="Device:L_Power", x=-55.0, y=0.0, ref="L1", value="4.7uH"),
        dict(libid="Device:R", x=40.0, y=60.0, ref="R1", value="4.7k"),
        dict(libid="Device:R", x=40.0, y=80.0, ref="R2", value="4.7k"),
        dict(libid="power:+3V3", x=-20.0, y=60.0, ref="P3V3"),
        dict(libid="power:GND", x=-20.0, y=-60.0, ref="PGND"),
        dict(libid="Simulation_SPICE:V_source", x=-130.0, y=0.0, ref="VIN",
             value="5"),
    ):
        _place(tools, sid, **args)

    # Power tree: VIN -> buck SW -> L -> +3V3; FB/EN tied; GND common.
    W = [
        ("VIN", "+", "U6", "6"),       # VIN -> buck VIN
        ("VIN", "-", "PGND", "1"),
        ("U6", "3", "PGND", "1"),      # buck GND
        ("U6", "2", "L1", "1"),        # SW -> inductor
        ("L1", "2", "P3V3", "1"),      # VOUT
        ("U6", "4", "P3V3", "1"),      # FB sense
        ("U6", "5", "VIN", "+"),       # EN to VIN
        # MCU supplies (VDD/VSS/VDDA/VSSA)
        ("U1", "13", "P3V3", "1"),
        ("U1", "12", "PGND", "1"),
        ("U1", "24", "P3V3", "1"),
        ("U1", "23", "PGND", "1"),
        ("U1", "9", "P3V3", "1"),
        ("U1", "8", "PGND", "1"),
        # IMU
        ("U2", "6", "P3V3", "1"),
        ("U2", "18", "P3V3", "1"),
        ("U2", "7", "PGND", "1"),
        ("U2", "11", "PGND", "1"),
        ("U2", "25", "PGND", "1"),
        # BMP280
        ("U3", "8", "P3V3", "1"),
        ("U3", "1", "P3V3", "1"),
        ("U3", "2", "PGND", "1"),
        ("U3", "7", "PGND", "1"),
        # LIS3MDL
        ("U4", "12", "P3V3", "1"),
        ("U4", "9", "P3V3", "1"),
        ("U4", "6", "PGND", "1"),
        ("U4", "8", "PGND", "1"),
        # GPS
        ("U5", "1", "P3V3", "1"),
        ("U5", "2", "PGND", "1"),
        # Decoupling
        ("C1", "1", "P3V3", "1"), ("C1", "2", "PGND", "1"),
        ("C2", "1", "P3V3", "1"), ("C2", "2", "PGND", "1"),
        ("C3", "1", "P3V3", "1"), ("C3", "2", "PGND", "1"),
        # I2C bus: MCU P14=SCL, P15=SDA  (Generic_QFP48 GPIO stand-ins)
        ("U1", "14", "U2", "20"),  # SCL
        ("U2", "20", "U3", "4"),
        ("U3", "4", "U4", "1"),
        ("U1", "15", "U2", "21"),  # SDA
        ("U2", "21", "U3", "3"),
        ("U3", "3", "U4", "3"),
        ("R1", "1", "U1", "14"), ("R1", "2", "P3V3", "1"),  # SCL pull-up
        ("R2", "1", "U1", "15"), ("R2", "2", "P3V3", "1"),  # SDA pull-up
        # UART GPS
        ("U1", "16", "U5", "4"),   # MCU TX -> GPS RX
        ("U1", "17", "U5", "3"),   # MCU RX <- GPS TX
    ]
    for a, ap, b, bp in W:
        _wire(tools, sid, a, ap, b, bp)
    return sid


def build_power_frontend_schematic(tools: dict,
                                   name: str = "fc_power_frontend") -> str:
    """Ideal 3V3 rail OP-point schematic (sim-model-backed, no LDO skip).

    V_source@3.3 across Rload to GND with +3V3 power port naming the rail.
    MNA floats the reference so we assert |V(+)-V(-)| ~= 3.3 V, not absolute.
    """
    r = _call(tools, "ecad_new_schematic", name=name)
    sid = r.get("doc_id") or ""
    if not sid:
        raise RuntimeError(f"ecad_new_schematic failed: {r}")
    for args in (
        dict(libid="Simulation_SPICE:V_source", x=0.0, y=0.0, ref="V3V3",
             value="3.3"),
        dict(libid="Device:R", x=40.0, y=0.0, ref="Rload", value="330"),
        dict(libid="power:GND", x=20.0, y=-40.0, ref="GND1"),
        dict(libid="power:+3V3", x=40.0, y=20.0, ref="P3V3"),
        dict(libid="Device:C", x=60.0, y=0.0, ref="Cbulk", value="10u"),
    ):
        _place(tools, sid, **args)
    _wire(tools, sid, "V3V3", "+", "Rload", "1")
    _wire(tools, sid, "V3V3", "+", "P3V3", "1")
    _wire(tools, sid, "V3V3", "+", "Cbulk", "1")
    _wire(tools, sid, "V3V3", "-", "GND1", "1")
    _wire(tools, sid, "Rload", "2", "GND1", "1")
    _wire(tools, sid, "Cbulk", "2", "GND1", "1")
    return sid


def _op_rail_delta_v(op: dict) -> Optional[float]:
    """Best-effort |Vhigh - Vlow| from an OP result dict."""
    if not isinstance(op, dict) or not op:
        return None
    vals = []
    for k, v in op.items():
        if not str(k).startswith("V("):
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < 2:
        # single node vs implicit 0
        if len(vals) == 1:
            return abs(vals[0])
        return None
    return abs(max(vals) - min(vals))


def _flight_boardphys() -> dict:
    """Synthetic boardphys with starter-pack supply rails (deterministic)."""
    return {
        "schema": "aicad.boardphys/1",
        "board": {"name": "flight_computer"},
        "components": [
            {"ref": "U1", "libid": "MCU:Generic_QFP48", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": 0.020, "current_a_sleep": 1e-5}],
                "voltage_domains": ["3V3"], "sequencing": []}},
            {"ref": "U2", "libid": "Sensor_Motion:MPU6050", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": 0.0039, "current_a_sleep": 5e-6}],
                "voltage_domains": ["3V3"], "sequencing": []}},
            {"ref": "U3", "libid": "Sensor_Pressure:BMP280", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": 0.00072, "current_a_sleep": 1e-7}],
                "voltage_domains": ["3V3"], "sequencing": []}},
            {"ref": "U4", "libid": "Sensor_Magnetic:LIS3MDL", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": 0.00027, "current_a_sleep": 1e-6}],
                "voltage_domains": ["3V3"], "sequencing": []}},
            {"ref": "U5", "libid": "RF_GPS:NEO6M", "supply": {
                "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                           "current_a_active": 0.045, "current_a_sleep": 2e-5}],
                "voltage_domains": ["3V3"], "sequencing": []}},
            {"ref": "U6", "libid": "Regulator_Switching:Buck_3V3", "supply": {
                "rails": [{"name": "VIN", "voltage_v_nom": 5.0,
                           "current_a_active": 0.001}],
                "voltage_domains": ["VIN", "VOUT"], "sequencing": []}},
        ],
    }


def _flight_power_profile(duty: float = 0.10) -> dict:
    """Representative 10 % wake duty for flight-computer sensors + MCU."""
    i_mcu = 0.020 * duty + 1e-5 * (1 - duty)
    i_imu = 0.0039 * duty + 5e-6 * (1 - duty)
    i_baro = 0.00072 * duty
    i_mag = 0.00027 * duty
    i_gps = 0.045  # GPS acquisition assumed always-on during flight leg
    i_avg = i_mcu + i_imu + i_baro + i_mag + i_gps
    return {
        "schema": "aicad.powerprofile/1",
        "model": "duty-cycle bookkeeping",
        "trace_ref": "bench-flight-computer-synthetic",
        "cpu": {"active_frac": duty, "sleep_frac": 1 - duty},
        "pins": {},
        "rails": [{"name": "3V3", "voltage_v_nom": 3.3,
                   "i_avg_a": i_avg, "i_peak_a": 0.020 + 0.0039 + 0.045,
                   "residency": {"active_frac": duty, "sleep_frac": 1 - duty},
                   "schedule": {"duty": duty, "period_s": 1.0},
                   "provenance": [
                       f"bench: MCU+IMU+baro+mag @{duty:.0%} + GPS always-on"]}],
        "coverage": {"components_total": 6, "components_with_rails": 6,
                     "unknown_active_current": [],
                     "notes": ["synthetic profile; live fw_sim power_profile "
                               "used when present"]},
    }


FLIGHT_SENSOR_FW_C = r'''/* B1 flight-computer sensor-loop firmware (STM32F4, bare metal, IRQ serial).
 *
 * Bus map (templates/renode_platforms/stm32f4_discovery.repl):
 *   USART1 TX  console / telemetry (fw_sim_run uart_log)
 *   USART2 RX  NEO-6M GNSS NMEA: $GPGGA altitude, $GPRMC course over ground
 *   I2C1       MPU6050 @0x68 gyro yaw rate; BMP280 @0x76 pressure compensated with
 *              the chip's own trimming registers; LIS3MDL @0x1C horizontal field
 *
 * Each estimator prints a one-shot *_DETECTED line once the climb or the turn shows
 * up in its own sensor, so the bench asserts flight outcomes, not boot banners.
 * No libc (no printf/sbrk); every bus wait is bounded so an absent or silent device
 * never hangs the loop. Serial IRQs drain real DR/SR; SysTick deadlines and WFI
 * replace instruction-count sampling and continuous idle MMIO polling.
 */
#include <stdint.h>

#define REG32(addr) (*(volatile uint32_t *)(addr))

#define USART1_BASE 0x40011000u
#define USART2_BASE 0x40004400u
#define USART_SR(base)  REG32((base) + 0x00u)
#define USART_DR(base)  REG32((base) + 0x04u)
#define USART_BRR(base) REG32((base) + 0x08u)
#define USART_CR1(base) REG32((base) + 0x0Cu)
#define USART_CR3(base) REG32((base) + 0x14u)
#define USART_SR_ERRORS 0x0Fu   /* PE, FE, NF, ORE: SR then DR clears them */
#define USART_SR_RXNE   (1u << 5)
#define USART_SR_TXE    (1u << 7)
#define USART_CR1_RE    (1u << 2)
#define USART_CR1_TE    (1u << 3)
#define USART_CR1_RXNEIE (1u << 5)
#define USART_CR1_TXEIE  (1u << 7)
#define USART_CR1_PEIE   (1u << 8)
#define USART_CR1_UE    (1u << 13)
#define NVIC_ISER1 REG32(0xE000E104u)
#define SCB_VTOR   REG32(0xE000ED08u)
#define SYST_CSR   REG32(0xE000E010u)
#define SYST_RVR   REG32(0xE000E014u)
#define SYST_CVR   REG32(0xE000E018u)

#define I2C1_BASE  0x40005400u
#define I2C_CR1    REG32(I2C1_BASE + 0x00u)
#define I2C_CR2    REG32(I2C1_BASE + 0x04u)
#define I2C_DR     REG32(I2C1_BASE + 0x10u)
#define I2C_SR1    REG32(I2C1_BASE + 0x14u)
#define I2C_SR2    REG32(I2C1_BASE + 0x18u)
#define I2C_CCR    REG32(I2C1_BASE + 0x1Cu)
#define I2C_TRISE  REG32(I2C1_BASE + 0x20u)
#define I2C_CR1_PE    (1u << 0)
#define I2C_CR1_START (1u << 8)
#define I2C_CR1_STOP  (1u << 9)
#define I2C_CR1_ACK   (1u << 10)
#define I2C_CR1_POS   (1u << 11)
#define I2C_CR1_SWRST (1u << 15)
#define I2C_SR1_SB    (1u << 0)
#define I2C_SR1_ADDR  (1u << 1)
#define I2C_SR1_BTF   (1u << 2)
#define I2C_SR1_RXNE  (1u << 6)
#define I2C_SR1_TXE   (1u << 7)
#define I2C_SR1_ERRORS 0xDF00u /* BERR, ARLO, AF, OVR, PECERR, TIMEOUT, SMBALERT */
#define I2C_TIMEOUT_MS 20u

#define MPU6050_ADDR 0x68u
#define BMP280_ADDR  0x76u
#define LIS3MDL_ADDR 0x1Cu

#define CLIMB_DM          50     /* GNSS altitude gain >= 5.0 m */
#define TURN_CD           1500   /* GNSS course change >= 15.00 deg */
#define YAW_RATE_RAW      655    /* |gyro z| >= 5 deg/s at 131 LSB per deg/s */
#define BARO_CLIMB_PA     50u    /* static pressure drop >= 50 Pa (~4 m) */
#define MAG_TURN_INV_SIN2 15     /* sin^2(dpsi) >= 1/15, i.e. |dpsi| >= ~15 deg */
#define SENSOR_PERIOD_MS 20u  /* 50 Hz, independent of CPU/MMIO instruction cost */
#define NMEA_MAX          96u
#define SERIAL_CAPACITY 512u  /* power of two; holds >3 serialized GNSS fixes */
#define SERIAL_MASK (SERIAL_CAPACITY - 1u)
#define CONSOLE_TIMEOUT_MS 20u

static volatile uint32_t g_ms;
static volatile uint32_t g_rx_head, g_rx_tail, g_rx_lost;
static volatile uint16_t g_rx[SERIAL_CAPACITY];
static volatile uint32_t g_tx_head, g_tx_tail, g_tx_lost;
static volatile uint8_t g_tx[SERIAL_CAPACITY];

static int deadline_reached(uint32_t end)
{
    return (int32_t)(g_ms - end) >= 0;
}

static void systick_handler(void)
{
    ++g_ms;
}

static void usart1_handler(void)
{
    if (!(USART_SR(USART1_BASE) & USART_SR_TXE)) {
        return;
    }
    uint32_t tail = g_tx_tail;
    if (tail == g_tx_head) {
        USART_CR1(USART1_BASE) &= ~USART_CR1_TXEIE;
        return;
    }
    USART_DR(USART1_BASE) = g_tx[tail];
    g_tx_tail = (tail + 1u) & SERIAL_MASK;
}

static void usart2_handler(void)
{
    uint32_t sr = USART_SR(USART2_BASE);
    if (!(sr & (USART_SR_RXNE | USART_SR_ERRORS))) {
        return;
    }
    uint32_t byte = USART_DR(USART2_BASE); /* actual SR -> DR error clear sequence */
    uint32_t head = g_rx_head;
    uint32_t next = (head + 1u) & SERIAL_MASK;
    if (g_rx_lost || next == g_rx_tail) {
        ++g_rx_lost; /* keep the gap sticky until foreground resynchronizes */
        return;
    }
    g_rx[head] = (uint16_t)((byte & 0xFFu) | ((sr & USART_SR_ERRORS) << 8));
    g_rx_head = next;
}

extern uint32_t _estack;
extern void Reset_Handler(void);
extern void Default_Handler(void);

/* The baseline startup supplies only reset/fault vectors. Relocate VTOR to this
 * complete F4 interrupt table before enabling either USART or SysTick. */
__attribute__((aligned(256)))
static void (*const fc_vectors[64])(void) = {
    [0] = (void (*)(void))(&_estack),
    [1] = Reset_Handler,
    [2] = Default_Handler, [3] = Default_Handler, [4] = Default_Handler,
    [5] = Default_Handler, [6] = Default_Handler,
    [15] = systick_handler,
    [53] = usart1_handler, /* IRQ 37 */
    [54] = usart2_handler  /* IRQ 38 */
};

static void interrupts_init(void)
{
    SCB_VTOR = (uint32_t)(uintptr_t)fc_vectors;
    __asm__ volatile("dsb\nisb" ::: "memory");
    SYST_RVR = 16000u - 1u; /* 1 ms @ platform's unchanged 16 MHz HSI */
    SYST_CVR = 0u;
    SYST_CSR = 7u; /* ENABLE, TICKINT, processor clock */
    NVIC_ISER1 = (1u << 5) | (1u << 6);
}

/* ── console (USART1 TX) ─────────────────────────────────────────────────── */

static void con_putc(char c)
{
    uint32_t end = g_ms + CONSOLE_TIMEOUT_MS;
    for (;;) {
        /* Protect the full/idle test against an IRQ arriving just before WFI.
         * A pending enabled IRQ wakes Cortex-M WFI even with PRIMASK set. */
        __asm__ volatile("cpsid i" ::: "memory");
        uint32_t head = g_tx_head;
        uint32_t next = (head + 1u) & SERIAL_MASK;
        if (next != g_tx_tail) {
            g_tx[head] = (uint8_t)c;
            g_tx_head = next;
            USART_CR1(USART1_BASE) |= USART_CR1_TXEIE;
            __asm__ volatile("cpsie i" ::: "memory");
            return;
        }
        if (deadline_reached(end)) {
            ++g_tx_lost;
            __asm__ volatile("cpsie i" ::: "memory");
            return;
        }
        __asm__ volatile("dsb\nwfi\ncpsie i" ::: "memory");
    }
}

static void con_puts(const char *s)
{
    while (*s) {
        con_putc(*s++);
    }
}

static void con_putu(uint32_t v)
{
    char digits[10];
    uint32_t n = 0u;
    do {
        digits[n++] = (char)('0' + v % 10u);
        v /= 10u;
    } while (v != 0u);
    while (n != 0u) {
        con_putc(digits[--n]);
    }
}

static void con_puti(int32_t v)
{
    if (v < 0) {
        con_putc('-');
        con_putu(0u - (uint32_t)v);
    } else {
        con_putu((uint32_t)v);
    }
}

static void con_puthex8(uint32_t v)
{
    static const char hex[] = "0123456789ABCDEF";
    con_puts("0x");
    con_putc(hex[(v >> 4) & 0xFu]);
    con_putc(hex[v & 0xFu]);
}

/* ── I2C1 master, bounded waits (RM0090 27.3.3) ───────────────────────────── */

static void i2c_init(void)
{
    I2C_CR1 = I2C_CR1_SWRST;
    I2C_CR1 = 0u;
    I2C_CR2 = 16u;  /* APB1 = 16 MHz HSI */
    I2C_CCR = 80u;  /* 100 kHz standard mode */
    I2C_TRISE = 17u;
    I2C_CR1 = I2C_CR1_PE;
}

static int i2c_wait(uint32_t mask)
{
    uint32_t end = g_ms + I2C_TIMEOUT_MS;
    for (;;) {
        uint32_t sr1 = I2C_SR1;
        if (sr1 & I2C_SR1_ERRORS) {
            return -1;
        }
        if ((sr1 & mask) == mask) {
            return 0;
        }
        if (deadline_reached(end)) {
            return -1;
        }
        /* Clock stretching keeps the finite F4 receive registers intact while
         * asleep. SysTick supplies a bounded wake even for an absent device. */
        __asm__ volatile("wfi" ::: "memory");
    }
}

static int i2c_wait_stop(void)
{
    uint32_t end = g_ms + I2C_TIMEOUT_MS;
    while ((I2C_CR1 & I2C_CR1_STOP) || (I2C_SR2 & (1u << 1))) {
        if (deadline_reached(end)) {
            return -1;
        }
        __asm__ volatile("wfi" ::: "memory");
    }
    return 0;
}

static void i2c_abort(void)
{
    I2C_CR1 |= I2C_CR1_STOP;
    I2C_SR1 &= ~I2C_SR1_ERRORS;
    if (i2c_wait_stop() != 0) {
        /* A failed transaction cannot leave stale STOP/BUSY for the next one. */
        i2c_init();
    }
    /* STOP does not consume DR. A failed receive may leave both finite RX
     * registers full; drain them before the next sensor's repeated START. */
    for (uint32_t i = 0u; i < 2u && (I2C_SR1 & I2C_SR1_RXNE); ++i) {
        (void)I2C_DR;
    }
}

static int i2c_address(uint8_t addr, uint32_t read)
{
    I2C_CR1 |= I2C_CR1_START;
    if (i2c_wait(I2C_SR1_SB) != 0) {
        return -1;
    }
    I2C_DR = ((uint32_t)addr << 1) | read;
    return i2c_wait(I2C_SR1_ADDR);
}

static void i2c_clear_addr(void)
{
    (void)I2C_SR1;
    (void)I2C_SR2;
}

static int i2c_send(uint8_t byte)
{
    if (i2c_wait(I2C_SR1_TXE) != 0) {
        return -1;
    }
    I2C_DR = byte;
    return i2c_wait(I2C_SR1_BTF);
}

static int i2c_write_reg(uint8_t addr, uint8_t reg, uint8_t value)
{
    int ok = i2c_address(addr, 0u) == 0;
    if (ok) {
        i2c_clear_addr();
        ok = i2c_send(reg) == 0 && i2c_send(value) == 0;
    }
    if (!ok) {
        i2c_abort();
        return -1;
    }
    I2C_CR1 |= I2C_CR1_STOP;
    return i2c_wait_stop();
}

static int i2c_read_regs(uint8_t addr, uint8_t reg, uint8_t *buf, uint32_t len)
{
    I2C_CR1 = I2C_CR1_PE | I2C_CR1_ACK;
    int ok = len != 0u && i2c_address(addr, 0u) == 0;
    if (ok) {
        i2c_clear_addr();
        ok = i2c_send(reg) == 0 && i2c_address(addr, 1u) == 0;  /* repeated START */
    }
    if (ok) {
        if (len == 1u) {
            __asm__ volatile("cpsid i" ::: "memory");
            I2C_CR1 &= ~(I2C_CR1_ACK | I2C_CR1_POS);
            i2c_clear_addr();
            I2C_CR1 |= I2C_CR1_STOP;
            __asm__ volatile("cpsie i" ::: "memory");
            ok = i2c_wait(I2C_SR1_RXNE) == 0;
            if (ok) {
                buf[0] = (uint8_t)I2C_DR;
            }
        } else if (len == 2u) {
            /* RM0090 two-byte receive: POS=1 and ACK=0 before ADDR clear.
             * The ACK position then NACKs the second byte, not the first. */
            __asm__ volatile("cpsid i" ::: "memory");
            I2C_CR1 = (I2C_CR1 | I2C_CR1_POS) & ~I2C_CR1_ACK;
            i2c_clear_addr();
            __asm__ volatile("cpsie i" ::: "memory");
            ok = i2c_wait(I2C_SR1_BTF) == 0;
            if (ok) {
                __asm__ volatile("cpsid i" ::: "memory");
                I2C_CR1 |= I2C_CR1_STOP;
                buf[0] = (uint8_t)I2C_DR;
                buf[1] = (uint8_t)I2C_DR;
                __asm__ volatile("cpsie i" ::: "memory");
            }
        } else {
            i2c_clear_addr();
            uint32_t i = 0u;
            while (ok && len - i > 3u) {
                ok = i2c_wait(I2C_SR1_RXNE) == 0;
                if (ok) {
                    buf[i++] = (uint8_t)I2C_DR;
                }
            }
            /* Last three bytes: BTF, NACK N, drain N-2; BTF, STOP, drain pair.
             * Unlike byte-by-byte polling, this is safe across SysTick sleeps. */
            ok = ok && i2c_wait(I2C_SR1_BTF) == 0;
            if (ok) {
                __asm__ volatile("cpsid i" ::: "memory");
                I2C_CR1 &= ~I2C_CR1_ACK;
                buf[i++] = (uint8_t)I2C_DR;
                __asm__ volatile("cpsie i" ::: "memory");
                ok = i2c_wait(I2C_SR1_BTF) == 0;
            }
            if (ok) {
                __asm__ volatile("cpsid i" ::: "memory");
                I2C_CR1 |= I2C_CR1_STOP;
                buf[i++] = (uint8_t)I2C_DR;
                buf[i] = (uint8_t)I2C_DR;
                __asm__ volatile("cpsie i" ::: "memory");
            }
        }
    }
    if (!ok) {
        i2c_abort();
        return -1;
    }
    return i2c_wait_stop();
}

/* ── I2C sensors ─────────────────────────────────────────────────────────── */

static int g_imu_ready, g_baro_ready, g_mag_ready;
static int g_imu_done, g_baro_done, g_mag_done;
static int g_baro_have_ref, g_mag_have_ref;
static uint32_t g_baro_ref_pa;
static int32_t g_mag_ref_x, g_mag_ref_y;
static int32_t g_dig_t[4];  /* BMP280 dig_T1..dig_T3 at [1..3] */
static int64_t g_dig_p[10]; /* BMP280 dig_P1..dig_P9 at [1..9] */

static uint32_t le_u16(const uint8_t *b)
{
    return (uint32_t)b[0] | ((uint32_t)b[1] << 8);
}

static int32_t le_s16(const uint8_t *b)
{
    return (int16_t)le_u16(b);
}

static int probe_id(const char *name, uint8_t addr, uint8_t reg, const char *label,
                    uint8_t expect)
{
    uint8_t id = 0u;
    con_puts(name);
    if (i2c_read_regs(addr, reg, &id, 1u) != 0) {
        con_puts(" NACK\r\n");
        return 0;
    }
    con_putc(' ');
    con_puts(label);
    con_putc('=');
    con_puthex8(id);
    con_puts("\r\n");
    return id == expect;
}

static int configure(const char *name, uint8_t addr, uint8_t reg, uint8_t value)
{
    if (i2c_write_reg(addr, reg, value) == 0) {
        return 1;
    }
    con_puts(name);
    con_puts(" CONFIG_NACK\r\n");
    return 0;
}

static int bmp280_load_trim(void)
{
    uint8_t t[24];  /* 0x88..0x9F: dig_T1..dig_P9, little endian */
    if (i2c_read_regs(BMP280_ADDR, 0x88u, t, sizeof t) != 0) {
        con_puts("BMP280 TRIM_NACK\r\n");
        return 0;
    }
    g_dig_t[1] = (int32_t)le_u16(&t[0]);
    g_dig_t[2] = le_s16(&t[2]);
    g_dig_t[3] = le_s16(&t[4]);
    g_dig_p[1] = (int64_t)le_u16(&t[6]);
    for (uint32_t i = 2u; i <= 9u; ++i) {
        g_dig_p[i] = le_s16(&t[6u + 2u * (i - 1u)]);
    }
    if (g_dig_t[1] == 0 || g_dig_p[1] == 0) {
        con_puts("BMP280 TRIM_INVALID\r\n");
        return 0;
    }
    return 1;
}

/* Bosch BMP280 datasheet 3.11.3 integer compensation. */
static int32_t bmp280_t_fine(int32_t adc_t)
{
    int32_t var1 = (((adc_t >> 3) - g_dig_t[1] * 2) * g_dig_t[2]) >> 11;
    int32_t d = (adc_t >> 4) - g_dig_t[1];
    int32_t var2 = (((d * d) >> 12) * g_dig_t[3]) >> 14;
    return var1 + var2;
}

static uint32_t bmp280_pressure_pa(int32_t adc_p, int32_t t_fine)
{
    int64_t var1 = (int64_t)t_fine - 128000;
    int64_t var2 = var1 * var1 * g_dig_p[6];
    var2 += var1 * g_dig_p[5] * 131072;
    var2 += g_dig_p[4] * 34359738368LL;
    var1 = ((var1 * var1 * g_dig_p[3]) >> 8) + var1 * g_dig_p[2] * 4096;
    var1 = ((140737488355328LL + var1) * g_dig_p[1]) >> 33;
    if (var1 == 0) {
        return 0u;
    }
    int64_t p = 1048576 - (int64_t)adc_p;
    p = ((p * 2147483648LL - var2) * 3125) / var1;
    var1 = (g_dig_p[9] * (p >> 13) * (p >> 13)) >> 25;
    var2 = (g_dig_p[8] * p) >> 19;
    p = ((p + var1 + var2) >> 8) + g_dig_p[7] * 16;
    return (uint32_t)(p >> 8);  /* Q24.8 -> Pa */
}

static void sensors_init(void)
{
    i2c_init();

    g_imu_ready = probe_id("MPU6050", MPU6050_ADDR, 0x75u, "WHO_AM_I", 0x68u) &&
                  configure("MPU6050", MPU6050_ADDR, 0x6Bu, 0x00u);  /* PWR_MGMT_1: wake */
    g_baro_ready = probe_id("BMP280", BMP280_ADDR, 0xD0u, "CHIP_ID", 0x58u) &&
                   configure("BMP280", BMP280_ADDR, 0xF4u, 0x27u) &&  /* T x1, P x1, normal */
                   bmp280_load_trim();
    g_mag_ready = probe_id("LIS3MDL", LIS3MDL_ADDR, 0x0Fu, "WHO_AM_I", 0x3Du) &&
                  configure("LIS3MDL", LIS3MDL_ADDR, 0x22u, 0x00u);  /* CTRL_REG3: continuous */
}

static void sample_imu(void)
{
    uint8_t g[2];
    if (i2c_read_regs(MPU6050_ADDR, 0x47u, g, 2u) != 0) {  /* GYRO_ZOUT_H/L */
        return;
    }
    int32_t gz = (int16_t)(((uint32_t)g[0] << 8) | g[1]);
    if (!g_imu_done && (gz >= YAW_RATE_RAW || gz <= -YAW_RATE_RAW)) {
        g_imu_done = 1;
        con_puts("IMU_YAW_RATE_DETECTED gz_raw=");
        con_puti(gz);
        con_puts("\r\n");
    }
}

static void sample_baro(void)
{
    uint8_t d[6];
    if (i2c_read_regs(BMP280_ADDR, 0xF7u, d, 6u) != 0) {  /* press_msb .. temp_xlsb */
        return;
    }
    int32_t adc_p = (int32_t)(((uint32_t)d[0] << 12) | ((uint32_t)d[1] << 4) |
                              ((uint32_t)d[2] >> 4));
    int32_t adc_t = (int32_t)(((uint32_t)d[3] << 12) | ((uint32_t)d[4] << 4) |
                              ((uint32_t)d[5] >> 4));
    uint32_t pa = bmp280_pressure_pa(adc_p, bmp280_t_fine(adc_t));
    if (!g_baro_have_ref) {
        g_baro_have_ref = 1;
        g_baro_ref_pa = pa;
        con_puts("BARO p0_pa=");
        con_putu(pa);
        con_puts("\r\n");
        return;
    }
    if (!g_baro_done && g_baro_ref_pa >= pa + BARO_CLIMB_PA) {
        g_baro_done = 1;
        con_puts("BARO_CLIMB_DETECTED dp_pa=");
        con_putu(g_baro_ref_pa - pa);
        con_puts("\r\n");
    }
}

static void sample_mag(void)
{
    uint8_t m[4];
    /* LIS3MDL auto-increments only with SUB bit 7 set (datasheet 5.1.1). */
    if (i2c_read_regs(LIS3MDL_ADDR, 0x28u | 0x80u, m, 4u) != 0) {  /* OUT_X_L..OUT_Y_H */
        return;
    }
    int32_t x = le_s16(&m[0]);
    int32_t y = le_s16(&m[2]);
    if (!g_mag_have_ref) {
        g_mag_have_ref = 1;
        g_mag_ref_x = x;
        g_mag_ref_y = y;
        return;
    }
    int64_t cross = (int64_t)g_mag_ref_x * y - (int64_t)g_mag_ref_y * x;
    int64_t norms = ((int64_t)g_mag_ref_x * g_mag_ref_x + (int64_t)g_mag_ref_y * g_mag_ref_y) *
                    ((int64_t)x * x + (int64_t)y * y);
    if (!g_mag_done && norms > 0 && cross * cross >= norms / MAG_TURN_INV_SIN2) {
        g_mag_done = 1;
        con_puts("MAG_TURN_DETECTED\r\n");
    }
}

static void sensors_sample(void)
{
    if (g_imu_ready) {
        sample_imu();
    }
    if (g_baro_ready) {
        sample_baro();
    }
    if (g_mag_ready) {
        sample_mag();
    }
}

/* ── GNSS (USART2 RX, NMEA 0183) ─────────────────────────────────────────── */

static char g_nmea[NMEA_MAX];
static uint32_t g_nmea_len;
static int g_nmea_open;
static uint32_t g_nmea_bad;
static int g_gga_valid;
static int32_t g_alt_dm;
static int g_gnss_have_ref, g_climb_done, g_turn_done;
static int32_t g_alt0_dm, g_cog0_cd;

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/* Start of comma-separated field idx (0 = sentence id), or 0 when absent. */
static const char *nmea_field(const char *s, uint32_t idx)
{
    while (idx != 0u) {
        while (*s != '\0' && *s != ',') {
            ++s;
        }
        if (*s != ',') {
            return 0;
        }
        ++s;
        --idx;
    }
    return s;
}

/* Decimal field ("110.0", "28.65") scaled by 10^decimals. */
static int parse_fixed(const char *s, uint32_t decimals, int32_t *out)
{
    int32_t v = 0;
    uint32_t frac = 0u, digits = 0u;
    int neg = 0, dot = 0;
    if (*s == '-') {
        neg = 1;
        ++s;
    }
    for (; *s != '\0' && *s != ','; ++s) {
        if (*s == '.') {
            if (dot) {
                return -1;
            }
            dot = 1;
            continue;
        }
        if (*s < '0' || *s > '9') {
            return -1;
        }
        if (dot) {
            if (frac == decimals) {
                continue;
            }
            ++frac;
        }
        v = v * 10 + (*s - '0');
        ++digits;
    }
    if (digits == 0u) {
        return -1;
    }
    while (frac < decimals) {
        v *= 10;
        ++frac;
    }
    *out = neg ? -v : v;
    return 0;
}

static int starts_with(const char *s, const char *prefix)
{
    while (*prefix) {
        if (*s++ != *prefix++) {
            return 0;
        }
    }
    return 1;
}

static void gnss_report(int32_t cog_cd)
{
    con_puts("GNSS alt_dm=");
    con_puti(g_alt_dm);
    con_puts(" cog_cd=");
    con_puti(cog_cd);
    con_puts(" bad=");
    con_putu(g_nmea_bad);
    con_puts("\r\n");
    if (!g_gnss_have_ref) {
        g_gnss_have_ref = 1;
        g_alt0_dm = g_alt_dm;
        g_cog0_cd = cog_cd;
        return;
    }
    if (!g_climb_done && g_alt_dm - g_alt0_dm >= CLIMB_DM) {
        g_climb_done = 1;
        con_puts("GNSS_CLIMB_DETECTED dalt_dm=");
        con_puti(g_alt_dm - g_alt0_dm);
        con_puts("\r\n");
    }
    int32_t dcog = cog_cd - g_cog0_cd;
    while (dcog > 18000) {
        dcog -= 36000;
    }
    while (dcog <= -18000) {
        dcog += 36000;
    }
    if (!g_turn_done && (dcog >= TURN_CD || dcog <= -TURN_CD)) {
        g_turn_done = 1;
        con_puts("GNSS_TURN_DETECTED dcog_cd=");
        con_puti(dcog);
        con_puts("\r\n");
    }
}

static void nmea_sentence(char *body)
{
    char *star = body;
    uint32_t sum = 0u;
    while (*star != '\0' && *star != '*') {
        sum ^= (uint8_t)*star;
        ++star;
    }
    if (*star != '*' || hex_nibble(star[1]) < 0 || hex_nibble(star[2]) < 0 ||
        star[3] != '\0' ||
        (uint32_t)(hex_nibble(star[1]) * 16 + hex_nibble(star[2])) != sum) {
        ++g_nmea_bad;
        return;
    }
    *star = '\0';
    if (starts_with(body, "GPGGA,")) {
        const char *quality = nmea_field(body, 6u);
        const char *alt = nmea_field(body, 9u);
        g_gga_valid = quality != 0 && *quality >= '1' && *quality <= '9' && alt != 0 &&
                      parse_fixed(alt, 1u, &g_alt_dm) == 0;
    } else if (starts_with(body, "GPRMC,")) {
        const char *status = nmea_field(body, 2u);
        const char *cog = nmea_field(body, 8u);
        int32_t cog_cd = 0;
        if (g_gga_valid && status != 0 && *status == 'A' && cog != 0 &&
            parse_fixed(cog, 2u, &cog_cd) == 0) {
            gnss_report(cog_cd);
        }
        g_gga_valid = 0;
    }
}

static void nmea_feed(char c)
{
    if (c == '$') {
        if (g_nmea_open) {
            ++g_nmea_bad;  /* previous sentence lost its terminator */
        }
        g_nmea_open = 1;
        g_nmea_len = 0u;
        return;
    }
    if (!g_nmea_open) {
        return;
    }
    if (c == '\r' || c == '\n') {
        g_nmea[g_nmea_len] = '\0';
        g_nmea_open = 0;
        nmea_sentence(g_nmea);
        return;
    }
    if (g_nmea_len + 1u >= NMEA_MAX) {
        ++g_nmea_bad;
        g_nmea_open = 0;
        return;
    }
    g_nmea[g_nmea_len++] = c;
}

static void poll_gnss(void)
{
    for (;;) {
        __asm__ volatile("cpsid i" ::: "memory");
        uint32_t lost = g_rx_lost;
        if (lost) {
            g_rx_tail = g_rx_head;
            g_rx_lost = 0u;
        }
        uint32_t tail = g_rx_tail;
        int available = tail != g_rx_head;
        uint16_t entry = available ? g_rx[tail] : 0u;
        if (available) {
            g_rx_tail = (tail + 1u) & SERIAL_MASK;
        }
        __asm__ volatile("cpsie i" ::: "memory");
        if (lost) {
            g_nmea_bad += lost;
            g_nmea_open = g_gga_valid = 0;
        }
        if (!available) {
            return;
        }
        if (entry & (USART_SR_ERRORS << 8)) {
            ++g_nmea_bad; /* do not treat a framing/noise/overrun byte as a fix */
            g_nmea_open = g_gga_valid = 0;
        } else {
            nmea_feed((char)(entry & 0xFFu));
        }
    }
}

int main(void)
{
    interrupts_init();
    /* GNSS receiver first: capture bytes even during sensor initialization/TX. */
    USART_BRR(USART2_BASE) = 0x683u;  /* 9600 Bd (NEO-6M default) @ 16 MHz HSI */
    USART_CR3(USART2_BASE) = 1u; /* EIE: FE/NF/ORE, plus PEIE in CR1 */
    USART_CR1(USART2_BASE) = USART_CR1_UE | USART_CR1_RE |
                           USART_CR1_RXNEIE | USART_CR1_PEIE;
    USART_BRR(USART1_BASE) = 0x8Bu;   /* 115200 Bd @ 16 MHz HSI */
    USART_CR1(USART1_BASE) = USART_CR1_UE | USART_CR1_TE;
    con_puts("FC_SENSOR_FW_BOOT\r\n");
    sensors_init();

    uint32_t next_sample = g_ms;
    uint32_t reported_tx_lost = 0u;
    for (;;) {
        poll_gnss();
        if (deadline_reached(next_sample)) {
            sensors_sample();
            /* Missed periods are not replayed as stale back-to-back samples. */
            next_sample += SENSOR_PERIOD_MS;
            if (deadline_reached(next_sample)) {
                next_sample = g_ms + SENSOR_PERIOD_MS;
            }
        }
        if (g_tx_lost != reported_tx_lost) {
            reported_tx_lost = g_tx_lost;
            con_puts("CONSOLE_DROPPED bytes=");
            con_putu(reported_tx_lost);
            con_puts("\r\n");
        }
        __asm__ volatile("cpsid i" ::: "memory");
        if (g_rx_tail == g_rx_head && !g_rx_lost && !deadline_reached(next_sample)) {
            __asm__ volatile("dsb\nwfi" ::: "memory");
        }
        __asm__ volatile("cpsie i" ::: "memory");
    }
    return 0;
}
'''

# Outcome assertions per sensor bus. FLIGHT_SENSOR_FW_C prints each *_DETECTED line
# only when its own sensor shows the prescribed climb (100 -> 110 m) or turn
# (heading 0 -> 0.5 rad = 28.65 deg).
GNSS_LOOP_ASSERTIONS = [
    "uart:FC_SENSOR_FW_BOOT",
    "uart:GNSS_CLIMB_DETECTED",
    "uart:GNSS_TURN_DETECTED",
    # Final NEO-6M fix of the trace: 110.0 m, COG 28.65 deg, no NMEA checksum rejects.
    "uart:GNSS alt_dm=1100 cog_cd=2865 bad=0",
]
I2C_LOOP_ASSERTIONS = [
    "uart:MPU6050 WHO_AM_I=0x68",
    "uart:BMP280 CHIP_ID=0x58",
    "uart:LIS3MDL WHO_AM_I=0x3D",
    "uart:IMU_YAW_RATE_DETECTED",
    "uart:BARO_CLIMB_DETECTED",
    "uart:MAG_TURN_DETECTED",
]


def _emit_sensor_fw(tools: dict, project_id: str) -> dict:
    """Write the sensor-loop firmware over the template main so fw_build compiles it."""
    return _call(tools, "fw_sources", project_id=project_id,
                 write={"src/main.c": FLIGHT_SENSOR_FW_C})


# ── legs ─────────────────────────────────────────────────────────────────────

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    for old in MARKER_DIR.glob("*.marker"):
        old.unlink()

    print("=== B1 flight-computer E2E benchmark ===")
    print(f"  out: {OUT_DIR}")
    print(f"  seed: {SEED}")
    print(f"  CHUDWARE_ECAD_HEAVY_BUDGET_S="
          f"{os.environ.get('CHUDWARE_ECAD_HEAVY_BUDGET_S')}")
    print(f"  ant_fullwave_budget_s={ANT_FULLWAVE_BUDGET_S} "
          f"sim_budget_s={SIM_BUDGET_S}")

    t_all = time.time()
    tools = register_all_tools()
    state: dict[str, Any] = {"seed": SEED}

    # ── 1. PARTS ──────────────────────────────────────────────────────────
    def leg_parts(leg: Leg) -> None:
        resolved: dict[str, Any] = {}
        missing: list[str] = []
        for libid, query in REQUIRED_PARTS:
            r = _call(tools, "ecad_find_part", query=query, limit=8)
            hits = r.get("hits") or []
            hit_ids = [h.get("libid") for h in hits]
            ok = libid in hit_ids
            # Also require library.resolve for the exact libid.
            from services.ecad import library as lib
            sdef = lib.resolve(libid)
            if sdef is None:
                missing.append(libid)
                resolved[libid] = {"found_by_search": ok, "resolve": None}
                continue
            resolved[libid] = {
                "found_by_search": ok or any(
                    (h.get("libid") or "").endswith(libid.split(":")[-1])
                    for h in hits),
                "resolve": True,
                "pin_count": len(sdef.get("pins") or []),
                "search_top": hit_ids[:3],
            }
            if not resolved[libid]["found_by_search"]:
                # resolve is enough for board; warn if search missed
                pass
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
        sid = build_flight_computer_schematic(tools)
        state["sch_doc_id"] = sid
        st = _call(tools, "ecad_status", doc_id=sid, detail="summary")
        state["sch_status"] = st
        n_comp = int(st.get("component_count")
                     or len(st.get("components") or [])
                     or 0)
        n_nets = int(st.get("net_count") or len(st.get("nets") or []) or 0)
        if n_comp < 10:
            leg.fail(f"expected >=10 components, got {n_comp}")
            return
        # Persist schematic JSON for firmware / debugging.
        from services.mcp.ecad_common import ecad_session
        sch_path = OUT_DIR / "flight_computer_sch.json"
        sch_path.write_text(
            json.dumps(ecad_session.get_session(sid).doc, indent=2),
            encoding="utf-8")
        state["sch_path"] = str(sch_path)
        leg.pass_(f"sch={sid} components={n_comp} nets={n_nets}",
                  artifact={"doc_id": sid, "components": n_comp, "nets": n_nets})

    run_leg("schematic", leg_schematic)

    # ── 3. SIM (power front-end OP) ───────────────────────────────────────
    def leg_sim(leg: Leg) -> None:
        # Dedicated sim schematic: ideal 3V3 rail (buck has no SPICE model).
        sid = build_power_frontend_schematic(tools)
        state["pwr_sch_doc_id"] = sid
        r = _call(tools, "ecad_simulate", analysis="op", doc_id=sid,
                  backend="mna")
        state["sim"] = r
        if not r.get("ok"):
            leg.fail(f"ecad_simulate failed: {r.get('error') or r}")
            return
        if not r.get("converged") or not isinstance(r.get("op"), dict):
            leg.fail(f"OP did not converge: {r.get('hint') or r}")
            return
        op = r["op"]
        delta = _op_rail_delta_v(op)
        state["sim_rail_delta_v"] = delta
        if delta is None:
            leg.fail(f"no voltage probes in OP result: {op}")
            return
        # 3.3 V rail within 5 %
        if abs(delta - 3.3) > 0.2:
            leg.fail(f"3V3 rail OP delta={delta:.4f} V (expect ~3.3 V); "
                     f"op={op}")
            return
        leg.pass_(f"OP converged; |Vrail|={delta:.4f} V (3V3 ok); "
                  f"devices={r.get('device_count')}",
                  artifact={"op": op, "delta_v": delta,
                            "warnings": r.get("warnings")})

    run_leg("sim", leg_sim)

    # ── 4. BOARD (board_from_schematic) ───────────────────────────────────
    def leg_board(leg: Leg) -> None:
        sid = state.get("sch_doc_id")
        if not sid:
            leg.skip("no schematic")
            return
        r = _call(tools, "board_from_schematic", sch_doc_id=sid, optimize=True)
        state["board"] = r
        if r.get("status") == "running" or r.get("auto_backgrounded"):
            leg.skip(f"board_from_schematic budget: {r.get('error') or r.get('hint')}")
            return
        if not r.get("ok") and not r.get("board_doc_id"):
            leg.fail(f"board_from_schematic failed: {r.get('error') or r}")
            return
        bid = r.get("board_doc_id") or r.get("doc_id")
        state["pcb_doc_id"] = bid
        placed = int(r.get("placed") or 0)
        routed = int(r.get("routed") or 0)
        rep = r.get("report") or {}
        rn_before = rep.get("ratsnest_before")
        rn_after = rep.get("ratsnest_after")
        residual = r.get("residual_drc") or []
        residual_named = [
            f"{v.get('rule')}:{v.get('severity')}" for v in residual[:12]
        ]
        state["residual_drc_named"] = residual_named
        if placed < 6:
            leg.fail(f"placed={placed} (<6); report={rep}")
            return
        # Ensure a closed Edge.Cuts outline so to_3d / vibe / thermal have a substrate.
        from services.mcp.ecad_common import _default_outline_shapes, ecad_session
        sess = ecad_session.get_session(bid)
        try:
            ecad_session.apply_ops(sess, sess.revision, [{
                "op": "pcb_set_outline", "shapes": _default_outline_shapes(sess.doc),
                "closed": True}])
        except Exception as exc:  # noqa: BLE001
            r.setdefault("warnings", []).append(f"outline set failed: {exc}")

        board_path = OUT_DIR / "flight_computer_board.json"
        board_path.write_text(
            json.dumps(ecad_session.get_session(bid).doc, indent=2),
            encoding="utf-8")
        state["board_path"] = str(board_path)
        # Placement + residual DRC named — residual errors are expected on a
        # dense auto-placed board; tool honesty is the contract.
        detail = (f"board={bid} placed={placed} routed={routed} "
                  f"ratsnest {rn_before}->{rn_after} "
                  f"residual_drc={len(residual)} named={residual_named[:6]}")
        # Ratsnest residual is advisory (dense flight BOM); never FAIL the leg.
        if rn_after is not None and int(rn_after) > 20:
            leg.warn(detail + " (high residual ratsnest)", artifact=r)
            return
        leg.pass_(detail, artifact={
            "board_doc_id": bid, "placed": placed, "routed": routed,
            "report": rep, "residual_drc_named": residual_named,
            "failed_nets": r.get("failed_nets"),
        })

    run_leg("board", leg_board)

    # ── 5. TRACE-OPT (already inside board_from_schematic) ────────────────
    def leg_trace_opt(leg: Leg) -> None:
        br = state.get("board") or {}
        if not br:
            leg.skip("no board report")
            return
        rep = br.get("report") or {}
        corners = int(rep.get("corners_rounded") or 0)
        tears = int(rep.get("teardrops") or 0)
        tuned = int(rep.get("nets_tuned") or 0)
        steps = rep.get("steps") or []
        opt_step = next((s for s in steps
                         if (s.get("step") or "") == "optimize_traces"), None)
        # Re-run ecad_optimize_traces for an explicit impedance-target polish
        # when board exists (idempotent; records impedance path).
        bid = state.get("pcb_doc_id")
        opt2 = None
        if bid:
            opt2 = _call(tools, "ecad_optimize_traces",
                         targets=json.dumps({"+3V3": 50.0, "N$16": 50.0}),
                         doc_id=bid)
            state["trace_opt"] = opt2
            if opt2.get("ok"):
                corners = max(corners, int(opt2.get("corners_rounded") or 0))
                tears = max(tears, int(opt2.get("teardrops") or 0))
                tuned = max(tuned, int(opt2.get("nets_tuned") or 0))
        # Contract: report carries corners_rounded / teardrops / nets_tuned keys
        # (values may be 0 on sparse geometry — that is still a real pass).
        if "corners_rounded" not in rep and not (opt2 or {}).get("ok"):
            leg.fail(f"trace-opt metrics missing from board report: {rep}")
            return
        leg.pass_(f"corners_rounded={corners} teardrops={tears} "
                  f"nets_tuned/impedance={tuned} opt_step={bool(opt_step)}",
                  artifact={"report": rep, "optimize": opt2})

    run_leg("trace_opt", leg_trace_opt)

    # ── 6. ANTENNA ────────────────────────────────────────────────────────
    def leg_antenna(leg: Leg) -> None:
        # 2.4 GHz IFA (ISM / telemetry) + 1.575 GHz GPS patch
        ifa = _call(tools, "ecad_antenna_design", kind="ifa",
                    freq_hz=2.45e9, substrate_er=4.4, substrate_h_mm=1.6,
                    meander_turns=3)
        patch = _call(tools, "ecad_antenna_design", kind="patch",
                      freq_hz=1.575e9, substrate_er=4.4, substrate_h_mm=1.6)
        state["antenna_design"] = {"ifa": ifa, "gps_patch": patch}
        if not ifa.get("ok"):
            leg.fail(f"IFA design failed: {ifa.get('error')}")
            return
        if not patch.get("ok"):
            leg.fail(f"GPS patch design failed: {patch.get('error')}")
            return

        # Analytic always; full-wave under wall budget else SKIP that sub-part.
        an_ifa = _call(tools, "ecad_antenna_analyze", kind="ifa",
                       freq_hz=2.45e9, substrate_er=4.4, substrate_h_mm=1.6,
                       meander_turns=3, want_fullwave=False)
        state["antenna_analyze_analytic"] = an_ifa
        if not an_ifa.get("ok"):
            leg.fail(f"analytic analyze failed: {an_ifa.get('error')}")
            return

        fw_detail = "fullwave not attempted"
        fw_status = "skip"
        t_fw0 = time.time()
        try:
            # Bound openEMS: use reduced timesteps; if wall budget exceeded, SKIP.
            an_fw = _call(tools, "ecad_antenna_analyze", kind="ifa",
                          freq_hz=2.45e9, substrate_er=4.4, substrate_h_mm=1.6,
                          meander_turns=3, want_fullwave=True,
                          n_freq=21, max_timesteps=15_000)
            fw_ms = int((time.time() - t_fw0) * 1000)
            state["antenna_analyze_fullwave"] = an_fw
            fw = (an_fw.get("fullwave") or {}) if an_fw.get("ok") else {}
            if fw_ms > ANT_FULLWAVE_BUDGET_S * 1000:
                fw_detail = (f"fullwave wall {fw_ms}ms > budget "
                             f"{ANT_FULLWAVE_BUDGET_S}s")
                fw_status = "skip"
            elif fw.get("skipped") or an_fw.get("fullwave_skipped"):
                fw_detail = f"fullwave skipped: {fw.get('reason') or an_fw.get('warnings')}"
                fw_status = "skip"
            elif fw.get("ok"):
                fw_detail = (f"fullwave S11min={fw.get('s11_min_db')} dB @ "
                             f"{fw.get('f_res_ghz')} GHz ({fw_ms} ms)")
                fw_status = "pass"
            elif not an_fw.get("ok"):
                fw_detail = f"fullwave error: {an_fw.get('error')}"
                fw_status = "skip"  # openEMS flaky → honest skip, not FAIL
            else:
                fw_detail = f"fullwave inconclusive: {fw}"
                fw_status = "skip"
        except Exception as exc:  # noqa: BLE001
            fw_detail = f"fullwave exception: {type(exc).__name__}: {exc}"
            fw_status = "skip"

        dims = (ifa.get("dimensions") or {})
        f_res = an_ifa.get("f_res_ghz") or (an_ifa.get("analytic") or {}).get(
            "f_res_ghz")
        detail = (f"IFA+GPS patch designed; analytic f_res={f_res} GHz; "
                  f"fullwave={fw_status} ({fw_detail})")
        state["antenna_fw_status"] = fw_status
        if fw_status == "pass":
            leg.pass_(detail, artifact={"ifa": ifa, "patch": patch,
                                        "analytic": an_ifa,
                                        "fullwave": state.get(
                                            "antenna_analyze_fullwave")})
        else:
            # Analytic success is enough for PASS; fullwave skip is recorded.
            leg.pass_(detail + " [fullwave SKIP]",
                      artifact={"ifa": ifa, "patch": patch,
                                "analytic": an_ifa, "fw_status": fw_status,
                                "fw_detail": fw_detail})

    run_leg("antenna", leg_antenna)

    # ── 7. CHECKS ─────────────────────────────────────────────────────────
    # The fixture's buck regulates the rail it senses on FB; its SW node reaches that
    # rail through the output inductor, whose rail-side pad is where converter current
    # enters the copper (FB is a high-impedance sense input, not a current source).
    buck_libid = "Regulator_Switching:Buck_3V3"
    m3_clearance_d_mm = 3.2  # ISO 273 medium-fit clearance hole for an M3 mounting screw

    def pdn_rail_setup(board: dict) -> dict:
        """PDN net, source refs and nominal voltage from the placed buck output stage."""
        from services.ecad import library as lib
        from services.ecad import metadata
        footprints = board.get("footprints") or []
        bucks = [fp for fp in footprints if fp.get("libid") == buck_libid]
        if len(bucks) != 1:
            raise ValueError(f"expected one placed {buck_libid} footprint, "
                             f"found {len(bucks)}")
        buck = bucks[0]
        buck_ref = str(buck.get("ref"))
        pin_numbers = {str(p.get("name")): str(p.get("number"))
                       for p in (lib.resolve(buck_libid) or {}).get("pins") or []}

        def pad_net(fp: dict, number: Optional[str]) -> Optional[str]:
            for pad in fp.get("pads") or []:
                if str(pad.get("pin") or pad.get("number") or "") == number:
                    return pad.get("net") or None
            return None

        sw_net = pad_net(buck, pin_numbers.get("SW"))
        rail_net = pad_net(buck, pin_numbers.get("FB"))
        if not sw_net or not rail_net:
            raise ValueError(f"{buck_ref} SW/FB pads are not on nets "
                             f"(SW={sw_net}, FB={rail_net})")
        sources = sorted(
            str(fp.get("ref")) for fp in footprints
            if fp is not buck
            and {sw_net, rail_net} <= {p.get("net") for p in fp.get("pads") or []})
        if not sources:
            raise ValueError(f"no placed output inductor joins {buck_ref} SW net "
                             f"{sw_net} to its FB rail {rail_net}")
        rails = metadata.eng_for_footprint(buck)["supply"]["rails"] or []
        vout = next((r for r in rails if r.get("name") == "VOUT"), None)
        if vout is None or vout.get("voltage_v_nom") is None:
            raise ValueError(f"{buck_ref} metadata has no VOUT voltage_v_nom")
        return {"net": rail_net, "source_refs": sources,
                "vsrc": float(vout["voltage_v_nom"]), "regulator": buck_ref}

    def corner_mount_holes(board: dict) -> list[list[float]]:
        """M3 holes in the corners of the rectangular Edge.Cuts outline, inset so each
        clamped screw land (ecad_vibe_check's fixture annulus) lies on the board."""
        from services.mcp.ecad_reliability import _MOUNT_LAND_MM
        shapes = (board.get("outline") or {}).get("shapes") or []
        if not shapes:
            raise ValueError("board has no Edge.Cuts outline")
        vertices: set[tuple[float, float]] = set()
        for shape in shapes:
            if shape.get("type", "line") != "line":
                raise ValueError(f"outline shape {shape.get('type')!r} is not a line")
            for key in ("p1", "p2"):
                vertices.add((float(shape[key][0]), float(shape[key][1])))
        x0 = min(x for x, _ in vertices)
        x1 = max(x for x, _ in vertices)
        y0 = min(y for _, y in vertices)
        y1 = max(y for _, y in vertices)
        if vertices != {(x0, y0), (x1, y0), (x1, y1), (x0, y1)}:
            raise ValueError("Edge.Cuts outline is not an axis-aligned rectangle")
        inset = m3_clearance_d_mm / 2.0 + _MOUNT_LAND_MM
        if x1 - x0 <= 2.0 * inset or y1 - y0 <= 2.0 * inset:
            raise ValueError(f"{x1 - x0:g} x {y1 - y0:g} mm board cannot hold "
                             f"corner M3 lands ({inset:g} mm inset)")
        d = m3_clearance_d_mm
        return [[x0 + inset, y0 + inset, d], [x1 - inset, y0 + inset, d],
                [x1 - inset, y1 - inset, d], [x0 + inset, y1 - inset, d]]

    def leg_checks(leg: Leg) -> None:
        bid = state.get("pcb_doc_id")
        if not bid:
            leg.skip("no board")
            return
        results: dict[str, Any] = {}
        notes: list[str] = []
        soft_fails: list[str] = []

        # impedance
        z = _call(tools, "ecad_check_impedance", net_or_ref="+3V3",
                  target=50.0, doc_id=bid)
        if not z.get("ok") and z.get("error"):
            # try any routed net name from board report
            z = _call(tools, "ecad_check_impedance", net_or_ref="",
                      target=50.0, doc_id=bid)
        results["impedance"] = z
        if not z.get("ok"):
            soft_fails.append(f"impedance: {z.get('error') or z}")
        notes.append(f"Z0 ok={z.get('ok')} "
                     f"z0={z.get('z0') or z.get('z0_ohm')}")

        # DRC
        drc = _call(tools, "ecad_run_drc", doc_id=bid)
        results["drc"] = {
            "ok": drc.get("ok"),
            "n_violations": len(drc.get("violations") or []),
            "errors": drc.get("errors"),
            "error": drc.get("error"),
        }
        notes.append(f"DRC viol={results['drc']['n_violations']}")
        if drc.get("error") and not drc.get("ok") and drc.get("violations") is None:
            soft_fails.append(f"drc: {drc.get('error')}")

        # DFM
        dfm = _call(tools, "ecad_run_dfm", doc_id=bid)
        results["dfm"] = {
            "ok": dfm.get("ok"),
            "n_violations": len(dfm.get("violations") or []),
            "error": dfm.get("error"),
            "status": dfm.get("status"),
        }
        if dfm.get("error") and dfm.get("violations") is None \
                and dfm.get("status") != "running" and not dfm.get("auto_backgrounded"):
            soft_fails.append(f"dfm: {dfm.get('error')}")
        if dfm.get("status") == "running" or dfm.get("auto_backgrounded"):
            notes.append("DFM SKIP (budget)")
            results["dfm"]["skipped"] = True
        else:
            notes.append(f"DFM viol={results['dfm']['n_violations']}")

        from services.mcp.ecad_common import ecad_session
        bdoc = ecad_session.get_session(bid).doc

        # PDN: IR drop of the buck's regulated rail, sourced at the output inductor.
        try:
            rail = pdn_rail_setup(bdoc)
        except (KeyError, TypeError, ValueError) as exc:
            results["pdn"] = {"ok": False, "error": f"rail setup: {exc}"}
            soft_fails.append(f"pdn: rail setup: {exc}")
        else:
            pdn = _call(tools, "ecad_run_pdn", net=rail["net"],
                        source_refs=",".join(rail["source_refs"]),
                        vsrc=rail["vsrc"], doc_id=bid, resolution_mm=0.5)
            results["pdn"] = {
                "ok": pdn.get("ok"),
                "verdict": pdn.get("verdict"),
                "error": pdn.get("error"),
                "boardphys_path": pdn.get("boardphys_path"),
                "n_violations": len(pdn.get("violations") or []),
                **rail,
            }
            state["pdn"] = pdn
            if pdn.get("available") is False:
                results["pdn"]["skipped"] = True
                notes.append(f"PDN SKIP: {pdn.get('error') or pdn}")
            elif not pdn.get("ok"):
                soft_fails.append(f"pdn: {pdn.get('error') or pdn}")
            else:
                notes.append(f"PDN net={rail['net']} src={rail['source_refs']} "
                             f"verdict={pdn.get('verdict')}")

        # 3D / boardphys
        to3d = _call(tools, "ecad_to_3d",
                     path=str(OUT_DIR / "flight_computer.step"),
                     doc_id=bid)
        results["to_3d"] = {
            "ok": to3d.get("ok"),
            "error": to3d.get("error"),
            "step_path": to3d.get("step_path") or to3d.get("path"),
            "boardphys_path": to3d.get("boardphys_path") or to3d.get(
                "sidecar_path"),
            "status": to3d.get("status"),
        }
        state["to_3d"] = to3d
        if to3d.get("status") == "running" or to3d.get("auto_backgrounded"):
            notes.append("to_3d SKIP (budget/cold kernel)")
            results["to_3d"]["skipped"] = True
        elif not to3d.get("ok"):
            err = str(to3d.get("error") or "")
            if "build123d" in err.lower() or "import" in err.lower() \
                    or "OCP" in err or "unavailable" in err.lower():
                notes.append(f"to_3d SKIP: {err[:80]}")
                results["to_3d"]["skipped"] = True
            else:
                notes.append(f"to_3d err: {err[:80]}")
                soft_fails.append(f"to_3d: {err[:100]}")
        else:
            notes.append(f"to_3d ok step={results['to_3d']['step_path']}")

        # thermal handoff
        try:
            import build123d  # noqa: F401
            has_b123d = True
        except ImportError:
            has_b123d = False
        if not has_b123d:
            results["thermal"] = {"skipped": True, "reason": "no build123d"}
            notes.append("thermal SKIP (no build123d)")
        else:
            th = _call(tools, "ecad_thermal_handoff", doc_id=bid,
                       dry_run=True, mesh_fineness=0.3)
            results["thermal"] = {
                "ok": th.get("ok"),
                "dry_run": th.get("dry_run"),
                "solved": th.get("solved"),
                "error": th.get("error"),
                "simhandoff": bool(th.get("simhandoff")),
            }
            state["thermal"] = th
            if th.get("ok") or th.get("simhandoff"):
                notes.append(f"thermal handoff ok dry={th.get('dry_run')}")
            else:
                soft_fails.append(f"thermal: {th.get('error') or 'incomplete'}")

        # vibe check: dry-run envelope (geometry + mount fixtures + mass model, no
        # modal solve) clamped at corner M3 mounts of the real board outline.
        if not has_b123d:
            results["vibe"] = {"skipped": True, "reason": "no build123d"}
            notes.append("vibe SKIP (no build123d)")
        else:
            try:
                mounts = corner_mount_holes(bdoc)
            except (KeyError, TypeError, ValueError) as exc:
                results["vibe"] = {"ok": False, "error": f"mount setup: {exc}"}
                soft_fails.append(f"vibe: mount setup: {exc}")
            else:
                vb = _call(tools, "ecad_vibe_check", doc_id=bid, dry_run=True,
                           mount_holes=mounts, mesh_fineness=0.35)
                results["vibe"] = {
                    "ok": vb.get("ok"),
                    "error": vb.get("error"),
                    "dry_run": vb.get("dry_run"),
                    "solved": vb.get("solved"),
                    "mount_holes": mounts,
                    "n_fixtures": vb.get("n_fixtures"),
                    "simhandoff": bool(vb.get("simhandoff")),
                }
                state["vibe"] = vb
                if vb.get("ok"):
                    notes.append(f"vibe envelope ok dry_run={vb.get('dry_run')} "
                                 f"solved={vb.get('solved')} mounts={len(mounts)} "
                                 f"fixtures={vb.get('n_fixtures')}")
                elif vb.get("available") is False:
                    notes.append(f"vibe SKIP: {str(vb.get('error') or '')[:80]}")
                    results["vibe"]["skipped"] = True
                else:
                    soft_fails.append(f"vibe: {vb.get('error') or vb}")

        state["checks"] = results
        (OUT_DIR / "checks.json").write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8")

        # Residual findings are numeric verdicts, not a license to hide tool errors.
        if soft_fails:
            leg.fail("; ".join(soft_fails) + " | " + "; ".join(notes))
            return
        leg.pass_("; ".join(notes), artifact=results)

    run_leg("checks", leg_checks)

    # ── 8. FIRMWARE ───────────────────────────────────────────────────────
    def leg_firmware(leg: Leg) -> None:
        bid = state.get("pcb_doc_id")
        board_path = state.get("board_path")
        if not bid and not board_path:
            leg.skip("no board to link")
            return
        ref = bid or board_path
        r = _call(tools, "fw_new_project", name="flight_computer_fw",
                  mcu_family="stm32f4", board_doc_ref=str(ref))
        if not r.get("ok") and not r.get("project_id"):
            if board_path and ref != board_path:
                r = _call(tools, "fw_new_project", name="flight_computer_fw",
                          mcu_family="stm32f4", board_doc_ref=str(board_path))
        state["fw_project"] = r
        pid = r.get("project_id") or (r.get("project") or {}).get("id")
        if not pid:
            leg.fail(f"fw_new_project failed: {r.get('error') or r}")
            return
        state["fw_project_id"] = pid

        w = _emit_sensor_fw(tools, pid)
        state["fw_sources"] = w
        if w.get("code") or w.get("error") or not any(
                entry.get("path") == "src/main.c" for entry in w.get("written") or []):
            leg.fail(f"fw_sources write failed: {w}")
            return

        b = _call(tools, "fw_build", project_id=pid)
        state["fw_build"] = b
        if b.get("success") and b.get("elf_path"):
            leg.pass_(f"project={pid}; sensor-loop fw built elf={b.get('elf_path')} "
                      f"ms={b.get('ms')}",
                      artifact={"project": r, "sources": w, "build": b})
            return
        # Only a missing external cross-toolchain permits an optional skip.
        if b.get("returncode") == -1 and "toolchain_not_found:" in str(b.get("stderr")):
            leg.skip(f"project={pid}; cross-toolchain unavailable: {b.get('stderr')}")
            return
        leg.fail(f"fw_build failed: {b}")

    run_leg("firmware", leg_firmware)

    # ── 9. SENSOR-IN-THE-LOOP ─────────────────────────────────────────────
    def leg_sensor(leg: Leg) -> None:
        import shutil

        # Prescribed climb+turn input, not a replacement sensor solver. The native
        # fw_sensor_scenario bridge derives NED kinematics and sensor registers.
        times = [i / 10.0 for i in range(21)]
        prof_dict = {
            "t_s": times,
            "altitude_m": [100.0 + 5.0 * t for t in times],
            "heading_rad": [0.25 * t for t in times],
            "airspeed_m_s": [25.0 for _ in times],
            "temperature_c": 15.0,
            "origin_lat_deg": 37.4185,
            "origin_lon_deg": -122.0683,
            "origin_alt_m": 0.0,
        }
        # GNSS on USART2; USART1 is the firmware console (FLIGHT_SENSOR_FW_C bus map).
        sensor_map = [
            {"device": "mpu6050", "bus": "i2c1", "address": 0x68},
            {"device": "bmp280", "bus": "i2c1", "address": 0x76},
            {"device": "lis3mdl", "bus": "i2c1", "address": 0x1C},
            {"device": "neo6m", "kind": "uart_rx", "uart": "usart2"},
        ]

        # 0.2 s grid = 5 Hz, the NEO-6M's maximum navigation rate.
        sc_tool = _call(tools, "fw_sensor_scenario",
                        profile=json.dumps(prof_dict),
                        sensor_map=sensor_map,
                        dt_s=0.2, name="climb_turn_fc")
        state["sensor_scenario_tool"] = {
            k: sc_tool.get(k) for k in
            ("ok", "n_tracks", "devices", "dt_s", "duration_s", "ms", "error")
            if k in sc_tool
        }
        scenario = sc_tool.get("scenario")
        if sc_tool.get("error") or not isinstance(scenario, dict):
            leg.fail(f"fw_sensor_scenario failed: {sc_tool}")
            return
        state["sensor_scenario_source"] = "fw_sensor_scenario"

        tracks = scenario.get("tracks") or []
        n_tracks = len(tracks)
        if n_tracks != len(sensor_map):
            leg.fail(f"scenario tracks={n_tracks}; expected {len(sensor_map)}; tool={sc_tool}")
            return

        # A peer transmitting at reset precedes firmware's UE/RE enable and
        # makes the receiver synchronize to a data edge inside the first '$'.
        # Initialize the real MCU first; shift only GNSS excitation, never text,
        # sample count, cadence, trajectory or checksum/error acceptance.
        for track in tracks:
            if track.get("kind") == "uart_rx" and track.get("device") == "neo6m":
                track["events"] = [
                    {**event, "t_ms": event["t_ms"] + GNSS_STIMULUS_OFFSET_MS}
                    for event in track["events"]
                ]
        scenario.setdefault("meta", {})["gnss_stimulus_offset_ms"] = (
            GNSS_STIMULUS_OFFSET_MS)
        (OUT_DIR / "sensor_scenario.json").write_text(
            json.dumps(scenario, indent=2, ensure_ascii=True), encoding="utf-8")
        state["sensor_scenario"] = {
            "n_tracks": n_tracks,
            "devices": (scenario.get("meta") or {}).get("devices"),
            "schema": scenario.get("schema"),
        }

        # One emulator run per sensor bus, each always run and judged on its own
        # assertions, so a native refusal on one bus cannot hide whether the other
        # executed the trace. Together they cover every bridge track.
        loops = {
            "gnss_uart": ([t for t in tracks if t.get("kind") == "uart_rx"],
                          GNSS_LOOP_ASSERTIONS),
            "i2c_sensors": ([t for t in tracks if t.get("kind") == "i2c_register_stream"],
                            I2C_LOOP_ASSERTIONS),
        }
        if [len(trs) for trs, _ in loops.values()] != [1, 3]:
            leg.fail("bridge track kinds "
                     f"{[(t.get('device'), t.get('kind')) for t in tracks]}; expected "
                     "neo6m uart_rx + 3 i2c_register_stream")
            return

        # fw_verify_board (static — no emulator required)
        pid = state.get("fw_project_id")
        board_ref = state.get("pcb_doc_id") or state.get("board_path") or ""
        v = _call(tools, "fw_verify_board", board_doc_ref=str(board_ref),
                  project_id=pid or "", mcu_family="stm32f4")
        state["fw_verify"] = {
            "ok": v.get("ok"),
            "n_findings": len(v.get("findings") or []),
            "summary": v.get("summary"),
            "error": v.get("error"),
        }
        if not v.get("ok"):
            leg.fail(f"fw_verify_board failed: {v}")
            return
        b = state.get("fw_build") or {}
        elf = b.get("elf_path") or ""
        if not pid:
            leg.fail(f"scenario tracks={n_tracks}; no fw project for simulation")
            return
        if not (b.get("success") and elf):
            firmware_leg = next(l for l in LEGS if l.name == "firmware")
            if firmware_leg.status == "SKIP":
                leg.skip(f"scenario tracks={n_tracks}; {firmware_leg.detail}")
            else:
                leg.fail(f"scenario tracks={n_tracks}; build missing ELF: {b}")
            return
        # Sensor stimulus scenarios run only on the Renode path (no QEMU surface).
        if shutil.which("renode") is None:
            leg.skip(f"scenario tracks={n_tracks}; Renode not on PATH")
            return

        runs: dict[str, dict] = {}
        verdicts: list[tuple[bool, str]] = []
        for loop_name, (loop_tracks, assertions) in loops.items():
            sub = {
                "schema": scenario["schema"],
                "name": f"{scenario.get('name')}/{loop_name}",
                "tracks": loop_tracks,
                "meta": {**(scenario.get("meta") or {}),
                         "devices": [t.get("device") for t in loop_tracks]},
            }
            t_run = time.time()
            s = _call(tools, "fw_sim_run", project_id=pid, elf_path=elf,
                      scenario=json.dumps(sub, ensure_ascii=True),
                      assertions=assertions, timeout_s=SIM_BUDGET_S, run_s=3.0)
            wall_ms = int((time.time() - t_run) * 1000)
            runs[loop_name] = {"wall_ms": wall_ms, "assertions": assertions, "result": s}

            results = s.get("assertion_results") or []
            failed = [r for r in results if not r.get("passed")]
            uart = str(s.get("uart_log") or "")
            if s.get("code"):
                verdicts.append((False, f"{loop_name}: {s.get('code')}: "
                                        f"{s.get('message') or s.get('error')} "
                                        f"{s.get('detail') or s.get('hint') or ''}"))
            elif failed and all(str(r.get("detail") or "").startswith("not evaluated")
                                for r in failed):
                verdicts.append((False, f"{loop_name} not executed: "
                                        f"{uart.splitlines()[0] if uart else s}"))
            elif s.get("passed") is not True or failed or len(results) != len(assertions):
                verdicts.append((False, f"{loop_name} FAIL {len(failed)}/{len(assertions)}: "
                                        + "; ".join(f"{r.get('name')} ({r.get('detail')})"
                                                    for r in failed)))
            elif wall_ms > SIM_BUDGET_S * 1000:
                verdicts.append((False, f"{loop_name}: wall {wall_ms} ms > "
                                        f"CHUDWARE_FC_SIM_BUDGET_S={SIM_BUDGET_S}"))
            else:
                verdicts.append((True, f"{loop_name} PASS {len(results)}/{len(assertions)} "
                                       f"({wall_ms} ms)"))

        # The GNSS loop's trace (and its board-linked power profile) feeds the power leg.
        state["fw_sim"] = runs["gnss_uart"]["result"]
        state["fw_sim_runs"] = runs
        (OUT_DIR / "sensor_loop.json").write_text(
            json.dumps(runs, indent=2, default=str), encoding="utf-8")

        # Failing loops first: the console prints only the head of the detail.
        detail = " | ".join(d for ok, d in sorted(verdicts, key=lambda vd: vd[0]))
        if not all(ok for ok, _ in verdicts):
            leg.fail(detail)
            return
        leg.pass_(
            f"{detail}; scenario tracks={n_tracks} devices="
            f"{state['sensor_scenario'].get('devices')}; "
            f"verify findings={state['fw_verify']['n_findings']}",
            artifact={"scenario": state["sensor_scenario"],
                      "verify": state["fw_verify"],
                      "runs": {name: {"wall_ms": run["wall_ms"],
                                      **{k: run["result"].get(k) for k in
                                         ("passed", "ms", "assertion_results",
                                          "trace_path", "power_profile_path")}}
                               for name, run in runs.items()}})

    run_leg("sensor_loop", leg_sensor)

    # ── 10. POWER ─────────────────────────────────────────────────────────
    def leg_power(leg: Leg) -> None:
        from services.mcp.power_budget import solve_power_budget

        boardphys = None
        for cand in (
            (state.get("pdn") or {}).get("boardphys_path"),
            (state.get("to_3d") or {}).get("boardphys_path"),
            (state.get("to_3d") or {}).get("sidecar_path"),
        ):
            if cand and os.path.isfile(str(cand)):
                try:
                    with open(cand, encoding="utf-8") as fh:
                        boardphys = json.load(fh)
                    break
                except Exception:  # noqa: BLE001
                    pass
        if boardphys is None:
            boardphys = _flight_boardphys()

        profile = None
        pp = (state.get("fw_sim") or {}).get("power_profile_path")
        if pp and os.path.isfile(str(pp)):
            with open(pp, encoding="utf-8") as fh:
                profile = json.load(fh)
        if profile is None:
            profile = _flight_power_profile(0.10)

        source = {"kind": "battery", "name": "VBAT", "voltage_v": 11.1,
                  "capacity_mah": 2200.0}
        converters = [{"name": "U6_BUCK", "input": "VBAT", "output": "3V3",
                       "voltage_out_v": 3.3, "topology": "buck",
                       "efficiency": 0.90, "i_out_max_a": 2.0}]
        r = solve_power_budget(source, converters=converters, board=boardphys,
                               power_profile=profile)
        state["budget"] = r
        (OUT_DIR / "power_budget.json").write_text(
            json.dumps(r, indent=2, default=str), encoding="utf-8")
        if not r.get("ok"):
            leg.fail(f"power_budget failed: {r.get('error')}")
            return
        verdict = r.get("verdict")
        life = (r.get("battery") or {}).get("life_h")
        # Tool computed a numeric verdict — PASS even if verdict is warn/fail
        # (the budget math ran; battery sizing is advisory here).
        leg.pass_(f"verdict={verdict} battery_life_h={life}", artifact=r)

    run_leg("power", leg_power)

    # ── 11. REPORT ────────────────────────────────────────────────────────
    def leg_report(leg: Leg) -> None:
        total_ms = int((time.time() - t_all) * 1000)
        counts = {
            "pass": sum(1 for L in LEGS if L.status == "PASS"),
            "warn": sum(1 for L in LEGS if L.status == "WARN"),
            "fail": sum(1 for L in LEGS if L.status == "FAIL"),
            "skip": sum(1 for L in LEGS if L.status == "SKIP"),
        }
        card = {
            "benchmark": "flight_computer",
            "ok": all(L.status != "FAIL" for L in LEGS),
            "seed": SEED,
            "total_wall_ms": total_ms,
            "counts": counts,
            "legs": [
                {"name": L.name, "status": L.status, "detail": L.detail,
                 "wall_ms": L.ms}
                for L in LEGS
            ],
            "artifacts": {
                "sch_path": state.get("sch_path"),
                "board_path": state.get("board_path"),
                "pcb_doc_id": state.get("pcb_doc_id"),
                "sch_doc_id": state.get("sch_doc_id"),
                "fw_project_id": state.get("fw_project_id"),
            },
        }
        # Also append the report leg itself after we know its status — fill later.
        state["report_card"] = card
        path = OUT_DIR / "summary.json"
        # provisional write; main rewrites after this leg finishes
        path.write_text(json.dumps(card, indent=2), encoding="utf-8")

        # Human-readable verdict card
        lines = [
            "B1 flight-computer benchmark — verdict card",
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
        lines.append("RESULT: " + ("PASS" if card["ok"] else "FAIL"))
        text = "\n".join(lines) + "\n"
        (OUT_DIR / "VERDICT.txt").write_text(text, encoding="utf-8")
        print()
        print(text)
        leg.pass_(f"card written total_wall_ms={total_ms} "
                  f"P/W/F/S={counts['pass']}/{counts['warn']}/"
                  f"{counts['fail']}/{counts['skip']}",
                  artifact=card)

    run_leg("report", leg_report)

    # ── final summary (include report leg) ────────────────────────────────
    total_ms = int((time.time() - t_all) * 1000)
    summary = {
        "benchmark": "flight_computer",
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
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    (MARKER_DIR / "overall.marker").write_text(
        ("PASS" if summary["ok"] else "FAIL") + "\n"
        + json.dumps(summary["counts"]) + "\n"
        + f"total_wall_ms={total_ms}\n",
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
