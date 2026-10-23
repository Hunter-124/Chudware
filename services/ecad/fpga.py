"""FPGA support pack — bank-aware checks + pin-constraint export.

Data-driven FPGA part model: every supported device is ONE JSON file under
``services/ecad/fpga_devices/`` (schema ``aicad.fpga_device/1`` below). Adding a new
family/package is a DATA addition — no code changes.

Device JSON schema (aicad.fpga_device/1)
----------------------------------------
::

    {
      "schema": "aicad.fpga_device/1",
      "device": "iCE40UP5K-SG48",          # canonical id (match key, case-insensitive)
      "family": "ice40up",                  # freeform family tag
      "vendor": "lattice" | "xilinx" | ...,
      "package": "SG48",
      "constraint_dialect": "lpf" | "xdc",  # native constraint file for this vendor
      "coverage": "full" | "partial",       # is the pin table complete?
      "capabilities": {                     # which checks are meaningful for this seed
        "clock_pins_known": bool,           # false -> clock check SKIPS (never false-flags)
        "diff_pairs_known": bool            # false -> diff-pair check SKIPS
      },
      "banks": {                            # IO bank -> VCCIO facts
        "<bank>": {"vccio_pins": ["<pkg pin>", ...],   # package pins feeding this bank's VCCIO
                    "vccio_v_allowed": [1.8, 2.5, 3.3]} # legal VCCIO voltages
      },
      "pins": {                             # package pin/ball -> pin facts
        "<pkg pin>": {
          "name": "IOT_46B_G0",             # vendor site/function name
          "kind": "io"|"power"|"config"|"jtag"|"special"|"nc",
          "bank": "<bank>" | null,          # for io pins (and VCCIO power pins)
          "clock": bool,                    # true = clock-capable (GBIN/MRCC/SRCC/PCLK)
          "pair": "<pair id>" | null,       # true differential pair id (both members share it)
          "pol": "p"|"n"|null,              # polarity within the pair
          "role": "VCC"|"GND"|"CRESET_B"|"TCK"|... | null   # for non-io pins
        }, ...
      },
      "config": {
        "required": {                       # config/JTAG pins that MUST be wired
          "<pkg pin>": {"role": "CRESET_B", "rule": "wired", "note": "..."}
        },
        "notes": "datasheet guidance (free text, surfaced in check output)"
      },
      "sources": ["provenance notes — where the pin data came from"]
    }

Unknowns are honest: ``coverage: "partial"`` + ``capabilities`` flags make a check SKIP
(with a warning) instead of guessing. Seeds shipped here:

* ``iCE40UP5K-SG48`` — FULL (icestorm pinloc + iCEBreaker-bitsy symbol table)
* ``LFE5U-25F-CABGA256`` — partial (prjtrellis iodb: banks + true pairs)
* ``XC7A35T-CPG236`` — partial (Digilent Cmod A7 master XDC: sites, banks, MRCC/SRCC, pairs)

Checks (:func:`fpga_check`), all pure functions of the netlist + device data:

1. **bank_vccio** — every IO pin of a bank must be on a net whose required IO voltage
   (from its IOSTANDARD or net-name voltage tag) is compatible with ONE bank VCCIO; the
   bank VCCIO itself is inferred from the net wired to the bank's VCCIO pins.
2. **diff_pair** — nets named ``X_P``/``X_N`` (or ``X+``/``X-``) must land on the p/n
   halves of ONE true pair.
3. **clock** — nets whose name says clock (``*CLK*``/``*CLOCK*``) must enter on a
   clock-capable pin.
4. **config** — every ``config.required`` pin must be wired.

Exports (:func:`export_constraints`): XDC (Xilinx), LPF (Lattice), neutral CSV pinout —
all byte-deterministic (sorted) so they golden-test.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from typing import Any

_DEVICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fpga_devices")

# component field / sim_params key that names the device on a placed symbol
DEVICE_FIELD = "fpga_device"

# ── device registry ──────────────────────────────────────────────────────────────

_DEVICES: dict[str, dict] | None = None


def load_devices() -> dict[str, dict]:
    """Load (and cache) every ``aicad.fpga_device/1`` JSON under ``fpga_devices/``."""
    global _DEVICES
    if _DEVICES is None:
        devs: dict[str, dict] = {}
        if os.path.isdir(_DEVICE_DIR):
            for fn in sorted(os.listdir(_DEVICE_DIR)):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(_DEVICE_DIR, fn), encoding="utf-8") as fh:
                    d = json.load(fh)
                if d.get("schema") == "aicad.fpga_device/1" and d.get("device"):
                    devs[d["device"].lower()] = d
        _DEVICES = devs
    return _DEVICES


def get_device(name: str) -> dict | None:
    """Resolve a device by canonical id (case-insensitive; also matches with the
    package suffix omitted, e.g. 'iCE40UP5K' -> iCE40UP5K-SG48 when unambiguous)."""
    if not name:
        return None
    devs = load_devices()
    d = devs.get(name.strip().lower())
    if d is not None:
        return d
    pref = [v for k, v in devs.items() if k.startswith(name.strip().lower())]
    return pref[0] if len(pref) == 1 else None


def device_for_component(comp: dict) -> dict | None:
    """Find the FPGA device for a netlist component (fields.fpga_device > value > libid)."""
    fields = comp.get("fields") or {}
    for cand in (fields.get(DEVICE_FIELD), comp.get("value"),
                 str(comp.get("libid", "")).split(":")[-1]):
        d = get_device(str(cand or ""))
        if d is not None:
            return d
    return None


# ── voltage inference ────────────────────────────────────────────────────────────

# IOSTANDARD -> required VCCIO volts (common single-ended + LVDS standards)
IOSTANDARD_VCCIO: dict[str, float] = {
    "LVCMOS33": 3.3, "LVTTL": 3.3, "TMDS_33": 3.3,
    "LVCMOS25": 2.5, "LVDS_25": 2.5, "LVDS25": 2.5,
    "LVCMOS18": 1.8, "LVCMOS15": 1.5, "SSTL15": 1.5,
    "SSTL135": 1.35, "LVCMOS135": 1.35,
    "LVCMOS12": 1.2,
    # Lattice iCE40/ECP5 LVDS inputs run on a 2.5 V bank
    "LVDS": 2.5, "SB_LVDS_INPUT": 2.5,
}

_VNET_RE = re.compile(r"(?:^|[^0-9A-Z])\+?(\d)V(\d{1,2})(?:$|[^0-9])", re.IGNORECASE)


def voltage_from_net(net: str) -> float | None:
    """Infer a rail voltage from a net name: '+3V3'/'3V3'/'VCC1V8' -> 3.3/1.8; else None."""
    if not net:
        return None
    m = _VNET_RE.search(net.upper() + " ")
    if not m:
        return None
    whole, frac = m.group(1), m.group(2)
    return float(f"{whole}.{frac}")


def _iostd_voltage(iostd: str) -> float | None:
    return IOSTANDARD_VCCIO.get((iostd or "").strip().upper())


# ── assignment extraction ────────────────────────────────────────────────────────

def _comp_pins(netlist: dict, ref: str) -> dict[str, str]:
    """pin number -> net for the component with reference ``ref`` (numbers only)."""
    for comp in netlist.get("components", []):
        if comp.get("ref") == ref:
            return {str(k): v for k, v in (comp.get("pins") or {}).items() if v}
    return {}


def _find_fpgas(netlist: dict) -> list[tuple[str, dict]]:
    out = []
    for comp in netlist.get("components", []):
        d = device_for_component(comp)
        if d is not None:
            out.append((comp.get("ref", ""), d))
    return out


def assignments_from_netlist(netlist: dict, ref: str, device: dict,
                             io: dict | None = None) -> list[dict]:
    """Pin assignments for the FPGA ``ref``: one row per connected IO pin.

    ``io`` optionally maps net name -> {"iostandard": ...} (freq etc. ignored for now).
    Rows: {pin, net, name, bank, clock, pair, pol, iostandard}. Sorted by (net, pin).
    """
    io = io or {}
    pins = device.get("pins", {})
    rows: list[dict] = []
    for pin, net in _comp_pins(netlist, ref).items():
        pdef = pins.get(pin)
        if not pdef or pdef.get("kind") != "io":
            continue
        rows.append({
            "pin": pin, "net": net, "name": pdef.get("name"),
            "bank": pdef.get("bank"), "clock": bool(pdef.get("clock")),
            "pair": pdef.get("pair"), "pol": pdef.get("pol"),
            "iostandard": (io.get(net) or {}).get("iostandard"),
        })
    rows.sort(key=lambda r: (r["net"], r["pin"]))
    return rows


# ── checks ───────────────────────────────────────────────────────────────────────

_CLK_NET_RE = re.compile(r"CLK|CLOCK", re.IGNORECASE)
_DIFF_P_RE = re.compile(r"^(.*?)(?:_P|\+)$", re.IGNORECASE)
_DIFF_N_RE = re.compile(r"^(.*?)(?:_N|-)$", re.IGNORECASE)


def _viol(check: str, ref: str, msg: str, **detail: Any) -> dict:
    d = {"check": check, "ref": ref, "message": msg}
    d.update(detail)
    return d


def fpga_check(netlist: dict, ref: str = "", device: dict | None = None,
               io: dict | None = None) -> dict:
    """Run all FPGA checks. With no ``ref``, checks every FPGA found in the netlist.

    Returns ``{"ok", "devices": [...], "violations": [...], "warnings": [...]}``.
    Each violation: ``{check, ref, message, ...detail}`` with ``check`` one of
    ``bank_vccio | diff_pair | clock | config``. Partial device seeds SKIP the checks
    their ``capabilities`` say they can't do (surfaced as warnings, never guesses).
    """
    targets: list[tuple[str, dict]]
    if ref and device is not None:
        targets = [(ref, device)]
    elif ref:
        d = None
        for r, dd in _find_fpgas(netlist):
            if r == ref:
                d = dd
        if d is None:
            return {"ok": False, "devices": [],
                    "violations": [_viol("device", ref, f"no FPGA device model found for {ref!r}")],
                    "warnings": []}
        targets = [(ref, d)]
    else:
        targets = _find_fpgas(netlist)

    violations: list[dict] = []
    warnings: list[str] = []
    devices: list[dict] = []
    for r, dev in targets:
        v, w = _check_one(netlist, r, dev, io or {})
        violations += v
        warnings += w
        devices.append({"ref": r, "device": dev.get("device"),
                        "coverage": dev.get("coverage")})
    return {"ok": not violations, "devices": devices,
            "violations": violations, "warnings": sorted(warnings)}


def _check_one(netlist: dict, ref: str, dev: dict, io: dict) -> tuple[list[dict], list[str]]:
    violations: list[dict] = []
    warnings: list[str] = []
    pins = dev.get("pins", {})
    caps = dev.get("capabilities", {})
    conn = _comp_pins(netlist, ref)
    devid = dev.get("device", "?")
    if dev.get("coverage") != "full":
        warnings.append(f"{ref} ({devid}): PARTIAL pin-table seed — pins absent from the "
                        "model are not checked")

    # ── 1. bank VCCIO ──
    # bank VCCIO voltage: inferred from the net on the bank's VCCIO pins (net-name volts).
    bank_v: dict[str, float | None] = {}
    for bank, bdef in (dev.get("banks") or {}).items():
        volts: set[float] = set()
        vnets: set[str] = set()
        for vp in bdef.get("vccio_pins", []):
            net = conn.get(str(vp))
            if not net:
                continue
            vnets.add(net)
            v = voltage_from_net(net)
            if v is not None:
                volts.add(v)
        if len(vnets) > 1:
            violations.append(_viol(
                "bank_vccio", ref,
                f"bank {bank} VCCIO pins are on {len(vnets)} different nets "
                f"({', '.join(sorted(vnets))}) — a bank has ONE VCCIO",
                bank=bank, nets=sorted(vnets)))
        if len(volts) > 1:
            violations.append(_viol(
                "bank_vccio", ref,
                f"bank {bank} VCCIO nets imply conflicting voltages {sorted(volts)}",
                bank=bank, volts=sorted(volts)))
        bv = next(iter(volts)) if len(volts) == 1 else None
        allowed = bdef.get("vccio_v_allowed") or []
        if bv is not None and allowed and bv not in allowed:
            violations.append(_viol(
                "bank_vccio", ref,
                f"bank {bank} VCCIO {bv} V is not a legal VCCIO for {devid} "
                f"(allowed: {allowed})", bank=bank, volts=[bv]))
        bank_v[bank] = bv

    # every IO pin's required voltage (IOSTANDARD, else net-name volts) vs its bank VCCIO
    for row in assignments_from_netlist(netlist, ref, dev, io):
        need = _iostd_voltage(row["iostandard"]) if row["iostandard"] else None
        if need is None:
            need = voltage_from_net(row["net"])
        bank = row["bank"]
        have = bank_v.get(bank)
        if need is not None and have is not None and abs(need - have) > 1e-6:
            violations.append(_viol(
                "bank_vccio", ref,
                f"pin {row['pin']} ({row['name']}, bank {bank}) net {row['net']!r} needs "
                f"{need} V IO but bank VCCIO is {have} V",
                bank=bank, pin=row["pin"], net=row["net"], need_v=need, vccio_v=have))

    # ── 2. diff pairs ──
    if caps.get("diff_pairs_known", True):
        # intended pairs from net naming: base -> {p: (pin,row), n: (pin,row)}
        want: dict[str, dict[str, dict]] = {}
        for row in assignments_from_netlist(netlist, ref, dev, io):
            for pol, rx in (("p", _DIFF_P_RE), ("n", _DIFF_N_RE)):
                m = rx.match(row["net"])
                if m and m.group(1):
                    want.setdefault(m.group(1).upper(), {})[pol] = row
        for base, half in sorted(want.items()):
            if len(half) != 2:
                continue  # a lone *_P net isn't necessarily differential
            rp, rn = half["p"], half["n"]
            if not rp["pair"] or rp["pair"] != rn["pair"]:
                violations.append(_viol(
                    "diff_pair", ref,
                    f"diff nets {rp['net']}/{rn['net']} are on pins {rp['pin']}/{rn['pin']} "
                    f"({rp['name']}/{rn['name']}) which are NOT a true pair on {devid}",
                    nets=[rp["net"], rn["net"]], pins=[rp["pin"], rn["pin"]]))
            elif rp["pol"] != "p" or rn["pol"] != "n":
                violations.append(_viol(
                    "diff_pair", ref,
                    f"diff nets {rp['net']}/{rn['net']} land on pair {rp['pair']} with "
                    f"swapped polarity (P on '{rp['pol']}' pin, N on '{rn['pol']}' pin)",
                    nets=[rp["net"], rn["net"]], pins=[rp["pin"], rn["pin"]],
                    pair=rp["pair"]))
    else:
        warnings.append(f"{ref} ({devid}): diff-pair data not populated — diff check skipped")

    # ── 3. clocks ──
    if caps.get("clock_pins_known", True):
        for row in assignments_from_netlist(netlist, ref, dev, io):
            if _CLK_NET_RE.search(row["net"]) and not row["clock"]:
                violations.append(_viol(
                    "clock", ref,
                    f"clock net {row['net']!r} enters on pin {row['pin']} ({row['name']}) "
                    f"which is not clock-capable on {devid}",
                    pin=row["pin"], net=row["net"]))
    else:
        warnings.append(f"{ref} ({devid}): clock-capable pins not populated — clock check "
                        "skipped")

    # ── 4. config / JTAG ──
    # "wired" = on a net with at least one OTHER member. An unconnected pin still gets
    # an auto single-member N$<n> net from the netlist builder — that is NOT wired.
    nets = netlist.get("nets", {})

    def _wired(pin: str) -> bool:
        net = conn.get(str(pin))
        return bool(net) and len(nets.get(net, [])) >= 2

    req = (dev.get("config") or {}).get("required") or {}
    for pin, rdef in sorted(req.items()):
        if not _wired(str(pin)):
            violations.append(_viol(
                "config", ref,
                f"required config pin {pin} ({rdef.get('role')}) is unconnected — "
                f"{rdef.get('note') or 'wire per datasheet'}",
                pin=pin, role=rdef.get("role")))
    notes = (dev.get("config") or {}).get("notes")
    if notes and not req:
        warnings.append(f"{ref} ({devid}): {notes}")

    return violations, warnings


# ── constraint exporters ─────────────────────────────────────────────────────────

_DEFAULT_IOSTD = {"xdc": "LVCMOS33", "lpf": "LVCMOS33"}


def _port(net: str) -> str:
    """Sanitize a net name into a legal HDL port name (deterministic)."""
    s = re.sub(r"[^A-Za-z0-9_\[\]]", "_", net)
    return s if s and not s[0].isdigit() else f"n_{s}"


def to_xdc(assignments: list[dict], device: dict) -> str:
    """Xilinx XDC pin constraints (deterministic, one PACKAGE_PIN+IOSTANDARD per port)."""
    out = [f"## Generated by Chudware fpga.py — {device.get('device')} "
           f"({device.get('package')}); board netlist is the source of truth.",
           "## Pin table sources: " + "; ".join(device.get("sources", [])[:1]), ""]
    for r in sorted(assignments, key=lambda r: (r["net"], r["pin"])):
        std = r.get("iostandard") or _DEFAULT_IOSTD["xdc"]
        port = _port(r["net"])
        out.append(
            f"set_property -dict {{ PACKAGE_PIN {r['pin']}   IOSTANDARD {std} }} "
            f"[get_ports {{ {port} }}]; # {r.get('name')}"
            + (" [clock-capable]" if r.get("clock") else ""))
    return "\n".join(out) + "\n"


def to_lpf(assignments: list[dict], device: dict) -> str:
    """Lattice LPF pin constraints (LOCATE COMP + IOBUF PORT lines, deterministic)."""
    out = [f"# Generated by Chudware fpga.py — {device.get('device')} "
           f"({device.get('package')}); board netlist is the source of truth.",
           "BLOCK RESETPATHS;", "BLOCK ASYNCPATHS;", ""]
    for r in sorted(assignments, key=lambda r: (r["net"], r["pin"])):
        std = r.get("iostandard") or _DEFAULT_IOSTD["lpf"]
        port = _port(r["net"])
        out.append(f'LOCATE COMP "{port}" SITE "{r["pin"]}";'
                   f'  # {r.get("name")}' + (" [clock-capable]" if r.get("clock") else ""))
        out.append(f'IOBUF PORT "{port}" IO_TYPE={std};')
    return "\n".join(out) + "\n"


def to_csv(assignments: list[dict], device: dict) -> str:
    """Neutral CSV pinout: pin,net,port,site,bank,clock_capable,diff_pair,polarity,iostandard."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["pin", "net", "port", "site", "bank", "clock_capable",
                "diff_pair", "polarity", "iostandard"])
    for r in sorted(assignments, key=lambda r: (r["net"], r["pin"])):
        w.writerow([r["pin"], r["net"], _port(r["net"]), r.get("name") or "",
                    r.get("bank") or "", "yes" if r.get("clock") else "no",
                    r.get("pair") or "", r.get("pol") or "",
                    r.get("iostandard") or ""])
    return buf.getvalue()


def export_constraints(netlist: dict, ref: str = "", device: dict | None = None,
                       io: dict | None = None) -> dict[str, dict[str, str]]:
    """All constraint texts for the FPGA(s) in a netlist.

    Returns ``{ref: {"xdc"|"lpf": <native dialect text>, "csv": <neutral pinout>}}`` —
    the native dialect comes from the device's ``constraint_dialect``; CSV always ships.
    """
    if ref and device is not None:
        targets = [(ref, device)]
    else:
        targets = [(r, d) for r, d in _find_fpgas(netlist) if not ref or r == ref]
    out: dict[str, dict[str, str]] = {}
    for r, dev in targets:
        rows = assignments_from_netlist(netlist, r, dev, io)
        files = {"csv": to_csv(rows, dev)}
        dialect = dev.get("constraint_dialect")
        if dialect == "xdc":
            files["xdc"] = to_xdc(rows, dev)
        elif dialect == "lpf":
            files["lpf"] = to_lpf(rows, dev)
        out[r] = files
    return out
