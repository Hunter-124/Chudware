"""Shared session + persistence helpers for the ``civil_*`` MCP tool modules.

contracts/civil_schema.md §5: one ``civil_site`` document study per civil model,
``params.model`` = the ``aicad.civil/1`` document, ``results.<analysis key>`` = the latest
analysis of THAT model. Storing a different model drops every stored result (a stale
analysis is never shown as current), mirroring ``piping_network``.

Tool modules (``civil_tools``, ``civil_hydro_tools``, ``civil_link_tools``) share one
session: :data:`SITES` maps a model name to ``{"model", "results"}`` and :data:`ACTIVE`
names the model a call without ``site=`` works on.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Callable, Optional, Union

from services.civil import schema as civil_schema
from services.civil.errors import CivilError
from services.mcp import core_bridge as cb
from services.mcp._envelopes import plain_dumps as dumps, reason_err as err
from services.mcp.cad_common import _doc, _persist_study

STUDY_TYPE = "civil_site"
ENGINE = "py-civil"
RESULT_KEYS = ("water", "fire_flow", "gravity", "hydrology", "detention", "culvert",
               "earthwork", "buried", "thrust", "supports", "qto", "cost", "schedule",
               "tie_ins")

#: Session models by name: ``{name: {"model": dict, "results": dict}}``.
SITES: dict[str, dict] = {}
#: Name of the model a call without ``site=`` addresses.
ACTIVE: dict[str, Optional[str]] = {"name": None}


def as_obj(x: Union[str, dict, list, None]) -> Any:
    """A JSON string, a ``.json`` path, or a native object."""
    if x is None or isinstance(x, (dict, list)):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s and s[0] not in "{[" and os.path.isfile(s):
            with open(s, encoding="utf-8") as fh:
                return json.load(fh)
        return json.loads(s)
    raise CivilError("bad_argument", f"expected JSON object/array, got {type(x).__name__}")


def remember(model: dict, results: Optional[dict] = None) -> dict:
    """Make ``model`` the active session site; a changed model drops its old results."""
    name = model.get("name") or "site"
    entry = SITES.get(name)
    if entry is None or entry.get("model") != model:
        entry = {"model": model, "results": {}}
        SITES[name] = entry
    if results:
        entry["results"].update(results)
    ACTIVE["name"] = name
    return entry


def studies(doc_id: str) -> list:
    try:
        res = cb.call("sim_study_list", {"doc_id": doc_id})
    except Exception:  # noqa: BLE001 — an unreadable study list reads as empty
        return []
    return [s for s in (res.get("studies") or []) if isinstance(s, dict)]


def find_study(doc_id: str, name: Optional[str]) -> Optional[dict]:
    """The ``civil_site`` study ``name`` in ``doc_id`` (the only one when ``name`` is None)."""
    found = [s for s in studies(doc_id) if s.get("type") == STUDY_TYPE]
    if name is None:
        return found[0] if len(found) == 1 else None
    return next((s for s in found if s.get("name") == name), None)


def target_doc(doc_id: Optional[str]) -> Optional[str]:
    """Resolve an OPTIONAL persistence target; raises ``unknown_doc`` for a bad id."""
    if not doc_id:
        return None
    try:
        return _doc(doc_id)
    except Exception as exc:  # noqa: BLE001 — DocRefError names the open documents
        raise CivilError("unknown_doc", str(exc)) from exc


def load_from_doc(doc_id: str, name: Optional[str]) -> dict:
    """Restore a persisted civil model into the session; refuses when absent/ambiguous."""
    st = find_study(doc_id, name)
    if st is None or not isinstance((st.get("params") or {}).get("model"), dict):
        names = [s.get("name") for s in studies(doc_id) if s.get("type") == STUDY_TYPE]
        raise CivilError("no_civil_site",
                         f"document {doc_id} has no civil_site study"
                         + (f" named {name!r}" if name else "")
                         + f"; saved sites: {names or 'none'}",
                         saved_sites=names)
    entry = remember(st["params"]["model"], st.get("results") or {})
    if st.get("id"):
        entry.setdefault("study_ids", {})[doc_id] = st["id"]
    return entry


def resolve_site(site: Union[str, dict, None], doc_id: Optional[str] = None) -> dict:
    """``(entry)`` for a site argument: inline model / JSON / path / session name / None.

    None → the active session site, else the single site saved in ``doc_id``.
    """
    if site is None:
        name = ACTIVE.get("name")
        if name and name in SITES:
            return SITES[name]
        if doc_id:
            return load_from_doc(target_doc(doc_id), None)
        raise CivilError("no_civil_site",
                         "pass site=<aicad.civil/1 model or session site name>, or create one "
                         "with civil_site_new / civil_import (civil_load restores a saved one)",
                         session_sites=sorted(SITES))
    if isinstance(site, str) and site.strip() in SITES:
        return SITES[site.strip()]
    if isinstance(site, str) and site.strip() and site.strip()[0] not in "{[" \
            and not os.path.isfile(site.strip()):
        if doc_id:
            return load_from_doc(target_doc(doc_id), site.strip())
        raise CivilError("unknown_site", f"{site!r} is not a session site; session sites: "
                         f"{sorted(SITES) or 'none'}", session_sites=sorted(SITES))
    model = as_obj(site)
    if isinstance(model, dict) and model.get("schema") != civil_schema.SCHEMA \
            and isinstance(model.get("model"), dict):
        model = model["model"]
    if not isinstance(model, dict):
        raise CivilError("bad_site", "site must be an aicad.civil/1 object")
    errors = civil_schema.validate(model)
    if errors:
        raise CivilError("invalid_site", "; ".join(errors[:10]), errors=errors)
    return remember(model)


def persist(doc_id: Optional[str], entry: dict) -> dict:
    """Write ``entry`` as the doc's ``civil_site`` study; reports, never raises.

    The entry remembers which study it was loaded from / saved to per document
    (``entry["study_ids"]``), so a renamed site edits its own study instead of leaving the
    old one behind; without a remembered id the study is matched by model name.
    """
    if not doc_id:
        return {"saved_to_study": False}
    model = entry["model"]
    name = model.get("name") or "site"
    known = (entry.get("study_ids") or {}).get(doc_id)
    existing = None
    if known:
        existing = next((s for s in studies(doc_id)
                         if s.get("id") == known and s.get("type") == STUDY_TYPE), None)
    if existing is None:
        existing = find_study(doc_id, name) or {}
    rec: dict = {"type": STUDY_TYPE, "name": name, "engine": ENGINE,
                 "params": {"model": model}, "results": dict(entry.get("results") or {})}
    if existing.get("id"):
        rec["id"] = existing["id"]
    sid = _persist_study(doc_id, rec)
    if sid:
        entry.setdefault("study_ids", {})[doc_id] = sid
    out: dict = {"doc_id": doc_id, "saved_to_study": bool(sid)}
    if sid:
        out["study_id"] = sid
    else:
        out["study_not_saved_reason"] = (
            f"the result could not be written into document {doc_id}'s studies; the numbers "
            "above are unaffected")
    return out


def store_result(entry: dict, key: str, result: dict) -> None:
    """Record ``result`` under analysis ``key`` (contract §5 keys only)."""
    if key not in RESULT_KEYS:
        raise CivilError("unknown_result_key", f"{key!r} not in {RESULT_KEYS}")
    entry.setdefault("results", {})[key] = {k: v for k, v in result.items()
                                            if k not in ("ok", "hint", "next_actions")}


def set_model(entry: dict, model: dict) -> dict:
    """Replace the entry's model after an edit (validated); results of the old model drop."""
    errors = civil_schema.validate(model)
    if errors:
        raise CivilError("invalid_site", "; ".join(errors[:10]), errors=errors)
    old_name = entry["model"].get("name")
    if entry.get("model") != model:
        entry["model"] = model
        entry["results"] = {}
    new_name = model.get("name") or "site"
    if old_name != new_name:
        SITES.pop(old_name, None)
    SITES[new_name] = entry
    ACTIVE["name"] = new_name
    return entry


def run_tool(fn: Callable[[], dict]) -> str:
    """Execute a tool body: CivilError → reason refusal; other errors → ``civil_internal_error``."""
    try:
        out = fn()
    except CivilError as exc:
        return dumps(exc.to_dict())
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return dumps(err("bad_argument", hint=str(exc)))
    except OSError as exc:
        return dumps(err("io_error", hint=str(exc)))
    return dumps(out)


def model_copy(entry: dict) -> dict:
    return copy.deepcopy(entry["model"])
