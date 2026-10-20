"""Shared session state + helpers for the ECAD MCP tool family modules (T-RF8 split of the
former ecad_tools.py god file): the _ECAD session registry, autosave/recovery, provider
budgets, symbol/pin/net helpers, ERC formatting, and the module-level sim summarizers.
Imported by the ecad_*_tools family modules; re-exported through the ecad_tools facade."""

from __future__ import annotations

import copy
import json
import math
import os
from typing import Any, Optional, Union

from services.mcp import result_serving as _rs
from services.mcp import core_bridge as cb

# T-RF20: the ECAD document/session/netlist/ERC/DRC kernel now lives in the C++ core
# (core/ecad/*, dispatched via the ecad_*/sim_* RPCs — see worker_client.cpp). The direct
# engine.ecad.{session,netlist,schema,ops} imports are replaced by the RPC-backed shims
# below (ecad_session facade, netlist_mod facade, schema facade, OpError). Modules with NO
# C++ counterpart stay as direct imports (re-homed by another T-RF leaf):
#   - services.ecad.library.search/resolve (no ecad_library RPC; reads starter/kicad_sym data)
#   - services.ecad.metadata (advisory eng-metadata layer, Python-only)
#   - services.ecad.autosave (lazy, crash-recovery; Python-only)
from services.ecad.library import search as lib_search  # STAYS: no ecad_library RPC surface

try:  # T-FIN8 engineering-metadata layer — advisory, must never break the tools
    from services.ecad import metadata as _eng_metadata  # STAYS: no C++ counterpart
except Exception:  # noqa: BLE001
    _eng_metadata = None  # type: ignore[assignment]


class OpError(Exception):
    """A structured op error (ecad_ops.md §7). Carries ``code`` + ``detail`` for the RPC envelope.

    Local home after T-RF20: raised by tool code exactly like engine.ecad.ops.OpError, and
    translated from core_bridge.CoreError (the C++ core's {code,message,detail} envelope) by
    ``_rpc`` so the family modules keep a single except type.
    """

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_envelope(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _rpc(method: str, params: dict | None = None) -> dict:
    """One serialized C++ core RPC with CoreError -> OpError translation."""
    try:
        # Core calls from handed-off and foreground ECAD tools share one bridge/session boundary.
        # Provider-only budget jobs never touch this lock, so fallback/status remains responsive.
        with _ECAD_OPERATION_LOCK:
            return cb.call(method, params or {})
    except cb.CoreError as e:
        raise OpError(e.code or "engine_error", str(e),
                      e.detail if isinstance(e.detail, dict) else {}) from e


# --------------------------------------------------------------------------- schema helpers
#
# Pure doc-dict geometry/predicates ported verbatim from engine.ecad.schema (the doc — with
# its embedded lib_cache — is fetched from the C++ session, so the helpers run unchanged on
# the client side; there is no per-helper RPC).

_SCHEMA_SCH = "aicad.sch/1"
_SCHEMA_PCB = "aicad.pcb/1"


def _is_schematic(doc: dict) -> bool:
    return doc.get("schema") == _SCHEMA_SCH


def _is_board(doc: dict) -> bool:
    return doc.get("schema") == _SCHEMA_PCB


def _doc_kind(doc: dict) -> str:
    return "sch" if _is_schematic(doc) else "pcb" if _is_board(doc) else ""


def _rot(x: float, y: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (x * c - y * s, x * s + y * c)


def _apply_mirror(x: float, y: float, mirror: str) -> tuple[float, float]:
    if mirror == "x":
        return (-x, y)
    if mirror == "y":
        return (x, -y)
    return (x, y)


def _local_to_world(p, pos, angle: float = 0.0, mirror: str = "none") -> tuple[float, float]:
    """Transform a local point: mirror, then rotate CCW by ``angle``, then translate by ``pos``."""
    x, y = float(p[0]), float(p[1])
    x, y = _apply_mirror(x, y, mirror)
    x, y = _rot(x, y, angle)
    return (x + float(pos[0]), y + float(pos[1]))


def _pin_world(symbol: dict, pin: dict) -> tuple[float, float]:
    """World endpoint of a symbol pin (the connection point — the tip away from the body)."""
    px, py = float(pin["pos"][0]), float(pin["pos"][1])
    length = float(pin.get("length", 2.54))
    a = math.radians(float(pin.get("angle", 0.0)))
    tip = (px + length * math.cos(a), py + length * math.sin(a))
    return _local_to_world(tip, symbol["pos"], symbol.get("angle", 0.0),
                           symbol.get("mirror", "none"))


def _pad_world(footprint: dict, pad: dict) -> tuple[float, float]:
    """World position of a footprint pad. Bottom-side footprints are mirrored across x."""
    mirror = "x" if footprint.get("side") == "bottom" else "none"
    return _local_to_world(pad["pos"], footprint["pos"], footprint.get("angle", 0.0), mirror)


def _symbol_def(doc: dict, libid: str) -> dict | None:
    """Resolve a symbol definition from the doc's embedded lib_cache."""
    return doc.get("lib_cache", {}).get(libid)


def _iter_placed_pins(doc: dict):
    """Yield ``(symbol, pin, world_xy)`` for every pin of every placed symbol."""
    cache = doc.get("lib_cache", {})
    for sym in doc.get("symbols", []):
        sdef = cache.get(sym.get("libid"))
        if not sdef:
            continue
        unit = sym.get("unit", 1)
        for pin in sdef.get("pins", []):
            if pin.get("unit", 1) not in (unit, 0):
                continue
            yield sym, pin, _pin_world(sym, pin)


class _SchemaFacade:
    """Drop-in for the old ``from engine.ecad import schema`` (pure helpers only)."""

    is_schematic = staticmethod(_is_schematic)
    is_board = staticmethod(_is_board)
    doc_kind = staticmethod(_doc_kind)
    pin_world = staticmethod(_pin_world)
    pad_world = staticmethod(_pad_world)
    symbol_def = staticmethod(_symbol_def)
    iter_placed_pins = staticmethod(_iter_placed_pins)


schema = _SchemaFacade()


# --------------------------------------------------------------------------- C++ session facade
#
# ECAD documents live in the C++ core (core/ecad/session.cpp): ecad_new/ecad_open mint an
# "ecadcpp" doc; every mutation is an ecad_apply_ops RPC whose response carries
# {revision, kind, tree (the full doc), compile {netlist|erc|drc|ratsnest}}. This facade
# mirrors the engine.ecad.session API the tools were written against.

class EcadSession:
    """doc_id handle to a C++ ECAD session; mirrors engine.ecad.session.EcadSession's attrs."""

    def __init__(self, doc_id: str, doc: dict, revision: int = 0,
                 compile_field: dict | None = None, path: str | None = None) -> None:
        self.doc_id = doc_id
        self.doc = doc
        self.revision = revision
        self.last_compile = compile_field
        self.path = path
        self.dirty = False

    @property
    def kind(self) -> str:
        return _doc_kind(self.doc)

    def _update(self, resp: dict) -> None:
        if isinstance(resp.get("tree"), dict):
            self.doc = resp["tree"]
        if isinstance(resp.get("revision"), int):
            self.revision = resp["revision"]
        if isinstance(resp.get("compile"), dict):
            self.last_compile = resp["compile"]


class _SessionFacade:
    """Drop-in for the old ``from engine.ecad import session as ecad_session``."""

    EcadSession = EcadSession

    def __init__(self) -> None:
        self._by_id: dict[str, EcadSession] = {}
        self._alias: dict[str, str] = {}  # old doc_id -> new doc_id (swap_doc re-mints)

    @property
    def _REGISTRY(self) -> dict[str, EcadSession]:
        """engine.ecad.session._REGISTRY compat (tests clear it between phases)."""
        return self._by_id

    @staticmethod
    def _autosave(session: EcadSession) -> None:
        """Crash-recovery persist after every mutation (mirrors where engine.ecad.session
        called autosave_session; no-op unless armed). engine.ecad.autosave STAYS (no C++
        counterpart)."""
        try:
            from services.ecad import autosave as _eauto  # STAYS (T-RF20): no C++ counterpart
            _eauto.autosave_session(session)
        except Exception:  # noqa: BLE001
            pass

    def _resolve(self, doc_id: str) -> str:
        seen = set()
        while doc_id in self._alias and doc_id not in seen:
            seen.add(doc_id)
            doc_id = self._alias[doc_id]
        return doc_id

    def _register(self, s: EcadSession) -> EcadSession:
        self._by_id[s.doc_id] = s
        return s

    # -- engine.ecad.session API surface -------------------------------------

    def new_session(self, kind: str = "sch", name: str | None = None) -> EcadSession:
        resp = _rpc("ecad_new", {"kind": kind, "name": name or ""})
        s = EcadSession(resp["doc_id"], resp.get("tree") or {},
                        revision=resp.get("revision", 0),
                        compile_field=resp.get("compile"))
        self._register(s)
        self._autosave(s)  # persist from creation (mirrors engine.ecad.session.new_session)
        return s

    def get_session(self, doc_id: str) -> EcadSession:
        did = self._resolve(doc_id)
        # Always refetch (ecad_tree) — in daemon mode another client may have advanced the doc.
        resp = _rpc("ecad_tree", {"doc_id": did})
        s = self._by_id.get(did)
        if s is None:
            s = EcadSession(did, resp.get("tree") or {})
        s._update(resp)
        return self._register(s)

    def open_session(self, doc: dict, path: str | None = None) -> EcadSession:
        """engine.ecad.session.open_session: adopt an inline doc dict (autosave recovery)."""
        resp = _mint_from_doc(doc)
        s = EcadSession(resp["doc_id"], resp.get("tree") or {},
                        revision=resp.get("revision", 0),
                        compile_field=resp.get("compile"), path=path)
        return self._register(s)

    def iter_sessions(self) -> list[EcadSession]:
        return list(self._by_id.values())

    def close_session(self, doc_id: str) -> bool:
        """engine.ecad.session.close_session: close the C++ doc and drop the handle."""
        real = self._resolve(doc_id)
        try:
            _rpc("ecad_close", {"doc_id": real})
        except OpError:
            return False
        self._by_id.pop(real, None)
        if doc_id != real:
            self._alias.pop(doc_id, None)
        try:
            from services.ecad import autosave as _eauto
            _eauto.remove(real)  # closing removes the crash-recovery file (no resurrection)
        except Exception:  # noqa: BLE001
            pass
        return True

    def apply_ops(self, session: EcadSession, base_revision: int | None, ops: list[dict],
                  emit=None) -> dict:
        resp = _rpc("ecad_apply_ops", {
            "doc_id": session.doc_id,
            "base_revision": base_revision if base_revision is not None else -1,
            "ops": _attach_lib_defs(session, ops),
        })
        session._update(resp)
        self._autosave(session)
        return resp

    def rebuild(self, session: EcadSession, emit=None) -> dict:
        resp = _rpc("ecad_rebuild", {"doc_id": session.doc_id})
        session._update(resp)
        return resp

    def tree(self, session: EcadSession) -> dict:
        resp = _rpc("ecad_tree", {"doc_id": session.doc_id})
        session._update(resp)
        return resp

    def swap_doc(self, session: EcadSession, new_doc: dict, *, base_revision: int | None = None,
                 warnings: list[str] | None = None, emit=None) -> dict:
        """Wholesale doc replacement via the ecad_swap_doc RPC (T-RF21): validate ->
        undo snapshot -> swap -> revision+1 -> compile, all inside the C++ session, so
        doc_id and undo history survive (the pre-RPC re-mint restarted both)."""
        resp = _rpc("ecad_swap_doc", {
            "doc_id": session.doc_id,
            "doc": new_doc,
            "base_revision": base_revision if base_revision is not None else -1,
            "warnings": list(warnings) if warnings else [],
        })
        session._update(resp)
        self._register(session)
        self._autosave(session)
        return resp


ecad_session = _SessionFacade()


def _attach_lib_defs(session: EcadSession, ops: list[dict]) -> list[dict]:
    """Inline resolved symbol/footprint defs on add ops that lack one (T-RF20).

    The Python op handlers fell back to engine.ecad.library when an op carried no ``def``;
    the C++ core has NO library server-side (core/ecad/ops.cpp ensure_lib_def / resolve_fp_def
    stub instead — a stub corrupts ref-prefix annotation, pin geometry, and pads). The library
    module itself has no RPC surface (STAYS), so resolution happens here at the seam, exactly
    where the old op-layer fallback sat.
    """
    lib = None
    out: list[dict] = []
    for op in ops:
        if isinstance(op, dict) and "def" not in op:
            kind = op.get("op")
            key = ("libid" if kind == "sch_add_symbol"
                   else "fpid" if kind == "pcb_add_footprint" else None)
            ident = op.get(key) if key else None
            if isinstance(ident, str) and ident and not (
                    key == "libid" and ident in (session.doc.get("lib_cache") or {})):
                if lib is None:
                    try:
                        from services.ecad import library as _lib  # STAYS: no ecad_library RPC
                        lib = _lib
                    except Exception:  # noqa: BLE001
                        lib = False
                if lib:
                    try:
                        d = (lib.resolve(ident) if key == "libid"
                             else (lib.resolve_footprint(ident)
                                   if hasattr(lib, "resolve_footprint")
                                   else lib.resolve(ident)))
                        if isinstance(d, dict):
                            op = {**op, "def": d}
                    except Exception:  # noqa: BLE001 — library is best-effort, never hard-fail
                        pass
        out.append(op)
    return out


def _mint_from_doc(doc: dict) -> dict:
    """Mint a C++ session owning an inline doc dict via a temp .ecad+ container + ecad_open.

    (No ecad_swap_doc / ecad_mint RPC surface yet — see swap_doc.) Returns the ecad_open
    ops-response (+ doc_id)."""
    import tempfile
    import zipfile

    kind = _doc_kind(doc)
    meta = {
        "container": "aicad.ecad+/1",
        "app_version": "mcp-swap",
        "kind": kind,
        "schema": doc.get("schema", ""),
        "created": "",
        "modified": "",
        "units": doc.get("units") if isinstance(doc.get("units"), dict) else {},
        "name": doc.get("name", "Untitled"),
    }
    tmp = tempfile.NamedTemporaryFile(suffix=".ecad+", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("meta.json", json.dumps(meta))
            z.writestr("document.json", json.dumps(doc))
        return _rpc("ecad_open", {"path": tmp.name})
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


class _NetlistFacade:
    """Drop-in for ``from engine.ecad import netlist as netlist_mod``.

    build_netlist(doc) resolves the doc's owning C++ session by its embedded ``id`` and
    returns the session's compiled netlist (the C++ core compiles netlist/ERC on every
    apply/tree — core/ecad/session.cpp compile_field)."""

    def build_netlist(self, doc: dict) -> dict:
        did = doc.get("id") if isinstance(doc, dict) else None
        if not did:
            raise OpError("unknown_doc", "netlist: doc carries no session id", {})
        s = ecad_session.get_session(str(did))
        nl = (s.last_compile or {}).get("netlist")
        if nl is None:
            ecad_session.rebuild(s)
            nl = (s.last_compile or {}).get("netlist")
        nl = nl or {}
        # C++ netlist.cpp parity gaps vs engine/ecad/netlist.py, enriched at the seam:
        #  - pin-NAME aliases on comp["pins"] (netlist.py:451-461, Bug E3): a subckt-backed
        #    part whose sim.pin_order is by name (op-amp IN+/IN-/OUT, 555) otherwise gets
        #    "?" nodes and simulates to garbage. Only an UNAMBIGUOUS name (one pin of the
        #    placed unit carries it) is aliased; a name never shadows a number key.
        #  - per-symbol sim_params extras (netlist.py:478-487): ac_mag/ac_phase (AC sweep
        #    stimulus, Bug E-1) and eng_overrides (T-FIN8 metadata).
        by_ref = {sym.get("ref"): sym for sym in doc.get("symbols", [])}
        cache = doc.get("lib_cache", {}) or {}
        for comp in nl.get("components", []) or []:
            sym = by_ref.get(comp.get("ref")) or {}
            sdef = cache.get(sym.get("libid")) or {}
            comp_pins = comp.get("pins")
            if isinstance(comp_pins, dict):
                unit = sym.get("unit", 1)
                unit_pins = [p for p in sdef.get("pins", []) if p.get("unit", 1) in (unit, 0)]
                name_counts: dict = {}
                for p in unit_pins:
                    nm = p.get("name")
                    if nm and nm != "~" and str(nm) != str(p.get("number", "")):
                        name_counts[nm] = name_counts.get(nm, 0) + 1
                for p in unit_pins:
                    nm = p.get("name")
                    if not nm or nm == "~" or name_counts.get(nm, 0) != 1 or nm in comp_pins:
                        continue
                    num = str(p.get("number", ""))
                    if num in comp_pins:
                        comp_pins[nm] = comp_pins[num]
            sp = sym.get("sim_params") or {}
            if sp.get("ac_mag") is not None:
                comp.setdefault("ac_mag", sp["ac_mag"])
            if sp.get("ac_phase") is not None:
                comp.setdefault("ac_phase", sp["ac_phase"])
            if isinstance(sp.get("eng"), dict):
                comp.setdefault("eng_overrides", sp["eng"])
            # Third gap: the Python netlist->circuit translator fell back to the starter
            # SYMBOLS library for a component's "sim" mapping (spice.py:766-770); the C++
            # translator has no library server-side but DOES honor a "sim" carried on the
            # component (pin_order, model). Attach it from the doc's lib_cache so Q/M/subckt
            # parts get the right node order (pin_order) and model server-side.
            sim_def = sdef.get("sim")
            if sim_def is not None:
                comp.setdefault("sim", sim_def)
        return nl


netlist_mod = _NetlistFacade()


# Default footprint by libid (Bug E6) — verbatim from engine/ecad/sync.py::_DEFAULT_FOOTPRINTS.
# The C++ sync (core/ecad/sync.cpp) has NO defaulting table, so the seam applies it to the
# inline netlist it hands to ecad_update_from_sch_inline. Sources / power symbols are
# intentionally absent (never on a board).
_DEFAULT_FOOTPRINTS = {
    "Device:R": "Resistor_SMD:R_0603",
    "Device:C": "Capacitor_SMD:C_0603",
    "Device:C_pol": "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
    "Device:L": "Inductor_SMD:L_0603",
    "Device:Ferrite_Bead": "Inductor_SMD:FB_0603",
    "Device:D": "Diode_SMD:D_SMA",
    "Device:LED": "LED_SMD:LED_0805",
    "Device:Q_NPN": "Package_TO_SOT_SMD:SOT-23",
    "Device:Q_PNP": "Package_TO_SOT_SMD:SOT-23",
    "Device:NMOS": "Package_TO_SOT_SMD:SOT-23",
    "Device:PMOS": "Package_TO_SOT_SMD:SOT-23",
    "Amplifier_Operational:OPAMP_dual": "Package_SO:SOIC-8",
    "Amplifier_Operational:LM358": "Package_SO:SOIC-8",
    "Timer:NE555": "Package_DIP:DIP-8",
    "Regulator_Linear:AMS1117-3.3": "Package_TO_SOT_SMD:SOT-223",
    "Regulator_Linear:7805": "Package_TO_SOT_THT:TO-220-3",
    "MCU:Generic_QFP48": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
    "Connector:Header_2x03": "Connector_PinHeader_2.54mm:Header_2x03",
    "Connector:Header_2x05": "Connector_PinHeader_2.54mm:Header_2x05",
    "Connector:USB_C": "Connector_USB:USB_C",
    "Connector:USB_B": "Connector_USB:USB_B",
    "Sensor_Motion:MPU6050": "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
    "Sensor_Pressure:BMP280": "Package_LGA:LGA-8_2x2.5mm",
    "Sensor_Magnetic:LIS3MDL": "Package_LGA:LGA-12_2x2mm",
    "RF_GPS:NEO6M": "RF_Module:GPS_Castellated_1x6",
    "Regulator_Switching:Buck_3V3": "Package_TO_SOT_SMD:SOT-23-6",
    "Device:Crystal_32k768": "Crystal:Crystal_SMD_3215-2Pin",
    "Device:Crystal_HSE": "Crystal:Crystal_HC49-4H_Vertical",
    "Connector:SWD_2x5": "Connector_PinHeader_2.54mm:Header_2x05",
    "Battery_Management:MCP73831": "Package_TO_SOT_SMD:SOT-23-5",
    "Battery_Management:SwitchingCharger_SOP8": "Package_SO:SOIC-8",
    "Device:NMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Reverse": "Package_TO_SOT_SMD:SOT-23",
    "Device:R_Sense": "Resistor_SMD:R_2512",
    "Power_Management:Load_Switch": "Package_TO_SOT_SMD:SOT-23-5",
    "Power_Management:LVD_Comparator": "Package_TO_SOT_SMD:SOT-23-5",
    "Device:NTC": "Resistor_SMD:R_0603",
    "Device:L_Power": "Inductor_SMD:L_Power_7x7",
    # Live 2026-07-30 (ESP32 board flow): Conn_2/Conn_3/Conn_4/Header_1x4 are real starter
    # SYMBOLS that ecad_find_part('header') offers, but they had no default footprint here,
    # so `placeable()` in core/ecad/sync.cpp rejected them and update_from_sch dropped both
    # headers off the board WITHOUT listing them in `unresolved` — an invisible loss.
    "Connector:Conn_2": "Connector_PinHeader_2.54mm:Header_1x2",
    "Connector:Conn_3": "Connector_PinHeader_2.54mm:Header_1x3",
    "Connector:Conn_4": "Connector_PinHeader_2.54mm:Header_1x4",
    "Connector:Header_1x4": "Connector_PinHeader_2.54mm:Header_1x4",
}


def _is_power_component(comp: dict) -> bool:
    """Mirror of core/ecad/sync.cpp::is_power_component — power ports never take a footprint."""
    ref = str((comp or {}).get("ref") or "")
    libid = str((comp or {}).get("libid") or "")
    return ref.startswith("#") or libid.startswith("power:")


def _library():
    """The Python ECAD library module, or None. STAYS (T-RF20): no ecad_library RPC."""
    try:
        from services.ecad import library as _lib
        return _lib
    except Exception:  # noqa: BLE001
        return None


def _default_footprint_for(libid: str) -> str:
    """The fpid a symbol should land on when the schematic assigned none.

    Beyond the static table: a part built by ``ecad_make_part`` generates BOTH a symbol
    ``Generated:<name>`` and a footprint ``Generated:<name>_<package>``, but nothing links
    them — so the part the model just authored (an ESP32 module, live 2026-07-30) was
    dropped by sync while its own footprint sat in the library. Recover the pairing by
    the generator's own naming convention rather than leaving the part off the board.
    """
    lid = str(libid or "")
    dflt = _DEFAULT_FOOTPRINTS.get(lid, "")
    if dflt:
        return dflt
    lib = _library()
    if lib is None or ":" not in lid:
        return ""
    ns, name = lid.split(":", 1)
    if ns != "Generated" or not name:
        return ""
    for pkg in ("qfn", "qfp", "lqfp", "soic", "sop", "ssop", "tssop", "dfn", "lga", "sot",
                "dip", "header", "module_castellated", "0402", "0603", "0805", "1206"):
        cand = f"Generated:{name}_{pkg}"
        try:
            if isinstance(lib.resolve_footprint(cand), dict):
                return cand
        except Exception:  # noqa: BLE001 — library lookup is best-effort
            pass
    return ""


def _next_fp_id(board: dict) -> str:
    """Next free ``fpN`` id on *board* (mirrors core/ecad/schema.cpp::next_id)."""
    n = 0
    for fp in board.get("footprints") or []:
        fid = str((fp or {}).get("id") or "")
        if fid.startswith("fp") and fid[2:].isdigit():
            n = max(n, int(fid[2:]))
    return f"fp{n + 1}"


def _rescue_unresolved_footprints(board: dict, report: dict, comps_by_ref: dict) -> list[str]:
    """Add the footprints core/ecad/sync.cpp could not resolve, using the PYTHON library.

    Measured 2026-07-30: 20 of the 27 fpids ``_DEFAULT_FOOTPRINTS`` assigns are absent from
    the C++ ``library::resolve_footprint`` table (every regulator, connector, crystal,
    inductor, QFN/QFP/LGA package), while ``services.ecad.library`` resolves all of them.
    Sync therefore reported ``unresolved`` and moved on, so a nine-part ESP32 schematic
    synced to five footprints and one net and STILL answered "All multi-pad nets routed".
    This closes the parity gap the same way ``_attach_lib_defs`` closes it for ops: resolve
    at the seam and write the footprint into the board dict that is about to be swapped in.

    Mirrors core/ecad/sync.cpp::new_footprint + bind_pads. Returns the rescued refs; the
    ones the Python library cannot resolve either stay in ``report['unresolved']``.
    """
    unresolved = [u for u in (report.get("unresolved") or []) if len(u) == 2]
    if not unresolved:
        return []
    lib = _library()
    if lib is None:
        return []
    fps = board.setdefault("footprints", [])
    nets = board.setdefault("nets", [])
    known_nets = {n.get("name") for n in nets if isinstance(n, dict)}
    origin = board.get("origin") if isinstance(board.get("origin"), list) else [0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])
    grid_i = len(fps)
    rescued: list[str] = []
    still: list = []
    bound = 0
    for ref, fpid in unresolved:
        try:
            fpdef = lib.resolve_footprint(fpid)
        except Exception:  # noqa: BLE001
            fpdef = None
        comp = comps_by_ref.get(ref)
        if not isinstance(fpdef, dict) or not isinstance(comp, dict):
            still.append([ref, fpid])
            continue
        pads = copy.deepcopy(fpdef.get("pads") or [])
        pins = comp.get("pins") if isinstance(comp.get("pins"), dict) else {}
        for pad in pads:
            key = str(pad.get("pin") or pad.get("number") or "")
            net = pins.get(key)
            pad["net"] = net if isinstance(net, str) and net else None
            if pad["net"]:
                bound += 1
                if pad["net"] not in known_nets:
                    nets.append({"name": pad["net"], "class": "default"})
                    known_nets.add(pad["net"])
        # 25.4 mm grid, matching core/ecad/sync.cpp kGridPitch/kGridCols.
        fps.append({
            "id": _next_fp_id(board), "fpid": fpid, "ref": ref,
            "value": str(comp.get("value") or fpdef.get("value") or ""),
            "pos": [ox + (grid_i % 6) * 25.4, oy + (grid_i // 6) * 25.4],
            "angle": 0.0, "side": "top", "pads": pads,
            "graphics": copy.deepcopy(fpdef.get("graphics") or []),
            "attr": fpdef.get("attr", "smd"),
            "linked_symbol": str(comp.get("sym") or ""),
            "libid": str(comp.get("libid") or ""),
        })
        grid_i += 1
        rescued.append(ref)
    report["unresolved"] = still
    report["added"] = sorted(set(list(report.get("added") or []) + rescued))
    report["rescued"] = rescued
    report["nets_bound"] = int(report.get("nets_bound") or 0) + bound
    return rescued


def _update_from_sch(board_session, sch_session, *, prune: bool = False,
                     place_origin=None, base_revision: int | None = None) -> dict:
    """engine.ecad.rpc._ecad_update_from_sch, composed over the inline C++ sync RPC.

    The STATEFUL C++ ecad_update_from_sch builds its netlist server-side WITHOUT the Bug-E6
    default-footprint table (core/ecad/sync.cpp gap) and without the seam netlist enrichment,
    so a schematic whose parts carry no explicit footprint would sync to 0 footprints. This
    mirrors the Python worker handler exactly: build the (enriched) netlist client-side, run
    ecad_update_from_sch_inline, swap the mutated board back, attach the sync report.
    """
    if not _is_board(board_session.doc):
        raise OpError("invalid_op",
                      "ecad_update_from_sch: board_doc_id is not a pcb document",
                      {"reason": "wrong doc kind", "doc_id": board_session.doc_id})
    if not _is_schematic(sch_session.doc):
        raise OpError("invalid_op",
                      "ecad_update_from_sch: sch_doc_id is not a sch document",
                      {"reason": "wrong doc kind", "doc_id": sch_session.doc_id})
    netlist = dict(netlist_mod.build_netlist(sch_session.doc))
    no_footprint: list = []
    for comp in netlist.get("components", []) or []:
        fpid = comp.get("footprint")
        if not (isinstance(fpid, str) and fpid.strip()):
            dflt = _default_footprint_for(str(comp.get("libid", "")))
            if dflt:
                comp["footprint"] = dflt
            elif not _is_power_component(comp) and not comp.get("dnp"):
                # core/ecad/sync.cpp::placeable() rejects a component with no fpid and the
                # loop `continue`s BEFORE report["unresolved"] — so these parts left no trace
                # at all. Live 2026-07-30: a nine-part ESP32 schematic synced as five
                # footprints and the response still read "5 footprint(s) added ... Next:
                # ecad_autoroute". Name them.
                no_footprint.append([str(comp.get("ref") or ""), str(comp.get("libid") or "")])
    # Carry the schematic's id so sync records board.linked_sch = the source schematic.
    netlist["id"] = sch_session.doc.get("id")
    params: dict = {"board": board_session.doc, "sch_netlist": netlist, "prune": bool(prune)}
    if place_origin is not None:
        params["place_origin"] = place_origin
    result = _rpc("ecad_update_from_sch", params)  # inline path (a "board" object is present)
    mutated = result.get("mutated_board")
    report = result.get("report") or {}
    if not isinstance(mutated, dict):
        raise OpError("engine_error",
                      "ecad_update_from_sch: sync returned no mutated_board", {})
    # C++ sync.cpp::new_footprint drops the component's fields/libid/eng_overrides
    # (engine/ecad/sync.py:230-241, T-FIN8 parity gap) — backfill them from the netlist so
    # board-side metadata resolution (engine.ecad.metadata) works from the board alone.
    comps_by_ref = {c.get("ref"): c for c in netlist.get("components", []) or []}
    for fp in mutated.get("footprints", []) or []:
        comp = comps_by_ref.get(fp.get("ref"))
        if not comp:
            continue
        if not fp.get("libid"):
            fp["libid"] = comp.get("libid", "")
        if not isinstance(fp.get("fields"), dict) and isinstance(comp.get("fields"), dict):
            fp["fields"] = comp["fields"]
        if fp.get("eng_overrides") is None and isinstance(comp.get("eng_overrides"), dict):
            fp["eng_overrides"] = comp["eng_overrides"]

    _rescue_unresolved_footprints(mutated, report, comps_by_ref)
    report["no_footprint"] = no_footprint

    warnings: list[str] = []
    if report.get("unresolved"):
        warnings.append("unresolved footprints (no def found): "
                        + ", ".join(f"{ref}({fpid})" for ref, fpid in report["unresolved"]))
    if no_footprint:
        warnings.append(
            "NOT ON THE BOARD — no footprint assigned: "
            + ", ".join(f"{ref}({libid})" for ref, libid in no_footprint)
            + ". These schematic parts were skipped entirely; their pads and nets do not "
              "exist on the board, so DRC/autoroute cannot see them. Re-place each one with "
              "an explicit footprint (ecad_place_symbol(..., footprint='<fpid>')) or pick a "
              "library part that has one (ecad_find_part).")
    if report.get("removed") and not prune:
        warnings.append("footprints no longer in schematic (not pruned): "
                        + ", ".join(report["removed"]))

    resp = ecad_session.swap_doc(board_session, mutated,
                                 base_revision=base_revision, warnings=warnings or None)
    resp["report"] = report
    resp["doc_id"] = board_session.doc_id
    return resp


def _compile_of(session) -> dict:
    """Fresh compile field {netlist|erc|drc|ratsnest|stats} for a session (one ecad_tree RPC)."""
    ecad_session.tree(session)
    return session.last_compile or {}


# --------------------------------------------------------------------------- sim RPC helpers


def _netlist_to_circuit(nl: dict, sim_params: dict | None = None):
    """engine.sim.spice.netlist_to_spice_circuit -> C++ sim_netlist_to_circuit RPC.
    Returns (circuit, warnings)."""
    resp = _rpc("sim_netlist_to_circuit", {"netlist": nl, "sim_params": sim_params})
    circuit = resp.get("circuit") or {}
    # C++ gap: the Python translator carries transistor polarity on device params
    # (spice.py:860-866 — the seam both solvers read); the C++ port drops it, which
    # silently simulates every schematic PMOS/PNP with INVERTED polarity. Re-attach
    # from the component's sim model (attached by the netlist facade enrichment).
    comps = {c.get("ref"): c for c in nl.get("components", []) or []}
    for d in circuit.get("devices", []) or []:
        if d.get("kind") in ("M", "Q"):
            sim = (comps.get(d.get("name")) or {}).get("sim") or {}
            pol = {"NMOS": "nmos", "PMOS": "pmos", "NPN": "npn", "PNP": "pnp"}.get(
                str(sim.get("model", "")))
            if pol:
                d.setdefault("params", {}).setdefault("type", pol)
    return circuit, list(resp.get("warnings") or [])


def _sanitize_circuit(circuit: dict) -> dict:
    """Drop explicit null ``model``/``value`` keys from circuit devices before sim_spice.

    C++ spice.cpp parity gap: nlohmann .value() returns an explicit null and get<string>
    throws ("type must be string, but is null"), where the Python solver treated null as
    unset (device defaults). The netlist->circuit translators EMIT nulls for model-less
    devices (e.g. an LED), so unsanitized studies crashed the RPC.
    """
    import copy as _copy
    c = _copy.deepcopy(circuit)
    for d in c.get("devices", []) or []:
        for k in ("model", "value"):
            if k in d and d[k] is None:
                del d[k]
        # Second C++ gap: E (VCVS) and G (VCCS) read their gain ONLY from "value"
        # (core/sim/spice.cpp:403-412 dev_value), while the Python devices fall back to
        # params.gain (devices.py VCVS/VCCS: value else _mp("gain", 1.0)) — and the subckt
        # expansion emits the gain in params (e.g. the op-amp's Egain). F/H already have
        # the fallback C++-side. Mirror the Python precedence here.
        if d.get("kind") in ("E", "G") and "value" not in d:
            g = (d.get("params") or {}).get("gain")
            if isinstance(g, (int, float)):
                d["value"] = g
    return c


def _sim_run(circuit: dict, study: dict, backend: str = "mna") -> dict:
    """engine.sim.circuit.simulate -> C++ sim_spice RPC for the internal MNA solver.

    The ngspice subprocess backend has NO C++ counterpart (engine.sim.ngspice_backend shells
    out to the ngspice binary) — that leg STAYS a direct import, mirroring
    circuit.simulate's resolve_backend dispatch so every backend choice degrades honestly.
    """
    # STAYS (T-RF20): no C++ counterpart; re-homed from engine.sim to services/installer.
    from services.installer import ngspice_backend as _ng

    choice, note = _ng.resolve_backend(backend, circuit, study)
    if choice == "ngspice":
        try:
            result = _ng.simulate_ngspice(circuit, study)
            result["backend"] = "ngspice"
            if note:
                result["backend_note"] = note
            return result
        except _ng.NgspiceError as e:
            note = ((note + " ") if note else "") + \
                f"ngspice run failed ({e}) — fell back to the internal MNA solver."
    result = _rpc("sim_spice", {"circuit": _sanitize_circuit(circuit), "study": study})
    result["backend"] = "mna"
    if note:
        result["backend_note"] = note
        result.setdefault("log", []).append(f"backend: {note}")
    return result


def _component_eng(libid: str = "", fpid: str = "", overrides=None):
    """Normalized eng metadata for one component (contracts/ecad/boardphys.md), or None."""
    if _eng_metadata is None:
        return None
    try:
        return _eng_metadata.resolve_eng(libid=libid, fpid=fpid, overrides=overrides)
    except Exception:  # noqa: BLE001 — metadata is advisory
        return None


def _fp_overrides(fp: dict):
    if _eng_metadata is None:
        return None
    try:
        return _eng_metadata.footprint_overrides(fp)
    except Exception:  # noqa: BLE001
        return None


def _power_totals(engs: list):
    """Board power rollup (nulls when unknown — never invented zeros), or None."""
    if _eng_metadata is None:
        return None
    try:
        return _eng_metadata.power_totals([e for e in engs if e])
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- module state
#
# We keep our OWN session bookkeeping (the FastMCP-direct pattern). ``docs`` maps each opened
# doc_id -> its EcadSession; ``current_sch`` / ``current_pcb`` track the "active" document so the
# weak model can omit the doc_id on every call (it usually works on one doc at a time).

_ECAD: dict = {
    "current_sch": None,   # doc_id of the active schematic
    "current_pcb": None,   # doc_id of the active board
    "current": None,       # doc_id of the MOST-RECENTLY active doc (either kind) — what status defaults to
    "out_dir": None,       # last export directory used
}

# Provider-plan serialization for ecad_design (Bug E4). The ENGINE state is fully per-doc (the
# session registry is a doc_id-keyed dict; no global lock), so parallel ecad_design calls do not
# block each other — UNLESS each fires a provider plan at the SAME single-threaded local model
# server, where the second request queues behind the first and BOTH burn the full plan budget,
# overrunning the MCP request deadline (-32001). This NON-BLOCKING flag lets a concurrent
# ecad_design see the provider is busy and take the instant, deterministic recipe path instead of
# stacking provider latency. A plain bool guarded by a Lock (no waiting) — the recipe path always
# produces a verified schematic, so skipping the model here only costs the model's topology choice.
import sys as _sys_ecad
import threading as _threading
import time as _time_ecad
import uuid as _uuid_ecad

from . import cad_common as _cad_jobs

# The core bridge/session registry is not safe to enter concurrently.  Serialize every ECAD RPC;
# get_job polling and provider-only budget work never touch this lock and remain responsive.
_ECAD_OPERATION_LOCK = _threading.RLock()

_PROVIDER_PLAN_LOCK = _threading.Lock()
_PROVIDER_PLAN_BUSY = {"v": False}


# --------------------------------------------------------------------------- crash recovery
#
# ECAD docs live in the C++ core's session registry (core/ecad/session.cpp). A heavy ecad_* tool
# that overruns the MCP client's transport timeout gets the server KILLED, and without on-disk
# backing every open schematic/board is lost — unlike C++ part docs, which autosave + recover
# (cad_tools). The engine.ecad.autosave module (no C++ counterpart — STAYS) persists each doc to
# %APPDATA%/Chudware/ecad_autosave; here we arm it and expose recovery to server._warm_core.

_ECAD_SESSION_ID = _uuid_ecad.uuid4().hex[:12]


def enable_ecad_autosave() -> None:
    """Arm ECAD crash-recovery autosave for this server process. Called once from
    server._warm_core at startup. Best-effort.

    NOTE (T-RF20): with ECAD sessions in the C++ core, autosave is no longer mutation-hooked
    (the Python session drove it before); it fires from _autosave_all_open_ecad — the pre-heavy-op
    sweep — and from recovery. The OpenECAD docs remain invisible to the daemon's own part-doc
    autosave (core/hostd/autosave.cpp), so this stays armed in both engine modes."""
    try:
        from services.ecad import autosave as _eauto
        _eauto.enable(_ECAD_SESSION_ID)
    except Exception:  # noqa: BLE001
        pass


def recover_ecad_autosaved(scope: str = "recent"):
    """Reopen ECAD docs autosaved before a (possibly killed) prior session as fresh C++ sessions,
    and re-point _ECAD['current_*'] at the recovered docs so default-doc tool calls work. Returns
    the recovery list. Best-effort — never raises into startup.

    T-RF20: the autosave FILES are still written by services.ecad.autosave (no C++ counterpart —
    the direct import stays), but recovery now re-mints the doc into the C++ session registry
    (temp .ecad+ container + ecad_open) instead of the deleted Python session registry.

    MONOTONICITY (critical): recovery runs on a BACKGROUND warm thread (server._warm_core), so it
    can finish AFTER a user has already called ecad_new_schematic / ecad_new_pcb / ecad_design on
    the fresh server. We fill ONLY empty current_* defaults — never overwrite a just-created or
    explicitly-selected document. The recovered sessions still land in the session registry and
    remain addressable by their explicit doc_id.
    """
    try:
        from services.ecad import autosave as _eauto  # STAYS: no C++ counterpart
        _eauto.prune()
        files = sorted(_eauto.autosave_dir().glob("*.json"))
    except Exception:  # noqa: BLE001
        return []
    info: list = []  # (path, payload, session_tag, saved_at)
    for p in files:
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            ts = float(payload.get("saved_at", p.stat().st_mtime))
        except Exception:  # noqa: BLE001 — skip a half-written/corrupt file
            continue
        if isinstance(payload, dict):
            info.append((p, payload, payload.get("session"), ts))
    if scope != "all":
        prior = [t for t in info if t[2] != _ECAD_SESSION_ID]  # exclude our own session
        if prior:
            latest = max(prior, key=lambda t: t[3])[2]  # most-recent prior session's tag
            if latest is None:
                prior.sort(key=lambda t: t[3], reverse=True)
                info = prior[:8]
            else:
                info = [t for t in prior if t[2] == latest]
        else:
            info = []
    recovered: list = []
    for p, payload, _tag, _ts in info:
        doc = payload.get("doc")
        if not isinstance(doc, dict) or not (_is_schematic(doc) or _is_board(doc)):
            continue
        try:
            resp = _mint_from_doc(doc)
        except Exception:  # noqa: BLE001 — a bad doc must never break recovery
            continue
        s = ecad_session._register(EcadSession(
            resp["doc_id"], resp.get("tree") or {}, revision=resp.get("revision", 0),
            compile_field=resp.get("compile"), path=payload.get("path")))
        recovered.append({"doc_id": s.doc_id, "kind": s.kind,
                          "name": doc.get("name", ""),
                          "original_doc_id": payload.get("doc_id")})
        # MIGRATE the file: persist the reopened doc under its NEW id (current session tag) and
        # delete the stale prior-session source, so the autosave dir doesn't grow per restart.
        try:
            if _eauto.is_enabled():
                _eauto.autosave_session(s)
                if p.name != f"{s.doc_id}.json":
                    p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 — migration is best-effort; never fail recovery
            pass
    # Re-establish the "active" docs so ecad_status / ecad_simulate / etc. find a default doc after
    # a restart, mirroring how cad_tools.recover_autosaved sets _CUR["doc_id"].
    #
    # CRITICAL: recovery runs on a BACKGROUND warm thread (server._warm_core), so it can finish
    # AFTER a user has already called ecad_new_schematic/ecad_new_pcb on the fresh server. We must
    # NOT clobber an active pointer the user just established — otherwise their brand-new doc is
    # silently shadowed by a stale recovered one and the next op lands in the wrong document.
    # So: pick the last recovered doc of each kind (most-recently autosaved), but only ADOPT it as
    # the active pointer if the user has not already set one.
    picked_sch = picked_pcb = picked_any = None
    for rec in recovered:
        did = rec.get("doc_id")
        if not did:
            continue
        if rec.get("kind") == "sch":
            picked_sch = did
        elif rec.get("kind") == "pcb":
            picked_pcb = did
        picked_any = did
    # Empty-defaults only — never replace a non-None pointer (monotonic recovery).
    if picked_sch and _ECAD["current_sch"] is None:
        _ECAD["current_sch"] = picked_sch
    if picked_pcb and _ECAD["current_pcb"] is None:
        _ECAD["current_pcb"] = picked_pcb
    if picked_any and _ECAD["current"] is None:
        _ECAD["current"] = picked_any
    return recovered


def _autosave_all_open_ecad() -> None:
    """Autosave every open ECAD session before a heavy op, so even a hard transport-timeout kill
    (which the soft budget below can't always pre-empt) can never lose open work. Mirrors
    cad_tools._autosave_all_open for the C++ side. Best-effort."""
    try:
        from services.ecad import autosave as _eauto  # STAYS: no C++ counterpart
        if not _eauto.is_enabled():
            return
        for s in ecad_session.iter_sessions():
            _eauto.autosave_session(s)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- heavy-tool budget
#
# Heavy ECAD calls share the already-exposed get_job(job_id) registry with CAD solves.  Fast
# calls are removed from that registry before returning, so their synchronous contract is
# unchanged.  Calls handed off after the budget remain retrievable until the shared TTL expires,
# with an additional ECAD-specific count bound.  A cap on in-flight ECAD workers prevents an
# unresponsive backend from creating an unbounded number of daemon threads.
try:
    _ECAD_HEAVY_BUDGET_S = float(
        os.environ.get("CHUDWARE_ECAD_HEAVY_BUDGET_S", "") or 30.0)
except (TypeError, ValueError):
    _ECAD_HEAVY_BUDGET_S = 30.0
if not (_ECAD_HEAVY_BUDGET_S > 0.0):
    _ECAD_HEAVY_BUDGET_S = 30.0


try:
    _ECAD_JOB_MAX_RUNNING = max(
        1, int(os.environ.get("CHUDWARE_ECAD_MAX_RUNNING_JOBS", "") or 8))
except (TypeError, ValueError):
    _ECAD_JOB_MAX_RUNNING = 8
try:
    _ECAD_JOB_MAX_RETAINED = max(
        1, int(os.environ.get("CHUDWARE_ECAD_MAX_RETAINED_JOBS", "") or 64))
except (TypeError, ValueError):
    _ECAD_JOB_MAX_RETAINED = 64

_ECAD_BUDGET_LOCAL = _threading.local()


def _trim_ecad_jobs_locked() -> None:
    """Prune terminal ECAD jobs while ``cad_common._JOB_LOCK`` is held."""
    _cad_jobs._evict_old_jobs()
    completed = sorted(
        (float(job.get("done_at") or 0.0), jid)
        for jid, job in _cad_jobs._JOBS.items()
        if job.get("owner") == "ecad" and job.get("status") in ("done", "error")
    )
    for _done_at, jid in completed[:-_ECAD_JOB_MAX_RETAINED]:
        _cad_jobs._JOBS.pop(jid, None)


def _run_with_budget(fn, budget_s: Optional[float] = None):
    """Run ``fn()`` on a bounded daemon worker and hand overruns to ``get_job``.

    Returns ``(status, value)``:
      * ``("done",  result)``    — fn returned within the budget
      * ``("error", exception)`` — fn raised within the budget, or the worker cap was reached
      * ``("timeout", None)``    — fn continues as a retrievable in-process job

    Python cannot safely terminate a running thread.  Handed-off jobs therefore advertise
    cancellation as unsupported and retain their eventual result/error instead of pretending
    that cancellation succeeded.  The timeout formatter consumes the pending job identity from
    the same tool-call frame, avoiding a process-global "last job" race between concurrent calls.
    """
    _ECAD_BUDGET_LOCAL.pending = None
    if budget_s is None:
        budget_s = _ECAD_HEAVY_BUDGET_S
    wait_s = max(1.0, float(budget_s))
    caller = _sys_ecad._getframe(1)
    caller_key = (id(caller), caller.f_code)
    finished = _threading.Event()
    box: dict = {}

    with _cad_jobs._JOB_LOCK:
        _trim_ecad_jobs_locked()
        running = sum(
            1 for job in _cad_jobs._JOBS.values()
            if job.get("owner") == "ecad" and job.get("status") == "running"
        )
        if running >= _ECAD_JOB_MAX_RUNNING:
            return "error", RuntimeError(
                f"ECAD background job limit reached ({_ECAD_JOB_MAX_RUNNING}); "
                "poll the outstanding get_job results before starting more work")
        jid = "job-" + _uuid_ecad.uuid4().hex[:12]
        while jid in _cad_jobs._JOBS:
            jid = "job-" + _uuid_ecad.uuid4().hex[:12]
        _cad_jobs._JOBS[jid] = {
            "status": "running",
            "kind": "ecad-heavy",
            "owner": "ecad",
            "result": None,
            "error": None,
            "progress": {},
            "started_at": _time_ecad.time(),
            "done_at": None,
            "cancel_supported": False,
            "budget_s": wait_s,
        }

    def _worker() -> None:
        try:
            box["v"] = fn()
            update = {"status": "done", "result": box["v"], "done_at": _time_ecad.time()}
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc
            update = {"status": "error", "error": str(exc), "done_at": _time_ecad.time()}
        with _cad_jobs._JOB_LOCK:
            job = _cad_jobs._JOBS.get(jid)
            if job is not None:
                job.update(update)
                _trim_ecad_jobs_locked()
        finished.set()

    try:
        from services.worker_activity import start_background_thread

        start_background_thread(_worker, name=f"ecad-heavy-{jid[-6:]}")
    except Exception as exc:  # noqa: BLE001
        with _cad_jobs._JOB_LOCK:
            _cad_jobs._JOBS.pop(jid, None)
        return "error", exc

    if not finished.wait(wait_s):
        _ECAD_BUDGET_LOCAL.pending = (
            caller_key, jid, _time_ecad.monotonic() + 5.0)
        return "timeout", None

    with _cad_jobs._JOB_LOCK:
        _cad_jobs._JOBS.pop(jid, None)
    if "e" in box:
        return "error", box["e"]
    return "done", box.get("v")


def _provider_reachable(provider, timeout_s: float = 0.6) -> bool:
    """Fast TCP pre-flight: is the provider's endpoint actually accepting connections?

    The common ecad_design failure mode is a provider configured against a LOCAL model server
    (LM Studio / llama.cpp / Ollama, e.g. http://localhost:1234/v1) that is NOT running. The
    OpenAI SDK then spends ~14 s on connect-retries/backoff before raising — which overruns the
    MCP transport deadline (-32001) even with the plan-budget guard. A sub-second socket connect
    lets us detect a dead local endpoint and go straight to the instant recipe path.

    Returns True when we cannot tell where the endpoint is (no base_url -> assume a real remote
    API like api.openai.com): never penalise a correctly-configured cloud provider.
    """
    base_url = getattr(provider, "base_url", None)
    if not base_url:
        return True  # remote default (no local host:port to probe) — assume reachable
    try:
        import socket
        from urllib.parse import urlparse
        u = urlparse(base_url)
        host = u.hostname
        if host is None:
            return True
        port = u.port or (443 if u.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:  # noqa: BLE001 — refused / unresolved / timed out => treat as down
        return False


def _budget_timeout_err(what: str, *, simplify: str,
                        next_actions: list[str] | None = None, **extra) -> str:
    """Return the retrievable job envelope for the immediately preceding budget handoff."""
    pending = getattr(_ECAD_BUDGET_LOCAL, "pending", None)
    _ECAD_BUDGET_LOCAL.pending = None
    caller = _sys_ecad._getframe(1)
    caller_key = (id(caller), caller.f_code)
    if (not pending or pending[0] != caller_key
            or _time_ecad.monotonic() > pending[2]):
        return _err(
            f"{what} budget handoff had no retrievable job context.",
            hint="The operation was not reported as running; inspect the active document state.",
            next_actions=["ecad_status()"],
            status="error", auto_backgrounded=False, **extra)

    jid = pending[1]
    with _cad_jobs._JOB_LOCK:
        job = _cad_jobs._JOBS.get(jid)
        if job is None:
            return _err(
                f"{what} background job {jid!r} is no longer available.",
                hint="Inspect the active document state before deciding whether to run it again.",
                next_actions=["ecad_status()"],
                status="error", auto_backgrounded=False, job_id=jid, **extra)
        job["kind"] = what
        budget = float(job.get("budget_s") or _ECAD_HEAVY_BUDGET_S)

    # The worker can cross the finish line in the few instructions between the wait expiring
    # and the tool formatting its response.  In that race, return the normal result/error rather
    # than publishing a stale running state.
    state = job.get("status")
    if state == "done":
        result = job.get("result")
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            return _dumps(result)
        return _dumps({"ok": True, "job_id": jid, "status": "done", "result": result})
    if state == "error":
        return _err(
            f"{what} failed in background: {job.get('error') or 'unknown error'}",
            hint="Inspect the inputs and active document state before trying a corrected call.",
            next_actions=["ecad_status()"],
            status="error", auto_backgrounded=False, job_id=jid, **extra)

    cancellation = {
        "supported": False,
        "requested": False,
        "reason": ("This operation runs in a Python thread, which cannot be stopped safely. "
                   "It will finish in the background and retain its result or error."),
    }
    return _err(
        f"{what} did not finish within the {budget:.0f}s budget — job {jid} is still running.",
        hint=("Do not retry this mutating operation while its job is running. "
              f"Poll get_job('{jid}') for the eventual result. " + simplify),
        next_actions=[f"get_job('{jid}')"],
        status="running", auto_backgrounded=True, budget_s=budget,
        job_id=jid, retry_safe_while_running=False, cancel_supported=False,
        cancellation=cancellation, **extra)


# --------------------------------------------------------------------------- small helpers


def _dumps(payload: dict) -> str:
    """Compact JSON by default (CHUDWARE_MCP_PRETTY=1 for indent=2)."""
    return _rs.dumps(payload)


def _err(message: str, *, hint: str = "", next_actions: list[str] | None = None,
         **extra) -> str:
    """A structured error payload that still TEACHES — every error carries a hint + next steps."""
    return _rs.dumps(_rs.err_envelope(message, hint=hint, next_actions=next_actions, **extra))


def _active_sch_id(doc_id: str | None) -> str | None:
    return doc_id or _ECAD["current_sch"]


def _active_pcb_id(doc_id: str | None) -> str | None:
    return doc_id or _ECAD["current_pcb"]


def _get_sch(doc_id: str | None):
    """Resolve the active schematic session, or raise OpError with a friendly message."""
    did = _active_sch_id(doc_id)
    if did is None:
        raise OpError("no_doc", "no active schematic — call ecad_new_schematic(name) first",
                      {})
    s = ecad_session.get_session(did)
    if s.kind != "sch":
        raise OpError("wrong_kind", f"doc '{did}' is a {s.kind}, not a schematic", {})
    return s


def _get_pcb(doc_id: str | None):
    did = _active_pcb_id(doc_id)
    if did is None:
        raise OpError("no_doc", "no active board — call ecad_new_pcb(name) first", {})
    s = ecad_session.get_session(did)
    if s.kind != "pcb":
        raise OpError("wrong_kind", f"doc '{did}' is a {s.kind}, not a board", {})
    return s


def _sym_by_ref(doc: dict, ref: str) -> dict | None:
    """Find a placed symbol by its ref-des (e.g. 'R1'). Refs are how the model thinks; ids are
    internal. Returns the symbol instance dict or None."""
    for s in doc.get("symbols", []):
        if s.get("ref") == ref:
            return s
    return None


def _apply_sim_source_params(doc: dict, circuit: dict, warns: list,
                             *, apply_waveforms: bool = False) -> None:
    """Attach per-symbol ``sim_params`` source waveforms the structured netlist path drops
    (engine/ecad/netlist.py surfaces only ac_mag/ac_phase/eng — the netlist contract has
    no waveform field, so this is applied at the MCP seam by mutating device ``params``,
    the shape both solvers read).

    Only when ``apply_waveforms`` (tran/four analyses; an op/dc/ac run keeps the DC
    ``value`` as its level, matching SPICE .op semantics). A V/I symbol's ``sim_params``
    may carry either the canonical sim.md shape
    ``{"waveform": {"type": "SIN", "vo":..,"va":..,"f":..}}`` or the flat GUI shape
    ``{"waveform": "SIN", "amplitude": "5", "freq": "1000", "offset": "0"}``.
    Without this a sine-driven rectifier transient silently ran pure DC.
    """
    if not apply_waveforms:
        return
    by_ref = {d.get("name"): d for d in circuit.get("devices", [])}
    for sym in doc.get("symbols", []):
        dev = by_ref.get(sym.get("ref"))
        if dev is None:
            continue
        sp = sym.get("sim_params") or {}
        if not (dev.get("kind") in ("V", "I") and sp.get("waveform")):
            continue
        wf_in = sp["waveform"]
        wf: dict | None = None
        if isinstance(wf_in, dict) and wf_in.get("type"):
            wf = dict(wf_in)  # canonical sim.md §1 shape — pass through
        else:
            kind = str(wf_in).upper()

            def _f(key: str, default: float = 0.0) -> float:
                try:
                    return float(sp.get(key, default))
                except (TypeError, ValueError):
                    return default

            if kind == "SIN":
                wf = {"type": "SIN", "vo": _f("offset"), "va": _f("amplitude", 1.0),
                      "f": _f("freq", 1e3), "td": _f("td"), "theta": _f("theta")}
            elif kind == "PULSE":
                wf = {"type": "PULSE", "v1": _f("v1"), "v2": _f("v2", 1.0),
                      "td": _f("td"), "tr": _f("tr", 1e-9), "tf": _f("tf", 1e-9),
                      "pw": _f("pw", 1e-3), "per": _f("per", 2e-3)}
            elif kind == "PWL" and isinstance(sp.get("points"), list):
                wf = {"type": "PWL", "points": sp["points"]}
        if wf:
            dev.setdefault("params", {})["waveform"] = wf
        else:
            warns.append(f"{sym.get('ref')}: unrecognized sim_params waveform "
                         f"{wf_in!r} — simulated as DC")


def _resolve_ref_or_id(doc: dict, token: str) -> dict | None:
    """Accept either a ref-des ('R1') or an internal symbol id ('s1')."""
    sym = _sym_by_ref(doc, token)
    if sym is not None:
        return sym
    for s in doc.get("symbols", []):
        if s.get("id") == token:
            return s
    return None


def _placed_pins(doc: dict, sym: dict) -> list[dict]:
    """The pin list of a placed symbol with world coords + the net each pin currently sits on.

    This is the single most important context for a weak model: "here is what you can wire, by
    name/number, and what it's already connected to."
    """
    sdef = schema.symbol_def(doc, sym.get("libid")) or {}
    unit = sym.get("unit", 1)
    nl = netlist_mod.build_netlist(doc)
    by_pin = nl.get("by_pin", {})
    out: list[dict] = []
    for pin in sdef.get("pins", []):
        if pin.get("unit", 1) not in (unit, 0):
            continue
        world = schema.pin_world(sym, pin)
        pinref = f"{sym.get('id', '')}.{pin.get('number', '')}"
        net = by_pin.get(pinref, "")
        out.append({
            "number": str(pin.get("number", "")),
            "name": str(pin.get("name", "")),
            "type": str(pin.get("type", "passive")),
            "xy": [round(world[0], 4), round(world[1], 4)],
            "net": net or None,
            "connected": bool(net) and _net_size(nl, net) > 1,
        })
    return out


def _net_size(nl: dict, net: str) -> int:
    return len(nl.get("nets", {}).get(net, []))


def _unconnected_pins(doc: dict, nl: dict | None = None) -> list[dict]:
    """Every pin that is floating (no net) or alone on its net (single-pin net = a wiring slip).

    Returns ``[{"pinref","ref","pin","name","type","xy","net"}]`` sorted by ref then pin — this is
    what feeds the "3 pins unconnected: U1.4, R2.1 — wire them or mark no-connect" next-action.
    """
    nl = nl or netlist_mod.build_netlist(doc)
    by_pin = nl.get("by_pin", {})
    sym_by_id = {s.get("id"): s for s in doc.get("symbols", [])}
    out: list[dict] = []
    for sym, pin, world in schema.iter_placed_pins(doc):
        pinref = f"{sym.get('id', '')}.{pin.get('number', '')}"
        net = by_pin.get(pinref, "")
        size = _net_size(nl, net) if net else 0
        ptype = str(pin.get("type", "passive"))
        if ptype == "no_connect":
            continue
        if net == "" or size <= 1:
            s = sym_by_id.get(sym.get("id"), sym)
            out.append({
                "pinref": pinref,
                "ref": s.get("ref", ""),
                "pin": str(pin.get("number", "")),
                "name": str(pin.get("name", "")),
                "type": ptype,
                "xy": [round(world[0], 4), round(world[1], 4)],
                "net": net or None,
            })
    out.sort(key=lambda d: (d["ref"] or "~", d["pin"]))
    return out


def _pretty_pin(p: dict) -> str:
    """'R1.2(passive)' style label for a pin record from _unconnected_pins / _placed_pins."""
    ref = p.get("ref") or "?"
    num = p.get("pin") or p.get("number") or "?"
    return f"{ref}.{num}"


def _apply(session, ops: list[dict]) -> dict:
    """Apply an op batch at the session's current revision and return the ops-response."""
    return ecad_session.apply_ops(session, session.revision, ops)



def _attach_schematic_ascii_readback(payload: dict, doc_id: str = "",
                                    focus: Optional[list] = None) -> dict:
    """Best-effort compact schematic ASCII + grid/index on mutation responses."""
    if not isinstance(payload, dict) or not payload.get("ok", True):
        return payload
    try:
        from services.mcp.ascii2d import (  # noqa: PLC0415
            attach_schematic_readback, _load_schematic,
        )
        did = doc_id or payload.get("doc_id") or ""
        sch = _load_schematic(str(did) if did else "")
        kwargs: dict = {}
        if focus is not None:
            kwargs["focus"] = focus
        attach_schematic_readback(payload, sch, **kwargs)
    except Exception:  # noqa: BLE001
        payload.setdefault(
            "ascii_readback",
            {"ok": False, "error": "schematic readback unavailable"},
        )
    return payload


def _attach_board_ascii_readback(payload: dict, doc_id: str = "",
                                 focus: Optional[list] = None,
                                 layer: str = "") -> dict:
    """Best-effort compact ASCII + NL net_delta on a board mutation response.

    Never raises — mutation success must not depend on the readback path.
    """
    if not isinstance(payload, dict) or not payload.get("ok", True):
        return payload
    try:
        from services.mcp.ascii2d import (  # noqa: PLC0415
            attach_board_readback, _load_board,
        )
        did = doc_id or payload.get("doc_id") or payload.get("board_doc_id") or ""
        board = _load_board(str(did) if did else "")
        kwargs: dict = {}
        if layer:
            kwargs["layer"] = layer
        if focus is not None:
            kwargs["focus"] = focus
        attach_board_readback(payload, board, **kwargs)
    except Exception:  # noqa: BLE001
        payload.setdefault(
            "ascii_readback",
            {"ok": False, "error": "board readback unavailable"},
        )
    return payload


# --------------------------------------------------------------------------- ERC formatting


def _erc_summary(violations: list[dict]) -> dict:
    """Group ERC violations by rule with offending refs/pins, and split error/warning counts."""
    errors = [v for v in violations if v.get("severity") == "error"]
    warnings = [v for v in violations if v.get("severity") != "error"]
    by_rule: dict[str, dict] = {}
    for v in violations:
        rule = v.get("rule", "?")
        slot = by_rule.setdefault(rule, {"rule": rule, "severity": v.get("severity"),
                                         "count": 0, "pins": [], "nets": [], "messages": []})
        slot["count"] += 1
        for pr in v.get("pins", []) or []:
            if pr not in slot["pins"]:
                slot["pins"].append(pr)
        n = v.get("net")
        if n and n not in slot["nets"]:
            slot["nets"].append(n)
        if len(slot["messages"]) < 3:
            slot["messages"].append(v.get("message", ""))
        # if any instance is an error, the rule group is an error
        if v.get("severity") == "error":
            slot["severity"] = "error"
    groups = sorted(by_rule.values(), key=lambda g: (g["severity"] != "error", g["rule"]))
    return {"errors": len(errors), "warnings": len(warnings), "groups": groups}


def _erc_lines(violations: list[dict]) -> list[str]:
    """One readable line per violation, errors first."""
    ordered = sorted(violations, key=lambda v: (v.get("severity") != "error", v.get("rule", "")))
    lines = []
    for v in ordered:
        sev = (v.get("severity") or "warning").upper()
        lines.append(f"[{sev}] {v.get('rule')}: {v.get('message')}")
    return lines


# --------------------------------------------------------------------------- sim helpers (module level)


def _build_analysis(analysis: str, params: dict, circuit: dict) -> dict | None:
    """Map a friendly analysis name to a validated sim.md §3 analysis dict."""
    if not isinstance(params, dict):
        raise OpError("invalid_op", "analysis params must be an object", {})

    def bad(message: str):
        raise OpError("invalid_op", f"invalid {a} analysis: {message}", {})

    def finite(name: str, default: float) -> float:
        raw = params.get(name, default)
        if isinstance(raw, bool):
            bad(f"{name} must be numeric, not boolean")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            bad(f"{name} must be numeric")
        if not math.isfinite(value):
            bad(f"{name} must be finite")
        return value

    def integer(name: str, default: int, minimum: int = 1) -> int:
        raw = params.get(name, default)
        if isinstance(raw, bool):
            bad(f"{name} must be an integer >= {minimum}")
        try:
            numeric = float(raw)
        except (TypeError, ValueError):
            bad(f"{name} must be an integer >= {minimum}")
        if not math.isfinite(numeric) or not numeric.is_integer() or numeric < minimum:
            bad(f"{name} must be an integer >= {minimum}")
        return int(numeric)

    def frequency_spec(default_start: float, default_stop: float,
                       default_points: int) -> tuple[str, int, float, float]:
        sweep = str(params.get("sweep", "dec")).lower()
        if sweep not in ("lin", "dec", "oct"):
            bad("sweep must be one of 'lin', 'dec', or 'oct'")
        points = integer("points", default_points)
        fstart = finite("fstart", default_start)
        fstop = finite("fstop", default_stop)
        if not (0.0 < fstart < fstop):
            bad("frequencies must satisfy 0 < fstart < fstop")
        return sweep, points, fstart, fstop

    if not isinstance(analysis, str):
        raise OpError("invalid_op", "analysis must be a string", {})
    a = (analysis or "op").lower()
    if a == "op":
        return {"type": "op"}
    if a == "dc":
        src = params.get("source")
        if not src:
            src = next((d["name"] for d in circuit.get("devices", [])
                        if d.get("kind") == "V"), None)
        if not isinstance(src, str) or not src.strip():
            bad("source must name an independent voltage source")
        if not any(device.get("kind") == "V" and device.get("name") == src.strip()
                   for device in circuit.get("devices", [])):
            bad(f"source {src!r} is not an independent voltage source in the circuit")
        start = finite("start", 0.0)
        stop = finite("stop", 5.0)
        if start == stop:
            bad("start and stop must differ")
        default_step = (stop - start) / 50.0
        step = finite("step", default_step)
        if step == 0.0 or (stop - start) * step <= 0.0:
            bad("step must be non-zero and point from start toward stop")
        return {"type": "dc", "source": src.strip(), "start": start,
                "stop": stop, "step": step}
    if a == "tran":
        tstop = finite("tstop", 1e-3)
        tstart = finite("tstart", 0.0)
        tstep = finite("tstep", (tstop - tstart) / 200.0)
        if tstart < 0.0 or tstop <= tstart:
            bad("times must satisfy 0 <= tstart < tstop")
        if tstep <= 0.0 or tstep > tstop - tstart:
            bad("tstep must be > 0 and no larger than tstop-tstart")
        uic = params.get("uic", False)
        if not isinstance(uic, bool):
            bad("uic must be boolean")
        return {"type": "tran", "tstep": tstep, "tstop": tstop,
                "tstart": tstart, "uic": uic}
    if a == "ac":
        sweep, points, fstart, fstop = frequency_spec(1.0, 1e6, 20)
        return {"type": "ac", "sweep": sweep, "points": points,
                "fstart": fstart, "fstop": fstop}
    if a == "four":
        tstop = finite("tstop", 1e-3)
        f0 = finite("f0", 1e3)
        tstep = finite("tstep", tstop / 400.0)
        nharm = integer("nharm", 9)
        if f0 <= 0.0 or tstop <= 0.0 or tstep <= 0.0 or tstep > tstop:
            bad("f0, tstop, and tstep must be positive, with tstep <= tstop")
        if tstop * f0 < 1.0:
            bad("tstop must contain at least one fundamental period (tstop*f0 >= 1)")
        an = {"type": "four", "f0": f0, "tstop": tstop, "tstep": tstep,
              "probe": params.get("probe"), "nharm": nharm}
        if "periods" in params:
            an["periods"] = integer("periods", 1)
        else:
            whole = int(round(tstop * f0))
            if whole >= 2:
                an["periods"] = whole - 1
        return an
    if a in ("mc", "temp"):
        inner_name = str(params.get("inner", "op")).lower()
        if inner_name not in ("op", "dc", "tran", "ac"):
            bad("inner must be one of 'op', 'dc', 'tran', or 'ac'")
        inner_params = params.get("inner_params", {})
        if not isinstance(inner_params, dict):
            bad("inner_params must be an object")
        inner_an = _build_analysis(inner_name, inner_params, circuit)
        if a == "mc":
            tol = params.get("tol")
            if tol is not None:
                if not isinstance(tol, dict):
                    bad("tol must be an object of device-kind to fractional tolerance")
                for key, value in tol.items():
                    if isinstance(value, bool):
                        bad(f"tol[{key!r}] must be numeric, not boolean")
                    try:
                        tolerance = float(value)
                    except (TypeError, ValueError):
                        bad(f"tol[{key!r}] must be numeric")
                    if not math.isfinite(tolerance) or tolerance < 0.0:
                        bad(f"tol[{key!r}] must be finite and >= 0")
            result = {"type": "mc", "runs": integer("runs", 50),
                      "analysis": inner_an, "tol": tol,
                      "seed": integer("seed", 42, minimum=0)}
            if params.get("probe") is not None:
                result["probe"] = params["probe"]
            return result
        vals = params.get("values", [-40.0, 27.0, 85.0])
        if not isinstance(vals, list) or len(vals) < 2:
            bad("values must contain at least two temperatures in degrees C")
        temperatures = []
        for index, raw in enumerate(vals):
            if isinstance(raw, bool):
                bad(f"values[{index}] must be numeric, not boolean")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                bad(f"values[{index}] must be numeric")
            if not math.isfinite(value) or value < -273.15:
                bad(f"values[{index}] must be finite and >= -273.15 C")
            temperatures.append(value)
        if len(set(temperatures)) != len(temperatures):
            bad("temperature sweep values must be distinct")
        result = {"type": "temp", "values": temperatures, "analysis": inner_an}
        if params.get("probe") is not None:
            result["probe"] = params["probe"]
        return result
    if a == "noise":
        sweep, points, fstart, fstop = frequency_spec(10.0, 1e6, 20)
        output = params.get("output", "V(OUT)")
        if not isinstance(output, str) or not output.strip():
            bad("output must be a non-empty voltage probe")
        source = params.get("source")
        if source is not None:
            if not isinstance(source, str) or not source.strip():
                bad("source must name an independent voltage source")
            if not any(device.get("kind") == "V" and device.get("name") == source.strip()
                       for device in circuit.get("devices", [])):
                bad(f"source {source!r} is not an independent voltage source in the circuit")
            source = source.strip()
        return {"type": "noise", "output": output.strip(), "source": source,
                "fstart": fstart, "fstop": fstop, "points": points, "sweep": sweep}
    return None


def _simulate_advanced(session, circuit: dict, an: dict, probe_list: list[str],
                       warns: list[str]) -> str:
    """Run one of the advanced analyses (four/mc/temp/noise) and return a rich payload."""
    sim_run = _sim_run  # C++ sim_spice RPC (advanced analyses always run on the MNA solver)

    atype = an["type"]

    if atype == "four":
        # Run a tran then Fourier on the result.
        tran_an = {"type": "tran",
                   "tstep": an.get("tstep", 5e-6),
                   "tstop": an.get("tstop", 1e-3),
                   "tstart": 0.0, "uic": False}
        tran_probes = list(probe_list)
        probe = an.get("probe")
        if probe and probe not in tran_probes:
            tran_probes = [probe] + tran_probes
        study = {"analyses": [an], "probes": tran_probes}
        result = sim_run(circuit, study)
        four = result.get("four", {})
        if not four:
            return _err("Fourier analysis produced no result -- tran may not have run or the probe "
                        "is missing",
                        hint="Supply 'f0' (fundamental Hz), 'tstop' (seconds), and 'probe' "
                             "(e.g. 'V(OUT)') in the params dict.",
                        warnings=warns,
                        next_actions=["ecad_simulate('four', params={'f0':1e3,'tstop':5e-3,"
                                      "'probe':'V(OUT)'})"])
        harmonics = four.get("harmonics", [])
        thd = float(four.get("thd_pct", float("nan")))
        h1 = harmonics[0][1] if harmonics else float("nan")
        conv = (bool(result.get("convergence", {}).get("four", False))
                and bool(harmonics) and _finite(thd) and _finite(h1))
        probe_used = four.get("probe", "?")
        dom = [f"H{n}={m:.4g} V" for n, m, _ in harmonics[:4]]
        reading = (f"{probe_used} @ f0={four.get('f0', '?'):.4g} Hz: "
                   f"H1={h1:.4g} V, THD={thd:.3f}%, "
                   f"dominant harmonics: {', '.join(dom)}; "
                   f"DC bias={four.get('dc', 0.0):.4g} V")
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "four",
            "converged": conv,
            **({} if conv else {"error": "four analysis did not converge or returned no finite harmonics"}),
            "device_count": len(circuit["devices"]),
            "four": four,
            "thd_pct": _round(thd), "h1_mag": _round(h1),
            "harmonics": [[int(n), _round(m), _round(p)] for n, m, p in harmonics],
            "reading": reading,
            "warnings": warns + ([] if conv else ["four/tran did not converge"]),
            "hint": (f"THD={thd:.3f}% (H1={h1:.4g} V). "
                     + ("Low THD < 1% -- good linearity." if thd < 1.0 else
                        "High THD -- significant harmonic content; check for clipping/nonlinearity.")),
            "next_actions": ["ecad_simulate('op')  # check DC bias point",
                             "ecad_simulate('ac')  # see frequency response"],
            "sim_log": result.get("log", [])[-6:],
        })

    if atype == "mc":
        study = {"analyses": [an], "probes": probe_list}
        result = sim_run(circuit, study)
        mc = result.get("mc", {})
        conv = bool(result.get("convergence", {}).get("mc", False))
        if not mc or not mc.get("runs"):
            return _err("Monte-Carlo produced no results -- inner analysis may have failed",
                        hint="Specify 'probe' (e.g. 'V(OUT)'), 'runs' (e.g. 50), and "
                             "'inner' ('op'/'tran') in params.",
                        warnings=warns,
                        next_actions=["ecad_simulate('mc', params={'probe':'V(OUT)','runs':50})"])
        mean = float(mc.get("mean", float("nan")))
        std = float(mc.get("std", float("nan")))
        mn = float(mc.get("min", float("nan")))
        mx = float(mc.get("max", float("nan")))
        probe_used = mc.get("probe", "?")
        tol = mc.get("tol", {})
        tol_str = ", ".join(f"{k}={v*100:.0f}%" for k, v in (tol.items() if isinstance(tol, dict) else []))
        reading = (f"Monte-Carlo {mc.get('runs')} run(s) on {probe_used}: "
                   f"mean={mean:.4g} V, sigma={std:.4g} V, "
                   f"range=[{mn:.4g}, {mx:.4g}] V "
                   f"(tolerances: {tol_str or 'defaults'})")
        values = mc.get("values", [])
        requested_runs = int(mc.get("runs", 0) or 0)
        yield_pct = (100.0 * sum(1 for v in values if _finite(v))
                     / max(requested_runs, 1))
        conv = (conv and requested_runs > 0 and len(values) == requested_runs
                and all(_finite(v) for v in (mean, std, mn, mx))
                and all(_finite(v) for v in values))
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "mc",
            "converged": conv,
            **({} if conv else {"error": "Monte-Carlo did not converge at every requested run"}),
            "device_count": len(circuit["devices"]),
            "mc": {k: (round(v, 6) if isinstance(v, float) else v)
                   for k, v in mc.items() if k != "values"},
            "mean": _round(mean), "std": _round(std), "min": _round(mn), "max": _round(mx),
            "yield_pct": round(yield_pct, 1),
            "reading": reading,
            "warnings": warns,
            "hint": (f"MC mean={mean:.4g} V, 3-sigma range [{mean-3*std:.4g}, {mean+3*std:.4g}] V. "
                     + (f"Yield (finite) = {yield_pct:.1f}%. "
                        "Tight distribution -- good tolerance stack-up." if yield_pct > 95.0 else
                        f"Yield = {yield_pct:.1f}% -- some runs may fail; tighten tolerances or "
                        "redesign for wider margin.")),
            "next_actions": ["ecad_simulate('op')  # nominal operating point",
                             "ecad_sweep('R1', [...])  # deterministic sensitivity"],
            "sim_log": result.get("log", [])[-6:],
        })

    if atype == "temp":
        # Honor the caller's requested probe (Bug E8): build the simulated probe set so the
        # requested probe is included, and report the trend for THAT probe (not just probe_list[0]).
        req_probe = an.get("probe")
        sim_probes = list(probe_list)
        if req_probe and req_probe not in sim_probes:
            sim_probes = [req_probe] + sim_probes
        study = {"analyses": [an], "probes": sim_probes}
        result = sim_run(circuit, study)
        temp_res = result.get("temp", {})
        conv = bool(result.get("convergence", {}).get("temp", False))
        points_data = temp_res.get("points", [])
        temps = temp_res.get("values", [])
        conv = conv and bool(temps) and len(points_data) == len(temps)
        # Summarise the requested probe across temperatures (else the first probe).
        probe_used = req_probe or (probe_list[0] if probe_list else None)
        trend_vals: list[tuple[float, float]] = []
        for pt in points_data:
            op = pt.get("op", {})
            if probe_used and op and probe_used in op:
                v = op[probe_used]
                if _finite(v):
                    trend_vals.append((pt["temp"], float(v)))
        if probe_used:
            conv = conv and len(trend_vals) == len(temps)
        if trend_vals and probe_used:
            first_t, first_v = trend_vals[0]
            last_t, last_v = trend_vals[-1]
            delta = last_v - first_v
            verb = "rises" if delta > 0 else ("falls" if delta < 0 else "holds")
            reading = (f"{probe_used} over [{temps[0]:.0f}, {temps[-1]:.0f}] degC: "
                       f"{verb} {first_v:.4g} -> {last_v:.4g} V "
                       f"(delta={delta:.4g} V, "
                       f"TC={(delta/(last_t-first_t)*1e3):.3g} mV/degC"
                       f" at {first_v:.4g} V nominal)")
        else:
            reading = f"Temperature sweep over {temps} degC -- see 'temp_points' for raw values."
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "temp",
            "converged": conv,
            **({} if conv else {"error": "temperature sweep did not return every requested finite point"}),
            "device_count": len(circuit["devices"]),
            "temps": temps,
            "trend": [{"temp": t, probe_used or "probe": round(v, 6)}
                      for t, v in trend_vals],
            "temp_points": [{k: (round(v2, 6) if isinstance(v2, float) else v2)
                             for k, v2 in pt.items() if k != "tran"}
                            for pt in points_data],
            "reading": reading,
            "warnings": warns,
            "hint": reading,
            "next_actions": ["ecad_simulate('op')  # nominal operating point",
                             "ecad_simulate('mc')  # combine with tolerance analysis"],
            "sim_log": result.get("log", [])[-6:],
        })

    if atype == "noise":
        study = {"analyses": [an], "probes": probe_list}
        result = sim_run(circuit, study)
        noise = result.get("noise", {})
        conv = bool(result.get("convergence", {}).get("noise", False))
        if not noise:
            return _err("noise analysis produced no result",
                        hint="Specify 'output' (e.g. 'V(OUT)'), 'fstart', 'fstop' in params. "
                             "The circuit must have at least one resistor.",
                        warnings=warns,
                        next_actions=["ecad_simulate('noise', "
                                      "params={'output':'V(OUT)','fstart':10,'fstop':1e6})"])
        total_rms = float(noise.get("total_noise_rms", 0.0))
        per_r = noise.get("per_resistor", {})
        freqs = noise.get("x_f", [])
        spectrum = noise.get("onoise_spectrum", [])
        output = noise.get("output", "?")
        # Peak noise density.
        peak_density = max((v for v in spectrum if _finite(v)), default=0.0)
        conv = (conv and _finite(total_rms) and total_rms >= 0.0
                and bool(freqs) and all(_finite(f) and float(f) > 0.0 for f in freqs))
        # Which resistor contributes most?
        dominant_r = max(per_r, key=lambda k: per_r[k]) if per_r else None
        reading = (f"Output noise at {output}: "
                   f"total RMS={_eng(total_rms)}Vrms "
                   f"over {freqs[0] if freqs else '?'}..{freqs[-1] if freqs else '?'} Hz; "
                   f"peak spectral density={_eng(peak_density)}V/sqrt(Hz)"
                   + (f"; dominant source: {dominant_r} ({_eng(per_r[dominant_r])}Vrms)"
                      if dominant_r else ""))
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "noise",
            "converged": conv,
            **({} if conv else {"error": "noise analysis did not converge or returned invalid data"}),
            "device_count": len(circuit["devices"]),
            "output": output,
            # Significant-figure rounding so micro/nano-scale noise figures don't collapse to 0.0
            # (Bug E9: _round to 4 decimals zeroed a 9.1 uVrms total).
            "total_noise_rms": _round_sig(total_rms),
            "peak_density_v_sqrthz": _round_sig(peak_density),
            "per_resistor_rms": {k: _round_sig(v) for k, v in per_r.items()},
            "dominant_source": dominant_r,
            "noise": {k: v for k, v in noise.items() if k not in ("onoise_spectrum", "onoise_psd")},
            "reading": reading,
            "warnings": warns,
            "hint": (reading + ". "
                     + (f"To reduce noise: lower resistance of {dominant_r}, "
                        "narrow the bandwidth, or add a post-filter."
                        if dominant_r else
                        "No dominant resistor noise source identified.")),
            "next_actions": ["ecad_simulate('ac')  # check frequency response / bandwidth",
                             "ecad_simulate('op')  # verify DC operating point"],
            "sim_log": result.get("log", [])[-6:],
        })

    return _err(f"internal: _simulate_advanced called with unknown type {atype!r}",
                next_actions=["ecad_simulate('op')"])


def _finite(v: Any) -> bool:
    try:
        return math.isfinite(float(v))
    except Exception:  # noqa: BLE001
        return False


def _round(v: Any, n: int = 4) -> Any:
    try:
        f = float(v)
        if not math.isfinite(f):
            return None
        return round(f, n)
    except Exception:  # noqa: BLE001
        return v


def _round_sig(v: Any, sig: int = 4) -> Any:
    """Round to *sig* SIGNIFICANT figures (not decimal places) so very small magnitudes survive.

    ``_round`` truncates to 4 decimals, which silently zeroes micro/nano-scale values (e.g. a
    9.1 uVrms noise figure rounds to 0.0 — Bug E9). This keeps the leading significant digits for
    any magnitude. Returns ``None`` for non-finite, passes non-numerics through."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if not math.isfinite(f):
        return None
    if f == 0.0:
        return 0.0
    exp = math.floor(math.log10(abs(f)))
    return round(f, max(0, sig - 1 - exp))


def _eng(v: float) -> str:
    """Engineering-notation magnitude for a current/value, e.g. 3.1m, 1.5u, 12.0 ."""
    try:
        f = float(v)
    except Exception:  # noqa: BLE001
        return str(v)
    if f == 0 or not math.isfinite(f):
        return f"{f:g} "
    sign = "-" if f < 0 else ""
    a = abs(f)
    for suf, scale in (("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3),
                       ("u", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        q = a / scale
        if 1.0 <= q < 1000.0:
            return f"{sign}{q:.3g}{suf}"
    return f"{f:.3g} "


def _summarize_sweep(dc: dict, reading_lines: list[str], unit: str = "V") -> dict:
    """Compact a DC sweep: keep endpoints + min/max per trace."""
    out: dict = {"sweep": dc.get("sweep"), "points": len(dc.get("x", [])),
                 "x_range": [dc.get("x", [None])[0], dc.get("x", [None])[-1]]
                 if dc.get("x") else [], "traces": {}}
    for p, vals in dc.get("traces", {}).items():
        fin = [v for v in vals if _finite(v)]
        if not fin:
            continue
        out["traces"][p] = {"first": _round(vals[0]), "last": _round(vals[-1]),
                            "min": _round(min(fin)), "max": _round(max(fin))}
        # A current probe (I(...)) is amps, not volts — don't label it with the voltage unit.
        u = "A" if str(p).startswith(("I(", "i(")) else unit
        reading_lines.append(f"{p}: {_round(fin[0])}->{_round(fin[-1])} {u} over sweep")
    return out


def _summarize_tran(tran: dict, reading_lines: list[str]) -> dict:
    """Compact a transient run: final value + peak per trace, and the time span."""
    times = tran.get("x_t", [])
    out: dict = {"points": len(times),
                 "t_end": _round(times[-1], 9) if times else None, "traces": {}}
    for p, vals in tran.get("traces", {}).items():
        fin = [v for v in vals if _finite(v)]
        if not fin:
            continue
        final = vals[-1] if _finite(vals[-1]) else fin[-1]
        out["traces"][p] = {"final": _round(final), "min": _round(min(fin)),
                            "max": _round(max(fin))}
        reading_lines.append(f"{p} settles to {_round(final)} V "
                             f"(peak {_round(max(fin, key=abs))} V)")
    return out


def _summarize_ac(ac: dict, reading_lines: list[str]) -> dict:
    """Compact an AC sweep: peak gain + the -3 dB corner frequency per trace."""
    freqs = ac.get("x_f", [])
    out: dict = {"points": len(freqs),
                 "f_range": [_round(freqs[0], 3), _round(freqs[-1], 3)] if freqs else [],
                 "traces": {}}
    for p, mags in ac.get("mag", {}).items():
        fin = [(f, m) for f, m in zip(freqs, mags) if _finite(m)]
        if not fin:
            continue
        peak = max(m for _, m in fin)
        # -3 dB point: first frequency where |H| drops below peak/sqrt(2).
        thresh = peak / math.sqrt(2.0)
        corner = None
        for f, m in fin:
            if m < thresh:
                corner = f
                break
        out["traces"][p] = {"peak_mag": _round(peak),
                            "f_-3dB": _round(corner, 3) if corner else None}
        if corner:
            reading_lines.append(f"{p}: -3 dB corner ~{_round(corner, 3)} Hz "
                                 f"(peak |H|={_round(peak)})")
        else:
            reading_lines.append(f"{p}: peak |H|={_round(peak)} (no -3 dB rolloff in band)")
    return out


def _zone_polygon(doc: dict, rect_or_polygon: Optional[list]) -> list | None:
    """Resolve the ecad_add_zone shape arg into a polygon ``[[x, y], ...]`` (>=3 vertices).

    Accepts a rect ``[x0, y0, x1, y1]`` (4 scalars -> 4 corners), an explicit polygon
    ``[[x, y], ...]``, or ``None`` (flood the board outline's bounding box — the whole-board pour).
    Returns None if no usable polygon can be derived (e.g. None + no outline)."""
    if rect_or_polygon is None:
        bbox = _outline_bbox(doc)
        if bbox is None:
            return None
        x0, y0, x1, y1 = bbox
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    # explicit polygon: list of [x, y] points
    if (isinstance(rect_or_polygon, list) and len(rect_or_polygon) >= 3
            and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in rect_or_polygon)):
        return [[float(p[0]), float(p[1])] for p in rect_or_polygon]
    # rect: [x0, y0, x1, y1]
    if (isinstance(rect_or_polygon, list) and len(rect_or_polygon) == 4
            and all(isinstance(v, (int, float)) for v in rect_or_polygon)):
        x0, y0, x1, y1 = (float(v) for v in rect_or_polygon)
        lo_x, hi_x = sorted((x0, x1))
        lo_y, hi_y = sorted((y0, y1))
        return [[lo_x, lo_y], [hi_x, lo_y], [hi_x, hi_y], [lo_x, hi_y]]
    return None


def _outline_bbox(doc: dict) -> tuple[float, float, float, float] | None:
    """Bounding box (minx, miny, maxx, maxy) of the board outline shapes, or None if empty."""
    xs: list[float] = []
    ys: list[float] = []
    for sh in doc.get("outline", {}).get("shapes", []) or []:
        for key in ("p1", "p2", "center"):
            p = sh.get(key)
            if isinstance(p, (list, tuple)) and len(p) == 2:
                xs.append(float(p[0]))
                ys.append(float(p[1]))
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _parts_extents(doc: dict) -> tuple[float, float, float, float] | None:
    """Bounding box of placed footprints (origins + pad offsets), or None if empty."""
    xs: list[float] = []
    ys: list[float] = []
    for fp in doc.get("footprints", []) or []:
        pos = fp.get("pos")
        if not (isinstance(pos, (list, tuple)) and len(pos) == 2):
            continue
        fx, fy = float(pos[0]), float(pos[1])
        pads = fp.get("pads", []) or []
        added_pad = False
        for pad in pads:
            # pad offset relative to the footprint origin (field name varies: pos/at/offset)
            off = pad.get("pos") or pad.get("at") or pad.get("offset")
            if isinstance(off, (list, tuple)) and len(off) >= 2:
                xs.append(fx + float(off[0])); ys.append(fy + float(off[1]))
                added_pad = True
        if not added_pad:
            xs.append(fx); ys.append(fy)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _default_outline_shapes(doc: dict, margin: float = 5.0,
                            min_side: float = 20.0) -> list[dict]:
    """A closed rectangular Edge.Cuts outline enclosing the placed parts (+margin).

    Falls back to a min_side square at the origin when nothing is placed yet, so the
    one-shot board flow always produces a valid outline (Bug E-3 — ecad_to_3d needs
    an Edge.Cuts loop, and the GND pour wants the real board extents).
    """
    ext = _parts_extents(doc)
    if ext is None:
        x0, y0, x1, y1 = 0.0, 0.0, min_side, min_side
    else:
        x0, y0, x1, y1 = ext
        x0 -= margin; y0 -= margin; x1 += margin; y1 += margin
        if x1 - x0 < min_side:
            cx = (x0 + x1) / 2.0; x0, x1 = cx - min_side / 2.0, cx + min_side / 2.0
        if y1 - y0 < min_side:
            cy = (y0 + y1) / 2.0; y0, y1 = cy - min_side / 2.0, cy + min_side / 2.0
    corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    return [{"type": "line", "p1": corners[i], "p2": corners[(i + 1) % 4], "layer": "Edge.Cuts"}
            for i in range(4)]


def _summarize_sweep_steps(runs: list, atype: str, probe: Optional[str],
                           probe_order: Optional[list] = None):
    """Compact a `.step` sweep into (trend_sentence, sweep_table, chosen_probe).

    For each swept value we pull a single scalar for the chosen probe (the op node voltage, or the
    final transient value, or the peak AC magnitude) and build a plain-English 'rises/falls/holds'
    sentence so a weak model can read the trend without parsing the raw runs.

    ``probe_order`` (T-RF20): the study's probe list. The C++ core returns result dicts with
    alphabetically-ordered keys (nlohmann::json), which scrambled the old insertion-order-based
    "first finite probe" heuristic (it could pick the stiff source net over the probed output);
    the caller's probe order restores the intended priority."""
    # Pick the probe to report: the explicit one, else the first probe that has finite values.
    def _scalar(run: dict, p: str):
        if atype == "op":
            return run.get("op", {}).get(p)
        if atype == "tran":
            tr = run.get("tran", {}).get("traces", {}).get(p, [])
            return tr[-1] if tr else None
        if atype == "ac":
            mags = run.get("ac", {}).get("mag", {}).get(p, [])
            fin = [m for m in mags if _finite(m)]
            return max(fin) if fin else None
        if atype == "dc":
            tr = run.get("dc", {}).get("traces", {}).get(p, [])
            fin = [v for v in tr if _finite(v)]
            return fin[-1] if fin else None
        return None

    chosen = probe
    if chosen is None:
        # find the probe name present across runs with the most finite values
        names: list[str] = []
        for run in runs:
            for key in ("op",):
                names += list(run.get(key, {}).keys())
            for key in ("tran", "dc"):
                names += list(run.get(key, {}).get("traces", {}).keys())
            for key in ("ac",):
                names += list(run.get(key, {}).get("mag", {}).keys())
        # prefer a node voltage
        v_names = [n for n in names if str(n).startswith("V(")]
        ordered = v_names or names
        if probe_order:
            rank = {p: i for i, p in enumerate(probe_order)}
            ordered = sorted(ordered, key=lambda n: rank.get(n, len(rank)))
        for cand in ordered:
            if any(_finite(_scalar(run, cand)) for run in runs):
                chosen = cand
                break

    table: list[dict] = []
    series: list[tuple[float, float]] = []
    for run in runs:
        val = run.get("value")
        scal = _scalar(run, chosen) if chosen else None
        row = {"value": _round(val), chosen or "probe": _round(scal)}
        table.append(row)
        if _finite(val) and _finite(scal):
            series.append((float(val), float(scal)))

    if not series or chosen is None:
        return "", table, chosen
    first_v, first_y = series[0]
    last_v, last_y = series[-1]
    delta = last_y - first_y
    unit = "" if atype == "ac" else " V"
    if abs(delta) < 1e-9:
        verb = "holds at"
        trend = f"{chosen} {verb} {_round(first_y)}{unit} as {_round(first_v)} -> {_round(last_v)}"
    else:
        verb = "rises" if delta > 0 else "falls"
        trend = (f"{chosen} {verb} {_round(first_y)}{unit} -> {_round(last_y)}{unit} "
                 f"as the swept value goes {_round(first_v)} -> {_round(last_v)}")
    return trend, table, chosen


def _default_out_dir(name: str):
    from services import paths
    return paths.model_dir(f"ecad_{name}")
