"""Shared session state + helpers for the ECAD MCP tool family modules (T-RF8 split of the
former ecad_tools.py god file): the _ECAD session registry, autosave/recovery, provider
budgets, symbol/pin/net helpers, ERC formatting, and the module-level sim summarizers.
Imported by the ecad_*_tools family modules; re-exported through the ecad_tools facade."""

from __future__ import annotations

import json
import math
import os
from typing import Any, Optional, Union

from mcp_server import result_serving as _rs

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
    the recovery list. Best-effort — never raises into startup.

    MONOTONICITY (critical): recovery runs on a BACKGROUND warm thread (server._warm_core), so it
    can finish AFTER a user has already called ecad_new_schematic / ecad_new_pcb / ecad_design on
    the fresh server. We fill ONLY empty current_* defaults — never overwrite a just-created or
    explicitly-selected document. The recovered sessions still land in the session registry and
    remain addressable by their explicit doc_id.
    """
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
    """Compact JSON by default (AICAD_MCP_PRETTY=1 for indent=2)."""
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
        from mcp_server.ascii2d import (  # noqa: PLC0415
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
        from mcp_server.ascii2d import (  # noqa: PLC0415
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
        an = {"type": "four", "f0": f0, "tstop": tstop,
              "tstep": float(params.get("tstep", tstop / 400)),
              "probe": probe, "nharm": int(params.get("nharm", 9))}
        # Window: the solvers' own default is "as many whole periods as fit", which
        # INCLUDES the start-up transient (a circuit starting from its op point reads
        # phantom THD — measured 2.3% on a plain linear RC) and, worse, whether the
        # first period gets included is a float-rounding lottery in int(tspan/period).
        # Analyze the LAST whole periods excluding the first (SPICE .four convention
        # is the last period only), unless the caller pins 'periods' explicitly.
        if "periods" in params:
            an["periods"] = int(params["periods"])
        else:
            whole = int(round(tstop * f0))
            if whole >= 2:
                an["periods"] = whole - 1
        return an
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
