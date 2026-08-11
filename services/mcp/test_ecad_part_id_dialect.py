"""ECAD ``part_id`` dialect — the invented ``ecad:<Library>:<Symbol>`` id (2026-07-29).

Four of the six measured failures in the pass-2c confirmation sweep (claude-sonnet-4-5,
ecad turns) were one shape::

    part_get(part_id="ecad:RF_Module:ESP32-WROOM-32")   -> "part_id not found"
    part_get(part_id="ecad:Device:R_0603")              -> "part_id not found"
    part_place(part_id="ecad:RF_Module:ESP32-WROOM-32") -> "unknown part_id ..."

The id SHAPE is right — ``services/parts/adapters/ecad_kicad_adapter.py`` mints every ECAD
catalogue id as ``ecad:<libid>`` — but those symbols are not in the clean-room starter
library (0603 is a footprint size; the symbol is plain ``Device:R``).

Two behaviours are pinned here, and they pull in opposite directions on purpose:

  * spelling noise that CANNOT change which record you get (domain prefix, case) resolves;
  * a near miss is REFUSED, never substituted — but the refusal now names real ids so the
    model can retry without spending a ``part_search`` round trip.

Covers services/ecad/library/partid.py, ecad_kicad_adapter.get and parts_tools._part_id_hint.
"""
from __future__ import annotations

import json

import pytest

from services.ecad.library import partid


def _pin_starter_only() -> None:
    """Pin the ECAD catalogue to the clean-room starter tier for the current test.

    Two independent caches have to be reset, and missing either makes these tests pass or
    fail on collection order:

    * the Library SINGLETON's native dirs. ``ecad_make_part`` registers one when it stores
      a part, and ``register_ecad_sch_tools`` now registers the user's real library dir at
      tool-registration time. Both outlive the call that caused them.
    * the parts HUB singleton, whose FTS index caches PartRecords. A record indexed while a
      generated part existed survives the part's own directory being deleted, so
      ``part_get`` would answer from the index long after the library forgot the part.
    """
    from services import parts as _parts
    from services.ecad.library.native import get_default_library
    lib = get_default_library()
    lib._native_dirs.clear()
    lib.clear_cache()
    _parts._hub = None  # rebuilt lazily against the pinned library


@pytest.fixture(autouse=True)
def _starter_only_library(monkeypatch):
    """Pin the ECAD catalogue to the clean-room starter tier for every test here.

    Without this, a part another test generated (RF_Module:ESP32-WROOM-32 really is one)
    makes the measured-failure ids resolve and these tests pass or fail on collection order.

    The parts index is also forced IN-MEMORY. Its on-disk snapshot lives at
    ``<app data>/cache/parts_index.sqlite`` and is revalidated against a fingerprint of
    static data files only (hub._fingerprint_sources), so a record indexed during a test
    run would otherwise survive in a developer's cache indefinitely — and these tests would
    answer from whatever that cache happens to hold.
    """
    from services import parts as _parts
    from services.ecad.library.native import get_default_library
    monkeypatch.setenv("CHUDWARE_PARTS_INDEX_CACHE", "0")
    lib = get_default_library()
    saved = list(lib._native_dirs)
    _pin_starter_only()
    try:
        yield
    finally:
        lib._native_dirs[:] = saved
        lib.clear_cache()
        # Hand the next module a hub that matches the restored library and its own cache
        # setting, not one built against our pinned in-memory view.
        _parts._hub = None


class _StubMCP:
    """Collects @mcp.tool()-decorated functions by name (no FastMCP needed)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _tools() -> dict:
    from services.mcp.parts_tools import register_parts_tools
    stub = _StubMCP()
    register_parts_tools(stub)
    return stub.tools


def _call(tools: dict, name: str, **kw) -> dict:
    out = tools[name](**kw)
    assert isinstance(out, str), f"{name} must return a JSON string, got {type(out)}"
    return json.loads(out)


# --------------------------------------------------------------- exact-or-refuse resolution


def test_spelling_noise_resolves_to_the_one_real_id() -> None:
    """Case and a missing/doubled 'ecad:' prefix cannot change WHICH record you get."""
    tools = _tools()
    for written in ("ecad:Device:R", "ecad:device:r", "ECAD:Device:R",
                    "Device:R", "ecad:ecad:Device:R", "  ecad:Device:R  "):
        got = _call(tools, "part_get", part_id=written)
        assert got["ok"], f"{written!r} must resolve: {got}"
        assert got["part"]["part_id"] == "ecad:Device:R", \
            f"{written!r} resolved to the WRONG record: {got['part']['part_id']}"
        assert got["part"]["domain"] == "ecad", got["part"]


def test_a_near_miss_is_never_resolved_to_a_substitute() -> None:
    """'Device:R_0603' looks like a resistor and is NOT one — refusing beats guessing.

    Resolving it to ``ecad:Resistor_SMD:R_0603`` would cross the symbol/footprint boundary;
    resolving it to ``ecad:Device:R`` would silently drop the 0603 the model asked for.
    """
    for near in ("ecad:Device:R_0603", "ecad:RF_Module:ESP32-WROOM-32",
                 "ecad:Device:R0603", "ecad:Resistor_SMD:R_0604"):
        assert partid.resolve(near) is None, \
            f"{near!r} must NOT resolve — got {partid.resolve(near)}"
    tools = _tools()
    for near in ("ecad:Device:R_0603", "ecad:RF_Module:ESP32-WROOM-32"):
        got = _call(tools, "part_get", part_id=near)
        assert got["ok"] is False, f"{near!r} must fail, not return a substitute: {got}"
        assert "part" not in got, got


# ------------------------------------------------------------------ the refusal is useful


def test_part_get_hint_names_a_real_candidate_id() -> None:
    """The dead-end 'use part_search' hint now carries the search RESULT.

    ``ecad:Resistor_SMD:R_0603`` is the real 0603 record and ``ecad:Device:R`` is the real
    resistor symbol; both are named so the next call can be correct.
    """
    got = _call(_tools(), "part_get", part_id="ecad:Device:R_0603")
    assert got["ok"] is False, got
    hint = got["hint"]
    assert "'ecad:Resistor_SMD:R_0603'" in hint, hint
    assert "'ecad:Device:R'" in hint, hint
    assert "ecad_place_symbol" in hint, "the hint must name the tool that takes a libid"


def test_part_place_hint_names_a_real_candidate_and_places_nothing() -> None:
    """The measured part_place failure. It must stay a refusal AND stop being a dead end."""
    got = _call(_tools(), "part_place", part_id="ecad:RF_Module:ESP32-WROOM-32")
    assert got["ok"] is False, f"an unknown id must never place a substitute: {got}"
    for invented in ("doc_id", "entity_id", "domain"):
        assert invented not in got, f"a refused placement leaked {invented}: {got}"
    hint = got["hint"]
    assert "'ecad:RF_Module:GPS_Castellated_1x6'" in hint, \
        f"the hint must name a real id from the library the model asked for: {hint}"
    assert all("ESP32" not in c["part_id"]
               for c in partid.candidates("ecad:RF_Module:ESP32-WROOM-32")), \
        "no candidate may be an invented ESP32 id"


def test_a_genuinely_unknown_id_fails_without_inventing_anything() -> None:
    """No library, no keyword, no candidate — the refusal must stay honest."""
    got = _call(_tools(), "part_place", part_id="ecad:Frobnicate:Xyzzy_9000")
    assert got["ok"] is False, got
    assert "Frobnicate" not in str(partid.candidates("ecad:Frobnicate:Xyzzy_9000")), \
        "an unknown library must not manufacture candidate ids"
    assert partid.candidates("ecad:Frobnicate:Xyzzy_9000") == [], \
        partid.candidates("ecad:Frobnicate:Xyzzy_9000")
    assert "Closest real catalogue ids" not in got["hint"], got["hint"]
    assert "ecad_find_part" in got["hint"], got["hint"]


def test_every_candidate_is_a_real_catalogue_id() -> None:
    """The hint is search output, so each id it names must actually exist."""
    tools = _tools()
    for written in ("ecad:Device:R_0603", "ecad:RF_Module:ESP32-WROOM-32",
                    "ecad:MCU:STM32F103C8T6", "ecad:Connector:USB_C_Receptacle"):
        for cand in partid.candidates(written):
            got = _call(tools, "part_get", part_id=cand["part_id"])
            assert got["ok"], f"hint for {written!r} named a dead id {cand['part_id']!r}"


def test_non_ecad_ids_keep_their_own_hint() -> None:
    """A mechanical id is not ECAD's to explain — the resolver must stay out of it."""
    assert partid.hint("fastener:ISO4762:M8") == ""
    assert partid.hint("mech_family:shcs:M5") == ""
    assert partid.hint("") == ""
    assert partid.resolve("fastener:ISO4762:M8") is None
    got = _call(_tools(), "part_get", part_id="fastener:NOT_A_STANDARD:M99")
    assert got["ok"] is False, got
    assert "well-formed ECAD id" not in got["hint"], got["hint"]
    assert "part_search" in got["hint"], got["hint"]


def test_hint_never_raises_on_hostile_input() -> None:
    """It decorates an error; it must not become one."""
    for junk in (None, "", ":", "::::", "ecad:", "ecad::", 42, ["ecad:Device:R"],
                 "ecad:" + "x" * 5000):
        assert isinstance(partid.hint(junk), str)
        assert isinstance(partid.candidates(junk), list)
        assert partid.resolve(junk) is None or isinstance(partid.resolve(junk), dict)


# ----------------------------------------------------- the KiCad standard-library dialect
# 'Device', 'RF_Module', 'Connector_Generic' and 'power' are literal KiCad standard-library
# names, and KiCad's Library:Symbol convention is what KiCad MCP servers expose — so the
# model was reaching for a convention it was trained on, not hallucinating. Our starter
# library uses the same convention, so most KiCad ids already match exactly; partid's
# _KICAD_IDENTITY table covers the rest, and ONLY where the device is genuinely the same.

# (KiCad spelling, the one native id it must resolve to)
KICAD_IDENTITIES = [
    ("Device:R_Small", "ecad:Device:R"),
    ("Device:R_US", "ecad:Device:R"),
    ("ecad:Device:C_Small", "ecad:Device:C"),
    ("Device:CP", "ecad:Device:C_pol"),
    ("Device:C_Polarized", "ecad:Device:C_pol"),
    ("Device:L_Small", "ecad:Device:L"),
    ("Device:D_Small", "ecad:Device:D"),
    ("ecad:Device:LED_Small", "ecad:Device:LED"),
    ("Device:D_Zener", "ecad:Device:Zener"),
    ("Device:Thermistor_NTC", "ecad:Device:NTC"),
    ("Device:FerriteBead", "ecad:Device:Ferrite_Bead"),
    ("ecad:Device:Q_NPN_BEC", "ecad:Device:Q_NPN"),
    ("Device:Q_PNP_BEC", "ecad:Device:Q_PNP"),
    ("Device:Q_NMOS_GSD", "ecad:Device:NMOS"),
    ("Device:Q_PMOS_GSD", "ecad:Device:PMOS"),
    ("Switch:SW_SPST", "ecad:Device:Switch"),
    ("Connector_Generic:Conn_01x02", "ecad:Connector:Conn_2"),
    ("Connector_Generic:Conn_01x04", "ecad:Connector:Conn_4"),
    ("power:+3.3V", "ecad:power:+3V3"),
    ("Regulator_Linear:LM7805_TO220", "ecad:Regulator_Linear:7805"),
    ("Timer:NE555P", "ecad:Timer:NE555"),
    ("Sensor_Motion:MPU-6050", "ecad:Sensor_Motion:MPU6050"),
    # Pin-ORDER variants: the same device under a different pin NUMBERING, carried by an
    # explicit audited permutation. The numbering itself is asserted below.
    ("Device:Q_NPN_BCE", "ecad:Device:Q_NPN"),
    ("Device:Q_NPN_ECB", "ecad:Device:Q_NPN"),
    ("Device:Q_PNP_CBE", "ecad:Device:Q_PNP"),
    ("Device:Q_NMOS_DGS", "ecad:Device:NMOS"),
    ("Device:Q_PMOS_SGD", "ecad:Device:PMOS"),
    ("Timer:LM555", "ecad:Timer:NE555"),
    ("Connector_Generic:Conn_02x03_Odd_Even", "ecad:Connector:Header_2x03"),
    ("Connector_Generic:Conn_02x03_Counter_Clockwise", "ecad:Connector:Header_2x03"),
    ("Connector_Generic:Conn_02x05_Counter_Clockwise", "ecad:Connector:Header_2x05"),
]

# (KiCad id, its permutation onto OUR pin numbers, the pin NAME of each KiCad pin).
# This is the load-bearing assertion of the remap feature: not "it resolved" but "KiCad pin
# 2 of a BCE transistor is our pin 3, and that pin really is the collector".
KICAD_PIN_REMAPS = [
    # KiCad Device library states a discrete's pin order in the suffix, one letter per pin
    # in pin-number order; ours is the SOT-23 JEDEC order 1=B, 2=E, 3=C (starter.py).
    ("Device:Q_NPN_BCE", {"1": "1", "2": "3", "3": "2"}, {"1": "B", "2": "C", "3": "E"}),
    ("Device:Q_NPN_ECB", {"1": "2", "2": "3", "3": "1"}, {"1": "E", "2": "C", "3": "B"}),
    ("Device:Q_PNP_CBE", {"1": "3", "2": "1", "3": "2"}, {"1": "C", "2": "B", "3": "E"}),
    # ours: 1=G, 2=S, 3=D (2N7002/BSS84 SOT-23 class).
    ("Device:Q_NMOS_DGS", {"1": "3", "2": "1", "3": "2"}, {"1": "D", "2": "G", "3": "S"}),
    ("Device:Q_PMOS_SGD", {"1": "2", "2": "1", "3": "3"}, {"1": "S", "2": "G", "3": "D"}),
    # The bipolar 555 under another vendor's part number: same DIP-8 datasheet pinout, and
    # all eight pins are stated so the audit is on the record rather than implied.
    ("Timer:LM555",
     {str(i): str(i) for i in range(1, 9)},
     {"1": "GND", "2": "TRIG", "3": "OUT", "4": "RST",
      "5": "CV", "6": "THR", "7": "DIS", "8": "VCC"}),
    # KiCad's 2xNN _Counter_Clockwise is the DIP topology (1..N down the first row, then
    # N+1..2N back UP the second); ours numbers odd-left/even-right, i.e. _Odd_Even.
    ("Connector_Generic:Conn_02x03_Counter_Clockwise",
     {"1": "1", "2": "3", "3": "5", "4": "6", "5": "4", "6": "2"},
     {"1": "Pin_1", "2": "Pin_3", "3": "Pin_5", "4": "Pin_6", "5": "Pin_4", "6": "Pin_2"}),
    ("Connector_Generic:Conn_02x03_Odd_Even",
     {"1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6"},
     {"1": "Pin_1", "2": "Pin_2", "3": "Pin_3", "4": "Pin_4", "5": "Pin_5", "6": "Pin_6"}),
]

# KiCad ids that LOOK like a spelling of one of ours and denote something else. Each must
# refuse — and each refusal must still name a real id so the model is not left guessing.
# NONE of these is a pin-NUMBERING question, so the permutation field cannot rescue them.
KICAD_NON_IDENTITIES = [
    "Device:Crystal",                      # HSE or 32k768? picking is a substitution
    "Device:D_Schottky",                   # not the generic diode
    "Device:R_Shunt",                      # KiCad's is 4-terminal Kelvin; ours is 2-pin
    "Switch:SW_Push",                      # momentary; our Switch has no such semantics
    "Timer:ICM7555",                       # CMOS silicon, not a spelling of the bipolar 555
    "Timer:TLC555",
    "Connector_Generic:Conn_02x03_Row_Letter_First",   # numbers pins a1..b3, not 1..6
    "Connector_Generic:Conn_02x03_Row_Letter_Last",
    "Connector_Generic:Conn_02x03_Top_Bottom",         # second row's start pad unverified
]


def test_kicad_standard_library_spellings_resolve_and_are_echoed() -> None:
    """A KiCad spelling of a device we HAVE resolves to that one id, and says it did."""
    tools = _tools()
    for written, expect in KICAD_IDENTITIES:
        hit = partid.resolve(written)
        assert hit is not None, f"{written!r} must resolve to {expect}"
        assert hit["part_id"] == expect, \
            f"{written!r} resolved to {hit['part_id']} instead of {expect}"
        assert expect in hit["note"] and "KiCad" in hit["note"], hit["note"]
        got = _call(tools, "part_get", part_id=written)
        assert got["ok"] and got["part"]["part_id"] == expect, got
        echo = " ".join(got.get("interpreted") or [])
        assert expect in echo and written in echo, \
            f"an id rewrite must be echoed, not silent: {got.get('interpreted')}"


def test_kicad_pin_order_and_subtype_variants_are_refused_with_a_real_candidate() -> None:
    """The deliberate NON-aliases. Binding any of these would report success on a part with
    different pins or different silicon — the worst outcome available here."""
    tools = _tools()
    for written in KICAD_NON_IDENTITIES:
        assert partid.resolve(written) is None, \
            f"{written!r} must NOT resolve — got {partid.resolve(written)}"
        got = _call(tools, "part_get", part_id=written)
        assert got["ok"] is False and "part" not in got, got
        found = partid.candidates(written)
        assert found, f"{written!r} refused with no candidate to retry with"
        assert found[0]["part_id"] in got["hint"], \
            f"the refusal must name a real id: {got['hint']}"


def test_kicad_identity_table_only_points_at_ids_that_exist() -> None:
    """A stale row would turn a resolvable id into a dead one — resolve() must refuse it,
    so prove every row's target is really in the library."""
    from services.ecad.library.starter import FOOTPRINTS, SYMBOLS
    for key, entry in partid._KICAD_IDENTITY.items():
        assert entry.native in SYMBOLS or entry.native in FOOTPRINTS, \
            f"_KICAD_IDENTITY[{key!r}] points at {entry.native!r}, which does not exist"
        assert partid.resolve(entry.native) is not None, entry.native


def test_kicad_pin_order_suffix_rows_match_our_real_pin_numbering() -> None:
    """The load-bearing audit: a '_BEC'/'_GSD'/'_BCE' suffix is a PIN-NUMBER claim — KiCad
    names the device's pin order one letter per pin. Read through the row's permutation it
    must land on the native pin of that NAME, or the mapping silently moves every wire drawn
    to that device onto the wrong leg."""
    from services.ecad.library.starter import SYMBOLS
    checked = 0
    for key, entry in partid._KICAD_IDENTITY.items():
        suffix = key.rpartition("_")[2]
        if len(suffix) != 3 or not set(suffix) <= set("bcegds"):
            continue
        pins = {str(p["number"]): str(p["name"]) for p in SYMBOLS[entry.native]["pins"]}
        remap = entry.pins or {str(n): str(n) for n in pins}
        actual = "".join(pins.get(remap.get(str(n), ""), "?") for n in (1, 2, 3)).lower()
        assert actual == suffix, \
            f"_KICAD_IDENTITY[{key!r}] -> {entry.native!r} claims pins {suffix.upper()} " \
            f"but its permutation {remap} lands on {actual.upper()}"
        checked += 1
    assert checked >= 24, f"the pin-order audit checked only {checked} rows"


def test_remapped_kicad_ids_resolve_to_the_right_pin_identities() -> None:
    """The permutation feature's reason to exist: a remapped id must land on the pin that
    carries the SIGNAL the caller named, not merely resolve.

    ``Device:Q_NPN_BCE`` pin 2 is the collector. Our Q_NPN numbers the collector 3. So the
    resolved table must say number='2' -> native_number='3', name='C'. Asserting only that
    resolution succeeded is what would let an off-by-one permutation ship.
    """
    for written, want_remap, want_names in KICAD_PIN_REMAPS:
        hit = partid.resolve(written)
        assert hit is not None, f"{written!r} must resolve"
        assert hit["pin_remap"] == want_remap, \
            f"{written!r} permutation is {hit['pin_remap']}, expected {want_remap}"
        table = partid.resolved_pins(written)
        assert table is not None, f"{written!r} produced no pin table"
        got_names = {p["number"]: p["name"] for p in table}
        got_native = {p["number"]: p["native_number"] for p in table}
        assert got_names == want_names, \
            f"{written!r} pin NAMES resolved to {got_names}, expected {want_names}"
        assert got_native == want_remap, \
            f"{written!r} pin NUMBERS resolved to {got_native}, expected {want_remap}"
        # and the native symbol really does carry that name on that number
        from services.ecad.library.starter import SYMBOLS
        native_pins = {str(p["number"]): str(p["name"])
                       for p in SYMBOLS[hit["libid"]]["pins"]}
        for caller, native_num in want_remap.items():
            assert native_pins[native_num] == want_names[caller], \
                (f"{written!r} pin {caller} maps to {hit['libid']} pin {native_num}, which "
                 f"is {native_pins[native_num]}, not {want_names[caller]}")


def test_a_remap_is_stated_out_loud_and_reaches_part_get() -> None:
    """A renumbering the caller cannot see is a renumbering it cannot obey. It must be in
    resolve()'s note, on the PartRecord, and in part_get's interpreted notes."""
    hit = partid.resolve("Device:Q_NPN_BCE")
    assert "PIN REMAP" in hit["note"], hit["note"]
    assert "pin 2 (C) is pin 3" in hit["note"], hit["note"]
    got = _call(_tools(), "part_get", part_id="ecad:Device:Q_NPN_BCE")
    assert got["ok"], got
    rec = got["part"]
    assert rec["part_id"] == "ecad:Device:Q_NPN"
    assert {p["number"]: p["native_number"] for p in rec["pin_remap"]} == \
        {"1": "1", "2": "3", "3": "2"}, rec["pin_remap"]
    echo = " ".join(got.get("interpreted") or [])
    assert "PIN REMAP" in echo and "your pin 2 (C) = pin 3" in echo, got.get("interpreted")
    # An id with NO permutation must not grow a phantom one.
    plain = _call(_tools(), "part_get", part_id="ecad:Device:R_Small")
    assert plain["ok"] and "pin_remap" not in plain["part"], plain["part"]


def test_a_partial_permutation_is_refused_not_identity_filled() -> None:
    """The guard the whole feature hangs on. A permutation that does not name every pin of
    the native symbol is a partial GUESS; completing it with an identity fallback would wire
    some legs right and some wrong while reporting success. It must refuse instead."""
    entry = partid._Entry("Timer:NE555", {"1": "1", "2": "2", "3": "3"})  # 3 of 8 pins
    problem = partid._remap_problem(entry)
    assert "covers 3 of 8 pins" in problem, problem
    for missing in ("4", "5", "6", "7", "8"):
        assert missing in problem, problem

    # ... and resolve() refuses such a row rather than falling back to identity.
    key = partid._fold("Timer:Bogus555")
    partid._KICAD_IDENTITY[key] = entry
    try:
        assert partid.resolve("Timer:Bogus555") is None, \
            "a partial permutation must refuse, not identity-map"
        assert partid.resolved_pins("Timer:Bogus555") is None
    finally:
        del partid._KICAD_IDENTITY[key]

    # The other two ways a permutation can be wrong, both refused for the same reason.
    dupe = partid._Entry("Device:Q_NPN", {"1": "1", "2": "3", "3": "3"})
    assert "more than one source pin" in partid._remap_problem(dupe)
    alien = partid._Entry("Device:Q_NPN", {"1": "1", "2": "2", "3": "9"})
    assert "does not have" in partid._remap_problem(alien)

    # And the table itself refuses to load a bad row at all.
    partid._KICAD_IDENTITY[key] = dupe
    try:
        with pytest.raises(RuntimeError, match="not one-to-one covers"):
            partid._audit_remaps()
    finally:
        del partid._KICAD_IDENTITY[key]
    partid._audit_remaps()  # the real table is clean


def test_every_tool_named_in_a_hint_actually_exists() -> None:
    """A hint that recommends a phantom tool is the same dead end it exists to remove.

    ``ecad_make_part`` is the answer when the library genuinely has no such part: it builds
    a symbol + footprint from a caller-supplied pinout and stores it under the very libid
    that just failed. Naming it is only useful if it is really registered.
    """
    import re

    from services.mcp.ecad_tools import register_ecad_tools
    ecad = _StubMCP()
    register_ecad_tools(ecad)
    _pin_starter_only()  # tool registration registers the real user library dir
    known = set(ecad.tools) | set(_tools())

    texts = [partid.hint("ecad:RF_Module:ESP32-WROOM-32"),   # no candidates at all
             partid.hint("ecad:Device:R_0603"),              # candidates present
             partid.hint("Device:R_Shunt")]                   # a refused KiCad variant
    named: set = set()
    for text in texts:
        assert text, "these three must all produce a hint"
        named |= set(re.findall(r"\b([a-z][a-z0-9_]{3,})\(", text))
    assert "ecad_make_part" in named, f"the create path must be offered: {named}"
    assert {"ecad_place_symbol", "ecad_find_part"} <= named, named
    unknown = named - known
    assert not unknown, f"hint names tool(s) that do not exist: {sorted(unknown)}"


def test_make_part_hint_carries_the_pinout_caveat_and_the_failed_libid() -> None:
    """ecad_make_part refuses a part name with no pin list, so the hint must not send the
    model there empty-handed — and it must pass the libid that failed, or the created part
    lands under 'Generated:<name>' and the original id still will not resolve."""
    text = partid.hint("ecad:RF_Module:ESP32-WROOM-32")
    assert "libid='RF_Module:ESP32-WROOM-32'" in text, text
    assert "pins=[" in text and "datasheet" in text, text
    assert "'ecad:RF_Module:ESP32-WROOM-32' then resolves" in text, text


def test_a_generated_part_becomes_fetchable_under_the_id_the_hint_promised(tmp_path) -> None:
    """The hint promises "'ecad:<id>' then resolves on the next call" — prove it does.

    ecad_make_part writes into the NATIVE library tier. Before this slice the ECAD parts
    adapter enumerated only the starter library and user .kicad_sym files, so a part the
    agent had just created resolved through ecad_place_symbol but was invisible to
    part_get/part_place — and partid.candidates() could name an id part_get could not
    fetch. This is the round trip that pins the two views together.
    """
    from services.ecad.library import generate as gen
    from services.ecad.library.native import get_default_library

    libid = "RF_Module:TEST-WROOM-9"
    part_id = "ecad:" + libid
    tools = _tools()

    # Absent to begin with (the fixture pinned us to the starter tier).
    assert partid.resolve(part_id) is None
    assert _call(tools, "part_get", part_id=part_id)["ok"] is False

    gen.make_part(
        "TEST-WROOM-9",
        [{"number": "1", "name": "GND", "type": "power_in", "side": "left"},
         {"number": "2", "name": "3V3", "type": "power_in", "side": "left"},
         {"number": "3", "name": "TXD", "type": "output", "side": "right"},
         {"number": "4", "name": "RXD", "type": "input", "side": "right"}],
        libid=libid, directory=str(tmp_path), overwrite=True)
    assert str(tmp_path) in get_default_library()._native_dirs

    hit = partid.resolve(part_id)
    assert hit is not None and hit["part_id"] == part_id, hit
    assert partid.hint(part_id) == "", "a resolvable id must not be taught about"

    got = _call(_tools(), "part_get", part_id=part_id)
    assert got["ok"], f"a generated part must be fetchable by part_id: {got}"
    assert got["part"]["part_id"] == part_id, got["part"]
    assert got["part"]["params"]["pin_count"] == 4, got["part"]["params"]


def test_a_symbol_and_its_footprint_under_one_id_yield_one_placeable_record(tmp_path) -> None:
    """ecad_make_part accepts fpid == libid, which made ONE part_id produce TWO records.

    The index kept whichever it saw last; the footprint record's geometry_ref carries no
    ``libid``, so part_place answered "geometry_ref missing 'libid'" for a part that had
    just been created successfully. The symbol must win — it is the placeable one and it
    already names its footprint.
    """
    from services.ecad.library import generate as gen
    from services.parts.adapters.ecad_kicad_adapter import _load_native_dirs

    libid = "RF_Module:TEST-COLLIDE-1"
    gen.make_part(
        "TEST-COLLIDE-1",
        [{"number": "1", "name": "GND", "type": "power_in", "side": "left"},
         {"number": "2", "name": "VCC", "type": "power_in", "side": "right"}],
        libid=libid, fpid=libid, package="module_castellated",
        pitch=1.27, body_w=18.0, body_h=25.5, pad_len=1.5, pad_width=0.9,
        directory=str(tmp_path), overwrite=True)

    ids = [r["part_id"] for r in _load_native_dirs()]
    assert len(ids) == len(set(ids)), f"one part_id must yield one record: {ids}"

    got = _call(_tools(), "part_get", part_id="ecad:" + libid)
    assert got["ok"], got
    assert got["part"]["params"]["kind"] == "symbol", \
        f"the SYMBOL must win the id, not the footprint: {got['part']['params']}"
    assert got["part"]["geometry_ref"]["libid"] == libid, \
        f"geometry_ref must carry the libid part_place needs: {got['part']['geometry_ref']}"
