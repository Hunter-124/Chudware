"""Firmware ↔ board co-verification (T-FIN6) — static pin checks + emulated-trace diff.

Closes the one-way ``pinmap.py`` loop: the board generated the firmware's ``PIN_<NET>``
constants; this module checks the firmware BACK against the board before fab.

Two passes, both returning findings in the exact ERC/DRC violation shape
(``engine.ecad.erc`` — ``{"rule", "severity", "net", "pins", "xy", "message"}``) extended
append-only with a ``"fix"`` hint string:

* **Static** (:func:`static_checks`): every firmware-referenced ``PIN_<NET>`` exists on the
  board and lands on the MCU footprint; firmware write direction vs the net's other drivers
  (two push-pull drivers, driving an input-only pin); boot-strap / reset pins not repurposed
  as signals; required decoupling per MCU supply rail (T-FIN8 eng metadata).
* **Dynamic** (:func:`dynamic_checks`): diff observed pin activity from a T-FIN1 emulation
  trace (``aicad.firmware.trace/1`` — live ``fw_sim_run`` or an existing ``<elf>.trace.json``)
  against the same netlist expectations.

Rule catalog (see ``contracts/firmware_rpc.md`` §9):

  fw_pin_unknown_net    error    PIN_<X> has no matching board net
  fw_pin_not_on_mcu     warning  net exists but touches no MCU footprint pad
  fw_strap_repurposed   error    boot-strap/reset pin used as a general signal
  fw_direction_clash    error    firmware drives a net that has another push-pull driver
  fw_drive_input_only   error    firmware drives a physical pin with no output driver
  fw_missing_decouple   warning  MCU supply rail with no decoupling capacitor
  fw_trace_drive_conflict error  emulation observed the firmware driving a conflicted net
  fw_trace_unmapped_pin warning  emulation observed activity on a pin with no board net

Pure + advisory, mirroring ERC: deterministic ordering, never raises on a bad design, never
mutates inputs. Direction expectations need the linked *schematic* (pin types live on symbol
pins, not board pads); without one those checks degrade to skipped, honestly — no guessing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from engine.firmware import mcu_pinouts, pinmap

# ── violation shape (ERC-compatible + append-only "fix" hint) ───────────────────

_SEV_ERROR = "error"
_SEV_WARNING = "warning"

# Ground-ish rail names — supply rails that need no decoupling check of their own.
_GROUND_NAMES = {"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND", "EGND", "VSS", "VSSA",
                 "EARTH", "CHASSIS"}


def _violation(rule: str, severity: str, net: Optional[str], pins: list[str],
               xy: Any, message: str, fix: str = "") -> dict:
    return {"rule": rule, "severity": severity, "net": net,
            "pins": sorted(pins), "xy": list(xy) if xy is not None else None,
            "message": message, "fix": fix}


def _sorted(out: list[dict]) -> list[dict]:
    out.sort(key=lambda v: (v["rule"], v["net"] or "", tuple(v["pins"]),
                            tuple(v["xy"]) if v["xy"] else ()))
    return out


# ── firmware source scan ────────────────────────────────────────────────────────

_PIN_REF_RE = re.compile(r"\bPIN_([A-Za-z0-9_]+)\b")
# Direction heuristics: GPIO write/read call names across the HALs the templates target
# (STM32 HAL, bare-metal gpio_*, Arduino, RP2040 SDK, our own trace shim).
_WRITE_RE = re.compile(
    r"\b(?:HAL_GPIO_WritePin|HAL_GPIO_TogglePin|LL_GPIO_SetOutputPin|LL_GPIO_ResetOutputPin|"
    r"gpio_set|gpio_clear|gpio_write|gpio_toggle|gpio_put|digitalWrite|AICAD_GPIO_TRACE)\b")
_READ_RE = re.compile(
    r"\b(?:HAL_GPIO_ReadPin|LL_GPIO_ReadInputPort|gpio_read|gpio_get|digitalRead)\b")
_MODE_OUT_RE = re.compile(
    r"\b(?:pinMode|gpio_set_dir|gpio_mode)\s*\(\s*PIN_([A-Za-z0-9_]+)\s*,\s*"
    r"(?:OUTPUT|GPIO_OUT|GPIO_MODE_OUTPUT\w*)\b")
_MODE_IN_RE = re.compile(
    r"\b(?:pinMode|gpio_set_dir|gpio_mode)\s*\(\s*PIN_([A-Za-z0-9_]+)\s*,\s*"
    r"(?:INPUT\w*|GPIO_IN|GPIO_MODE_INPUT\w*)\b")


def scan_firmware_pins(sources: dict[str, str]) -> dict[str, dict]:
    """Scan firmware source text for ``PIN_<TOKEN>`` references and direction hints.

    ``sources`` is ``{filename: text}`` (``pinmap.h`` itself should not be included — its
    #defines are declarations, not uses). Returns ``{token: {"reads", "writes", "refs",
    "files"}}`` where ``writes``/``reads`` count lines whose GPIO call context indicates the
    firmware drives / samples the pin (an OUTPUT ``pinMode``/``gpio_set_dir`` counts as a
    write intent). Pure — no I/O.
    """
    out: dict[str, dict] = {}

    def _rec(token: str) -> dict:
        return out.setdefault(token, {"reads": 0, "writes": 0, "refs": 0, "files": []})

    for fname, text in sorted(sources.items()):
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            tokens = _PIN_REF_RE.findall(line)
            if not tokens:
                continue
            is_write = bool(_WRITE_RE.search(line))
            is_read = bool(_READ_RE.search(line))
            mode_out = set(_MODE_OUT_RE.findall(line))
            mode_in = set(_MODE_IN_RE.findall(line))
            for tok in tokens:
                rec = _rec(tok)
                rec["refs"] += 1
                if is_write or tok in mode_out:
                    rec["writes"] += 1
                if is_read or tok in mode_in:
                    rec["reads"] += 1
                if fname not in rec["files"]:
                    rec["files"].append(fname)
    return out


def project_sources(project) -> dict[str, str]:
    """Read a :class:`engine.firmware.project.FirmwareProject`'s sources as ``{relpath: text}``.
    Unreadable files are skipped (never raises)."""
    out: dict[str, str] = {}
    try:
        for rel, ap in zip(project.src_paths, project.abs_sources):
            try:
                out[str(rel)] = Path(ap).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    except Exception:
        pass
    return out


# ── board-side indexes ──────────────────────────────────────────────────────────


def _mcu_footprints(board: dict) -> list[dict]:
    return [fp for fp in board.get("footprints", [])
            if isinstance(fp, dict) and mcu_pinouts.component_family(fp)]


def _fp_nets(fp: dict) -> set[str]:
    return {str(p.get("net")) for p in fp.get("pads", [])
            if isinstance(p, dict) and p.get("net")}


def _is_capacitor(fp: dict) -> bool:
    fpid = str(fp.get("fpid") or "")
    ref = str(fp.get("ref") or "")
    return ("Capacitor" in fpid or fpid.split(":")[-1].startswith(("C_", "CP_"))
            or re.match(r"^C\d", ref) is not None)


def _external_strong_drivers(sch: Optional[dict], net: str,
                             mcu_refs: set[str]) -> list[str]:
    """Pinrefs of non-MCU push-pull drivers (``output``/``power_out``) on ``net`` per the
    linked schematic's netlist. Empty when no schematic is available (honest skip)."""
    if not isinstance(sch, dict):
        return []
    try:
        from engine.ecad import netlist as netlist_mod, schema
        if not schema.is_schematic(sch):
            return []
        nl = netlist_mod.build_netlist(sch)
    except Exception:
        return []
    sym_ref = {s.get("id"): str(s.get("ref", "")) for s in sch.get("symbols", [])}
    out = []
    for m in nl.get("nets", {}).get(net, []):
        if str(m.get("type", "passive")) not in ("output", "power_out"):
            continue
        ref = sym_ref.get(m.get("sym"), "")
        if ref in mcu_refs:
            continue  # the MCU's own pin is the firmware side, not an external driver
        out.append(f"{m.get('sym', '')}.{m.get('pin', '')}")
    return sorted(out)


def _mcu_sch_refs(sch: Optional[dict]) -> set[str]:
    if not isinstance(sch, dict):
        return set()
    return {str(s.get("ref", "")) for s in sch.get("symbols", [])
            if isinstance(s, dict) and mcu_pinouts.component_family(s)}


# ── static checks ───────────────────────────────────────────────────────────────


def static_checks(board: dict, fw_pins: dict[str, dict], *,
                  sch: Optional[dict] = None,
                  mcu_family: Optional[str] = None) -> list[dict]:
    """Static firmware↔board checks. ``fw_pins`` is :func:`scan_firmware_pins` output.

    Pure + deterministic; returns ERC-shaped violations (module docstring rule catalog).
    """
    out: list[dict] = []
    family = mcu_family or mcu_pinouts.select_mcu_family(board)
    model = {rec["token"]: rec for rec in pinmap.pin_model(board, mcu_family=family)}
    mcus = _mcu_footprints(board)
    mcu_nets: set[str] = set()
    for fp in mcus:
        mcu_nets |= _fp_nets(fp)
    mcu_refs = _mcu_sch_refs(sch) | {str(fp.get("ref", "")) for fp in mcus}

    # per firmware-referenced pin
    for token in sorted(fw_pins):
        use = fw_pins[token]
        rec = model.get(token)
        if rec is None:
            out.append(_violation(
                "fw_pin_unknown_net", _SEV_ERROR, None, [f"PIN_{token}"], None,
                f"firmware references PIN_{token} but no board net produces that pin-map "
                f"token (power rails are never emitted)",
                fix=f"add/rename a net matching PIN_{token} on the board, or drop the "
                    f"firmware reference; regenerate pinmap.h"))
            continue
        net = rec["net"]
        if mcus and net not in mcu_nets:
            out.append(_violation(
                "fw_pin_not_on_mcu", _SEV_WARNING, net, [f"PIN_{token}"], None,
                f"net {net} exists but is not connected to any pad of the MCU footprint"
                f" ({', '.join(sorted(str(fp.get('ref', '?')) for fp in mcus))})",
                fix=f"wire net {net} to the intended MCU pin, or stop referencing "
                    f"PIN_{token} in firmware"))
        if rec["strap"] and (use.get("writes") or use.get("reads")):
            out.append(_violation(
                "fw_strap_repurposed", _SEV_ERROR, net, [f"PIN_{token}"], None,
                f"boot-strap/reset pin {net} ({family or 'unknown family'}) is used as a "
                f"general GPIO by firmware (reads={use.get('reads', 0)}, "
                f"writes={use.get('writes', 0)}) — its level at reset selects the boot mode",
                fix=f"move the signal to a free GPIO; keep {net} on its strap "
                    f"resistor/reset circuit only"))
        if use.get("writes"):
            if rec["input_only"]:
                out.append(_violation(
                    "fw_drive_input_only", _SEV_ERROR, net, [f"PIN_{token}"], None,
                    f"firmware drives PIN_{token} but {net} is an input-only pin on "
                    f"{family} (no output driver exists)",
                    fix=f"move the output to a pin with an output driver; input-only "
                        f"pins can only sample"))
            drivers = _external_strong_drivers(sch, net, mcu_refs)
            if drivers:
                out.append(_violation(
                    "fw_direction_clash", _SEV_ERROR, net,
                    [f"PIN_{token}", *drivers], None,
                    f"firmware drives net {net} which already has push-pull driver(s) "
                    f"{', '.join(drivers)} — two drivers fighting (output-output short)",
                    fix=f"configure PIN_{token} as input (the peripheral drives {net}), "
                        f"or rewire the peripheral pin"))

    # decoupling per MCU supply rail (T-FIN8 metadata + power pads)
    out.extend(_decoupling_checks(board, mcus))
    return _sorted(out)


def _decoupling_checks(board: dict, mcus: list[dict]) -> list[dict]:
    """One ``fw_missing_decouple`` warning per MCU supply rail with no capacitor on it.

    Rails come from the MCU's T-FIN8 eng metadata (``supply.rails`` names, when known) plus
    any power-named net on the MCU's own pads; ground rails are exempt.
    """
    out: list[dict] = []
    caps_nets: set[str] = set()
    for fp in board.get("footprints", []):
        if isinstance(fp, dict) and _is_capacitor(fp):
            caps_nets |= _fp_nets(fp)
    board_nets = {str(n.get("name")) for n in board.get("nets", [])
                  if isinstance(n, dict) and n.get("name")}

    for fp in mcus:
        ref = str(fp.get("ref", "?"))
        rails: set[str] = set()
        # T-FIN8 eng metadata: supply.rails carries the datasheet rail names
        try:
            from engine.ecad import metadata
            eng = metadata.eng_for_footprint(fp)
            for rail in eng.get("supply", {}).get("rails") or []:
                nm = rail.get("name")
                if nm:
                    rails.add(str(nm))
        except Exception:
            pass
        # plus power nets actually landing on the MCU's pads
        for net in _fp_nets(fp):
            if pinmap.is_power_net(net):
                rails.add(net)
        for rail in sorted(rails):
            if rail.upper() in _GROUND_NAMES:
                continue
            if rail not in board_nets:
                continue  # metadata rail name with no matching board net — not checkable here
            if rail not in caps_nets:
                out.append(_violation(
                    "fw_missing_decouple", _SEV_WARNING, rail, [ref], None,
                    f"MCU {ref} supply rail {rail} has no decoupling capacitor on the "
                    f"board (checked all capacitor footprints' pads)",
                    fix=f"place a 100nF (plus bulk) capacitor between {rail} and GND "
                        f"close to {ref}'s supply pins"))
    return out


# ── dynamic checks (trace vs netlist expectations) ──────────────────────────────


def load_trace(trace_path: str) -> Optional[dict]:
    """Load an ``aicad.firmware.trace/1`` artifact; None on any failure (never raises)."""
    try:
        data = json.loads(Path(trace_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _trace_driven_pins(trace: dict) -> list[str]:
    """Pin names the emulation observed transitioning (gpio events), sorted."""
    pins: set[str] = set()
    for ev in trace.get("events", []) or trace.get("gpio_events", []) or []:
        if isinstance(ev, dict) and ev.get("type", "gpio") == "gpio" and ev.get("pin"):
            pins.add(str(ev["pin"]))
    return sorted(pins)


def dynamic_checks(board: dict, trace: dict, *, sch: Optional[dict] = None,
                   mcu_family: Optional[str] = None) -> list[dict]:
    """Diff observed emulated pin activity against the board's netlist expectations.

    ``trace`` is a T-FIN1 trace artifact dict (``aicad.firmware.trace/1``) or any dict with
    ``gpio_events``/``events`` (a live ``SimResult`` dict works). An observed transition on
    pin ``PXn`` means the firmware DROVE that pin; we map the pin name back to a board net
    (exact net-name match first, then decoded-pin-id equality) and check the net could accept
    a driver. Pure + deterministic.
    """
    out: list[dict] = []
    family = mcu_family or mcu_pinouts.select_mcu_family(board)
    nets = [str(n.get("name")) for n in board.get("nets", [])
            if isinstance(n, dict) and n.get("name")]
    # decoded pin-id → net name (positional nets only)
    id_to_net: dict[int, str] = {}
    for n in nets:
        pid = mcu_pinouts.resolve_pin(n, family)
        if pid is not None and pid not in id_to_net:
            id_to_net[pid] = n
    mcus = _mcu_footprints(board)
    mcu_refs = _mcu_sch_refs(sch) | {str(fp.get("ref", "")) for fp in mcus}

    for pin in _trace_driven_pins(trace):
        net: Optional[str] = pin if pin in nets else None
        if net is None:
            pid = mcu_pinouts.resolve_pin(pin, family)
            if pid is not None:
                net = id_to_net.get(pid)
        if net is None:
            out.append(_violation(
                "fw_trace_unmapped_pin", _SEV_WARNING, None, [pin], None,
                f"emulation observed activity on pin {pin} but no board net corresponds "
                f"to it — the firmware toggles a pin the board leaves unconnected",
                fix=f"route pin {pin} to its intended net, or remove the firmware output"))
            continue
        problems: list[str] = []
        pins_involved = [pin]
        if mcu_pinouts.is_strap_net(net, family):
            problems.append("it is a boot-strap/reset pin")
        drivers = _external_strong_drivers(sch, net, mcu_refs)
        if drivers:
            problems.append(f"push-pull driver(s) {', '.join(drivers)} also drive it")
            pins_involved += drivers
        if mcu_pinouts.is_input_only_pin(net, family):
            problems.append("the net expects an input-only pin")
        if problems:
            out.append(_violation(
                "fw_trace_drive_conflict", _SEV_ERROR, net, pins_involved, None,
                f"emulation observed the firmware driving {pin} (net {net}) but "
                f"{'; '.join(problems)}",
                fix=f"stop driving {pin} in firmware, or rewire net {net} so it has a "
                    f"single driver"))
    return _sorted(out)


# ── top-level entry ─────────────────────────────────────────────────────────────


def verify_board(board: dict, fw_pins: dict[str, dict], *,
                 sch: Optional[dict] = None, mcu_family: Optional[str] = None,
                 trace: Optional[dict] = None) -> list[dict]:
    """Full co-verification: static checks always; dynamic checks when a trace is given.

    Returns one merged, deterministically-ordered findings list (ERC violation shape +
    ``fix`` hint). Advisory — callers surface findings, nothing is blocked.
    """
    out = static_checks(board, fw_pins, sch=sch, mcu_family=mcu_family)
    if isinstance(trace, dict):
        out.extend(dynamic_checks(board, trace, sch=sch, mcu_family=mcu_family))
    return _sorted(out)


def summarize(findings: list[dict]) -> dict:
    """`{n, errors, warnings, rules}` rollup for tool responses."""
    return {
        "n": len(findings),
        "errors": sum(1 for f in findings if f.get("severity") == _SEV_ERROR),
        "warnings": sum(1 for f in findings if f.get("severity") == _SEV_WARNING),
        "rules": sorted({f.get("rule", "") for f in findings}),
    }
