"""Component engineering-metadata layer — resolution + normalization.

The bundled library (``library/starter.py``) carries optional ``eng`` blocks on symbol
defs (electrical: power, supply rails, junction limits) and footprint defs (physical:
package body dims, package-level theta_ja). This module is the single place that merges
those library seeds with per-instance overrides into ONE normalized dict shape, used by:

  * ``threed.board_to_step`` — component bodies + the ``aicad.boardphys/1`` sidecar,
  * MCP ``ecad_status`` / ``describe_board`` — per-component power + board totals,
  * downstream rule packs (host-board rules, PDN, thermal handoff, power budget).

Honesty contract (contracts/ecad/boardphys.md): every key is ALWAYS present; unknown
values are ``None`` (JSON ``null``), NEVER an invented 0. ``sequencing == []`` means
"known: no sequencing requirement"; ``None`` means "unknown".

Merge precedence (later wins, dicts deep-merge, scalars/lists replace):
  footprint ``eng``  <  symbol ``eng``  <  per-instance override
Per-instance overrides live in ``symbol["sim_params"]["eng"]`` on a schematic, and are
carried onto board footprints by sync as ``footprint["eng_overrides"]`` (or may be set
directly in ``footprint["fields"]["eng"]``).

No geometry-kernel imports here — this module stays cheap to import.
"""

from __future__ import annotations

import copy
from typing import Any

# ── normalized shape ───────────────────────────────────────────────────────────

_RAIL_KEYS = ("name", "voltage_v_min", "voltage_v_nom", "voltage_v_max",
              "current_a_active", "current_a_sleep")


def empty_eng() -> dict:
    """The full-null normalized engineering-metadata skeleton (all keys present)."""
    return {
        "power_w": {"typ": None, "max": None},
        "theta_c_per_w": {"ja": None, "jc": None},
        "limits_c": {"tj_max": None, "ta_max": None},
        "package": {"shape": None, "l_mm": None, "w_mm": None,
                    "h_mm": None, "d_mm": None},
        "supply": {"rails": None, "voltage_domains": None, "sequencing": None},
        "sources": [],
    }


def _deep_merge(base: dict, over: dict) -> dict:
    """Deep-merge ``over`` into ``base`` (dicts recurse; None in ``over`` is a no-op so an
    override can never *erase* known data into unknown; scalars/lists replace)."""
    out = dict(base)
    for k, v in (over or {}).items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _normalize_rails(rails: Any) -> Any:
    """Coerce a rails list so every rail dict has all rail keys (missing -> None)."""
    if not isinstance(rails, list):
        return rails if rails is None else None
    out = []
    for r in rails:
        if not isinstance(r, dict):
            continue
        out.append({k: r.get(k) for k in _RAIL_KEYS})
    return out


def normalize_eng(raw: Any) -> dict:
    """Coerce an arbitrary partial ``eng`` block into the full normalized skeleton."""
    eng = empty_eng()
    if not isinstance(raw, dict):
        return eng
    eng = _deep_merge(eng, raw)
    # Prune to the known envelope (unknown keys dropped, keeping the sidecar stable).
    skel = empty_eng()
    out: dict = {}
    for k, v in skel.items():
        if isinstance(v, dict):
            out[k] = {kk: eng.get(k, {}).get(kk) if isinstance(eng.get(k), dict) else None
                      for kk in v}
        else:
            out[k] = eng.get(k, v)
    out["supply"]["rails"] = _normalize_rails(out["supply"]["rails"])
    src = eng.get("sources")
    if isinstance(src, str):
        src = [src]
    out["sources"] = list(src) if isinstance(src, list) else []
    return out


# ── resolution ─────────────────────────────────────────────────────────────────


def _lib(library: Any):
    if library is not None:
        return library
    from services.ecad import library as lib_mod
    return lib_mod


def symbol_eng(libid: str, library: Any = None) -> dict | None:
    """Raw ``eng`` block from the resolved symbol def, or None."""
    if not libid:
        return None
    sdef = _lib(library).resolve(libid)
    return (sdef or {}).get("eng")


def footprint_eng(fpid: str, library: Any = None) -> dict | None:
    """Raw ``eng`` block from the resolved footprint def, or None."""
    if not fpid:
        return None
    fpdef = _lib(library).resolve_footprint(fpid)
    return (fpdef or {}).get("eng")


def resolve_eng(libid: str = "", fpid: str = "", overrides: Any = None,
                library: Any = None) -> dict:
    """Merged, normalized engineering metadata for one component instance.

    ``overrides`` is a partial eng dict (typically ``sim_params["eng"]``). Missing
    metadata degrades to honest ``None`` values — never invented zeros.
    """
    merged: dict = {}
    fp_eng = footprint_eng(fpid, library)
    if isinstance(fp_eng, dict):
        merged = _deep_merge(merged, fp_eng)
    sym_eng = symbol_eng(libid, library)
    if isinstance(sym_eng, dict):
        merged = _deep_merge(merged, sym_eng)
    if isinstance(overrides, dict):
        merged = _deep_merge(merged, overrides)
        merged.setdefault("sources", [])
        if overrides.get("sources") is None and merged.get("sources") is not None:
            pass  # keep library sources when the override adds none
    # sources ACCUMULATE rather than replace (provenance must never be silently lost).
    srcs: list = []
    for blk in (fp_eng, sym_eng, overrides):
        if isinstance(blk, dict):
            s = blk.get("sources")
            if isinstance(s, str):
                s = [s]
            if isinstance(s, list):
                srcs.extend(x for x in s if x not in srcs)
    merged["sources"] = srcs
    return normalize_eng(merged)


def instance_overrides(symbol: dict) -> Any:
    """The per-instance eng override block from a schematic symbol instance."""
    sp = symbol.get("sim_params") or {}
    return sp.get("eng") if isinstance(sp, dict) else None


def footprint_overrides(fp: dict) -> Any:
    """The per-instance eng override block from a board footprint instance."""
    ov = fp.get("eng_overrides")
    if isinstance(ov, dict):
        return ov
    fields = fp.get("fields") or {}
    ov = fields.get("eng") if isinstance(fields, dict) else None
    return ov if isinstance(ov, dict) else None


def eng_for_symbol(symbol: dict, library: Any = None) -> dict:
    """Normalized metadata for a placed schematic symbol instance."""
    return resolve_eng(libid=str(symbol.get("libid") or ""),
                       fpid=str(symbol.get("footprint") or ""),
                       overrides=instance_overrides(symbol), library=library)


def eng_for_footprint(fp: dict, library: Any = None) -> dict:
    """Normalized metadata for a placed board footprint instance.

    Uses the footprint's ``libid`` back-link when sync carried it, else fpid-only.
    """
    return resolve_eng(libid=str(fp.get("libid") or ""),
                       fpid=str(fp.get("fpid") or ""),
                       overrides=footprint_overrides(fp), library=library)


# ── board rollup ───────────────────────────────────────────────────────────────


def power_totals(component_engs: list[dict]) -> dict:
    """Board power rollup over normalized eng dicts.

    Totals are the sum over components WITH data; when no component has power data the
    totals are ``None`` (honest unknown), never 0. ``n_missing_power`` counts the
    components excluded from the sum so consumers can judge coverage.
    """
    typ_vals = [e["power_w"]["typ"] for e in component_engs
                if isinstance(e.get("power_w"), dict) and e["power_w"].get("typ") is not None]
    max_vals = [e["power_w"]["max"] for e in component_engs
                if isinstance(e.get("power_w"), dict) and e["power_w"].get("max") is not None]
    n = len(component_engs)
    n_with = len({id(e) for e in component_engs
                  if e.get("power_w", {}).get("typ") is not None
                  or e.get("power_w", {}).get("max") is not None})
    return {
        "power_w_typ": float(sum(typ_vals)) if typ_vals else None,
        "power_w_max": float(sum(max_vals)) if max_vals else None,
        "n_components": n,
        "n_with_power": n_with,
        "n_missing_power": n - n_with,
    }
