"""ECAD part-ID and KiCad library resolution regressions.

The older Python starter-library spelling helpers are tested independently.
Parts tools must prefer exact native KiCad IDs and never substitute a different
symbol or pin numbering when the requested ID is absent.
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
    if isinstance(out, list):
        from mcp.server.fastmcp import Image
        assert len(out) == 2 and isinstance(out[0], Image) and isinstance(out[1], str), out
        out = out[1]
    assert isinstance(out, str), f"{name} must return JSON text, got {type(out)}"
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
    for near in ("ecad:Device:R_0603", "ecad:RF_Module:NO_SUCH_TEST_PART_9Z9",
                 "ecad:Device:R0603", "ecad:Resistor_SMD:R_0604"):
        assert partid.resolve(near) is None, \
            f"{near!r} must NOT resolve — got {partid.resolve(near)}"
    tools = _tools()
    for near in ("ecad:Device:R_0603", "ecad:RF_Module:NO_SUCH_TEST_PART_9Z9"):
        got = _call(tools, "part_get", part_id=near)
        assert got["ok"] is False, f"{near!r} must fail, not return a substitute: {got}"
        assert "part" not in got, got


# ------------------------------------------------------------------ the refusal is useful


def test_part_get_hint_points_to_native_discovery() -> None:
    got = _call(_tools(), "part_get", part_id="ecad:Device:R_0603")
    assert got["ok"] is False, got
    assert "part_search" in got["hint"]
    assert "ecad_symbols" in got["hint"]
    assert "ecad_find_footprint" in got["hint"]


def test_part_place_unknown_id_places_nothing() -> None:
    got = _call(_tools(), "part_place", part_id="ecad:RF_Module:NO_SUCH_TEST_PART_9Z9")
    assert got["ok"] is False, got
    for invented in ("doc_id", "entity_id", "domain"):
        assert invented not in got, got
    assert "part_search" in got["hint"]


def test_a_genuinely_unknown_id_fails_without_inventing_anything() -> None:
    """No library, no keyword, no candidate — the refusal must stay honest."""
    got = _call(_tools(), "part_place", part_id="ecad:Frobnicate:Xyzzy_9000")
    assert got["ok"] is False, got
    assert "Frobnicate" not in str(partid.candidates("ecad:Frobnicate:Xyzzy_9000")), \
        "an unknown library must not manufacture candidate ids"
    assert partid.candidates("ecad:Frobnicate:Xyzzy_9000") == [], \
        partid.candidates("ecad:Frobnicate:Xyzzy_9000")
    assert "Closest real catalogue ids" not in got["hint"], got["hint"]
    assert "ecad_symbols" in got["hint"], got["hint"]


def test_candidate_search_ignores_numeric_only_tokens_but_keeps_related_hits(monkeypatch):
    calls = []

    def search(query):
        calls.append(query)
        if query == "9000":
            return [{"libid": "Isolator:HCPL-9000", "score": 50}]
        if query == "esp32":
            return [{"libid": "RF_Module:ESP32-WROOM-32", "score": 50}]
        return []

    monkeypatch.setattr(partid, "_search_hits", search)
    assert partid.candidates("ecad:Frobnicate:Xyzzy_9000") == []
    assert "9000" not in calls
    related = partid.candidates("ecad:RF_Module:ESP32-WROOM-32")
    assert any(r["part_id"] == "ecad:RF_Module:ESP32-WROOM-32" for r in related)
    assert "esp32" in calls
    assert any(r["part_id"] == "ecad:Resistor_SMD:R_0603"
               for r in partid.candidates("ecad:Device:R_0603"))


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
    ("Sensor_Motion:MPU-6050", "ecad:Sensor_Motion:MPU6050"),
    # Pin-ORDER variants: the same device under a different pin NUMBERING, carried by an
    # explicit audited permutation. The numbering itself is asserted below.
    ("Device:Q_NPN_BCE", "ecad:Device:Q_NPN"),
    ("Device:Q_NPN_ECB", "ecad:Device:Q_NPN"),
    ("Device:Q_PNP_CBE", "ecad:Device:Q_PNP"),
    ("Device:Q_NMOS_DGS", "ecad:Device:NMOS"),
    ("Device:Q_PMOS_SGD", "ecad:Device:PMOS"),
    ("Timer:LM555", "ecad:Timer:NE555P"),
    ("Timer:SA555", "ecad:Timer:NE555P"),
    ("Connector_Generic:Conn_02x03_Odd_Even", "ecad:Connector:Header_2x03"),
    ("Connector_Generic:Conn_02x03_Counter_Clockwise", "ecad:Connector:Header_2x03"),
    ("Connector_Generic:Conn_02x05_Counter_Clockwise", "ecad:Connector:Header_2x05"),
]

# KiCad's eight physical timer pins, shared by its PDIP and SOIC variants.
TIMER_PINS = {"1": "GND", "2": "TRIG", "3": "OUT", "4": "~{RST}",
              "5": "CONT", "6": "THRES", "7": "DISCH", "8": "VCC"}
TIMER_IDENTITY_REMAP = {str(i): str(i) for i in range(1, 9)}

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
    ("Timer:LM555", TIMER_IDENTITY_REMAP, TIMER_PINS),
    ("Timer:SA555", TIMER_IDENTITY_REMAP, TIMER_PINS),
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


def test_starter_identity_table_is_independently_audited() -> None:
    """The Python starter helper's aliases remain independent of native lookup."""
    for written, expect in KICAD_IDENTITIES:
        hit = partid.resolve(written)
        assert hit is not None and hit["part_id"] == expect
        assert expect in hit["note"] and "KiCad" in hit["note"], hit["note"]


def test_timer_package_ids_resolve_exactly() -> None:
    """The PDIP and SOIC IDs must stay distinct rather than becoming aliases."""
    from services.ecad.library.starter import SYMBOLS

    tools = _tools()
    for libid, footprint in (
        ("Timer:NE555P", "Package_DIP:DIP-8_W7.62mm"),
        ("Timer:NE555D", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"),
    ):
        hit = partid.resolve(libid)
        assert hit is not None, libid
        assert hit["part_id"] == "ecad:" + libid and hit["libid"] == libid, hit
        assert hit["kind"] == "symbol" and hit["pin_remap"] is None and not hit["note"], hit
        pins = partid.resolved_pins(libid)
        assert pins is not None, libid
        assert {pin["number"]: pin["name"] for pin in pins} == TIMER_PINS
        assert {pin["number"]: pin["native_number"] for pin in pins} == TIMER_IDENTITY_REMAP
        assert SYMBOLS[libid]["default_footprint"] == footprint

        got = _call(tools, "part_get", part_id="ecad:" + libid, include_live=False)
        assert got["ok"], got
        assert got["part"]["part_id"] == "ecad:" + libid, got
        assert got["part"]["geometry_ref"]["libid"] == libid, got
        assert got["part"]["params"]["pin_count"] == 8, got
        assert "pin_remap" not in got["part"], got


def test_kicad_pin_order_and_subtype_variants_are_refused_with_a_real_candidate() -> None:
    """The deliberate NON-aliases. Binding any of these would report success on a part with
    different pins or different silicon — the worst outcome available here."""
    for written in KICAD_NON_IDENTITIES:
        assert partid.resolve(written) is None, \
            f"{written!r} must NOT resolve in the starter helper"
        assert partid.candidates(written), f"{written!r} has no starter candidate"


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


def test_kicad_native_symbol_is_not_remapped_to_starter(monkeypatch) -> None:
    from services.mcp import core_bridge

    def rpc(method, params):
        if method == "ecad_symbols":
            return {"ok": True, "symbols": [
                {"libid": "Device:R_Small", "name": "R_Small",
                 "description": "Resistor", "pin_count": 2, "footprint": ""}]}
        if method == "ecad_footprints":
            return {"ok": True, "footprints": []}
        raise AssertionError(method)

    monkeypatch.setattr(core_bridge, "call", rpc)
    got = _call(_tools(), "part_get", part_id="ecad:Device:R_Small", include_live=False)
    assert got["ok"], got
    assert got["part"]["part_id"] == "ecad:Device:R_Small"
    assert got["part"]["geometry_ref"]["libid"] == "Device:R_Small"
    assert "pin_remap" not in got["part"]


def test_a_partial_permutation_is_refused_not_identity_filled() -> None:
    """The guard the whole feature hangs on. A permutation that does not name every pin of
    the native symbol is a partial GUESS; completing it with an identity fallback would wire
    some legs right and some wrong while reporting success. It must refuse instead."""
    entry = partid._Entry("Timer:NE555P", {"1": "1", "2": "2", "3": "3"})  # 3 of 8 pins
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
    searched = _call(tools, "part_search", query="TEST WROOM 9", domain="ecad",
                     include_live=False, detail="full")
    assert any(r["part_id"] == part_id for r in searched["results"]), searched


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


def test_live_ecad_placement_reports_lock_errors(monkeypatch):
    from services.mcp import ecad_common
    from services.mcp.parts_tools import _place_ecad_part

    session = ecad_common.EcadSession("sch-1", {"schema": "aicad.sch/1"}, revision=3)
    monkeypatch.setattr(ecad_common.ecad_session, "get_session", lambda _: session)
    monkeypatch.setattr(ecad_common.ecad_session, "bind_unbound", lambda s: s)

    def locked(*_args):
        raise ecad_common.OpError("kicad_locked", "KiCad has locked the document",
                                  {"applied": False})

    monkeypatch.setattr(ecad_common, "_apply", locked)
    result = _place_ecad_part(
        {"geometry_ref": {"kind": "symbol_footprint", "libid": "Device:R"}},
        None, "sch-1", {"pos": [4, 5]})
    assert result["ok"] is False
    assert result["code"] == "kicad_locked"
    assert result["detail"]["applied"] is False


def test_shared_library_id_places_the_exact_board_footprint(monkeypatch):
    from services.mcp import ecad_common
    from services.mcp.parts_tools import _place_ecad_part

    session = ecad_common.EcadSession("pcb-1", {
        "schema": "aicad.pcb/1", "footprints": []}, revision=3)
    monkeypatch.setattr(ecad_common.ecad_session, "get_session", lambda _: session)
    bound = []
    monkeypatch.setattr(ecad_common.ecad_session, "bind_unbound",
                        lambda s: bound.append(s) or s)
    applied = []

    def apply(target, ops):
        applied.extend(ops)
        target.doc["footprints"].append({"id": "fp1", "fpid": ops[0]["fpid"],
                                         "ref": "U1", "pos": ops[0]["pos"]})
        return {"id_map": {"$0": "fp1"}, "revision": 4}

    monkeypatch.setattr(ecad_common, "_apply", apply)
    record = {"geometry_ref": {"kind": "symbol_footprint", "libid": "Module:X",
                               "footprint": "Other:Default", "board_footprint": "Module:X"}}
    out = _place_ecad_part(record, None, "pcb-1", {"pos": [2, 3]})
    assert out["ok"], out
    assert applied[0]["fpid"] == "Module:X"
    assert bound == [session]
    assert out["entity_id"] == "fp1"


def test_kicad_library_parts_search_place_and_reopen(tmp_path):
    """Use native KiCad libraries and verify the saved schematic and board."""
    from services.mcp import core_bridge

    try:
        status = core_bridge.call("ecad_kicad_status", {})
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"native KiCad bridge unavailable: {exc}")
    if not status.get("found") or not status.get("symbol_dir") or not status.get("footprint_dir"):
        pytest.skip("KiCad and its symbol/footprint libraries are required")

    sessions = []
    try:
        sch_path = tmp_path / "parts.kicad_sch"
        pcb_path = tmp_path / "parts.kicad_pcb"
        sch = core_bridge.call("ecad_new", {
            "kind": "sch", "name": "parts", "path": str(sch_path)})
        pcb = core_bridge.call("ecad_new", {
            "kind": "pcb", "name": "parts", "path": str(pcb_path)})
        sessions.extend([sch["doc_id"], pcb["doc_id"]])
        tools = _tools()

        sym_id = "ecad:RF_Module:ESP32-WROOM-32"
        found = _call(tools, "part_search", query="ESP32-WROOM-32", domain="ecad",
                      doc_id=sch["doc_id"], include_live=False, detail="full")
        sym = next(r for r in found["results"] if r["part_id"] == sym_id)
        assert sym["params"]["pin_count"] >= 30
        assert sym["geometry_ref"]["footprint"] == "RF_Module:ESP32-WROOM-32"
        detached = _call(tools, "part_place", part_id=sym_id, doc=sch["tree"],
                         params={"pos": [1, 2]}, include_live=False)
        assert detached["ok"], detached
        assert detached["saved"] is False and detached["path"] is None
        assert detached["doc"]["symbols"][-1]["libid"] == "RF_Module:ESP32-WROOM-32"
        assert not sch["tree"]["symbols"]
        placed = _call(tools, "part_place", part_id=sym_id, doc_id=sch["doc_id"],
                       params={"pos": [10, 20], "ref": "U1"}, include_live=False)
        assert placed["ok"], placed
        assert placed["saved"] is True and placed["path"] == str(sch_path)
        assert placed["entity_id"]
        assert placed["doc"]["symbols"][-1]["libid"] == "RF_Module:ESP32-WROOM-32"
        assert len(placed["doc"]["lib_cache"]["RF_Module:ESP32-WROOM-32"]["pins"]) >= 30
        module_fp = _call(tools, "part_place", part_id=sym_id, doc_id=pcb["doc_id"],
                          params={"pos": [20, 20], "ref": "U1"}, include_live=False)
        assert module_fp["ok"], module_fp
        assert module_fp["saved"] is True and module_fp["path"] == str(pcb_path)
        assert module_fp["doc"]["footprints"][-1]["fpid"] == "RF_Module:ESP32-WROOM-32"
        assert len(module_fp["doc"]["footprints"][-1]["pads"]) >= 30

        fp_id = "ecad:Resistor_SMD:R_0603_1608Metric"
        found_fp = _call(tools, "part_get", part_id=fp_id, doc_id=pcb["doc_id"],
                         include_live=False)
        assert found_fp["ok"], found_fp
        assert found_fp["part"]["params"]["pad_count"] == 2
        placed_fp = _call(tools, "part_place", part_id=fp_id, doc=pcb["doc_id"],
                          params={"pos": [4, 6], "ref": "R1"}, include_live=False)
        assert placed_fp["ok"], placed_fp
        assert placed_fp["doc"]["footprints"][-1]["fpid"] == "Resistor_SMD:R_0603_1608Metric"
        assert len(placed_fp["doc"]["footprints"][-1]["pads"]) == 2

        reopened_sch = core_bridge.call("ecad_open", {"path": str(sch_path)})
        reopened_pcb = core_bridge.call("ecad_open", {"path": str(pcb_path)})
        sessions.extend([reopened_sch["doc_id"], reopened_pcb["doc_id"]])
        assert any(s["libid"] == "RF_Module:ESP32-WROOM-32"
                   for s in reopened_sch["tree"]["symbols"])
        assert any(f["fpid"] == "Resistor_SMD:R_0603_1608Metric" and len(f["pads"]) == 2
                   for f in reopened_pcb["tree"]["footprints"])
    finally:
        for session in sessions:
            core_bridge.call("ecad_close", {"doc_id": session})


def test_project_library_search_and_placement_use_doc_context(tmp_path):
    from pathlib import Path
    from services.mcp import core_bridge

    try:
        status = core_bridge.call("ecad_kicad_status", {})
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"native KiCad bridge unavailable: {exc}")
    if not status.get("found") or not status.get("symbol_dir"):
        pytest.skip("KiCad symbol libraries are not installed")

    library = Path(status["symbol_dir"]) / "Device.kicad_sym"
    if not library.is_file():
        pytest.skip("KiCad Device symbol library is not installed")
    (tmp_path / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) '
        f'(lib (name "ProjectDevice") (type "KiCad") (uri "{library}") '
        '(options "") (descr "")))', encoding="utf-8")
    sch = core_bridge.call("ecad_new", {
        "kind": "sch", "name": "project", "path": str(tmp_path / "project.kicad_sch")})
    try:
        tools = _tools()
        part_id = "ecad:ProjectDevice:R_Small"
        result = _call(tools, "part_search", query="ProjectDevice:R_Small",
                       domain="ecad", doc_id=sch["doc_id"], include_live=False, detail="full")
        assert any(r["part_id"] == part_id for r in result["results"]), result
        part = _call(tools, "part_get", part_id=part_id, doc_id=sch["doc_id"],
                     include_live=False)
        assert part["ok"], part
        placed = _call(tools, "part_place", part_id=part_id, doc_id=sch["doc_id"],
                       params={"pos": [0, 0]}, include_live=False)
        assert placed["ok"], placed
        assert placed["doc"]["symbols"][-1]["libid"] == "ProjectDevice:R_Small"
        assert len(placed["doc"]["lib_cache"]["ProjectDevice:R_Small"]["pins"]) == 2
    finally:
        core_bridge.call("ecad_close", {"doc_id": sch["doc_id"]})
