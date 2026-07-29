"""Functional substitution for ECAD library misses.

WHY THIS EXISTS
---------------
The clean-room starter library (``starter.SYMBOLS``) holds ~53 GENERIC symbols —
``MCU:Generic_QFP48``, ``Regulator_Switching:Buck_3V3``, ``Device:NMOS`` and friends.
Models, however, always ask for real part numbers: "ESP32-S3-WROOM", "TPS62840",
"MPU9250". A measured live run burned FOUR consecutive ``ecad_find_part`` calls
(``ESP32-S3-WROOM`` -> ``ESP32`` -> ``TPS62840`` -> ``ESP32-S3``, all "no library parts
match") before the model gave up and manually placed ``MCU:Generic_QFP48`` — exactly
what this table returns on the first call.

WHAT THIS IS NOT
----------------
It does NOT invent pins. Every entry points at a symbol that already exists in the
starter library with a real, hand-authored pin definition. The substitute is a
FUNCTIONAL equivalent (same role in the circuit, same rough pin-count class), never
a claim that the pinout matches the requested part number. Callers MUST surface
``caveat`` to the user — a silent generic swap corrupts a netlist far more quietly
than an honest miss.

NOT A CATALOG BRIDGE
--------------------
``part_search`` (services/mcp/parts_tools.py) indexes ~3.2k records, but its only
``domain='ecad'`` rows are a mirror of these same starter symbols; the remaining
~3.1k are mechanical (seals, bearings, fasteners, motors, fittings) and carry no pin
definitions at all. A record with no pins cannot be placed on a schematic, so routing
``ecad_find_part`` through the catalog would return either the identical libids or
unplaceable mechanical junk. Hence: functional substitution, not a bridge.
"""

from __future__ import annotations

import re

from .starter import SYMBOLS

# Each entry: (pattern, libid, family label, caveat).
# Order matters — the FIRST match wins, so specific classes precede broad ones.
# Every libid is asserted to exist in SYMBOLS at import time (see _validate below).
_RULES: tuple[tuple[str, str, str, str], ...] = (
    # -- FPGA before MCU: "ice40 mcu"-style phrasing should land on the FPGA. --
    (r"(fpga|ice40|ecp5|artix|spartan|cyclone|lattice|xc7a|max10)",
     "FPGA:iCE40UP5K-SG48", "FPGA",
     "iCE40UP5K-SG48 bank/pin model — a different FPGA has a different package, "
     "bank layout and pin count. Re-check IOSTANDARDs with ecad_fpga_check."),

    # -- Battery charge controllers (before the generic regulator rules). --
    (r"(tp4056|mcp7383|bq2[0-9]{4}|lipo\s*charger|li-?ion\s*charger|"
     r"battery\s*charg|charge\s*controller|charger)",
     "Battery_Management:MCP73831", "Li-ion charge controller",
     "MCP73831-class SOT-23-5 linear charger pinout (5 pins). A switching or "
     "multi-cell charger has a completely different pin set."),

    # -- Switching regulators / buck converters. --
    (r"(tps6[0-9]{4}|tps5[0-9]{4}|mp[0-9]{4}|lm2596|lmr[0-9]{4}|rt[0-9]{4}|"
     r"buck|boost|step[-\s]?down|step[-\s]?up|switch\w*\s*regulator|"
     r"switch\w*\s*converter|dc[-\s]?dc|smps|synchronous\s*regulator)",
     "Regulator_Switching:Buck_3V3", "switching (buck) regulator",
     "Generic 3.3 V synchronous buck in a SOT-23-6 / TPS562201-class 6-pin "
     "arrangement. Output voltage, feedback divider and pin order of the real "
     "part MUST be checked against its datasheet."),

    # -- Linear regulators / LDOs. --
    (r"(ldo|linear\s*regulator|voltage\s*regulator|ams1117|lm1117|lp29[0-9]{2}|"
     r"mcp17[0-9]{2}|xc62|ap2112|ncp[0-9]{3}|tlv7[0-9]{2}|reg[0-9]{4})",
     "Regulator_Linear:AMS1117-3.3", "linear regulator (LDO)",
     "AMS1117-3.3 SOT-223 4-pin pinout (GND/VOUT/VIN/VOUT-tab). A SOT-23-5 LDO "
     "with EN/NR pins will not match this footprint or pin order."),

    # -- Load switches / supervisors. --
    (r"(load\s*switch|tps22[0-9]{3}|power\s*switch|sip32[0-9]{2}|e-?fuse)",
     "Power_Management:Load_Switch", "load switch",
     "TPS22918-class SOT-23-5 load switch (VIN/GND/ON/CT/VOUT). Confirm the real "
     "part's control polarity and quick-output-discharge pin."),
    (r"(supervisor|reset\s*ic|brown-?out|undervoltage|uvlo|comparator|lm393|"
     r"tlv3[0-9]{3}|mic8[0-9]{3})",
     "Power_Management:LVD_Comparator", "voltage supervisor / comparator",
     "SOT-23-5 low-voltage-detect comparator pinout; a dual comparator such as "
     "the LM393 is an 8-pin part with a different pin map."),

    # -- Microcontrollers. Broad, and deliberately LAST among the IC families. --
    (r"(esp32|esp8266|wroom|wrover|stm32|rp2040|rp2350|atmega[0-9]|attiny[0-9]|"
     r"atsam|samd[0-9]|nrf5[0-9]|nrf9[0-9]|pic[0-9]{2}|msp430|"
     r"\bmcu\b|microcontroller|micro-?controller|processor|\bsoc\b|cortex)",
     "MCU:Generic_QFP48", "microcontroller",
     "Generic 48-pin QFP MCU (STM32F0-class). Pin NUMBERS are sequential and "
     "generic — they are NOT the requested part's pinout. Assign the real "
     "footprint and verify every net against the datasheet before fabrication."),

    # -- Sensors. --
    (r"(\bimu\b|accelerometer|\baccel\b|gyro|mpu6[0-9]{3}|mpu9[0-9]{3}|"
     r"icm2[0-9]{4}|lsm6|bmi[0-9]{3}|adxl[0-9]{3})",
     "Sensor_Motion:MPU6050", "6-axis IMU",
     "MPU-6050-class QFN-24 IMU (I2C + SPI). A different IMU has a different "
     "package and interrupt/address pin assignment."),
    (r"(barometer|\bbaro\b|pressure\s*sensor|altimeter|bmp[0-9]{3}|bme[0-9]{3}|"
     r"ms5[0-9]{3}|lps[0-9]{2})",
     "Sensor_Pressure:BMP280", "barometric pressure sensor",
     "BMP280-class LGA-8 pinout. A BME280 adds humidity but keeps 8 pins; an "
     "MS5611 does not match this pin map."),
    (r"(magnetometer|compass|lis3mdl|qmc5[0-9]{3}|hmc5[0-9]{3}|mmc5|ak89[0-9]{2})",
     "Sensor_Magnetic:LIS3MDL", "3-axis magnetometer",
     "LIS3MDL-class LGA-12 pinout (I2C/SPI)."),
    (r"(\bgps\b|gnss|glonass|galileo|neo-?[0-9]m|ublox|u-blox|max-?m[0-9])",
     "RF_GPS:NEO6M", "GNSS/GPS module",
     "NEO-6M-class 6-pin castellated breakout (UART). A bare GNSS chip has far "
     "more pins including RF and antenna bias."),
    (r"(thermistor|\bntc\b|temperature\s*sensor|thermocouple)",
     "Device:NTC", "NTC thermistor",
     "A 2-pin NTC. A digital temperature-sensor IC is not pin-compatible."),

    # -- Amplifiers / timers. --
    (r"(op-?amp|operational\s*amplifier|amplifier|lm358|tl07[0-9]|mcp6[0-9]{3}|"
     r"lmv[0-9]{3}|opa[0-9]{3})",
     "Amplifier_Operational:LM358", "dual operational amplifier",
     "LM358 DIP-8/SOIC-8 dual-opamp pinout. Single or quad opamps differ."),
    (r"(\b555\b|ne555|timer\s*ic|astable|monostable)",
     "Timer:NE555", "555 timer", "NE555 8-pin pinout."),

    # -- Discretes. --
    (r"(n-?channel|\bnmos\b|n-?mosfet|ao34[0-9]{2}|si2302|irlml|bss138|2n7002|"
     r"\bdmn[0-9]|\bmosfet\b|\bfet\b)",
     "Device:NMOS", "N-channel MOSFET",
     "SOT-23 pin numbering (1=G, 2=S, 3=D). Power packages such as SO-8 or DFN "
     "differ — see Device:NMOS_Power for a multi-pad part."),
    (r"(p-?channel|\bpmos\b|p-?mosfet|si2301|\bdmp[0-9])",
     "Device:PMOS", "P-channel MOSFET", "SOT-23 pin numbering (1=G, 2=S, 3=D)."),
    (r"(\bnpn\b|bc8[0-9]{2}|2n39[0-9]{2}|mmbt3904|s8050)",
     "Device:Q_NPN", "NPN transistor", "SOT-23 pin numbering."),
    (r"(\bpnp\b|mmbt3906|s8550)",
     "Device:Q_PNP", "PNP transistor", "SOT-23 pin numbering."),
    (r"(zener|\btvs\b|esd\s*(diode|protection)|bzx[0-9]|smaj[0-9]|pesd)",
     "Device:Zener", "zener / clamp diode",
     "A generic 2-pin zener symbol; a multi-line ESD array is not 2-pin."),
    (r"(schottky|\bss1[0-9]\b|\bss3[0-9]\b|\bsk[0-9]{2}\b|1n4[0-9]{3}|1n5[0-9]{3}|"
     r"bat5[0-9]|b5819|rectifier|freewheel)",
     "Device:D", "diode",
     "A generic 2-pin diode symbol — forward drop and current rating are not "
     "modelled; set them via value/sim_params."),
    (r"(ferrite|\bbead\b|blm[0-9]|emi\s*filter|common[-\s]?mode\s*choke)",
     "Device:Ferrite_Bead", "ferrite bead",
     "2-pin series bead; a common-mode choke is a 4-pin coupled part."),
    (r"(power\s*inductor|shielded\s*inductor|srp[0-9]{3}|ihlp|"
     r"[0-9](\.[0-9]+)?\s*[u\u00b5]h\b)",
     "Device:L_Power", "power inductor",
     "Generic 2-pin power inductor; set the value (e.g. '2.2uH')."),
    (r"(\bshunt\b|current\s*sense\s*resistor|sense\s*resistor|milliohm)",
     "Device:R_Sense", "current-sense shunt",
     "2512-class 2-pin shunt; 4-wire Kelvin shunts have 4 pads."),
    (r"(32\.?768\s*khz|rtc\s*crystal|watch\s*crystal)",
     "Device:Crystal_32k768", "32.768 kHz crystal", "2-pin watch crystal."),
    (r"(crystal|\bxtal\b|oscillator|resonator|[0-9]+\s*mhz|\bhse\b|tcxo)",
     "Device:Crystal_HSE", "crystal",
     "2-pin crystal (no load caps included — add two Device:C). A 4-pad TCXO or "
     "a canned oscillator is not 2-pin."),

    # -- Connectors. --
    (r"(usb-?c\b|type-?c\b|usb\s+c\b)", "Connector:USB_C", "USB-C receptacle",
     "Simplified 16-pin USB-C symbol (no SS pairs broken out individually)."),
    (r"(usb-?b\b|usb\s*micro|usb\s*mini|\busb\b)",
     "Connector:USB_B", "USB Type-B receptacle",
     "4-pin USB 2.0 receptacle (VBUS/D-/D+/GND); shield pins are not modelled."),
    (r"(\bswd\b|\bjtag\b|debug\s*(header|connector)|tag-?connect)",
     "Connector:SWD_2x5", "SWD/JTAG header", "ARM 10-pin 2x5 2.54 mm pinout."),
    (r"(header|terminal\s*block|\bjst\b|molex|screw\s*terminal|connector|receptacle)",
     "Connector:Header_1x4", "pin header",
     "A 1x4 header. Pick Connector:Conn_2/3/4, Header_2x03 or Header_2x05 if you "
     "need a different pin count — search 'header' to list them."),
)

_COMPILED: tuple[tuple[re.Pattern, str, str, str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), libid, family, caveat)
    for pat, libid, family, caveat in _RULES
)


def _validate() -> None:
    """Fail loudly at import if a rule points at a symbol that does not exist.

    A substitution rule naming a missing libid would hand the model an id that
    ecad_place_symbol then rejects — worse than the original miss.
    """
    missing = sorted({libid for _, libid, _, _ in _RULES if libid not in SYMBOLS})
    if missing:
        raise RuntimeError(
            "ecad substitution rules reference unknown libids: " + ", ".join(missing))


_validate()


def suggest(query: str) -> dict | None:
    """Best GENERIC functional equivalent for a query the library could not match.

    Returns ``{"libid", "family", "caveat"}`` or ``None`` when nothing in the
    starter library plays the requested role. ``None`` is the correct answer for a
    genuinely unavailable function — the caller must then report an honest miss
    rather than substitute something unrelated.
    """
    if not query or not query.strip():
        return None
    q = query.strip()
    for pat, libid, family, caveat in _COMPILED:
        if pat.search(q):
            return {"libid": libid, "family": family, "caveat": caveat}
    return None


def family_index() -> list[dict]:
    """Every symbol in the library, grouped by its library prefix.

    Backs the ``ecad_find_part('list')`` browse capability: on a miss the model
    needs to see which generic families actually EXIST, not a fixed example list
    that happens to omit the MCU and regulator families it was looking for.
    """
    groups: dict[str, list[dict]] = {}
    for libid, sdef in SYMBOLS.items():
        lib = libid.split(":", 1)[0]
        groups.setdefault(lib, []).append({
            "libid": libid,
            "pin_count": len(sdef.get("pins", [])),
            "description": sdef.get("description", ""),
        })
    return [{"library": lib, "count": len(parts),
             "parts": sorted(parts, key=lambda p: p["libid"])}
            for lib, parts in sorted(groups.items())]


def family_names() -> list[str]:
    """Sorted library-prefix names, e.g. ['Amplifier_Operational', 'Battery_Management', ...]."""
    return sorted({libid.split(":", 1)[0] for libid in SYMBOLS})


#: Queries that mean "show me everything" rather than "find this part".
LIST_QUERIES = frozenset({"list", "all", "*", "index", "catalog", "everything",
                          "list all", "list parts", "all parts", "library"})
