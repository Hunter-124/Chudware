"""Loss-aware EPANET 2.2 INP interchange for the SI civil water contract.

Reference: Rossman et al., *EPANET 2.2 User Manual* (EPA, 2020),
Appendix A and Appendix C.2; curve semantics are in section 3.2:
https://19january2021snapshot.epa.gov/sites/static/files/2020-05/documents/epanet_userss_manual_2.2.0.pdf
Engine conventions that differ from the manual (MINPDIFF, default trials,
absolute/relative viscosity, and pump OPEN resetting speed) follow the
archived EPA 2.2 ``input1.c``, ``input3.c``, and ``types.h``:
https://github.com/USEPA/EPANET2.2/tree/master/SRC_engines/src/solver

US pressure settings are psi, unlike reservoir heads and tank levels (ft).
The conversion uses EPANET's 0.4333 psi/ft convention. Map coordinates have
no prescribed units in EPANET; absent civil/map metadata we explicitly
assume the hydraulic length units. BACKDROP UNITS None/Degrees retains
unscaled coordinates separately rather than claiming metre/CRS geometry.
Unsupported records are retained verbatim in
``epanet_passthrough`` and reported in ``dropped`` (meaning not interpreted,
not discarded). Active hydraulic passthrough must be refused by a solver.

The writer emits ordinary INP records plus an explicit, hash-bound civil
JSON comment. The comment preserves metadata, optional-field presence,
collection ordering, and exact SI floating-point values. Ordinary records
are always parsed independently before an unchanged snapshot is restored;
editing ordinary INP records invalidates the snapshot rather than hiding
an edit. This adapter does not execute hydraulic or water-quality analysis.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from .errors import CivilError

__all__ = ["read", "write"]

_FLOW = {
    "GPM": 0.003785411784 / 60.0,
    "CFS": 0.3048 ** 3,
    "MGD": 0.003785411784 * 1e6 / 86400.0,
    "IMGD": 0.00454609 * 1e6 / 86400.0,
    "AFD": 43560.0 * 0.3048 ** 3 / 86400.0,
    "LPS": 0.001,
    "LPM": 0.001 / 60.0,
    "MLD": 1000.0 / 86400.0,
    "CMH": 1.0 / 3600.0,
    "CMD": 1.0 / 86400.0,
}
_US = {"GPM", "CFS", "MGD", "IMGD", "AFD"}
_SUPPORTED = {
    "TITLE", "JUNCTIONS", "RESERVOIRS", "TANKS", "PIPES", "PUMPS",
    "VALVES", "PATTERNS", "CURVES", "DEMANDS", "STATUS", "OPTIONS",
    "TIMES", "COORDINATES", "VERTICES", "CONTROLS", "END",
}
_NONHYDRAULIC = {
    "QUALITY", "REACTIONS", "SOURCES", "MIXING", "ENERGY", "REPORT",
    "LABELS", "BACKDROP", "TAGS",
}
_NONHYDRAULIC_OPTIONS = {
    "QUALITY", "DIFFUSIVITY", "TOLERANCE", "MAP", "CHECKFREQ", "MAXCHECK",
    "DAMPLIMIT", "UNBALANCED", "EMITTER",
}
_MARKER = ";@CHUDWARE_CIVIL/1 "
_REFERENCE = "EPANET 2.2 User Manual (EPA, 2020), Appendix A and C.2"
_TIME_FIELDS = {
    "DURATION": "duration_s",
    "HYDRAULIC TIMESTEP": "hydraulic_step_s",
    "QUALITY TIMESTEP": "quality_step_s",
    "RULE TIMESTEP": "rule_step_s",
    "PATTERN TIMESTEP": "pattern_step_s",
    "PATTERN START": "pattern_start_s",
    "REPORT TIMESTEP": "report_step_s",
    "REPORT START": "report_start_s",
    "START CLOCKTIME": "start_clocktime_s",
}


def _bad(hint: str, **extra: Any) -> None:
    raise CivilError("epanet_bad_syntax", hint, **extra)


def _unsupported(feature: str, **extra: Any) -> None:
    raise CivilError("epanet_unsupported_feature", "This INP feature cannot be represented safely.", feature=feature, **extra)


def _number(value: Any, *, line: int | None = None) -> float:
    if isinstance(value, bool):
        _bad("Expected a finite number, not a boolean.", line=line)
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        _bad("Expected a finite number.", value=value, line=line)
    if not math.isfinite(result):
        _bad("Non-finite numbers are not allowed.", value=str(value), line=line)
    return result


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or re.search(r"\s|[;\[\]]", value):
        _bad("INP identifiers must be nonempty strings without whitespace, semicolons, or brackets.", value=value)
    return value


def _count(tokens: list[str], minimum: int, maximum: int, line: int) -> None:
    if not minimum <= len(tokens) <= maximum:
        _bad("Wrong number of fields in an INP record.", line=line, fields=len(tokens), expected=[minimum, maximum])


def _time(tokens: list[str], line: int | None = None, *, clock: bool = False) -> float:
    if not tokens or len(tokens) > 2:
        _bad("Expected a time with an optional unit or AM/PM suffix.", line=line)
    if ":" in tokens[0]:
        parts = tokens[0].split(":")
        if len(parts) not in (2, 3):
            _bad("Time must be hours:minutes or hours:minutes:seconds.", line=line)
        values = [_number(v, line=line) for v in parts]
        if any(v < 0 for v in values) or any(v >= 60 for v in values[1:]):
            _bad("Invalid minute or second in time.", line=line)
        result = values[0] * 3600 + values[1] * 60 + (values[2] if len(values) == 3 else 0)
        if len(tokens) == 2 and tokens[1].upper() not in {"AM", "PM"}:
            _bad("Colon-form times cannot also specify duration units.", line=line)
    else:
        unit = tokens[1].upper() if len(tokens) == 2 else "HOURS"
        factors = {"SECONDS": 1, "SECOND": 1, "SEC": 1, "MINUTES": 60, "MINUTE": 60, "MIN": 60,
                   "HOURS": 3600, "HOUR": 3600, "HR": 3600, "DAYS": 86400, "DAY": 86400,
                   "AM": 3600, "PM": 3600}
        if unit not in factors:
            _bad("Unknown time unit.", unit=unit, line=line)
        result = _number(tokens[0], line=line) * factors[unit]
    if len(tokens) == 2 and tokens[1].upper() in {"AM", "PM"}:
        if not clock or not 3600 <= result < 13 * 3600:
            _bad("AM/PM applies only to a 12-hour clock time.", line=line)
        result = result % (12 * 3600) + (12 * 3600 if tokens[1].upper() == "PM" else 0)
    if result < 0 or (clock and result >= 86400):
        _bad("Time is outside its valid range.", line=line)
    return result


def _units(units: str, pressure: str | None = None) -> dict[str, float | str]:
    units = units.upper()
    if units not in _FLOW:
        _unsupported("flow_units", units=units)
    if pressure is not None and not isinstance(pressure, str):
        _bad("Pressure units must be a string.")
    pressure = (pressure or ("PSI" if units in _US else "METERS")).upper()
    pressure = {"M": "METERS", "METER": "METERS", "METRES": "METERS"}.get(pressure, pressure)
    if pressure not in {"PSI", "METERS", "KPA"}:
        _unsupported("pressure_units", units=pressure)
    requested_pressure = pressure
    if units in _US:
        pressure = "PSI"
    elif pressure == "PSI":
        pressure = "METERS"
    return {
        "flow": _FLOW[units], "length": 0.3048 if units in _US else 1.0,
        "diameter": 0.0254 if units in _US else 0.001,
        "roughness": 0.0003048 if units in _US else 0.001,
        "pressure": {"PSI": 0.3048 / 0.4333, "METERS": 1.0, "KPA": 0.3048 / 0.4333 / 6.895}[pressure],
        "pressure_units": pressure, "pressure_units_requested": requested_pressure,
    }


def _source(path_or_text: str | os.PathLike[str]) -> tuple[str, str]:
    if not isinstance(path_or_text, (str, os.PathLike)):
        _bad("read expects INP text or a filesystem path.")
    if isinstance(path_or_text, str) and (not path_or_text or "\n" in path_or_text or "\r" in path_or_text or path_or_text.lstrip().startswith(("[", ";"))):
        return path_or_text, "text"
    path = Path(path_or_text)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return stream.read(), str(path)
    except (OSError, UnicodeError) as exc:
        raise CivilError("epanet_io_error", "Could not read the UTF-8 INP file.", path=str(path), detail=str(exc)) from exc


def _sections(text: str) -> tuple[list[dict[str, Any]], str, str]:
    sections: list[dict[str, Any]] = []
    metadata: list[str] = []
    body: list[str] = []
    current: dict[str, Any] | None = None
    ended = False
    for line, raw in enumerate(text.splitlines(keepends=True), 1):
        if raw.startswith(_MARKER):
            metadata.append(raw[len(_MARKER):].strip())
            continue
        body.append(raw)
        content = raw.split(";", 1)[0].strip()
        if content.startswith("["):
            match = re.fullmatch(r"\[([A-Za-z][A-Za-z0-9_ ]*)\]", content)
            if match is None:
                _bad("Malformed section header.", line=line)
            if ended:
                _bad("A section follows [END].", line=line)
            section = match.group(1).upper()
            current = {"section": section, "raw": raw, "rows": [], "line": line}
            sections.append(current)
            ended = section == "END"
        elif current is None:
            if content:
                _bad("Data precedes the first section header.", line=line)
        else:
            if ended and content:
                _bad("Data follows [END].", line=line)
            current["raw"] += raw
            current["rows"].append((line, raw, content.split()))
    return sections, "".join(metadata), "".join(body)


def _passthrough(section: str, text: str, *, active: bool | None = None, hydraulic: bool | None = None) -> dict[str, Any]:
    if active is None:
        active = any(row.split(";", 1)[0].strip() and not row.lstrip().startswith("[") for row in text.splitlines())
    if hydraulic is None:
        hydraulic = section not in _NONHYDRAULIC
    return {"section": section, "text": text, "active": active, "hydraulic": hydraulic}


def _curve_points(points: Any, kind: str, curve_id: str) -> list[list[float]]:
    if not isinstance(points, list) or not points:
        _bad("A curve requires at least one point.", curve_id=curve_id)
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            _bad("Curve points must be [x, y] pairs.", curve_id=curve_id)
        result.append([_number(point[0]), _number(point[1])])
    if any(b[0] <= a[0] for a, b in zip(result, result[1:])):
        _bad("Curve x values must be strictly increasing.", curve_id=curve_id)
    if kind == "pump":
        if len(result) == 2:
            _unsupported("two_point_pump_curve", curve_id=curve_id)
        if any(q < 0 or h < 0 for q, h in result) or any(b[1] >= a[1] for a, b in zip(result, result[1:])):
            _bad("Pump flow/head must be nonnegative and head must decrease with flow.", curve_id=curve_id)
        if len(result) == 1 and (result[0][0] <= 0 or result[0][1] <= 0):
            _bad("A one-point pump curve requires positive design flow and head.", curve_id=curve_id)
    return result


def read(path_or_text: str | os.PathLike[str]) -> dict[str, Any]:
    """Read INP text/path into ``{network, provenance, dropped}``.

    See EPANET 2.2 manual Appendix C.2. Multiple DEMANDS records replace
    the JUNCTIONS base demand; they become additive ``junction.demands``
    with a zero scalar base. Cylindrical tank diameter uses ft/m, *not*
    pipe diameter's in/mm. Simple tank-level and elapsed-time open/closed
    controls are interpreted; all other controls/rules remain explicit
    hydraulic passthrough. Unknown sections are never silently dropped.
    """
    text, source = _source(path_or_text)
    sections, encoded, body = _sections(text)
    if not any(s["section"] == "JUNCTIONS" for s in sections):
        _bad("An INP network requires a [JUNCTIONS] section.")
    rows: dict[str, list[tuple[int, str, list[str]]]] = {}
    for section in sections:
        rows.setdefault(section["section"], []).extend(section["rows"])
    units, pressure_units, headloss = "GPM", None, "H-W"
    for line, raw, tokens in rows.get("OPTIONS", []):
        if not tokens:
            continue
        key = tokens[0].upper()
        if key in {"UNITS", "PRESSURE", "HEADLOSS"} and not (key == "PRESSURE" and len(tokens) > 1 and tokens[1].upper() == "EXPONENT"):
            _count(tokens, 2, 2, line)
            if key == "UNITS":
                units = tokens[1].upper()
            elif key == "PRESSURE":
                pressure_units = tokens[1]
            else:
                headloss = tokens[1].upper()
    if headloss not in {"H-W", "D-W"}:
        _unsupported("headloss", headloss=headloss)
    factors = _units(units, pressure_units)
    qf, lf, df, rf, pf = (float(factors[k]) for k in ("flow", "length", "diameter", "roughness", "pressure"))
    options: dict[str, Any] = {
        "duration_s": 0.0, "hydraulic_step_s": 3600.0, "pattern_step_s": 3600.0, "report_step_s": 3600.0,
        "demand_model": "DDA", "p_min_m": 0.0, "p_req_m": 0.1 * pf,
        "pda_exponent": 0.5, "viscosity_m2s": 1.1e-5 * 0.3048 ** 2,
        "accuracy": 0.001, "max_iter": 200, "default_pattern": "1",
    }
    network: dict[str, Any] = {
        "id": "epanet", "name": "EPANET network", "headloss": headloss,
        "junctions": [], "reservoirs": [], "tanks": [], "pipes": [], "pumps": [], "valves": [],
        "patterns": {}, "curves": {}, "options": options, "title": [],
        "epanet_units": units, "epanet_pressure_units": factors["pressure_units"],
    }
    dropped: list[dict[str, Any]] = []
    passthrough: list[dict[str, Any]] = []
    comments: list[dict[str, str]] = []
    for raw in text.splitlines():
        if raw.split(";", 1)[0].strip().startswith("["):
            break
        if raw.lstrip().startswith(";") and not raw.startswith(_MARKER):
            comments.append({"section": "PREAMBLE", "text": raw})
    for section in sections:
        name = section["section"]
        if name not in _SUPPORTED:
            record = _passthrough(name, section["raw"])
            passthrough.append(record)
            dropped.append(dict(record))
        elif name != "CONTROLS":
            for _, raw, tokens in section["rows"]:
                if ";" in raw and not (tokens and name in {"TITLE", "DEMANDS"}):
                    comments.append({"section": name, "text": raw[raw.index(";"):].rstrip("\r\n")})
    for _, raw, tokens in rows.get("TITLE", []):
        if tokens:
            network["title"].append(raw.rstrip("\r\n"))
    if network["title"]:
        network["name"] = network["title"][0].split(";", 1)[0].strip()
    scalar_options = {"ACCURACY": ("accuracy", 1.0), "TRIALS": ("max_iter", 1.0),
                      "VISCOSITY": ("viscosity_m2s", 1.1e-5 * 0.3048 ** 2)}
    paired_options = {"MINIMUM PRESSURE": ("p_min_m", pf), "REQUIRED PRESSURE": ("p_req_m", pf),
                      "PRESSURE EXPONENT": ("pda_exponent", 1.0), "DEMAND MULTIPLIER": ("demand_multiplier", 1.0)}
    default_pattern_explicit = False
    for line, raw, tokens in rows.get("OPTIONS", []):
        if not tokens:
            continue
        key, pair = tokens[0].upper(), " ".join(tokens[:2]).upper()
        if key in {"UNITS", "HEADLOSS", "PRESSURE"} and pair != "PRESSURE EXPONENT":
            continue
        if key in scalar_options:
            _count(tokens, 2, 2, line)
            field, factor = scalar_options[key]
            raw_value = _number(tokens[1], line=line)
            if field == "viscosity_m2s" and raw_value <= 1e-3:
                factor = lf ** 2
            value = raw_value * factor
            if value <= 0 or (field == "max_iter" and value != int(value)):
                _bad("Hydraulic accuracy, viscosity, and integer trials must be positive.", line=line)
            options[field] = int(value) if field == "max_iter" else value
        elif pair in paired_options:
            _count(tokens, 3, 3, line)
            field, factor = paired_options[pair]
            options[field] = _number(tokens[2], line=line) * factor
        elif pair == "DEMAND MODEL":
            _count(tokens, 3, 3, line)
            if tokens[2].upper() not in {"DDA", "PDA"}:
                _unsupported("demand_model", value=tokens[2], line=line)
            options["demand_model"] = tokens[2].upper()
        elif key == "PATTERN":
            _count(tokens, 2, 2, line)
            options["default_pattern"] = None if tokens[1] == "0" else _identifier(tokens[1])
            default_pattern_explicit = True
        elif pair == "SPECIFIC GRAVITY":
            _count(tokens, 3, 3, line)
            if _number(tokens[2], line=line) != 1.0:
                _unsupported("specific_gravity", value=tokens[2], line=line)
            passthrough.append(_passthrough("OPTIONS", "[OPTIONS]\n" + raw, active=False, hydraulic=False))
        else:
            record = _passthrough("OPTIONS", "[OPTIONS]\n" + raw,
                                  hydraulic=key not in _NONHYDRAULIC_OPTIONS)
            passthrough.append(record)
            dropped.append(dict(record))
    for line, raw, tokens in rows.get("TIMES", []):
        if not tokens:
            continue
        key = tokens[0].upper() if tokens[0].upper() == "DURATION" else " ".join(tokens[:2]).upper()
        if key in _TIME_FIELDS:
            values = tokens[1:] if key == "DURATION" else tokens[2:]
            value = _time(values, line, clock=key == "START CLOCKTIME")
            if key.endswith("TIMESTEP") and value <= 0:
                _bad("Timesteps must be positive.", line=line)
            options[_TIME_FIELDS[key]] = value
        elif tokens[0].upper() == "STATISTIC":
            _count(tokens, 2, 2, line)
            if tokens[1].upper() not in {"NONE", "AVERAGED", "MINIMUM", "MAXIMUM", "RANGE"}:
                _bad("Unknown report statistic.", line=line)
            options["statistic"] = tokens[1].upper()
        else:
            record = _passthrough("TIMES", "[TIMES]\n" + raw)
            passthrough.append(record)
            dropped.append(dict(record))
    nodes: dict[str, dict[str, Any]] = {}
    links: dict[str, dict[str, Any]] = {}
    def add(collection: str, record: dict[str, Any], line: int) -> None:
        table = nodes if collection in {"junctions", "reservoirs", "tanks"} else links
        record["id"] = _identifier(record["id"])
        if record["id"] in table:
            _bad("Duplicate node or link identifier.", element_id=record["id"], line=line)
        table[record["id"]] = record
        network[collection].append(record)
    for line, _, tokens in rows.get("JUNCTIONS", []):
        if not tokens:
            continue
        _count(tokens, 2, 4, line)
        add("junctions", {"id": tokens[0], "x": None, "y": None, "elevation": _number(tokens[1], line=line) * lf,
                          "demand_m3s": _number(tokens[2], line=line) * qf if len(tokens) > 2 else 0.0,
                          "pattern": tokens[3] if len(tokens) > 3 and tokens[3] != "0" else None}, line)
    if not network["junctions"]:
        _bad("An INP network requires at least one junction.")
    for line, _, tokens in rows.get("RESERVOIRS", []):
        if not tokens:
            continue
        _count(tokens, 2, 3, line)
        add("reservoirs", {"id": tokens[0], "x": None, "y": None, "head": _number(tokens[1], line=line) * lf,
                           "pattern": tokens[2] if len(tokens) > 2 and tokens[2] != "0" else None}, line)
    for line, _, tokens in rows.get("TANKS", []):
        if not tokens:
            continue
        _count(tokens, 6, 9, line)
        record = {"id": tokens[0], "x": None, "y": None}
        record.update({field: _number(value, line=line) * lf for field, value in
                       zip(("elevation", "init_level", "min_level", "max_level", "diameter"), tokens[1:6])})
        record["min_volume_m3"] = _number(tokens[6], line=line) * lf ** 3 if len(tokens) > 6 else 0.0
        record["volume_curve"] = tokens[7] if len(tokens) > 7 and tokens[7] not in {"0", "*"} else None
        if len(tokens) > 8:
            if tokens[8].upper() not in {"YES", "NO"}:
                _bad("Tank overflow must be YES or NO.", line=line)
            record["overflow"] = tokens[8].upper() == "YES"
        if record["diameter"] <= 0 or not 0 <= record["min_level"] <= record["init_level"] <= record["max_level"] or record["min_volume_m3"] < 0:
            _bad("Invalid tank diameter, levels, or minimum volume.", line=line)
        add("tanks", record, line)
    if not network["reservoirs"] and not network["tanks"]:
        _bad("An INP network requires a reservoir or tank.")
    for line, _, tokens in rows.get("PIPES", []):
        if not tokens:
            continue
        _count(tokens, 6, 8, line)
        record = {"id": tokens[0], "from": tokens[1], "to": tokens[2], "length": _number(tokens[3], line=line) * lf,
                  "diameter": _number(tokens[4], line=line) * df, "roughness": _number(tokens[5], line=line) * (rf if headloss == "D-W" else 1.0),
                  "minor_loss": _number(tokens[6], line=line) if len(tokens) > 6 else 0.0,
                  "status": tokens[7].lower() if len(tokens) > 7 else "open", "material": None, "vertices": []}
        if record["status"] not in {"open", "closed", "cv"}:
            _bad("Pipe status must be OPEN, CLOSED, or CV.", line=line)
        if record["length"] <= 0 or record["diameter"] <= 0 or record["roughness"] < 0 or (headloss == "H-W" and record["roughness"] == 0) or record["minor_loss"] < 0:
            _bad("Invalid pipe dimensions, roughness, or minor loss.", line=line)
        add("pipes", record, line)
    for line, _, tokens in rows.get("PUMPS", []):
        if not tokens:
            continue
        _count(tokens, 5, 11, line)
        if (len(tokens) - 3) % 2:
            _bad("Pump properties must be keyword/value pairs.", line=line)
        props: dict[str, str] = {}
        for key, value in zip(tokens[3::2], tokens[4::2]):
            key = key.upper()
            if key not in {"HEAD", "SPEED", "PATTERN"}:
                _unsupported("pump_" + key.lower(), line=line)
            if key in props:
                _bad("Repeated pump property.", line=line, property=key)
            props[key] = value
        if "HEAD" not in props:
            _unsupported("pump_without_head_curve", line=line)
        speed = _number(props.get("SPEED", 1.0), line=line)
        if speed < 0:
            _bad("Pump speed cannot be negative.", line=line)
        add("pumps", {"id": tokens[0], "from": tokens[1], "to": tokens[2], "curve_id": _identifier(props["HEAD"]),
                      "curve": [], "speed": speed, "status": "open", "speed_pattern": props.get("PATTERN")}, line)
    for line, _, tokens in rows.get("VALVES", []):
        if not tokens:
            continue
        _count(tokens, 6, 7, line)
        kind = tokens[4].upper()
        if kind not in {"PRV", "PSV", "FCV", "TCV"}:
            _unsupported("valve_type", valve_type=kind, line=line)
        record = {"id": tokens[0], "from": tokens[1], "to": tokens[2], "diameter": _number(tokens[3], line=line) * df,
                  "type": kind, "setting": _number(tokens[5], line=line) * (pf if kind in {"PRV", "PSV"} else qf if kind == "FCV" else 1.0),
                  "minor_loss": _number(tokens[6], line=line) if len(tokens) > 6 else 0.0,
                  "status": "active"}
        if record["diameter"] <= 0 or record["setting"] < 0 or record["minor_loss"] < 0:
            _bad("Invalid valve diameter, setting, or minor loss.", line=line)
        add("valves", record, line)
    for link in links.values():
        if link["from"] not in nodes or link["to"] not in nodes or link["from"] == link["to"]:
            _bad("A link needs two different, existing endpoint nodes.", element_id=link["id"])
    for line, _, tokens in rows.get("PATTERNS", []):
        if not tokens:
            continue
        _count(tokens, 2, 1000000, line)
        key = _identifier(tokens[0])
        values = [_number(t, line=line) for t in tokens[1:]]
        if any(v < 0 for v in values):
            _bad("Pattern multipliers cannot be negative.", line=line)
        network["patterns"].setdefault(key, []).extend(values)
    curve_kinds: dict[str, str] = {}
    for pump in network["pumps"]:
        curve_kinds[pump["curve_id"]] = "pump"
    for tank in network["tanks"]:
        key = tank["volume_curve"]
        if key:
            if key in curve_kinds and curve_kinds[key] != "volume":
                _bad("A curve cannot be shared between pump head and tank volume uses.", curve_id=key)
            curve_kinds[key] = "volume"
    pending_kind: str | None = None
    pending_description = ""
    for line, raw, tokens in rows.get("CURVES", []):
        hint = re.match(r"\s*;\s*(PUMP|VOLUME|EFFICIENCY|HEADLOSS)\s*:(.*)", raw, re.IGNORECASE)
        if hint:
            pending_kind, pending_description = hint[1].lower(), hint[2].strip()
        if not tokens:
            continue
        _count(tokens, 3, 3, line)
        key = _identifier(tokens[0])
        kind = curve_kinds.get(key, pending_kind or "unknown")
        if key not in network["curves"]:
            record = {"type": kind, "points": [], "description": pending_description}
            if kind == "unknown":
                record = {"type": kind, "points_inp": [], "units_in": units, "description": pending_description}
            network["curves"][key] = record
        record = network["curves"][key]
        kind = record["type"]
        point = [_number(tokens[1], line=line), _number(tokens[2], line=line)]
        if kind == "volume":
            point = [point[0] * lf, point[1] * lf ** 3]
        elif kind in {"pump", "headloss"}:
            point = [point[0] * qf, point[1] * lf]
        elif kind == "efficiency":
            point[0] *= qf
        record["points_inp" if kind == "unknown" else "points"].append(point)
        pending_kind, pending_description = None, ""
    for key, curve in network["curves"].items():
        _curve_points(curve.get("points", curve.get("points_inp")), curve["type"], key)
        if curve["type"] == "unknown":
            raw = "[CURVES]\n" + "".join(row for _, row, tokens in rows.get("CURVES", []) if tokens and tokens[0] == key)
            dropped.append(_passthrough("CURVES", raw, active=False, hydraulic=False))
    for pump in network["pumps"]:
        key = pump["curve_id"]
        if key not in network["curves"]:
            _bad("Pump references a missing head curve.", curve_id=key, element_id=pump["id"])
        pump["curve"] = [point[:] for point in network["curves"][key]["points"]]
    for tank in network["tanks"]:
        if tank["volume_curve"] and tank["volume_curve"] not in network["curves"]:
            _bad("Tank references a missing volume curve.", element_id=tank["id"])
    junction_ids = {node["id"] for node in network["junctions"]}
    for line, raw, tokens in rows.get("DEMANDS", []):
        if not tokens:
            continue
        _count(tokens, 2, 3, line)
        if tokens[0] not in junction_ids:
            _bad("A demand record references an unknown junction.", line=line)
        node = nodes[tokens[0]]
        if "demands" not in node:
            node["demands"] = []
            node["demand_m3s"], node["pattern"] = 0.0, None
        node["demands"].append({"demand_m3s": _number(tokens[1], line=line) * qf,
                                "pattern": tokens[2] if len(tokens) > 2 and tokens[2] != "0" else None,
                                "category": raw.split(";", 1)[1].strip() if ";" in raw else None})
    valve_ids = {link["id"] for link in network["valves"]}
    pump_ids = {link["id"] for link in network["pumps"]}
    for line, _, tokens in rows.get("STATUS", []):
        if not tokens:
            continue
        _count(tokens, 2, 2, line)
        key, value = tokens
        if key not in links:
            _bad("Status references an unknown link.", line=line)
        link = links[key]
        if link.get("status") == "cv":
            _bad("Check-valve pipe status cannot be overridden.", line=line)
        if value.upper() in {"OPEN", "CLOSED"}:
            link["status"] = value.lower()
            if key in pump_ids and value.upper() == "OPEN":
                link["speed"] = 1.0
        elif key in pump_ids:
            link["speed"] = _number(value, line=line)
            if link["speed"] < 0:
                _bad("Pump speed cannot be negative.", line=line)
            link["status"] = "closed" if link["speed"] == 0 else "open"
        elif key in valve_ids:
            link["setting"] = _number(value, line=line) * (pf if link["type"] in {"PRV", "PSV"} else qf if link["type"] == "FCV" else 1.0)
            link["status"] = "active"
            if link["setting"] < 0:
                _bad("Valve setting cannot be negative.", line=line)
        else:
            _bad("A pipe status must be OPEN or CLOSED.", line=line)
    coordinate_unit = "hydraulic"
    for line, _, tokens in rows.get("BACKDROP", []):
        if tokens and tokens[0].upper() == "UNITS":
            _count(tokens, 2, 2, line)
            coordinate_unit = tokens[1].lower()
    if coordinate_unit not in {"hydraulic", "feet", "ft", "meters", "metres", "m", "none", "degrees"}:
        _unsupported("map_units", units=coordinate_unit)
    cf = lf if coordinate_unit == "hydraulic" else 0.3048 if coordinate_unit in {"feet", "ft"} else 1.0 if coordinate_unit in {"meters", "metres", "m"} else None
    network["epanet_coordinate_units"] = coordinate_unit
    diagram = {"units": coordinate_unit, "coordinates": {}, "vertices": {}}
    coordinate_ids: set[str] = set()
    for line, _, tokens in rows.get("COORDINATES", []):
        if not tokens:
            continue
        _count(tokens, 3, 3, line)
        if tokens[0] not in nodes or tokens[0] in coordinate_ids:
            _bad("Coordinates require a unique, existing node.", line=line)
        coordinate_ids.add(tokens[0])
        point = [_number(v, line=line) for v in tokens[1:]]
        if cf is None:
            diagram["coordinates"][tokens[0]] = point
        else:
            nodes[tokens[0]]["x"], nodes[tokens[0]]["y"] = (v * cf for v in point)
    for line, _, tokens in rows.get("VERTICES", []):
        if not tokens:
            continue
        _count(tokens, 3, 3, line)
        if tokens[0] not in links:
            _bad("Vertices reference an unknown link.", line=line)
        point = [_number(v, line=line) for v in tokens[1:]]
        if cf is None:
            diagram["vertices"].setdefault(tokens[0], []).append(point)
        else:
            links[tokens[0]].setdefault("vertices", []).append([v * cf for v in point])
    if cf is None:
        network["epanet_map"] = diagram
    tank_ids = {node["id"] for node in network["tanks"]}
    controls: list[dict[str, Any]] = []
    for section in sections:
        if section["section"] != "CONTROLS":
            continue
        remaining: list[str] = []
        controls_before = len(controls)
        for line, raw, tokens in section["rows"]:
            if not tokens:
                continue
            upper = [t.upper() for t in tokens]
            control = None
            if len(tokens) >= 3 and upper[0] == "LINK" and tokens[1] not in links:
                _bad("Control references an unknown link.", line=line)
            if len(tokens) == 8 and upper[0] == "LINK" and upper[2] in {"OPEN", "CLOSED"} and upper[3:5] == ["IF", "NODE"] and upper[6] in {"ABOVE", "BELOW"} and tokens[5] in tank_ids:
                control = {"link_id": tokens[1], "status": upper[2].lower(), "node_id": tokens[5],
                           "relation": upper[6].lower(), "level_m": _number(tokens[7], line=line) * lf}
            elif len(tokens) in {6, 7} and upper[0] == "LINK" and upper[2] in {"OPEN", "CLOSED"} and upper[3:5] == ["AT", "TIME"]:
                control = {"link_id": tokens[1], "status": upper[2].lower(), "time_s": _time(tokens[5:], line)}
            if control is None:
                remaining.append(raw)
            else:
                if links[control["link_id"]].get("status") == "cv":
                    _bad("A check-valve pipe cannot be controlled.", line=line)
                controls.append(control)
        if remaining:
            original = section["raw"] if len(controls) == controls_before else "[CONTROLS]\n" + "".join(remaining)
            record = _passthrough("CONTROLS", original)
            passthrough.append(record)
            dropped.append(dict(record))
        if not remaining or len(controls) != controls_before:
            # Preserve the original full section, but never execute it twice.
            passthrough.append(_passthrough("CONTROLS", section["raw"], active=False, hydraulic=False) | {"interpreted": True})
    if controls:
        network["controls"] = controls
    for node in network["junctions"] + network["reservoirs"]:
        patterns = [node.get("pattern")] + [d.get("pattern") for d in node.get("demands", [])]
        if any(key and key not in network["patterns"] for key in patterns):
            _bad("A node references an undefined pattern.", element_id=node["id"])
    for pump in network["pumps"]:
        if pump["speed_pattern"] and pump["speed_pattern"] not in network["patterns"]:
            _bad("A pump references an undefined speed pattern.", element_id=pump["id"])
    requested_default_pattern = options["default_pattern"]
    missing_default_pattern = requested_default_pattern is not None and requested_default_pattern not in network["patterns"]
    if missing_default_pattern:
        # EPANET adjustdata only assigns a found default pattern; otherwise
        # unpatterned demands remain constant, even for an explicit option.
        options["default_pattern"] = None
        network["epanet_default_pattern"] = {"requested": requested_default_pattern, "explicit": default_pattern_explicit}
    if options["p_min_m"] < 0 or options["p_req_m"] < 0 or (options["demand_model"] == "PDA" and options["p_req_m"] <= options["p_min_m"]) or options["pda_exponent"] <= 0 or options.get("demand_multiplier", 1.0) < 0:
        _bad("PDA pressures/exponent or demand multiplier are invalid.")
    for record in passthrough:
        if record["active"] and record["hydraulic"]:
            record["units_in"] = units
    for record in dropped:
        if record["active"] and record["hydraulic"]:
            record["units_in"] = units
    if passthrough:
        network["epanet_passthrough"] = passthrough
    if comments:
        network["epanet_comments"] = comments
    provenance: dict[str, Any] = {
        "source": source, "kind": "epanet_inp", "units_in": units, "units_out": "SI",
        "reference": _REFERENCE, "pressure_units_in": factors["pressure_units"],
        "pressure_units_requested": factors["pressure_units_requested"],
        "defaults_reference": "EPA EPANET 2.2 SRC_engines/src/solver/input1.c setdefaults/adjustdata and types.h MINPDIFF/VISCOS",
        "default_pattern": {"requested": requested_default_pattern, "explicit": default_pattern_explicit,
                            "resolved": options["default_pattern"]},
        "conversions": {"flow_to_m3s": qf, "head_length_to_m": lf, "pipe_diameter_to_m": df,
                        "dw_roughness_to_m": rf, "pressure_to_head_m": pf, "tank_volume_to_m3": lf ** 3,
                        "map_to_m": cf},
        "assumptions": ["EPANET map coordinates/vertices use the hydraulic length unit; INP itself does not prescribe map units."] if coordinate_unit == "hydraulic" else [],
        "map_units_in": coordinate_unit,
        "warnings": [f"[{record['section']}] contains retained, uninterpreted records." for record in dropped],
        "metadata_restored": False,
    }
    if missing_default_pattern:
        origin = "explicit OPTIONS PATTERN" if default_pattern_explicit else "implicit default"
        provenance["warnings"].append(f"Absent EPANET {origin} {requested_default_pattern!r}; unpatterned demands use constant multiplier 1.")
    if factors["pressure_units"] != factors["pressure_units_requested"]:
        provenance["warnings"].append("EPANET pressure units follow the flow-unit family: US uses PSI; metric PSI becomes METERS.")
    if encoded:
        try:
            snapshot = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"),
                                  parse_constant=lambda value: _bad("Non-finite civil metadata value.", value=value))
        except (ValueError, UnicodeError) as exc:
            raise CivilError("epanet_bad_syntax", "Malformed civil metadata comment.") from exc
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not isinstance(snapshot.get("network"), dict):
            _bad("Unsupported or malformed civil metadata comment.")
        if snapshot.get("body_sha256") == hashlib.sha256(body.encode("utf-8")).hexdigest():
            network = snapshot["network"]
            provenance["metadata_restored"] = True
            provenance["assumptions"] = []
        else:
            network["epanet_unapplied_metadata"] = snapshot["network"]
            provenance["warnings"].append("Civil metadata snapshot was not restored because ordinary INP records changed.")
    return {"network": network, "provenance": provenance, "dropped": dropped}


def _fmt(value: Any) -> str:
    return format(_number(value), ".17g")


def write(network: dict[str, Any], path: str | os.PathLike[str]) -> dict[str, Any]:
    """Write a deterministic INP and return ``{ok: True, path}``.

    The input is not mutated. Export units are ``network.epanet_units``
    (LPS if absent). See EPANET 2.2 Appendix C.2 for section grammar. The
    explicit civil comment retains nonstandard metadata and exact canonical
    SI values; unsupported passthrough is written, not silently discarded.
    Constant-power pumps, GPV/PBV, C-M, and two-point pump curves refuse.
    """
    if not isinstance(network, dict):
        _bad("write expects a network dictionary.")
    try:
        snapshot_json = json.dumps(network, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CivilError("epanet_bad_syntax", "Network metadata must be finite JSON data.") from exc
    if json.loads(snapshot_json) != network:
        _bad("The canonical network must use JSON string object keys and list arrays.")
    units = str(network.get("epanet_units", "LPS")).upper()
    factors = _units(units, network.get("epanet_pressure_units"))
    qf, lf, df, rf, pf = (float(factors[k]) for k in ("flow", "length", "diameter", "roughness", "pressure"))
    headloss = str(network.get("headloss", "H-W")).upper()
    if headloss not in {"H-W", "D-W"}:
        _unsupported("headloss", headloss=headloss)
    output: list[str] = []
    def section(name: str, records: list[str]) -> None:
        output.append(f"[{name}]\n")
        output.extend(record + "\n" for record in records)
        output.append("\n")
    def ident(value: Any) -> str:
        return _identifier(value)
    def value(record: dict[str, Any], field: str, factor: float = 1.0, default: Any = None) -> str:
        if field not in record and default is None:
            _bad("A required network field is missing.", field=field, element_id=record.get("id"))
        return _fmt(_number(record.get(field, default)) / factor)
    collections = {key: [] if network.get(key) is None else network[key] for key in ("junctions", "reservoirs", "tanks", "pipes", "pumps", "valves")}
    for key, records in collections.items():
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            _bad("Network collections must be lists of dictionaries.", collection=key)
    required = {
        "junctions": ("id", "elevation"), "reservoirs": ("id", "head"),
        "tanks": ("id", "elevation", "init_level", "min_level", "max_level", "diameter"),
        "pipes": ("id", "from", "to", "length", "diameter", "roughness"),
        "pumps": ("id", "from", "to"), "valves": ("id", "from", "to", "type", "diameter", "setting"),
    }
    for key, fields in required.items():
        for record in collections[key]:
            if any(field not in record for field in fields):
                _bad("A required network field is missing.", collection=key, element_id=record.get("id"))
    title = network.get("title", [network.get("name", "EPANET network")])
    if not isinstance(title, list) or any(not isinstance(t, str) or "\n" in t or "\r" in t or t.strip().startswith("[") for t in title):
        _bad("TITLE must be a list of single-line strings, not section headers.")
    section("TITLE", title)
    junctions, reservoirs, tanks = (collections[key] for key in ("junctions", "reservoirs", "tanks"))
    pipes, pumps, valves = (collections[key] for key in ("pipes", "pumps", "valves"))
    node_ids: set[str] = set()
    link_ids: set[str] = set()
    for records, seen in ((junctions + reservoirs + tanks, node_ids), (pipes + pumps + valves, link_ids)):
        for record in records:
            key = ident(record.get("id"))
            if key in seen:
                _bad("Duplicate node or link identifier.", element_id=key)
            seen.add(key)
    if not junctions or not (reservoirs or tanks):
        _bad("An INP network requires junctions and a reservoir or tank.")
    for record in pipes + pumps + valves:
        if record.get("from") not in node_ids or record.get("to") not in node_ids or record["from"] == record["to"]:
            _bad("A link needs two different, existing endpoint nodes.", element_id=record["id"])
    section("JUNCTIONS", [" ".join([ident(n["id"]), value(n, "elevation", lf), value(n, "demand_m3s", qf, 0.0)] +
                                    ([ident(n["pattern"])] if n.get("pattern") else [])) for n in junctions])
    section("RESERVOIRS", [" ".join([ident(n["id"]), value(n, "head", lf)] +
                                     ([ident(n["pattern"])] if n.get("pattern") else [])) for n in reservoirs])
    tank_rows = []
    for tank in tanks:
        fields = [ident(tank["id"])] + [value(tank, field, lf) for field in ("elevation", "init_level", "min_level", "max_level", "diameter")]
        fields.append(value(tank, "min_volume_m3", lf ** 3, 0.0))
        if tank.get("volume_curve") or "overflow" in tank:
            fields.append(ident(tank["volume_curve"]) if tank.get("volume_curve") else "*")
        if "overflow" in tank:
            if not isinstance(tank["overflow"], bool):
                _bad("Tank overflow must be a boolean.", element_id=tank["id"])
            fields.append("YES" if tank["overflow"] else "NO")
        if _number(tank["diameter"]) <= 0 or not 0 <= _number(tank["min_level"]) <= _number(tank["init_level"]) <= _number(tank["max_level"]) or _number(tank.get("min_volume_m3", 0)) < 0:
            _bad("Invalid tank diameter, levels, or minimum volume.", element_id=tank["id"])
        tank_rows.append(" ".join(fields))
    section("TANKS", tank_rows)
    pipe_rows = []
    for pipe in pipes:
        status = str(pipe.get("status", "open")).lower()
        if status not in {"open", "closed", "cv"}:
            _bad("Pipe status must be open, closed, or cv.", element_id=pipe["id"])
        if _number(pipe["length"]) <= 0 or _number(pipe["diameter"]) <= 0 or _number(pipe["roughness"]) < 0 or (headloss == "H-W" and _number(pipe["roughness"]) == 0) or _number(pipe.get("minor_loss", 0)) < 0:
            _bad("Invalid pipe dimensions, roughness, or minor loss.", element_id=pipe["id"])
        pipe_rows.append(" ".join([ident(pipe["id"]), ident(pipe["from"]), ident(pipe["to"]), value(pipe, "length", lf),
                                    value(pipe, "diameter", df), value(pipe, "roughness", rf if headloss == "D-W" else 1.0),
                                    value(pipe, "minor_loss", default=0.0), status.upper()]))
    section("PIPES", pipe_rows)
    raw_curves = network.get("curves") or {}
    if not isinstance(raw_curves, dict):
        _bad("Network curves must be a dictionary.")
    curves: dict[str, dict[str, Any]] = {}
    for key, curve in raw_curves.items():
        ident(key)
        if not isinstance(curve, dict):
            _bad("Each curve must declare type and points.", curve_id=key)
        curves[key] = dict(curve)
    pump_rows = []
    status_rows = []
    for pump in pumps:
        key = pump.get("curve_id")
        if not key:
            base = "CIVIL_PUMP_" + ident(pump["id"])
            key, suffix = base, 1
            while key in curves:
                key, suffix = f"{base}_{suffix}", suffix + 1
        key = ident(key)
        points = pump.get("curve", curves.get(key, {}).get("points"))
        points = _curve_points(points, "pump", key)
        if key in curves and (curves[key].get("type") != "pump" or curves[key].get("points") != points):
            _bad("Inline pump curve conflicts with its named curve.", curve_id=key)
        curves[key] = {**curves.get(key, {}), "type": "pump", "points": points}
        speed = _number(pump.get("speed", 1.0))
        if speed < 0:
            _bad("Pump speed cannot be negative.", element_id=pump["id"])
        fields = [ident(pump["id"]), ident(pump["from"]), ident(pump["to"]), "HEAD", key, "SPEED", _fmt(speed)]
        if pump.get("speed_pattern"):
            fields.extend(["PATTERN", ident(pump["speed_pattern"])])
        pump_rows.append(" ".join(fields))
        status = str(pump.get("status", "open")).lower()
        if status not in {"open", "closed"}:
            _bad("Pump status must be open or closed.", element_id=pump["id"])
        if status == "closed":
            status_rows.append(f"{ident(pump['id'])} CLOSED")
    section("PUMPS", pump_rows)
    valve_rows = []
    for valve in valves:
        kind = str(valve.get("type", "")).upper()
        if kind not in {"PRV", "PSV", "FCV", "TCV"}:
            _unsupported("valve_type", valve_type=kind)
        if _number(valve["diameter"]) <= 0 or _number(valve["setting"]) < 0 or _number(valve.get("minor_loss", 0)) < 0:
            _bad("Invalid valve diameter, setting, or minor loss.", element_id=valve["id"])
        valve_rows.append(" ".join([ident(valve["id"]), ident(valve["from"]), ident(valve["to"]), value(valve, "diameter", df), kind,
                                     value(valve, "setting", pf if kind in {"PRV", "PSV"} else qf if kind == "FCV" else 1.0),
                                     value(valve, "minor_loss", default=0.0)]))
        status = str(valve.get("status", "active")).lower()
        if status not in {"active", "open", "closed"}:
            _bad("Valve status must be active, open, or closed.", element_id=valve["id"])
        if status != "active":
            status_rows.append(f"{ident(valve['id'])} {status.upper()}")
    section("VALVES", valve_rows)
    patterns = network.get("patterns") or {}
    if not isinstance(patterns, dict):
        _bad("Patterns must be an ID-to-multipliers dictionary.")
    for node in junctions + reservoirs:
        demands = node.get("demands", [])
        if not isinstance(demands, list) or any(not isinstance(d, dict) for d in demands):
            _bad("Junction demands must be a list of demand dictionaries.", element_id=node["id"])
        refs = [node.get("pattern")] + [d.get("pattern") for d in demands]
        if any(key and key not in patterns for key in refs):
            _bad("A node references an undefined pattern.", element_id=node["id"])
    for pump in pumps:
        if pump.get("speed_pattern") and pump["speed_pattern"] not in patterns:
            _bad("A pump references an undefined speed pattern.", element_id=pump["id"])
    pattern_rows = []
    for key in sorted(patterns):
        values = patterns[key]
        if not isinstance(values, list) or not values or any(_number(v) < 0 for v in values):
            _bad("A pattern requires nonnegative multipliers.", pattern_id=key)
        for start in range(0, len(values), 6):
            pattern_rows.append(ident(key) + " " + " ".join(_fmt(v) for v in values[start:start + 6]))
    section("PATTERNS", pattern_rows)
    for tank in tanks:
        if tank.get("volume_curve") and (tank["volume_curve"] not in curves or curves[tank["volume_curve"]].get("type") != "volume"):
            _bad("Tank volume curve must reference a defined volume curve.", element_id=tank["id"])
    curve_rows = []
    for key in sorted(curves):
        curve = curves[key]
        kind = curve.get("type", "unknown").lower()
        if kind not in {"pump", "volume", "efficiency", "headloss", "unknown"}:
            _unsupported("curve_type", curve_id=key, curve_type=kind)
        points = _curve_points(curve.get("points_inp") if kind == "unknown" else curve.get("points"), kind, key)
        if kind == "unknown" and curve.get("units_in") != units:
            _unsupported("untyped_curve_unit_conversion", curve_id=key)
        if kind != "unknown":
            description = str(curve.get("description", ""))
            if "\n" in description or "\r" in description:
                _bad("Curve descriptions must be single-line strings.", curve_id=key)
            curve_rows.append(f";{kind.upper()}: {description}")
        for x, y in points:
            xf, yf = (lf, lf ** 3) if kind == "volume" else (qf, lf) if kind in {"pump", "headloss"} else (qf, 1.0) if kind == "efficiency" else (1.0, 1.0)
            curve_rows.append(f"{ident(key)} {_fmt(x / xf)} {_fmt(y / yf)}")
    section("CURVES", curve_rows)
    demand_rows = []
    for node in junctions:
        if not node.get("demands"):
            continue
        demands = node["demands"]
        if not isinstance(demands, list) or any(not isinstance(d, dict) for d in demands):
            _bad("Junction demands must be a list of demand dictionaries.", element_id=node["id"])
        # INP DEMANDS replaces JUNCTIONS: include the scalar base as a category.
        if _number(node.get("demand_m3s", 0)) != 0:
            demands = [{"demand_m3s": node["demand_m3s"], "pattern": node.get("pattern"), "category": None}] + demands
        for demand in demands:
            fields = [ident(node["id"]), value(demand, "demand_m3s", qf), ident(demand["pattern"]) if demand.get("pattern") else "0"]
            category = demand.get("category")
            if category is not None:
                if not isinstance(category, str) or "\n" in category or "\r" in category:
                    _bad("Demand categories must be single-line strings.", element_id=node["id"])
                fields.append(";" + category)
            demand_rows.append(" ".join(fields))
    section("DEMANDS", demand_rows)
    section("STATUS", status_rows)
    options = network.get("options") or {}
    if not isinstance(options, dict):
        _bad("Network options must be a dictionary.")
    option_rows = [f"UNITS {units}", f"HEADLOSS {headloss}", f"PRESSURE {factors['pressure_units']}"]
    model = str(options.get("demand_model", "DDA")).upper()
    if model not in {"DDA", "PDA"}:
        _unsupported("demand_model", value=model)
    option_rows.append(f"DEMAND MODEL {model}")
    for key, field, factor, default in (("MINIMUM PRESSURE", "p_min_m", pf, 0.0), ("REQUIRED PRESSURE", "p_req_m", pf, 20.0),
                                       ("PRESSURE EXPONENT", "pda_exponent", 1.0, 0.5), ("ACCURACY", "accuracy", 1.0, 1e-6),
                                       ("TRIALS", "max_iter", 1.0, 200), ("VISCOSITY", "viscosity_m2s", 1.1e-5 * 0.3048 ** 2, 1.004e-6)):
        if field == "viscosity_m2s" and _number(options.get(field, default)) / factor <= 1e-3:
            factor = lf ** 2
        option_rows.append(key + " " + value(options, field, factor, default))
    if _number(options.get("accuracy", 1e-6)) <= 0 or _number(options.get("viscosity_m2s", 1.004e-6)) <= 0 or _number(options.get("max_iter", 200)) <= 0 or _number(options.get("max_iter", 200)) != int(_number(options.get("max_iter", 200))):
        _bad("Hydraulic accuracy, viscosity, and integer trials must be positive.")
    if _number(options.get("p_min_m", 0)) < 0 or _number(options.get("p_req_m", 20)) < 0 or (model == "PDA" and _number(options.get("p_req_m", 20)) <= _number(options.get("p_min_m", 0))) or _number(options.get("pda_exponent", .5)) <= 0:
        _bad("PDA pressures and exponent are invalid.")
    if "default_pattern" in options:
        option_rows.append("PATTERN " + (ident(options["default_pattern"]) if options["default_pattern"] else "0"))
    if "demand_multiplier" in options:
        if _number(options["demand_multiplier"]) < 0:
            _bad("Demand multiplier cannot be negative.")
        option_rows.append("DEMAND MULTIPLIER " + value(options, "demand_multiplier"))
    section("OPTIONS", option_rows)
    time_rows = []
    for keyword, field in _TIME_FIELDS.items():
        if field in options or field in {"duration_s", "hydraulic_step_s", "pattern_step_s"}:
            default = 0.0 if field == "duration_s" else 3600.0
            seconds = _number(options.get(field, default))
            if seconds < 0 or (keyword.endswith("TIMESTEP") and seconds == 0) or (field == "start_clocktime_s" and seconds >= 86400):
                _bad("Invalid duration, timestep, or start clock time.", field=field)
            time_rows.append(f"{keyword} {_fmt(seconds / 3600) if field == 'start_clocktime_s' else _fmt(seconds)}" + ("" if field == "start_clocktime_s" else " SECONDS"))
    if "statistic" in options:
        statistic = str(options["statistic"]).upper()
        if statistic not in {"NONE", "AVERAGED", "MINIMUM", "MAXIMUM", "RANGE"}:
            _bad("Unknown report statistic.")
        time_rows.append("STATISTIC " + statistic)
    section("TIMES", time_rows)
    coordinate_unit = network.get("epanet_coordinate_units", "hydraulic")
    cf = lf if coordinate_unit == "hydraulic" else 0.3048 if coordinate_unit in {"feet", "ft"} else 1.0 if coordinate_unit in {"meters", "metres", "m"} else None
    if coordinate_unit not in {"hydraulic", "feet", "ft", "meters", "metres", "m", "none", "degrees"}:
        _unsupported("map_units", units=coordinate_unit)
    diagram = network.get("epanet_map", {})
    if not isinstance(diagram, dict) or not isinstance(diagram.get("coordinates", {}), dict) or not isinstance(diagram.get("vertices", {}), dict):
        _bad("Unscaled EPANET map coordinates and vertices must be dictionaries.")
    if any(key not in node_ids for key in diagram.get("coordinates", {})) or any(key not in link_ids for key in diagram.get("vertices", {})):
        _bad("Unscaled map coordinates/vertices reference unknown elements.")
    coordinate_rows = []
    for node in junctions + reservoirs + tanks:
        x, y = node.get("x"), node.get("y")
        if (x is None) != (y is None):
            _bad("Node coordinates require both x and y.", element_id=node["id"])
        if x is not None:
            if cf is None:
                _unsupported("physical_coordinates_in_unscaled_map", element_id=node["id"])
            coordinate_rows.append(f"{ident(node['id'])} {_fmt(_number(x) / cf)} {_fmt(_number(y) / cf)}")
        elif node["id"] in diagram.get("coordinates", {}):
            point = diagram["coordinates"][node["id"]]
            if cf is not None or not isinstance(point, list) or len(point) != 2:
                _bad("Unscaled map coordinates require a NONE/DEGREES map and coordinate pairs.")
            coordinate_rows.append(f"{ident(node['id'])} {_fmt(point[0])} {_fmt(point[1])}")
    section("COORDINATES", coordinate_rows)
    vertex_rows = []
    for link in pipes + pumps + valves:
        vertices = link.get("vertices", [])
        if not isinstance(vertices, list):
            _bad("Link vertices must be a list of coordinate pairs.", element_id=link["id"])
        for point in vertices:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                _bad("Link vertices must be coordinate pairs.", element_id=link["id"])
            if cf is None:
                _unsupported("physical_vertices_in_unscaled_map", element_id=link["id"])
            vertex_rows.append(f"{ident(link['id'])} {_fmt(_number(point[0]) / cf)} {_fmt(_number(point[1]) / cf)}")
        for point in diagram.get("vertices", {}).get(link["id"], []):
            if cf is not None or not isinstance(point, list) or len(point) != 2:
                _bad("Unscaled map vertices require a NONE/DEGREES map and coordinate pairs.")
            vertex_rows.append(f"{ident(link['id'])} {_fmt(point[0])} {_fmt(point[1])}")
    section("VERTICES", vertex_rows)
    control_rows = []
    tank_ids = {tank["id"] for tank in tanks}
    cv_ids = {pipe["id"] for pipe in pipes if str(pipe.get("status", "open")).lower() == "cv"}
    for control in network.get("controls", []):
        if not isinstance(control, dict) or control.get("link_id") not in link_ids or control.get("link_id") in cv_ids:
            _bad("A control requires an existing non-check-valve link.")
        status = str(control.get("status", "")).upper()
        if status not in {"OPEN", "CLOSED"}:
            _unsupported("control_setting")
        prefix = f"LINK {ident(control['link_id'])} {status}"
        if "time_s" in control:
            if "node_id" in control or _number(control["time_s"]) < 0:
                _bad("Elapsed-time controls cannot also have a node condition.")
            control_rows.append(prefix + " AT TIME " + _fmt(_number(control["time_s"]) / 3600))
        else:
            relation = str(control.get("relation", "")).upper()
            if control.get("node_id") not in tank_ids or relation not in {"ABOVE", "BELOW"}:
                _unsupported("control_condition")
            control_rows.append(prefix + f" IF NODE {ident(control['node_id'])} {relation} " + value(control, "level_m", lf))
    if control_rows:
        section("CONTROLS", control_rows)
    for record in network.get("epanet_passthrough", []):
        if not isinstance(record, dict) or not isinstance(record.get("text"), str) or not isinstance(record.get("section"), str):
            _bad("Passthrough records require section and verbatim text.")
        if record.get("active") and record.get("hydraulic") and record.get("units_in", units) != units:
            _unsupported("hydraulic_passthrough_unit_conversion", section=record["section"])
        if record.get("interpreted"):
            if record["section"].upper() != "CONTROLS":
                _bad("Only parsed simple controls may use interpreted passthrough.")
            # The semantic controls above are authoritative after edits.
            output.extend("; Original control text: " + line + "\n" for line in record["text"].splitlines())
        else:
            parsed, metadata, _ = _sections(record["text"])
            if metadata or len(parsed) != 1 or parsed[0]["section"] != record["section"].upper() or parsed[0]["section"] == "END":
                _bad("Passthrough text must contain exactly its declared section.")
            output.append(record["text"])
            if not record["text"].endswith("\n"):
                output.append("\n")
            output.append("\n")
    for comment in network.get("epanet_comments", []):
        if not isinstance(comment, dict) or not isinstance(comment.get("text"), str) or "\n" in comment["text"] or "\r" in comment["text"] or any(c in str(comment.get("section", "")) for c in "\r\n"):
            _bad("Retained comments must be single-line text records.")
        output.append("; Original [" + str(comment.get("section", "")) + "] comment: " + comment["text"] + "\n")
    output.append("[END]\n")
    body = "".join(output)
    payload = '{"version":1,"body_sha256":' + json.dumps(hashlib.sha256(body.encode("utf-8")).hexdigest()) + ',"network":' + snapshot_json + '}'
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    comment = "".join(_MARKER + encoded[start:start + 120] + "\n" for start in range(0, len(encoded), 120))
    try:
        destination = Path(path)
        destination.write_text(comment + body, encoding="utf-8", newline="\n")
    except (TypeError, OSError, UnicodeError) as exc:
        raise CivilError("epanet_io_error", "Could not write the INP file.", path=str(path), detail=str(exc)) from exc
    return {"ok": True, "path": str(destination)}
