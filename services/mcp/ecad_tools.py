"""AI-ECAD MCP tools — schematic capture, ERC, SPICE simulation, PCB DRC, fab/3D export.

THE DESIGN GOAL (project owner): "the AI is extremely limited; its tools must have plenty of
automatic features/helpers to give it the context it needs to ask the right questions and do the
right things." So every tool here does the BOOKKEEPING and returns RICH, ACTIONABLE context — the
current state, what's wrong, and an imperative ``next_actions`` list — exactly the way the CAD
agent loop's feedback functions (``agent/loop.py``: ``_feedback_ok`` / ``_repeat_nudge`` /
``NOOP_CHANGE_NOTE``) guide a weak local model. A tool that just returns ``{"ok": true}`` is a
failure: it must name the unconnected pins, summarize the nets, group the ERC violations, and tell
the model the single best next op.

Architecture mirrors ``mcp_server/server.py`` exactly: ``@mcp.tool()`` (FastMCP) functions that
return JSON strings and call the engine helpers DIRECTLY (engine.ecad / engine.sim), holding their
own session state in a module-level dict (``_ECAD``). Registration is deferred behind
:func:`register_ecad_tools` so ``server.py`` can wire us in with a tiny fenced block.

Tools (each returns ``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}``):
  ecad_new_schematic / ecad_new_pcb   start a doc
  ecad_find_part                      library.search -> ranked real libids/fpids + pin counts
  ecad_place_symbol                   place a symbol -> its ref + full pin list + unconnected count
  ecad_wire                           auto-route pin->pin -> resulting net + remaining unconnected
  ecad_add_power / ecad_label         power port / net label
  ecad_status                         THE context tool: components, nets, ERC summary, next_actions
  ecad_run_erc                        readable ERC violation lines + "N errors, M warnings"
  ecad_simulate                       netlist -> SPICE -> simulate; op/dc/tran/ac + four/mc/temp/noise
  ecad_run_drc                        PCB DRC summary with the worst offenders
  ecad_export / ecad_to_3d            wrap fab writers / 3D export (lazy import, clean if unavailable)
  ecad_check_impedance                Z0 + delay for a routed trace vs. target; plain-English reading
  ecad_tf                             DC .tf: gain/Rin/Rout with a plain-English reading
  ecad_make_board                     one-shot sync->route->fill->DRC; sets current_pcb
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional, Union

from engine.ecad import netlist as netlist_mod
from engine.ecad import schema
from engine.ecad import session as ecad_session
from engine.ecad.library import search as lib_search
from engine.ecad.ops import OpError

try:  # T-FIN8 engineering-metadata layer — advisory, must never break the tools
    from engine.ecad import metadata as _eng_metadata
except Exception:  # noqa: BLE001
    _eng_metadata = None  # type: ignore[assignment]


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
import threading as _threading
import uuid as _uuid_ecad
_PROVIDER_PLAN_LOCK = _threading.Lock()
_PROVIDER_PLAN_BUSY = {"v": False}


# --------------------------------------------------------------------------- crash recovery
#
# ECAD docs live only in memory (engine.ecad.session registry). A heavy ecad_* tool that overruns
# the MCP client's transport timeout gets the server KILLED, and without on-disk backing every open
# schematic/board is lost — unlike C++ part docs, which autosave + recover (cad_tools). The
# engine.ecad.autosave module persists each session to %APPDATA%/AI-CAD/ecad_autosave after every
# mutation; here we arm it and expose recovery to server._warm_core.

_ECAD_SESSION_ID = _uuid_ecad.uuid4().hex[:12]


def enable_ecad_autosave() -> None:
    """Arm ECAD crash-recovery autosave for this server process. Called once from
    server._warm_core at startup. Best-effort.

    NOTE (daemon mode): unlike the C++ part-doc autosave — which moved into
    aicad-hostd (core/hostd/autosave.cpp) and is DISABLED in cad_tools when
    AI_CAD_ENGINE_DAEMON=1 — ECAD docs are pure Python living in THIS process,
    invisible to the daemon, and engine.ecad.autosave is mutation-hooked (no
    threading.Timer chain). So it stays armed in both modes."""
    try:
        from engine.ecad import autosave as _eauto
        _eauto.enable(_ECAD_SESSION_ID)
    except Exception:  # noqa: BLE001
        pass


def recover_ecad_autosaved(scope: str = "recent"):
    """Reopen ECAD docs autosaved before a (possibly killed) prior session into the live registry,
    and re-point _ECAD['current_*'] at the recovered docs so default-doc tool calls work. Returns
    the recovery list. Best-effort — never raises into startup."""
    try:
        from engine.ecad import autosave as _eauto
        recovered = _eauto.recover_ecad_autosaved(scope=scope) or []
    except Exception:  # noqa: BLE001
        return []
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
        from engine.ecad import autosave as _eauto
        from engine.ecad import session as _esess
        if not _eauto.is_enabled():
            return
        for s in _esess.iter_sessions():
            _eauto.autosave_session(s)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- heavy-tool budget
#
# A SYNCHRONOUS ecad_* tool that overruns the client's transport timeout gets the server killed
# (-32001). The C++ side auto-backgrounds at _SYNC_SOLVE_BUDGET_S=45 (cad_tools); ecad_design
# already uses a worker-thread + wall-clock deadline (_PLAN_BUDGET_S). Match that for the other
# heavy ECAD tools (simulate / make_board / autoroute / fill / dfm): run the heavy work on a worker
# thread with a deadline. If it finishes in time the result is returned inline (the common
# small-board case is unchanged); if it overruns we return a clean "still running — simplify"
# message instead of blocking until the client gives up and kills us. The doc is autosaved first,
# so the abandoned (daemon) worker can keep running harmlessly.

try:
    _ECAD_HEAVY_BUDGET_S = float(os.environ.get("AI_CAD_ECAD_HEAVY_BUDGET_S", "") or 30.0)
except (TypeError, ValueError):
    _ECAD_HEAVY_BUDGET_S = 30.0
if not (_ECAD_HEAVY_BUDGET_S > 0.0):
    _ECAD_HEAVY_BUDGET_S = 30.0


def _run_with_budget(fn, budget_s: Optional[float] = None):
    """Run ``fn()`` on a daemon worker thread with a wall-clock deadline.

    Returns ``(status, value)``:
      * ``("done",  result)``    — fn returned within the budget
      * ``("error", exception)`` — fn raised within the budget
      * ``("timeout", None)``    — fn overran (still running in the background daemon)

    ECAD ops are bounded, so an overrun thread finishes on its own; we just stop WAITING on it.
    """
    if budget_s is None:
        budget_s = _ECAD_HEAVY_BUDGET_S
    box: dict = {}

    def _worker():
        try:
            box["v"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["e"] = exc

    th = _threading.Thread(target=_worker, name="ecad-heavy", daemon=True)
    th.start()
    th.join(max(1.0, float(budget_s)))
    if th.is_alive():
        return "timeout", None
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
    """Clean 'still running — simplify' payload returned when a heavy ECAD tool overruns the budget,
    instead of blocking until the client kills the server."""
    return _err(
        f"{what} did not finish within the {_ECAD_HEAVY_BUDGET_S:.0f}s budget — it was left running "
        "in the background to avoid a transport timeout that would kill the MCP server.",
        hint=("Open documents were autosaved and are safe (they recover on restart). " + simplify),
        next_actions=next_actions or ["ecad_status()"],
        status="running", auto_backgrounded=True, budget_s=_ECAD_HEAVY_BUDGET_S, **extra)


# --------------------------------------------------------------------------- small helpers


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(message: str, *, hint: str = "", next_actions: list[str] | None = None,
         **extra) -> str:
    """A structured error payload that still TEACHES — every error carries a hint + next steps."""
    out = {"ok": False, "error": message, "warnings": [],
           "hint": hint or message,
           "next_actions": next_actions or []}
    out.update(extra)
    return _dumps(out)


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


# --------------------------------------------------------------------------- the tools


def register_ecad_tools(mcp) -> None:
    """Register every AI-ECAD tool on the given FastMCP instance.

    Called from ``mcp_server/server.py`` after ``mcp`` is created. Kept as a function (not
    import-time side effects) so the tools can be driven against a stub ``mcp`` in tests without
    importing the whole stdio server.
    """

    # ---------------------------------------------------------------- new doc

    @mcp.tool()
    def ecad_new_schematic(name: str = "Schematic1") -> str:
        """Start a NEW schematic document and make it the active one.

        Returns the doc id and tells you the next move: find a real part with
        ecad_find_part, then place it with ecad_place_symbol. The doc starts empty (no
        components, no nets) — every later tool defaults to this active schematic, so you can
        omit doc_id.
        """
        s = ecad_session.new_session("sch", name)
        _ECAD["current_sch"] = s.doc_id
        _ECAD["current"] = s.doc_id
        return _dumps({
            "ok": True, "doc_id": s.doc_id, "kind": "sch", "name": name,
            "revision": s.revision, "components": 0, "nets": 0,
            "warnings": [],
            "hint": "Empty schematic. Find a real part before inventing one: "
                    "ecad_find_part('resistor') returns library libids you can place.",
            "next_actions": [
                "ecad_find_part('resistor')  # discover real libids (e.g. Device:R)",
                "ecad_place_symbol('Device:R', x=0, y=0, value='1k')  # place your first part",
            ],
        })

    @mcp.tool()
    def ecad_new_pcb(name: str = "Board1") -> str:
        """Start a NEW PCB (board) document and make it the active one.

        Returns the doc id + stackup summary. A fresh board has a 2-layer copper stackup, a
        default net class, and a GND net but no footprints or outline yet. Use ecad_find_part to
        discover footprint fpids, place them, route, then ecad_run_drc.
        """
        s = ecad_session.new_session("pcb", name)
        _ECAD["current_pcb"] = s.doc_id
        _ECAD["current"] = s.doc_id
        n_cu = s.doc.get("stackup", {}).get("copper_layers", 2)
        return _dumps({
            "ok": True, "doc_id": s.doc_id, "kind": "pcb", "name": name,
            "revision": s.revision, "copper_layers": n_cu, "footprints": 0,
            "nets": [n.get("name") for n in s.doc.get("nets", [])],
            "warnings": [],
            "hint": f"Empty {n_cu}-layer board. Place footprints (ecad_find_part finds fpids), "
                    "route tracks, then ecad_run_drc to check spacing.",
            "next_actions": [
                "ecad_find_part('0603')  # discover footprint fpids",
                "ecad_run_drc()  # at any time, see clearance/width violations",
            ],
        })

    # ---------------------------------------------------------------- find part

    @mcp.tool()
    def ecad_find_part(query: str, limit: int = 12) -> str:
        """Search the component library and return RANKED real parts so you place an actual part
        instead of inventing a libid.

        For each hit: its ``libid`` (symbols) or ``fpid`` (footprints), name, description, and
        PIN COUNT (symbols) so you know how many pins you'll have to wire. Hits are sorted best
        first. Pass the exact ``libid``/``fpid`` of a hit to ecad_place_symbol / a footprint op.
        """
        from engine.ecad.library import resolve as lib_resolve  # local: pin-count lookup
        hits = lib_search(query) or []
        out_hits: list[dict] = []
        for h in hits[:max(1, int(limit))]:
            rec = {"kind": h.get("kind"), "name": h.get("name", ""),
                   "description": h.get("description", ""), "score": h.get("score", 0)}
            if h.get("kind") == "symbol":
                libid = h.get("libid", "")
                rec["libid"] = libid
                sdef = lib_resolve(libid) or {}
                pins = sdef.get("pins", [])
                rec["pin_count"] = len(pins)
                rec["pins"] = [f"{p.get('number')}:{p.get('name')}({p.get('type')})"
                               for p in pins[:12]]
                rec["reference_prefix"] = sdef.get("reference_prefix", "")
                rec["default_value"] = sdef.get("value", "")
                if sdef.get("sim"):
                    rec["simulatable"] = True
            else:
                rec["fpid"] = h.get("fpid", "")
            out_hits.append(rec)
        if not out_hits:
            return _err(f"no library parts match {query!r}",
                        hint="Try a broader keyword (e.g. 'resistor', 'cap', 'led', 'opamp', "
                             "'connector', 'voltage', 'gnd', '0603').",
                        next_actions=["ecad_find_part('resistor')",
                                      "ecad_find_part('voltage')"],
                        query=query, hits=[])
        first = out_hits[0]
        first_id = first.get("libid") or first.get("fpid")
        ex = (f"ecad_place_symbol('{first.get('libid')}', x=0, y=0)"
              if first.get("kind") == "symbol"
              else f"use fpid '{first.get('fpid')}' on the board")
        return _dumps({
            "ok": True, "query": query, "count": len(out_hits), "hits": out_hits,
            "warnings": [],
            "hint": f"Best match: {first_id} ({first.get('description') or first.get('name')}). "
                    "Place it with its EXACT libid/fpid — do not invent ids.",
            "next_actions": [ex],
        })

    # ---------------------------------------------------------------- place symbol

    @mcp.tool()
    def ecad_place_symbol(libid: str, x: float = 0.0, y: float = 0.0,
                          ref: str = "", value: str = "",
                          angle: float = 0.0, footprint: str = "", doc_id: str = "",
                          sim_params: str = "") -> str:
        """Place a symbol on the active schematic and return its assigned REF plus its full PIN
        LIST (name/number/type/world-xy) and the count of pins still unconnected.

        The pin list is the context you need to wire: each pin's world ``xy`` is where a wire must
        reach, and ``net`` tells you what it already touches. ``value`` matters for simulation —
        a resistor needs e.g. '1k', a voltage source '5' — set it now or with ecad_set later.
        ``ref`` auto-assigns (R1, R2, ...) if omitted. ``footprint`` (an fpid like
        'Resistor_SMD:R_0603') makes the part board-ready for ecad_update_from_sch; omit it and a
        sane default for the part's libid is used automatically when you sync to a board.

        ``sim_params`` (JSON object string) attaches per-instance simulation/engineering data.
        Its ``"eng"`` sub-object overrides the library engineering metadata for THIS instance
        (partial dict — e.g. '{"eng": {"power_w": {"typ": 0.02}}}'); see
        contracts/ecad/boardphys.md. Overrides flow onto the board footprint at sync and into
        the aicad.boardphys/1 sidecar + ecad_status power totals.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        # E7: Validate libid resolves BEFORE dispatching the op. An unresolvable libid would
        # silently produce a 2-pin stub with ok:true, which then corrupts the netlist. Fail fast
        # with a clear error and direct the user to ecad_find_part.
        from engine.ecad import library as _lib
        if _lib.resolve(libid) is None:
            keyword = libid.split(":")[-1] if ":" in libid else libid
            return _err(
                f"unknown library part {libid!r} — use ecad_find_part to find a real libid",
                hint=(f"'{libid}' is not in the component library. "
                      f"Run ecad_find_part('{keyword}') to discover the correct libid."),
                next_actions=[
                    f"ecad_find_part('{keyword}')  # search for the correct libid",
                    "ecad_find_part('resistor')  # browse common parts",
                ],
                libid=libid,
            )
        op: dict = {"op": "sch_add_symbol", "libid": libid, "pos": [float(x), float(y)],
                    "angle": float(angle)}
        if ref:
            op["ref"] = ref
        if value:
            op["value"] = value
        if footprint:
            op["footprint"] = footprint
        if sim_params:
            try:
                sp = json.loads(sim_params)
            except (TypeError, ValueError) as e:
                return _err(f"sim_params is not valid JSON: {e}",
                            hint='Pass a JSON object string, e.g. '
                                 '\'{"eng": {"power_w": {"typ": 0.02}}}\'.',
                            next_actions=["ecad_place_symbol(...)  # retry with valid JSON"])
            if not isinstance(sp, dict):
                return _err("sim_params must be a JSON object",
                            hint='e.g. \'{"eng": {"power_w": {"typ": 0.02}}}\'.')
            op["sim_params"] = sp
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not place {libid!r}: {e.message}",
                        hint="Check the libid with ecad_find_part; pass an exact match.",
                        next_actions=[f"ecad_find_part('{libid.split(':')[-1]}')"],
                        detail=e.detail)
        sid = resp.get("id_map", {}).get("$0")
        doc = session.doc
        sym = next((s for s in doc.get("symbols", []) if s.get("id") == sid), None)
        if sym is None:
            return _err("placed symbol but could not locate it (internal)",
                        hint="Call ecad_status() to inspect the document.")
        pins = _placed_pins(doc, sym)
        nl = netlist_mod.build_netlist(doc)
        unconnected = _unconnected_pins(doc, nl)
        this_unconnected = [p for p in unconnected if p["ref"] == sym.get("ref")]
        warnings = list(resp.get("warnings", []))
        pin_labels = ", ".join(f"{p['number']}({p['name']}/{p['type']})" for p in pins)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "ref": sym.get("ref"), "id": sid, "libid": libid,
            "value": sym.get("value", ""), "pos": sym.get("pos"),
            "pins": pins,
            "unconnected_pins_this_symbol": [p["pinref"] for p in this_unconnected],
            "total_unconnected": len(unconnected),
            "warnings": warnings,
            "hint": (f"Placed {sym.get('ref')} with pins: {pin_labels}. "
                     f"All {len(this_unconnected)} of its pins are floating — wire each pin to "
                     "another pin (ecad_wire) or to a power/ground port (ecad_add_power)."
                     if this_unconnected else
                     f"Placed {sym.get('ref')}; its pins are already on existing nets."),
            "next_actions": (
                [f"ecad_wire('{sym.get('ref')}', '{pins[0]['number']}', '<otherRef>', "
                 f"'<otherPin>')  # connect pin {pins[0]['number']}"]
                if pins else []
            ) + ["ecad_status()  # see the whole design + what's still unconnected"],
        })

    # ---------------------------------------------------------------- wire

    @mcp.tool()
    def ecad_wire(from_ref: str, from_pin: str, to_ref: str, to_pin: str,
                  doc_id: str = "") -> str:
        """Auto-route an orthogonal wire from one pin to another (emits sch_connect) and return
        the RESULTING net plus the updated list of what is still unconnected.

        ``from_ref``/``to_ref`` are ref-des ('R1') or internal ids; pins are pin numbers ('1') or
        names. The engine names the net automatically (power/label name wins, else N$n). The
        returned ``net`` + ``net_members`` confirm the two pins really joined.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        a = _resolve_ref_or_id(doc, from_ref)
        b = _resolve_ref_or_id(doc, to_ref)
        if a is None or b is None:
            missing = from_ref if a is None else to_ref
            refs = [s.get("ref") for s in doc.get("symbols", [])]
            return _err(f"no symbol {missing!r} on the schematic",
                        hint=f"Placed refs are: {', '.join(refs) or '(none)'}. "
                             "Place the part first or fix the ref.",
                        next_actions=["ecad_status()"],
                        placed_refs=refs)
        op = {"op": "sch_connect",
              "from": {"sym": a.get("id"), "pin": str(from_pin)},
              "to": {"sym": b.get("id"), "pin": str(to_pin)}}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            # Most common cause: a bad pin name/number — list the real pins to recover.
            a_pins = [p["number"] for p in _placed_pins(doc, a)]
            b_pins = [p["number"] for p in _placed_pins(doc, b)]
            return _err(f"could not wire {from_ref}.{from_pin} -> {to_ref}.{to_pin}: {e.message}",
                        hint=f"{from_ref} pins: {a_pins}; {to_ref} pins: {b_pins}. "
                             "Use a real pin number/name.",
                        next_actions=[f"ecad_wire('{from_ref}', '{a_pins[0] if a_pins else '1'}', "
                                      f"'{to_ref}', '{b_pins[0] if b_pins else '1'}')"],
                        detail=e.detail)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        # which net did this pin land on?
        by_pin = nl.get("by_pin", {})
        net = by_pin.get(f"{a.get('id')}.{from_pin}") or by_pin.get(f"{b.get('id')}.{to_pin}") or ""
        members = nl.get("nets", {}).get(net, [])
        member_refs = [f"{m.get('ref')}.{m.get('pin')}" for m in members]
        unconnected = _unconnected_pins(doc, nl)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net or None, "net_members": member_refs, "net_size": len(members),
            "total_unconnected": len(unconnected),
            "unconnected_pins": [_pretty_pin(p) for p in unconnected[:12]],
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Connected {from_ref}.{from_pin} <-> {to_ref}.{to_pin} on net "
                     f"{net or 'N$?'} ({len(members)} pins). "
                     + (f"{len(unconnected)} pin(s) still unconnected: "
                        f"{', '.join(_pretty_pin(p) for p in unconnected[:6])}"
                        f"{'...' if len(unconnected) > 6 else ''} — wire them or mark no-connect."
                        if unconnected else "Every pin is now connected.")),
            "next_actions": (
                [f"ecad_wire('{unconnected[0]['ref']}', '{unconnected[0]['pin']}', "
                 "'<ref>', '<pin>')"] if unconnected else
                ["ecad_run_erc()  # verify the design electrically",
                 "ecad_simulate('op')  # solve DC node voltages"]
            ),
        })

    # ---------------------------------------------------------------- power / label

    @mcp.tool()
    def ecad_add_power(net: str, x: float = 0.0, y: float = 0.0, doc_id: str = "") -> str:
        """Drop a power/ground port (e.g. net='GND', 'VCC', '+5V') at ``(x, y)``.

        A power port both NAMES the net it sits on and acts as its source for ERC (a 'GND' port
        means power_in pins on that net are driven). Place it at a pin's world xy (from
        ecad_place_symbol's pin list) to bond a pin straight to that rail without a wire.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        op = {"op": "sch_add_power", "net": net, "pos": [float(x), float(y)]}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not add power port {net!r}: {e.message}", hint=e.message)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        members = nl.get("nets", {}).get(net, [])
        member_refs = [f"{m.get('ref')}.{m.get('pin')}" for m in members]
        unconnected = _unconnected_pins(doc, nl)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "pos": [float(x), float(y)],
            "net_size": len(members),
            "net_members": member_refs,
            "total_unconnected": len(unconnected),
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Power port '{net}' placed. "
                     + (f"It bonded to {len(members)} pin(s): "
                        f"{', '.join(member_refs[:6])}."
                        if members else
                        "No pin is at this location yet — place it on a pin's world xy "
                        "(see a symbol's pin list) or run a wire to it.")),
            "next_actions": ["ecad_status()",
                             "ecad_run_erc()  # power_not_driven errors disappear once a rail "
                             "is sourced"],
        })

    @mcp.tool()
    def ecad_label(text: str, x: float = 0.0, y: float = 0.0, scope: str = "local",
                   doc_id: str = "") -> str:
        """Place a net label ``text`` at ``(x, y)``. Two labels of the same text+scope on a sheet
        merge their nets; ``scope='global'`` merges across sheets. Drop a label on a wire/pin to
        give that net a readable name (used by ERC, the netlist, and SPICE probes).
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        op = {"op": "sch_add_label", "text": text, "pos": [float(x), float(y)], "scope": scope}
        try:
            resp = _apply(session, [op])
        except OpError as e:
            return _err(f"could not add label {text!r}: {e.message}", hint=e.message)
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        members = nl.get("nets", {}).get(text, [])
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "label": text, "scope": scope, "pos": [float(x), float(y)],
            "net_size": len(members),
            "net_members": [f"{m.get('ref')}.{m.get('pin')}" for m in members],
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Label '{text}' placed. "
                     + (f"It names a net with {len(members)} pin(s)."
                        if members else
                        "It is not on any wire/pin yet — place it ON a wire or a pin's world xy "
                        "to name that net.")),
            "next_actions": ["ecad_status()"],
        })

    # ---------------------------------------------------------------- status (THE context tool)

    @mcp.tool()
    def ecad_status(doc_id: str = "") -> str:
        """THE situational-awareness call. Returns a COMPACT design summary sized for a small
        context window: component count + list (each with its ``power_w`` typ/max from the
        engineering-metadata layer — honest nulls when unknown), a board ``power`` total
        (contracts/ecad/boardphys.md), every net with its size and members, the ERC
        violation summary grouped by rule, and a PRIORITIZED ``next_actions`` list naming the
        exact pins to fix.

        Call this whenever you are unsure what to do next — it tells you what exists, what is
        wrong, and the single best next op.
        """
        did = doc_id or _ECAD["current"] or _ECAD["current_sch"] or _ECAD["current_pcb"]
        if did is None:
            return _err("no active document",
                        hint="Start one first.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_new_pcb('myboard')"])
        try:
            session = ecad_session.get_session(did)
        except OpError as e:
            return _err(e.message, hint="That doc id is not open.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        if session.kind == "pcb":
            return _pcb_status(session)
        # ---- schematic status ----
        nl = netlist_mod.build_netlist(doc)
        components = []
        comp_engs = []
        for c in nl.get("components", []):
            eng = _component_eng(libid=c.get("libid") or "", fpid=c.get("footprint") or "",
                                 overrides=c.get("eng_overrides"))
            comp_engs.append(eng)
            components.append({"ref": c.get("ref"), "libid": c.get("libid"),
                               "value": c.get("value"), "footprint": c.get("footprint") or None,
                               "pins": c.get("pins"),
                               # T-FIN8: per-component power (honest nulls when unknown —
                               # contracts/ecad/boardphys.md)
                               "power_w": eng["power_w"] if eng else {"typ": None, "max": None}})
        power = _power_totals(comp_engs)
        nets = []
        for name, members in sorted(nl.get("nets", {}).items()):
            nets.append({"name": name, "size": len(members),
                         "members": [f"{m.get('ref')}.{m.get('pin')}" for m in members]})
        # ERC
        violations: list[dict] = []
        try:
            from engine.ecad import erc as erc_mod
            violations = erc_mod.run_erc(doc, nl)
        except Exception:  # noqa: BLE001 — ERC is advisory; never break status
            violations = []
        erc = _erc_summary(violations)
        unconnected = _unconnected_pins(doc, nl)

        # Build a prioritized next_actions list — errors first, then unconnected pins.
        next_actions: list[str] = []
        for g in erc["groups"]:
            if g["severity"] == "error":
                tgt = (g["pins"][:3] or g["nets"][:3])
                next_actions.append(
                    f"FIX ERROR {g['rule']} ({g['count']}x): {', '.join(map(str, tgt))} — "
                    f"{g['messages'][0] if g['messages'] else ''}")
        if unconnected:
            names = ", ".join(_pretty_pin(p) for p in unconnected[:6])
            more = "..." if len(unconnected) > 6 else ""
            next_actions.append(
                f"{len(unconnected)} pin(s) unconnected: {names}{more} — wire them "
                "(ecad_wire) or mark no-connect.")
        if not components:
            next_actions.append("Empty schematic — ecad_find_part then ecad_place_symbol.")
        elif not violations and not unconnected:
            next_actions.append("Design is clean — ecad_simulate('op') or ecad_run_drc on a board.")

        # one-line headline
        head = (f"{len(components)} component(s), {len(nets)} net(s), "
                f"{erc['errors']} ERC error(s), {erc['warnings']} warning(s), "
                f"{len(unconnected)} unconnected pin(s).")
        if power and power.get("power_w_typ") is not None:
            head += f" Board power ~{power['power_w_typ'] * 1000:.1f} mW typ."
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "sch",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "component_count": len(components), "components": components,
            "power": power,
            "net_count": len(nets), "nets": nets,
            "erc": erc,
            "unconnected_pins": [_pretty_pin(p) for p in unconnected],
            "warnings": nl.get("warnings", []),
            "hint": head + " Fix ERC errors first, then connect floating pins, then simulate.",
            "next_actions": next_actions or ["ecad_simulate('op')"],
        })

    def _pcb_status(session) -> str:
        doc = session.doc
        fps = doc.get("footprints", [])
        nets = doc.get("nets", [])
        tracks = doc.get("tracks", [])
        vias = doc.get("vias", [])
        violations: list[dict] = []
        try:
            from engine.ecad import drc as drc_mod
            res = drc_mod.run_drc(doc)
            violations = res if isinstance(res, list) else res.get("violations", [])
        except Exception:  # noqa: BLE001
            violations = []
        erc = _erc_summary(violations)  # same grouping shape works for DRC
        next_actions: list[str] = []
        for g in erc["groups"]:
            if g["severity"] == "error":
                next_actions.append(f"FIX DRC {g['rule']} ({g['count']}x): "
                                    f"{g['messages'][0] if g['messages'] else ''}")
        if not fps:
            next_actions.append("No footprints — place parts (ecad_find_part finds fpids).")
        if not doc.get("outline", {}).get("shapes"):
            next_actions.append("No board outline yet — set one with a pcb_set_outline op.")
        # T-FIN8: per-footprint power + board total (honest nulls when unknown).
        fp_engs = [_component_eng(libid=f.get("libid") or "", fpid=f.get("fpid") or "",
                                  overrides=_fp_overrides(f)) for f in fps]
        power = _power_totals(fp_engs)
        head = (f"{len(fps)} footprint(s), {len(nets)} net(s), {len(tracks)} track(s), "
                f"{len(vias)} via(s), {erc['errors']} DRC error(s), {erc['warnings']} warning(s).")
        if power and power.get("power_w_typ") is not None:
            head += f" Board power ~{power['power_w_typ'] * 1000:.1f} mW typ."
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "pcb",
            "name": doc.get("name"), "revision": session.revision,
            "summary": head,
            "footprint_count": len(fps),
            "footprints": [{"ref": f.get("ref"), "fpid": f.get("fpid"),
                            "pos": f.get("pos"), "side": f.get("side"),
                            "power_w": (e or {}).get("power_w", {"typ": None, "max": None})}
                           for f, e in zip(fps, fp_engs)],
            "power": power,
            "nets": [n.get("name") for n in nets],
            "track_count": len(tracks), "via_count": len(vias),
            "drc": erc,
            "warnings": [],
            "hint": head + " Fix DRC errors first.",
            "next_actions": next_actions or ["ecad_run_drc()", "ecad_export('gerber')"],
        })

    # ---------------------------------------------------------------- ERC

    @mcp.tool()
    def ecad_run_erc(doc_id: str = "") -> str:
        """Run the Electrical Rules Check on the active schematic and return the violations as
        readable lines plus a one-line verdict ('N errors, M warnings; fix errors first: ...').

        ERC is advisory — it never blocks edits. Errors (output-output shorts, undriven power,
        no-connect-connected) are real bugs; warnings (floating pins, missing footprint) are
        usually wiring you haven't done yet.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.ecad import erc as erc_mod
            violations = erc_mod.run_erc(doc, nl)
        except Exception as e:  # noqa: BLE001
            return _err(f"ERC engine error: {e}", hint="Inspect with ecad_status().")
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        first_errors = [v for v in violations if v.get("severity") == "error"][:3]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" fix errors first: "
                      + "; ".join(v.get("message", "") for v in first_errors)
                      if first_errors else
                      (" No errors." if summary['warnings'] else " Clean.")))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warnings": summary["warnings"],
            "verdict": verdict,
            "violations": lines,
            "by_rule": summary["groups"],
            "hint": verdict,
            "next_actions": (
                [f"Address {first_errors[0].get('rule')}: "
                 f"{first_errors[0].get('message')}"] if first_errors else
                ["ecad_simulate('op')  # ERC is clean enough to solve the circuit"]
            ),
        })

    # ---------------------------------------------------------------- simulate

    @mcp.tool()
    def ecad_simulate(analysis: str = "op", params: Optional[dict] = None,
                      probes: Optional[list] = None, doc_id: str = "") -> str:
        """Build the netlist -> SPICE circuit -> run the native simulator, and return the KEY
        results with a plain-English reading.

        ``analysis`` is one of:
          'op'    -- DC operating point (no params needed)
          'dc'    -- source sweep {"source":"V1","start":0,"stop":5,"step":0.05}
          'tran'  -- transient {"tstep":1e-6,"tstop":1e-3,"uic":false}
          'ac'    -- frequency response {"sweep":"dec","points":20,"fstart":1,"fstop":1e6}
          'four'  -- Fourier/THD on a transient trace {"f0":1e3,"tstop":5e-3,"probe":"V(OUT)"}
                     -> returns THD% + dominant harmonics; reading: "THD=0.12%, H1=4.99 V"
          'mc'    -- Monte-Carlo tolerance {"runs":50,"probe":"V(OUT)","tol":{"R":0.01}}
                     -> returns mean/std/yield; reading: "mean=2.5V, sigma=0.03V, yield=98%"
          'temp'  -- temperature sweep {"values":[-40,27,85],"inner":"op","probe":"V(OUT)"}
                     -> returns output trend vs temperature
          'noise' -- output noise density {"output":"V(OUT)","fstart":10,"fstop":1e6}
                     -> returns total RMS noise + per-resistor contributions
        If ``probes`` is omitted, DEFAULTS TO ALL NODE VOLTAGES (you won't always know the node
        names — this surfaces them all). Returns op node voltages, or for tran/ac a compact
        summary (final value / -3 dB point) plus a sentence like 'V(OUT) settles to 2.49 V'.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a schematic first.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.sim.spice import netlist_to_spice_circuit
            from engine.sim.circuit import simulate as sim_run
        except Exception as e:  # noqa: BLE001
            return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                        hint="The sim engine (numpy/scipy) is not importable here.")
        circuit, warns = netlist_to_spice_circuit(nl)
        if not circuit.get("devices"):
            return _err("nothing to simulate — the netlist has no simulatable devices",
                        hint="Place parts with sim models (R/C/L/V/I/D/Q) and give them VALUES "
                             "(a resistor needs '1k', a source '5'); wire them into a circuit.",
                        next_actions=["ecad_find_part('voltage')  # add a V source",
                                      "ecad_status()"],
                        warnings=warns)

        # default probes = every non-ground node voltage (the weak model rarely names probes).
        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        if probes:
            probe_list = list(probes)
        else:
            probe_list = [f"V({n})" for n in nodes]
            # also add the current through every voltage source — usually the interesting datum.
            probe_list += [f"I({d['name']})" for d in circuit["devices"] if d.get("kind") == "V"]

        an = _build_analysis(analysis, params or {}, circuit)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac', 'four', 'mc', 'temp', 'noise'.",
                        next_actions=["ecad_simulate('op')"])

        # Advanced analyses: four/mc/temp/noise each wrap or extend an inner analysis. These are the
        # heaviest sim paths (Monte-Carlo runs many solves) — budget-guard so an overrun never times
        # out the transport and kills the server (Gap (b)).
        if an["type"] in ("four", "mc", "temp", "noise"):
            _autosave_all_open_ecad()
            status, val = _run_with_budget(
                lambda: _simulate_advanced(session, circuit, an, probe_list, warns))
            if status == "timeout":
                return _budget_timeout_err(
                    f"{an['type']} analysis",
                    simplify=("Reduce the work: fewer Monte-Carlo runs, fewer temperature points, a "
                              "shorter tstop / coarser tstep, or a narrower frequency span."),
                    next_actions=["ecad_simulate('op')  # a single fast operating point",
                                  "ecad_status()"],
                    analysis=an["type"])
            if status == "error":
                raise val
            return val

        # AC needs a small-signal stimulus. The structured netlist carries only DC
        # values, so unless a source already has an AC magnitude (an ecad_design
        # filter now persists one) an AC sweep would read a flat zero at every
        # frequency (Bug E-1). Honor an explicit ac_source/ac_mag in params; else
        # auto-drive a source with AC=1 and SAY SO, so the agent never silently
        # gets zero and has no idea why.
        if an["type"] == "ac":
            p = params or {}

            def _ac_mag(d: dict) -> float:
                try:
                    return float(((d.get("params") or {}).get("ac") or {}).get("mag", 0.0) or 0.0)
                except (TypeError, ValueError):
                    return 0.0

            vsrcs = [d for d in circuit["devices"] if d.get("kind") == "V"]
            want_mag = float(p.get("ac_mag", 1.0) or 1.0)
            want_src = p.get("ac_source") or p.get("source")
            if not vsrcs:
                warns.append("AC sweep has no voltage source to drive — add a V source "
                             "and wire it to the input.")
            elif want_src:
                tgt = next((d for d in vsrcs if d.get("name") == want_src), None)
                if tgt is None:
                    warns.append(f"ac_source {want_src!r} not found among "
                                 f"{[d['name'] for d in vsrcs]} — drove {vsrcs[0]['name']} instead.")
                    tgt = vsrcs[0]
                tgt.setdefault("params", {})["ac"] = {"mag": want_mag, "phase": 0.0}
            elif not any(_ac_mag(d) > 0 for d in vsrcs):
                tgt = vsrcs[0]
                tgt.setdefault("params", {})["ac"] = {"mag": want_mag, "phase": 0.0}
                warns.append(
                    f"no source carried an AC magnitude — auto-drove {tgt['name']} with AC={want_mag} "
                    f"so the sweep has a stimulus. Pass params={{'ac_source':'V1','ac_mag':1}} to set it explicitly.")

        study = {"analyses": [an], "probes": probe_list}
        _autosave_all_open_ecad()
        status, result = _run_with_budget(lambda: sim_run(circuit, study))
        if status == "timeout":
            return _budget_timeout_err(
                f"{an['type']} simulation",
                simplify=("Simplify the circuit (fewer nodes) or the analysis — for tran shorten "
                          "tstop / coarsen tstep, for ac narrow the frequency span, or just run "
                          "'op'."),
                next_actions=["ecad_simulate('op')  # a single fast operating point",
                              "ecad_status()"],
                analysis=an["type"])
        if status == "error":
            raise result
        conv = result.get("convergence", {})
        converged = bool(conv.get(an["type"], False))

        payload: dict = {
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "converged": converged,
            "device_count": len(circuit["devices"]),
            "nodes": nodes, "probes": probe_list,
            "warnings": warns + ([] if converged else
                                 [f"{an['type']} did not converge — see sim_log"]),
            "sim_log": result.get("log", [])[-6:],
        }

        reading_lines: list[str] = []
        if an["type"] == "op":
            op = result.get("op", {})
            payload["op"] = {k: _round(v) for k, v in op.items()}
            for k, v in sorted(op.items()):
                if k.startswith("V(") and _finite(v):
                    reading_lines.append(f"{k} = {_round(v)} V")
            for k, v in sorted(op.items()):
                if k.startswith("I(") and _finite(v):
                    reading_lines.append(f"{k} = {_eng(v)}A")
        elif an["type"] == "dc":
            payload["dc"] = _summarize_sweep(result.get("dc", {}), reading_lines, unit="V")
        elif an["type"] == "tran":
            payload["tran"] = _summarize_tran(result.get("tran", {}), reading_lines)
        elif an["type"] == "ac":
            payload["ac"] = _summarize_ac(result.get("ac", {}), reading_lines)

        reading = ("; ".join(reading_lines) if reading_lines else
                   "no finite probe values — check connectivity / source values")
        payload["reading"] = reading
        payload["hint"] = (
            (f"{an['type'].upper()} solved: {reading}." if converged else
             f"{an['type'].upper()} did NOT converge: {reading}. Check that every node has a DC "
             "path to ground and that source/part values are sane (run ecad_run_erc).")
        )
        payload["next_actions"] = (
            ["ecad_status()  # the result matches the topology you built"] if converged else
            ["ecad_run_erc()  # find the connectivity/driver problem",
             "ecad_status()  # confirm wiring"]
        )
        return _dumps(payload)

    # ---------------------------------------------------------------- DRC

    @mcp.tool()
    def ecad_run_drc(doc_id: str = "") -> str:
        """Run the Design Rule Check on the active board and return a summary with the worst
        offenders (clearance, track width, via size, annular ring, edge clearance, etc.).

        DRC is advisory. The grouped ``by_rule`` view + the readable lines tell you which physical
        spacing/size rule each track/pad/via breaks so you can widen, move, or re-space it.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Open a board first.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        try:
            from engine.ecad import drc as drc_mod
            res = drc_mod.run_drc(doc)
            violations = res if isinstance(res, list) else res.get("violations", [])
        except Exception as e:  # noqa: BLE001
            return _err(f"DRC engine error: {e}", hint="Inspect with ecad_status().")
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)
        worst = lines[:5]
        verdict = (f"{summary['errors']} error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(worst) if worst else " Clean board."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warnings": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "warnings": [],
            "hint": verdict,
            "next_actions": (
                ["Address the worst DRC offenders above (widen tracks / increase spacing / "
                 "move parts), then re-run ecad_run_drc()."] if violations else
                ["ecad_export('gerber')  # board passes DRC — generate fab outputs"]
            ),
        })

    # ---------------------------------------------------------------- DFM

    @mcp.tool()
    def ecad_run_dfm(doc_id: str = "") -> str:
        """Run the Design-For-Manufacturing (DFM) check suite on the active board and return a
        readable summary grouped by rule (counts + worst offenders) with a severity split.

        DFM is advisory: violations are warnings or (for courtyard_overlap) errors but never block
        editing. Rules checked: silk_over_pad, silk_min_width, silk_min_text, mask_sliver,
        acid_trap, courtyard_overlap, annular_min (DFM grade), drill_min (DFM grade).

        The reading tells you which rule to fix first: courtyard_overlap (component collision) is
        always fix-first, followed by drill_min errors, then warnings.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="DFM needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        try:
            from engine.ecad import dfm as dfm_mod  # lazy import
        except Exception as e:  # noqa: BLE001
            return _err(f"DFM engine error: {e}", hint="Inspect with ecad_status().")
        # DFM walks every pad/track/courtyard — on a dense board it can run long. Budget-guard so an
        # overrun never times out the transport and kills the server (Gap (b)).
        _autosave_all_open_ecad()
        status, violations = _run_with_budget(lambda: dfm_mod.run_dfm(doc))
        if status == "timeout":
            return _budget_timeout_err(
                "DFM check",
                simplify="The board is large/dense — run DFM on a simpler board or after thinning "
                         "silk/copper, or check DRC first (ecad_run_drc).",
                next_actions=["ecad_run_drc()", "ecad_status()"])
        if status == "error":
            return _err(f"DFM engine error: {violations}", hint="Inspect with ecad_status().")
        summary = _erc_summary(violations)
        lines = _erc_lines(violations)

        # group by rule: count + worst message
        worst_errors = [v for v in violations if v.get("severity") == "error"][:3]
        worst_lines = lines[:5]

        # Build a priority-ordered fix list.
        fix_order: list[str] = []
        # courtyard_overlap first (components collide -> assembly fails)
        cy_group = next((g for g in summary["groups"] if g["rule"] == "courtyard_overlap"), None)
        if cy_group:
            items = cy_group.get("items", [])[:2]
            fix_order.append(
                f"fix courtyard_overlap first -- components collide "
                f"({cy_group['count']}x, items: {', '.join(map(str, items))})")
        for g in summary["groups"]:
            if g["rule"] == "courtyard_overlap":
                continue
            if g["severity"] == "error":
                fix_order.append(
                    f"fix {g['rule']} ({g['count']}x): "
                    f"{g['messages'][0] if g['messages'] else ''}")
        for g in summary["groups"]:
            if g["severity"] != "error":
                fix_order.append(
                    f"review {g['rule']} ({g['count']} warning(s))")

        verdict = (f"{summary['errors']} DFM error(s), {summary['warnings']} warning(s)."
                   + (" worst: " + " | ".join(worst_lines) if worst_lines else " Clean."))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "errors": summary["errors"], "warning_count": summary["warnings"],
            "verdict": verdict,
            "violations": lines, "by_rule": summary["groups"],
            "fix_priority": fix_order,
            "warnings": worst_lines[:3],
            "reading": verdict,
            "hint": verdict + (" " + fix_order[0] if fix_order else ""),
            "next_actions": (
                fix_order[:2] if fix_order else
                ["ecad_export('gerber')  # DFM clean -- proceed to fab"]
            ),
        })

    # ---------------------------------------------------------------- sync sch -> pcb

    @mcp.tool()
    def ecad_update_from_sch(sch_doc_id: str = "", pcb_doc_id: str = "") -> str:
        """Populate the active PCB from the active schematic (KiCad 'Update PCB from Schematic').

        Adds a footprint for every schematic component that carries one, binds each pad to its
        schematic net, registers missing nets, and reports parts that no longer exist on the
        schematic. Sim-only parts (V/I sources) and power symbols are skipped (they have no
        footprint). Run this AFTER the schematic is wired and BEFORE routing: the typical flow is
        update_from_sch -> ecad_autoroute -> ecad_fill_zones.
        """
        sch_id = sch_doc_id or _ECAD["current_sch"]
        pcb_id = pcb_doc_id or _ECAD["current_pcb"]
        if sch_id is None:
            return _err("no active schematic to sync FROM",
                        hint="Build a schematic first (ecad_new_schematic, place + wire parts).",
                        next_actions=["ecad_new_schematic('mydesign')"])
        if pcb_id is None:
            return _err("no active board to sync INTO",
                        hint="Create a board first; it will be populated from the schematic.",
                        next_actions=["ecad_new_pcb('myboard')"])
        from engine.ecad import rpc as ecad_rpc
        try:
            resp = ecad_rpc._ecad_update_from_sch(
                {"board_doc_id": pcb_id, "sch_doc_id": sch_id}, lambda _d: None)
        except OpError as e:
            return _err(f"update_from_sch failed: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        report = resp.get("report", {})
        board = resp.get("tree", {})
        compile_info = resp.get("compile", {})
        ratsnest = compile_info.get("ratsnest", []) or []
        added = report.get("added", [])
        updated = report.get("updated", [])
        unresolved = report.get("unresolved", [])
        warnings = list(resp.get("warnings", []))
        head = (f"{len(added)} footprint(s) added, {len(updated)} updated, "
                f"{report.get('nets_bound', 0)} pad-net binding(s); "
                f"{len(ratsnest)} airwire(s) now need routing.")
        next_actions: list[str] = []
        if unresolved:
            next_actions.append(
                "Resolve missing footprints (assign a real fpid in the schematic): "
                + ", ".join(f"{ref}({fpid})" for ref, fpid in unresolved))
        if ratsnest:
            next_actions.append("ecad_autoroute()  # connect the ratsnest, then ecad_fill_zones()")
        else:
            next_actions.append("ecad_run_drc()  # board populated; check spacing")
        return _dumps({
            "ok": True, "doc_id": pcb_id, "sch_doc_id": sch_id, "kind": "pcb",
            "revision": resp.get("revision"),
            "footprints_added": added, "footprints_updated": updated,
            "nets_bound": report.get("nets_bound", 0),
            "unresolved": unresolved,
            "footprint_count": len(board.get("footprints", [])),
            "ratsnest_remaining": len(ratsnest),
            "warnings": warnings,
            "hint": head + " Next: ecad_autoroute then ecad_fill_zones.",
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- autoroute

    @mcp.tool()
    def ecad_autoroute(pitch: float = 0.0,
                       diff_pairs: Optional[Union[list, str]] = None,
                       ripup_passes: int = 2, doc_id: str = "") -> str:
        """Auto-route the active board's ratsnest with a basic 2-layer grid (Lee/A*) router.

        Connects the unrouted airwires with orthogonal tracks + through-vias, respecting clearance.
        Honest about limits: failed nets are reported by name and need manual routing or more space.

        ``pitch`` (mm) overrides the grid resolution (default 0.25); finer threads tighter gaps
        but is slower. Run ecad_update_from_sch first so there is a ratsnest to route.

        ``diff_pairs`` controls differential-pair routing:
          - omit (default): auto-detect pairs by *_P/*_N, *+/*- naming
          - [['CLK_P','CLK_N'], ...]: route specific named pairs coupled
          - 'auto': same as omitting (explicit auto-detect)

        ``ripup_passes`` (default 2): how many rip-up-and-reroute sweeps to attempt after the
        greedy pass; 0 for the original single-pass behaviour.

        Returns per-pair skew/coupling stats in ``diff_pair_stats`` when pairs are routed.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Routing needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        from engine.ecad import rpc as ecad_rpc
        opts: dict = {}
        if pitch and pitch > 0:
            opts["pitch"] = float(pitch)
        opts["ripup_passes"] = max(0, int(ripup_passes))
        # diff_pairs: None/'auto'/'' -> let the router auto-detect; a list -> pass through
        # M3: diff_pairs may be the string 'auto' (documented alias for auto-detect);
        # treat any non-list value as "auto" so callers that pass 'auto' don't get a
        # Pydantic/iteration error trying to iterate over a string.
        if diff_pairs is not None and not isinstance(diff_pairs, str):
            opts["diff_pairs"] = [[str(p), str(n)] for p, n in (
                (entry[0], entry[1]) for entry in diff_pairs if len(entry) >= 2)]
        # The Lee/A* router over a fine grid is one of the heaviest ECAD ops — budget-guard so an
        # overrun never times out the transport and kills the server (Gap (b)).
        _autosave_all_open_ecad()
        status, resp = _run_with_budget(lambda: ecad_rpc._ecad_autoroute(
            {"doc_id": session.doc_id, "opts": opts}, lambda _d: None))
        if status == "timeout":
            return _budget_timeout_err(
                "autoroute",
                simplify="Give the router less to do: route fewer nets, use a coarser pitch "
                         "(ecad_autoroute(pitch=0.3)), or place parts with more room.",
                next_actions=["ecad_autoroute(pitch=0.3)  # coarser grid is much faster",
                              "ecad_status()"])
        if status == "error":
            e = resp  # on 'error', _run_with_budget returns the raised exception
            if isinstance(e, OpError):
                return _err(f"autoroute failed: {e.message}", hint=e.message,
                            next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
            raise e
        report = resp.get("report", {})
        routed = report.get("routed", [])
        failed = report.get("failed", [])
        before = report.get("airwires_before", 0)
        after = report.get("airwires_after", 0)
        warnings = list(resp.get("warnings", []))
        ripup_iters = report.get("ripup_iterations", 0)
        dp_stats = report.get("diff_pairs", [])

        # Build a human-readable summary of diff-pair results.
        dp_reading_lines: list[str] = []
        for dp in dp_stats:
            # The autoroute engine uses 'nets' list and 'routed' bool.
            nets = dp.get("nets", [])
            net_p = nets[0] if len(nets) > 0 else dp.get("net_p", "?")
            net_n = nets[1] if len(nets) > 1 else dp.get("net_n", "?")
            skew = dp.get("skew_mm")
            coupled_pct = dp.get("coupled_pct")
            dp_routed = dp.get("routed", dp.get("status") == "ok")
            status_str = "ok" if dp_routed else "failed"
            line = f"Pair {net_p}/{net_n}: status={status_str}"
            if skew is not None:
                line += f", skew={skew:.3f}mm"
            if coupled_pct is not None:
                line += f", coupled={coupled_pct:.0f}%"
            dp_reading_lines.append(line)

        head = (f"routed {len(routed)} net(s), {len(failed)} failed; ratsnest "
                f"{before} -> {after} airwire(s); {report.get('vias', 0)} via(s), "
                f"{report.get('length_mm', 0.0)} mm of track"
                + (f"; rip-up rescued {ripup_iters} attempt(s)" if ripup_iters else "")
                + (f"; {len(dp_stats)} diff pair(s)" if dp_stats else "") + ".")
        next_actions: list[str] = []
        if failed:
            next_actions.append(
                f"{len(failed)} net(s) failed ({', '.join(map(str, failed[:6]))}) — "
                "they need manual routing or more space (move parts apart / finer pitch).")
            next_actions.append("ecad_autoroute(pitch=0.15)  # retry with a finer grid")
        else:
            next_actions.append("ecad_fill_zones()  # pour copper, then ecad_run_drc()")
        if dp_stats and any(not dp.get("routed", True) for dp in dp_stats):
            next_actions.append(
                "Check diff-pair results: skew > 0.2mm or coupled_pct < 80 means "
                "the pair needs manual tuning or more board space.")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "routed_nets": routed, "failed_nets": failed,
            "airwires_before": before, "airwires_after": after,
            "ratsnest_remaining": after,
            "vias": report.get("vias", 0), "length_mm": report.get("length_mm", 0.0),
            "grid": report.get("grid", {}),
            "ripup_iterations": ripup_iters,
            "diff_pair_stats": dp_stats,
            "diff_pair_reading": "; ".join(dp_reading_lines) if dp_reading_lines else None,
            "warnings": warnings,
            "hint": head + (" All nets routed." if not failed else
                            f" {len(failed)} net(s) failed — manual routing or more space needed.")
                  + (" " + "; ".join(dp_reading_lines) if dp_reading_lines else ""),
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- zones

    @mcp.tool()
    def ecad_add_zone(net: str, layer: str = "F.Cu",
                      rect_or_polygon: Optional[list] = None, doc_id: str = "") -> str:
        """Add a copper pour (zone) bound to ``net`` on ``layer`` — usually a ground/power plane.

        ``rect_or_polygon`` is either a rectangle ``[x0, y0, x1, y1]`` (auto-expanded to 4 corners)
        or an explicit polygon ``[[x, y], ...]`` (>=3 points). Omit it to flood the whole board
        outline bounding box (a full-board GND pour, the common case). The pour is not filled until
        ecad_fill_zones runs (compile fills a coarse preview automatically after this edit).
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Zones live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        polygon = _zone_polygon(doc, rect_or_polygon)
        if polygon is None:
            return _err("could not derive a zone polygon",
                        hint="Pass rect_or_polygon as [x0,y0,x1,y1] or [[x,y],...] (>=3 pts), "
                             "or set a board outline first so the whole-board default works.",
                        next_actions=["ecad_status()"])
        # The zone op requires the net to already exist on the board (there is no add-net op). A
        # fresh board has GND; other nets arrive via ecad_update_from_sch. Guide the model rather
        # than emitting an op that would fail.
        if not any(n.get("name") == net for n in doc.get("nets", [])):
            existing = [n.get("name") for n in doc.get("nets", [])]
            return _err(f"net {net!r} is not on the board yet",
                        hint=f"Board nets are: {', '.join(existing) or '(none)'}. A fresh board "
                             "has GND; populate other nets with ecad_update_from_sch first, or "
                             "pour GND.",
                        next_actions=["ecad_add_zone('GND')  # the default ground net",
                                      "ecad_update_from_sch()  # bind schematic nets to the board"],
                        board_nets=existing)
        try:
            resp = _apply(session, [{"op": "pcb_add_zone", "net": net, "layer": layer,
                                     "polygon": polygon}])
        except OpError as e:
            return _err(f"could not add zone on {net!r}/{layer}: {e.message}", hint=e.message,
                        next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
        zones = session.doc.get("zones", [])
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": session.revision,
            "net": net, "layer": layer, "polygon": polygon,
            "zone_count": len(zones),
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Added a {net} copper pour on {layer} ({len(polygon)} vertices). "
                     "Fill it (ecad_fill_zones) to compute the actual copper, then ecad_run_drc."),
            "next_actions": ["ecad_fill_zones()  # compute the poured copper",
                             "ecad_run_drc()  # confirm clearance to other-net copper"],
        })

    @mcp.tool()
    def ecad_fill_zones(doc_id: str = "") -> str:
        """Fill every copper pour on the active board at FULL resolution and report the result.

        Computes each zone's copper polygons (outline clipped to the board, minus clearance around
        other-net copper, with thermal relief on same-net through-hole pads) and persists them — this
        is the copper that gets exported to Gerber. Reports zones filled + total copper area. The
        interactive rebuild already fills a coarse preview after each edit; call this before export
        for the precise fill.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Pours live on a board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        if not session.doc.get("zones"):
            return _err("the board has no copper pours to fill",
                        hint="Add a ground/power pour first.",
                        next_actions=["ecad_add_zone('GND')  # whole-board ground pour"])
        from engine.ecad import rpc as ecad_rpc
        # Full-resolution zone fill rasterizes the whole board at 0.1mm (~1/res^2 cells) — a big or
        # fine-grid pour can run long. Budget-guard so an overrun never times out the transport and
        # kills the server (Gap (b); this was the make_board trigger in adb46f62).
        _autosave_all_open_ecad()
        status, resp = _run_with_budget(
            lambda: ecad_rpc._ecad_fill_zones({"doc_id": session.doc_id}, lambda _d: None))
        if status == "timeout":
            return _budget_timeout_err(
                "zone fill",
                simplify="The pour is large/fine — shrink the zone polygon or the board outline. "
                         "The interactive compile already keeps a coarse preview fill.",
                next_actions=["ecad_run_drc()", "ecad_status()"])
        if status == "error":
            e = resp
            if isinstance(e, OpError):
                return _err(f"zone fill failed: {e.message}", hint=e.message,
                            next_actions=["ecad_status()"], detail=getattr(e, "detail", {}))
            raise e
        summary = resp.get("fill_summary", {})
        filled = summary.get("zones_filled", 0)
        total = summary.get("zones", 0)
        area = summary.get("copper_area_mm2", 0.0)
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "revision": resp.get("revision"),
            "zones": total, "zones_filled": filled,
            "polygons": summary.get("polygons", 0),
            "copper_area_mm2": area,
            "warnings": list(resp.get("warnings", [])),
            "hint": (f"Filled {filled}/{total} pour(s): {area} mm^2 of copper across "
                     f"{summary.get('polygons', 0)} polygon(s). This is the copper Gerber exports."),
            "next_actions": ["ecad_run_drc()  # check the pour respects clearance",
                             "ecad_export('gerber')  # the fill is baked into the copper layers"],
        })

    # ---------------------------------------------------------------- parameter sweep

    @mcp.tool()
    def ecad_sweep(param: str, values: Optional[list] = None,
                   analysis: str = "op", probe: str = "", doc_id: str = "") -> str:
        """Parameter sweep: re-simulate the active schematic once per value of ``param`` and report
        how the output moves (a SPICE ``.step``).

        ``param`` is a device name (e.g. 'R1', 'V1' — sets its value) or a circuit .param. ``values``
        is the list to sweep (e.g. [1000, 2000, 5000]); start/stop/step is not exposed here — pass an
        explicit list. ``analysis`` is 'op'/'dc'/'tran'/'ac' (default 'op'). ``probe`` picks the
        output trace to summarize (default: the first non-ground node voltage). Returns the swept
        trend in plain English ('V(OUT) rises 2.5 V -> 4.0 V as R1 goes 1k -> 5k').
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Sweeps run on a schematic.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        if not values or not isinstance(values, list):
            return _err("ecad_sweep needs a non-empty 'values' list",
                        hint="Pass the values to sweep, e.g. ecad_sweep('R1', [1000, 2000, 5000]).",
                        next_actions=["ecad_sweep('R1', [1000, 2000, 5000])"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.sim.spice import netlist_to_spice_circuit
            from engine.sim.circuit import simulate as sim_run
        except Exception as e:  # noqa: BLE001
            return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                        hint="The sim engine (numpy/scipy) is not importable here.")
        circuit, warns = netlist_to_spice_circuit(nl)
        if not circuit.get("devices"):
            return _err("nothing to sweep — the netlist has no simulatable devices",
                        hint="Place R/C/L/V/I/D parts with VALUES and wire them up first.",
                        next_actions=["ecad_status()"], warnings=warns)

        nodes = [n for n in circuit.get("nodes", []) if str(n) not in ("0", "gnd", "GND")]
        probe_list = [probe] if probe else [f"V({n})" for n in nodes]
        an = _build_analysis(analysis, {}, circuit)
        if an is None:
            return _err(f"unknown analysis {analysis!r}",
                        hint="Use one of: 'op', 'dc', 'tran', 'ac'.",
                        next_actions=["ecad_sweep('R1', [1000, 2000])"])
        fvals = [float(v) for v in values]
        study = {"analyses": [an], "probes": probe_list,
                 "step": {"param": param, "values": fvals}}
        result = sim_run(circuit, study)
        step = result.get("step", {})
        runs = step.get("runs", [])
        trend, sweep_table, chosen = _summarize_sweep_steps(runs, an["type"], probe or None)
        converged = bool(result.get("convergence", {}).get("step", False))
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "analysis": an["type"],
            "param": param, "values": fvals, "converged": converged,
            "probe": chosen, "sweep": sweep_table,
            "trend": trend,
            "warnings": warns + ([] if converged else
                                 ["one or more sweep points did not converge"]),
            "hint": (f"Swept {param} over {len(fvals)} value(s): {trend}" if trend else
                     f"Swept {param} but found no finite {chosen or 'probe'} value — "
                     "check connectivity / values (ecad_run_erc)."),
            "next_actions": ["ecad_simulate('op')  # inspect one operating point in detail",
                             "ecad_status()"],
        })

    # ---------------------------------------------------------------- export

    @mcp.tool()
    def ecad_export(kind: str, path: str = "", doc_id: str = "") -> str:
        """Export a fabrication output from the active board.

        ``kind`` is one of: 'gerber' (copper/mask/silk layer set), 'drill' (Excellon PTH/NPTH),
        'bom' (CSV bill of materials), 'placement' (pick-and-place CSV). ``path`` is the output
        directory (gerber/drill) or file (bom/placement); defaults under the AI-CAD cache. Returns
        the written path(s).
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc
        kind = (kind or "").lower()
        try:
            from engine.ecad import fab
        except Exception as e:  # noqa: BLE001
            return _err(f"fab writers unavailable ({type(e).__name__}: {e})",
                        hint="The engine.ecad.fab package failed to import.")
        out_dir = path or str(_default_out_dir(doc.get("name", "board")))
        try:
            if kind == "gerber":
                os.makedirs(out_dir, exist_ok=True)
                files = fab.write_gerbers(doc, out_dir)
                result = {"kind": "gerber", "dir": out_dir, "files": files}
            elif kind == "drill":
                os.makedirs(out_dir, exist_ok=True)
                files = fab.write_drill(doc, out_dir)
                result = {"kind": "drill", "dir": out_dir, "files": files}
            elif kind == "bom":
                text = fab.write_bom(doc, fmt="csv")
                fp = path or os.path.join(out_dir, "bom.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                # M5: text.count("\n") includes the header line; subtract 1 to report
                # only DATA rows (the meaningful count for a 5-component board = 5, not 6).
                result = {"kind": "bom", "path": fp,
                          "rows": max(0, text.count("\n") - 1)}
            elif kind == "placement":
                text = fab.write_placement(doc)
                fp = path or os.path.join(out_dir, "placement.csv")
                os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                # M5: same header-line subtraction for placement CSV
                result = {"kind": "placement", "path": fp,
                          "rows": max(0, text.count("\n") - 1)}
            else:
                return _err(f"unknown export kind {kind!r}",
                            hint="Use 'gerber', 'drill', 'bom', or 'placement'.",
                            next_actions=["ecad_export('gerber')"])
        except Exception as e:  # noqa: BLE001
            return _err(f"export {kind} failed: {type(e).__name__}: {e}",
                        hint="Check ecad_run_drc() and that the board has footprints/copper.")
        _ECAD["out_dir"] = out_dir
        result.update({"ok": True, "doc_id": session.doc_id, "warnings": [],
                       "hint": f"Wrote {kind} to {result.get('path') or result.get('dir')}.",
                       "next_actions": ["ecad_export('drill')  # the other fab outputs",
                                        "ecad_export('bom')"]})
        return _dumps(result)

    @mcp.tool()
    def ecad_to_3d(path: str = "", doc_id: str = "") -> str:
        """Export a 3D model (STEP) of the active board + the aicad.boardphys/1 sidecar.

        Calls ``engine.ecad.threed.board_to_step``: assembles the board substrate and a
        component BODY for every on-board footprint (real model_3d STEP > library package
        box/cylinder from the eng metadata > pad-extent proxy box) and writes a STEP file.
        Also writes ``<step>.boardphys.json`` — the machine-readable ``aicad.boardphys/1``
        sidecar mapping each component solid to its power/thermal metadata (power_w typ/max,
        theta_ja/jc, Tj/Ta limits, supply rails) with board power totals. Missing metadata is
        honest ``null``, never 0 (contracts/ecad/boardphys.md). Downstream consumers (thermal
        handoff, PDN, power budget, host-board rules) read the sidecar.

        Returns the STEP path, sidecar path, volume summary + power totals, and a hint telling
        the user to open the STEP as an import feature in the AI-CAD app so they can design an
        enclosure around the board.

        Falls back to a clean, actionable error (never raises) when the board has no outline or
        when there is no active PCB session.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="3D export needs an open board.",
                        next_actions=["ecad_new_pcb('myboard')"])
        doc = session.doc

        # Build the output path (default: AI-CAD cache dir named after the board).
        out_path = path or str(_default_out_dir(doc.get("name", "board")) / "board.step")

        # The FIRST ecad_to_3d call cold-imports OCP/build123d (OpenCASCADE Python bindings) — that
        # import alone can take 10-30 s in a freshly-warmed server, which on its own overruns the
        # client transport timeout and gets the server KILLED before any STEP is written. Every
        # OTHER heavy ECAD tool (simulate/make_board/autoroute/fill/dfm) is budget-guarded; this one
        # was not. Run the import + geometry build on the bounded worker and autosave first, so an
        # overrun returns a clean "still running" message instead of a -32001 server kill.
        _autosave_all_open_ecad()

        def _build():
            from engine.ecad.threed import board_to_step  # lazy import; pulls in OCP/build123d
            return board_to_step(doc, out_path)

        bstatus, bval = _run_with_budget(_build)
        if bstatus == "timeout":
            return _budget_timeout_err(
                "3D STEP export",
                simplify="The 3D exporter is still loading/building (the first call cold-loads the "
                         "OCP/build123d CAD kernel). Retry ecad_to_3d() — the kernel is warm now — "
                         "or use ecad_export('gerber') for fab output.",
                next_actions=["ecad_to_3d()  # retry; the CAD kernel is now loaded",
                              "ecad_export('gerber')"],
                doc_id=session.doc_id)
        if bstatus == "error":
            e = bval
            if isinstance(e, ImportError):
                return _err(f"3D exporter unavailable (ImportError: {e})",
                            hint="engine.ecad.threed could not be imported — "
                                 "OCP/build123d may not be installed in this environment. "
                                 "Use ecad_export('gerber') for fab output.",
                            next_actions=["ecad_export('gerber')", "ecad_export('drill')"],
                            available=False)
            return _err(f"3D export failed: {type(e).__name__}: {e}",
                        hint="Check that the board has a closed Edge.Cuts outline "
                             "(pcb_set_outline op, closed=True). "
                             "An empty outline or open chain raises BoardGeometryError.",
                        next_actions=["ecad_run_drc()  # confirms outline is present",
                                      "ecad_status()"])
        result = bval

        step_path = result.get("step_path", out_path)
        summary   = result.get("summary", {})
        _ECAD["out_dir"] = os.path.dirname(step_path)
        # T-FIN8: persist the aicad.boardphys/1 sidecar next to the STEP.
        boardphys = result.get("boardphys")
        sidecar_path = None
        sidecar_warnings: list = []
        if boardphys:
            sidecar_path = step_path + ".boardphys.json"
            try:
                with open(sidecar_path, "w", encoding="utf-8") as fh:
                    json.dump(boardphys, fh, indent=1)
            except Exception as e:  # noqa: BLE001 — sidecar failure must not void the STEP
                sidecar_warnings.append(f"boardphys sidecar write failed: {e}")
                sidecar_path = None
        totals = (boardphys or {}).get("totals") or {}
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "kind": "step",
            "step_path": step_path,
            "boardphys_path": sidecar_path,
            "power": totals or None,
            "board_volume_mm3": summary.get("board_volume", 0.0),
            "n_components": summary.get("n_components", 0),
            "n_proxies": summary.get("n_proxies", 0),
            "summary": summary,
            "warnings": list(summary.get("warnings", [])) + sidecar_warnings,
            "hint": (f"Wrote STEP to {step_path} "
                     f"(board volume {summary.get('board_volume', 0):.1f} mm³, "
                     f"{summary.get('n_components', 0)} component body/bodies)"
                     + (f" + boardphys sidecar {sidecar_path} (power/thermal map)."
                        if sidecar_path else ".")
                     + " Open this STEP in the CAD app as an import feature to design "
                       "the enclosure."),
            "next_actions": [
                f"# In the CAD app: File > Import > {step_path}",
                "# Or drag the STEP onto the 3D canvas to start the enclosure design.",
            ],
        })

    # ---------------------------------------------------------------- autonomous design

    @mcp.tool()
    def ecad_design(request: str) -> str:
        """Autonomous ECAD design: natural-language request -> verified schematic + simulation.

        Runs the full deterministic orchestrator pipeline (PLAN -> BUILD -> ERC -> SIMULATE ->
        REPORT) and returns a summary with the doc_id, stage results, ERC counts, and sim
        pass/fail. With a provider configured, the model proposes the topology; without one (or
        if the provider is offline/unavailable) the closest built-in recipe is used so the
        pipeline ALWAYS produces a verified schematic.

        After this call the produced schematic becomes the active one -- ecad_status() and
        ecad_simulate() operate on it immediately.

        ``request`` is a plain-English design description, e.g.
          '5V voltage divider with 2.5V output'
          'LED driver for a red LED from a 3.3V supply'
          'inverting op-amp with gain 10'
        """
        # Lazy-import so a missing scipy/numpy degrades gracefully.
        try:
            from engine.ecad import orchestrator as _orch  # type: ignore
        except ImportError as exc:
            return _err(f"orchestrator unavailable ({type(exc).__name__}: {exc})",
                        hint="engine.ecad.orchestrator could not be imported "
                             "(numpy/scipy may be missing). "
                             "Build the schematic manually with ecad_place_symbol + ecad_wire.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_find_part('resistor')"])

        # Resolve the configured provider (best-effort; fall back to None -> recipe path).
        provider = None
        try:
            from agent import config as _cfg  # type: ignore
            provider = _cfg.get_provider()
        except Exception:  # noqa: BLE001 -- offline / unconfigured / module absent
            pass

        # Fast pre-flight: if the provider points at a LOCAL model server that is down (the most
        # common case — LM Studio/llama.cpp not started), a real provider.complete() would burn
        # ~14 s on connect-retries and overrun the MCP deadline before falling back. A sub-second
        # socket probe lets us skip straight to the instant recipe path instead.
        provider_unreachable = False
        if provider is not None and not _provider_reachable(provider):
            provider_unreachable = True
            provider = None

        # The model-planning stage can stall on a slow/loading local server (LM Studio,
        # llama.cpp) well past the MCP request budget — and ecad_design must ALWAYS return a
        # verified schematic. Run the provider-backed plan in a worker thread with a hard
        # wall-clock deadline; if it overruns, fall back to the deterministic recipe path
        # (provider=None) which is instant (~2 s). The abandoned thread's finite provider timeout
        # lets it die on its own rather than lingering.
        #
        # BUDGET: the provider plan + the ~2 s recipe fallback must finish UNDER the client's MCP
        # tool timeout, which is shorter than the 45 s heavy-solve budget on some clients (a 22 s
        # plan + recipe overran one and tripped -32001, returning nothing). Default to a
        # conservative 12 s so the worst case (plan times out -> recipe) stays ~14 s; override with
        # AI_CAD_ECAD_PLAN_BUDGET_S where the client allows a longer call.
        import threading

        try:
            _PLAN_BUDGET_S = float(os.environ.get("AI_CAD_ECAD_PLAN_BUDGET_S", "") or 12.0)
        except (TypeError, ValueError):
            _PLAN_BUDGET_S = 12.0
        if not (_PLAN_BUDGET_S > 0.0):
            _PLAN_BUDGET_S = 12.0
        box: dict = {}

        def _worker():
            try:
                box["run"] = _orch.run_design(request, provider=provider)
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        # Only ONE provider plan runs at a time (Bug E4): if another ecad_design already holds the
        # provider, this call takes the instant recipe path rather than queueing behind it on a
        # single-threaded local model server (which would overrun the MCP deadline). The flag is
        # claimed without blocking and always released in finally.
        provider_busy = False
        if provider is not None:
            with _PROVIDER_PLAN_LOCK:
                if _PROVIDER_PLAN_BUSY["v"]:
                    provider_busy = True
                else:
                    _PROVIDER_PLAN_BUSY["v"] = True

        provider_timed_out = False
        if provider is not None and not provider_busy:
            th = threading.Thread(target=_worker, daemon=True)
            th.start()
            th.join(_PLAN_BUDGET_S)
            if th.is_alive():
                provider_timed_out = True
            with _PROVIDER_PLAN_LOCK:
                _PROVIDER_PLAN_BUSY["v"] = False
        elif provider is None:
            _worker()
        # provider_busy: leave box empty -> recipe fallback below (instant).

        run = box.get("run")
        if run is None:
            # Provider path overran or errored — use the deterministic recipe fallback.
            try:
                run = _orch.run_design(request, provider=None)
            except Exception as exc:  # noqa: BLE001
                return _err(f"ecad_design failed: {type(exc).__name__}: {exc}",
                            hint="Check that engine.ecad.orchestrator and its deps are available.",
                            next_actions=["ecad_status()"])

        doc_id: str | None = run.doc_id

        # Register the produced session as the active schematic so downstream tools work.
        if doc_id is not None:
            _ECAD["current_sch"] = doc_id
            _ECAD["current"] = doc_id

        # Summarise stages concisely.  run.stages is a list[dict] (asdict was called internally).
        stages_raw = run.stages  # type: ignore[attr-defined]
        stage_names = [
            (s.get("stage") if isinstance(s, dict) else getattr(s, "stage", "?"))
            for s in stages_raw
        ]
        erc = run.erc  # type: ignore[attr-defined]
        sim = run.sim  # type: ignore[attr-defined]
        recipe = run.recipe_used  # type: ignore[attr-defined]
        stop = run.stop_reason  # type: ignore[attr-defined]
        ok = bool(run.ok)  # type: ignore[attr-defined]

        # Build a human summary of the sim result.
        sim_line = ""
        if sim:
            probe = sim.get("probe", "")
            got = sim.get("got")
            exp = sim.get("expect")
            passed = sim.get("pass", False)
            if got is not None:
                sim_line = (f"{probe}={got:.4g} (expect {exp}, "
                            f"{'PASS' if passed else 'MISS'})")
            elif sim.get("log"):
                sim_line = sim["log"][-1] if sim["log"] else ""

        hint = ((f"The configured model server did not plan within {_PLAN_BUDGET_S:.0f}s — used "
                 "the built-in recipe instead (it is loading/slow; the schematic is still verified). "
                 if provider_timed_out else "")
                + ("The configured model server is not reachable — used the built-in recipe "
                   "instead (the schematic is still verified). "
                   if provider_unreachable else "")
                + f"Design '{request}' produced with stop_reason={stop!r}. "
                + (f"Recipe fallback used: {recipe!r}. " if recipe else "")
                + (f"ERC: {erc.get('errors', 0)} error(s), "
                   f"{erc.get('warnings', 0)} warning(s). " if erc else "")
                + (f"Sim: {sim_line}." if sim_line else "")
                + (" Use ecad_status() to inspect the schematic."
                   if doc_id else " No document was produced."))

        next_actions: list[str] = []
        if doc_id:
            next_actions.append("ecad_status()  # inspect the generated schematic")
            next_actions.append("ecad_simulate('op')  # re-run simulation or change params")
        if erc and erc.get("errors", 0) > 0:
            next_actions.insert(0,
                "ecad_run_erc()  # investigate the ERC errors in the generated schematic")
        if not next_actions:
            next_actions = ["ecad_status()", "ecad_simulate('op')"]

        return _dumps({
            "ok": ok,
            "doc_id": doc_id,
            "stop_reason": stop,
            "recipe_used": recipe,
            "stages": stage_names,
            "ops_applied": run.ops_applied,  # type: ignore[attr-defined]
            "erc": erc,
            "sim": sim,
            "warnings": [],
            "hint": hint,
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- signal-integrity

    @mcp.tool()
    def ecad_check_impedance(net_or_ref: str = "", target: float = 50.0,
                             doc_id: str = "") -> str:
        """Compute the characteristic impedance (Z0) and one-way propagation delay for a routed
        trace on the active board, and compare it to a target impedance.

        ``net_or_ref`` may be a net name ('CLK', 'USB_D+') or a track id ('t3'). If omitted,
        the tool scans ALL routed nets and summarises which ones are likely controlled-impedance
        candidates (width >= 0.1 mm on an outer layer, or any inner layer).

        Returns a plain-English reading like:
          "Net CLK on F.Cu: Z0=52.1 ohm (target 50.0, +4.2%) -- within 10% tolerance;
           delay=6.7 ps/mm, length=23.4 mm, one-way delay=157 ps"
        plus ``next_actions`` to widen/narrow the trace if the target is missed.

        Impedance model: outer copper -> IPC-2141A microstrip; inner copper -> stripline.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Impedance check needs a board with routed tracks.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_autoroute()"])
        doc = session.doc
        tracks = doc.get("tracks", [])
        if not tracks:
            return _err("no routed tracks on this board",
                        hint="Route the board first (ecad_autoroute or ecad_update_from_sch).",
                        next_actions=["ecad_autoroute()", "ecad_update_from_sch()"])

        try:
            from engine.ecad.impedance import track_impedance, _stackup_geometry  # lazy import
        except ImportError as e:  # noqa: BLE001
            return _err(f"impedance engine unavailable ({e})",
                        hint="engine.ecad.impedance could not be imported.",
                        next_actions=["ecad_run_drc()"])

        tol_pct = 10.0  # +/-10% is the standard controlled-impedance fab tolerance

        def _pct(z0: float) -> float:
            return 100.0 * (z0 - target) / target if target else 0.0

        def _reading(r: dict) -> str:
            pct = _pct(r["z0"])
            status = ("within tolerance" if abs(pct) <= tol_pct else
                      "OUT OF TOLERANCE -- widen trace to lower Z0, or narrow to raise Z0")
            return (f"Net {r.get('net') or '?'} on {r.get('layer')}: "
                    f"Z0={r['z0']:.1f} ohm (target {target:.1f}, {pct:+.1f}%) -- {status}; "
                    f"delay={r['delay_ps_per_mm']:.2f} ps/mm, "
                    f"length={r['length_mm']:.2f} mm, "
                    f"one-way delay={r['delay_ps']:.1f} ps "
                    f"[{r['kind']}, w={r['width']:.3f} mm, h={r['h']:.3f} mm, er={r['er']:.2f}]")

        if net_or_ref:
            try:
                r = track_impedance(doc, net_or_ref)
            except Exception as e:  # noqa: BLE001
                nets_on_board = sorted({tr.get("net", "") for tr in tracks if tr.get("net")})
                return _err(f"could not compute impedance for {net_or_ref!r}: {e}",
                            hint=f"Routed nets on this board: {', '.join(nets_on_board[:10])}. "
                                 "Pass a net name or a track id (from ecad_status).",
                            next_actions=["ecad_status()"])
            pct = _pct(r["z0"])
            reading = _reading(r)
            within = abs(pct) <= tol_pct
            # Guidance: wider -> lower Z0; narrower -> higher Z0.
            if not within:
                if pct > 0:  # Z0 too high -> widen
                    adj = "Widen the trace (increase width) to lower Z0 toward the target."
                else:  # Z0 too low -> narrow
                    adj = "Narrow the trace (decrease width) to raise Z0 toward the target."
            else:
                adj = "Trace impedance is within the standard +/-10% controlled-impedance tolerance."
            return _dumps({
                "ok": True, "doc_id": session.doc_id,
                "net": r.get("net"), "layer": r.get("layer"),
                "z0": round(r["z0"], 2), "target": target,
                "pct_error": round(pct, 2), "within_tolerance": within,
                "kind": r["kind"], "model": r["model"],
                "width": r["width"], "h": r["h"], "er": r["er"], "t": r["t"],
                "length_mm": round(r["length_mm"], 3),
                "delay_ps_per_mm": round(r["delay_ps_per_mm"], 3),
                "delay_ps": round(r["delay_ps"], 2),
                "eeff": round(r["eeff"], 4),
                "warnings": ([] if within else [f"Z0 {pct:+.1f}% from target -- trace needs resizing"]),
                "reading": reading,
                "hint": reading + " " + adj,
                "next_actions": ([adj] if not within else
                                 ["ecad_export('gerber')  # impedance OK -- proceed to fab"]),
            })

        # No net/ref given: summarise every net's impedance as a quick SI health check.
        seen_nets: set = set()
        summaries: list[dict] = []
        for tr in tracks:
            net = tr.get("net", "")
            if net in seen_nets or not net:
                continue
            seen_nets.add(net)
            try:
                r = track_impedance(doc, tr)
                pct = _pct(r["z0"])
                summaries.append({
                    "net": net, "layer": r["layer"], "z0": round(r["z0"], 1),
                    "pct_error": round(pct, 1), "length_mm": round(r["length_mm"], 2),
                    "delay_ps": round(r["delay_ps"], 1),
                    "within_tolerance": abs(pct) <= tol_pct,
                })
            except Exception:  # noqa: BLE001
                pass
        out_of_tol = [s for s in summaries if not s["within_tolerance"]]
        worst = (sorted(out_of_tol, key=lambda s: abs(s["pct_error"]), reverse=True)[:3]
                 if out_of_tol else [])
        head = (f"{len(summaries)} net(s) analysed; "
                f"{len(out_of_tol)} out of +/-{tol_pct:.0f}% target={target:.0f} ohm tolerance.")
        return _dumps({
            "ok": True, "doc_id": session.doc_id, "target": target,
            "nets_analysed": len(summaries), "out_of_tolerance": len(out_of_tol),
            "summaries": summaries,
            "worst": worst,
            "warnings": ([f"{w['net']}: Z0={w['z0']} ohm ({w['pct_error']:+.1f}%)" for w in worst]
                         if worst else []),
            "reading": head,
            "hint": (head + (" Fix the worst offenders: "
                             + ", ".join(w["net"] for w in worst)) if worst else
                     head + " All controlled-impedance traces are within tolerance."),
            "next_actions": (
                [f"ecad_check_impedance('{worst[0]['net']}', target={target})  # inspect worst offender"]
                if worst else
                ["ecad_export('gerber')  # SI check passed"]
            ),
        })

    # ---------------------------------------------------------------- differential impedance

    @mcp.tool()
    def ecad_check_diff_impedance(net_p: str, net_n: str, target: float = 100.0,
                                  doc_id: str = "") -> str:
        """Compute the differential impedance (Z_diff) of an edge-coupled differential pair on the
        active board and compare it to a target (default 100 ohm, the common PCIe/USB3 spec).

        ``net_p`` / ``net_n`` are the positive and negative net names of the pair (e.g. 'CLK_P',
        'CLK_N' or 'USB3_TX+', 'USB3_TX-'). Both must have routed tracks on the board.

        Uses the IPC-2141A edge-coupled coupling formula:
          Z_diff = 2 * Z0_se * (1 - 0.48*exp(-0.96*s/h))   [microstrip]
          Z_diff = 2 * Z0_se * (1 - 0.347*exp(-2.9*s/h))   [stripline]
        where ``s`` is the intra-pair edge gap (inferred from the tracks' centre-to-centre distance
        minus both widths) and ``h`` is the dielectric height from the stackup.

        Returns a plain-English reading like:
          "Pair CLK_P/CLK_N: Z_diff=98.2 ohm (target 100, -1.8%) -- within 10% tolerance;
           Z0_se=52.1 ohm, gap=0.20mm, delay=6.7 ps/mm, length=23.4mm, one-way delay=157ps"
        plus next_actions to widen/narrow/gap the traces.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Differential impedance needs a board with routed tracks.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_autoroute()"])
        doc = session.doc
        tracks = doc.get("tracks", [])
        if not tracks:
            return _err("no routed tracks on this board",
                        hint="Route the board first (ecad_autoroute).",
                        next_actions=["ecad_autoroute()"])

        try:
            from engine.ecad.impedance import (  # lazy import
                track_impedance, _stackup_geometry, _track_length_mm, diff_pair_z0,
            )
        except ImportError as e:  # noqa: BLE001
            return _err(f"impedance engine unavailable ({e})",
                        hint="engine.ecad.impedance could not be imported.",
                        next_actions=["ecad_run_drc()"])

        # Locate routed tracks for each net.
        def _first_track(net: str) -> dict | None:
            return next((t for t in tracks if t.get("net") == net), None)

        tr_p = _first_track(net_p)
        tr_n = _first_track(net_n)
        routed_nets = sorted({t.get("net", "") for t in tracks if t.get("net")})

        if tr_p is None or tr_n is None:
            missing = ([net_p] if tr_p is None else []) + ([net_n] if tr_n is None else [])
            return _err(
                f"nets {', '.join(missing)} are not routed on this board",
                hint=f"Routed nets: {', '.join(routed_nets[:10])}. "
                     "Run ecad_autoroute with diff_pairs=[['{net_p}','{net_n}']] first.",
                next_actions=[f"ecad_autoroute(diff_pairs=[['{net_p}','{net_n}']])"],
                routed_nets=routed_nets)

        # Use the positive net's first track for geometry (width, layer).
        width_p = float(tr_p.get("width", 0.25))
        width_n = float(tr_n.get("width", width_p))
        layer = tr_p.get("layer", "F.Cu")

        # Infer intra-pair gap: centre-to-centre of the two tracks minus half-widths of each.
        # If tracks are on the same horizontal or vertical run we estimate from the two p1s.
        import math as _math
        p1_p = tr_p.get("p1", [0.0, 0.0])
        p1_n = tr_n.get("p1", [0.0, 0.0])
        centre_dist = _math.hypot(float(p1_p[0]) - float(p1_n[0]),
                                  float(p1_p[1]) - float(p1_n[1]))
        # Clamp gap to something physical (0.05mm minimum; fall back to DEFAULT_DIFF_GAP=0.2)
        gap = max(centre_dist - width_p / 2.0 - width_n / 2.0, 0.05)
        if centre_dist < 0.01:
            gap = 0.2  # overlapping start points -- use default gap

        geo = _stackup_geometry(doc, layer)
        h, t_cu, er = geo["h"], geo["t"], geo["er"]
        kind = geo["kind"]  # 'microstrip' or 'stripline'

        try:
            dz = diff_pair_z0(w=width_p, s=gap, h=h, t=t_cu, er=er, kind=kind)
        except Exception as e:  # noqa: BLE001
            return _err(f"diff_pair_z0 failed: {e}",
                        hint="Check that width/gap/h are positive and er >= 1.",
                        next_actions=["ecad_status()"])

        z_diff = dz["z0_diff"]
        z0_se = dz["z0_se"]
        z0_odd = dz["z0_odd"]
        delay_ps_per_mm = dz["delay_ps_per_mm"]
        model = dz["model"]

        length_p = _track_length_mm(doc, net_p, layer)
        length_n = _track_length_mm(doc, net_n, layer)
        length_mm = (length_p + length_n) / 2.0  # average pair length
        skew_mm = abs(length_p - length_n)
        delay_ps = delay_ps_per_mm * length_mm

        tol_pct = 10.0
        pct = 100.0 * (z_diff - target) / target if target else 0.0
        within = abs(pct) <= tol_pct

        status = "within tolerance" if within else "OUT OF TOLERANCE"
        reading = (f"Pair {net_p}/{net_n}: Z_diff={z_diff:.1f} ohm "
                   f"(target {target:.0f}, {pct:+.1f}%) -- {status}; "
                   f"Z0_se={z0_se:.1f} ohm, Z0_odd={z0_odd:.1f} ohm, "
                   f"gap={gap:.3f}mm, delay={delay_ps_per_mm:.2f}ps/mm, "
                   f"length={length_mm:.2f}mm (skew={skew_mm:.3f}mm), "
                   f"one-way delay={delay_ps:.1f}ps [{kind}, w={width_p:.3f}mm, h={h:.3f}mm, "
                   f"er={er:.2f}] model={model}")

        # Guidance: Z_diff too high -> widen traces or reduce gap; too low -> narrow / widen gap.
        if not within:
            if pct > 0:  # Z_diff above target
                adj = ("Widen traces (increase width) or reduce intra-pair gap to lower Z_diff. "
                       "Current gap={:.3f}mm; try gap={:.3f}mm.".format(gap, gap * 0.8))
            else:  # Z_diff below target
                adj = ("Narrow traces or increase intra-pair gap to raise Z_diff. "
                       "Current gap={:.3f}mm; try gap={:.3f}mm.".format(gap, gap * 1.2))
        else:
            adj = ("Differential impedance is within the standard +/-10% tolerance. "
                   f"Skew={skew_mm:.3f}mm" +
                   (" -- consider length matching if skew > 0.2mm." if skew_mm > 0.2 else "."))

        next_actions: list[str] = [adj]
        if skew_mm > 0.2:
            next_actions.append(
                f"Length skew {skew_mm:.3f}mm exceeds 0.2mm: re-route with "
                f"ecad_autoroute(diff_pairs=[['{net_p}','{net_n}']]) for automatic "
                "length-matching meanders.")
        if within:
            next_actions.append("ecad_export('gerber')  # SI check passed -- proceed to fab")

        return _dumps({
            "ok": True, "doc_id": session.doc_id,
            "net_p": net_p, "net_n": net_n,
            "z_diff": round(z_diff, 2), "z0_se": round(z0_se, 2), "z0_odd": round(z0_odd, 2),
            "target": target, "pct_error": round(pct, 2), "within_tolerance": within,
            "gap_mm": round(gap, 4), "width_mm": round(width_p, 4),
            "h": round(h, 4), "er": round(er, 2), "t": round(t_cu, 4),
            "kind": kind, "model": model,
            "length_mm": round(length_mm, 3),
            "length_p_mm": round(length_p, 3), "length_n_mm": round(length_n, 3),
            "skew_mm": round(skew_mm, 4),
            "delay_ps_per_mm": round(delay_ps_per_mm, 3),
            "delay_ps": round(delay_ps, 2),
            "eeff": round(dz["eeff"], 4),
            "warnings": ([] if within else
                         [f"Z_diff {pct:+.1f}% from target -- pair needs retuning"])
                      + ([f"skew {skew_mm:.3f}mm > 0.2mm"] if skew_mm > 0.2 else []),
            "reading": reading,
            "hint": reading + " " + adj,
            "next_actions": next_actions,
        })

    # ---------------------------------------------------------------- .tf transfer function

    @mcp.tool()
    def ecad_tf(output: str, source: str, doc_id: str = "") -> str:
        """Run a DC small-signal transfer function (.tf) on the active schematic's circuit.

        Computes:
          * DC gain  -- output / source (V/V for V-source input, V/A transimpedance for I-source)
          * Rin      -- resistance seen looking into the source terminals
          * Rout     -- resistance seen looking into the output terminals (all sources nulled)

        ``output`` is a voltage probe: 'V(OUT)', 'V(out,in)', etc.
        ``source`` is the name of an independent V or I source: 'V1', 'I1', etc.

        Reading example: "DC gain V(OUT)/V1 = -10.00, Rin = 1.00 kohm, Rout = 100 ohm"
        This is exactly the SPICE .tf analysis, useful for amplifier gain/loading checks.
        """
        try:
            session = _get_sch(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="Transfer function needs an open schematic.",
                        next_actions=["ecad_new_schematic('mydesign')"])
        doc = session.doc
        nl = netlist_mod.build_netlist(doc)
        try:
            from engine.sim.spice import netlist_to_spice_circuit
            from engine.sim.circuit import simulate as sim_run
        except Exception as e:  # noqa: BLE001
            return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                        hint="The sim engine (numpy/scipy) is not importable here.")
        circuit, warns = netlist_to_spice_circuit(nl)
        if not circuit.get("devices"):
            return _err("nothing to analyse -- netlist has no simulatable devices",
                        hint="Place R/C/V/I/op-amp parts with VALUES and wire them first.",
                        next_actions=["ecad_find_part('voltage')", "ecad_status()"],
                        warnings=warns)

        study = {"analyses": [{"type": "tf", "output": output, "source": source}],
                 "probes": [output]}
        result = sim_run(circuit, study)
        tf = result.get("tf", {})
        conv = bool(result.get("convergence", {}).get("tf", False))
        gain = tf.get("gain", float("nan"))
        rin = tf.get("rin", float("nan"))
        rout = tf.get("rout", float("nan"))

        def _ohm(v: float) -> str:
            if not _finite(v):
                return "n/a"
            a = abs(v)
            if a >= 1e6:
                return f"{v/1e6:.3g} Mohm"
            if a >= 1e3:
                return f"{v/1e3:.3g} kohm"
            return f"{v:.3g} ohm"

        gain_str = f"{gain:.4g}" if _finite(gain) else "n/a"
        reading = (f"DC gain {output}/{source} = {gain_str}, "
                   f"Rin = {_ohm(rin)}, Rout = {_ohm(rout)}")
        hint = reading
        if not conv:
            hint += " -- did NOT converge (check connectivity/source names)"

        # Guidance based on results.
        next_actions: list[str] = []
        if not conv:
            next_actions += ["ecad_run_erc()  # find connectivity problems",
                             "ecad_status()  # confirm wiring"]
        else:
            next_actions.append("ecad_simulate('ac')  # see frequency response, not just DC gain")
            if _finite(rin) and rin < 100.0:
                next_actions.append(
                    f"Low Rin={_ohm(rin)} -- the source may be heavily loaded by the circuit.")
            if _finite(rout) and rout > 1e3:
                next_actions.append(
                    f"High Rout={_ohm(rout)} -- output may not drive low-impedance loads well.")

        return _dumps({
            "ok": conv, "doc_id": session.doc_id,
            "output": output, "source": source,
            "gain": _round(gain), "rin": _round(rin), "rout": _round(rout),
            "converged": conv,
            "device_count": len(circuit["devices"]),
            "warnings": warns + ([] if conv else ["tf did not converge"]),
            "reading": reading,
            "hint": hint,
            "next_actions": next_actions or ["ecad_status()"],
            "sim_log": result.get("log", [])[-4:],
        })

    # ---------------------------------------------------------------- one-shot board flow

    @mcp.tool()
    def ecad_make_board(pcb_name: str = "Board1", doc_id: str = "") -> str:
        """ONE-SHOT board flow: from the active schematic, run sync -> autoroute -> fill zones ->
        DRC and report every step's outcome, remaining issues, and what to do next.

        Steps in order:
          1. ecad_new_pcb          -- create a fresh board document
          2. ecad_update_from_sch  -- populate footprints from the schematic
          3. ecad_autoroute        -- route the ratsnest with the 2-layer router
          4. ecad_fill_zones       -- pour copper on GND (adds a zone automatically if none)
          5. ecad_run_drc          -- report remaining violations

        Sets _ECAD['current_pcb'] to the produced board so ecad_status / ecad_run_drc /
        ecad_export work immediately after.

        ``doc_id`` selects WHICH schematic to build the board from (defaults to the active
        schematic). Pass it when you have several schematics open and want a board from a specific
        one (Bug E5: this argument was previously ignored).

        Finishes with a hint: "call ecad_to_3d to bring the board into the CAD app for a housing".
        """
        sch_id = doc_id or _ECAD.get("current_sch")
        if sch_id is None:
            return _err("no active schematic -- build one first",
                        hint="Create a schematic (ecad_new_schematic + place + wire parts) before "
                             "calling ecad_make_board.",
                        next_actions=["ecad_new_schematic('mydesign')",
                                      "ecad_design('your circuit description')"])
        # Validate the requested schematic resolves and IS a schematic (a clear error beats
        # silently building an empty board from a bad/board doc_id).
        try:
            sch_sess = ecad_session.get_session(sch_id)
            if sch_sess.kind != "sch":
                return _err(f"doc '{sch_id}' is a {sch_sess.kind}, not a schematic",
                            hint="Pass the doc_id of a SCHEMATIC to build the board from.",
                            next_actions=["ecad_status()"])
        except OpError as e:
            return _err(e.message, hint="That schematic doc_id is not open.",
                        next_actions=["ecad_new_schematic('mydesign')"])

        steps: list[dict] = []

        def _step(name: str, outcome: dict, note: str = "") -> None:
            steps.append({
                "step": name,
                "ok": outcome.get("ok", False),
                "note": note or outcome.get("hint", "")[:120],
            })

        # ---- 1. new board (instant — kept OUTSIDE the budget so a timeout below can still report
        # the created board's id + partial steps for recovery) ----
        pcb_sess = ecad_session.new_session("pcb", pcb_name)
        _ECAD["current_pcb"] = pcb_sess.doc_id
        _ECAD["current"] = pcb_sess.doc_id
        _step("new_pcb", {"ok": True}, f"board '{pcb_name}' created ({pcb_sess.doc_id})")

        from engine.ecad import rpc as ecad_rpc

        # The full sync->route->fill->DRC flow is the heaviest ECAD entry point — it was the
        # original transport-timeout trigger (adb46f62 fixed the 100x100 GND-pour symptom; this
        # bounds the whole flow). Run it under ONE wall-clock budget so its TOTAL time can never
        # overrun the transport and get the server killed (per-substep budgets wouldn't bound the
        # sum). Autosave first so even a hard kill keeps the work (Gap (a)+(b)).
        _autosave_all_open_ecad()

        def _flow() -> str:
            # ---- 2. sync schematic -> board ----
            sync_ok = False
            try:
                sync_resp = ecad_rpc._ecad_update_from_sch(
                    {"board_doc_id": pcb_sess.doc_id, "sch_doc_id": sch_id}, lambda _: None)
                sync_rep = sync_resp.get("report", {})
                added = sync_rep.get("added", [])
                ratsnest = sync_resp.get("compile", {}).get("ratsnest", []) or []
                sync_ok = True
                _step("update_from_sch", {"ok": True},
                      f"{len(added)} footprint(s) placed, {len(ratsnest)} airwire(s) to route")
            except OpError as e:
                _step("update_from_sch", {"ok": False}, f"FAILED: {e.message}")
                added = []; ratsnest = []

            # ---- 3. autoroute ----
            ar_ok = False
            try:
                ar_resp = ecad_rpc._ecad_autoroute(
                    {"doc_id": pcb_sess.doc_id, "opts": {}}, lambda _: None)
                ar_rep = ar_resp.get("report", {})
                routed = ar_rep.get("routed", [])
                failed = ar_rep.get("failed", [])
                ar_ok = True
                _step("autoroute", {"ok": True},
                      f"routed {len(routed)} net(s), {len(failed)} failed"
                      + (f": {failed[:4]}" if failed else ""))
            except OpError as e:
                _step("autoroute", {"ok": False}, f"FAILED: {e.message}")
                routed = []; failed = []

            # M4: compute the true routed-net count = multi-pad nets with NO remaining airwires.
            # The autorouter's ``routed`` list only includes nets that HAD airwires and the
            # router successfully connected them; single-pad stubs and zone-only nets are never
            # in the list.  We count all board nets that are NOT in ``failed`` (unrouted) and
            # have at least 2 pads (so single-pad stubs don't inflate the "routed" figure).
            try:
                _pcb_doc_now = pcb_sess.doc
                _all_board_nets = {
                    n.get("name") for n in _pcb_doc_now.get("nets", [])
                    if isinstance(n, dict) and n.get("name")
                }
                # count pads per net
                from collections import Counter as _Counter
                _pad_net_counts: _Counter = _Counter()
                for _fp in _pcb_doc_now.get("footprints", []):
                    for _pad in _fp.get("pads", []):
                        _pn = _pad.get("net")
                        if _pn:
                            _pad_net_counts[_pn] += 1
                # a net is "multi-pad" when it has >= 2 pads (i.e. needs a connection)
                _multi_pad_nets = {n for n, c in _pad_net_counts.items() if c >= 2}
                _failed_set = set(failed)
                _nets_routed_count = len(_multi_pad_nets - _failed_set)
                _nets_failed_count = len(_multi_pad_nets & _failed_set)
            except Exception:  # noqa: BLE001
                _nets_routed_count = len(routed)
                _nets_failed_count = len(failed)

            # ---- 3.5 board outline (auto Edge.Cuts rectangle if none) ----
            # The one-shot flow must leave a closed Edge.Cuts loop: ecad_to_3d needs it to
            # build the substrate, and the GND pour wants the true board extents (Bug E-3).
            outline_ok = False
            try:
                pcb_doc = pcb_sess.doc
                if not (pcb_doc.get("outline", {}) or {}).get("shapes"):
                    shapes = _default_outline_shapes(pcb_doc)
                    _apply(pcb_sess, [{"op": "pcb_set_outline", "shapes": shapes, "closed": True}])
                    bb = _outline_bbox(pcb_sess.doc)
                    outline_ok = True
                    _step("set_outline", {"ok": True},
                          "auto Edge.Cuts rectangle "
                          + (f"{bb[2]-bb[0]:.1f} x {bb[3]-bb[1]:.1f} mm" if bb else "added"))
                else:
                    outline_ok = True
                    _step("set_outline", {"ok": True}, "outline already present — kept")
            except Exception as e:  # noqa: BLE001
                _step("set_outline", {"ok": False}, f"FAILED: {type(e).__name__}: {e}")

            # ---- 4. fill zones (add GND zone automatically if board has none) ----
            fill_ok = False
            try:
                pcb_doc = pcb_sess.doc
                if not pcb_doc.get("zones"):
                    # add a whole-board GND zone (need a bounding box for the polygon).
                    poly = _outline_bbox(pcb_doc)
                    if poly is None:
                        # No board outline: size the pour to the actual COPPER extent (placed
                        # footprints + routed tracks) plus a margin — NOT a fixed 100x100 mm. A
                        # 100x100 zone filled at 0.1 mm resolution is ~1e6 cells (~5 s); for a small
                        # board that overran the MCP transport timeout in this SYNCHRONOUS flow, the
                        # server was killed, and every ECAD doc (Python, no autosave) was lost. The
                        # tight extent fills in ~0.1 s. (engine.ecad.autoroute._board_bbox already
                        # returns the copper extent + a small pad when there is no outline.)
                        from engine.ecad import autoroute as _ar_bbox
                        bx0, by0, bx1, by1 = _ar_bbox._board_bbox(pcb_doc)
                        m = 5.0  # pour margin beyond the copper
                        poly = (bx0 - m, by0 - m, bx1 + m, by1 + m)
                    x0, y0, x1, y1 = poly
                    poly_pts = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                    _apply(pcb_sess, [{"op": "pcb_add_zone", "net": "GND", "layer": "F.Cu",
                                       "polygon": poly_pts}])
                fill_resp = ecad_rpc._ecad_fill_zones({"doc_id": pcb_sess.doc_id}, lambda _: None)
                fs = fill_resp.get("fill_summary", {})
                fill_ok = True
                _step("fill_zones", {"ok": True},
                      f"{fs.get('zones_filled', 0)} zone(s) filled, {fs.get('copper_area_mm2', 0):.1f} mm^2")
            except Exception as e:  # noqa: BLE001
                _step("fill_zones", {"ok": False}, f"FAILED: {type(e).__name__}: {e}")

            # ---- 5. DRC ----
            drc_errors = 0; drc_warnings = 0; drc_lines: list[str] = []
            try:
                from engine.ecad import drc as drc_mod
                violations = drc_mod.run_drc(pcb_sess.doc)
                if isinstance(violations, dict):
                    violations = violations.get("violations", [])
                drc_sum = _erc_summary(violations)
                drc_errors = drc_sum["errors"]
                drc_warnings = drc_sum["warnings"]
                drc_lines = _erc_lines(violations)[:6]
                _step("drc", {"ok": drc_errors == 0},
                      f"{drc_errors} error(s), {drc_warnings} warning(s)")
            except Exception as e:  # noqa: BLE001
                _step("drc", {"ok": False}, f"DRC unavailable: {e}")

            all_ok = all(s["ok"] for s in steps)
            summary_lines = [f"  {s['step']}: {'OK' if s['ok'] else 'FAIL'} -- {s['note']}"
                             for s in steps]
            reading = "\n".join(summary_lines)
            hint = (f"Board flow {'complete' if all_ok else 'partial'}: "
                    + "; ".join(f"{s['step']}={'OK' if s['ok'] else 'FAIL'}" for s in steps)
                    + f". DRC: {drc_errors} error(s). "
                    "Next: call ecad_to_3d() to bring the board into the CAD app for a housing.")

            next_actions: list[str] = []
            if failed:
                next_actions.append(
                    f"{len(failed)} net(s) not routed ({', '.join(map(str, failed[:4]))}). "
                    "Try ecad_autoroute(pitch=0.15) or move parts to give the router more room.")
            if drc_errors:
                next_actions.append(f"ecad_run_drc()  # {drc_errors} DRC error(s) to fix")
            next_actions.append("ecad_to_3d()  # export STEP and design an enclosure in the CAD app")
            if not next_actions:
                next_actions.append("ecad_export('gerber')  # board passes DRC -- ready for fab")

            return _dumps({
                "ok": all_ok, "doc_id": pcb_sess.doc_id,
                "sch_doc_id": sch_id, "pcb_name": pcb_name,
                "steps": steps, "reading": reading,
                "footprints_placed": len(added),
                # M4: nets_routed = multi-pad nets with no remaining airwires (all connected).
                # nets_autorouted = nets the router actively connected in this run (subset).
                # nets_failed = multi-pad nets that still have unrouted airwires.
                "nets_routed": _nets_routed_count,
                "nets_autorouted": len(routed),
                "nets_failed": _nets_failed_count,
                "drc_errors": drc_errors, "drc_warnings": drc_warnings,
                "drc_violations": drc_lines,
                "warnings": drc_lines[:3],
                "hint": hint,
                "next_actions": next_actions,
            })

        status, out = _run_with_budget(_flow)
        if status == "timeout":
            return _budget_timeout_err(
                "make_board flow",
                simplify="The one-shot sync->route->fill->DRC flow is too heavy for a single call. "
                         "Run the steps separately so each is individually budget-guarded: "
                         "ecad_update_from_sch(), then ecad_autoroute(pitch=0.3), then "
                         "ecad_fill_zones(), then ecad_run_drc().",
                next_actions=["ecad_autoroute(pitch=0.3)  # on the partial board",
                              "ecad_fill_zones()", "ecad_run_drc()"],
                doc_id=pcb_sess.doc_id,
                steps_completed=[s["step"] for s in steps])
        if status == "error":
            raise out
        return out

    # tools registered; nothing to return.


# --------------------------------------------------------------------------- sim helpers (module level)


def _build_analysis(analysis: str, params: dict, circuit: dict) -> dict | None:
    """Map a friendly analysis name + params dict to a sim.md §3 analysis dict, filling sane
    defaults (a weak model usually under-specifies)."""
    a = (analysis or "op").lower()
    if a == "op":
        return {"type": "op"}
    if a == "dc":
        src = params.get("source")
        if not src:  # default to the first voltage source in the circuit
            src = next((d["name"] for d in circuit.get("devices", []) if d.get("kind") == "V"),
                       "V1")
        return {"type": "dc", "source": src,
                "start": float(params.get("start", 0.0)),
                "stop": float(params.get("stop", 5.0)),
                "step": float(params.get("step", (float(params.get("stop", 5.0)) -
                                                  float(params.get("start", 0.0))) / 50 or 0.1))}
    if a == "tran":
        tstop = float(params.get("tstop", 1e-3))
        return {"type": "tran",
                "tstep": float(params.get("tstep", tstop / 200)),
                "tstop": tstop,
                "tstart": float(params.get("tstart", 0.0)),
                "uic": bool(params.get("uic", False))}
    if a == "ac":
        return {"type": "ac",
                "sweep": params.get("sweep", "dec"),
                "points": int(params.get("points", 20)),
                "fstart": float(params.get("fstart", 1.0)),
                "fstop": float(params.get("fstop", 1e6))}
    # Advanced analyses: return a spec dict with the analysis type for routing.
    if a == "four":
        # Fourier/THD: needs a preceding tran. Default tran params if not supplied.
        tstop = float(params.get("tstop", 1e-3))
        f0 = float(params.get("f0", 1e3))
        probe = params.get("probe")
        return {"type": "four", "f0": f0, "tstop": tstop,
                "tstep": float(params.get("tstep", tstop / 400)),
                "probe": probe, "nharm": int(params.get("nharm", 9))}
    if a == "mc":
        inner_name = params.get("inner", "op")
        inner_params = params.get("inner_params", {})
        inner_an = _build_analysis(inner_name, inner_params, circuit) or {"type": "op"}
        return {"type": "mc",
                "runs": int(params.get("runs", 50)),
                "analysis": inner_an,
                "tol": params.get("tol"),
                "probe": params.get("probe"),
                "seed": int(params.get("seed", 42))}
    if a == "temp":
        inner_name = params.get("inner", "op")
        inner_params = params.get("inner_params", {})
        inner_an = _build_analysis(inner_name, inner_params, circuit) or {"type": "op"}
        vals = params.get("values", [-40.0, 27.0, 85.0])
        # Carry the requested probe so the temp-sweep trend reports the node the caller asked for,
        # not just the first node in probe_list (Bug E8).
        return {"type": "temp", "values": [float(v) for v in vals], "analysis": inner_an,
                "probe": params.get("probe")}
    if a == "noise":
        return {"type": "noise",
                "output": params.get("output", "V(OUT)"),
                "source": params.get("source"),
                "fstart": float(params.get("fstart", 10.0)),
                "fstop": float(params.get("fstop", 1e6)),
                "points": int(params.get("points", 20)),
                "sweep": params.get("sweep", "dec")}
    return None


def _simulate_advanced(session, circuit: dict, an: dict, probe_list: list[str],
                       warns: list[str]) -> str:
    """Run one of the advanced analyses (four/mc/temp/noise) and return a rich payload."""
    try:
        from engine.sim.circuit import simulate as sim_run
    except Exception as e:  # noqa: BLE001
        return _err(f"simulator unavailable ({type(e).__name__}: {e})",
                    hint="The sim engine (numpy/scipy) is not importable here.")

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
        conv = bool(result.get("convergence", {}).get("four", False))
        harmonics = four.get("harmonics", [])
        thd = float(four.get("thd_pct", 0.0))
        h1 = harmonics[0][1] if harmonics else 0.0
        probe_used = four.get("probe", "?")
        dom = [f"H{n}={m:.4g} V" for n, m, _ in harmonics[:4]]
        reading = (f"{probe_used} @ f0={four.get('f0', '?'):.4g} Hz: "
                   f"H1={h1:.4g} V, THD={thd:.3f}%, "
                   f"dominant harmonics: {', '.join(dom)}; "
                   f"DC bias={four.get('dc', 0.0):.4g} V")
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "four",
            "converged": conv,
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
        yield_pct = 100.0 * sum(1 for v in mc.get("values", []) if _finite(v)) / max(mc.get("runs", 1), 1)
        return _dumps({
            "ok": conv, "doc_id": session.doc_id, "analysis": "mc",
            "converged": conv,
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
        # Summarise the requested probe across temperatures (else the first probe).
        probe_used = req_probe or (probe_list[0] if probe_list else None)
        trend_vals: list[tuple[float, float]] = []
        for pt in points_data:
            op = pt.get("op", {})
            if probe_used and op and probe_used in op:
                v = op[probe_used]
                if _finite(v):
                    trend_vals.append((pt["temp"], float(v)))
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


def _summarize_sweep_steps(runs: list, atype: str, probe: Optional[str]):
    """Compact a `.step` sweep into (trend_sentence, sweep_table, chosen_probe).

    For each swept value we pull a single scalar for the chosen probe (the op node voltage, or the
    final transient value, or the peak AC magnitude) and build a plain-English 'rises/falls/holds'
    sentence so a weak model can read the trend without parsing the raw runs."""
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
    from engine import paths
    return paths.model_dir(f"ecad_{name}")
