"""Shared state + helpers for the mechanical-CAD MCP tool family modules (split out
of the former monolithic cad_tools.py): doc registry/aliases, handoff publish, autosave +
recovery, background jobs, materials/appearance, inspect/measure/render helper machinery.
Imported by the cad_*_tools family modules; re-exported through the cad_tools facade."""

from __future__ import annotations

import contextlib
import ast
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Optional, Sequence, Union
from services.agent.request_context import owner_key, root_value

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from . import simhandoff as sh
from .core_bridge import CoreError

# Current parts are agent-scoped for host calls, process-scoped for standalone MCP.
# Keep the mapping surface used by the tool families; all imported _CUR references
# resolve the slot at access time, including open/recovery helpers.
class _CurrentPart(MutableMapping):
    def __init__(self):
        self._slots = {None: {"doc_id": None}}

    def _slot(self):
        owner = owner_key()
        agent = cb.CURRENT_AGENT.get()
        return self._slots.setdefault((owner, agent) if owner is not None else agent, {"doc_id": None})

    def __getitem__(self, key):
        return self._slot()[key]

    def __setitem__(self, key, value):
        self._slot()[key] = value

    def __delitem__(self, key):
        del self._slot()[key]

    def __iter__(self):
        return iter(self._slot())

    def __len__(self):
        return len(self._slot())


_CUR: MutableMapping = _CurrentPart()


def current_doc_id() -> Optional[str]:
    """Calling agent's current handle, without adopting an engine-global document."""
    value = _CUR.get("doc_id")
    return _resolve_alias(value) if value else None


@contextlib.contextmanager
def agent_doc_context(agent_id, *, scope=None):
    """Bind host calls to their own persistent slot; restore nested callers safely."""
    token = cb.CURRENT_AGENT.set((scope, agent_id))
    try:
        yield
    finally:
        cb.CURRENT_AGENT.reset(token)


@contextlib.contextmanager
def doc_scope():
    """Run nested work with its OWN current part, like a subshell's cwd.

    Whatever the nested agent creates or opens is current for IT; the caller's
    current part is restored on the way out. Without this a sub-agent's last
    new_part() would silently redirect its parent's next edit into a document
    the parent never asked for."""
    saved = _CUR["doc_id"]
    try:
        yield
    finally:
        _CUR["doc_id"] = saved


# Cross-PROCESS document ownership. `_CUR` and `doc_scope()` only ever separate work inside ONE
# interpreter; they say nothing across the OS-process boundary that separates two MCP client
# connections. Each external client (Claude Code, Cursor, a cowork swarm member) is its own
# `services.mcp.server` process with its own `_CUR` — but they all talk to the SAME
# chudware-hostd and therefore see the SAME engine-global `doc_list`. `_PROCESS_DOCS` is this
# process's claim set: every document it created, opened, recovered, or was explicitly handed
# by doc_id. It is what lets the sole-open-document adopt in `_doc()` tell "the part I am
# working on" from "another agent's part that happens to be the only one open right now".
_PROCESS_DOCS: set = set()


def claim_process_doc(doc_id: Optional[str]) -> None:
    """Record `doc_id` as a document THIS process has a claim on."""
    if doc_id:
        _PROCESS_DOCS.add(str(doc_id))


# doc_id STABILITY across an autosave-recovery (Issue 1). When a transport timeout kills the
# server and it restarts, recovery reopens each autosaved .step+ — which the engine assigns a
# FRESH doc_id (the per-process counter doesn't reset). Any doc_id the agent was holding would
# then be dead. We map the ORIGINAL id (the autosave filename stem) -> the new live id here, so
# an old handle the agent passes still resolves to the recovered document instead of erroring.
_DOC_ALIASES: dict = {}

# GSL Stage 1a — last-known solid volume per doc, so add_features can report a volume DELTA
# (material added / removed / unchanged) in its write-diff confirmation. Best-effort cache.
_LAST_VOLUME: dict = {}

# Save discipline (user-vs-AI ownership): the engine revision this doc was last DELIVERED at
# (save_part / export_part to a file). Native saves also happen automatically via
# _project_save, so a clean engine `dirty` flag alone is not proof of explicit delivery.
# Revision IS reliable: it's monotonic and bumped
# only by mutating ops (never by a save), so `current_revision > _LAST_DELIVER_REV[doc]` means
# "edited since the last real save". close_part uses it to nudge the agent to save its work.
_LAST_DELIVER_REV: dict = {}

# Placement provenance (assembly-merge discipline). A component dropped at an `at`/`rotate`
# pose is only as good as the measurement it came from: the failed merge session placed a
# wheel by a guessed transform, never re-checked interference, and exported it off-frame.
# doc.meta["placement_sources"][component_id] = {"placement_source": "transform"|"mate"|
# "origin", "at_revision": <engine revision when it was placed>}. This travels inside the
# .step+ document; a mate always supersedes a starting-pose transform.

# The engine revision at which each doc was last interference-checked (check_interference).
# Compared against the doc's CURRENT revision — monotonic, bumped only by mutating ops — so
# "placed by transform and nothing verified it since the last mutation" is decidable without
# any extra engine state.
_LAST_INTERFERENCE_REV: dict = {}


def pose_to_transform16(at: Optional[list], axis: Optional[list],
                        angle: float = 0.0) -> list:
    """``at``/``rotate``+``angle`` -> the row-major 4x4 the ``asm_*`` ops carry.

    The rotation (axis + degrees, Rodrigues) is applied FIRST, then the translation —
    the convention ``asm_insert_component``/``asm_edit_component`` read. One home for the
    math because both the insert tool (asm_add_component) and the re-pose path
    (add_features' asm_edit_component) accept the same at/rotate dialect.
    """
    import math  # noqa: PLC0415 — tiny, and this is not a hot path
    t = [0.0, 0.0, 0.0] if at is None else [float(v) for v in at]
    while len(t) < 3:
        t.append(0.0)
    r3 = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if axis is not None and angle:
        ax = [float(v) for v in axis]
        while len(ax) < 3:
            ax.append(0.0)
        n = math.sqrt(sum(v * v for v in ax[:3])) or 1.0
        x, y, z = (v / n for v in ax[:3])
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        C = 1 - c
        r3 = [[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
              [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
              [z * x * C - y * s, z * y * C + x * s, c + z * z * C]]
    return [r3[0][0], r3[0][1], r3[0][2], t[0],
            r3[1][0], r3[1][1], r3[1][2], t[1],
            r3[2][0], r3[2][1], r3[2][2], t[2],
            0.0, 0.0, 0.0, 1.0]


def _placement_sources(doc_id: str) -> dict:
    """Read placement provenance from the document, including after save/reopen."""
    value = cb.call("doc_get_meta", {"doc_id": doc_id, "key": "placement_sources"}).get("value")
    return value if isinstance(value, dict) else {}


def note_placement_source(doc_id: Optional[str], component: Optional[str],
                          source: str) -> None:
    """Record HOW a component was positioned ("transform" = a raw pose, "mate" = solved)."""
    if not doc_id or not component:
        return
    rec = _placement_sources(str(doc_id))
    prev = rec.get(str(component))
    if isinstance(prev, dict) and prev.get("placement_source") == "mate" and source != "mate":
        return  # a mate owns the pose; a later starting-pose transform does not un-verify it
    rec[str(component)] = {"placement_source": source, "at_revision": _doc_revision(str(doc_id))}
    cb.call("doc_set_meta", {"doc_id": str(doc_id), "key": "placement_sources", "value": rec})


def placement_source_of(doc_id: Optional[str], component: Optional[str]) -> Optional[str]:
    """"transform" | "mate" | "origin" from the document, else None (unknown/imported)."""
    if not doc_id or not component:
        return None
    rec = _placement_sources(str(doc_id)).get(str(component))
    return rec.get("placement_source") if isinstance(rec, dict) else None


def note_interference_check(doc_id: Optional[str]) -> None:
    """Record that check_interference ran on this doc at its current revision."""
    if not doc_id:
        return
    rev = _doc_revision(str(doc_id))
    if rev is not None:
        _LAST_INTERFERENCE_REV[str(doc_id)] = rev


def placement_warnings(doc_id: Optional[str]) -> list:
    """Non-blocking delivery warnings for transform-placed, unverified components.

    Returns `["placement_unverified: N component(s) placed by transform with no
    check_interference since last mutation"]` when this doc has transform-placed components
    and the last check_interference predates the doc's current (mutation-bumped) revision.
    Empty list when everything was mated, or verified after the last mutation.
    """
    if not doc_id:
        return []
    did = str(doc_id)
    by_transform = [c for c, rec in _placement_sources(did).items()
                    if isinstance(rec, dict) and rec.get("placement_source") == "transform"]
    if not by_transform:
        return []
    # Only a doc that really holds several placed things can be mis-placed relative to
    # something: a single-body part has nothing to be off-frame from.
    try:
        bodies = cb.call("asm_world_bodies", {"doc_id": did}).get("bodies") or []
    except Exception:  # noqa: BLE001 — never let a warning probe break a delivery
        bodies = []
    if len(bodies) < 2:
        return []
    cur = _doc_revision(did)
    checked = _LAST_INTERFERENCE_REV.get(did)
    if checked is not None and cur is not None and checked >= cur:
        return []
    return [f"placement_unverified: {len(by_transform)} component(s) placed by transform "
            f"with no check_interference since last mutation"]


def _doc_revision(doc_id: str) -> Optional[int]:
    """Current engine revision for a doc via the cheap doc_list (no geometry compute)."""
    try:
        for e in cb.call("doc_list", {}).get("docs", []):
            if isinstance(e, dict) and e.get("doc_id") == doc_id:
                r = e.get("revision")
                return int(r) if r is not None else None
    except Exception:  # noqa: BLE001
        pass
    return None


# --------------------------------------------------------------------------- helpers

# --------------------------------------------------------------------------- model-dialect guards
# LIVE CRASH (run-97813015264134): the model sent `"feature": "{'type': 'cylinder', ...,
# 'expr': None}"` — a PYTHON REPR STRING (single-quoted, bare None/True/False) where an
# object belonged — and a bare `.get()` on it blew up with `'str' object has no attribute
# 'get'`: a transport-level crash that taught the model nothing. Every tool that reaches into
# a model-supplied "structured" argument (ops list, feature dict, refs/params, selector list,
# view spec) MUST run it through this first. json.loads handles the common double-encoded
# case; ast.literal_eval (Python-literal syntax: single quotes, None/True/False, tuples) is
# strictly needed for the observed repr shape json.loads cannot parse. Never raises, never
# stringifies — the ORIGINAL value comes back untouched when neither parses, so a genuine
# non-string argument (already a dict/list) passes through for free and a genuinely broken
# string still reaches the caller's own type check (and its teaching error) unchanged.
def coerce_json_or_repr(value: Any) -> Any:
    """If `value` is a str that encodes a dict/list/etc as JSON or a Python repr, decode it;
    otherwise return `value` unchanged. Safe on any input — this never raises."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s[0] not in "{[('\"" and s not in ("None", "True", "False"):
        return value  # not literal-shaped (e.g. a plain doc_id) — don't even try
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    try:
        return ast.literal_eval(s)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return value


def _describe_type(value: Any) -> str:
    """A short, model-legible description of what was actually received, for teaching errors."""
    if isinstance(value, str):
        preview = value if len(value) <= 60 else value[:57] + "..."
        return f"str ({preview!r})"
    return type(value).__name__


def require_mapping(value: Any, arg_name: str, shape_hint: str = "a JSON object") -> dict:
    """Coerce + type-check a structured argument that MUST be a mapping (dict). Raises a
    teaching ValueError naming the argument, the type actually received, and the accepted
    shape — never lets a bare `.get()`/subscript on the wrong type leak a raw AttributeError/
    TypeError to the model (the `'str' object has no attribute 'get'` crash)."""
    coerced = coerce_json_or_repr(value)
    if not isinstance(coerced, dict):
        raise ValueError(
            f"{arg_name} must be {shape_hint} (a mapping), got {_describe_type(value)}. "
            f"If you meant to send an object, pass real JSON — not a quoted Python repr string.")
    return coerced


def require_list(value: Any, arg_name: str, shape_hint: str = "a JSON array") -> list:
    """Coerce + type-check a structured argument that MUST be a list (ops batches, refs
    lists, selector lists). A bare scalar/dict is wrapped as a single-element list ONLY when
    it plainly isn't itself a stringified list (models routinely send one op object instead
    of a one-item batch); anything else raises the same teaching shape as `require_mapping`."""
    coerced = coerce_json_or_repr(value)
    if isinstance(coerced, list):
        return coerced
    if isinstance(coerced, tuple):
        return list(coerced)
    if isinstance(coerced, dict):
        return [coerced]  # one op/ref sent bare instead of wrapped in a batch
    raise ValueError(
        f"{arg_name} must be {shape_hint} (a list), got {_describe_type(value)}. "
        f"If you meant to send a list, pass real JSON — not a quoted Python repr string.")


# Words a model sends to mean "I am explicitly leaving this OPTIONAL slot empty" instead of
# omitting the argument — the same vocabulary cad_doc_tools' add_features refs-slot fix
# teaches (live 2026-07-28: refs.faces:"none" and refs.remove:[] were both hard-rejected
# instead of read as "no selection"). Shared here so every OTHER selector-taking tool
# (asm_mate operand shape, simulate_motion/motion_fea_handoff mate_id, drawing view_id, …)
# treats the same placeholder words the same way instead of rejecting them one-by-one.
_EMPTY_SLOT_WORDS = frozenset({"none", "null", "nil", "nothing", "no", "n_a", "na",
                               "", "empty", "undefined", "not_applicable"})


def _is_empty_slot_word(v: Any) -> bool:
    """True when `v` is a string a model sent to mean "nothing here" (case/punct-insensitive
    match against _EMPTY_SLOT_WORDS), or an empty list/tuple/dict. `None` is NOT included —
    it already means "omitted" in Python and callers usually branch on it separately."""
    if isinstance(v, str):
        return re.sub(r"[^a-z0-9]+", "_", v.strip().lower()).strip("_") in _EMPTY_SLOT_WORDS
    if isinstance(v, (list, tuple, dict)):
        return len(v) == 0
    return False


def _normalize_optional_slot(v: Any) -> Any:
    """Collapse an explicit empty-placeholder value ('', "none", "null", [], {}, …) to `None`
    so an OPTIONAL id/selector argument reads identically whether the model omitted it or
    explicitly emptied it — instead of failing a literal lookup for a mate/view/component
    named "none". Non-empty values pass through unchanged."""
    if v is None or _is_empty_slot_word(v):
        return None
    return v


def _face_hist(doc_id: str) -> dict:
    """GSL Stage 1a — a cheap topological snapshot for the add_features write-diff: count faces
    by shape (surf) and by bore/boss concavity (the Stage-0 semantics now in the read-back), so
    a build op can confirm WHAT it changed, not just that the volume moved. Best-effort: returns
    an empty hist on advisory errors; engine connection loss must stop the mutation."""
    try:
        res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return {}
    hist: dict = {"total": 0, "surf": {}, "bore": 0, "boss": 0}
    for m in res.get("matches", []):
        hist["total"] += 1
        surf = m.get("surf") or "other"
        hist["surf"][surf] = hist["surf"].get(surf, 0) + 1
        conc = m.get("concavity")
        if conc == "bore":
            hist["bore"] += 1
        elif conc == "boss":
            hist["boss"] += 1
    return hist


# A volume change this small RELATIVE to the body is not real material: OCCT blend/boolean
# round-off lands here, and so does a feature authored microscopically small. Measured live
# 2026-07-30: a fillet of radius 1e-5 mm on a 60x40x20 box moved the volume by 0.0005 mm^3
# (1.0e-8 of the body) and the write-diff printed "volume 48000.0->48000.0 mm^3 (-0.0)" — a
# self-contradicting line the model read as a successful fillet. A real 0.5 mm fillet on the
# same box removes ~4 mm^3 (8e-5 of the body), four orders of magnitude clear of this floor.
_NEGLIGIBLE_VOLUME_FRACTION = 1e-7


def _write_diff(before: dict, after: dict, vol_before, vol_after, *,
                warnings: Sequence = (), bbox: Optional[dict] = None) -> dict:
    """GSL Stage 1a (W2) — compose a SEMANTIC confirmation of what an op actually did, so the
    return is no longer a bare ok:true + a volume number the model must diff itself. Reports the
    net face change (by shape, and how many are bores/holes), the volume delta with its sign,
    the part's overall size, and any per-feature warning the ENGINE raised; a no-op (a
    cut/boolean that missed the body, a feature that altered nothing) reads as an explicit
    'no geometry change' warning instead of looking successful.

    `warnings` are the engine's own `feature_status[].warning` strings for the batch. They used
    to travel ONLY in the `features[]` rows, one entry among a column of nulls, while this
    summary — the line the ops cheatsheet tells the model to read — printed a generic guess
    ("a cut/boolean may have missed") even when the engine had already named the exact cause
    (measured live 2026-07-30: "fillet 1e-08 below tolerance; no material removed"). The
    engine's reason always beats our guess, so it travels in the summary too.

    `bbox` is a doc_bbox reply. Its `size` answers "is my part the right size" — the question
    that otherwise costs a separate measure_part round trip (1060 chars measured) — for ~32
    chars. It catches the class where every delta looks healthy but the part is not: a boss
    sketched at [200,200] fuses fine ("added 6 face(s); volume 48000->49000 (+1000)") and
    silently makes a 60 mm part 240 mm wide."""
    before = before or {}
    after = after or {}
    dface = after.get("total", 0) - before.get("total", 0)
    dbore = after.get("bore", 0) - before.get("bore", 0)
    surf_delta = {}
    for sname in set(before.get("surf", {})) | set(after.get("surf", {})):
        d = after.get("surf", {}).get(sname, 0) - before.get("surf", {}).get(sname, 0)
        if d:
            surf_delta[sname] = d
    parts = []
    if dface > 0:
        descr = ", ".join(f"+{d} {s}" for s, d in sorted(surf_delta.items()) if d > 0)
        seg = f"added {dface} face(s)" + (f" ({descr})" if descr else "")
        if dbore > 0:
            seg += f"; {dbore} new bore/hole(s)"
        parts.append(seg)
    elif dface < 0:
        parts.append(f"removed {-dface} face(s)")
    dv = None
    if isinstance(vol_after, (int, float)):
        if isinstance(vol_before, (int, float)):
            dv = vol_after - vol_before
            floor = max(1e-6, _NEGLIGIBLE_VOLUME_FRACTION * abs(vol_after))
            if abs(dv) >= floor:
                parts.append(f"volume {vol_before:.1f}->{vol_after:.1f} mm^3 "
                             f"({'+' if dv >= 0 else ''}{dv:.1f})")
            elif dv:
                # Real but negligible: never print "48000.0->48000.0 (-0.0)" and call it a change.
                parts.append(
                    f"volume effectively UNCHANGED ({vol_after:.1f} mm^3, delta {dv:+.3g} mm^3 "
                    f"= {abs(dv) / max(abs(vol_after), 1e-9):.1g} of the body) - too small to be "
                    f"real material; re-check the size you asked for")
        elif vol_before is None and abs(vol_after) >= 1e-6:
            dv = float(vol_after)
            parts.append(f"volume now {vol_after:.1f} mm^3")
    wtexts = [t for t in (str(w).strip() for w in (warnings or ())) if t]
    if parts:
        summary = "; ".join(parts)
    elif wtexts:
        summary = ("no geometry change - " + "; ".join(wtexts) +
                   ". Re-check the placement/size against the request.")
        wtexts = []  # the engine's reason IS the summary here; don't say it twice
    else:
        summary = ("no geometry change - no faces added/removed and volume unchanged; the op "
                   "did not alter the body (a cut/boolean may have missed, or a boss/edit was a "
                   "no-op). Re-check the placement/size against the request.")
    if wtexts:
        summary = "ENGINE WARNING: " + "; ".join(wtexts) + " -- " + summary
    out = {"summary": summary, "faces_delta": dface, "bores_delta": dbore}
    if dv is not None:
        out["volume_delta_mm3"] = round(dv, 4)
    size = (bbox or {}).get("size")
    if (isinstance(size, (list, tuple)) and len(size) == 3
            and all(isinstance(v, (int, float)) for v in size)):
        # doc_bbox is inflated by ~1e-7 mm; 3dp rounding lands it back on the authored number.
        out["bbox_mm"] = [round(float(v), 3) for v in size]
        out["summary"] = (out["summary"].rstrip(". ") + "; part bbox "
                          + "x".join(f"{v:g}" for v in out["bbox_mm"]) + " mm")
    return out

def _resolve_alias(doc_id: str) -> str:
    """Follow the recovery alias chain so a stale (pre-restart) doc_id resolves to the live one."""
    seen: set = set()
    while doc_id in _DOC_ALIASES and doc_id not in seen:
        seen.add(doc_id)
        doc_id = _DOC_ALIASES[doc_id]
    return doc_id


# --- doc handle resolution ---------------------------------------------------------------
# Measured (bench-run ledgers): ~40 tool failures are the agent failing to NAME the document
# it means — the part's NAME instead of its id ("flanged_bushing_design"), the id with its
# "doc-" prefix dropped ("cpp00000009"), a whole `new_part(name='flanged_bushing')` call
# pasted where a handle belongs, or no doc_id at all with several parts open. The engine
# knows every open doc's id AND name (doc_list), so resolve a supplied handle against that
# vocabulary instead of forwarding it and letting the engine answer a bare "no such
# document: <thing>" — which names the offence but never the correction. Whichever way
# resolution fails, the error carries the open-document set as id="name" pairs so the NEXT
# turn can just pick one.

_DOC_LIST_MAX = 20  # cap the listing: a shared daemon can hold dozens of docs

# `name=`/`part=` out of a pasted call, quoted or bare: new_part(name='flanged_bushing').
_DOC_KWARG_NAME_RE = re.compile(
    r"""\b(?:doc_id|name|part_name|part|title)\s*=\s*(?:['"]([^'"]*)['"]|([^,)\s]+))""")
_DOC_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")

# An engine-minted handle from a registry doc_list does NOT enumerate: drawings
# ("dwg-cpp0000003") and ECAD docs ("ecadcpp000000a") are kept in their own C++ registries,
# so only "doc-" ids are ours to verify. Matches a <namespace>cpp<hex> handle that is not a
# part/assembly id — a bare "cpp00000009" (the dropped-prefix typo) deliberately does not.
_FOREIGN_HANDLE_RE = re.compile(r"^(?!doc-)[a-z]{3,8}-?cpp[0-9a-f]{4,}$", re.I)
# A part/assembly id as the engine mints it (core/doc/session.cpp new_doc_id), either spelling.
_ENGINE_DOC_ID_RE = re.compile(r"^doc[-_]cpp[0-9a-f]{1,16}$", re.I)


class DocRefError(CoreError, ValueError):
    """A document handle that could not be resolved to an open document.

    A CoreError so `_err` renders its structured `detail` (the open-document vocabulary plus
    the corrected call) next to the message, and still a ValueError so every existing
    `except ValueError` around `_doc` keeps catching it — this used to be a plain one."""


def _doc_listing(docs: list, limit: int = _DOC_LIST_MAX) -> str:
    """The open documents as id="name" pairs — the vocabulary a caller must choose from."""
    if not docs:
        return "(none)"
    out = ", ".join(f'{e.get("doc_id")}="{e.get("name") or ""}"' for e in docs[:limit])
    if len(docs) > limit:
        out += f", … +{len(docs) - limit} more (call list_parts() for the full set)"
    return out


def _doc_error(problem: str, docs: list, hint: str, code: str) -> DocRefError:
    """One teaching shape for every way a doc handle fails: what was wrong (naming the
    offending value), the FULL open-document set, and the corrected call.

    When recover_autosaved() ran in this process, the recovered docs are called out
    SEPARATELY. In the raw open-document list a batch of recovered snapshots is
    indistinguishable noise — often several sharing one name — so a bare "no such
    document" or "ambiguous" left the caller with no way to tell which ids it had just
    been handed. Naming them, with the pre-crash id each one replaces, turns the refusal
    into the disambiguation the caller actually needs."""
    recovered = [{"doc_id": r.get("doc_id"), "name": r.get("name") or "",
                  "original_doc_id": r.get("original_doc_id") or ""}
                 for r in _RECOVERED if r.get("doc_id")]
    msg = f"{problem} Open parts: {_doc_listing(docs)}. {hint}"
    detail = {"open_documents": [{"doc_id": e.get("doc_id"), "name": e.get("name") or ""}
                                 for e in docs[:_DOC_LIST_MAX]],
              "hint": hint}
    if recovered:
        listing = ", ".join(f'{r["doc_id"]}="{r["name"]}"'
                            for r in recovered[:_DOC_LIST_MAX])
        extra = f" +{len(recovered) - _DOC_LIST_MAX} more" \
            if len(recovered) > _DOC_LIST_MAX else ""
        msg += (f" Of those, {len(recovered)} were just reopened by recover_parts "
                f"(newest first): {listing}{extra} — pick one of THOSE if you meant the "
                "recovered work.")
        detail["recovered_documents"] = recovered[:_DOC_LIST_MAX]
        if _RECOVER_SKIPPED:
            msg += (f" {_RECOVER_SKIPPED} older autosaved doc(s) were deliberately NOT "
                    "reopened; call recover_parts(limit=…) if you need one of them.")
            detail["recovery_skipped"] = _RECOVER_SKIPPED
    return DocRefError(msg, code=code, detail=detail)


def _docs_named(docs: list, value: str) -> list:
    """Open docs whose NAME is `value` — an exact match wins outright, else case-insensitive."""
    v = value.strip()
    exact = [e for e in docs if (e.get("name") or "") == v]
    if exact:
        return exact
    folded = v.casefold()
    return [e for e in docs if (e.get("name") or "").strip().casefold() == folded]


def _project_dir() -> str:
    """The turn's project directory (CHUDWARE_PROJECT_DIR), absolute and normalized; "" if unset."""
    raw = root_value("CHUDWARE_PROJECT_DIR").strip()
    return os.path.normpath(os.path.abspath(os.path.expanduser(raw))) if raw else ""


def _resumable_docs() -> list:
    """Open docs a by-name resume may land on: every doc when no project is set, else the
    unsaved ones plus those whose file lies inside the project. Another project's open doc
    is never resumed (the engine applies the same rule to doc_new's `project_dir`) — an
    agent building "sixaxis_arm" in /tmp/run4 edited the user's Desktop assembly of that
    name because it happened to be open in the shared hostd (2026-09-27)."""
    docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
            if isinstance(e, dict) and e.get("doc_id")]
    project = _project_dir()
    if not project:
        return docs

    def inside(path: Any) -> bool:
        if not path:
            return True
        p = os.path.normpath(os.path.abspath(str(path)))
        return os.path.commonpath([p, project]) == project

    return [e for e in docs if inside(e.get("path"))]


def _doc_new_scope() -> dict:
    """doc_new params that scope its resume_if_open match to the turn's project."""
    project = _project_dir()
    return {"project_dir": project} if project else {}

# ---------------------------------------------------------------------------
# Version-series resolution — "the next version" is a REVISION, not a new part.
# ---------------------------------------------------------------------------
# Live 2026-08-05: one holster request left NINE documents open at once —
# ruger_lcp_iwb_holster_v3 … _v11, each 2-6 features, each a successive iteration of
# the SAME design. new_part's resume contract is keyed on the NAME, so it only catches
# a model that asks for the same name twice; a model that bumps a version suffix every
# iteration sails straight past it and mints. Each mint then costs a tab, a .step+ in
# the user's project folder, its own AI-VC delta chain (services/plm/ai_vc.py keys the
# chain by doc_id), its own auto-registered PLM item (services/plm/vault.py
# _explode_project_into_shadow registers every *.step+ it finds under the project as a
# fresh item), and — because nothing closes mission docs — its own row in the next
# launch's "Recover unsaved parts?" prompt.
#
# A trailing EXPLICIT version token is an unambiguous statement of "version N of the
# thing I already have". A trailing bare NUMBER is not (PN-1235 is a different part from
# PN-1234), so the token is REQUIRED and must be separated: "holster_v4",
# "arm-rev C", and "part_final2" are versions; "PN-1235" and "holster4" are not
# (the latter is already the engine uniquifier's own series — see _in_name_series
# in cad_doc_tools). A revision followed by a final pass is still one series:
# "sixaxis_arm_revD_final5" -> "sixaxis_arm". The revision/final *tokens* need a
# separator from the design, not between "rev" and its letter or "final" and its number.
_VERSION_SUFFIX_RE = re.compile(
    r"[\s._\-]+(?:(?:v|ver|vers|version|rev|revision|mk|mark|gen|iter|iteration"
    r"|attempt|try|pass|round)\s*\d{1,4}|(?:rev|revision)\s*[a-z]"
    r"|final\s*\d{1,4})$",
    re.IGNORECASE,
)


def _version_series_base(name: Optional[str]) -> str:
    """`name` with trailing explicit version tokens stripped, or "" with none.

    "holster_v4" -> "holster"; "arm-rev C" -> "arm";
    "sixaxis_arm_revD_final5" -> "sixaxis_arm"; "PN-1235" -> "".
    """
    n = (name or "").strip()
    base = n
    while m := _VERSION_SUFFIX_RE.search(base):
        remaining = base[: m.start()].strip(" ._-")
        if not remaining:
            break
        base = remaining
    return base if base != n else ""


def _strip_version_suffix(name: str) -> str:
    """Return the one-document mint name, with any explicit version token removed.

    One design is one document: leaving a version token in the mint name creates a
    second tab, a second .step+ file, a second vault item, and a recovery row, so the
    mint name is always the series base and the revision lives in PLM.

    This deliberately carries the shared version regex's known false-positive class:
    a genuine designator such as ``Intake_Manifold_V8`` is minted as
    ``Intake_Manifold``. That tradeoff is accepted rather than forking a second
    definition of "version token"; the tool reply reports the normalization so the
    caller can see it. This only normalizes new document names, never a save path or
    an existing document/file.
    """
    return _version_series_base(name) or (name or "").strip()


def _docs_in_version_series(docs: list, name: str, kind: str = "part") -> list:
    """Open, non-empty `kind` docs that are an EARLIER VERSION of `name`.

    Membership is base-to-base, so "holster", "holster_v3", "holster-rev C",
    and "holster_revD_final5" are one series. Excluded deliberately:
      * the exact name asked for — that is the engine's own resume_if_open path, decided
        under its mint lock, and second-guessing it here reintroduces the unlocked
        check-then-mint race that produced three tabs for one part (run-42508173312214);
      * a different `kind` — a part is never a version of an assembly;
      * EMPTY siblings — a zero-content leftover is not the design. Resuming one would
        hand the model an empty document while its real work sat in another tab;
        _discard_unbuilt_orphans closes those instead.
    """
    base = _version_series_base(name)
    if not base:
        return []
    folded = base.casefold()
    asked = (name or "").strip().casefold()
    out = []
    for e in docs:
        if not isinstance(e, dict) or not e.get("doc_id"):
            continue
        if (e.get("kind") or "part") != kind:
            continue
        n = (e.get("name") or "").strip()
        if not n or n.casefold() == asked:
            continue
        if n.casefold() != folded and _version_series_base(n).casefold() != folded:
            continue
        if not ((e.get("feature_count") or 0) or (e.get("component_count") or 0)):
            continue
        out.append(e)
    return out


def _pick_best_doc(rows: list) -> Optional[dict]:
    """The document a resume should land on: most features, then highest revision, then
    latest in `doc_list` (which is oldest-first, so the newest wins a tie). Mirrors the
    engine's own resume_if_open ranking (core/doc/session.cpp resume_open_doc)."""
    if not rows:
        return None
    return max(enumerate(rows),
               key=lambda t: ((t[1].get("feature_count") or 0),
                              (t[1].get("revision") or 0), t[0]))[1]


def _doc_near_misses(docs: list, value: str) -> list:
    """Docs whose name is CLOSE to `value` ("flanged_bushing" for "flanged_bushing_design").
    Only ever offered as a hint, never auto-resolved: editing the wrong part is worse than
    an error the model can fix on the next turn."""
    import difflib  # noqa: PLC0415 — failure path only
    v = value.strip().casefold()
    if not v:
        return []
    near = [e for e in docs
            if (n := (e.get("name") or "").strip().casefold()) and (n in v or v in n)]
    if near:
        return near
    by_name = {(e.get("name") or "").strip().casefold(): e for e in docs if e.get("name")}
    return [by_name[m] for m in difflib.get_close_matches(v, list(by_name), n=3, cutoff=0.72)]


# Origin kinds the daemon stamps on each `doc_list` entry (core/hostd/doc_registry.cpp
# annotateDocList, :740-763): the KIND of the client that MINTED the doc — gui | mcp | agent |
# test | unknown. "mcp"/"agent" means an AI client: an external MCP server process, or the
# in-app copilot's Python worker (which also hellos as "mcp", daemon_bridge.py:446-448).
#
# That kind is the ONLY per-document attribution on the wire. The engine's own entry carries
# doc_id / name / kind / path / revision / dirty / feature_count / component_count / empty and
# nothing about who made it (core/doc/session.cpp doc_list, :5653-5663); the daemon adds
# `origin_kind` and nothing else — no minting client id, pid, or session — and `hello` does not
# even hand a client its own connection id back (core/hostd/server.cpp:599-612). So "some other
# AI client's document" is as fine-grained as this can be made from here; distinguishing WHICH
# peer would need a new daemon field.
_AI_ORIGIN_KINDS = ("mcp", "agent")


def _sole_doc_is_adoptable(entry: dict) -> bool:
    """May an omitted doc_id silently adopt `entry`, the only document open engine-wide?

    Yes when there is no shared engine to steal from, when the doc is already ours, or when it
    is not an AI client's private working part:

      * legacy (non-daemon) mode — `doc_list` is THIS process's own in-process engine, so every
        document it can see is ours by construction. There is no second client; adopt.
      * a doc this process created / opened / recovered / was handed explicitly. `_doc()`
        reconciles `_PROCESS_DOCS` against the open documents on every call, so this is a
        claim on a doc that is still THERE — not a stale id the engine's per-daemon counter
        later handed to somebody else. Covers a sub-agent's part after `doc_scope()` put
        `_CUR` back, and our own part after a `close_part` of a different doc cleared it.
        (`_TOUCHED_DOCS` is deliberately NOT consulted: `note_touched_doc` feeds
        `_PROCESS_DOCS` too, while the terminal save sweep keeps touched history until a
        successful close explicitly evicts the document.)
      * `origin_kind` "gui" (the human's own part in the viewport), "test", "unknown", or absent
        — acting on the part the user is actually looking at is the whole point of the adopt.

    No when the sole open document was minted by an AI client and we hold no claim on it: that
    is another swarm member's part. From the engine's global view it looks exactly like our own
    would, purely because we have not made one yet — which is precisely the first tool call of
    a fresh cowork swarm member, the most common moment in a multi-agent session.
    """
    try:
        if not cb.active_daemon_mode():
            return True
    except Exception:  # noqa: BLE001 — mode unknown: fall through to the origin test
        pass
    if entry.get("doc_id") in _PROCESS_DOCS:
        return True
    return (entry.get("origin_kind") or "") not in _AI_ORIGIN_KINDS


def _foreign_sole_doc(entry: dict) -> "DocRefError":
    """The one open document belongs to another AI client: refuse, and name the exact way out.

    Never silently adopt here. An omitted doc_id cannot have meant a part this process never
    opened, and a WRITE tool landing in a peer's part is unrecoverable for that peer.
    """
    did = entry.get("doc_id")
    return _doc_error(
        "You have not created or opened a part yet, and the one document open here "
        f"({did!r}, named {entry.get('name') or ''!r}) was created by a DIFFERENT AI client "
        f"(origin_kind={entry.get('origin_kind')!r}). Every MCP client attached to this engine "
        "shares one document list, so adopting it would edit a part you never opened.",
        [entry], code="doc_owned_by_other_client",
        hint=('call new_part(name="…") to work in your own part — its doc_id then becomes your '
              "default and every later bare call lands there. If you genuinely mean that part, "
              f'call list_parts() and pass it explicitly: doc_id="{did}".'))


def _doc(doc_id: Optional[str]) -> str:
    # `_CUR` is the calling agent's current part (standalone MCP uses the process
    # slot). A part the human already had open in the GUI — or one another
    # client made — never touches it, so trusting `_CUR` alone reports "no current part" while
    # the part sits right there in the viewport. Always ask the engine what is actually open and
    # reconcile against it. The same listing is what resolves an imperfect supplied handle and
    # what every teaching error below quotes back.
    try:
        docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                if isinstance(e, dict) and e.get("doc_id")]
        listed = True
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        docs, listed = [], False

    def _ambiguous(candidates: Optional[list] = None,
                   supplied: Optional[str] = None) -> DocRefError:
        """The "which document did you mean?" error — always naming every open doc."""
        if candidates is None:
            return _doc_error(
                f"You have not created or opened a part yet, and {len(docs)} are already "
                "open here, so there is nothing to pick unambiguously.",
                docs, code="doc_ambiguous",
                hint=('call new_part(name="…") to work in your own part (its doc_id then '
                      'becomes your default), or call list_parts() and name the one you mean, '
                      f'e.g. doc_id="{docs[0].get("doc_id")}" — an open part\'s name works too.'))
        return _doc_error(
            f"Ambiguous doc_id={supplied!r}: {len(candidates)} open parts answer to that name "
            f"({_doc_listing(candidates)}), so it does not identify one document.",
            docs, code="doc_ambiguous",
            hint=f'pass the id of the one you mean, e.g. doc_id="{candidates[0].get("doc_id")}".')

    # An explicit doc_id always wins over the current part — the way to address a document
    # other than the one you are working in.
    if doc_id:
        raw = str(doc_id).strip()
        if not listed:
            # No engine listing to check against: forward the handle unverified (legacy path).
            return _resolve_alias(raw)
        ids = {e.get("doc_id"): e for e in docs}
        folded_ids = {str(k).casefold(): k for k in ids}

        def _lookup(tok: str) -> Optional[str]:
            """A handle -> a live doc_id: the id itself, an alias of it, the id with its
            "doc-" prefix dropped or case-mangled, or the part's unique name."""
            t = tok.strip()
            if not t:
                return None
            a = _resolve_alias(t)
            if a in ids:
                return a
            for cand in (t, f"doc-{t}"):
                hit = folded_ids.get(cand.casefold())
                if hit:
                    return hit
            named = _docs_named(docs, t)
            return named[0]["doc_id"] if len(named) == 1 else None

        resolved = _lookup(raw)
        if resolved:
            # Naming a document explicitly IS a claim on it: record it so this process's later
            # bare calls can still resolve to it once `_CUR` is cleared or scoped away.
            claim_process_doc(resolved)
            return resolved
        if _FOREIGN_HANDLE_RE.match(raw):
            # A drawing / ECAD handle: its registry, not doc_list, is the authority on
            # whether it is live. Forward it untouched and let that registry answer.
            return _resolve_alias(raw)
        same_name = _docs_named(docs, raw)
        if len(same_name) > 1:
            raise _ambiguous(same_name, raw)
        # Source text pasted where a handle belongs ("new_part(name='flanged_bushing')").
        # Say THAT: "no such document: new_part(name='flanged_bushing')" reads like a lookup
        # miss when the real mistake is the kind of argument. Never resolve it silently — the
        # text asks to CREATE a part, so adopting an existing one could edit the wrong doc.
        if "(" in raw or "=" in raw:
            m = _DOC_KWARG_NAME_RE.search(raw)
            guess = ((m.group(1) or m.group(2) or "") if m else "").strip()
            if not guess:
                q = _DOC_QUOTED_RE.search(raw)
                guess = q.group(1).strip() if q else ""
            inner = _lookup(guess) if guess else None
            if inner:
                hint = f'that part is already open — pass doc_id="{inner}".'
            elif guess:
                hint = (f'no open part is named "{guess}" — call new_part(name="{guess}") as its '
                        "own tool call first, then pass the doc_id it returns.")
            else:
                hint = ('call new_part(name="…") as its own tool call first, then pass the '
                        "doc_id it returns.")
            raise _doc_error(
                f"doc_id={raw!r} is source text (a call or an argument expression), not a "
                'document handle: doc_id takes an open document\'s id ("doc-cpp0000000b") or '
                "its name.",
                docs, code="doc_id_is_source_text", hint=hint)
        if not docs and not _ENGINE_DOC_ID_RE.match(raw):
            raise _doc_error(
                f"no such document {raw!r}: ZERO documents are open in this engine session.",
                docs, code="doc_not_found",
                hint='call new_part(name="…") first, then pass the doc_id it returns.')
        if _ENGINE_DOC_ID_RE.match(raw):
            # A well-formed engine id that is not open was open once: its document was closed,
            # reaped by doc-GC, or lived in an engine session that ended with the app. "Pass
            # one of the ids above" there invites editing whichever part IS open; the way back
            # to the part the caller meant is to reopen it (2026-09-28: a resumed turn's
            # render_part(doc_id="doc-cpp00000003") after an app restart).
            raise _doc_error(
                f"no such document {raw!r}: no open document has that id.",
                docs, code="doc_not_found",
                hint=("a doc_id lives only while its document stays open in this engine "
                      "session — closing the part, doc-GC or an app restart retires it, and "
                      "the engine may later re-issue the same number to a DIFFERENT part. "
                      "Do not substitute an open id unless it is the same part: reopen the "
                      'part you meant with import_part(path="<its .step+>", resume=True) and '
                      "use the doc_id it returns."))
        near = _doc_near_misses(docs, raw)
        raise _doc_error(
            f"no such document {raw!r}: it matches no open document's id or name.",
            docs, code="doc_not_found",
            hint=(f'did you mean doc_id="{near[0].get("doc_id")}" (the part named '
                  f'"{near[0].get("name")}")?' if len(near) == 1 else
                  "pass one of the ids above, or the exact name of an open part."))

    # The current part is whatever THIS agent last created or opened, and that claim is
    # authoritative however many other documents happen to be open. Host dispatch binds
    # each agent's slot; standalone MCP and nested doc_scope work retain their usual
    # process-local behavior.
    live = {e.get("doc_id") for e in docs} if listed else set()
    if listed:
        # Drop claims on documents that are GONE. Engine doc_ids are a per-daemon counter and
        # reset when the daemon restarts, so a claim kept past its document's close would
        # eventually match a PEER's freshly minted "doc-cpp00000001" and hand the adopt below
        # exactly the false positive it exists to prevent. Reconciling against what is open
        # keeps the claim set as short-lived as the documents themselves.
        _PROCESS_DOCS.intersection_update(live)
    d = _resolve_alias(_CUR["doc_id"]) if _CUR["doc_id"] else None
    if d and listed and d not in live:
        # Our part is gone — closed in the app, or reaped by doc-GC. Holding the dead
        # handle only defers the failure to a confusing "no such document"; drop it and
        # resolve from what is actually open.
        _CUR["doc_id"] = None
        d = None
    if d:
        claim_process_doc(d)
        return d
    if len(docs) == 1:
        # Nothing claimed, one part open: adopt it, so every read/measure/edit tool acts on
        # the part the user is actually looking at instead of demanding a redundant
        # new_part() — UNLESS that part is another AI client's own working document.
        #
        # `docs` is the ENGINE-GLOBAL listing, shared by every client attached to this
        # chudware-hostd, not a per-caller view. So "exactly one document open" is equally
        # what a fresh cowork swarm member sees the instant a PEER created its first part,
        # and adopting there silently redirected this caller's reads — and its WRITES — into
        # the peer's part. The window closes once a second doc opens (`_ambiguous()` below),
        # which is exactly why it bit the first tool call of every new swarm member.
        if not _sole_doc_is_adoptable(docs[0]):
            raise _foreign_sole_doc(docs[0])
        _CUR["doc_id"] = docs[0]["doc_id"]
        claim_process_doc(_CUR["doc_id"])
        return _resolve_alias(_CUR["doc_id"])
    if docs:
        raise _ambiguous()
    if not listed:
        raise _doc_error(
            "No current part, and the engine's document list is unavailable "
            "(doc_list failed), so nothing can be resolved for you.",
            docs, code="no_current_part",
            hint='pass doc_id= explicitly, or call new_part(name="…") first.')
    # Zero documents open is NOT "you forgot to pass doc_id" — there is nothing to
    # address yet, so say so plainly instead of implying a missing argument.
    raise _doc_error(
        "No current part: ZERO documents are open in this engine session, so no "
        "doc_id would work either.",
        docs, code="no_open_documents",
        hint='call new_part(name="…") first; it returns the doc_id to pass.')


def _dumps(obj: Any) -> str:
    out = json.dumps(obj, indent=2)
    # Foreign-edit surfacing (daemon mode only — legacy has no shared docs): the
    # daemon flagged that ANOTHER client (the human in the GUI, another agent)
    # edited a doc we touched since our last call. Append a note so the model
    # re-checks before overwriting. Additive: legacy mode never buffers any.
    try:
        for fe in cb.consume_foreign_edits():
            out += ("\nnote: {src} edited part {doc} since your last call (rev {rev}, "
                    "via {m}) — re-check its current state before overwriting.").format(
                src=fe.get("source_kind") or "another client",
                doc=fe.get("doc_id"), rev=fe.get("revision"), m=fe.get("method"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _teach_undo_denied(e: "CoreError") -> str:
    """The daemon's bare ``undo_denied`` refusal names alternatives without
    their call shapes — a dead end the model burns completions on. Worse, the
    usual reason an agent reaches for undo ("remove/alter the feature I just
    added") is not a checkpoint problem at all: it is an OP inside the
    modelling batch. The policy is unchanged — this only teaches the way out.
    """
    out = {
        "ok": False,
        "error": str(e),
        "code": e.code,
        "hint": (
            "undo/redo is refused for agent sessions by daemon policy "
            "(multi-writer safety) and there is no way to enable it from "
            "here — retrying undo() will fail identically. What to do instead "
            "depends on what you actually want:\n"
            "1) REMOVE or ALTER a feature you just added (the usual reason "
            "for wanting undo): that is an OP inside the modelling batch, not "
            "a history operation —\n"
            "   add_features(doc_id=..., ops=[{\"op\":\"delete_feature\","
            "\"id\":\"fN\"}])\n"
            "   add_features(doc_id=..., ops=[{\"op\":\"edit_feature\","
            "\"id\":\"fN\",\"params\":{...}}])\n"
            "   get_part(doc_id) / describe_part(doc_id) lists the feature "
            "ids to target.\n"
            "2) ROLL THE WHOLE DOCUMENT back to an earlier state: the daemon "
            "auto-checkpoints before destructive ops via the engine calls "
            "doc_checkpoint(doc_id) -> {\"checkpoint\": \"<id>\"} and "
            "doc_revert(doc_id, checkpoint=\"<id>\") — those are "
            "engine-internal; the tools YOU can call are "
            "plm_timeline(doc_id=\"...\") to list checkpoints, then "
            "plm_rollback(to=\"-1\", doc_id=\"...\") to restore one "
            "(to= a checkpoint id, \"-N\" relative from newest, "
            "\"turn:<n>\", or \"call:<turn>:<i>\")."
        ),
        "alternatives": {
            "remove_a_feature":
                'add_features(ops=[{"op":"delete_feature","id":"fN"}])',
            "alter_a_feature":
                'add_features(ops=[{"op":"edit_feature","id":"fN","params":{...}}])',
            "rollback_document":
                'plm_timeline(doc_id="...") then plm_rollback(to="-1", doc_id="...")',
            "engine_checkpoint_pair":
                'doc_checkpoint(doc_id) -> {"checkpoint": id}; '
                'doc_revert(doc_id, checkpoint=id) — engine-internal, '
                'already run automatically before destructive ops',
        },
    }
    if e.detail is not None:
        out["detail"] = e.detail
    return _dumps(out)


def _err(e: Exception) -> str:
    """Render an engine or connection failure with its structured code and detail."""
    if isinstance(e, CoreError):
        if e.code == "undo_denied":
            return _teach_undo_denied(e)
        out = {"ok": False, "error": str(e)}
        if e.code:
            out["code"] = e.code
        if e.detail is not None:
            out["detail"] = e.detail
        return _dumps(out)
    return _dumps({"ok": False, "error": str(e)})


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="chudware_mcp_")
    os.close(fd)
    return path


_RENDER_PRUNE_DONE = False


def render_output_path(stem: str = "render", suffix: str = ".png") -> str:
    """Durable path for a render the MODEL receives — and therefore the USER must see.

    Deliberately NOT :func:`_tmp`. ``services/agent/agent_events.py::_is_internal_scratch``
    drops any artifact whose basename starts with ``chudware_mcp_`` (the ``_tmp`` prefix),
    so ``derive_artifacts`` returned nothing for every render tool and the
    ``agent.tool_call`` event carried no ``artifacts`` field at all — verified on the live
    run ``run-138660919763973``, where ``render_part`` reported ``artifacts: None``. The
    GUI's inline-image path and the Artifacts panel read that field, so the user never saw
    the images the vision model was looking at. Renders therefore live under
    ``<CACHE>/renders/YYYY-MM-DD/<stem>-<token><suffix>``: outside the scratch filter,
    self-describing on disk, and pruned (see :func:`prune_render_cache`). Do NOT "tidy"
    this back onto a tempfile — that silently re-breaks render visibility.

    `stem` should say what the render is OF (part name + view, e.g. ``mudbucket-iso``); it
    is sanitised to ``[A-Za-z0-9._-]``, collapsed, trimmed to 60 chars, and falls back to
    ``render`` when nothing usable survives.
    """
    from services import paths as _paths  # noqa: PLC0415 — alias-aware root, read late
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(stem or ""))
    safe = re.sub(r"-{2,}", "-", safe).strip("-.")[:60].strip("-.") or "render"
    day = Path(_paths.CACHE) / "renders" / time.strftime("%Y-%m-%d")
    global _RENDER_PRUNE_DONE
    if not _RENDER_PRUNE_DONE:
        # Prune before creating today's directory.  prune_render_cache() removes empty day
        # directories; creating the directory first let the first render path in a process
        # point into a directory the prune had just deleted.
        _RENDER_PRUNE_DONE = True
        prune_render_cache()
    day.mkdir(parents=True, exist_ok=True)
    return os.path.abspath(str(day / f"{safe}-{os.urandom(4).hex()}{suffix}"))


def prune_render_cache(max_age_days: int = 7, max_files: int = 2000) -> int:
    """Bound ``<CACHE>/renders`` and return how many files were deleted.

    Files older than `max_age_days` go first; if the cache is still over `max_files` the
    oldest survivors go until it fits. Empty day directories are removed afterwards. This
    NEVER raises: a permission error or a racing unlink must not be able to break a render.
    """
    removed = 0
    try:
        from services import paths as _paths  # noqa: PLC0415
        root = Path(_paths.CACHE) / "renders"
        if not root.is_dir():
            return 0
        keep: list = []
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
            else:
                keep.append((mtime, p))
        if len(keep) > max_files:
            keep.sort(key=lambda e: e[0])
            for _mtime, p in keep[:len(keep) - max_files]:
                try:
                    p.unlink()
                    removed += 1
                except OSError:
                    pass
        for d in sorted(root.iterdir(), reverse=True):
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — a broken cache must never break a render
        pass
    return removed


def _captioned_render(png: str, doc_id: str, views: list,
                      annotations: Optional[dict] = None,
                      extra: str = "") -> Union[Image, list]:
    """A2/render-caption wiring: pair a render PNG with a one-paragraph deterministic text
    caption (services.mcp.gsl_tools._scene_caption) so a non-vision model reading the tool
    transcript gets SOME signal from a render it cannot see. `mcp` 1.28.1 flattens a list
    return into [ImageContent, TextContent] (func_metadata._convert_to_content), so a vision
    model still gets the image plus the caption is free extra context either way. Best-effort:
    any failure (missing gsl_tools, a bad doc_id) degrades to the bare Image exactly as before.

    annotations: the `info` dict from _annotate_render. When present its self-sufficient block
    (bbox + scale, features in frame, highlights and their reserved colours, the side-palette
    colour key, and what this camera CANNOT see plus the tool to call for it) is appended, so
    the caption alone fully describes the image. `extra` is appended after that."""
    tail = ""
    if annotations:
        try:
            view_name = ("+".join(str(v) for v in views) + " tiled-sheet"
                         if views and len(views) > 1
                         else (str(views[0]) if views else "current"))
            tail = _render_annotation_caption(view_name, annotations)
        except Exception:  # noqa: BLE001
            tail = ""
    if extra:
        tail = (tail + " " + extra).strip() if tail else extra.strip()
    try:
        from services.mcp.gsl_tools import _scene_caption
        caption = _scene_caption(doc_id, views)
        return [Image(path=png), (caption + " " + tail).strip() if tail else caption]
    except Exception:  # noqa: BLE001
        if tail:
            return [Image(path=png), tail]
        return Image(path=png)


def _sanitize_output_path(path: str) -> str:
    """Resolve an output destination and refuse credential/key locations.

    Relative paths use ``resolve_agent_path`` and the selected project root, never
    process cwd. ``~``, ``..``, and symlinks are resolved before the denylist check.
    CAD deliverables deliberately have no workspace-root jail, unlike fs_* tools.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("output path must be a non-empty string")
    from .fs_tools import _is_sensitive_path

    resolved = Path(os.path.realpath(resolve_agent_path(path, "output path")))
    if _is_sensitive_path(resolved):
        raise ValueError(f"output path {path!r} resolves into a credential/key location")
    return str(resolved)


def resolve_agent_path(path: str, arg: str) -> str:
    """Return the absolute path of an agent-supplied file or directory path.

    A relative path is project-root relative: CHUDWARE_PROJECT_DIR, then
    CHUDWARE_FS_ROOT. An explicit root is authoritative and canonicalized without
    widening to a marked ancestor. Absolute paths and ``~`` need no project root.
    This function does not apply a jail; the caller keeps its own containment policy.

    When no project root is known (a headless external MCP client), a relative path is
    refused with a message that tells the caller to pass an absolute path.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{arg} must be a non-empty path (absolute, or relative to the "
                         "project root).")
    expanded = os.path.expanduser(path.strip())
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        start = os.environ.get(key, "").strip()
        if not start:
            continue
        root = Path(start).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(
                f"{arg} {path!r} is relative, but the configured project root "
                f"{key}={start!r} is not an existing directory. Pass an ABSOLUTE {arg}.")
        return os.path.normpath(os.path.join(str(root), expanded))
    raise ValueError(
        f"{arg} {path!r} is relative, but this session has no project root "
        "(CHUDWARE_PROJECT_DIR and CHUDWARE_FS_ROOT are unset), so it has no base to "
        f"resolve against. Pass an ABSOLUTE {arg}, e.g. '/home/you/project/{path.strip()}'.")


# --- never overwrite an artifact: archive the old set first -----------------------------
# A released artifact is EVIDENCE, and evidence is not overwritable. On 2026-09-05 a
# mission re-ran one load case into the folder that already held that case's packet, and
# the previous run's result.json / inputs.json / provenance.json were simply gone: the
# never-overwrite rule lived only in the project's runbook, so the only thing that could
# enforce it was the agent's memory. It lives here now. Every writer of a packet file or
# a solver .vtu calls `archive_before_write()` first, and any existing file is MOVED into
# `<anchor>/superseded_<YYYY-MM-DD>_<short-hash>/` before the new bytes land — a rename,
# so it is free even for a 27 MB .vtu, and the old bytes stay on disk under a name that
# says what they are. The short hash comes from the file that IDENTIFIES the old set
# (result.json for a packet, the .vtu itself), so two different sets archived on the same
# day never collide and re-archiving the same content twice lands in the same directory.
SUPERSEDED_PREFIX = "superseded_"


def file_sha256(path: Union[str, Path]) -> Optional[str]:
    """The sha256 hex digest of a real file, or None — a hash of nothing is not evidence."""
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_before_write(
    targets: Sequence[Union[str, Path]],
    *,
    anchor: Optional[Union[str, Path]] = None,
    hash_source: Optional[Union[str, Path]] = None,
) -> Optional[dict]:
    """Move every EXISTING path in *targets* aside so a writer cannot destroy it.

    targets : the files about to be written. Ones that do not exist are ignored.
    anchor : directory the `superseded_*` folder is created in; defaults to the parent of
             the first existing target (so a lone .vtu is archived beside itself).
    hash_source : the file whose content names the archive; defaults to the first existing
             target. A packet passes its result.json — the identity of the old result.

    Returns None when there was nothing to protect, else
    {archived_to, archived_files, archived_utc, hash_of, sha256}. A name collision inside
    an existing archive directory is suffixed (`result.1.json`) rather than overwritten:
    this function never deletes and never replaces a byte.
    """
    existing = [Path(t) for t in targets if t and Path(t).is_file()]
    if not existing:
        return None
    base = Path(anchor) if anchor is not None else existing[0].parent
    src = Path(hash_source) if hash_source is not None and Path(hash_source).is_file() \
        else existing[0]
    digest = file_sha256(src) or ""
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    dest = base / f"{SUPERSEDED_PREFIX}{stamp}_{digest[:8] or 'nohash'}"
    dest.mkdir(parents=True, exist_ok=True)
    moved: list = []
    for p in existing:
        target = dest / p.name
        n = 1
        while target.exists():
            target = dest / f"{p.stem}.{n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(target))
        moved.append(target.name)
    return {
        "archived_to": str(dest.resolve()),
        "archived_files": sorted(moved),
        "archived_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hash_of": src.name,
        "sha256": digest or None,
    }


# --- live viewport handoff -------------------------------------------------------------
# The MCP runs in its OWN process with its own engine, so the running desktop app can't see
# its doc sessions. To make the AI's part visible in the app's viewport, we publish it as a
# native .step+ doc plus a tiny live.json {rev, step} into <appdata>/<app>/handoff/; the GUI
# polls that control file each frame (ui::pollAiCadHandoff) and opens/refreshes one tab from it.
# <app> follows the app-dir alias rule (Chudware, or the legacy Chudware dir when it already
# exists), matching platform::appdataRoot() so publisher and reader agree.
_PUB: dict = {"rev": None, "docs": {}, "active": None}


def _handoff_dir() -> Path:
    """Where the live viewport handoff is published.

    ``CHUDWARE_HANDOFF_DIR`` overrides (the test suite points it at a tmpdir), matching
    the override tier every other user-data location already offers
    (CHUDWARE_ECAD_LIB_DIR, CHUDWARE_SUBAGENTS_DIR). Without it a test run publishes
    `live-<session>-<doc>.step+` into the REAL user handoff dir — which the desktop GUI
    POLLS and turns into mirror tabs, so running the suite littered a developer's app
    with parts they never made. It also made
    test_live_preview_events_no_handoff_files order-dependent: publishes are deferrable
    onto a worker thread (`_publish_current(_deferred=True)` / `_start_job`), so one
    scheduled by an earlier test lands inside that test's before/after snapshot and
    fails it with "daemon mode must not touch the handoff dir". Observed 2026-07-30 once
    the suite grew enough documents to make the race reliable.
    """
    override = os.environ.get("CHUDWARE_HANDOFF_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get("APPDATA")
    if not base:
        if os.name == "nt":
            base = tempfile.gettempdir()
        else:
            # Match platform/paths_linux.cpp appdataDir() — the old tempdir fallback put
            # the handoff where the GUI never looked on Linux.
            base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    new_root = Path(base) / "Chudware"
    legacy_root = Path(base) / "Chudware"
    root = legacy_root if (not new_root.exists() and legacy_root.exists()) else new_root
    d = root / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _handoff_key(doc_id: str) -> str:
    """A filesystem-safe stem for a doc_id so each open part gets its OWN live-<key>.step+."""
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(doc_id)) or "doc"


def _handoff_doc_name(doc_id: str) -> str:
    """Best-effort human name for a doc so the mirror tab reads sensibly. Never raises."""
    try:
        for e in (cb.call("doc_list", {}) or {}).get("docs", []):
            if isinstance(e, dict) and e.get("doc_id") == doc_id:
                return e.get("name") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _prune_closed_docs() -> None:
    """Reconcile the published set against the engine's LIVE docs — drop any doc_id the engine no
    longer has open. An agent that re-creates, re-opens, saves-round-trips or recovers a part gets
    a NEW doc_id; the SUPERSEDED id would otherwise linger in the control file forever and the app
    would show that part TWICE (a frozen stale mirror tab beside the live one — the doubled-tab
    bug). Best-effort: if doc_list is unavailable we leave the set untouched."""
    try:
        live = {e.get("doc_id") for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                if isinstance(e, dict) and e.get("doc_id")}
    except Exception:  # noqa: BLE001
        return
    if not live:
        return  # never seen an empty list mid-session; treat as "can't tell" and keep entries
    for did in [d for d in _PUB["docs"] if d not in live]:
        _PUB["docs"].pop(did, None)
    if _PUB.get("active") not in _PUB["docs"]:
        _PUB["active"] = next(reversed(_PUB["docs"]), None) if _PUB["docs"] else None


def _supersede_same_name(doc_id: str, name: str) -> None:
    """Close abandoned earlier incarnations of the SAME part.

    When an agent re-creates a part it already had open (a fresh `doc_id` under the same name —
    e.g. it rebuilt `CC-201_Rim` from scratch instead of editing), the old doc stays live in the
    engine forever: it keeps its own mirror tab (the doubled-tab bug) AND pins its OCCT bodies in
    memory. Here we close any OTHER doc WE published under the same non-empty name, provided it is
    neither the just-published doc nor the active one. Scoped to `_PUB["docs"]` so we only ever
    close ids this process itself put on screen — never another agent's part or a doc we never
    owned. Best-effort: a close failure must never break publishing."""
    if not name:
        return  # unknown name — treat every such doc as distinct (never collapse blindly)
    stale = [did for did, v in _PUB["docs"].items()
             if did != doc_id and did != _PUB.get("active") and v.get("name") == name]
    for did in stale:
        try:
            cb.call("doc_close", {"doc_id": did})
        except Exception:  # noqa: BLE001
            pass  # already gone / engine declined — drop it from the mirror set regardless
        _PUB["docs"].pop(did, None)


def _write_handoff_control(d: Path, active: Optional[str]) -> None:
    """Emit BOTH control files, atomically:

      • live.json  (LEGACY {rev, step, doc_id}) — points at `active`. The OLD single-tab host
        keeps mirroring the most-recent part unchanged, so writing the new format can never
        break a running session.
      • docs.json  (NEW {rev, active, docs:[{doc_id, step, rev, name}]}) — the full set of parts
        the agent currently has open. A rebuilt host keeps ONE mirror tab per doc_id from this,
        so a swarm opening many parts leaves them all open for live inspection.
    """
    docs = _PUB["docs"]
    entries = [{"doc_id": did, "step": v["step"], "rev": v["rev"], "name": v.get("name", "")}
               for did, v in docs.items()]
    # Collapse duplicate same-name entries (newest rev wins) so a re-created part shows exactly ONE
    # mirror tab. Belt-and-suspenders behind _supersede_same_name: catches dupes that weren't
    # engine-closable (e.g. name was blank when first published, filled in later). Blank names are
    # NOT collapsed — an unknown name is treated as a distinct doc. The GUI closes any tab whose
    # doc_id stops being advertised here, so dropping stale entries retires their tabs automatically.
    by_name: dict = {}
    collapsed = []
    for e in entries:
        nm = e["name"]
        if not nm:
            collapsed.append(e)
            continue
        prev = by_name.get(nm)
        if prev is None or e["rev"] > prev["rev"]:
            by_name[nm] = e
    collapsed.extend(by_name.values())
    entries = collapsed
    # Keep `active` pointing at a surviving entry (remap to the newest same-name doc if the active
    # doc_id was collapsed away, so the app still focuses the right part).
    advertised = {e["doc_id"] for e in entries}
    if active is not None and active not in advertised:
        act_name = docs.get(active, {}).get("name", "")
        active = by_name.get(act_name, {}).get("doc_id", active) if act_name else active
    # Compound the advertised keys with our session id. The host uses doc_id as its per-tab key;
    # engine doc_ids reset per process, so without this two swarm members would both advertise
    # "doc-cpp00000001" and the host would fuse their tabs. The agent never reads these back, so an
    # opaque compound key is safe. Ops still use the bare engine doc_id (this is display-only).
    cid = lambda did: f"{_SESSION_ID}-{did}"
    frag_entries = [{"doc_id": cid(e["doc_id"]), "step": e["step"],
                     "rev": e["rev"], "name": e["name"]} for e in entries]
    frag_active = cid(active) if active else None
    # Write OUR OWN fragment (uniquely named → zero cross-process write contention). The single
    # docs.json the host reads is then rebuilt by merging every LIVE fragment, so concurrent
    # subagents each contribute their parts instead of clobbering the shared file (last-writer-wins).
    frag = {"rev": _PUB["rev"], "active": frag_active, "docs": frag_entries,
            "session": _SESSION_ID, "pid": _HANDOFF_PID, "updated_at": time.time()}
    tmp = d / f"docs-{_SESSION_ID}.json.tmp"
    tmp.write_text(json.dumps(frag))
    os.replace(tmp, d / f"docs-{_SESSION_ID}.json")
    _merge_handoff_fragments(d)


def _merge_handoff_fragments(d: Path) -> None:
    """Rebuild docs.json (and legacy live.json) by unioning every LIVE `docs-<session>.json`
    fragment. Idempotent over the fragment set — concurrent mergers converge because they see the
    same files — so no lock is needed; a transient partial merge self-heals on the next publish.
    Dead-process fragments are skipped and unlinked so a crashed swarm member's tabs disappear."""
    try:
        now = time.time()
        merged: list = []
        active: Optional[str] = None
        active_at = -1.0
        top_rev = 0
        live_sessions: set = set()  # sessions with a still-alive fragment (their step files are kept)
        for fp in sorted(d.glob("docs-*.json")):
            try:
                m = json.loads(fp.read_text())
            except Exception:  # noqa: BLE001 — half-written / corrupt; skip this round
                continue
            pid = int(m.get("pid", 0) or 0)
            updated = float(m.get("updated_at", 0.0) or 0.0)
            alive = _pid_alive(pid) and (now - updated) <= _HANDOFF_FRAG_TTL_S
            if not alive:
                # Owner is gone — retire its fragment (and mirror files) so its tabs close.
                if pid and not _pid_alive(pid):
                    try:
                        fp.unlink(missing_ok=True)
                        for step in d.glob(f"live-{m.get('session','')}-*.step+"):
                            step.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            live_sessions.add(str(m.get("session", "")))
            merged.extend(m.get("docs", []))
            top_rev = max(top_rev, int(m.get("rev", 0) or 0))
            if m.get("active") and updated > active_at:
                active, active_at = m.get("active"), updated
        # Orphan sweep: step files whose owning session has NO live fragment leak forever
        # (the dead-fragment cleanup above only fires while the fragment still exists; a
        # session whose fragment was already retired — or a crash before atexit — strands
        # its live-<session>-*.step+). Also collects legacy pre-session-prefix files
        # (live-doc-*.step+, no session segment). Only touch files older than the TTL so a
        # file a live process just wrote — before its fragment lands — is never raced away.
        try:
            for step in d.glob("live-*.step+"):
                stem = step.name[len("live-"):]  # "<session>-<doc>.step+" or "<doc>.step+"
                seg = stem.split("-", 1)
                sess = seg[0] if len(seg) == 2 and seg[0] and not seg[0].startswith("doc") else ""
                if sess and sess in live_sessions:
                    continue  # owned by a live session — keep
                try:
                    if (now - step.stat().st_mtime) <= _HANDOFF_FRAG_TTL_S:
                        continue  # too fresh — a publisher may be mid-write; leave it
                    step.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass
        payload = {"rev": top_rev, "active": active, "docs": merged}
        tmp = d / "docs.json.tmp"
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, d / "docs.json")
        # live.json (legacy single-doc host) — point at the active merged entry.
        act = next((e for e in merged if e["doc_id"] == active), None) or (merged[-1] if merged else None)
        if act is not None:
            tmp = d / "live.json.tmp"
            tmp.write_text(json.dumps({"rev": top_rev, "step": act["step"], "doc_id": act["doc_id"]}))
            os.replace(tmp, d / "live.json")
    except Exception:  # noqa: BLE001 — publishing is best-effort; never break an op on mirror IO
        pass


# Publish throttling: every publish serializes the FULL doc to a .step+ (50-200ms on a
# mid-size part), and mutation-heavy agents call _publish_current after every op batch.
# Two guards, both per-doc:
#   • revision-skip — the engine's monotonic doc revision hasn't moved since our last
#     publish (e.g. an activation republish), so the .step+ on disk is already current.
#   • burst-coalescing — publishes inside CHUDWARE_PUBLISH_MIN_INTERVAL_S of the previous
#     one are deferred to ONE trailing publish (daemon Timer), so a rapid edit burst
#     writes the mirror once at the end instead of per edit. 0 disables.
_PUBLISH_MIN_INTERVAL_S: float = float(os.environ.get("CHUDWARE_PUBLISH_MIN_INTERVAL_S", "0.25") or 0.25)
_PUB_THROTTLE: dict = {}  # doc_id -> {"revision": int|None, "t": float, "timer": Timer|None, "last": dict|None}


def _engine_doc_revision(doc_id: str) -> Optional[int]:
    """The engine's monotonic revision for `doc_id` (cheap metadata call), or None."""
    try:
        for e in (cb.call("doc_list", {}) or {}).get("docs", []):
            if isinstance(e, dict) and e.get("doc_id") == doc_id:
                rev = e.get("revision")
                return int(rev) if isinstance(rev, (int, float)) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _publish_current(doc_id: str, _deferred: bool = False) -> Optional[dict]:
    """Publish `doc_id` to the handoff dir so the open app shows it. Best-effort; never raises.

    Each open doc is saved to its OWN live-<key>.step+, and the control files list every open
    doc — so when an AI agent (built-in or an MCP-driven cowork swarm) opens several parts, the
    desktop app keeps a tab open for each instead of overwriting a single live view.

    Daemon mode (the default since the Phase-8 flip): the GUI shares the daemon's
    docs live and IGNORES handoff files, so writing them would only burn a full
    doc serialization per op batch and leave a stale second source of truth —
    skip entirely. The legacy path (explicit CHUDWARE_ENGINE_DAEMON=0, or daemon
    transport fallback) keeps publishing; the whole publisher is deleted in N+1.
    """
    if cb.active_daemon_mode():
        return None
    try:
        import threading as _t

        st = _PUB_THROTTLE.setdefault(
            doc_id, {"revision": None, "t": 0.0, "timer": None, "last": None})
        engine_rev = _engine_doc_revision(doc_id)
        # Revision-skip: nothing changed since the last publish of this doc — the mirror
        # file and control entry are already current. (Never skips a doc we've not yet
        # published this process: st["revision"] starts as None.)
        if (engine_rev is not None and st["revision"] is not None
                and engine_rev == st["revision"] and doc_id in _PUB["docs"]):
            return st["last"]
        # Burst-coalescing: too soon after the previous publish — arm ONE trailing timer
        # that flushes the newest state after the interval, and report the prior publish.
        now = time.time()
        if (not _deferred and _PUBLISH_MIN_INTERVAL_S > 0
                and now - st["t"] < _PUBLISH_MIN_INTERVAL_S):
            if st["timer"] is None:
                delay = max(0.01, _PUBLISH_MIN_INTERVAL_S - (now - st["t"]))
                timer = _t.Timer(delay, lambda: (_PUB_THROTTLE.get(doc_id, {}).update(timer=None),
                                                 _publish_current(doc_id, _deferred=True)))
                timer.daemon = True
                timer.name = f"chudware-publish-{doc_id}"
                st["timer"] = timer
                timer.start()
            return st["last"]
        st["t"] = now
        d = _handoff_dir()
        key = _handoff_key(doc_id)
        # Session-prefix the mirror file: engine doc_ids reset per process, so two concurrent MCP
        # servers both holding doc-cpp00000001 would otherwise write the SAME live-doc-cpp00000001
        # .step+ and clobber each other's mirror. _SESSION_ID makes every process's files disjoint.
        step = d / f"live-{_SESSION_ID}-{key}.step+"
        cb.call("doc_save", {"doc_id": doc_id, "path": str(step), "preserve_path": True})
        if _PUB["rev"] is None:
            # Seed from any existing control file so the rev keeps rising across MCP restarts.
            try:
                _PUB["rev"] = int(json.loads((d / "docs.json").read_text()).get("rev", 0))
            except Exception:
                try:
                    _PUB["rev"] = int(json.loads((d / "live.json").read_text()).get("rev", 0))
                except Exception:
                    _PUB["rev"] = 0
        _PUB["rev"] += 1
        name = _handoff_doc_name(doc_id)
        _PUB["docs"][doc_id] = {"rev": _PUB["rev"], "step": str(step), "name": name}
        _PUB["active"] = doc_id
        _prune_closed_docs()   # evict superseded/closed doc_ids so a part never mirrors twice
        _supersede_same_name(doc_id, name)  # close abandoned older docs of the same part name
        _write_handoff_control(d, doc_id)
        out = {"shown_in_app": True, "rev": _PUB["rev"], "open_docs": len(_PUB["docs"])}
        st["revision"] = engine_rev
        st["last"] = out
        return out
    except Exception:
        return None


def sweep_stale_handoff() -> None:
    """One-shot startup sweep: re-merge the handoff fragments so a previous publisher
    that CRASHED (atexit never dropped its fragment) stops being advertised now instead
    of after the TTL. The merge already drops dead-PID fragments and orphaned step
    files; it just needs someone alive to run it. Best-effort; never raises."""
    try:
        _merge_handoff_fragments(_handoff_dir())
    except Exception:  # noqa: BLE001
        pass


def _unpublish(doc_id: str) -> None:
    """Drop `doc_id` from the handoff set (called on close_part) so its mirror tab can close.
    Best-effort; never raises. Leaves the per-doc live-<key>.step+ on disk (cheap; overwritten
    on reuse) — only the control files decide what the app keeps open."""
    try:
        # Drop throttle state (cancel any pending trailing publish) so a re-created doc
        # with the same engine doc_id can never be revision-skipped or ghost-published.
        st = _PUB_THROTTLE.pop(doc_id, None)
        if st and st.get("timer") is not None:
            try:
                st["timer"].cancel()
            except Exception:  # noqa: BLE001
                pass
        if doc_id not in _PUB["docs"]:
            return
        del _PUB["docs"][doc_id]
        d = _handoff_dir()
        if _PUB["rev"] is None:
            _PUB["rev"] = 0
        _PUB["rev"] += 1
        if _PUB.get("active") == doc_id:
            _PUB["active"] = next(reversed(_PUB["docs"]), None) if _PUB["docs"] else None
        _write_handoff_control(d, _PUB.get("active"))
    except Exception:  # noqa: BLE001
        pass


def quiesce_publishes(timeout_s: float = 5.0) -> None:
    """Cancel every armed trailing publish and wait out the ones already running.

    Burst-coalescing arms a daemon Timer per doc (`chudware-publish-<doc_id>`) whose body
    is a WHOLE publish: a full `doc_save` of the document plus the `doc_list`/`doc_close`
    calls behind `_handoff_doc_name`, `_prune_closed_docs` and `_supersede_same_name`. The
    only cancel in the code is `_unpublish`'s, which covers one doc on `close_part` — so
    otherwise a trailing publish outlives whatever asked for it and keeps issuing engine
    calls on its own thread, minutes later if the bridge is busy.

    Two callers need it stopped on demand. `_cleanup_handoff_fragment` drops this
    session's fragment and re-merges so survivors close our tabs at once; a publish still
    in flight rewrites that fragment immediately afterwards and re-advertises a dead
    publisher's documents. And a caller that installs its own engine stand-in (the test
    suites' fake bridges) otherwise sees a foreign doc_save arrive in its own call log.

    Bounded and best-effort: a wedged engine call must never hold up a shutdown path.
    """
    import threading as _t  # noqa: PLC0415 — see _publish_current

    for st in list(_PUB_THROTTLE.values()):
        timer = st.get("timer")
        if timer is None:
            continue
        st["timer"] = None
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass
    deadline = time.monotonic() + max(0.0, timeout_s)
    for thread in _t.enumerate():
        if not thread.name.startswith("chudware-publish-"):
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            thread.join(remaining)
        except Exception:  # noqa: BLE001
            pass


# A per-process session id stamped onto every autosave. Recovery is scoped to the
# SINGLE most-recent PRIOR session (the one that crashed), not the whole autosave
# history, so a restart doesn't drag in 19 unrelated old docs (Bug S-1).
import uuid as _uuid_session  # noqa: E402
_SESSION_ID = _uuid_session.uuid4().hex[:12]
_HANDOFF_PID = os.getpid()
# A handoff fragment from another MCP process is included in the merged docs.json only while its
# owning process is alive (PID probe) and its file is fresh. PID-liveness closes a restarted
# server's stale tabs immediately (no duplicate-across-restart window); the mtime TTL is a backstop
# for the rare PID-reuse case.
_HANDOFF_FRAG_TTL_S = float(os.environ.get("CHUDWARE_HANDOFF_TTL_S", "1800") or 1800)


def _pid_alive(pid: int) -> bool:
    """True if `pid` is alive. Inaccessible owners are assumed alive."""
    if not pid or pid < 0:
        return True  # unknown owner (legacy fragment) — don't evict on liveness grounds
    if os.name == "nt":
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID no longer exists
        try:
            # WAIT_TIMEOUT = running; WAIT_OBJECT_0 = exited; WAIT_FAILED = unknown.
            return kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 — can't tell -> fall back to mtime TTL
        return True


import atexit as _atexit  # noqa: E402


@_atexit.register
def _cleanup_handoff_fragment() -> None:
    """On clean shutdown drop our fragment and re-merge so survivors close our tabs at once
    (rather than waiting for the PID probe on their next publish). Best-effort.

    Pending publishes are stopped FIRST: a trailing publish that fires after the unlink
    rewrites `docs-<session>.json` and puts this dead publisher's documents back on the
    survivors' list, which is exactly what the unlink exists to prevent."""
    try:
        quiesce_publishes(2.0)
        d = _handoff_dir()
        (d / f"docs-{_SESSION_ID}.json").unlink(missing_ok=True)
        _merge_handoff_fragments(d)
    except Exception:  # noqa: BLE001
        pass
# Autosaves older than this are pruned at startup so the directory can't grow forever.
_AUTOSAVE_MAX_AGE_DAYS = float(os.environ.get("CHUDWARE_AUTOSAVE_MAX_AGE_DAYS", "7") or 7)


def _autosave_dir() -> Path:
    """Return (and create) the autosave directory.

    Resolution order:
      1. CHUDWARE_AUTOSAVE_DIR env var (tests use this for hermeticity).
      2. services.paths.appdata_root() / "mcp_autosave"  — XDG-aware on Linux,
         %APPDATA%\\Chudware on Windows.  Matches the C++ convention in paths.cpp.
      3. tempfile.gettempdir() / "Chudware" / "mcp_autosave" (last-resort fallback).

    NOTE: The prior implementation fell back to os.environ.get("APPDATA") which
    silently resolved to /tmp on Linux (APPDATA unset), scattering autosave files
    outside the Chudware data tree.  Fixed to use appdata_root() (XDG-aware).
    """
    override = os.environ.get("CHUDWARE_AUTOSAVE_DIR")
    if override:
        d = Path(override)
    else:
        try:
            from services import paths as _paths  # noqa: PLC0415
            d = _paths.ROOT / "mcp_autosave"  # alias-aware root, not the always-new dir
        except Exception:  # noqa: BLE001
            d = Path(tempfile.gettempdir()) / "Chudware" / "mcp_autosave"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: Autosave COUNT cap. The age-based prune below was the only bound, and it never
#: fires in practice: the worker is forked PER TURN (core/agent/run.cpp), so every
#: turn gets a fresh _SESSION_ID and writes a brand-new session-prefixed snapshot for
#: every doc it touches — all of them younger than the age cutoff. One day of use
#: left 39,162 files / 3.6 GB under mcp_autosave, 19,581 distinct stems in seven
#: hours. These are crash-recovery snapshots, not user deliverables (those go to the
#: project folder via _project_save), so the directory is also bounded by count.
_AUTOSAVE_MAX_FILES = int(os.environ.get("CHUDWARE_AUTOSAVE_MAX_FILES") or 2000)

#: Autosave SESSION retention. The count cap above kept the pile at exactly its cap but
#: could not bound how many distinct SESSIONS it spanned: 2,000 surviving snapshots on
#: this workstation were spread over 512 sessions, and the single largest session held
#: 270 docs. That is what makes recovery a footgun (see _RECOVER_MAX_DOCS). The count
#: cap now evicts whole sessions oldest-first, and never touches the newest
#: _AUTOSAVE_KEEP_SESSIONS of them nor this process's own.
#:
#: WHY WHOLE SESSIONS: an autosave snapshot IS potentially-unsaved user work — there is
#: no dirty/clean flag in the sidecar, so we cannot tell debris from a part the user
#: never saved. The only honest safety rule is therefore structural: a session is either
#: kept ENTIRELY or dropped ENTIRELY, so a retained session is always fully recoverable
#: and a prune can never leave one half-restorable. The old per-doc_id protection was
#: actively unsafe here: doc_ids ("doc-cpp00000001") are engine-allocated and RESET per
#: process, so "keep the newest snapshot of each doc_id" happily deleted session A's
#: part because session B happened to reuse the same id for a different one.
_AUTOSAVE_KEEP_SESSIONS = int(os.environ.get("CHUDWARE_AUTOSAVE_KEEP_SESSIONS") or 12)

#: How many docs a single recovery call may reopen. recover_autosaved used to open EVERY
#: snapshot in the recovered scope: measured on this workstation's real pile that is 270
#: docs for scope="recent" and 1,803 for scope="all", in ONE turn. The engine then holds
#: hundreds of near-identically-named parts, so the very next doc-scoped call cannot
#: resolve "the part" — _doc() either refuses with a doc_ambiguous naming 108 candidates
#: or silently adopts whichever snapshot happened to sort last. Recovery means "give me
#: back what I was working on", not "reconstitute the whole history", so it takes the
#: newest N and REPORTS the rest instead of opening them.
_RECOVER_MAX_DOCS = int(os.environ.get("CHUDWARE_RECOVER_MAX_DOCS") or 8)

#: What the last recover_autosaved() call in this process reopened, newest first —
#: [{doc_id, name, original_doc_id}]. _doc() quotes it back when a handle fails, so a
#: post-recovery "which part?" refusal names the recovered candidates instead of only
#: the raw open-document list (in which the recovered docs are indistinguishable).
_RECOVERED: list = []

#: How many docs the last recover_autosaved() deliberately LEFT on disk because of the
#: bound above. Nothing is deleted — a call with a bigger `limit` reaches them.
_RECOVER_SKIPPED = 0


def _autosave(doc_id: Optional[str], name: Optional[str] = None) -> None:
    """Persist a doc to a recoverable .step+ (plus a sidecar tagging the session) so a
    server restart — e.g. after a client timeout killed it — can reload THIS session's
    work instead of dropping every open document. Best-effort; never raises.

    NO-OP IN DAEMON MODE: chudware-hostd owns the one recovery scheduler and orphan
    tier there. Keep the guard at this single write, not just aggregate entry points,
    so direct tool callers cannot create a competing recovery authority. Standalone
    snapshots use preserve_path so they never rebind the document's deliverable/base
    directory or clear its dirty flag."""
    if not doc_id or cb._daemon_mode():
        return
    try:
        # Resolve the doc's real name from the engine when the caller didn't pass one, so
        # the sidecar (and therefore recover_parts) never reports an empty name (Bug N-6).
        if not name:
            try:
                for d in (cb.call("doc_list", {}) or {}).get("docs", []):
                    if isinstance(d, dict) and d.get("doc_id") == doc_id:
                        name = d.get("name") or ""
                        break
            except Exception:  # noqa: BLE001
                pass
        # Session-prefix the filename: doc_ids are engine-allocated per-process ("doc-cpp0000000N")
        # and RESET across processes, so two concurrent MCP servers both holding doc-cpp00000001
        # would otherwise write the SAME mcp_autosave/doc-cpp00000001.step+ and silently clobber
        # each other's real geometry (unrecoverable data loss). The real doc_id is preserved in the
        # sidecar so recovery can still restore the agent's original handle via _DOC_ALIASES.
        stem = f"{_SESSION_ID}-{doc_id}"
        path = _autosave_dir() / f"{stem}.step+"
        cb.call("doc_save", {"doc_id": doc_id, "path": str(path), "preserve_path": True})
        meta = {"session": _SESSION_ID, "doc_id": doc_id, "name": name or "",
                "saved_at": time.time()}
        (_autosave_dir() / f"{stem}.json").write_text(json.dumps(meta))
        _prune_autosaves()  # once per process, after our own snapshot is safely down
    except Exception:  # noqa: BLE001
        pass


def _project_save_stem(name: Optional[str], doc_id: str) -> str:
    """Sanitize a doc name into a filesystem-safe stem, mirroring the host's rule:
    spaces -> '_', keep alnum/'_'/'-', drop everything else. Falls back to the
    doc_id when the name yields an empty stem.

    A doc NAMED for a file ("lexus_f_keychain.step" — agents do this) first has that
    CAD extension REMOVED, matching save_part's replace-don't-stack rule. Without it
    the dot was merely dropped as a disallowed character, so the deliverable landed at
    "lexus_f_keychainstep.step+" while save_part wrote "lexus_f_keychain.step+" — two
    files, two PLM items, one part."""
    raw = name or ""
    stem_part, dot, ext = raw.rpartition(".")
    if dot and stem_part:
        from services.mcp.cad_doc_tools import _CAD_EXTS_FOR_SAVE  # noqa: PLC0415 — cycle
        if ext.lower() in _CAD_EXTS_FOR_SAVE or ext.lower() == "step+":
            raw = stem_part
    stem = re.sub(r"\s+", "_", raw)
    stem = "".join(c for c in stem if c.isalnum() or c in "_-")
    return stem or doc_id


# doc_id -> the .step+ file the HARNESS chose and wrote for it (new_part / add_features
# auto project-save, bare save_part into CHUDWARE_PROJECT_SAVE_DIR). Nobody asked for that
# location, so when the same document is later saved to an explicit path the old file is a
# stale duplicate the harness itself created. Live 2026-09-24: five `<Stem>.step+` copies
# were left in the user's project root beside the real files the agent saved under CAD/.
_HARNESS_SAVED_PATHS: dict = {}


def note_harness_saved_path(doc_id: Optional[str], path: Optional[str]) -> None:
    if doc_id and path:
        _HARNESS_SAVED_PATHS[doc_id] = os.path.abspath(path)


def retire_harness_copy(doc_id: Optional[str], new_path: Optional[str],
                        prior_path: Optional[str]) -> Optional[str]:
    """After `doc_id` was saved to an explicit `new_path`, delete the harness-chosen copy it
    moved away from — only when that copy is the file the doc lived in until this save.
    Returns the removed path, or None. Never raises."""
    if not doc_id or not new_path:
        return None
    old = _HARNESS_SAVED_PATHS.pop(doc_id, None)
    if (not old or not prior_path or old != os.path.abspath(prior_path)
            or old == os.path.abspath(new_path)):
        return None
    try:
        if os.path.isfile(old):
            os.remove(old)
            return old
    except OSError:
        pass
    return None


def _new_project_deliverable_path(root: str, name: Optional[str], doc_id: str,
                                  suffix: str, open_docs: Sequence[dict] = ()) -> str:
    """Choose a first destination without replacing any file the document did not own."""
    directory = Path(os.path.abspath(os.path.expanduser(root)))
    directory.mkdir(parents=True, exist_ok=True)
    stem = _project_save_stem(name, doc_id)
    path = directory / f"{stem}{suffix}"
    index = 2
    while os.path.lexists(path) or user_doc_open_at(open_docs, str(path), doc_id):
        path = directory / f"{stem}_{index}{suffix}"
        index += 1
    return str(path)


def _native_deliverable_path(entry: dict) -> Optional[str]:
    """Use an established file or authored native edit, never study-only provenance."""
    current = entry.get("path")
    if not current and entry.get("native_source_edited") is True:
        current = entry.get("native_source_path")
    if (isinstance(current, str) and current.lower().endswith(".step+")
            and not _is_recovery_path(current)):
        return current
    return None


def _default_deliverable_path(doc_id: str, suffix: str) -> str:
    """Choose the destination for a save/export whose caller omitted ``path``.

    Agent turns configure ``CHUDWARE_PROJECT_SAVE_DIR`` to the user's working folder.
    A live 2026-08-05 replay instead let bare save_part fall through to ``_tmp``; that
    misplaced the deliverable and also made it invisible to GUI artifacts because the
    ``chudware_mcp_`` basename is deliberately classified as internal scratch. External
    headless MCP clients do not configure a project folder, so only that case keeps the
    historical temp-file fallback.

    An editable document already backed by a real ``.step+`` saves IN PLACE.
    Detached imports retain source-save behavior after design edits, but study-only
    scratch gets its own unused filename; provenance alone never authorizes overwrite.
    """
    root = root_value("CHUDWARE_PROJECT_SAVE_DIR")
    if not root:
        return _tmp(suffix)

    name = None
    entry = {}
    open_docs = []
    try:
        open_docs = (cb.call("doc_list", {}) or {}).get("docs", [])
        entry = next((e for e in open_docs
                      if isinstance(e, dict) and e.get("doc_id") == doc_id), {})
        name = entry.get("name")
    except Exception:  # noqa: BLE001 — the doc_id remains a stable filename fallback
        pass
    if suffix == ".step+":
        current = _native_deliverable_path(entry)
        if current:
            return current
        return _new_project_deliverable_path(root, name, doc_id, suffix, open_docs)
    directory = Path(os.path.abspath(os.path.expanduser(root)))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / f"{_project_save_stem(name, doc_id)}{suffix}")


def _is_recovery_path(path: str) -> bool:
    """Recognize snapshot directories, never a deliverable by filename alone."""
    from services import paths as _paths  # noqa: PLC0415

    roots = [_autosave_dir(), _handoff_dir()]
    roots.extend(root / "hostd_autosave" for root in _paths.data_roots())
    if os.name != "nt":
        # hostd's POSIX stateDir follows XDG even when APPDATA is set.
        roots.append(Path(os.environ.get("XDG_DATA_HOME") or
                          os.path.expanduser("~/.local/share")) / "Chudware" /
                     "hostd_autosave")
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _project_save(doc_id: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """Refresh the doc's real native deliverable, or choose its first project path.

    An established native destination owns the assembly's relative-source base directory;
    automatic saves must not relocate it to <CHUDWARE_PROJECT_SAVE_DIR>/<safe_stem>.step+.
    Only new/detached documents and recovery snapshots receive that default destination.
    A document the user opened (hostd origin_kind "gui") is never written here: the agent
    edits it live in the user's tab, and only an explicit save (the user's, or the agent's
    save_part) writes the user's file. hostd's autosave snapshots it meanwhile.
    Unset project configuration remains a no-op. Save errors are best-effort, except
    engine connection loss: callers must not claim durable success."""
    if not doc_id:
        return None
    try:
        root = root_value("CHUDWARE_PROJECT_SAVE_DIR")
        if not root:
            return None
        # Always inspect live metadata, even when a caller supplied the name. A failed
        # lookup must fail the best-effort save, not guess a path and move the document.
        open_docs = (cb.call("doc_list", {}) or {}).get("docs", [])
        entry = next((e for e in open_docs
                      if isinstance(e, dict) and e.get("doc_id") == doc_id), {})
        if entry.get("origin_kind") == "gui":
            # c259: every add_features batch rewrote the user's open base.step+ (cache.*
            # included) and cleared its dirty flag, so a broken intermediate state became
            # the file on disk with no unsaved marker and no hostd crash snapshot.
            return None
        # Non-study authored edits preserve source-save behavior, but another GUI
        # document owns its file. Preserve our work separately instead of replacing it.
        current = _native_deliverable_path(entry)
        chosen_by_harness = False
        if current and user_doc_open_at(open_docs, current, doc_id) is None:
            path = Path(current)
        else:
            path = Path(_new_project_deliverable_path(
                root, name or entry.get("name"), doc_id, ".step+", open_docs))
            chosen_by_harness = True
        r = cb.call("doc_save", {"doc_id": doc_id, "path": str(path)})
        written = r.get("path", str(path)) if isinstance(r, dict) else str(path)
        if not os.path.exists(written):
            return None
        if chosen_by_harness:
            note_harness_saved_path(doc_id, written)
        try:  # optional Box cloud mirror — spools the project only; uploads drain later
            from services.plm import box_mirror
            box_mirror.notify_saved_path(written)
        except Exception:  # noqa: BLE001 — a mirror must never fail a save
            pass
        return written
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) == "engine_connection_lost":
            raise
        return None


# ---------------------------------------------------------------------------
# Terminal save sweep — nothing the agent built is left "potentially lost".
# ---------------------------------------------------------------------------
# hostd retires an abandoned doc to the crash-recovery ORPHAN tier when
# `path.empty() || dirty || isAutosavePath(path)` (core/hostd/server.cpp::gcCloseDoc), and
# everything in that tier is offered back through the GUI's "Recover unsaved parts?"
# prompt. Nothing closes the documents a run built — services/agent/loop.py's
# `_terminal_autosave_close_docs` was removed and the C++ mission path only
# doc_saves the one doc a DAG node is on — so every agent doc without a real backing file
# eventually became a "part potentially lost" row. Measured on the reporting user's
# machine 2026-08-05: eight retired in the same second at the 30-min agent GC grace
# (`CHUDWARE_DOC_GC_AGENT_GRACE_S`), then all eight opened as tabs when he accepted.
#
# The fix is to SAVE, not to close. Closing would fan out doc.closed and destroy the GUI
# mirror tab the user browses after a run — the whole reason agent-origin docs get the
# long GC floor in the first place. A doc saved to a real project file is simply closed
# when its grace expires, with nothing left behind to "recover".
_TOUCHED_DOCS: list = []


def note_touched_doc(doc_id: Optional[str]) -> None:
    """Record a doc this process created or resumed, for the terminal sweep.

    A list, not a set: order is the order the run built them, which is the order the
    sweep reports and the order a human reads. Successful close evicts the document.
    """
    claim_process_doc(doc_id)   # creating/resuming a doc is also a cross-process claim on it
    if doc_id and doc_id not in _TOUCHED_DOCS:
        _TOUCHED_DOCS.append(doc_id)


# doc_id -> the doc's path when this process discarded it with close_part(discard=True) but
# hostd kept it alive (detached: an agent run or another client still holds it). The
# terminal sweep must honour the discard instead of saving the dropped state over the file
# (live 2026-09-24: the stale "Wheel End" doc the agent discarded was swept over the
# rebuilt Wheel End it had just saved to the same path). Keyed with the path because doc
# ids are reused across engine restarts.
_DISCARDED_DOCS: dict = {}


def note_discarded_doc(doc_id: Optional[str]) -> None:
    if not doc_id:
        return
    path = None
    try:
        path = next((e.get("path") for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                     if isinstance(e, dict) and e.get("doc_id") == doc_id), None)
    except Exception:  # noqa: BLE001 — an unknown path still records the discard intent
        pass
    _DISCARDED_DOCS[doc_id] = path


def user_doc_open_at(open_docs: list, path: str, doc_id: Optional[str]) -> Optional[dict]:
    """The `doc_list` entry of a document OTHER than `doc_id` that the user opened (hostd
    origin_kind "gui") and whose file is `path` — or None.

    save_part refuses to write another document over that file: the user's tab would keep
    the old model (none of ITS revisions changed, so nothing reaches the viewport) and a
    later save of the tab would put the old model back over the new file. A user doc the
    agent close_part(discard=True)-ed (hostd degraded the close to a detach) is not held.
    """
    if not path:
        return None
    want = os.path.realpath(path)
    for e in open_docs or []:
        if not isinstance(e, dict) or e.get("origin_kind") != "gui":
            continue
        did, p = e.get("doc_id"), e.get("path")
        if not did or did == doc_id or not isinstance(p, str) or not p:
            continue
        if did in _DISCARDED_DOCS and _DISCARDED_DOCS[did] == p:
            continue
        if os.path.realpath(p) == want:
            return e
    return None



def _needs_project_save(entry: dict) -> bool:
    """True when this doc holds content that is NOT yet in a real user file.

    Mirrors hostd's own retire test (`gcCloseDoc`): no path, unsaved edits, or a path
    that is only a crash-recovery autosave. A doc with no features, components or
    studies is skipped — hostd purges those instead of retiring them, so saving one
    would create a file for nothing. The engine's `empty` flag counts geometry only,
    so a study-only document (a hydraulic circuit, piping network, vessel or duct
    sizing) reads `empty` yet carries the user's whole design: it is saved.
    """
    if not ((entry.get("feature_count") or 0) or (entry.get("component_count") or 0)):
        try:
            studies = (cb.call("sim_study_list", {"doc_id": entry.get("doc_id")})
                       or {}).get("studies")
        except Exception:  # noqa: BLE001 — an unreadable study list reads as none
            studies = None
        if not studies:
            return False
    elif entry.get("empty"):
        return False
    path = entry.get("path") or ""
    if not isinstance(path, str) or not path.strip():
        return True
    if entry.get("dirty"):
        return True
    try:
        return _is_recovery_path(path)
    except OSError:
        return False


def _reproducible_import_sources(entry: dict) -> Optional[list]:
    """Source files that rebuild this doc exactly, or None when it holds authored work.

    The engine lists native source paths for unchanged detached opens, and neutral
    source paths while every feature remains an unmodified import. Any authored
    change empties it. Such a doc is scratch — an agent's body-roster probe import — and
    writing it into the project root duplicated a source file the user already has
    (2026-09-24, `_frame624_2026-09-07.step+`). hostd applies the same test and simply
    closes it at GC instead of retiring it to the recovery tier (hostd/autosave.cpp
    docListRowIsReproducible). A missing source leaves it authored-equivalent: saved.
    """
    sources = entry.get("reproducible_from")
    if not isinstance(sources, list) or not sources:
        return None
    for src in sources:
        if not (isinstance(src, str) and os.path.isabs(src) and os.path.isfile(src)):
            return None
        if entry.get("native_source_path") == src and _is_recovery_path(src):
            return None  # a recovered snapshot still needs a real project deliverable
    return list(sources)


def save_touched_docs() -> dict:
    """Save every doc this run built that has no real user file yet. Never raises.

    Scope is deliberately narrow: docs this process created/resumed (`_TOUCHED_DOCS`)
    plus open agent/mcp docs with no writer outside this worker's writer group. An
    untouched doc held by another writer may belong to another workspace. A doc whose
    `origin_kind` is `gui` is the USER's own and is never touched, however unsaved it
    looks — that is exactly the work the recovery prompt exists to protect.

    A missing `CHUDWARE_PROJECT_SAVE_DIR` is benign only when there is no authored
    agent document waiting to be delivered. If a touched document needs a real user
    file, `persistence_failure` identifies the affected documents so the turn cannot
    honestly report durable success.
    """
    out: dict = {
        "saved": [],
        "skipped": [],
        "errors": [],
        "persistence_failure": None,
    }
    try:
        rows = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                if isinstance(e, dict) and e.get("doc_id")]
    except Exception as exc:  # noqa: BLE001 — a terminal sweep never fails a turn
        out["errors"].append(f"doc_list: {type(exc).__name__}: {exc}")
        return out

    pending: list[dict] = []
    for e in rows:
        did = e["doc_id"]
        origin = e.get("origin_kind")
        ours = did in _TOUCHED_DOCS or (
            origin in ("mcp", "agent") and not e.get("other_writer")
        )
        if not ours or origin == "gui":
            continue
        if did in _DISCARDED_DOCS and _DISCARDED_DOCS[did] == e.get("path"):
            out["skipped"].append({"doc_id": did, "name": e.get("name"),
                                   "reason": "discarded"})
            continue
        if not _needs_project_save(e):
            out["skipped"].append({"doc_id": did, "name": e.get("name"),
                                   "reason": "already_saved"})
            continue
        sources = _reproducible_import_sources(e)
        if sources:
            out["skipped"].append({"doc_id": did, "name": e.get("name"),
                                   "reason": "reproducible_import",
                                   "source_paths": sources})
            continue
        pending.append(e)

    if not root_value("CHUDWARE_PROJECT_SAVE_DIR"):
        if pending:
            out["persistence_failure"] = {
                "reason": "no_project_save_dir",
                "docs": [
                    {"doc_id": e["doc_id"], "name": e.get("name")}
                    for e in pending
                ],
            }
        else:
            out["skipped"].append({"reason": "no_project_save_dir"})
        return out

    for e in pending:
        did = e["doc_id"]
        try:
            written = _project_save(did, e.get("name"))
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{did}: {type(exc).__name__}: {exc}")
            continue
        if written:
            out["saved"].append({"doc_id": did, "name": e.get("name"), "path": written})
        else:
            out["errors"].append(f"{did}: doc_save wrote nothing")
    try:
        # Optional Box cloud mirror — STARTED, never awaited. This used to drain
        # synchronously with a 90 s budget because "the agent worker is a fresh fork per
        # turn, so a background drain thread would die with it". That premise is dead:
        # hostd's worker_bridge owns ONE persistent services worker, so a daemon thread
        # outlives the turn and worker_main.stop_services drains what is left on the way
        # out. Blocking here put a network upload between the model's last word and the
        # turn's return: with a backlogged spool every chat message sat at "Thinking…"
        # for the full 90 s AFTER the agent had finished (measured: 5.5 s turn +
        # 90.2 s drain). A cloud mirror is never on a user's critical path.
        from services.plm import box_mirror
        if box_mirror.is_enabled() and (out["saved"] or box_mirror.status()["pending"]):
            out["box"] = box_mirror.drain(block=False)
    except Exception:  # noqa: BLE001 — a terminal sweep never fails a turn
        pass
    return out


# doc_id -> engine revision at the doc's last autosave. Each autosave serializes the
# full doc to disk; skipping unchanged docs turns the idle 60s sweep into a no-op.
_AUTOSAVE_LAST_REV: dict = {}


def _autosave_all_open() -> None:
    """Autosave every open document THAT CHANGED since its last autosave (the engine's
    monotonic per-doc revision is the change signal; docs without one are always saved).
    Called on the periodic timer and before a heavy solve so a transport timeout that
    kills the server can never lose an unrelated open part (Bug R-1).

    No-op in daemon mode — see _autosave, which owns that decision. Returning early here as
    well only skips a doc_list the sweep would waste."""
    if cb._daemon_mode():
        return
    try:
        docs = (cb.call("doc_list", {}) or {}).get("docs", [])
    except Exception:  # noqa: BLE001
        return
    seen = set()
    for d in docs:
        did = d.get("doc_id") if isinstance(d, dict) else None
        if not did:
            continue
        seen.add(did)
        rev = d.get("revision") if isinstance(d, dict) else None
        if isinstance(rev, (int, float)) and _AUTOSAVE_LAST_REV.get(did) == rev:
            continue  # unchanged since last autosave — skip the full serialize
        _autosave(did, d.get("name") if isinstance(d, dict) else None)
        if isinstance(rev, (int, float)):
            _AUTOSAVE_LAST_REV[did] = rev
    # Forget closed docs so a re-created doc_id (engine ids reset/reuse) is never skipped.
    for gone in [k for k in _AUTOSAVE_LAST_REV if k not in seen]:
        _AUTOSAVE_LAST_REV.pop(gone, None)


# ---------------------------------------------------------------------------
# Periodic autosave timer
# ---------------------------------------------------------------------------
# Cadence: CHUDWARE_AUTOSAVE_INTERVAL_S (seconds, float, default 60).  Set to 0
# to disable.  The timer is a daemon thread so it never prevents process exit.
# Overhead is measured per sweep; a warning is logged when p95 > 50 ms.

_AUTOSAVE_INTERVAL_S: float = float(os.environ.get("CHUDWARE_AUTOSAVE_INTERVAL_S", "60") or 60)
_PERIODIC_TIMER: list = [None]  # [Timer | None]  — list so it's mutable from inner scope
# Generation token for the timer chain. start/stop each bump it; a tick only
# reschedules while its captured generation is still current, so an in-flight tick
# that fires across a stop/start cycle dies instead of re-arming an orphan chain
# (the historical 12x "chudware-autosave" thread leak).
_AUTOSAVE_GEN: list = [0]
_AUTOSAVE_LATENCIES: list = []  # float seconds — rolling window of sweep durations
_LATENCY_WINDOW = 100


def _tick_autosave(gen: int) -> None:
    """Single timer tick: autosave all open docs, record latency, reschedule.

    gen: the _AUTOSAVE_GEN value this chain was armed with — reschedule only while
    it is still current, so stopped/restarted chains can never stack."""
    import threading as _t  # local to avoid circular at module level

    t0 = time.perf_counter()
    _autosave_all_open()
    dt = time.perf_counter() - t0

    _AUTOSAVE_LATENCIES.append(dt)
    if len(_AUTOSAVE_LATENCIES) > _LATENCY_WINDOW:
        _AUTOSAVE_LATENCIES.pop(0)

    # Warn when recent p95 > 50 ms (DoD overhead gate).
    if len(_AUTOSAVE_LATENCIES) >= 20:
        sorted_lat = sorted(_AUTOSAVE_LATENCIES)
        p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
        if p95 > 0.050:
            import logging as _log
            _log.getLogger(__name__).warning(
                "autosave p95 %.1f ms exceeds 50 ms target — consider longer interval "
                "or reducing open doc count", p95 * 1000)

    # Reschedule only if interval > 0 and this chain's generation is still current
    # (stop/start bumps the generation, killing any in-flight tick's chain).
    interval = _AUTOSAVE_INTERVAL_S
    if interval > 0 and gen == _AUTOSAVE_GEN[0]:
        timer = _t.Timer(interval, _tick_autosave, args=(gen,))
        timer.daemon = True
        timer.name = "chudware-autosave"
        _PERIODIC_TIMER[0] = timer
        timer.start()


def start_periodic_autosave(interval_s: float | None = None) -> None:
    """Arm the periodic autosave timer.  Call once from server startup.

    interval_s: cadence override (seconds); None → read CHUDWARE_AUTOSAVE_INTERVAL_S
    (default 60 s).  Passing 0 disables the timer.  Idempotent: if already running
    the old timer is cancelled and a new one started with the new interval.
    """
    import threading as _t

    if cb._daemon_mode():
        # Daemon mode (the default): chudware-hostd runs THE one autosave scheduler (core/hostd/
        # autosave.cpp). Arming a per-process Timer chain here would recreate the
        # exact thread-leak class the shared daemon exists to kill.
        return
    global _AUTOSAVE_INTERVAL_S
    if interval_s is not None:
        _AUTOSAVE_INTERVAL_S = float(interval_s)
    stop_periodic_autosave()  # cancel any previous timer + orphan any in-flight tick
    if _AUTOSAVE_INTERVAL_S <= 0:
        return
    _AUTOSAVE_GEN[0] += 1
    gen = _AUTOSAVE_GEN[0]
    _seed_autosave_revisions()
    timer = _t.Timer(_AUTOSAVE_INTERVAL_S, _tick_autosave, args=(gen,))
    timer.daemon = True
    timer.name = "chudware-autosave"
    _PERIODIC_TIMER[0] = timer
    timer.start()


def _seed_autosave_revisions() -> None:
    """Baseline every currently-open doc's engine revision so the FIRST sweep skips
    docs nobody has touched since the timer was armed (e.g. just-recovered docs whose
    bytes are already on disk). Docs opened later still get one baseline autosave."""
    try:
        docs = (cb.call("doc_list", {}) or {}).get("docs", [])
    except Exception:  # noqa: BLE001 — seeding is an optimization, never fatal
        return
    for d in docs:
        did = d.get("doc_id") if isinstance(d, dict) else None
        rev = d.get("revision") if isinstance(d, dict) else None
        if did and isinstance(rev, (int, float)) and did not in _AUTOSAVE_LAST_REV:
            _AUTOSAVE_LAST_REV[did] = rev


def stop_periodic_autosave() -> None:
    """Cancel the periodic autosave timer (if running).  Also bumps the generation
    token so a tick already in flight (past its Timer, mid-sweep) cannot re-arm."""
    _AUTOSAVE_GEN[0] += 1
    t = _PERIODIC_TIMER[0]
    if t is not None:
        t.cancel()
        _PERIODIC_TIMER[0] = None


def autosave_p95_ms() -> float | None:
    """Return current p95 autosave latency in milliseconds, or None if < 2 samples."""
    if len(_AUTOSAVE_LATENCIES) < 2:
        return None
    s = sorted(_AUTOSAVE_LATENCIES)
    return s[int(len(s) * 0.95)] * 1000.0


def _autosave_session_key(step_path: Path) -> str:
    """The session a snapshot belongs to, from its FILENAME (`{session}-{doc_id}.step+`).

    Filename, not sidecar, deliberately: the count cap runs on every autosave, and reading
    2,000 sidecar JSONs per snapshot to learn something the name already encodes is pure
    I/O. Session ids are uuid4 hex (never contain "-"), so everything up to the FIRST "-"
    is the session and the rest is the doc_id. A legacy untagged snapshot ("doc-cpp…")
    yields "doc"; that groups all of them into one pseudo-session, which is exactly the
    whole-or-nothing unit the retention rule wants for them too.

    Matching on the prefix rather than a 12-hex pattern also keeps the rule honest for any
    _SESSION_ID a caller injects: a key that failed to parse would silently drop this
    process's own-session protection."""
    return step_path.name[:-6].split("-", 1)[0]


def _autosave_prune_plan(entries: list[tuple[float, Path]], cap: int,
                         keep_sessions: int, own_session: str) -> list[Path]:
    """Which snapshots the COUNT cap may delete: whole sessions, oldest first.

    `entries` is [(mtime, path)]. Sessions rank by their NEWEST snapshot. Never
    prunable: `own_session` (live crash-recovery data for the running process) and the
    newest `keep_sessions` sessions. Sessions are then dropped WHOLE, oldest first, until
    the surviving count is within `cap` — and if no whole prunable session is left, the
    cap simply goes unmet. The invariant outranks the cap: a session that survives a
    prune is always fully recoverable, so no part is ever silently half-restored.
    """
    by_session: dict[str, list[Path]] = {}
    newest: dict[str, float] = {}
    for mt, p in entries:
        key = _autosave_session_key(p)
        by_session.setdefault(key, []).append(p)
        if mt > newest.get(key, -1.0):
            newest[key] = mt
    ranked = sorted(by_session, key=lambda k: newest[k], reverse=True)  # newest session first
    protected = set(ranked[:max(0, keep_sessions)]) | {own_session}
    doomed: list[Path] = []
    surviving = len(entries)
    for key in reversed(ranked):  # oldest session first
        if surviving <= cap:
            break
        if key in protected:
            continue
        doomed.extend(by_session[key])
        surviving -= len(by_session[key])
    return doomed


def _prune_autosaves(max_age_days: float = _AUTOSAVE_MAX_AGE_DAYS,
                     max_files: int = 0, keep_sessions: int = 0) -> int:
    """Delete autosave .step+ / sidecars older than max_age_days (by mtime).

    *max_files* (0 = use _AUTOSAVE_MAX_FILES) additionally caps how many snapshots may
    remain. The age rule alone never bounded anything in practice — the worker forks per
    turn, so every snapshot on disk is young — which is how the directory reached 39k
    files in one day.

    The count cap evicts WHOLE SESSIONS, oldest first, keeping the newest
    *keep_sessions* (0 = _AUTOSAVE_KEEP_SESSIONS) plus this process's own. A snapshot is
    potentially-unsaved user work and nothing on disk says otherwise, so the cap's safety
    rule is structural rather than a guess: a session survives entirely or not at all, and
    the cap yields to that invariant rather than shaving a session down.

    CAVEAT, stated rather than papered over: the AGE prune is still per-file, so a session
    straddling the cutoff loses only its stale snapshots. That is the pre-existing,
    documented contract (CHUDWARE_AUTOSAVE_MAX_AGE_DAYS, default 7) — work nobody saved in
    a week is deliberately not kept forever. Only the count cap, which deletes YOUNG files,
    is session-granular. Returns the number of snapshots deleted.
    """
    n = 0
    cutoff = time.time() - max_age_days * 86400.0
    try:
        entries: list[tuple[float, Path]] = []
        for p in _autosave_dir().glob("*.step+"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < cutoff:
                try:
                    p.unlink(missing_ok=True)
                    p.with_suffix(".json").unlink(missing_ok=True)
                    n += 1
                except OSError:
                    pass
                continue
            entries.append((mt, p))
        cap = max_files or _AUTOSAVE_MAX_FILES
        if cap > 0 and len(entries) > cap:
            for p in _autosave_prune_plan(entries, cap,
                                          keep_sessions or _AUTOSAVE_KEEP_SESSIONS,
                                          _SESSION_ID):
                try:
                    p.unlink(missing_ok=True)
                    p.with_suffix(".json").unlink(missing_ok=True)
                    n += 1
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return n


def _autosave_session_of(step_path: Path) -> tuple[Optional[str], float, str, str]:
    """(session_id, saved_at, name, doc_id) for an autosave from its sidecar, falling back to
    file mtime when there's no sidecar (legacy autosaves). The real doc_id comes from the sidecar
    (the filename is now `{session}-{doc_id}` so its stem can't be used as the doc_id); for a
    legacy untagged file the stem IS the doc_id."""
    side = step_path.with_suffix(".json")
    try:
        m = json.loads(side.read_text())
        return (m.get("session"),
                float(m.get("saved_at", step_path.stat().st_mtime)),
                m.get("name", ""),
                m.get("doc_id", "") or step_path.stem)
    except Exception:  # noqa: BLE001
        try:
            return None, step_path.stat().st_mtime, "", step_path.stem
        except OSError:
            return None, 0.0, "", step_path.stem


def _recover_via_daemon() -> list:
    """Daemon-mode recovery: chudware-hostd already recovered the prior session's
    autosaves itself (core/hostd/autosave.cpp) — we just consume the daemon's
    {old_id: new_id} rebind map into _DOC_ALIASES instead of re-opening files.
    Waits briefly for the daemon's recovery pass (it runs right after the engine
    loads), then re-hellos to fetch the final id_remap. Best-effort; never raises."""
    out: list = []
    try:
        from . import daemon_bridge
        c = daemon_bridge.client()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            st = c.call("daemon_status")
            if st.get("recovery") in ("done", "off") or st.get("state") == "failed":
                break
            time.sleep(0.2)
        c.refresh_server_info()
        remap = (c.server_info or {}).get("id_remap") or {}
        names = {}
        try:
            for d in (cb.call("doc_list", {}) or {}).get("docs", []):
                if isinstance(d, dict):
                    names[d.get("doc_id")] = d.get("name") or ""
        except Exception:  # noqa: BLE001
            pass
        for old, new in remap.items():
            if old and new and old != new:
                _DOC_ALIASES[old] = new
            out.append({"path": None, "doc_id": new, "name": names.get(new, ""),
                        "original_doc_id": old})
            _CUR["doc_id"] = new
    except Exception:  # noqa: BLE001
        pass
    return out


def _recover_orphans_via_daemon(limit: int = 0) -> list:
    """Open the daemon's orphan-tier docs (retired by clean shutdown / doc-GC —
    never auto-opened; see core/hostd/autosave.h). Only called from the explicit
    recover_parts tool, never at startup.

    Opens at most `limit` (0 = _RECOVER_MAX_DOCS) entries, NEWEST FIRST, one
    recovery_open{"id":…} per entry rather than the old single recovery_open{"all":true}:
    the tier holds up to CHUDWARE_ORPHAN_KEEP (50) crash survivors and reopening every
    one of them left the engine unable to say which was "the part". `_RECOVER_SKIPPED`
    records what was left in the tier — nothing is discarded, the next call reaches it.
    Best-effort; never raises."""
    global _RECOVER_SKIPPED
    out: list = []
    try:
        from . import daemon_bridge
        c = daemon_bridge.client()
        entries = [e for e in ((c.call("recovery_list") or {}).get("entries") or [])
                   if isinstance(e, dict) and e.get("id")]
        if not entries:
            return out
        # recoveryList() documents newest-first; sort defensively so the bound is
        # deterministic even if that ever changes.
        entries.sort(key=lambda e: e.get("mtime_ms") or 0, reverse=True)
        cap = limit or _RECOVER_MAX_DOCS
        chosen = entries[:cap] if cap > 0 else entries
        _RECOVER_SKIPPED += max(0, len(entries) - len(chosen))
        for meta in chosen:
            opened = (c.call("recovery_open", {"id": meta["id"]}) or {}).get("opened") or []
            for o in opened:
                if not isinstance(o, dict):
                    continue
                new_id = o.get("doc_id") or ""
                old_id = meta.get("doc_id") or ""
                if old_id and new_id and old_id != new_id:
                    _DOC_ALIASES[old_id] = new_id
                out.append({"path": meta.get("path"), "doc_id": new_id,
                            "name": meta.get("name", ""), "original_doc_id": old_id})
                if new_id:
                    _CUR["doc_id"] = new_id
    except Exception:  # noqa: BLE001
        pass
    return out


def recover_autosaved(scope: str = "recent", include_orphaned: bool = False,
                      limit: int = 0) -> list:
    """Reopen autosaved .step+ docs into the engine. A reopened doc gets a FRESH doc_id;
    returns [{path, doc_id, name, original_doc_id}] for the ones that loaded.

    scope="recent" (default): only the SINGLE most-recent prior session's docs — the
    crash we're recovering from — never the whole history. scope="all": every autosave.
    include_orphaned: open the daemon's on-disk recovery tier (crash-retired docs) —
    only the explicit recover_parts tool passes this. Startup must NOT (the tier keeps a
    deep history of crash recoveries; auto-opening all of it would flood the engine).
    limit: the most docs this call may reopen (0 = _RECOVER_MAX_DOCS), NEWEST FIRST.
    The rest stay on disk and are counted in `recover_skipped()`; a second call with a
    bigger limit reaches them. Unbounded recovery is the autosave-debris footgun: on a
    real pile scope="recent" opened 270 docs and scope="all" 1,803, after which no
    doc-scoped call could resolve "the part". The bound applies to the STARTUP call in
    server.py too, deliberately: silently auto-opening a 270-doc session is the same
    flood, just without anyone asking for it. Prunes stale autosaves first.

    Daemon mode: recovery lives in the orphan tier — a crashed daemon's autosaves and
    any crash-abandoned doc (GC-retired) are REGISTERED there, never auto-opened. A
    clean app quit sends doc_discard, so the tier holds only genuine crash survivors.
    recover_parts opens them DELIBERATELY; startup recovers nothing here (the hello
    id_remap fallback is empty on a current daemon)."""
    global _RECOVER_SKIPPED
    _RECOVERED.clear()
    _RECOVER_SKIPPED = 0
    cap = limit or _RECOVER_MAX_DOCS
    if cb._daemon_mode():
        out = _recover_via_daemon()  # [] on a current daemon (no auto-open)
        if include_orphaned:
            seen = {r.get("original_doc_id") for r in out}
            # The rebind map already spent part of the budget; the tier gets what is left.
            # Exhausted budget (room <= 0) means we must NOT touch the tier at all —
            # limit=0 would mean "use the default", i.e. no bound at all.
            room = (cap - len(out)) if cap > 0 else 0
            if cap <= 0 or room > 0:
                for r in _recover_orphans_via_daemon(limit=room):
                    if r.get("original_doc_id") not in seen:
                        out.append(r)
        _RECOVERED.extend(out)
        return out
    _prune_autosaves()
    out: list = []
    try:
        files = sorted(_autosave_dir().glob("*.step+"))
    except Exception:  # noqa: BLE001
        return out

    info = [(p, *_autosave_session_of(p)) for p in files]  # (path, session, saved_at, name, doc_id)
    if scope != "all":
        prior = [t for t in info if t[1] != _SESSION_ID]  # exclude our own (fresh) session
        if prior:
            # the most-recent prior session = the one whose newest autosave is latest.
            latest_session = max(prior, key=lambda t: t[2])[1]
            if latest_session is None:
                # legacy autosaves with no session tag — take the newest few by mtime.
                prior.sort(key=lambda t: t[2], reverse=True)
                info = prior[:8]
            else:
                info = [t for t in prior if t[1] == latest_session]
        else:
            info = []

    # NEWEST FIRST, then bounded: one session on this workstation's real pile held 270
    # snapshots, so "the recovered scope" is not a safe number of docs to open. Sorting by
    # saved_at also makes the LAST doc opened — and therefore `_CUR`, the part an omitted
    # doc_id resolves to — the OLDEST of the batch by construction, so the loop below
    # reverses back to chronological order and _CUR lands on the newest snapshot instead of
    # whichever stem happened to sort last lexically.
    info.sort(key=lambda t: t[2], reverse=True)
    if cap > 0 and len(info) > cap:
        _RECOVER_SKIPPED = len(info) - cap
        info = info[:cap]

    for p, _sess, _ts, name, orig_id in reversed(info):  # oldest -> newest
        try:
            r = cb.call("doc_open", {"path": str(p)})
            did = r.get("doc_id")
            if did:
                # Prefer the sidecar name; fall back to the reopened doc's own name so an
                # untagged/legacy autosave still recovers with a real name (Bug N-6).
                # `orig_id` is the REAL pre-restart doc_id from the sidecar (not the now
                # session-prefixed filename stem), so the _DOC_ALIASES handle stays valid.
                original = orig_id
                out.append({"path": str(p), "doc_id": did,
                            "name": name or r.get("name") or "",
                            "original_doc_id": original})
                # Keep the agent's old handle alive (Issue 1): the original id now aliases the
                # freshly-allocated one, so a doc_id captured before the restart still works.
                if original and original != did:
                    _DOC_ALIASES[original] = did
                _CUR["doc_id"] = did
                vol = r.get("volume")
                if isinstance(vol, (int, float)):
                    _LAST_VOLUME[did] = vol
        except Exception:  # noqa: BLE001
            continue
    out.reverse()  # report newest first, matching the order they were chosen in
    _RECOVERED.extend(out)
    return out


def recover_skipped() -> int:
    """How many autosaved docs the last recover_autosaved() left unopened (still on disk)."""
    return _RECOVER_SKIPPED


# --- async jobs ------------------------------------------------------------------------
# Heavy solves (CFD, FEA) can exceed the MCP transport's per-call timeout; when the client gives
# up it may KILL the server and lose every open doc. So the sim tools can run in the BACKGROUND:
# return a job_id immediately, run the engine call on a worker thread (the LBM/FEA solve itself
# does not hold the engine lock), and let the caller poll get_job(job_id). Default stays synchronous
# for simple use; pass background=True for the heavy runs.
import threading as _threading  # noqa: E402
import uuid as _uuid  # noqa: E402


#: Durable sim-job ids are minted by services/sim/jobs.py as ``sim-<hex>``; the thread
#: table mints ``job-<hex>``. The prefixes are disjoint, so one id space can serve both.
_DURABLE_JOB_PREFIX = "sim-"


#: In-process thread jobs only. Durable ``sim-*`` jobs live in the SQLite registry and are
#: resolved by ``get_job`` only after it releases ``_JOB_LOCK``; registry reads and DONE
#: collection may wait, call the engine, and persist a study, so they must never hide behind
#: this process-local bookkeeping lock.
class _OwnedJobs(MutableMapping):
    """Shared bookkeeping with mandatory owner-filtered lookup for every caller."""
    def __init__(self):
        self._records = {}

    def __getitem__(self, key):
        return self._records[(owner_key(), key)]

    def __setitem__(self, key, value):
        self._records[(owner_key(), key)] = value

    def __delitem__(self, key):
        del self._records[(owner_key(), key)]

    def __iter__(self):
        owner = owner_key()
        return iter([key for (stored_owner, key) in self._records if stored_owner == owner])

    def __len__(self):
        return sum(1 for _ in self)


_JOBS: MutableMapping = _OwnedJobs()
_JOB_LOCK = _threading.Lock()


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    owner = owner_key()
    with registry_lock(_JOB_LOCK):
        active = sum(job["status"] == "running"
                     for (stored_owner, _), job in _JOBS._records.items()
                     if stored_owner == owner)
    return {"by_kind": {"cad_thread_jobs": active}}

# G2/G5: completed jobs must stay retrievable for a generous window so a poll after a
# long OpenFOAM run (which may take minutes) never returns "unknown job_id".  We keep:
#   - ALL running jobs (never evict while in-flight)
#   - completed/errored jobs for at least this many seconds after completion
_JOB_DONE_TTL_S: float = float(os.environ.get("CHUDWARE_JOB_TTL_S", "600") or 600)  # 10 min


def _evict_old_jobs() -> None:
    """Evict ONLY completed/errored jobs whose completion time is older than _JOB_DONE_TTL_S.
    Running jobs are NEVER evicted regardless of age. Called inside _start_job so the dict
    can't grow without bound in a long-lived server, but the eviction window is generous
    enough that a poll right after a multi-minute OpenFOAM run still succeeds."""
    cutoff = time.time() - _JOB_DONE_TTL_S
    to_del = []
    for jid, job in _JOBS.items():
        if job["status"] in ("done", "error"):
            done_at = job.get("done_at", 0.0)
            if done_at and done_at < cutoff:
                to_del.append(jid)
    for jid in to_del:
        del _JOBS[jid]


def _start_job(kind: str, fn, progress: Optional[dict] = None) -> str:
    jid = "job-" + _uuid.uuid4().hex[:12]
    with _JOB_LOCK:
        _evict_old_jobs()  # prune stale completed jobs so dict stays bounded
        _JOBS[jid] = {"status": "running", "kind": kind, "result": None, "error": None,
                      "progress": progress if progress is not None else {},
                      "started_at": time.time(), "done_at": None}

    def _run() -> None:
        try:
            res = fn()
            with _JOB_LOCK:
                _JOBS[jid].update(status="done", result=res, done_at=time.time())
        except Exception as e:  # noqa: BLE001
            with _JOB_LOCK:
                _JOBS[jid].update(status="error", error=str(e), done_at=time.time())
                if isinstance(e, CoreError):
                    _JOBS[jid].update(code=e.code, detail=e.detail)

    # Daemon thread: a long OpenFOAM/FEA solve must never block server (or test) shutdown.
    from services.worker_activity import start_background_thread

    try:
        start_background_thread(_run, name=f"chudware-job-{kind}")
    except BaseException:
        with _JOB_LOCK:
            _JOBS.pop(jid, None)
        raise
    return jid


# How long a "synchronous" heavy solve may block the MCP call before we hand it off to
# the background so the call returns BEFORE the transport timeout can kill the server
# (Bug R-1). Stay safely under a typical 60s transport timeout.
_SYNC_SOLVE_BUDGET_S = float(os.environ.get("CHUDWARE_MCP_SYNC_BUDGET_S", "45") or 45)


def _solve_sync_or_handoff(kind: str, fn, progress: Optional[dict] = None,
                           budget_s: float = _SYNC_SOLVE_BUDGET_S) -> dict:
    """Run heavy work on the owning process's thread with a bounded inline reply wait.

    Always runs on a worker thread (via _start_job). For a synchronous request we poll
    the job up to `budget_s`; if it finishes we return the result inline (small solves
    behave exactly as before). If it overruns, we LEAVE it running and return the job_id
    for polling with get_job(). This does not make the work durable or cancellable.
    """
    jid = _start_job(kind, fn, progress)
    deadline = time.time() + max(1.0, budget_s)
    # Adaptive poll: start fine (10 ms) so a fast op — the common modeling case now that
    # add_features routes through here — returns almost immediately, then back off to 100 ms so a
    # multi-second solve doesn't spin. Heavy-op handoff at `budget_s` is unaffected.
    interval = 0.01
    while time.time() < deadline:
        with _JOB_LOCK:
            snap = dict(_JOBS.get(jid, {}))
        st = snap.get("status")
        if st == "done":
            return snap.get("result") or {"ok": False, "error": "no result"}
        if st == "error":
            return {"ok": False, "error": snap.get("error", "solve failed"),
                    **{key: snap[key] for key in ("code", "detail") if key in snap}}
        time.sleep(interval)
        if interval < 0.1:
            interval = min(0.1, interval * 1.5)
    return {"ok": True, "job_id": jid, "status": "running", "auto_backgrounded": True,
            "note": (f"solve still running after {budget_s:.0f}s — continued in the BACKGROUND "
                     f"to keep the tool reply responsive. This process-local job is not "
                     f"restart-durable or cancellable. Poll get_job('{jid}').")}


# --- durable (out-of-process) sim jobs ---------------------------------------------------
# The thread table above cannot do the three things a multi-minute solve needs: it cannot be
# cancelled (a Python thread sitting in one ctypes call into libchudware is unkillable), it
# cannot survive the process (an agent turn forks a fresh worker, and a client timeout can
# restart the server outright), and it exposes nothing until the closure returns. So every
# heavy run_fea_* / run_cfd_* path runs its SOLVE in a child process tracked by the SQLite
# registry in services/sim/jobs.py, and only the SETUP (needs the live document) and the
# COLLECT (writes the study back into the document) stay here.
#
# The thread table keeps serving everything else — add_features batches, ECAD autoroute —
# which are engine mutations on the live doc table and therefore cannot be moved to a child
# that shares no documents.

from services.sim import jobs as _simjobs  # noqa: E402

#: Durable job kind -> collector(status_dict, raw_stage_result) -> reply envelope.
#: Registered by services/mcp/cad_sim_tools.py, which owns the reply shapes. ``get_job``
#: calls ``durable_job_snapshot`` outside the thread-job lock for reconnect-safe lookup.
_DURABLE_COLLECTORS: dict = {}

#: Durable job kind -> terminal(status_dict, stage_result_or_None) -> reply envelope, for
#: the kinds that OPT IN to persisting FAILED/CANCELLED jobs. Separate from the collectors
#: on purpose: every success collector reads the raw fields of a converged solve, and a
#: failed or killed job has none of them.
_DURABLE_TERMINALS: dict = {}

#: How long a poller that lost the collect race waits for the winner's artifact before
#: collecting itself. Collect is an engine round-trip on an open document, not a solve.
_COLLECT_WAIT_S = 10.0


class DurableCollectRetry(Exception):
    """Raised by a collector or terminal hook whose document write failed RETRYABLY.

    ``envelope`` is returned to this caller as-is (plus ``retryable: true``); nothing is
    cached and the collect claim is released, so the next poll runs the collect again.
    """

    def __init__(self, envelope: dict):
        super().__init__(str(envelope.get("study_not_saved_reason") or "collect will retry"))
        self.envelope = envelope


def register_durable_collector(kind: str, fn) -> None:
    """Register the COLLECT stage for a durable job kind."""
    _DURABLE_COLLECTORS[str(kind)] = fn


def register_durable_terminal(kind: str, fn) -> None:
    """Opt a durable job kind into collecting its FAILED and CANCELLED jobs.

    ``fn(status, stage_result)`` runs under the same exactly-once claim/cache as the
    success collector. ``status`` is the reconciled job status (``state`` is ``failed`` or
    ``cancelled``); ``stage_result`` is the result the worker actually wrote, or None when
    it failed before replying or was killed. It returns an ``ok: false`` envelope carrying
    ``job_id``, ``state`` and ``error``, or raises :class:`DurableCollectRetry`.
    """
    _DURABLE_TERMINALS[str(kind)] = fn


def _durable_kind(st: dict) -> str:
    meta = st.get("meta") or {}
    return str(meta.get("analysis") or st.get("kind") or "")


def _acquire_collect(job_id: str) -> Optional[dict]:
    """None when this caller now HOLDS the collect claim; otherwise the reply to return."""
    deadline = time.time() + _COLLECT_WAIT_S
    while True:
        cached = _simjobs.collected(job_id)
        if cached is not None:
            return cached
        # Retried every turn: a holder whose write failed retryably released the claim.
        if _simjobs.claim_collect(job_id):
            return None
        if time.time() >= deadline:
            break
        time.sleep(0.05)
    # The claim holder died mid-collect (or was killed with the server). Break its stale
    # claim and take it over WITH a claim, so every exit below releases or fulfils ours.
    _simjobs.release_collect(job_id)
    if _simjobs.claim_collect(job_id):
        return None
    cached = _simjobs.collected(job_id)
    if cached is not None:
        return cached
    return {"ok": False, "job_id": job_id, "retryable": True,
            "error": f"another poller is collecting job {job_id}; poll it again"}


def collect_durable(job_id: str, st: Optional[dict] = None) -> dict:
    """The finished job's reply envelope, computed EXACTLY ONCE and replayed thereafter.

    Collect is not a pure formatting step: it writes the solved study into the document, so
    running it per poll would grow one study record per poll in the feature tree. The spool
    arbitrates with an atomic claim file, so concurrent pollers — including pollers in
    DIFFERENT processes after a restart — all read the one envelope the winner wrote.
    A FAILED/CANCELLED job is collected the same way only when its kind registered a
    terminal hook (:func:`register_durable_terminal`).
    """
    cached = _simjobs.collected(job_id)
    if cached is not None:
        return cached
    if st is None:
        st = _simjobs.status(job_id)
    state = st.get("state")
    kind = _durable_kind(st)
    terminal = None
    if state in (_simjobs.FAILED, _simjobs.CANCELLED):
        terminal = _DURABLE_TERMINALS.get(kind)
        if terminal is None:
            return {"ok": False, "job_id": job_id, "state": state,
                    "error": st.get("error") or f"solve {state}"}
    held = _acquire_collect(job_id)
    if held is not None:
        return held
    fulfilled = False
    try:
        try:
            if terminal is not None:
                out = terminal(st, _simjobs.terminal_result(job_id))
            else:
                raw = _simjobs.raw_result(job_id)
                if raw is None:
                    return {"ok": False, "job_id": job_id, "state": st.get("state"),
                            "error": "the job finished but its result artifact could not be "
                                     "read — nothing can be reported honestly. Re-run the "
                                     "solve."}
                collector = _DURABLE_COLLECTORS.get(kind)
                if collector is None:
                    return {"ok": False, "job_id": job_id,
                            "error": f"no result collector for job kind {kind!r}"}
                out = collector(st, raw)
        except DurableCollectRetry as retry:
            return {**retry.envelope, "job_id": job_id, "retryable": True}
        except Exception as exc:  # noqa: BLE001 — a broken collect must not strand the job
            return {"ok": False, "job_id": job_id,
                    "error": f"collecting job {job_id} failed: {exc}"}
        fulfilled = True
    finally:
        if not fulfilled:
            _simjobs.release_collect(job_id)
    return _simjobs.save_collected(job_id, out)


def collect_durable_terminal(job_id: str, st: dict) -> Optional[dict]:
    """A FAILED/CANCELLED job's terminal envelope, or None when its kind keeps none.

    Collects (exactly once) for a kind with a registered terminal hook, and otherwise
    only replays an envelope another process already collected.
    """
    if st.get("state") not in (_simjobs.FAILED, _simjobs.CANCELLED):
        return None
    if _durable_kind(st) in _DURABLE_TERMINALS:
        return collect_durable(job_id, st)
    return _simjobs.collected(job_id)


def durable_job_snapshot(job_id: str) -> Optional[dict]:
    """Render a durable job in the THREAD table's record shape, or return None.

    The caller must not hold ``_JOB_LOCK``: a DONE snapshot collects and persists the
    solved study exactly once, which can wait on another process's collect claim and call
    back into the document engine. A FAILED/CANCELLED job of a kind with a terminal hook
    is collected the same way; its envelope is the error's ``detail``, under ``code``
    ``solve_failed`` / ``solve_cancelled``.
    """
    try:
        st = _simjobs.status(job_id)
    except Exception:  # noqa: BLE001 — an unreachable registry is "not our job", not a crash
        return None
    state = st.get("state")
    if state == "unknown":
        return None
    rec = {"kind": _durable_kind(st), "durable": True, "job_id": job_id,
           "result": None, "error": None, "progress": st.get("progress") or {},
           "started_at": st.get("started_at"), "done_at": st.get("done_at")}
    if state == _simjobs.DONE:
        rec["status"] = "done"
        rec["result"] = collect_durable(job_id, st)
    elif state in (_simjobs.FAILED, _simjobs.CANCELLED):
        rec["status"] = "error"
        rec["error"] = st.get("error") or f"solve {state}"
        detail = collect_durable_terminal(job_id, st)
        if detail is not None:
            rec["code"] = f"solve_{state}"
            rec["detail"] = detail
    else:
        rec["status"] = "running"
        rec["progress"].setdefault("elapsed_s", st.get("elapsed_s"))
    return rec


def durable_handoff(job_id: str, budget_s: float) -> dict:
    """The reply for a solve that outran its synchronous budget and stayed in the child."""
    return {"ok": True, "job_id": job_id, "status": "running", "state": "running",
            "auto_backgrounded": True, "cancel_supported": True,
            "note": (f"solve still running after {budget_s:.0f}s — continued in a SEPARATE "
                     "PROCESS to avoid a transport timeout (which would drop open "
                     "documents). Open parts were autosaved. The job is persisted on disk, "
                     "so it survives an MCP restart and can be collected in a later turn: "
                     f"poll get_job('{job_id}') or sim_status('{job_id}')."),
            "next_actions": [f"get_job('{job_id}')", f"sim_cancel('{job_id}')"]}


def solve_durable_or_handoff(kind: str, stage: str, payload: dict,
                             meta: Optional[dict] = None,
                             budget_s: float = _SYNC_SOLVE_BUDGET_S) -> dict:
    """Start a durable solve and wait for it, but never past the transport's patience.

    A fast solve returns its collected envelope inline — byte-identical to what the
    synchronous tool always returned. A slow one is LEFT RUNNING in its own process and the
    call returns the job id in time, so the transport never times out and kills the session.
    A failed or cancelled solve of a kind with a terminal hook returns that hook's
    (exactly-once, persisted) envelope.
    """
    job = _simjobs.start(kind, stage, payload, meta)
    jid = job["job_id"]
    deadline = time.time() + max(1.0, budget_s)
    # Adaptive poll: fine at first so a small solve returns almost immediately, backing off
    # so a multi-minute run isn't hammering SQLite for its whole budget.
    interval = 0.02
    while time.time() < deadline:
        st = _simjobs.status(jid)
        state = st.get("state")
        if state == _simjobs.DONE:
            return collect_durable(jid, st)
        if state in (_simjobs.FAILED, _simjobs.CANCELLED):
            terminal = collect_durable_terminal(jid, st)
            if terminal is not None:
                return terminal
            return {"ok": False, "job_id": jid, "state": state,
                    "error": st.get("error") or f"solve {state}"}
        time.sleep(interval)
        interval = min(0.4, interval * 1.5)
    return durable_handoff(jid, budget_s)


# A small material library so FEA tools take a friendly name. Fields are exactly what the
# host's study expects (host/src/ui/sim_manager.cpp): E in MPa, density g/cm^3, strengths MPa.
# These are sourced from the unified materials registry — the single provenance-stamped
# first-party table — now served by the C++ core (core/materials, via the libchudware
# material_fea_table / material_fea_aliases methods) instead of the retired Python
# engine.materials module. `fea_table()` reproduces exactly the legacy 7-entry library +
# `fea_aliases()` the alias map (values unchanged). Filled LAZILY on first material
# resolution: importing cad_common must never require a live engine bridge. The dicts are
# updated IN PLACE so names re-exported through cad_tools stay the same objects.
_MATERIALS: dict = {}
_MATERIAL_ALIASES: dict = {}


def _wire_materials_registry() -> None:
    """Point the C++ core at the bundled materials registry JSON when nothing else has.

    The core resolves the registry via $CHUDWARE_MATERIALS_REGISTRY (or a provisioned
    data-root copy) and caches it once per process — so this must run at IMPORT time,
    before the first engine RPC can spawn/attach an engine without it. ``setdefault``
    never overrides an explicit user/installer path."""
    try:
        p = Path(__file__).resolve().parents[2] / "core" / "data" / "materials" / "registry.json"
        if p.is_file():
            os.environ.setdefault("CHUDWARE_MATERIALS_REGISTRY", str(p))
    except Exception:  # noqa: BLE001
        pass


_wire_materials_registry()


def _ensure_materials() -> None:
    """Populate _MATERIALS / _MATERIAL_ALIASES from the C++ core on first use."""
    if _MATERIALS:
        return
    _wire_materials_registry()
    _MATERIALS.update(cb.call("material_fea_table", {}).get("result") or {})
    _MATERIAL_ALIASES.update(cb.call("material_fea_aliases", {}).get("result") or {})


# --- engineering-designation resolution -------------------------------------------------
# Measured live 2026-07-29 (claude-sonnet-4-5, structural FEA): the model asked for
# material='A36 steel' and was rejected with the 7-name listing, then retried with plain
# 'steel'. A36 IS the registry's steel row ("Mild steel (A36)", alias "a36") — only the
# two-word spelling missed. Two structural facts drive the fix below:
#   1. `material_fea_table` exposes only the 7 `fea_canonical` rows, but the registry itself
#      carries full first-party mechanical data for ~28 alloys/plastics (7075-T6, 1018, 4130,
#      2024-T3, cast iron, PEEK, ...). `material_get` resolves any registry key/name/alias, so
#      a designation outside the canonical 7 is answered with its OWN properties — 7075-T6
#      gets its real 503 MPa yield instead of generic aluminum's 276.
#   2. What the registry lacks is spelling variants, so normalize (case, whitespace, hyphens,
#      underscores, redundant category words, word order) rather than table another name.
# Anything we recognise but that the registry does NOT model is REFUSED with paste-ready
# properties (see _MATERIAL_NOT_MODELLED) — a quietly wrong yield is worse than a clear error.

# Category words that carry no designation information once a grade number is present:
# "a36 steel" -> "a36", "6061-T6 aluminium" -> "6061-t6", "304 stainless steel" -> "304".
# "grade" is deliberately NOT generic ("grade 2 titanium" needs it).
_MATERIAL_GENERIC_WORDS = frozenset({
    "steel", "stainless", "aluminum", "aluminium", "alu", "al", "titanium", "ti",
    "brass", "plastic", "metal", "alloy", "filament",
})

# Designations that name EXACTLY the same alloy as a registry row but that the registry's
# own alias list does not spell. IDENTITY, not substitution — each value is a registry
# name/alias, and the resolved properties are that row's own.
#   grade 5 titanium == Ti-6Al-4V (ASTM B265 Gr5 IS the 6Al-4V alloy)
#   18-8            == the 18Cr-8Ni austenitic grade sold as 304 (fastener trade name)
#   free-machining brass == C360 (the registry spells it "free-cutting brass")
#   PLA+ is a toughened PLA; nominal PLA is the conservative prior (lower yield) and the
#   only PLA data the registry has.
_MATERIAL_SAME_ALLOY = {
    "grade 5 titanium": "ti-6al-4v",
    "titanium grade 5": "ti-6al-4v",
    "ti grade 5": "ti-6al-4v",
    "gr5 titanium": "ti-6al-4v",
    "ti gr5": "ti-6al-4v",
    "ti 6al 4v": "ti-6al-4v",
    "18 8": "304",
    "18 8 stainless": "304",
    "free machining brass": "c360",
    "pla+": "pla",
    "pla plus": "pla",
}

# Designations we RECOGNISE but refuse to resolve, because the nearest registry row has a
# materially different yield and aliasing would silently produce a wrong safety factor.
# value = (spelled-out label, nearest registry row, why we refuse, paste-ready properties).
# All property values are public-handbook nominals (ASM Metals Reference / ASTM grade
# minimums), the same provenance class as core/data/materials/registry.json.
_MATERIAL_NOT_MODELLED = {
    "316": ("Stainless 316", "stainless (304)", "316 yields 290 MPa vs 304's 215 MPa",
            "{'E_mpa': 193000, 'nu': 0.30, 'density_g_cm3': 8.0, 'yield_mpa': 290, "
            "'ultimate_mpa': 580}"),
    "316l": ("Stainless 316L", "stainless (304)", "316L yields 170 MPa vs 304's 215 MPa",
             "{'E_mpa': 193000, 'nu': 0.30, 'density_g_cm3': 8.0, 'yield_mpa': 170, "
             "'ultimate_mpa': 485}"),
    "4140": ("Chromoly steel AISI 4140 (normalized)", "steel (A36) / 4130",
             "4140 yields 655 MPa vs 4130's 435 and A36's 250",
             "{'E_mpa': 205000, 'nu': 0.29, 'density_g_cm3': 7.85, 'yield_mpa': 655, "
             "'ultimate_mpa': 1020}"),
    "a572": ("A572 Grade 50 structural steel", "steel (A36)",
             "A572-50 yields 345 MPa vs A36's 250 MPa",
             "{'E_mpa': 200000, 'nu': 0.29, 'density_g_cm3': 7.85, 'yield_mpa': 345, "
             "'ultimate_mpa': 450}"),
    "6063": ("Aluminium 6063-T6", "aluminum (6061-T6)",
             "6063-T6 yields 214 MPa vs 6061-T6's 276 MPa — substituting would be "
             "UNCONSERVATIVE",
             "{'E_mpa': 68900, 'nu': 0.33, 'density_g_cm3': 2.7, 'yield_mpa': 214, "
             "'ultimate_mpa': 241}"),
}


def _material_norm(name: object) -> str:
    """Case/whitespace/hyphen/underscore-insensitive form of a material designation."""
    s = str(name).strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def _material_candidates(raw: object) -> list:
    """Spellings to try for one designation, MOST SPECIFIC FIRST.

    Never invents a material: every candidate is still looked up in the registry, so a
    wrong guess can only fail to match, not fabricate properties."""
    out: list = []

    def add(c: str) -> None:
        c = c.strip()
        if c and c not in out:
            out.append(c)

    add(" ".join(str(raw).strip().lower().split()))   # exact (today's behaviour)
    norm = _material_norm(raw)
    add(norm)
    add(norm.replace(" ", "-"))                        # "ti 6al 4v" -> "ti-6al-4v"
    add(norm.replace(" ", ""))                         # "a 36" -> "a36" (hyphen INSIDE a
                                                       # designation: "A-36 Steel")
    toks = norm.split()
    if len(toks) > 1:
        generic = [t for t in toks if t in _MATERIAL_GENERIC_WORDS]
        kept = [t for t in toks if t not in _MATERIAL_GENERIC_WORDS]
        if kept and generic:
            add(" ".join(kept))                        # "a36 steel" -> "a36"
            add("-".join(kept))                        # "6061 t6 aluminium" -> "6061-t6"
            add("".join(kept))                         # "A-36 steel" -> "a36"
            if len(generic) == 1:                      # word order: registry spells the
                add(" ".join(kept + generic))          # category word last
                add(" ".join(generic + kept))          # ("grade 2 titanium")
    return out


def _material_registry_record(name: str) -> Optional[dict]:
    """Read a full registry record without inventing properties when the bridge is absent."""
    try:
        rec = cb.call("material_get", {"name": name}).get("result")
    except Exception:  # noqa: BLE001 — no engine / RPC unavailable: not a resolution
        return None
    return rec if isinstance(rec, dict) else None


def _material_registry_row(name: str) -> Optional[dict]:
    """Flatten mechanical data, retaining a non-isotropic model restriction for validation."""
    rec = _material_registry_record(name)
    if rec is None:
        return None
    mech = rec.get("mechanical")
    if not isinstance(mech, dict):
        return None
    row: dict = {"name": rec.get("name") or name}
    for f in ("E_mpa", "nu", "density_g_cm3", "yield_mpa", "ultimate_mpa", "endurance_mpa"):
        if f in mech:
            row[f] = mech[f]
    if rec.get("constitutive_model", "isotropic") != "isotropic":
        row["constitutive_model"] = rec["constitutive_model"]
    return row


def _material_not_modelled(raw: object) -> Optional[tuple]:
    """The (label, nearest, why, props) refusal record for a recognised-but-unmodelled grade."""
    for cand in _material_candidates(raw):
        norm = _material_norm(cand)
        hit = _MATERIAL_NOT_MODELLED.get(norm)
        if hit:
            return hit
        for tok in norm.split():
            hit = _MATERIAL_NOT_MODELLED.get(tok)
            if hit:
                return hit
    return None


def _resolve_material_name(material: Optional[str]) -> dict:
    """Name -> properties dict, via the ladder documented above. Raises ValueError when
    the designation is unknown or deliberately not substituted."""
    _ensure_materials()
    raw = material if (material or "").strip() else "steel"
    candidates = _material_candidates(raw)

    # 1. the canonical FEA table + the registry's own fea_aliases (today's fast path)
    for cand in candidates:
        key = _MATERIAL_ALIASES.get(cand, cand)
        if key in _MATERIALS:
            return dict(_MATERIALS[key])  # F4: always a COPY, never the shared dict
    # 2. designations that ARE a canonical row under another engineering name
    for cand in candidates:
        target = _MATERIAL_SAME_ALLOY.get(_material_norm(cand))
        if target:
            key = _MATERIAL_ALIASES.get(target, target)
            if key in _MATERIALS:
                return dict(_MATERIALS[key])
            row = _material_registry_row(target)
            if row:
                return row
    # 3. the FULL registry — the alloy's OWN first-party properties, no substitution
    for cand in candidates:
        row = _material_registry_row(cand)
        if row:
            return row
    # 4. recognised, but we refuse to substitute a materially different alloy
    nm = _material_not_modelled(raw)
    if nm:
        label, nearest, why, props = nm
        raise ValueError(
            f"Material {material!r} ({label}) is not in the materials registry, and it is "
            f"NOT aliased onto {nearest} because {why} — the safety factor would be wrong. "
            f"Pass explicit properties: {props}. "
            f"Use {nearest!r} only if that grade is genuinely what you want."
        )
    raise ValueError(
        f"Unknown material {material!r}. Known: {sorted(_MATERIALS)} "
        f"(or pass an explicit dict with E_mpa/nu/density_g_cm3/yield_mpa). "
        f"Registry alloy designations also resolve to their own properties, e.g. "
        f"'A36', '1018', '1045', '4130', '6061-T6', '7075-T6', '2024-T3', 'Ti-6Al-4V', "
        f"'grade 2 titanium', 'C360', 'cast iron', 'copper', 'inconel 718', 'magnesium', "
        f"'PETG', 'nylon', 'polycarbonate', 'PEEK', 'acrylic', 'concrete'."
    )


# The mechanical-property schema is {E_mpa, nu, density_g_cm3, yield_mpa} — one
# capitalized key among lowercase siblings, which is a trap of our own making.
# A model that writes the obvious `e_mpa` used to be told
# "E_mpa must be > 0 (got 0.0)" about a material dict that was completely
# correct. Observed live 2026-08-04 (k3-256k, bracket test):
# {"name": "6061-T6", "e_mpa": 68900, "nu": 0.33, "yield_mpa": 276}.
#
# Case-fold every supplied key onto its canonical spelling, plus the handful of
# unambiguous long-form aliases. Unknown keys are passed through untouched —
# guessing at them is how you silently solve with the wrong modulus.
_MATERIAL_KEY_ALIASES = {
    "e": "E_mpa",
    "e_mpa": "E_mpa",
    "empa": "E_mpa",
    "youngs_modulus_mpa": "E_mpa",
    "young_modulus_mpa": "E_mpa",
    "elastic_modulus_mpa": "E_mpa",
    "nu": "nu",
    "poisson": "nu",
    "poissons_ratio": "nu",
    "poisson_ratio": "nu",
    "yield_mpa": "yield_mpa",
    "yield_strength_mpa": "yield_mpa",
    "sigma_y_mpa": "yield_mpa",
    "density_g_cm3": "density_g_cm3",
    "density": "density_g_cm3",
    "rho_g_cm3": "density_g_cm3",
}


def _canonical_material_keys(material: dict) -> dict:
    """Copy *material* with its property keys mapped to canonical spellings.

    A caller-supplied dict is always copied so the shared table can never be
    mutated. An explicit canonical key already present WINS over an alias, so a
    dict carrying both never silently flips to the alias's value.
    """
    out: dict = {}
    aliased: dict = {}
    for key, value in material.items():
        canon = _MATERIAL_KEY_ALIASES.get(str(key).strip().lower())
        if canon is None:
            out[key] = value
        elif canon == key:
            out[canon] = value
        else:
            aliased[canon] = value
    for canon, value in aliased.items():
        out.setdefault(canon, value)
    return out


def _resolve_material_density(material: Union[str, dict, None]) -> float:
    """Resolve mass density independently of whether an isotropic FEA model is available."""
    mat = (_canonical_material_keys(material) if isinstance(material, dict)
           else _resolve_material_name(material))
    density = mat.get("density_g_cm3")
    if (isinstance(density, bool) or not isinstance(density, (int, float))
            or not math.isfinite(density) or density <= 0):
        raise ValueError(
            f"Material {material!r} carries no finite positive density_g_cm3; "
            "pass density_g_cm3= explicitly to compute mass.")
    return float(density)


def _resolve_material(material: Union[str, dict, None]) -> dict:
    """Resolve a material name or explicit dict to a validated properties dict.

    Always returns a FRESH copy so callers cannot mutate the shared _MATERIALS cache
    between runs (which would silently corrupt subsequent solves with wrong E values).
    The returned dict is validated: E_mpa and nu must be physically plausible so a bad
    entry is caught here rather than inside the C++ solver.
    """
    if isinstance(material, dict):
        mat = _canonical_material_keys(material)
    else:
        mat = _resolve_material_name(material)
    model = mat.pop("constitutive_model", "isotropic")
    if model != "isotropic":
        raise ValueError(
            f"Material {material!r} has constitutive_model={model!r}, not a qualified "
            "isotropic FEA model. Its density may be used for mass, but missing or "
            "directional stiffness/strength must not be replaced with isotropic defaults.")
    # F4: validate that the resolved dict has physically plausible mechanical properties.
    # A zero or missing E_mpa means the C++ will reject the study with a cryptic error;
    # catch it here with a useful message that names the offending material.
    E = mat.get("E_mpa", 0.0)
    nu = mat.get("nu", -999.0)
    if not (isinstance(E, (int, float)) and E > 0):
        raise ValueError(
            f"Material {material!r}: E_mpa must be > 0 (got {E!r}). "
            f"Check your material dict or the _MATERIALS table."
        )
    if not (isinstance(nu, (int, float)) and -1.0 < nu < 0.5):
        raise ValueError(
            f"Material {material!r}: nu must be in (-1, 0.5) (got {nu!r})."
        )
    return mat


# Visual material presets (APPEARANCE only — distinct from the FEA mechanical `_MATERIALS` table
# above). Each maps a friendly name to the set_appearance fields: an [r,g,b] tint (0..1) + a RealView
# finish (metallic, roughness). Mirrors the host's appearance_common.cpp preset table so a material
# named in chat looks the same as one picked in the PropertyManager.
_APPEARANCE_PRESETS = {
    "plastic":   {"color": [0.71, 0.76, 0.82], "metallic": 0.0,  "roughness": 0.50},
    "abs":       {"color": [0.20, 0.21, 0.23], "metallic": 0.0,  "roughness": 0.60},
    "aluminum":  {"color": [0.83, 0.85, 0.88], "metallic": 0.90, "roughness": 0.35},
    "steel":     {"color": [0.56, 0.58, 0.62], "metallic": 0.85, "roughness": 0.25},
    "stainless": {"color": [0.69, 0.71, 0.74], "metallic": 0.88, "roughness": 0.30},
    "brass":     {"color": [0.80, 0.62, 0.28], "metallic": 0.95, "roughness": 0.30},
    "copper":    {"color": [0.85, 0.54, 0.40], "metallic": 0.95, "roughness": 0.30},
    "gold":      {"color": [0.90, 0.74, 0.36], "metallic": 0.97, "roughness": 0.28},
    "titanium":  {"color": [0.62, 0.61, 0.60], "metallic": 0.85, "roughness": 0.42},
    "nylon":     {"color": [0.90, 0.89, 0.85], "metallic": 0.0,  "roughness": 0.55},
    "black":     {"color": [0.09, 0.10, 0.12], "metallic": 0.0,  "roughness": 0.50},
    # Non-metal families a hobby/robotics build names in chat (live 2026-09-28: a
    # plywood rover's `set_material("birch plywood")` failed outright). Looks only:
    # engineering identity/density still come from the registry or stay unset.
    "wood":      {"color": [0.72, 0.51, 0.28], "metallic": 0.0,  "roughness": 0.65},
    "dark wood": {"color": [0.36, 0.24, 0.15], "metallic": 0.0,  "roughness": 0.60},
    "mdf":       {"color": [0.63, 0.50, 0.36], "metallic": 0.0,  "roughness": 0.80},
    "clear":     {"color": [0.85, 0.90, 0.95], "metallic": 0.0,  "roughness": 0.10},
    "rubber":    {"color": [0.12, 0.12, 0.13], "metallic": 0.0,  "roughness": 0.85},
}
_APPEARANCE_ALIASES = {
    "aluminium": "aluminum", "al": "aluminum", "ss": "stainless",
    "stainless steel": "stainless", "pla": "plastic", "petg": "plastic",
    "asa": "plastic", "resin": "plastic", "abs plastic": "abs",
    "dark metal": "black", "ti": "titanium", "pa": "nylon", "nylon 6": "nylon",
    "polyamide": "nylon", "mild steel": "steel", "carbon steel": "steel",
    "tool steel": "steel",
    "plywood": "wood", "birch": "wood", "birch plywood": "wood", "baltic birch": "wood",
    "oak": "wood", "pine": "wood", "maple": "wood", "beech": "wood", "bamboo": "wood",
    "hardwood": "wood", "softwood": "wood", "lumber": "wood", "timber": "wood",
    "walnut": "dark wood", "mahogany": "dark wood",
    "hardboard": "mdf", "particleboard": "mdf",
    "acrylic": "clear", "pmma": "clear", "plexiglass": "clear",
    "polycarbonate": "clear", "lexan": "clear",
    "tpu": "rubber", "tpe": "rubber", "silicone": "rubber",
}

# Engineering ALLOY DESIGNATIONS -> the appearance family they belong to. Consulted BEFORE the
# family-word token scan in _resolve_appearance, because a designation can CONTAIN a FOREIGN
# family token: "Ti-6Al-4V" (titanium grade 5) tokenises to {ti, al, v} and the "al" alias
# rendered it as ALUMINIUM with no error at all (measured live 2026-07-29). A wrong metal that
# reports success is worse than a refusal, so designations win over loose token matching.
#
# Only UNAMBIGUOUS designations are listed. 4-digit AISI steel (1018) and 4-digit wrought
# aluminium (1100) share a leading digit, and "A2" is both a stainless fastener class and a tool
# steel — so each entry is spelled out rather than derived from a series regex, and anything not
# listed falls through to the token scan and then to a NAMED error. Never guessed.
_ALLOY_DESIGNATIONS = {
    # wrought aluminium
    "1100": "aluminum", "2011": "aluminum", "2014": "aluminum", "2017": "aluminum",
    "2024": "aluminum", "3003": "aluminum", "5052": "aluminum", "5083": "aluminum",
    "5086": "aluminum", "6005": "aluminum", "6061": "aluminum", "6063": "aluminum",
    "6082": "aluminum", "7050": "aluminum", "7075": "aluminum",
    # carbon / alloy / tool steel
    "a36": "steel", "a53": "steel", "a500": "steel", "a514": "steel", "a572": "steel",
    "1008": "steel", "1010": "steel", "1018": "steel", "1020": "steel", "1026": "steel",
    "1045": "steel", "1095": "steel", "12l14": "steel", "4130": "steel", "4140": "steel",
    "4142": "steel", "4340": "steel", "8620": "steel", "o1": "steel", "d2": "steel",
    "s7": "steel", "m2": "steel", "h13": "steel",
    # stainless
    "303": "stainless", "304": "stainless", "304l": "stainless", "309": "stainless",
    "310": "stainless", "316": "stainless", "316l": "stainless", "321": "stainless",
    "347": "stainless", "410": "stainless", "416": "stainless", "420": "stainless",
    "430": "stainless", "440c": "stainless", "17-4": "stainless", "17-4ph": "stainless",
    "15-5": "stainless", "2205": "stainless",
    # brass / copper
    "c260": "brass", "c360": "brass", "c464": "brass", "c110": "copper", "c101": "copper",
    "c11000": "copper",
    # titanium
    "ti-6al-4v": "titanium", "ti6al4v": "titanium", "ti-64": "titanium", "ti64": "titanium",
    "grade5titanium": "titanium", "grade2titanium": "titanium", "cpti": "titanium",
}


def _resolve_appearance(material: Union[str, dict, list, tuple]) -> dict:
    """Resolve a visual-material name / dict / [r,g,b] to set_appearance fields {color[,metallic,
    roughness]}. Raises ValueError on an unknown name or a dict missing a colour."""
    if isinstance(material, dict):
        ap: dict = {}
        col = material.get("color", material.get("rgb"))
        if col is not None:
            ap["color"] = [float(c) for c in col][:3]
        if material.get("metallic") is not None:
            ap["metallic"] = float(material["metallic"])
        if material.get("roughness") is not None:
            ap["roughness"] = float(material["roughness"])
        if "color" not in ap:
            raise ValueError("appearance dict needs a 'color': [r,g,b] (0..1)")
        return ap
    if isinstance(material, (list, tuple)):
        return {"color": [float(c) for c in material][:3]}
    # A registry material (name, alias or designation) is accepted here exactly as set_material
    # accepts it as an engineering identity. Product-specific looks outrank generic family
    # tokens (e.g. black printed TPU or natural POM); these are illustrative finishes, not
    # mechanical qualifications.
    record = None
    for candidate in _material_candidates(material):
        record = _material_registry_record(candidate)
        if record is not None:
            break
    if record is not None and isinstance(record.get("appearance"), dict):
        return _resolve_appearance(record["appearance"])
    key = _appearance_family_key(material)
    if key is None and record is not None:
        # A registry row without its own finish: read its name/aliases for a family word,
        # else its category's neutral look. Never a refusal for a material set_material
        # resolves as an engineering identity.
        for alias in [record.get("name"), *(record.get("aliases") or [])]:
            key = _appearance_family_key(alias) if isinstance(alias, str) else None
            if key is not None:
                break
        if key is None and record.get("category") in _REGISTRY_CATEGORY_LOOKS:
            return dict(_APPEARANCE_PRESETS[_REGISTRY_CATEGORY_LOOKS[record["category"]]])
    if key is None:
        raise ValueError(
            f"Unknown material {material!r}: not a name, alias or designation in the "
            f"engineering material registry (e.g. A36, 6061-T6, ASTM A106 Grade B, A53 Gr B, "
            f"SA-516-70, A312 TP316) and no appearance family matches. Families: "
            f"{sorted(set(_APPEARANCE_PRESETS) | set(_APPEARANCE_ALIASES))}; "
            f"common alloy designations (6061, A36, 304, 4140, Ti-6Al-4V, C360, …) resolve to "
            f"their family. Or pass an explicit look: "
            f"{{'color':[r,g,b], 'metallic':0..1, 'roughness':0..1}}."
        )
    return dict(_APPEARANCE_PRESETS[key])


# Neutral look for a registry row that names no finish and no family word (Inconel 718,
# tungsten, PEEK, concrete, ...). Illustrative only; the engineering identity is the row.
_REGISTRY_CATEGORY_LOOKS = {
    "metal": "steel", "plastic": "plastic", "composite": "black", "ceramic": "plastic",
    "elastomer": "rubber", "adhesive": "plastic",
}


def _appearance_family_key(material: object) -> Optional[str]:
    """The appearance preset a free-text name lands on (alias, designation, family word)."""
    key = str(material).strip().lower()
    key = _APPEARANCE_ALIASES.get(key, key)
    if key not in _APPEARANCE_PRESETS:
        import re as _re  # noqa: PLC0415
        # 1. An explicit ALLOY DESIGNATION wins outright. It must be tried before the loose
        #    token scan below, which reads any family word ANYWHERE in the string and therefore
        #    mis-read "Ti-6Al-4V" as aluminium. Compacted so "6061-T6" / "316L" / "A36 steel"
        #    all reduce to a prefix of their designation; longest designation first so "316l"
        #    is preferred over "316" and "17-4ph" over "17-4".
        compact = _re.sub(r"[^a-z0-9]", "", key)
        for desig in sorted(_ALLOY_DESIGNATIONS, key=len, reverse=True):
            flat = _re.sub(r"[^a-z0-9]", "", desig)
            if compact == flat or compact.startswith(flat):
                key = _ALLOY_DESIGNATIONS[desig]
                break
    if key not in _APPEARANCE_PRESETS:
        # 2. A family WORD anywhere in the string: what a CAD model actually says —
        #    "6061-T6 Al", "PLA filament", "mild steel" (live 2026-07-27: haiku lost a call to
        #    each of "A36 steel" and "6061-T6 Al").
        #
        #    Precedence is EARLIEST-MATCH, then longest. Two reasons, both load-bearing:
        #      * Alloy designations name the BASE metal first ("Ti-6Al-4V" is titanium with 6 %
        #        aluminium), so the leading family word is the subject and a later one is an
        #        ingredient. Scanning by length alone read that as aluminium.
        #      * The candidate pool is built from a set, so equal-length aliases ("al", "ti")
        #        had NO defined order — the answer depended on set iteration. A material that
        #        renders differently run to run is the worst version of this bug, and sorting
        #        by (position, -length, name) removes the nondeterminism entirely.
        #    Longest-at-the-same-position still wins, so "stainless steel" beats "stainless".
        import re as _re  # noqa: PLC0415
        best: Optional[tuple] = None
        for cand in set(_APPEARANCE_PRESETS) | set(_APPEARANCE_ALIASES):
            # Whole-word match so "al" cannot fire inside "alloy" and "ti" not inside "tin".
            m = _re.search(rf"(?<![a-z0-9]){_re.escape(cand)}(?![a-z0-9])", key)
            if m is None:
                continue
            rank = (m.start(), -len(cand), cand)
            if best is None or rank < best[0]:
                best = (rank, cand)
        if best is not None:
            key = _APPEARANCE_ALIASES.get(best[1], best[1])
    return key if key in _APPEARANCE_PRESETS else None


# ------------------------------------------------------------------ free-text geometry queries
# The engine's query grammar (core/doc/picks.cpp — resolve_geom_query plus parse_side /
# canon_surf / parse_axis_vec) is a handful of TOKENS, not prose. `_as_face_query` used to hand
# the engine whatever string it was given as `side`, so a model that wrote a sentence got back
#     geom query: bad 'side' 'outer cylindrical wall face at bottom near x=200,y=0'
# — a dead end for a query whose intent the grammar expresses perfectly
# ({"of":"face","surf":"cylinder","side":"bottom","pick":"outer"}). We now PARSE the sentence
# into that grammar and only ever emit tokens picks.cpp accepts; when nothing is extractable we
# raise a teaching error rather than guess, because a `side` full of prose teaches nothing.

# Word -> parse_side() token. parse_side also takes the signed axes (+x/-x/+y/-y/+z/-z) verbatim.
# The word->axis convention is the host's named views: top=+Z, right=+X, front=-Y, back=+Y.
_QSIDE_WORDS = {
    "top": "top", "upper": "top", "uppermost": "top", "up": "top",
    "bottom": "bottom", "lower": "bottom", "lowermost": "bottom", "down": "bottom",
    "underside": "bottom",
    "left": "left", "right": "right",
    "front": "front", "back": "back", "rear": "back",
}
_QSIDE_AXIS_RE = re.compile(r"[+-][xyz]")

# Word -> canon_surf() token, per topology kind. "round" is the engine's CURVED CATEGORY (any
# non-planar face / non-line edge), not a single kernel type — canon_surf resolves it per kind,
# so the face and edge tables must stay separate ("flat" is meaningless for an edge).
_QSURF_FACE = {
    "flat": "plane", "planar": "plane", "plane": "plane",
    "round": "round", "rounded": "round", "curved": "round",
    "cylindrical": "cylinder", "cylinder": "cylinder",
    "conical": "cone", "cone": "cone",
    "spherical": "sphere", "sphere": "sphere",
    "toroidal": "torus", "torus": "torus",
    "bspline": "bspline",
}
_QSURF_EDGE = {
    "straight": "line", "linear": "line", "line": "line",
    "round": "round", "rounded": "round", "curved": "round",
    "circular": "circle", "circle": "circle",
    "elliptical": "ellipse", "ellipse": "ellipse",
    "bspline": "bspline",
}

# Word -> pick token: largest/biggest/longest = max measure, smallest/shortest = min,
# inner/outer classify coaxial round faces by concavity (bore vs boss).
_QPICK_WORDS = {
    "largest": "largest", "biggest": "largest", "longest": "longest",
    "smallest": "smallest", "shortest": "shortest",
    "outer": "outer", "outermost": "outer", "outside": "outer",
    "inner": "inner", "innermost": "inner", "inside": "inner", "bore": "inner",
}

# parse_axis_vec() filters on |dot|, so the sign is irrelevant — every spelling folds to a letter.
_QAXIS_WORDS = {"x": "x", "+x": "x", "-x": "x", "y": "y", "+y": "y", "-y": "y",
                "z": "z", "+z": "z", "-z": "z", "vertical": "z"}
_QAXIS_FILLER = {"to", "the", "a", "an", "of", "with", "along", "axis", "axes",
                 "direction", "dir", "vector", "world", "global"}

# Signed axis tokens stay whole ("+z"); everything else is a bare word. Digits never tokenise, so
# a trailing "near x=200,y=0" contributes nothing (a bare "x" is not a side — parse_side needs a
# sign), and it can never leak into an emitted value.
_QTOKEN_RE = re.compile(r"[+-][xyz](?![a-z0-9])|[a-z]+")

_QVOCAB_HELP = (
    '{"of":"face"|"edge" (default face), '
    '"side":top/bottom/left/right/front/back or +x/-x/+y/-y/+z/-z, '
    '"surf": faces flat/round/cylindrical/conical/spherical/toroidal, edges straight/round/'
    'circular, "pick":largest/smallest/longest/shortest/inner/outer, '
    '"parallel"|"perpendicular":"x"|"y"|"z", "near":[x,y,z], "count":N}'
)

# A request for the part's OVERALL extent, which no face/edge query can answer. Kept separate
# from the token grammar on purpose: these words must not become query keys, they must route the
# model to the tool that reports a bbox (describe_part). "bbox"/"bounding box"/"overall
# size"/"dimensions"/"extents"/"how big" are the spellings measured in live runs.
# Separators are [\s_-] because models write bounding_box / bounding-box as often as two words,
# and a JSON list renders as "['mass', 'bounding_box']" (measured live 2026-07-29).
_BBOX_INTENT_RE = re.compile(
    r"(?<![a-z0-9])(bbox|bounding[\s_-]*box|overall[\s_-]+(?:size|dimension|dimensions|extent|"
    r"extents)|extents?|dimensions|how[\s_-]+big)(?![a-z0-9])")


def _q_next_axis(toks: list[str], i: int) -> Optional[str]:
    """The axis letter qualifying toks[i] ("parallel to the -z axis" -> "z"), skipping filler.

    None when no axis follows, so a phrase like "perpendicular to the top face" emits NO
    parallel/perpendicular key — the engine silently ignores an unparseable one, and a filter
    that vanishes without a word is exactly the failure mode this module exists to prevent.
    """
    for t in toks[i + 1:i + 5]:
        if t in _QAXIS_WORDS:
            return _QAXIS_WORDS[t]
        if t not in _QAXIS_FILLER:
            return None
    return None


def _as_face_query(q: Union[str, dict]) -> dict:
    """Accept a geometry-query dict (returned untouched) OR free text -> a structured query.

    Free text is parsed into the engine's token grammar: a bare side word ("top", "+z") still
    means {"of":"face","side":"top"}, and "outer cylindrical wall face at bottom" becomes
    {"of":"face","surf":"cylinder","side":"bottom","pick":"outer"}. Raises ValueError naming the
    vocabulary when the text carries no recognised token.
    """
    if isinstance(q, dict):
        return q

    text = str(q).strip().lower()
    # Bare side word / signed axis — the original fast path, same result as before.
    if text in _QSIDE_WORDS:
        return {"of": "face", "side": _QSIDE_WORDS[text]}
    if _QSIDE_AXIS_RE.fullmatch(text):
        return {"of": "face", "side": text}

    toks = _QTOKEN_RE.findall(text)
    tokset = set(toks)
    # An explicit "face"/"edge" mention is itself an extraction: {"of":"edge"} is a valid query
    # (every edge), so "all the edges" must resolve rather than raise.
    named_kind = bool(tokset & {"face", "faces", "edge", "edges"})
    of = "edge" if tokset & {"edge", "edges"} else "face"
    surf_words = _QSURF_EDGE if of == "edge" else _QSURF_FACE

    side = surf = pick = None
    for t in toks:
        if side is None and (t in _QSIDE_WORDS or _QSIDE_AXIS_RE.fullmatch(t)):
            side = _QSIDE_WORDS.get(t, t)
        if surf is None and t in surf_words:
            surf = surf_words[t]
        if pick is None and t in _QPICK_WORDS:
            pick = _QPICK_WORDS[t]

    # Orientation. A FACE's axis is its NORMAL while an EDGE's axis is its DIRECTION, so
    # vertical/horizontal swap keys between the two: a vertical wall face has a normal
    # PERPENDICULAR to Z, whereas a vertical edge runs PARALLEL to Z.
    orient: dict = {}
    for i, t in enumerate(toks):
        if t in ("parallel", "perpendicular"):
            ax = _q_next_axis(toks, i)
            if ax:
                orient = {t: ax}
                break
        elif t in ("vertical", "upright"):
            orient = {"perpendicular" if of == "face" else "parallel": "z"}
            break
        elif t == "horizontal":
            orient = {"parallel" if of == "face" else "perpendicular": "z"}
            break

    if not (side or surf or pick or orient or named_kind):
        # A size/extent request is not a face/edge query at all — it is a different TOOL, and the
        # vocabulary wall alone never says which (measured 2026-07-29: query_geometry("bbox") and
        # ("bounding box") both dead-ended in the token grammar). describe_part's outline opens
        # with `bbox WxDxH mm`; measure_part carries volume/mass/centroid but NO bbox — so name
        # the tool that actually answers, and keep the two distinct. Checked ONLY here, after the
        # grammar found nothing, so a real query that happens to contain one of these words
        # ("the top face dimensions") still resolves as the query it is.
        if _BBOX_INTENT_RE.search(text):
            raise ValueError(
                f"{q!r} asks for the part's overall SIZE, which is not a face/edge query — this "
                f"tool resolves individual faces/edges. Call describe_part(doc_id) instead: its "
                f"outline opens with `bbox WxDxH mm` (plus the flat-face sid roster). For volume "
                f"/ mass / centre of mass call measure_part(doc_id) — it does NOT report a bbox. "
                f"To measure ONE face or edge, name it (e.g. \"top\") or pass a query "
                f"{_QVOCAB_HELP}.")
        raise ValueError(
            f"Can't read {q!r} as a geometry query — the grammar is TOKENS, not prose. "
            f"Pass a dict {_QVOCAB_HELP}, e.g. "
            '{"of":"face","surf":"cylinder","side":"bottom","pick":"outer"} or the bare side '
            'word "top". For open-ended exploration call describe_part (the named face/edge '
            "roster with stable sids), then hover(sid) on one of them.")

    out = {"of": of}
    if surf:
        out["surf"] = surf
    if side:
        out["side"] = side
    if pick:
        out["pick"] = pick
    out.update(orient)
    return out


# --------------------------------------------------------------------------- close-up inspection
# doc_render_view / doc_render_png only accept a WHOLE-PART camera direction (no bbox/eye
# override). Close-up inspection therefore: (1) resolve the target, (2) pick a face-normal
# (or fit) view dir, (3) render the whole part from that dir, (4) CROP the PNG to the
# projected target bbox + margin and upscale. Caption is honest about the crop fallback.
_RENDER_W = 512
_RENDER_H = 400
_RENDER_FIT = 0.85  # matches core/doc/session.cpp render_view_to_rgb


def _v3(v, default=(0.0, 0.0, 0.0)) -> tuple:
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return (float(v[0]), float(v[1]), float(v[2]))
        except (TypeError, ValueError):
            pass
    return default


def _vnorm(v) -> tuple:
    x, y, z = _v3(v)
    n = math.sqrt(x * x + y * y + z * z)
    if n < 1e-12:
        return (0.0, 0.0, 1.0)
    return (x / n, y / n, z / n)


def _vcross(a, b) -> tuple:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _vdot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vsub(a, b) -> tuple:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


# --------------------------------------------------------------- inspect/highlight target words
# A stable sid as emitted by describe_part / query_geometry: one KIND letter (F face, E edge,
# V vertex) plus six hex digits — core/doc/selectors.cpp stable_sid().
_SID_RE = re.compile(r"[fev][0-9a-f]{6}", re.I)

# Every token the geometry grammar recognises, plus the connectives it ignores. A string that
# carries a token OUTSIDE this set is prose — most often a FEATURE NAME ("Boss") — and must never
# be shipped to the engine as a `side` value (that is the `geom query: bad 'side' 'Boss'` bug).
_QKNOWN_TOKENS = (
    set(_QSIDE_WORDS) | set(_QSURF_FACE) | set(_QSURF_EDGE) | set(_QPICK_WORDS)
    | set(_QAXIS_WORDS) | set(_QAXIS_FILLER)
    | {"parallel", "perpendicular", "vertical", "horizontal", "upright",
       "face", "faces", "edge", "edges", "surface", "surfaces",
       "feature", "features", "part", "body", "and", "its", "on", "at", "in", "near"}
)


def _query_unknown_tokens(text) -> list:
    """Words in `text` the geometry grammar has no meaning for (digits never tokenise)."""
    return [t for t in _QTOKEN_RE.findall(str(text).lower())
            if t not in _QKNOWN_TOKENS and not _QSIDE_AXIS_RE.fullmatch(t)]


def _name_or_query(text, of: Optional[str] = None) -> dict:
    """Read a leftover phrase as a geometry query when it is built from grammar TOKENS, else as a
    feature NAME.

    Returns a query dict, or {"feature_name": text} — plus "fallback_query" when the phrase mixes
    real tokens with unknown words ("outer cylindrical wall face"), so the resolver can try the
    name first and still fall back to the query instead of dead-ending.
    """
    name = " ".join(str(text).strip().split())
    try:
        q = dict(_as_face_query(name))
    except ValueError:
        return {"feature_name": name}
    if of:
        q["of"] = of
    if _query_unknown_tokens(name):
        return {"feature_name": name, "fallback_query": q}
    return q


def _parse_inspect_target(target: Union[str, dict]) -> dict:
    """Normalize an inspect target into a geom-query dict, a sid handle, a feature handle or a
    feature NAME.

    Accepts:
      * geom-query dict  {"of":"face","side":"top"} / {"of":"edge","parallel":"z"}
      * sid string       "F58db6d" / "E1c4a90" — describe_part's stable face/edge handles
      * feature id       "f2" / "F3"   -> {"feature_id": "f2"}
      * feature name     "Boss"        -> {"feature_name": "Boss"}
      * bare side word   "top" / "+z"  -> face query
      * plain phrase     "the filleted edge" / "fillet faces" — light NL expansion onto the
                         same query vocabulary (round faces / circle edges).

    A phrase the grammar cannot read comes back as a feature NAME, never as a `side` value the
    engine will reject; _resolve_inspect_target turns the handle into real matches.
    """
    if isinstance(target, dict):
        if target.get("feature_id") or target.get("feature"):
            fid = str(target.get("feature_id") or target.get("feature")).strip()
            return {"feature_id": fid}
        if "of" in target or target.get("kind") == "query":
            return dict(target)
        # Resolved selector dict (sid/kind/centroid) — pass through for measure / bbox.
        if target.get("kind") in ("face", "edge") and target.get("centroid") is not None:
            return {"selector": target}
        # Bare {"sid": "F58db6d"} handle — resolved against the live face/edge roster.
        if target.get("sid"):
            return {"sid": str(target["sid"]).strip()}
        return dict(target)

    s = str(target).strip()
    if not s:
        raise ValueError("inspect target is empty — pass a face/edge query or feature id")

    import re as _re

    # Stable sid first: "f123456" is a legal sid AND matches the feature-id shape, and a sid is
    # the more specific reading. _resolve_inspect_target falls back to the feature reading when
    # no such sid exists in the document.
    if _SID_RE.fullmatch(s):
        return {"sid": s}

    # Feature id from the tree: f1 / F12.
    m = _re.fullmatch(r"[fF](\d+)", s)
    if m:
        return {"feature_id": f"f{m.group(1)}"}

    low = s.lower()
    # Strip leading articles so "the top face" still resolves.
    low_bare = _re.sub(r"^(the|a|an)\s+", "", low).strip()

    # Side-word face shortcuts (same as query_geometry bare words).
    side_words = {
        "top", "bottom", "front", "back", "left", "right",
        "+z", "-z", "+y", "-y", "+x", "-x",
    }
    if low_bare in side_words or low in side_words:
        return {"of": "face", "side": low_bare if low_bare in side_words else low}

    # Fillet / round phrasing → round faces (prefer) or circular edges.
    if any(w in low for w in ("fillet", "filleted", "rounded", "round edge", "round face")):
        if "edge" in low:
            return {"of": "edge", "surf": "circle"}
        return {"of": "face", "surf": "round"}

    if "chamfer" in low:
        if "edge" in low:
            return {"of": "edge", "surf": "line", "pick": "shortest"}
        return {"of": "face", "surf": "plane", "pick": "smallest"}

    # "top face" / "vertical edges" style: peel the of-kind suffix.
    if low_bare.endswith(" faces") or low_bare.endswith(" face"):
        stem = _re.sub(r"\s+faces?$", "", low_bare).strip()
        if stem in side_words:
            return {"of": "face", "side": stem}
        if stem in ("round", "rounded", "curved", "cylindrical"):
            return {"of": "face", "surf": "round"}
        return _name_or_query(stem, "face")
    if low_bare.endswith(" edges") or low_bare.endswith(" edge"):
        stem = _re.sub(r"\s+edges?$", "", low_bare).strip()
        if stem in ("vertical", "upright"):
            return {"of": "edge", "parallel": "z"}
        if stem in ("horizontal",):
            return {"of": "edge", "parallel": "x"}  # not perfect; caller can refine
        if stem in side_words:
            return {"of": "edge", "side": stem}
        if stem in ("round", "rounded", "filleted", "circular", "circle"):
            return {"of": "edge", "surf": "circle"}
        return _name_or_query(stem, "edge")

    # Neither a side word nor a grammar phrase → read it as a NAME (the resolver joins it to the
    # feature tree) instead of emitting a `side` the engine can only reject.
    return _name_or_query(s)


def _target_feature_roster(doc_id: str) -> tuple:
    """(features, owner_map) from doc_tree: the feature roster the tree lists ([{id,name,type}])
    and describe_part's feature->topology join table.

    Never raises — an unreachable tree degrades to an empty roster so target resolution can still
    report what it tried instead of masking the real error with an RPC failure.
    """
    try:
        tree = cb.call("doc_tree", {"doc_id": doc_id}) or {}
    except Exception:  # noqa: BLE001
        return [], {}
    feats = [f for f in (((tree.get("tree") or {}).get("features")) or [])
             if isinstance(f, dict)]
    return feats, (tree.get("owner_map") or {})


def _roster_text(feats: list, limit: int = 12) -> str:
    """`f1="Plate", f2="Boss"` — the id+name list every target error quotes so a model that
    guessed a name once can see the real ones and retry without another exploration call."""
    if not feats:
        return "(none — this document's tree lists no features yet)"
    bits = [f'{f.get("id")}="{f.get("name")}"' for f in feats[:limit]]
    if len(feats) > limit:
        bits.append(f"+{len(feats) - limit} more")
    return ", ".join(bits)


def _norm_feature_name(text) -> str:
    """Fold a feature name for comparison: lowercase, collapsed whitespace, no leading article
    and no trailing "feature" — so "  the Boss feature " matches the feature named "Boss"."""
    s = " ".join(str(text or "").strip().lower().split())
    s = re.sub(r"^(?:the|a|an)\s+", "", s)
    s = re.sub(r"\s+feature$", "", s)
    return s.strip()


def _features_named(feats: list, text) -> list:
    """Every feature whose name folds to `text` (case-insensitive, whitespace tolerant)."""
    want = _norm_feature_name(text)
    if not want:
        return []
    return [f for f in feats if _norm_feature_name(f.get("name")) == want]


def _sid_match(doc_id: str, sid: str) -> Optional[dict]:
    """The live doc_query match carrying this stable sid, or None.

    The sid's first letter IS its kind (F face / E edge / V vertex — stable_sid), so only that
    roster is scanned. Vertices aren't queryable, so a V sid never matches.
    """
    want = str(sid).strip().lower()
    for of in {"f": ("face",), "e": ("edge",)}.get(want[:1], ("face", "edge")):
        try:
            res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": of}}) or {}
        except Exception:  # noqa: BLE001
            continue
        for m in res.get("matches") or []:
            if str(m.get("sid") or "").lower() == want:
                return m
    return None


def _resolve_feature_ref(doc_id: str, ref, feats: Optional[list] = None,
                         what: str = "target") -> tuple:
    """(feature_id, feature_name, feats) for a feature id ("f2"/"F2") or NAME ("Boss").

    Name matching is case-insensitive, whitespace-tolerant, and ignores a leading article or a
    trailing "feature". Every failure TEACHES: the candidate list when a name is shared by
    several features, otherwise the whole id+name roster plus the describe_part sid pointer —
    a name must never reach the engine's side parser.
    """
    if feats is None:
        feats, _om = _target_feature_roster(doc_id)
    s = " ".join(str(ref or "").strip().split())
    if not s:
        raise ValueError(
            f"empty {what} — name a feature (id f1, f2, … or its name), a face/edge sid from "
            f"describe_part(doc_id), or pass a geometry query {_QVOCAB_HELP}.")

    m = re.fullmatch(r"[fF](\d+)", s)
    if m:
        fid = f"f{m.group(1)}"
        for f in feats:
            if str(f.get("id")) == fid:
                return fid, f.get("name"), feats
        raise ValueError(
            f"No feature {fid!r} in this document. Features: {_roster_text(feats)}. "
            f"If you meant one face/edge rather than a feature, describe_part(doc_id) lists "
            f"every sid (F58db6d / E1c4a90 style).")

    hits = _features_named(feats, s)
    if len(hits) == 1:
        return str(hits[0].get("id")), hits[0].get("name"), feats
    if len(hits) > 1:
        cands = ", ".join(f'{h.get("id")}="{h.get("name")}"' for h in hits)
        raise ValueError(
            f"Feature name {s!r} is ambiguous — {len(hits)} features share it: {cands}. "
            f"Pick one by id.")
    raise ValueError(
        f"Can't resolve {what} {s!r}: no feature has that name, and it is neither a face/edge "
        f"sid (F58db6d / E1c4a90 — describe_part(doc_id) lists them) nor a geometry-query "
        f"phrase. Features in this part: {_roster_text(feats)}. Geometry queries take "
        f"{_QVOCAB_HELP}.")


# ------------------------------------------------- what a feature actually CREATED (vs re-cut)
# Every boolean RE-CREATES the neighbour faces its tool touches, and OCCT credits those
# regenerated faces to the feature that ran the boolean. The smear runs in BOTH directions:
# a CUT is handed the faces it merely pierced (an r8 through-bore owns the plate's whole
# 9399 mm^2 top and bottom face) and a FUSE is handed the faces it merely landed on (an r18
# boss owns the plate's 8582 mm^2 top; a 25 mm spout owns a bucket's 309048 mm^2 outer wall).
# A caller that frames, tints or narrates "the boss" then lights up the entire part.
#
# The kernel's real provenance (CompileResult::tool_recs, the pre-boolean tool solid per
# feature) never leaves C++: doc_query's per-face `owner_feature` is hardcoded null
# (core/doc/picks.cpp), and eval_feature_overlap only reports pairwise common VOLUME between
# tool solids, never per-face membership. So ownership is reconstructed here from the two
# things Python CAN see, cheapest first:
#
#   1. TOOL SCALE, from the AUTHORED params — no RPC. A feature's tool cannot create a face
#      larger than its own cross-section: an r25 spout did not author an r165 wall, an r18
#      boss did not author an 8582 mm^2 plane. A feature type with no analytic cross-section
#      (extrude/revolve/loft/sweep/shell/fillet) has no scale and is left alone.
#   2. TOOL FOOTPRINT, from the real tessellation. A prism/revolve tool's LATERAL faces run
#      ALONG its axis and are always genuinely new; its CAP faces run ACROSS it and are either
#      the tool's own floor/top — which fits INSIDE the lateral footprint — or a neighbour it
#      merely punched through or landed on, which does not. This decides what (1) cannot: an
#      r50 bore through a 120x80 plate leaves a 1746 mm^2 top face, SMALLER than the tool's own
#      7854 mm^2 cross-section, so only the footprint can tell it is not the bore's.
#
# Neither test can invent geometry: they only ever drop faces from the owner_map join. A face
# dropped from its claimed owner is re-credited to the latest EARLIER feature whose tool could
# have authored it, and left unowned when none could — faces are conserved, never duplicated,
# and a feature left with nothing is reported as owning nothing rather than back-filled.

# Feature types that remove material outright — no `mode` param to read.
_SUBTRACTIVE_TYPES = frozenset({"hole", "bolt_circle"})
# Types that REPLAY another feature's tool, so their add/cut role is the seed's role.
_SEEDED_TYPES = frozenset({"linear_pattern", "circular_pattern", "curve_pattern",
                           "sketch_pattern", "table_pattern", "pattern", "mirror"})


def _feature_seed_ids(feat: dict) -> list:
    """Feature ids a pattern/mirror replays (params.seed / refs.seed / refs.features / a body
    selector's owner_feature) — a pattern of a cut is itself subtractive."""
    out: list = []
    for src in (feat.get("params") or {}, feat.get("refs") or {}):
        if not isinstance(src, dict):
            continue
        for key in ("seed", "seeds", "feature", "features"):
            v = src.get(key)
            cands = v if isinstance(v, list) else [v]
            for c in cands:
                if isinstance(c, str):
                    out.append(c)
                elif isinstance(c, dict):
                    o = c.get("owner_feature") or c.get("feature")
                    if isinstance(o, str):
                        out.append(o)
    return out


def _feature_is_subtractive(feat, feats: Optional[list] = None, _depth: int = 0) -> bool:
    """True when this feature REMOVES material, so the kernel re-created — and mis-attributed
    to it — the neighbour faces its tool passed through."""
    if not isinstance(feat, dict) or _depth > 4:
        return False
    ftype = str(feat.get("type") or "").strip().lower()
    if ftype in _SUBTRACTIVE_TYPES:
        return True
    params = feat.get("params") if isinstance(feat.get("params"), dict) else {}
    if str(params.get("mode") or "").strip().lower() in ("cut", "subtract", "remove"):
        return True
    if ftype == "boolean":
        return str(params.get("op") or "").strip().lower() in ("cut", "subtract", "difference")
    if ftype in _SEEDED_TYPES and feats:
        by_id = {str(f.get("id")): f for f in feats if isinstance(f, dict)}
        return any(_feature_is_subtractive(by_id.get(s), feats, _depth + 1)
                   for s in _feature_seed_ids(feat))
    return False


# A feature type that never owns solid faces (references/sketches) — never a candidate owner.
_NON_BODY_FEATURE_TYPES = frozenset({"sketch", "plane", "axis", "point", "csys"})
_TOOL_SCALE_EPS = 1.02  # slack for tessellation/imprint noise on an exact-size match


def _solid_feature_order(feats: Optional[list]) -> list:
    """Feature ids in tree order, skipping sketches/datums — the only features that can own a
    face, and the order the de-smear walks BACKWARDS when it re-credits one."""
    return [str(f.get("id")) for f in (feats or [])
            if isinstance(f, dict) and f.get("id")
            and f.get("type") not in _NON_BODY_FEATURE_TYPES]


def _dim_num(v) -> Optional[float]:
    """A dimension param's number, from {"value":..,"expr":..} or a bare number."""
    if isinstance(v, dict):
        v = v.get("value")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _feature_tool_scale(ft) -> Optional[dict]:
    """{"radius": largest curved radius this tool can produce (0.0 = none at all),
        "plane_area": largest planar face this tool can produce}, from the AUTHORED params.
    None for a feature type whose tool has no analytic cross-section — such a feature is
    unconstrained and keeps whatever the kernel attributed to it."""
    if not isinstance(ft, dict):
        return None
    t = (ft.get("type") or "").lower()
    p = ft.get("params") or {}
    if not isinstance(p, dict):
        return None

    def _radius(*keys) -> Optional[float]:
        vals = [_dim_num(p.get(k)) for k in keys]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    if t == "box":
        length, width, height = (_dim_num(p.get(k)) for k in ("length", "width", "height"))
        if None in (length, width, height):
            return None
        # A box tool's boundary is entirely planar: it cannot author a curved face.
        return {"radius": 0.0,
                "plane_area": max(length * width, length * height, width * height)}
    if t in ("cylinder", "sphere"):
        r = _radius("radius")
        if r is None:
            d = _dim_num(p.get("diameter"))
            r = d / 2.0 if d is not None else None
    elif t == "cone":
        r = _radius("radius", "radius1", "radius2", "radius_bottom", "radius_top")
    elif t == "torus":
        major, minor = _radius("radius", "major_radius"), _radius("radius2", "minor_radius")
        r = (major + minor) if (major is not None and minor is not None) else None
    elif t in ("hole", "bolt_circle"):
        # The widest authored diameter wins: a counterbore/countersink is wider than the drill.
        ds = [_dim_num(v) for k, v in p.items() if "diameter" in str(k).lower()]
        ds = [d for d in ds if d is not None]
        rs = [_dim_num(v) for k, v in p.items() if str(k).lower().endswith("radius")]
        rs = [x for x in rs if x is not None] + [d / 2.0 for d in ds]
        r = max(rs) if rs else None
    else:
        return None
    if r is None or r <= 0.0:
        return None
    return {"radius": r, "plane_area": math.pi * r * r}


def _tool_scale_accepts(scale: Optional[dict], f: dict) -> bool:
    """Could this feature's tool have created this face? Necessary condition only."""
    if not scale:
        return True
    if (f.get("surf") or "plane") != "plane":
        r, rt = f.get("radius"), scale.get("radius")
        if r is None or rt is None:
            return True  # no size cue on either side — keep, never guess a face away
        return r <= rt * _TOOL_SCALE_EPS + 1e-6
    a, pa = f.get("area_or_len"), scale.get("plane_area")
    if a is None or pa is None:
        return True
    return a <= pa * _TOOL_SCALE_EPS + 1e-6


def _perp_basis(axis) -> tuple:
    """Two unit vectors spanning the plane perpendicular to `axis`."""
    a = _vnorm(axis)
    seed = (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _vnorm(_vcross(a, seed))
    return u, _vnorm(_vcross(a, u))


def _face_world_points(doc_id: str, match: dict) -> list:
    """A face's real triangulation nodes in world space, or [] when the engine can't give them.

    Deliberately NOT _match_point_cloud: that synthesises a seed BOX from area_or_len when the
    tessellation is missing, and a made-up box must never decide whether a face is inside the
    cut footprint."""
    try:
        fp = cb.call("doc_face_points", {"doc_id": doc_id, "selector": match}) or {}
    except Exception:  # noqa: BLE001
        return []
    return [(float(p[0]), float(p[1]), float(p[2]))
            for p in (fp.get("points") or [])
            if isinstance(p, (list, tuple)) and len(p) >= 3]


_FACE_CLOUD_CACHE: dict = {}      # doc_id -> (revision token, {sid: [(x,y,z), ...]})


def _face_cloud_memo(doc_id: str) -> dict:
    """The {sid: world points} memo for `doc_id`, emptied whenever the model changes.

    doc_face_points costs ~3.5 ms a face (it re-samples the triangulation), and a whole-document
    attribution asks for several. The engine's tessellation handle carries the revision — the
    same invalidation trick _doc_mesh uses — so an edit empties this by itself and a describe/
    inspect/render burst over one unchanged model pays for each face exactly once."""
    try:
        rb = cb.call("doc_rebuild", {"doc_id": doc_id}) or {}
        token = ((rb.get("tess") or {}).get("aicm_path")) or None
    except Exception:  # noqa: BLE001
        token = None
    if token is None:
        return {}                 # no revision to key on — never serve a stale cloud
    cached = _FACE_CLOUD_CACHE.get(doc_id)
    if cached and cached[0] == token:
        return cached[1]
    memo: dict = {}
    _FACE_CLOUD_CACHE.clear()     # one document at a time, like _MESH_CACHE
    _FACE_CLOUD_CACHE[doc_id] = (token, memo)
    return memo


def _axis_reading(matches: list, axis) -> tuple:
    """(lateral indices, cap indices) reading `axis` as the tool's extrusion/revolution axis.

    A curved face is never a cap: a cylinder/cone/spline wall cannot be the flat neighbour a
    prism punched through. Cheap — match records only, so a caller can rule an axis out before
    paying for tessellation."""
    a = _vnorm(axis)
    lateral: list = []
    caps: list = []
    for i, m in enumerate(matches):
        if str(m.get("surf") or "").strip().lower() != "plane":
            lateral.append(i)
            continue
        n = _vnorm(m.get("normal") or m.get("axis") or (0.0, 0.0, 1.0))
        (caps if abs(_vdot(n, a)) > 0.7 else lateral).append(i)
    return lateral, caps


def _cut_axis_survivors(matches: list, clouds: list, axis) -> Optional[tuple]:
    """({kept indices}, dropped_area) reading `axis` as the tool's extrusion/revolution axis,
    or None when that reading explains nothing (no lateral wall, no cap, or nothing to drop)."""
    lateral, caps = _axis_reading(matches, axis)
    if not lateral or not caps:
        return None

    a = _vnorm(axis)
    u, v = _perp_basis(a)
    us = [_vdot(p, u) for i in lateral for p in clouds[i]]
    vs = [_vdot(p, v) for i in lateral for p in clouds[i]]
    if len(us) < 3:
        return None
    ulo, uhi, vlo, vhi = min(us), max(us), min(vs), max(vs)
    # 2% of the footprint absorbs tessellation chord error on a curved wall.
    tol = max(1e-6, 0.02 * max(uhi - ulo, vhi - vlo, 1.0))

    keep = set(lateral)
    dropped = 0.0
    for i in caps:
        inside = all(ulo - tol <= _vdot(p, u) <= uhi + tol
                     and vlo - tol <= _vdot(p, v) <= vhi + tol for p in clouds[i])
        if inside:
            keep.add(i)              # the tool's own floor / counterbore shoulder
        else:
            dropped += abs(float(matches[i].get("area_or_len") or 0.0))
    if dropped <= 0.0:
        return None
    return keep, dropped


def _dedupe_axes(axes: list) -> list:
    """Unique directions, sign-insensitive (an axis and its reverse describe the same prism)."""
    out: list = []
    for a in axes:
        if not any(abs(_vdot(a, b)) > 0.999 for b in out):
            out.append(a)
    return out


def _created_face_matches(doc_id: str, feat, matches: list,
                          feats: Optional[list] = None,
                          cloud_memo: Optional[dict] = None) -> list:
    """The subset of `matches` that feature `feat` demonstrably CREATED — the ONE de-smear
    implementation in this codebase. _face_attribution drives it over a whole document, and
    describe_part, the inspect/render targeting join and the callout roster all go through
    that, so there is a single place where a face is judged.

    Both tests from the section note above, cheapest first:
      * TOOL SCALE — authored params, no RPC. Fires for a FUSE exactly as for a CUT.
      * TOOL FOOTPRINT — real tessellation. The tool axis is recovered from the feature's own
        faces (a curved wall states it outright; an all-planar pocket yields candidate axes as
        the cross products of its wall normals) and the reading that explains the MOST
        regenerated area wins, so a blind hole keeps its floor and a through-bore keeps only
        its wall. Skipped for the FIRST solid feature in `feats`: it authored the body, so
        there is no earlier geometry it could have regenerated — and every cap it would test
        costs a doc_face_points round trip.

    Returns a subset of the SAME dicts in the SAME order, possibly EMPTY: a feature whose every
    claimed face belongs to something earlier owns nothing, and saying so is the honest answer.
    Ambiguity is still never resolved by guessing — no analytic tool scale and no usable
    tessellation leaves the matches untouched.
    """
    if not matches:
        return []
    ft = feat if isinstance(feat, dict) else {}
    scale = _feature_tool_scale(ft)
    kept = [m for m in matches if _tool_scale_accepts(scale, m)] if scale else list(matches)
    if len(kept) < 2 or any((m.get("kind") or "face") != "face" for m in kept):
        return kept
    order = _solid_feature_order(feats)
    if order and order[0] == str(ft.get("id")):
        return kept

    cands = []
    for m in kept:
        if str(m.get("surf") or "").strip().lower() == "plane":
            continue
        ax = _v3(m.get("axis"))
        if _vdot(ax, ax) > 1e-12:
            cands.append(_vnorm(ax))
    if not cands:
        # All-planar tool (box cut / rectangular pocket or slot): the axis is perpendicular to
        # two non-parallel wall normals.
        normals = [_vnorm(m.get("normal") or m.get("axis") or (0.0, 0.0, 1.0)) for m in kept]
        for i in range(len(normals)):
            for j in range(i + 1, len(normals)):
                c = _vcross(normals[i], normals[j])
                if _vdot(c, c) > 0.04:          # >~11 deg apart
                    cands.append(_vnorm(c))
    axes = []
    for ax in _dedupe_axes(cands):
        lat, caps = _axis_reading(kept, ax)
        if lat and caps:
            axes.append(ax)
    if not axes:
        return kept        # no reading has both a wall and a cap — nothing tessellation adds

    memo = _face_cloud_memo(doc_id) if cloud_memo is None else cloud_memo
    clouds = []
    for m in kept:
        sid = m.get("sid")
        if sid is None:
            clouds.append(_face_world_points(doc_id, m))
            continue
        if sid not in memo:
            memo[sid] = _face_world_points(doc_id, m)
        clouds.append(memo[sid])
    if any(len(c) < 3 for c in clouds):
        return kept        # no real geometry to judge with — leave the join alone
    best = None
    for ax in axes:
        got = _cut_axis_survivors(kept, clouds, ax)
        if got is not None and (best is None or got[1] > best[1]):
            best = got
    if best is None:
        return kept
    return [m for i, m in enumerate(kept) if i in best[0]]


def _face_attribution(doc_id: str, feats: Optional[list] = None,
                      owner_map: Optional[dict] = None,
                      faces: Optional[list] = None) -> dict:
    """Who owns each face, de-smeared — the one feature->face join.

    owner_map keys each feature's topology as [body_id, topo_id] (1-based surviving-body
    ordinal, 1-based TopExp index within that body). The engine stamps the same (body, topo)
    pair on every doc_query match, and the join is taken on it across EVERY surviving body.
    Legacy fallback (matches without those keys): doc_query enumerates the last body in
    first-encounter order, so topo_id == match_index + 1 on the highest body_id owner_map
    mentions, and a feature whose only pairs sit on another body reads as consumed.

    Each feature's claimed faces then go through _created_face_matches. A face it did not
    create is re-credited to the latest EARLIER feature whose tool could have authored it, and
    left unowned when none could.

    Returns {
      "faces":   [match, ...] in doc_query order,
      "owner":   [feature_id | None, ...] parallel to "faces" — the de-smeared partition,
      "owned":   {feature_id: [match, ...]} — that partition grouped, INCLUDING faces
                 re-credited from a later feature. The "who owns this face" view describe_part
                 narrates.
      "created": {feature_id: [match, ...]} — only the faces the feature itself claimed AND
                 kept. The "what did this feature add" view framing/tinting/callouts need: a
                 plate must not grow a top face just because the bore that pierced it handed
                 the face back. Empty (or absent) is a real answer.
      "claimed": {feature_id: int} — the raw owner_map face count on the live body, so a caller
                 can report honestly how many faces it moved,
      "reattributed": int — faces moved to a different feature,
      "last_body": int,
      "all_bodies": bool — True when the join used the engine's (body, topo) keys}
    """
    if feats is None or owner_map is None:
        feats, owner_map = _target_feature_roster(doc_id)
    owner_map = owner_map or {}
    if faces is None:
        try:
            faces = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
                     or {}).get("matches") or []
        except Exception:  # noqa: BLE001
            faces = []

    last_body = 0
    for om in owner_map.values():
        for pair in (om.get("faces") or []):
            if isinstance(pair, list) and len(pair) == 2:
                last_body = max(last_body, int(pair[0]))
    if last_body <= 0:            # a face-less tree (sketches/datums only) still has edges
        for om in owner_map.values():
            for pair in (om.get("edges") or []):
                if isinstance(pair, list) and len(pair) == 2:
                    last_body = max(last_body, int(pair[0]))

    by_id = {str(f.get("id")): f for f in (feats or [])
             if isinstance(f, dict) and f.get("id")}
    order = _solid_feature_order(feats)
    rank = {fid: i for i, fid in enumerate(order)}
    scales = {fid: _feature_tool_scale(by_id.get(fid)) for fid in order}

    claimed_idx: dict = {}
    owner: list = [None] * len(faces)
    # An engine that stamps each match with its (body, topo) key joins EXACTLY, over every
    # surviving body: doc_query counts faces across the whole document while owner_map counts
    # per body, so the positional topo-1 join below is only right on a single-body part — a
    # second solid (a floating rib) used to make every feature on body 1 read as consumed.
    by_key = {(int(m["body"]), int(m["topo"])): i for i, m in enumerate(faces)
              if isinstance(m, dict) and m.get("body") is not None
              and m.get("topo") is not None}
    for fid, om in owner_map.items():
        for pair in (om.get("faces") or []):
            if not (isinstance(pair, list) and len(pair) == 2):
                continue
            if by_key:
                i = by_key.get((int(pair[0]), int(pair[1])), -1)
            elif int(pair[0]) == last_body:
                i = int(pair[1]) - 1
            else:
                continue
            if 0 <= i < len(faces):
                claimed_idx.setdefault(str(fid), []).append(i)
                owner[i] = str(fid)

    created: dict = {}
    reattributed = 0
    cloud_memo = _face_cloud_memo(doc_id)
    for fid, idxs in claimed_idx.items():
        idxs.sort()
        mine = [faces[i] for i in idxs]
        if fid not in rank:
            # Not a solid feature in the tree: no authored tool to reason about, no tree
            # position to walk back from. Leave the kernel's answer alone.
            created[fid] = mine
            continue
        kept = _created_face_matches(doc_id, by_id.get(fid), mine, feats, cloud_memo)
        keep_ids = {id(m) for m in kept}
        created[fid] = [m for m in mine if id(m) in keep_ids]
        for i in idxs:
            if id(faces[i]) in keep_ids:
                continue
            owner[i] = None
            for j in range(rank[fid] - 1, -1, -1):
                if _tool_scale_accepts(scales.get(order[j]), faces[i]):
                    owner[i] = order[j]
                    reattributed += 1
                    break

    owned: dict = {}
    for i, f in enumerate(faces):
        if owner[i]:
            owned.setdefault(owner[i], []).append(f)
    return {"faces": faces, "owner": owner, "owned": owned, "created": created,
            "claimed": {fid: len(idxs) for fid, idxs in claimed_idx.items()},
            "reattributed": reattributed, "last_body": last_body,
            "all_bodies": bool(by_key)}


def _owned_matches_for(doc_id: str, fid: str, label: str, owner_map: dict,
                       feats: Optional[list] = None) -> list:
    """doc_query matches for the geometry a feature CREATED — _face_attribution's `created`
    view — falling back to owned edges only for a feature that ended up with no surviving face.

    `created`, not `owned`: this feeds framing, tinting and measurement of ONE feature, so the
    plate must not grow a top face just because the bore that pierced it handed the face back.

    Edges are deliberately excluded while faces exist. A fuse RE-CREATES the neighbour edges it
    splits and the kernel attributes them to the new feature, so a 36 mm boss "owns" the plate's
    102 mm top edges; framing on those blew the target bbox out past the whole part (151x131x102
    on a 138x98x33 model) and the close-up degenerated into a whole-part render.

    `feats` is the doc_tree roster — needed to read each feature's authored tool and its tree
    position; without it a feature is judged with no earlier candidate to hand a face back to.
    """
    att = _face_attribution(doc_id, feats, owner_map)
    last_body = att["last_body"]
    if last_body <= 0:
        raise ValueError(
            f"{label}: the document tree records no owned topology at all (owner_map is empty). "
            f"Call describe_part(doc_id) for the face/edge sid roster and inspect by sid.")

    # With exact (body, topo) keys every surviving body is live; without them only the
    # highest body id is (the legacy single-body positional join).
    live = (lambda b: True) if att.get("all_bodies") else (lambda b: b == last_body)
    om = (owner_map or {}).get(fid) or {}
    face_keys = {(int(p[0]), int(p[1])) for p in (om.get("faces") or [])
                 if isinstance(p, list) and len(p) == 2 and live(int(p[0]))}
    edge_keys = {(int(p[0]), int(p[1])) for p in (om.get("edges") or [])
                 if isinstance(p, list) and len(p) == 2 and live(int(p[0]))}
    if not face_keys and not edge_keys:
        raise ValueError(
            f"{label} owns no faces/edges on the current body — a later boolean/fillet consumed "
            f"it, so there is nothing to frame. describe_part(doc_id) marks consumed features "
            f"and lists the sids that DO still exist.")

    out = list(att["created"].get(fid) or [])
    if not out and edge_keys:
        edges = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "edge"}})
                 .get("matches") or [])
        if att.get("all_bodies"):
            out = [m for m in edges if m.get("body") is not None and m.get("topo") is not None
                   and (int(m["body"]), int(m["topo"])) in edge_keys]
        else:
            out = [m for i, m in enumerate(edges) if (last_body, i + 1) in edge_keys]
    if not out:
        feat = next((f for f in (feats or [])
                     if isinstance(f, dict) and str(f.get("id")) == fid), None)
        verb = "cut" if _feature_is_subtractive(feat, feats) else "fuse"
        raise ValueError(
            f"{label} created no face of its own: every one of the {len(face_keys)} face(s) "
            f"the kernel credited to it already existed — a {verb} regenerates the faces its "
            f"tool touches and OCCT re-attributes them to the {verb} — so there is nothing of "
            f"its own to frame. describe_part(doc_id) says which feature owns those faces; "
            f"inspect that feature by name, or one of its sids.")
    return out


def _feature_owned_matches(doc_id: str, feature_id: str) -> list:
    """Faces+edges owned by a feature named by id ("f2") or NAME ("Boss", case-insensitive)."""
    feats, owner_map = _target_feature_roster(doc_id)
    fid, fname, _ = _resolve_feature_ref(doc_id, feature_id, feats, "feature")
    return _owned_matches_for(doc_id, fid, _feature_label(fid, fname), owner_map, feats)


def _feature_label(fid: str, fname) -> str:
    """`feature f2 "Boss"` — captions must say WHICH feature, not just an opaque id."""
    return f'feature {fid} "{fname}"' if fname else f"feature {fid}"


def _feature_target_result(doc_id: str, ref, what: str = "inspect target",
                           feats: Optional[list] = None,
                           owner_map: Optional[dict] = None) -> dict:
    """Resolve a feature id/name into the standard {matches, query, label, kind} result."""
    if feats is None or owner_map is None:
        feats, owner_map = _target_feature_roster(doc_id)
    fid, fname, _ = _resolve_feature_ref(doc_id, ref, feats, what)
    label = _feature_label(fid, fname)
    return {
        "matches": _owned_matches_for(doc_id, fid, label, owner_map, feats),
        "query": {"feature_id": fid},
        "label": label,
        "kind": "feature",
        "feature_id": fid,
        "feature_name": fname,
    }


def _query_target_result(doc_id: str, q: dict, target) -> dict:
    """Run a resolved geometry query and pack it as a target result."""
    res = cb.call("doc_query", {"doc_id": doc_id, "query": q}) or {}
    matches = list(res.get("matches") or [])
    if not matches:
        feats, _om = _target_feature_roster(doc_id)
        raise ValueError(
            f"No geometry matched inspect target {target!r} (resolved query {q!r}). "
            f"query_geometry(...) explores faces/edges, describe_part(doc_id) lists every "
            f"face/edge sid, or name a feature: {_roster_text(feats)}.")
    kind = matches[0].get("kind") or q.get("of") or "face"
    if isinstance(target, str):
        label = target.strip()
    else:
        label = f'{kind} ({q.get("side") or q.get("surf") or kind})'
    return {"matches": matches, "query": q, "label": label, "kind": kind}


def _resolve_inspect_target(doc_id: str, target: Union[str, dict]) -> dict:
    """Resolve an inspect / highlight target → {matches, query, label, kind[, feature_id,
    feature_name]}.

    Accepted, in precedence order:
      1. geometry-query dict  {"of":"face","side":"top"} / {"of":"edge","parallel":"z"}
      2. sid string           "F58db6d" / "E1c4a90" — describe_part's stable face/edge handles
      3. feature id           "f2"
      4. feature NAME         "Boss" (case-insensitive, surrounding whitespace ignored)
      5. side/surf word or short phrase — the query grammar ("top", "the filleted edge")

    A feature target keeps ALL its matches so a crop frames the whole feature; a geometry query
    accepts 1+ matches and unions their bboxes. Nothing fails silently and nothing falls through
    to the engine's side parser: every miss names the feature roster and points at describe_part
    for the sid roster.
    """
    parsed = _parse_inspect_target(target)

    if "selector" in parsed:
        sel = parsed["selector"]
        return {"matches": [sel], "query": sel,
                "label": sel.get("sid") or "selector", "kind": sel.get("kind") or "face"}

    if "sid" in parsed:
        sid = str(parsed["sid"])
        m = _sid_match(doc_id, sid)
        if m is not None:
            return {"matches": [m], "query": {"sid": m.get("sid")},
                    "label": str(m.get("sid") or sid), "kind": m.get("kind") or "face"}
        # "f123456" is both a legal sid shape and a legal feature-id shape — try the other
        # reading before giving up.
        if re.fullmatch(r"[fF]\d+", sid):
            return _feature_target_result(doc_id, sid)
        feats, _om = _target_feature_roster(doc_id)
        extra = (" Vertex sids aren't inspectable — inspect the face or edge that carries it."
                 if sid[:1].lower() == "v" else "")
        raise ValueError(
            f"No face/edge with sid {sid!r} in this document (sids change whenever the model "
            f"changes).{extra} Call describe_part(doc_id) for the current sid roster, or name a "
            f"feature: {_roster_text(feats)}.")

    if "feature_id" in parsed:
        return _feature_target_result(doc_id, parsed["feature_id"])

    if "feature_name" in parsed:
        try:
            return _feature_target_result(doc_id, parsed["feature_name"])
        except ValueError as exc:
            fb = parsed.get("fallback_query")
            if not fb:
                raise
            # Mixed phrase ("outer cylindrical wall face"): the name lost, so read it as the
            # geometry query it also parses as.
            try:
                return _query_target_result(doc_id, fb, target)
            except ValueError:
                raise ValueError(
                    f"{exc} (Also read as the geometry query {fb!r} — no match.)") from None

    # A string that DID parse as geometry can still be a feature's NAME, and the name wins
    # (precedence 4 over 5): a model that wrote it got it from the feature tree.
    if isinstance(target, str):
        feats, owner_map = _target_feature_roster(doc_id)
        if _features_named(feats, target):
            return _feature_target_result(doc_id, target, feats=feats, owner_map=owner_map)

    return _query_target_result(doc_id, parsed, target)


def _match_point_cloud(doc_id: str, match: dict) -> list:
    """World-space sample points for a match (face triangulation, else centroid ± radius)."""
    pts: list = []
    if match.get("kind") == "face" or (match.get("surf") and match.get("kind") != "edge"):
        try:
            fp = cb.call("doc_face_points", {"doc_id": doc_id, "selector": match})
            for p in fp.get("points") or []:
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    pts.append((float(p[0]), float(p[1]), float(p[2])))
        except Exception:  # noqa: BLE001
            pass
    # If face points already frame the subshape, stop — do NOT inflate with a seed box
    # derived from area_or_len (a large planar face would explode the crop).
    if len(pts) >= 3:
        return pts

    if match.get("centroid") is None:
        return pts
    c = _v3(match.get("centroid"))
    pts.append(c)
    r = match.get("radius")
    try:
        rr = float(r) if r is not None else 0.0
    except (TypeError, ValueError):
        rr = 0.0
    al = match.get("area_or_len")
    try:
        extent = float(al) if al is not None else 0.0
    except (TypeError, ValueError):
        extent = 0.0
    # Seed a small box so edges without face points still project to a crop.
    # Prefer radius (fillet/circle); fall back to a modest fraction of length/area.
    if rr > 0:
        half = max(rr, 1.0)
    elif match.get("kind") == "edge" and extent > 0:
        half = max(extent * 0.5, 1.0)  # half-length along an edge
    elif extent > 0:
        half = max(math.sqrt(extent) * 0.5, 1.0)
    else:
        half = 1.0
    axis = _vnorm(match.get("axis") or match.get("normal") or (0, 0, 1))
    seed = (1.0, 0.0, 0.0) if abs(axis[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _vnorm(_vcross(axis, seed))
    v = _vnorm(_vcross(axis, u))
    for su in (-half, half):
        for sv in (-half, half):
            for sa in (-half * 0.25, half * 0.25):
                pts.append((c[0] + su * u[0] + sv * v[0] + sa * axis[0],
                            c[1] + su * u[1] + sv * v[1] + sa * axis[1],
                            c[2] + su * u[2] + sv * v[2] + sa * axis[2]))
    return pts



def _bbox_from_points(pts: list) -> dict:
    if not pts:
        raise ValueError("empty point set — cannot build target bbox")
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]; zs = [p[2] for p in pts]
    mn = (min(xs), min(ys), min(zs))
    mx = (max(xs), max(ys), max(zs))
    size = (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])
    center = ((mn[0] + mx[0]) * 0.5, (mn[1] + mx[1]) * 0.5, (mn[2] + mx[2]) * 0.5)
    # Degenerate (point/edge) — pad so projection has non-zero extent.
    pad = max(max(size) * 0.05, 0.5)
    size = (max(size[0], pad), max(size[1], pad), max(size[2], pad))
    mn = (center[0] - size[0] * 0.5, center[1] - size[1] * 0.5, center[2] - size[2] * 0.5)
    mx = (center[0] + size[0] * 0.5, center[1] + size[1] * 0.5, center[2] + size[2] * 0.5)
    return {"min": mn, "max": mx, "size": size, "center": center}


def _target_bbox(doc_id: str, matches: list) -> dict:
    pts: list = []
    for m in matches:
        pts.extend(_match_point_cloud(doc_id, m))
    return _bbox_from_points(pts)


def _camera_dir_for_target(matches: list, view: Optional[Union[str, dict]] = None) -> tuple:
    """Pick a scene->camera direction. auto = mean face normal (faces) or iso-toward-centroid."""
    if view is None or view == "auto":
        normals = []
        for m in matches:
            n = m.get("normal")
            if isinstance(n, (list, tuple)) and len(n) == 3:
                normals.append(_vnorm(n))
        if normals:
            acc = [0.0, 0.0, 0.0]
            for n in normals:
                # Flip so the sum doesn't cancel antiparallel pairs.
                if acc[0] or acc[1] or acc[2]:
                    if _vdot(tuple(acc), n) < 0:
                        n = (-n[0], -n[1], -n[2])
                acc[0] += n[0]; acc[1] += n[1]; acc[2] += n[2]
            return _vnorm(tuple(acc))
        # Edges / no normals: look from a mild iso that faces +Z-up content.
        return _vnorm((1.0, 1.0, 0.8))

    if isinstance(view, str):
        named = {
            "front": (0.0, -1.0, 0.0), "back": (0.0, 1.0, 0.0),
            "right": (1.0, 0.0, 0.0), "left": (-1.0, 0.0, 0.0),
            "top": (0.0, 0.0, 1.0), "bottom": (0.0, 0.0, -1.0),
            "iso": _vnorm((1.0, 1.0, 0.8)),
        }
        if view.lower() in named:
            return named[view.lower()]
        raise ValueError(f"Unknown named view {view!r}")

    if isinstance(view, dict):
        if "dir" in view:
            return _vnorm(view["dir"])
        if "azimuth" in view or "elevation" in view:
            # Match doc_render_view convention: az 0 = front (−Y), 90 = right (+X); el 90 = top.
            az = math.radians(float(view.get("azimuth", 0.0)))
            el = math.radians(float(view.get("elevation", 20.0)))
            # scene->camera
            x = math.sin(az) * math.cos(el)
            y = -math.cos(az) * math.cos(el)
            z = math.sin(el)
            return _vnorm((x, y, z))
    raise ValueError(f"Unsupported view spec {view!r}")


def _part_render_frame(part_bbox: dict, view_dir: tuple,
                       W: int = _RENDER_W, H: int = _RENDER_H) -> dict:
    """Reproduce the orthographic projection frame of core render_view_to_rgb.

    Returns {center, r, u, d, scale, cu, cv, W, H} so world points can be mapped to
    screen pixels the same way the C++ software rasterizer does (fit scale 0.85).
    """
    c = _v3(part_bbox.get("center"))
    d = _vnorm(view_dir)
    up_ref = (0.0, 1.0, 0.0) if abs(d[2]) > 0.985 else (0.0, 0.0, 1.0)
    r = _vnorm(_vcross(up_ref, d))
    u = _vcross(d, r)  # already unit if r,d unit + orthogonal
    u = _vnorm(u)

    # Project the 8 corners of the part bbox to find screen extents (same as C++ mesh walk
    # for axis-aligned bounds — exact for the bbox, slightly conservative for the mesh).
    mn = _v3(part_bbox.get("min"))
    mx = _v3(part_bbox.get("max"))
    corners = [(x, y, z)
               for x in (mn[0], mx[0])
               for y in (mn[1], mx[1])
               for z in (mn[2], mx[2])]
    us, vs = [], []
    for p in corners:
        dp = _vsub(p, c)
        us.append(_vdot(dp, r))
        vs.append(_vdot(dp, u))
    minu, maxu = min(us), max(us)
    minv, maxv = min(vs), max(vs)
    scale = _RENDER_FIT * min(W / max(maxu - minu, 1e-6), H / max(maxv - minv, 1e-6))
    cu = 0.5 * (minu + maxu)
    cv = 0.5 * (minv + maxv)
    return {"center": c, "r": r, "u": u, "d": d, "scale": scale,
            "cu": cu, "cv": cv, "W": W, "H": H}


def _world_to_screen(pt, frame: dict) -> tuple:
    dp = _vsub(_v3(pt), frame["center"])
    pu = _vdot(dp, frame["r"])
    pv = _vdot(dp, frame["u"])
    sx = (pu - frame["cu"]) * frame["scale"] + frame["W"] / 2.0
    sy = frame["H"] / 2.0 - (pv - frame["cv"]) * frame["scale"]
    return (sx, sy)


def _crop_rect_for_target(part_bbox: dict, target_bbox: dict, view_dir: tuple,
                          margin: float = 0.15, zoom_factor: float = 1.0,
                          W: int = _RENDER_W, H: int = _RENDER_H) -> dict:
    """Pixel crop rectangle that frames the target's projected bbox.

    margin: fractional padding of the projected target size (0.15 = 15% on each side).
    zoom_factor: >1 tightens the crop (more zoom), <1 loosens. Clamped to keep a usable window.
    Returns {x0,y0,x1,y1, method:"crop"} in pixel coords (inclusive-exclusive style for PIL).
    """
    frame = _part_render_frame(part_bbox, view_dir, W, H)
    tmin = _v3(target_bbox.get("min"))
    tmax = _v3(target_bbox.get("max"))
    corners = [(x, y, z)
               for x in (tmin[0], tmax[0])
               for y in (tmin[1], tmax[1])
               for z in (tmin[2], tmax[2])]
    sxs, sys_ = [], []
    for p in corners:
        sx, sy = _world_to_screen(p, frame)
        sxs.append(sx); sys_.append(sy)
    minx, maxx = min(sxs), max(sxs)
    miny, maxy = min(sys_), max(sys_)
    # Ensure a minimum projected size so thin edges still crop to something visible.
    if maxx - minx < 4:
        mid = 0.5 * (minx + maxx); minx, maxx = mid - 2, mid + 2
    if maxy - miny < 4:
        mid = 0.5 * (miny + maxy); miny, maxy = mid - 2, mid + 2

    zf = max(float(zoom_factor), 0.25)
    # zoom_factor>1 → shrink the window around the target center.
    cx, cy = 0.5 * (minx + maxx), 0.5 * (miny + maxy)
    half_w = 0.5 * (maxx - minx) / zf
    half_h = 0.5 * (maxy - miny) / zf
    minx, maxx = cx - half_w, cx + half_w
    miny, maxy = cy - half_h, cy + half_h

    pad_x = max((maxx - minx) * float(margin), 4.0)
    pad_y = max((maxy - miny) * float(margin), 4.0)
    x0 = int(math.floor(minx - pad_x))
    y0 = int(math.floor(miny - pad_y))
    x1 = int(math.ceil(maxx + pad_x))
    y1 = int(math.ceil(maxy + pad_y))
    # Clamp to image; if the target projected off-frame, fall back to a center window.
    x0 = max(0, min(x0, W - 1))
    y0 = max(0, min(y0, H - 1))
    x1 = max(x0 + 1, min(x1, W))
    y1 = max(y0 + 1, min(y1, H))
    # Keep a usable minimum crop so upscale has something to work with.
    min_side = 32
    if (x1 - x0) < min_side:
        mid = (x0 + x1) // 2
        x0 = max(0, mid - min_side // 2)
        x1 = min(W, x0 + min_side)
    if (y1 - y0) < min_side:
        mid = (y0 + y1) // 2
        y0 = max(0, mid - min_side // 2)
        y1 = min(H, y0 + min_side)
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "method": "crop", "W": W, "H": H,
            "view_dir": list(view_dir)}


def _crop_png_to_rect(src_png: str, rect: dict, out_png: Optional[str] = None,
                      out_size: tuple = (_RENDER_W, _RENDER_H)) -> str:
    """Crop src_png to rect and upscale to out_size. Returns the output path."""
    from PIL import Image as PILImage  # noqa: PLC0415
    out_png = out_png or _tmp(".png")
    with PILImage.open(src_png) as im:
        im = im.convert("RGB")
        crop = im.crop((rect["x0"], rect["y0"], rect["x1"], rect["y1"]))
        # NEAREST keeps the software-rasterizer's hard edges readable; BICUBIC softens labels.
        try:
            resample = PILImage.Resampling.BICUBIC
        except AttributeError:  # pragma: no cover — older Pillow
            resample = PILImage.BICUBIC
        crop = crop.resize(out_size, resample)
        crop.save(out_png, format="PNG")
    return out_png


def _measure_match(doc_id: str, match: dict) -> dict:
    """Best-effort intrinsic measure of one match (area/length/radius)."""
    out = {
        "sid": match.get("sid"),
        "kind": match.get("kind"),
        "surf": match.get("surf"),
        "side": match.get("side"),
        "centroid": match.get("centroid"),
        "radius": match.get("radius"),
        "area_or_len": match.get("area_or_len"),
        "normal": match.get("normal"),
        "axis": match.get("axis"),
    }
    try:
        m = cb.call("eval_measure", {"doc_id": doc_id, "refs": [match]})
        out["measure"] = m
    except Exception as e:  # noqa: BLE001
        out["measure_error"] = str(e)
    return out


def _inspect_caption(doc_id: str, resolved: dict, target_bbox: dict, view_dir: tuple,
                     method: str, n_matches: int) -> str:
    """Text caption: what's framed + key dims so non-vision models still get a read."""
    size = target_bbox.get("size") or (0, 0, 0)
    center = target_bbox.get("center") or (0, 0, 0)
    dims = (f"{size[0]:.3g}×{size[1]:.3g}×{size[2]:.3g} mm"
            if isinstance(size, (list, tuple)) and len(size) >= 3 else "?")
    cstr = (f"[{center[0]:.3g}, {center[1]:.3g}, {center[2]:.3g}]"
            if isinstance(center, (list, tuple)) and len(center) >= 3 else "?")
    dir_s = f"[{view_dir[0]:.2f}, {view_dir[1]:.2f}, {view_dir[2]:.2f}]"

    # Key dims from the first few matches.
    dim_bits = []
    for m in (resolved.get("matches") or [])[:4]:
        info = _measure_match(doc_id, m)
        bit = info.get("sid") or info.get("kind") or "?"
        meas = info.get("measure") or {}
        if meas.get("kind") == "area" and meas.get("area") is not None:
            bit += f" area={meas['area']:.4g} mm²"
        elif meas.get("kind") == "length" and meas.get("length") is not None:
            bit += f" length={meas['length']:.4g} mm"
        elif meas.get("kind") == "radius" and meas.get("radius") is not None:
            bit += f" radius={meas['radius']:.4g} mm"
        elif info.get("radius") is not None:
            bit += f" radius={info['radius']:.4g} mm"
        elif info.get("area_or_len") is not None:
            bit += f" size={info['area_or_len']:.4g}"
        if info.get("surf"):
            bit += f" ({info['surf']}"
            if info.get("side"):
                bit += f", {info['side']}"
            bit += ")"
        dim_bits.append(bit)

    method_note = (
        "Close-up via whole-part render + projected-bbox CROP/upscale "
        "(doc_render_view has no bbox/eye camera override)."
        if method == "crop" else
        f"Render method: {method}."
    )
    parts = [
        f"Close-up of {resolved.get('label')!r} "
        f"({n_matches} subshape{'s' if n_matches != 1 else ''}).",
        f"Target bbox {dims} centered at {cstr}.",
        f"Camera dir (scene→camera) {dir_s}.",
        method_note,
    ]
    if dim_bits:
        parts.append("Key dims: " + "; ".join(dim_bits) + ".")
    return " ".join(parts)


def _orbit_view_dirs(base_dir: tuple, n: int = 4) -> list:
    """N scene->camera dirs orbiting around world +Z (or base's perpendicular) at 90° steps.

    First view is the base_dir itself; subsequent views rotate base around a stable up axis.
    """
    n = max(int(n), 1)
    base = _vnorm(base_dir)
    # Orbit axis: world +Z unless base is nearly parallel to it.
    axis = (0.0, 0.0, 1.0)
    if abs(_vdot(base, axis)) > 0.9:
        axis = (0.0, 1.0, 0.0)
    # Rodrigues rotation of base around axis.
    def _rot(v, ax, ang):
        ca, sa = math.cos(ang), math.sin(ang)
        # v cos + (ax×v) sin + ax (ax·v) (1-cos)
        cross = _vcross(ax, v)
        dot = _vdot(ax, v)
        return _vnorm((
            v[0] * ca + cross[0] * sa + ax[0] * dot * (1 - ca),
            v[1] * ca + cross[1] * sa + ax[1] * dot * (1 - ca),
            v[2] * ca + cross[2] * sa + ax[2] * dot * (1 - ca),
        ))
    out = []
    for i in range(n):
        ang = (2.0 * math.pi * i) / n
        out.append(_rot(base, axis, ang) if i else base)
    return out


def _tile_pngs(png_paths: list, out_png: str, labels: Optional[list] = None) -> str:
    """Tile PNGs into a contact-sheet-like grid (2 columns)."""
    from PIL import Image as PILImage, ImageDraw, ImageFont  # noqa: PLC0415
    if not png_paths:
        raise ValueError("no tiles to compose")
    imgs = [PILImage.open(p).convert("RGB") for p in png_paths]
    try:
        tw, th = imgs[0].size
        cols = 2 if len(imgs) > 1 else 1
        rows = (len(imgs) + cols - 1) // cols
        sheet = PILImage.new("RGB", (tw * cols, th * rows), (245, 245, 245))
        draw = ImageDraw.Draw(sheet)
        for i, im in enumerate(imgs):
            r, c = divmod(i, cols)
            sheet.paste(im.resize((tw, th)), (c * tw, r * th))
            if labels and i < len(labels):
                draw.text((c * tw + 6, r * th + 4), str(labels[i]), fill=(20, 20, 20))
        sheet.save(out_png, format="PNG")
        return out_png
    finally:
        for im in imgs:
            try:
                im.close()
            except Exception:  # noqa: BLE001
                pass


def _tile_pngs_grid(png_paths: list, out_png: str, labels: Optional[list] = None,
                    cols: int = 3) -> str:
    """Tile PNGs into a labelled contact-sheet grid with an explicit column count.

    The label gets its OWN band above each tile rather than being stamped into the top-left
    corner: the engine already bakes a header there (view name + framed dims), and printing a
    second string over it left both unreadable on every close-up sheet."""
    from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    if not png_paths:
        raise ValueError("no tiles to compose")
    imgs = [PILImage.open(p).convert("RGB") for p in png_paths]
    try:
        tw, th = imgs[0].size
        cols = max(int(cols), 1)
        rows = (len(imgs) + cols - 1) // cols
        gap = 4
        font = _annot_font(15)
        band = 24 if labels else 0
        cell_h = th + band
        sheet = PILImage.new("RGB",
                             (tw * cols + gap * (cols - 1),
                              cell_h * rows + gap * (rows - 1)),
                             (30, 30, 32))
        draw = ImageDraw.Draw(sheet)
        for i, im in enumerate(imgs):
            r, c = divmod(i, cols)
            x = c * (tw + gap)
            y = r * (cell_h + gap)
            if labels and i < len(labels):
                draw.rectangle([x, y, x + tw - 1, y + band - 1], fill=(18, 19, 22))
                draw.text((x + 7, y + 4), str(labels[i]).upper(), font=font,
                          fill=(235, 240, 248))
            sheet.paste(im.resize((tw, th)), (x, y + band))
        sheet.save(out_png, format="PNG")
        return out_png
    finally:
        for im in imgs:
            try:
                im.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- multi-view + emphasis (vision-graph §B)
# Contact-sheet defaults, feature emphasis overlays, AABB framing helpers, and the
# inspect_feature one-call composition. Emphasis is a deterministic POST-process on the
# software-rasterized PNG (the C++ doc_render_* path has no per-face palette override):
# projected face samples are painted high-contrast, context is desaturated, and the
# palette is returned as structured metadata so tests/agents can assert identity without
# pixel hashing.

_STANDARD_ORTHO_VIEWS = ("front", "back", "left", "right", "top", "bottom", "iso")

# Deterministic high-contrast palette for emphasis / per-face disambiguation.
# Order is fixed so the same sid always maps to the same color across calls.
_EMPHASIS_PALETTE = (
    (255, 64, 64),    # red
    (64, 180, 255),   # cyan-blue
    (255, 200, 40),   # amber
    (80, 220, 120),   # green
    (220, 100, 255),  # magenta
    (255, 140, 60),   # orange
    (100, 160, 255),  # sky
    (255, 100, 160),  # pink
    (180, 255, 80),   # lime
    (120, 220, 220),  # teal
)
_EMPHASIS_GLOW = (255, 255, 80)  # outline/glow around emphasized faces
_CONTEXT_DESAT = 0.32            # 0 = full grey, 1 = original colour


def _stable_color_for_key(key: str, palette: tuple = _EMPHASIS_PALETTE) -> tuple:
    """Deterministic palette index from a stable string key (sid / feature id)."""
    if not key:
        return palette[0]
    h = 0
    for ch in str(key):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return palette[h % len(palette)]


def _match_color_key(match: dict, index: int = 0) -> str:
    return str(match.get("sid") or match.get("owner_feature")
               or match.get("kind") or f"m{index}")


def _build_emphasis_palette(matches: list, mode: str = "emphasis") -> dict:
    """Return {mode, entries:[{key, sid, rgb, role}], legend} for caption + tests.

    mode:
      * "emphasis" — all targets share one high-contrast EMPHASIS colour; context is
        reported as desaturated greys (no per-face entry).
      * "per_face"  — each match gets a distinct deterministic colour for disambiguation.
    """
    mode = (mode or "emphasis").lower().strip()
    if mode not in ("emphasis", "per_face"):
        mode = "emphasis"
    entries = []
    if mode == "emphasis":
        rgb = _EMPHASIS_PALETTE[0]
        for i, m in enumerate(matches or []):
            key = _match_color_key(m, i)
            entries.append({
                "key": key,
                "sid": m.get("sid"),
                "rgb": list(rgb),
                "role": "emphasis",
            })
        if not entries:
            entries.append({"key": "target", "sid": None, "rgb": list(rgb), "role": "emphasis"})
    else:
        # Sequential palette slots guarantee DISTINCT colours across a small set of
        # measured/emphasized elements (hash-by-sid can collide within 10 slots).
        n = len(_EMPHASIS_PALETTE)
        for i, m in enumerate(matches or []):
            key = _match_color_key(m, i)
            rgb = _EMPHASIS_PALETTE[i % n]
            entries.append({
                "key": key,
                "sid": m.get("sid"),
                "rgb": list(rgb),
                "role": "per_face",
            })
    legend = ", ".join(
        f"{e.get('sid') or e['key']}=#{e['rgb'][0]:02X}{e['rgb'][1]:02X}{e['rgb'][2]:02X}"
        for e in entries
    )
    return {
        "mode": mode,
        "entries": entries,
        "legend": legend,
        "context_desat": _CONTEXT_DESAT,
        "glow_rgb": list(_EMPHASIS_GLOW),
    }


def _desaturate_rgb(r: int, g: int, b: int, amount: float = _CONTEXT_DESAT) -> tuple:
    """Blend toward luminance; amount is how much ORIGINAL colour remains (0..1)."""
    amount = max(0.0, min(1.0, float(amount)))
    lum = int(0.299 * r + 0.587 * g + 0.114 * b)
    return (
        int(lum * (1.0 - amount) + r * amount),
        int(lum * (1.0 - amount) + g * amount),
        int(lum * (1.0 - amount) + b * amount),
    )


def _emphasis_shade(rgb: tuple, lum: float) -> tuple:
    """The emphasis colour modulated by a pixel's own luminance.

    A flat fill turns a bore into a sticker; keeping the shading means the emphasized face
    still reads as curved/tilted geometry while unmistakably carrying the target colour."""
    k = 0.45 + 0.85 * max(0.0, min(1.0, float(lum)))
    return tuple(int(max(0, min(255, round(c * k)))) for c in rgb)


# --------------------------------------------------------------------------- mesh-accurate render annotation
# The C++ rasterizer (core/doc/session.cpp render_view_to_rgb) hands back a flat-shaded PNG
# with NO B-rep edges, no feature identity and no scale reference — a fillet, a chamfer, a
# shallow pocket or two coincident faces are literally invisible against flat shading, and a
# model cannot tell a 4 mm part from a 400 mm one. Everything needed to fix that is ALREADY
# published by the engine: every rebuild writes an AICM tessellation (vertex positions,
# triangles, per-B-rep-FACE triangle ranges and per-B-rep-EDGE polylines) to the handle in
# doc_rebuild()["tess"]["aicm_path"]. Reprojecting that mesh through the SAME orthographic
# frame the C++ renderer uses (bbox centre, 0.85 fit over the projected mesh extents) lets us
# overlay in Python, pixel-exact against the shaded image:
#   * depth-tested B-rep edges — real hidden-line removal, not a wireframe smear,
#   * exact per-face highlight masks with a leader line to a named label,
#   * feature callouts (id + name) for the features actually visible in frame,
#   * a scale bar + precise bbox readout.
# Every step degrades to "no overlay, the engine's render exactly as before" when numpy, the
# AICM handle or the mesh is unavailable — the annotation layer is never load-bearing.

_EDGE_RGB = (20, 22, 28)          # B-rep edge ink (visible segments)
_CALLOUT_RGB = (250, 250, 252)    # feature-callout text
_CALLOUT_BG = (24, 26, 32)

# Highlight colours are RESERVED: none is produced by the renderer's side palette
# (orient_tint: +Z blue, -Z teal, +X red, -X orange, +Y green, -Y yellow), so "magenta means
# highlighted" can never be misread as "this face points right".
_HIGHLIGHT_PALETTE = (
    ((255, 0, 200), "magenta"),
    ((160, 70, 255), "violet"),
    ((255, 80, 140), "hot-pink"),
    ((120, 0, 190), "purple"),
    ((255, 255, 255), "white"),
)
_SIDE_PALETTE_LEGEND = (
    "flat faces are tinted by the direction they face — top +Z blue, bottom -Z teal, "
    "front -Y yellow, back +Y green, right +X red, left -X orange; curved faces stay grey"
)
_MAX_CALLOUTS = 5                 # legibility cap; omitted ones are named in the caption
_CALLOUT_MIN_PIXEL_FRAC = 0.003   # a feature must hold >=0.3% of the frame to earn a callout
_HLR_TRI_BUDGET = 2_000_000       # above this the depth pass is skipped (caption says so)
_RASTER_BATCH_PX = 1 << 17        # candidate pixels per vectorised raster batch (cache-sized)
_MESH_CACHE: dict = {}            # doc_id -> (aicm_handle, mesh)


def _annot_font(size: int = 13):
    """A scalable font for overlay labels. Pillow's bundled default is scalable from 10.1;
    a system sans is preferred when present. Never raises."""
    from PIL import ImageFont  # noqa: PLC0415
    paths = ("/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")
    if os.name == "nt":
        paths = (str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arialbd.ttf"),) + paths
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _doc_mesh(doc_id: str) -> Optional[dict]:
    """Current tessellation of `doc_id`, or None when it cannot be read.

    Returns {"pos": (N,3) float64, "idx": (M,3) int32, "tri_face": (M,) int32 (index into
    "faces", -1 for raw-mesh triangles), "faces": [{body, face_id, first_tri, tri_count,
    surf_type}] with GLOBAL triangle indices, "edges": [{body, edge_id, pts (K,3)}],
    "last_body": int, "body_components": {body_id: assembly_component}}.
    `face_id`/`edge_id` are the OCCT topo ids, which are exactly the
    1-based doc_query enumeration index on that body (the same join doc_tree.owner_map and
    _feature_owned_matches use), so a sid or a feature id maps straight onto mesh geometry.
    Cached per (doc_id, aicm handle) — the handle carries the revision, so an edit
    invalidates it automatically."""
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        rb = cb.call("doc_rebuild", {"doc_id": doc_id}) or {}
    except Exception:  # noqa: BLE001
        return None
    handle = ((rb.get("tess") or {}).get("aicm_path")) or ""
    if not handle:
        return None
    cached = _MESH_CACHE.get(doc_id)
    if cached and cached[0] == handle:
        return cached[1]
    try:
        from services.aicm_handoff import read_handoff  # noqa: PLC0415
        from services.sim.tessellate import read_aicm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    try:
        parsed = read_aicm(read_handoff(handle))
    except Exception:  # noqa: BLE001
        return None

    bodies = parsed.get("bodies") or []
    pos_parts, idx_parts, faces, edges = [], [], [], []
    vbase = 0
    tbase = 0
    last_body = 0
    for b in bodies:
        p = np.asarray(b.get("pos"), dtype=np.float64).reshape(-1, 3)
        t = np.asarray(b.get("idx"), dtype=np.int64).reshape(-1, 3)
        if p.size == 0 or t.size == 0:
            continue
        bid = int(b.get("body_id") or 0)
        last_body = max(last_body, bid)
        pos_parts.append(p)
        idx_parts.append(t + vbase)
        for fr in (b.get("faces") or []):
            faces.append({"body": bid, "face_id": int(fr["face_id"]),
                          "first_tri": int(fr["first_tri"]) + tbase,
                          "tri_count": int(fr["tri_count"]),
                          "surf_type": int(fr.get("surf_type", 0))})
        epts = b.get("edge_pts")
        if epts is not None:
            epts = np.asarray(epts, dtype=np.float64).reshape(-1, 3)
            for er in (b.get("edges") or []):
                a = int(er["first_pt"]); n = int(er["pt_count"])
                if n >= 2:
                    edges.append({"body": bid, "edge_id": int(er["edge_id"]),
                                  "pts": epts[a:a + n]})
        vbase += p.shape[0]
        tbase += t.shape[0]
    if not pos_parts:
        return None

    pos = np.concatenate(pos_parts, axis=0)
    idx = np.concatenate(idx_parts, axis=0)
    tri_face = np.full(idx.shape[0], -1, dtype=np.int32)
    for fi, fr in enumerate(faces):
        tri_face[fr["first_tri"]:fr["first_tri"] + fr["tri_count"]] = fi
    mesh = {"pos": pos, "idx": idx, "tri_face": tri_face, "faces": faces,
            "edges": edges, "last_body": last_body,
            "body_components": {int(b["body_id"]): b["component"]
                                for b in (rb.get("tess") or {}).get("bodies") or []
                                if b.get("component")}}
    _MESH_CACHE.clear()  # one doc's mesh at a time — these arrays are the big allocation
    _MESH_CACHE[doc_id] = (handle, mesh)
    return mesh


def _mesh_topo_index(mesh: dict) -> tuple:
    """(face_by_topo, edge_by_topo): 1-based doc_query topo id -> index into mesh["faces"] /
    mesh["edges"], across EVERY body in the document.

    AICM `face_id`/`edge_id` are 1-based TopExp indices WITHIN their own body, while
    doc_query enumerates the whole document body-by-body with ONE running counter — so the
    two agree only on the first body. Offsetting each body by the subshape count of every
    preceding body (ascending body id, the order doc_query walks them) is the join.

    The overlay used to build these maps from the LAST body alone, which on a multi-body doc
    failed twice over: every face enumerated after the first body fell out of the map (no
    mask, "no tessellation"), and the first body's ids silently ALIASED onto the last body's
    faces (a study figure painting the wrong solid). Single-body docs were unaffected, which
    is why it survived — same class of bug as the doc_query/face_points last-body join.
    """
    out: list = []
    for recs, id_key in ((mesh.get("faces") or [], "face_id"),
                         (mesh.get("edges") or [], "edge_id")):
        counts: dict = {}
        for r in recs:
            b = int(r["body"])
            counts[b] = max(counts.get(b, 0), int(r[id_key]))
        offset, acc = {}, 0
        for b in sorted(counts):
            offset[b] = acc
            acc += counts[b]
        out.append({offset[int(r["body"])] + int(r[id_key]): i for i, r in enumerate(recs)})
    return out[0], out[1]


def _mesh_render_frame(mesh: dict, view_dir: tuple,
                       W: int = _RENDER_W, H: int = _RENDER_H) -> dict:
    """The EXACT orthographic frame core render_view_to_rgb builds for this mesh.

    Same key set as _part_render_frame (so _world_to_screen works unchanged), but the
    extents come from the projected MESH vertices, like the C++ does — not from the
    slightly-inflated doc_bbox, which would put the overlay a pixel or two off."""
    import numpy as np  # noqa: PLC0415
    pos = mesh["pos"]
    lo = pos.min(axis=0); hi = pos.max(axis=0)
    c = tuple((lo + hi) * 0.5)
    d = _vnorm(view_dir)
    up_ref = (0.0, 1.0, 0.0) if abs(d[2]) > 0.985 else (0.0, 0.0, 1.0)
    r = _vnorm(_vcross(up_ref, d))
    u = _vnorm(_vcross(d, r))
    rel = pos - np.asarray(c)
    us = rel @ np.asarray(r); vs = rel @ np.asarray(u)
    minu, maxu = float(us.min()), float(us.max())
    minv, maxv = float(vs.min()), float(vs.max())
    scale = _RENDER_FIT * min(W / max(maxu - minu, 1e-6), H / max(maxv - minv, 1e-6))
    return {"center": c, "r": r, "u": u, "d": d, "scale": scale,
            "cu": 0.5 * (minu + maxu), "cv": 0.5 * (minv + maxv), "W": W, "H": H}


def _screen_xyz(pts, frame: dict):
    """Bulk world -> (sx, sy, depth) through `frame`. depth is dot(p-centre, view_dir):
    LARGER is nearer the camera, matching the C++ z-buffer test. `scale_y` (only set by
    _crop_frame) lets the same code project into a cropped-and-rescaled image. A
    perspective frame (engine camera_pose.v1) projects through the pinhole instead, with
    reciprocal depth — still larger-is-nearer, and NaN/-1e30 for points behind the near
    plane (cad_viewpoint.perspective_project)."""
    if frame.get("projection") == "perspective":
        from . import cad_viewpoint as _vp  # noqa: PLC0415
        return _vp.perspective_project(pts, frame)
    import numpy as np  # noqa: PLC0415
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 3) - np.asarray(frame["center"])
    sy_scale = frame.get("scale_y", frame["scale"])
    su = (p @ np.asarray(frame["r"]) - frame["cu"]) * frame["scale"] + frame["W"] / 2.0
    sv = frame["H"] / 2.0 - (p @ np.asarray(frame["u"]) - frame["cv"]) * sy_scale
    return np.stack([su, sv, p @ np.asarray(frame["d"])], axis=-1)


def _crop_frame(frame: dict, rect: dict, out_size: tuple = (_RENDER_W, _RENDER_H)) -> dict:
    """The projection frame of an image that _crop_png_to_rect produced from `frame`.

    The crop is a pure 2D affine on screen space (translate by the rect origin, then scale
    each axis independently to out_size), so folding it back into the frame keeps the overlay
    pixel-exact on the CROPPED image instead of drawing 1-px edges that the upscale would
    smear into 8-px bands."""
    w2, h2 = int(out_size[0]), int(out_size[1])
    sx = w2 / max(rect["x1"] - rect["x0"], 1)
    sy = h2 / max(rect["y1"] - rect["y0"], 1)
    scale_x = frame["scale"] * sx
    scale_y = frame["scale"] * sy
    return dict(frame,
                scale=scale_x, scale_y=scale_y, W=w2, H=h2,
                cu=frame["cu"] + ((frame["W"] / 2.0 - rect["x0"]) * sx - w2 / 2.0) / scale_x,
                cv=frame["cv"] + ((frame["H"] / 2.0 - rect["y0"]) * sy - h2 / 2.0) / scale_y)


# --------------------------------------------------------------------------- focused close-ups
# A close-up used to be a 32x32 patch of a 512x400 whole-part render, bicubic-upscaled 16x into
# mush — the engine could only auto-fit the whole model. core/doc/session.cpp now accepts a
# `focus` world AABB (plus `clip_near` for a cutaway) and echoes the exact projection it used,
# so a feature review is RENDERED at the resolution the model reads it at, and every overlay
# reprojects through the engine's own camera instead of guessing it back.

_CLOSEUP_W, _CLOSEUP_H = 768, 576   # close-ups render larger than the whole-part 512x400 shot
_CLOSEUP_MARGIN = 0.35              # padding around the target AABB: enough context to place it


def _frame_from_rpc(resp: Optional[dict]) -> Optional[dict]:
    """The projection doc_render_view actually used, as the frame dict _screen_xyz consumes.

    Re-deriving the camera in Python (_part_render_frame / _mesh_render_frame) is a guess that
    is a pixel or two off on a whole-part render and completely wrong on a focused one. The
    engine echoes `frame`; prefer it whenever it is there. `clip_depth` comes with it so an
    overlay can apply the SAME near-clip — without it, the material the engine cut away still
    wins the overlay's depth test and the target the cutaway exists to reveal reads as hidden."""
    f = (resp or {}).get("frame")
    if not isinstance(f, dict) or not f.get("width"):
        return None
    if f.get("projection") == "perspective":
        from . import cad_viewpoint as _vp  # noqa: PLC0415
        try:
            return dict(_vp.frame_from_echo(f, (resp or {}).get("camera")), focused=True)
        except (KeyError, TypeError, ValueError):
            return None
    try:
        return {"center": _v3(f["center"]), "r": _v3(f["right"]), "u": _v3(f["up"]),
                "d": _v3(f["dir"]), "scale": float(f["scale"]),
                "cu": float(f["cu"]), "cv": float(f["cv"]),
                "W": int(f["width"]), "H": int(f["height"]),
                "focused": bool(f.get("focused")),
                "clip_near": bool(f.get("clip_near")),
                "clip_depth": float(f.get("clip_depth") or 0.0)}
    except (KeyError, TypeError, ValueError):
        return None


def render_background(background: Optional[Sequence]) -> Optional[list]:
    """Normalize a caller's `background` into the engine's [r, g, b] 0-255 form, or None.

    One place decides what a background argument means, because every render tool takes the
    same one and the engine clamps rather than refuses: three numbers in 0-255 (rounded,
    clamped), None/omitted meaning "keep the engine's flat 245 light grey". A hex string is
    accepted too ("#101114") — the film/report callers already carry their palette that way.
    Anything else is a caller error and is refused here rather than silently ignored."""
    if background is None:
        return None
    if isinstance(background, str):
        text = background.strip().lstrip("#")
        if len(text) != 6:
            raise ValueError(
                f"background must be [r, g, b] (0-255) or a '#rrggbb' string; got "
                f"{background!r}.")
        try:
            return [int(text[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            raise ValueError(
                f"background {background!r} is not a valid '#rrggbb' colour.") from None
    try:
        vals = [float(v) for v in background]
    except (TypeError, ValueError):
        raise ValueError(
            f"background must be [r, g, b] (0-255) or a '#rrggbb' string; got "
            f"{background!r}.") from None
    if len(vals) != 3:
        raise ValueError(
            f"background needs exactly 3 components [r, g, b] (0-255); got {len(vals)}.")
    return [max(0, min(255, int(round(v)))) for v in vals]


def _render_view_png(doc_id: str, view_dir, out_png: str, *,
                     name: str = "VIEW", focus: Optional[dict] = None,
                     margin: float = _CLOSEUP_MARGIN, clip_near: bool = False,
                     W: int = _RENDER_W, H: int = _RENDER_H,
                     annotate: bool = True,
                     background: Optional[Sequence] = None,
                     camera: Optional[dict] = None,
                     section: Optional[dict] = None) -> tuple:
    """One engine render along `view_dir`, optionally FRAMED on a world AABB.

    `focus` is a bbox dict ({"min","max"} — what _target_bbox returns). With it the engine fits
    THAT box to the image, so the close-up is real pixels rather than a magnified patch;
    `clip_near` additionally cuts away whatever sits between the camera and the box, which is
    how a bore or a pocket gets seen into without a separate section operation.

    `background` is an [r, g, b] 0-255 frame clear colour handed straight to the engine
    (doc_render_view's `background`); None keeps the engine's flat 245 light grey.

    `camera` is an exact perspective pose (cad_viewpoint.perspective_camera): it replaces the
    direction/focus camera entirely — the engine refuses the two together — and the returned
    frame is then a perspective frame every overlay helper projects through.

    `section` ({"point", "normal"}) renders the engine's section view (doc_render_view
    `view.section`): the half-space dot(p - point, normal) <= 0 is kept, its cut face is
    capped and hatched, and the removed half is drawn as a ghost with its edges. The returned
    frame carries the plane so every overlay clips the tessellation the same way.

    Returns (png_path, frame, resp). `frame` is the engine's own projection record — pass it
    straight to _annotate_render so the overlay lands on the right pixels. Raises ValueError
    with the engine's reason when the render fails."""
    if camera:
        keys = ("projection", "position", "target", "up", "vertical_fov_deg", "near", "far",
                "width", "height")
        view = {"name": str(name), "camera": {k: camera[k] for k in keys if k in camera}}
    else:
        view = {"dir": [float(x) for x in _vnorm(view_dir)], "name": str(name),
                "width": int(W), "height": int(H)}
    if focus and not camera:
        view["focus"] = {"min": [float(x) for x in _v3(focus["min"])],
                         "max": [float(x) for x in _v3(focus["max"])],
                         "margin": float(margin), "clip_near": bool(clip_near)}
    plane = None
    if section:
        plane = {"point": [float(x) for x in _v3(section["point"])],
                 "normal": [float(x) for x in _vnorm(section["normal"])]}
        view["section"] = dict(plane, ghost=True, hatch=True)
    params = {"doc_id": doc_id, "out_path": out_png, "view": view,
              "annotate": bool(annotate)}
    bg = render_background(background)
    if bg is not None:
        params["background"] = bg
    r = cb.call("doc_render_view", params)
    if not r.get("ok") or not os.path.exists(out_png):
        raise ValueError(
            f"Render failed: {r.get('reason', r.get('error', 'no bodies to render'))}")
    frame = _frame_from_rpc(r)
    if frame is not None and plane is not None:
        frame["section"] = plane
    return out_png, frame, r


def _target_mesh_tris(mesh: Optional[dict], doc_id: str, matches: list):
    """Boolean array over `mesh["idx"]` rows marking the triangles the target owns.

    Joins each match's sid to its 1-based doc_query topo index and then to the AICM FaceRec
    triangle range. Returns None when there is no mesh, no numpy, or the target is edges-only
    (nothing to rasterize) — callers then fall back to the target AABB alone."""
    if mesh is None:
        return None
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    topo = _doc_topo_maps(doc_id)
    want = {topo["face_sid"][str(m.get("sid"))] for m in (matches or [])
            if str(m.get("sid") or "") in topo["face_sid"]}
    if not want:
        return None
    face_by_topo, _e = _mesh_topo_index(mesh)
    sel = np.zeros(mesh["idx"].shape[0], dtype=bool)
    hit = False
    for t in want:
        fi = face_by_topo.get(t)
        if fi is None:
            continue
        fr = mesh["faces"][fi]
        sel[fr["first_tri"]:fr["first_tri"] + fr["tri_count"]] = True
        hit = True
    return sel if hit else None


def _ideal_views(doc_id: str, matches: list, target_bbox: dict, *, n: int = 1) -> list:
    """The planner's best `n` complementary camera(s) for a target, best first:
    [{"dir", "label", "section", "score", "visible_frac"}] (shot_planner.plan_shots — the
    engine-measured, set-ranked choice every render tool uses).

    Degrades to the plain mean-normal / iso direction when the planner cannot measure (no
    geometry, an engine without doc_view_metrics) — a measurement figure is never lost to a
    camera-choice problem."""
    fallback = [{"dir": _camera_dir_for_target(matches, "auto"), "label": "auto",
                 "section": None, "score": None, "visible_frac": None}]
    try:
        from . import shot_planner as _sp  # noqa: PLC0415
        plan = _sp.plan_shots(doc_id, target={"matches": list(matches or []),
                                              "bbox": target_bbox, "label": "target"},
                              k=max(1, int(n)))
    except Exception:  # noqa: BLE001
        return fallback
    return [{"dir": tuple(v["dir"]), "label": v.get("label") or "auto",
             "section": v.get("section"), "score": v.get("utility"),
             "visible_frac": (v.get("summary") or {}).get("target_visible")}
            for v in plan["views"]] or fallback


def _clip_mesh_halfspace(pos, idx, tri_face, point, normal) -> tuple:
    """The tessellation clipped to the kept half-space dot(p - point, normal) <= 0 — the same
    half a section render keeps. Triangles wholly kept pass through; straddling ones are
    Sutherland-Hodgman clipped against the plane and fanned, winding preserved, so back-face
    tests still see outward normals. Returns (pos, idx, tri_face)."""
    import numpy as np  # noqa: PLC0415
    p = np.asarray(_v3(point), dtype=np.float64)
    n = np.asarray(_vnorm(normal), dtype=np.float64)
    s = (pos - p) @ n
    st = s[idx]
    kept = (st <= 0.0).all(axis=1)
    straddle = ~kept & (st <= 0.0).any(axis=1)
    new_pts: list = []
    new_tris: list = []
    new_face: list = []
    base = pos.shape[0]
    for t in np.flatnonzero(straddle).tolist():
        ids = idx[t]
        poly: list = []
        for k in range(3):
            a, b = int(ids[k]), int(ids[(k + 1) % 3])
            sa, sb = float(s[a]), float(s[b])
            if sa <= 0.0:
                poly.append(pos[a])
            if (sa <= 0.0) != (sb <= 0.0):
                poly.append(pos[a] + (pos[b] - pos[a]) * (sa / (sa - sb)))
        if len(poly) < 3:
            continue
        first = base + len(new_pts)
        new_pts.extend(poly)
        for k in range(1, len(poly) - 1):
            new_tris.append((first, first + k, first + k + 1))
            new_face.append(int(tri_face[t]))
    out_pos = np.concatenate([pos, np.asarray(new_pts, dtype=np.float64).reshape(-1, 3)])
    out_idx = np.concatenate([idx[kept], np.asarray(new_tris, dtype=idx.dtype).reshape(-1, 3)])
    out_face = np.concatenate([tri_face[kept],
                               np.asarray(new_face, dtype=tri_face.dtype).reshape(-1)])
    return out_pos, out_idx, out_face


def _mesh_depth_buffer(mesh: dict, frame: dict) -> Optional[tuple]:
    """Rasterize the mesh into (depth, face_index) buffers matching the engine's render.

    Reproduces render_view_to_rgb's barycentric span fill and `depth > zbuf` test exactly,
    so a pixel's winning triangle here is the pixel the engine shaded. Like the engine, every
    triangle is rasterized, back faces included: on a closed solid they never win, and on an
    open shell (an imported sheet body, a tube with no end face) the engine SHOWS the inside
    of the surface, so the overlay must attribute those pixels to the same face. Returns
    (Z, FI, S) or None when the mesh is over the triangle budget — the caller then reports
    that edges were skipped rather than lying about them.

    When `frame` carries the engine's near-clip (`clip_near` + `clip_depth`, a focused cutaway),
    the same fragments are dropped here. Skipping that is not a cosmetic mismatch: the cut-away
    material would win this z-test, and the feature the cutaway was made to expose would come
    back reported as invisible. A perspective frame instead clips each triangle against the
    near/far planes in camera space (as the engine does) and interpolates reciprocal depth,
    and back-faces (for the section cap) are judged against the ray from the eye.

    A frame carrying a `section` plane (a section render) gets the engine's section rule: the
    tessellation is clipped to the kept half-space, back faces are rasterized too, and a pixel
    whose nearest fragment is a back face is the CAP (FI = -2) — without that the far wall of
    a bore would show through the solid cut face and be reported visible."""
    import numpy as np  # noqa: PLC0415
    idx = mesh["idx"]
    if idx.shape[0] > _HLR_TRI_BUDGET:
        return None
    W, H = int(frame["W"]), int(frame["H"])
    pos = mesh["pos"]
    tri_face = mesh["tri_face"]
    section = frame.get("section")
    if section:
        pos, idx, tri_face = _clip_mesh_halfspace(pos, idx, tri_face, section["point"],
                                                  section["normal"])
    S = _screen_xyz(pos, frame)
    persp = frame.get("projection") == "perspective"
    tri_ids = np.arange(idx.shape[0])
    if section:
        p0, p1, p2 = pos[idx[:, 0]], pos[idx[:, 1]], pos[idx[:, 2]]
        if persp:
            eye = np.asarray(frame["position"], dtype=np.float64)
            front = np.einsum("ij,ij->i", np.cross(p1 - p0, p2 - p0), eye[None, :] - p0) > 0.0
        else:
            front = np.cross(p1 - p0, p2 - p0) @ np.asarray(frame["d"]) > 0.0
        tri_face = np.where(front, tri_face, -2).astype(np.int32)
    clip = (float(frame["clip_depth"])
            if not persp and frame.get("clip_near") and frame.get("clip_depth") is not None
            else None)

    order = np.arange(tri_ids.shape[0], dtype=np.int64)
    if not persp:
        t_idx = idx[tri_ids]
        return (*_raster_tris(S[t_idx[:, 0]], S[t_idx[:, 1]], S[t_idx[:, 2]],
                              tri_face[tri_ids], order * 8, W, H, clip), S)

    from . import cad_viewpoint as _vp  # noqa: PLC0415
    C = _vp.camera_space(pos, frame)
    near, far = float(frame["near"]), float(frame["far"])
    focal, cx, cy = float(frame["focal"]), float(frame["cx"]), float(frame["cy"])
    t_idx = idx[tri_ids]
    zs3 = C[t_idx, 2]
    whole = (zs3.min(axis=1) >= near) & (zs3.max(axis=1) <= far)
    gone = (zs3.max(axis=1) < near) | (zs3.min(axis=1) > far)
    A, B, Cc = S[t_idx[whole, 0]], S[t_idx[whole, 1]], S[t_idx[whole, 2]]
    face, key = tri_face[tri_ids][whole], order[whole] * 8
    # Triangles crossing the near/far planes are clipped in camera space exactly as the engine
    # does and fanned; the fan keeps its parent's draw order so depth ties resolve the same.
    fan_a, fan_b, fan_c, fan_f, fan_k = [], [], [], [], []
    for j in np.flatnonzero(~whole & ~gone).tolist():
        poly = _vp.clip_polygon_depth([tuple(C[i]) for i in t_idx[j]], near, far)
        if len(poly) < 3:
            continue
        sp = [(cx + focal * p[0] / p[2], cy - focal * p[1] / p[2], 1.0 / p[2]) for p in poly]
        for k in range(1, len(sp) - 1):
            fan_a.append(sp[0]); fan_b.append(sp[k]); fan_c.append(sp[k + 1])
            fan_f.append(int(tri_face[tri_ids[j]])); fan_k.append(int(order[j]) * 8 + k)
    if fan_a:
        A = np.concatenate([A, np.asarray(fan_a, dtype=np.float64)])
        B = np.concatenate([B, np.asarray(fan_b, dtype=np.float64)])
        Cc = np.concatenate([Cc, np.asarray(fan_c, dtype=np.float64)])
        face = np.concatenate([face, np.asarray(fan_f, dtype=face.dtype)])
        key = np.concatenate([key, np.asarray(fan_k, dtype=np.int64)])
    return (*_raster_tris(A, B, Cc, face, key, W, H, None), S)


def _raster_tris(A, B, C, face, key, W: int, H: int, clip: Optional[float]) -> tuple:
    """Z-buffer screen triangles (rows of (sx, sy, depth)) into (Z, FI), vectorised.

    Same fill rule as the engine's span fill: pixel centres (x + 0.5, y + 0.5) whose three
    barycentrics are >= 0 inside the clamped bounding box, depth interpolated linearly, and a
    strictly nearer (larger) depth wins — so on an exact tie the triangle drawn FIRST (smallest
    `key`, its draw order) keeps the pixel. Triangles are bucketed by power-of-two bounding-box
    size and evaluated in batches of at most _RASTER_BATCH_PX candidate pixels."""
    import numpy as np  # noqa: PLC0415
    Z = np.full(H * W, -1e30, dtype=np.float64)
    FI = np.full(H * W, -1, dtype=np.int32)
    if A.shape[0] == 0:
        return Z.reshape(H, W), FI.reshape(H, W)
    xs = np.stack((A[:, 0], B[:, 0], C[:, 0]))
    ys = np.stack((A[:, 1], B[:, 1], C[:, 1]))
    lo_x, hi_x = xs.min(axis=0), xs.max(axis=0)
    lo_y, hi_y = ys.min(axis=0), ys.max(axis=0)
    area = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (C[:, 0] - A[:, 0]) * (B[:, 1] - A[:, 1])
    ok = (np.isfinite(lo_x) & np.isfinite(hi_x) & np.isfinite(lo_y) & np.isfinite(hi_y)
          & ~((hi_x < 0.0) | (lo_x > W) | (hi_y < 0.0) | (lo_y > H)) & (np.abs(area) >= 1e-9))
    sel = np.flatnonzero(ok)
    minx = np.maximum(np.floor(lo_x[sel]), 0).astype(np.int64)
    maxx = np.minimum(np.ceil(hi_x[sel]), W - 1).astype(np.int64)
    miny = np.maximum(np.floor(lo_y[sel]), 0).astype(np.int64)
    maxy = np.minimum(np.ceil(hi_y[sel]), H - 1).astype(np.int64)
    keep = (maxx >= minx) & (maxy >= miny)
    sel, minx, maxx, miny, maxy = sel[keep], minx[keep], maxx[keep], miny[keep], maxy[keep]
    if sel.size == 0:
        return Z.reshape(H, W), FI.reshape(H, W)
    bw = np.ceil(np.log2(maxx - minx + 1)).astype(np.int64)
    bh = np.ceil(np.log2(maxy - miny + 1)).astype(np.int64)
    bucket = bw * 64 + bh
    by = np.argsort(bucket, kind="stable")
    starts = np.flatnonzero(np.r_[True, bucket[by][1:] != bucket[by][:-1]])
    ends = np.r_[starts[1:], by.size]
    frag_p, frag_d, frag_k = [], [], []
    for s0, s1 in zip(starts.tolist(), ends.tolist()):
        grp = by[s0:s1]
        gw = int((maxx[grp] - minx[grp]).max()) + 1
        gh = int((maxy[grp] - miny[grp]).max()) + 1
        per = max(1, _RASTER_BATCH_PX // (gw * gh))
        ox = np.arange(gw, dtype=np.int64)[None, None, :]
        oy = np.arange(gh, dtype=np.int64)[None, :, None]
        for c0 in range(s0, s1, per):
            m = by[c0:min(c0 + per, s1)]
            t = sel[m]
            px = minx[m][:, None, None] + ox
            py = miny[m][:, None, None] + oy
            fx = px.astype(np.float64) + 0.5
            fy = py.astype(np.float64) + 0.5
            s0x, s0y, s0z = (A[t, k][:, None, None] for k in range(3))
            s1x, s1y, s1z = (B[t, k][:, None, None] for k in range(3))
            s2x, s2y, s2z = (C[t, k][:, None, None] for k in range(3))
            ar = area[t][:, None, None]
            a0 = ((s1x - fx) * (s2y - fy) - (s2x - fx) * (s1y - fy)) / ar
            a1 = ((s2x - fx) * (s0y - fy) - (s0x - fx) * (s2y - fy)) / ar
            a2 = 1.0 - a0 - a1
            inside = ((a0 >= 0) & (a1 >= 0) & (a2 >= 0)
                      & (px <= maxx[m][:, None, None]) & (py <= maxy[m][:, None, None]))
            dep = a0 * s0z + a1 * s1z + a2 * s2z
            if clip is not None:
                inside &= dep <= clip
            if not inside.any():
                continue
            frag_p.append(np.broadcast_to(py * W + px, inside.shape)[inside])
            frag_d.append(dep[inside])
            frag_k.append(np.broadcast_to(key[t][:, None, None], inside.shape)[inside])
    if not frag_p:
        return Z.reshape(H, W), FI.reshape(H, W)
    p = np.concatenate(frag_p)
    d = np.concatenate(frag_d)
    k = np.concatenate(frag_k)
    np.maximum.at(Z, p, d)
    win = d == Z[p]
    first = np.full(H * W, np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(first, p[win], k[win])
    hit = first != np.iinfo(np.int64).max
    face_of_key = dict(zip(key.tolist(), face.tolist()))
    uk, inv = np.unique(first[hit], return_inverse=True)
    FI[hit] = np.asarray([face_of_key[x] for x in uk.tolist()], dtype=np.int32)[inv]
    return Z.reshape(H, W), FI.reshape(H, W)


def _edge_depth_ref(Z):
    """Per pixel, the FARTHEST covered depth in its 3x3 neighbourhood (background ignored;
    -1e30 where all nine pixels are background) — the depth an edge sample there must reach.

    An edge lies ON the faces it bounds, so a one-pixel screen offset on a face sloping in
    depth already puts the surface nearer than the edge (in perspective the reciprocal-depth
    slope across a pixel is about the 0.4 % tolerance itself), and at a silhouette the sample
    sits half a pixel off the face. The farthest covered neighbour absorbs both, while a real
    occluder covers the whole neighbourhood with nearer depth and still hides the edge."""
    import numpy as np  # noqa: PLC0415
    P = np.pad(np.where(Z > -1e29, Z, np.inf), 1, mode="constant", constant_values=np.inf)
    h, w = Z.shape
    out = P[0:h, 0:w].copy()
    for a in range(3):
        for c in range(3):
            np.minimum(out, P[a:a + h, c:c + w], out=out)
    return np.where(np.isinf(out), -1e30, out)


def _visible_runs(pts, frame: dict, Zref, eps: float, *, hidden: bool = False) -> list:
    """Split a world polyline into visible or hidden screen-space runs against `Zref`.

    Each run is a list of (x, y) floats ready for ImageDraw.line. Points are resampled to
    ~2 px so a long edge disappearing behind a boss is cut at the right place."""
    import numpy as np  # noqa: PLC0415
    S = _screen_xyz(pts, frame)
    if S.shape[0] < 2:
        return []
    H, W = Zref.shape
    a, b = S[:-1], S[1:]
    seg_len = np.hypot(b[:, 0] - a[:, 0], b[:, 1] - a[:, 1])
    fin = np.isfinite(seg_len)
    # A segment with an endpoint behind the near plane (perspective) has no screen position:
    # it contributes one invisible sample, which ends the run in progress.
    n = np.where(fin, np.maximum(2, (np.where(fin, seg_len, 0.0) / 2.0).astype(np.int64) + 2), 1)
    seg = np.repeat(np.arange(a.shape[0]), n)
    j = np.arange(seg.size) - np.repeat(np.cumsum(n) - n, n)
    t = (j / np.maximum(n - 1, 1)[seg])[:, None]
    smp = a[seg] * (1.0 - t) + b[seg] * t
    ok = fin[seg] & np.isfinite(smp[:, 0]) & np.isfinite(smp[:, 1])
    sx = np.where(ok, smp[:, 0], -1e9)
    sy = np.where(ok, smp[:, 1], -1e9)
    xi = np.clip(sx.astype(np.int64), 0, W - 1)
    yi = np.clip(sy.astype(np.int64), 0, H - 1)
    vis = (ok & (sx >= -2) & (sx < W + 2) & (sy >= -2) & (sy < H + 2)
           & ((smp[:, 2] < Zref[yi, xi] - eps) if hidden
              else (smp[:, 2] >= Zref[yi, xi] - eps)))
    edge = np.diff(np.concatenate(([0], vis.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edge == 1), np.flatnonzero(edge == -1)
    xy = np.stack((sx, sy), axis=1)
    return [list(map(tuple, xy[s:e].tolist())) for s, e in zip(starts.tolist(), ends.tolist())
            if e - s > 1]


def _mask_anchor(mask) -> Optional[tuple]:
    """A pixel INSIDE `mask` closest to its centroid — where a leader line should land."""
    import numpy as np  # noqa: PLC0415
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    cx, cy = float(xs.mean()), float(ys.mean())
    k = int(np.argmin((xs - cx) ** 2 + (ys - cy) ** 2))
    return (int(xs[k]), int(ys[k]))


def _mask_outline(draw, mask, rgb: tuple, width: int = 2) -> None:
    """Trace `mask`'s boundary pixels so a highlighted face reads as an outlined region
    even where the tint is subtle against its own shading."""
    import numpy as np  # noqa: PLC0415
    m = mask
    b = np.zeros_like(m)
    b[1:, :] |= m[1:, :] & ~m[:-1, :]
    b[:-1, :] |= m[:-1, :] & ~m[1:, :]
    b[:, 1:] |= m[:, 1:] & ~m[:, :-1]
    b[:, :-1] |= m[:, :-1] & ~m[:, 1:]
    if not b.any():
        return
    r = max(1, width) - 1
    if r:
        # The disc of radius r around each boundary pixel, as one point list.
        grown = b.copy()
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy > r * r + r:
                    continue
                src = b[max(0, -dy):b.shape[0] - max(0, dy), max(0, -dx):b.shape[1] - max(0, dx)]
                grown[max(0, dy):b.shape[0] - max(0, -dy),
                      max(0, dx):b.shape[1] - max(0, -dx)] |= src
        b = grown
    ys, xs = np.nonzero(b)
    draw.point(list(zip(xs.tolist(), ys.tolist())), fill=rgb)


# render_view_to_rgb's orientation tint (orient_tint + its annotate blend): a PLANAR face whose
# averaged triangle normal is within ~23 deg of a principal axis is mixed 55 % toward that side's
# hue before the Lambert shade 0.35 + 0.65|n.L| is applied. Reproduced here from the face-index
# buffer so an exact-camera tile gets the side colours without the engine's TOP/FRONT text
# callouts, triad and header (which the planner's own tags, triad and badge replace).
_ORIENT_MIX = 0.55
_ORIENT_LIGHT = (0.4, 0.5, 1.0)


def _orient_hue(n) -> Optional[tuple]:
    ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
    if max(ax, ay, az) < 0.92:
        return None
    if az >= ax and az >= ay:
        return (90, 140, 225) if n[2] > 0 else (70, 175, 185)
    if ax >= ay and ax >= az:
        return (220, 80, 80) if n[0] > 0 else (225, 140, 70)
    return (95, 185, 95) if n[1] > 0 else (215, 200, 70)


def _orientation_tints(mesh: dict) -> tuple:
    """(has[F] bool, rgb[F,3], lam[F]) per mesh face, cached on the (revision-keyed) mesh."""
    import numpy as np  # noqa: PLC0415
    cached = mesh.get("_orient_tints")
    if cached is not None:
        return cached
    F = len(mesh["faces"])
    pos, idx, tf = mesh["pos"], mesh["idx"], mesh["tri_face"]
    n = np.cross(pos[idx[:, 1]] - pos[idx[:, 0]], pos[idx[:, 2]] - pos[idx[:, 0]])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(ln > 0.0, n / np.maximum(ln, 1e-300), 0.0)
    acc = np.zeros((F, 3))
    ok = tf >= 0
    np.add.at(acc, tf[ok], n[ok])
    light = np.asarray(_ORIENT_LIGHT) / np.linalg.norm(_ORIENT_LIGHT)
    has = np.zeros(F, dtype=bool)
    rgb = np.zeros((F, 3))
    lam = np.ones(F)
    for fi, fr in enumerate(mesh["faces"]):
        a = acc[fi]
        la = float(np.linalg.norm(a))
        if int(fr.get("surf_type", 0)) != 0 or la <= 0.0:
            continue
        u = a / la
        hue = _orient_hue(u)
        if hue is None:
            continue
        has[fi] = True
        rgb[fi] = hue
        lam[fi] = 0.35 + 0.65 * abs(float(u @ light))
    mesh["_orient_tints"] = (has, rgb, lam)
    return mesh["_orient_tints"]


def _apply_orientation_tint(arr, FI, mesh: dict) -> int:
    """Blend the engine's side hue into every visible pixel of a principal planar face, in
    place on the float RGB `arr`. The shaded pixel is base * 255 * lam, so the engine's tinted
    pixel ((1 - mix) * base + mix * hue) * lam is (1 - mix) * pixel + mix * hue * lam.
    Returns the number of pixels tinted."""
    import numpy as np  # noqa: PLC0415
    has, rgb, lam = _orientation_tints(mesh)
    if not has.any():
        return 0
    vis = FI >= 0
    fi = FI[vis]
    sel = has[fi]
    if not sel.any():
        return 0
    ys, xs = np.nonzero(vis)
    ys, xs, f = ys[sel], xs[sel], fi[sel]
    arr[ys, xs] = np.minimum(255.0, (1.0 - _ORIENT_MIX) * arr[ys, xs]
                             + _ORIENT_MIX * rgb[f] * lam[f][:, None])
    return int(sel.sum())


def _place_label(draw, font, anchor: tuple, text: str, rgb: tuple,
                 W: int, H: int, taken: list, strict: bool = False,
                 forbidden=None) -> Optional[tuple]:
    """Draw a leader line from `anchor` to a non-overlapping text chip and record its rect.

    Candidate offsets fan out from the anchor; the first that fits the frame and clears every
    rect in `taken` and every `forbidden` pixel wins. The chip is forced next to the anchor
    when no candidate clears — or, with `strict`, nothing is drawn and None is returned.
    A chip too large for the frame is always omitted."""
    try:
        tb = draw.textbbox((0, 0), text, font=font)
    except Exception:  # noqa: BLE001
        tb = (0, 0, 7 * len(text), 12)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad = 3
    bw, bh = tw + 2 * pad, th + 2 * pad
    if bw + 4 > W or bh + 4 > H:
        return None
    ax, ay = anchor
    best = None
    for dist in (26, 46, 70, 96, 124, 168, 208, 256, 320):
        for dx, dy in ((1, -1), (1, 1), (-1, -1), (-1, 1), (1, 0), (-1, 0), (0, -1), (0, 1)):
            x = ax + dx * dist - (bw if dx < 0 else 0)
            y = ay + dy * dist - (bh // 2 if dy == 0 else (bh if dy < 0 else 0))
            x = max(2, min(int(x), W - bw - 2))
            y = max(2, min(int(y), H - bh - 2))
            rect = (x, y, x + bw, y + bh)
            if any(not (rect[2] < o[0] or rect[0] > o[2]
                        or rect[3] < o[1] or rect[1] > o[3]) for o in taken):
                continue
            # PIL rectangle endpoints are inclusive: its outline also clears the mask.
            if forbidden is not None and forbidden[y:y + bh + 1, x:x + bw + 1].any():
                continue
            best = rect
            break
        if best:
            break
    if best is None:
        if strict:
            return None
        best = (max(2, min(ax + 26, W - bw - 2)), max(2, min(ay - 26, H - bh - 2)))
        best = (best[0], best[1], best[0] + bw, best[1] + bh)
    lx = best[0] if best[0] > ax else best[2]
    ly = (best[1] + best[3]) // 2
    draw.line([(ax, ay), (lx, ly)], fill=rgb, width=2)
    draw.line([(ax, ay), (lx, ly)], fill=(0, 0, 0), width=1)
    draw.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill=rgb, outline=(0, 0, 0))
    draw.rectangle(best, fill=_CALLOUT_BG, outline=rgb)
    draw.text((best[0] + pad - tb[0], best[1] + pad - tb[1]), text, font=font, fill=rgb)
    taken.append(best)
    return best


def _largest_cluster_anchor(mask) -> Optional[tuple]:
    """The most interior pixel of `mask`'s LARGEST 4-connected region (ties: nearest that
    region's centroid).

    A face split by an occluder (a plate seen either side of a boss) has its whole-mask
    centroid in the occluder; a leader landing there points at the wrong surface. The same
    holds inside one region: a plate pierced by a bore has its centroid in the hole, and the
    region pixel nearest it sits on the hole's rim, so a leader there reads as naming the bore.
    Interior = the largest distance to the region's edge along its row and column. Regions
    come from a run-length scan (row runs unioned with overlapping runs of the row above)."""
    import numpy as np  # noqa: PLC0415
    runs: list = []
    parent: list = []

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    prev: list = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        cur = []
        for x0, x1 in zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist()):
            i = len(runs)
            runs.append((y, x0, x1))
            parent.append(i)
            for j in prev:
                if runs[j][1] <= x1 and x0 <= runs[j][2]:
                    a, b = _find(i), _find(j)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
            cur.append(i)
        prev = cur
    if not runs:
        return None
    groups: dict = {}
    for i, (y, x0, x1) in enumerate(runs):
        n = x1 - x0 + 1
        g = groups.setdefault(_find(i), [0, 0.0, 0.0, []])
        g[0] += n
        g[1] += n * (x0 + x1) * 0.5
        g[2] += n * y
        g[3].append((y, x0, x1))
    n, sx, sy, members = max(groups.values(), key=lambda g: g[0])
    cx, cy = sx / n, sy / n
    y0 = min(m[0] for m in members)
    y1 = max(m[0] for m in members)
    xa = min(m[1] for m in members)
    xb = max(m[2] for m in members)
    reg = np.zeros((y1 - y0 + 1, xb - xa + 1), dtype=bool)
    for y, x0, x1 in members:
        reg[y - y0, x0 - xa:x1 - xa + 1] = True
    depth = np.minimum(np.minimum(_run_length(reg, 1, False), _run_length(reg, 1, True)),
                       np.minimum(_run_length(reg, 0, False), _run_length(reg, 0, True)))
    ys, xs = np.nonzero(depth == depth.max())
    k = int(np.argmin((xs + xa - cx) ** 2 + (ys + y0 - cy) ** 2))
    return (int(xs[k] + xa), int(ys[k] + y0))


def _run_length(mask, axis: int, reverse: bool):
    """Per True pixel, how many consecutive True pixels lead up to it (itself included) along
    `axis`, scanning forward or in `reverse`; 0 on False pixels."""
    import numpy as np  # noqa: PLC0415
    m = np.flip(mask, axis=axis) if reverse else mask
    n = m.shape[axis]
    shape = [1, 1]
    shape[axis] = n
    idx = np.arange(n).reshape(shape)
    last_off = np.maximum.accumulate(np.where(m, -1, idx), axis=axis)
    run = np.where(m, idx - last_off, 0)
    return np.flip(run, axis=axis) if reverse else run


def _draw_face_tags(draw, font, FI, mesh: dict, face_by_topo: dict, topo: dict,
                    spec: dict, W: int, H: int, taken: list, forbidden=None) -> list:
    """SID tags for up to spec["max"] faces: role order first (spec["priority"]: topo ->
    0 target, 1 fixture/load, 2 context ring), then visible pixels; faces under spec["min_px"]
    pixels get none. Each tag is a leader to the face's largest visible pixel cluster and a
    chip that overlaps no other chip (a tag with no free spot is dropped, not stacked).
    Highlighted SIDs use spec["labels"] (topo -> {"text", "rgb"}) instead of a second chip.
    Returns [{"sid", "text", "topo", "px", "role", "anchor"}]."""
    import numpy as np  # noqa: PLC0415
    prio = spec.get("priority") or {}
    labels = spec.get("labels") or {}
    limit = int(spec.get("max") or 8)
    min_px = max(int(spec.get("min_px") or 0), 1)
    counts = np.bincount(FI[FI >= 0].ravel(), minlength=len(mesh["faces"]))
    topo_of = {fi: t for t, fi in face_by_topo.items()}
    cands = []
    for fi, n in enumerate(counts.tolist()):
        t = topo_of.get(fi)
        if n >= min_px and t is not None:
            cands.append((int(prio.get(t, 3)), -int(n), t, fi))
    cands.sort()
    out: list = []
    drawn_sids: set = set()
    roles = ("target", "bc", "context", "other")
    for rank, neg_px, t, fi in cands:
        if len(out) >= limit:
            break
        m = topo["face"][t - 1] if 0 < t <= len(topo["face"]) else {}
        sid = m.get("sid")
        if not sid or str(sid) in drawn_sids:
            continue
        anchor = _largest_cluster_anchor(FI == fi)
        if anchor is None:
            continue
        ax, ay = anchor
        label = labels.get(t) or {}
        text = str(label.get("text") or sid)
        rgb = label.get("rgb") or (_EMPHASIS_GLOW if rank == 0 else _CALLOUT_RGB)
        if _place_label(draw, font, (ax, ay), text, rgb, W, H, taken,
                        strict=True, forbidden=forbidden) is None:
            continue
        drawn_sids.add(str(sid))
        out.append({"sid": str(sid), "text": text, "topo": int(t), "px": -neg_px,
                    "role": roles[rank], "anchor": [ax, ay]})
    return out


def _nice_step(v: float) -> float:
    """Round `v` down to the nearest 1/2/5 x 10^n so a scale bar reads as a round number."""
    if v <= 0:
        return 1.0
    e = math.floor(math.log10(v))
    base = 10.0 ** e
    for m in (5.0, 2.0, 1.0):
        if v >= m * base:
            return m * base
    return base


def _scale_bar_layout(draw, font, frame: dict, size_mm: tuple, W: int, H: int) -> dict:
    """Where the bottom-right scale plate goes: {"mm", "rect", "bar", "label", "dims"}.

    Two rows on one dark plate, both measured with the real font so nothing is clipped at the
    frame edge: the part bbox readout on top, and the bar with its length written to its LEFT
    below. `rect` is what other chips must stay clear of."""
    mm = _nice_step((0.22 * W) / max(frame["scale"], 1e-9))
    px = int(round(mm * frame["scale"]))
    px = max(24, min(px, W // 2))
    # "part bbox", not "bbox": on a close-up this sits next to a 5 mm scale bar while quoting
    # the WHOLE part's 120 mm, and the unqualified word reads as the size of what is in frame.
    dims = f"part bbox {size_mm[0]:.4g} x {size_mm[1]:.4g} x {size_mm[2]:.4g} mm"
    # A perspective bar is true only at the look-at depth; say so in the image itself.
    label = f"{mm:.4g} mm" + (" @ target" if frame.get("projection") == "perspective" else "")

    def _tb(text: str) -> tuple:
        try:
            return tuple(int(v) for v in draw.textbbox((0, 0), text, font=font))
        except Exception:  # noqa: BLE001
            return (0, 0, 7 * len(text), 12)

    tl, td = _tb(label), _tb(dims)
    th = max(tl[3] - tl[1], td[3] - td[1], 10)
    pad = 5
    right, bottom = W - 4, H - 4
    row2 = bottom - pad - th            # top of the bar row
    row1 = row2 - 4 - th                # top of the bbox row
    x1 = right - pad - 2
    x0 = x1 - px
    lx = x0 - 8 - (tl[2] - tl[0])
    dx = right - pad - (td[2] - td[0])
    rect = (max(0, min(lx, dx) - pad), row1 - pad, right, bottom)
    return {"mm": mm, "rect": rect, "bar": (x0, x1, row2 + th // 2),
            "label": (lx - tl[0], row2 - tl[1], label),
            "dims": (dx - td[0], row1 - td[1], dims)}


def _draw_scale_bar(draw, font, frame: dict, size_mm: tuple, W: int, H: int,
                    layout: Optional[dict] = None) -> float:
    """Bottom-right scale bar + exact bbox readout, so the image itself carries scale.

    Returns the bar's length in mm (also reported in the caption)."""
    lay = layout or _scale_bar_layout(draw, font, frame, size_mm, W, H)
    draw.rectangle(list(lay["rect"]), fill=(24, 26, 32))
    x0, x1, y = lay["bar"]
    draw.line([(x0, y), (x1, y)], fill=(240, 242, 248), width=2)
    for x in (x0, x1):
        draw.line([(x, y - 4), (x, y + 4)], fill=(240, 242, 248), width=2)
    lx, ly, label = lay["label"]
    draw.text((lx, ly), label, font=font, fill=(240, 242, 248))
    dx, dy, dims = lay["dims"]
    draw.text((dx, dy), dims, font=font, fill=(196, 202, 214))
    return lay["mm"]


# ---------------------------------------------------------------- load vectors on a figure
# A boundary-condition figure that paints only WHICH faces are loaded cannot tell an axial
# 136 N from a radial 980 N: two studies sharing a face pair render byte-identical, which is
# indistinguishable from a copy-paste mistake in an evidence folder. The load VECTOR is drawn.
_ARROW_MIN_FRAC = 0.075   # shortest arrow (smallest entry), as a fraction of min(W, H)
_ARROW_MAX_FRAC = 0.22    # longest arrow (the largest-magnitude entry)
_ARROW_AXIAL_TOL = 0.12   # projected/world length below this = the vector points at the eye


def _draw_load_arrows(draw, font, arrows: list, frame: dict, W: int, H: int,
                      taken: list) -> list:
    """One arrow per load entry, reprojected through `frame` — the camera every other
    overlay uses — so the arrow lands on the face it loads.

    `arrows` is [{"at": world xyz anchor, "vec": world force, "mag": N, "rel": 0..1,
    "rgb": (r,g,b), "label": str|None}]. The arrow flies INTO its anchor (tail upstream
    along the force direction, tip ON the face centroid) so the paint under the tip stays
    readable, and its length interpolates `rel` between a legible floor and a frame-relative
    ceiling — the largest entry is the longest arrow, and equal magnitudes draw equal.
    A vector within a few degrees of the view axis projects to nothing; that entry gets the
    head-on glyph a drawing uses (ringed dot coming at the reader, ringed cross going away)
    instead of a one-pixel smudge. Returns a record per drawn entry for the caption."""
    out: list = []
    span = float(min(W, H))
    lo, hi = _ARROW_MIN_FRAC * span, _ARROW_MAX_FRAC * span
    for a in arrows or []:
        v = _v3(a.get("vec"))
        if math.sqrt(sum(c * c for c in v)) < 1e-12:
            continue
        u = _vnorm(v)
        at = _v3(a.get("at"))
        S = _screen_xyz([at, [at[i] + u[i] for i in range(3)]], frame)
        tipx, tipy = float(S[0, 0]), float(S[0, 1])
        dx, dy = float(S[1, 0] - S[0, 0]), float(S[1, 1] - S[0, 1])
        dz = float(S[1, 2] - S[0, 2])
        plen = math.hypot(dx, dy)
        L = lo + (hi - lo) * max(0.0, min(1.0, float(a.get("rel", 1.0))))
        rgb = tuple(int(c) for c in a["rgb"])
        rec = {"label": a.get("label"), "mag": a.get("mag"), "head_on": False,
               "tip": (int(round(tipx)), int(round(tipy)))}
        if plen < _ARROW_AXIAL_TOL * max(frame.get("scale", 1.0), 1e-9):
            r = max(7.0, 0.18 * L)
            draw.ellipse([tipx - r, tipy - r, tipx + r, tipy + r], outline=rgb, width=3)
            if dz > 0:                       # larger depth = nearer the camera
                q = r * 0.34
                draw.ellipse([tipx - q, tipy - q, tipx + q, tipy + q], fill=rgb)
            else:
                k = r * 0.62
                draw.line([(tipx - k, tipy - k), (tipx + k, tipy + k)], fill=rgb, width=3)
                draw.line([(tipx - k, tipy + k), (tipx + k, tipy - k)], fill=rgb, width=3)
            rec["head_on"] = True
            tail = (tipx, tipy - r - 4.0)
        else:
            ux, uy = dx / plen, dy / plen
            tail = (tipx - ux * L, tipy - uy * L)
            hl = max(8.0, min(20.0, 0.34 * L))
            hw = hl * 0.5
            bx, by = tipx - ux * hl, tipy - uy * hl
            draw.line([tail, (bx, by)], fill=rgb, width=4)
            draw.line([tail, (bx, by)], fill=(0, 0, 0), width=1)
            draw.polygon([(tipx, tipy), (bx - uy * hw, by + ux * hw),
                          (bx + uy * hw, by - ux * hw)], fill=rgb, outline=(0, 0, 0))
        rec["tail"] = (int(round(tail[0])), int(round(tail[1])))
        if a.get("label"):
            _place_label(draw, font, (max(2, min(int(tail[0]), W - 3)),
                                      max(2, min(int(tail[1]), H - 3))),
                         str(a["label"]), rgb, W, H, taken)
        out.append(rec)
    return out


def _draw_bottom_banner(draw, font, lines: list, W: int, H: int, rgb: tuple,
                        bottom: int) -> Optional[tuple]:
    """Burn `lines` into a dark full-width band whose BOTTOM edge sits at y=`bottom`.

    Numbers a figure states only in its caption are lost the moment the PNG is pasted into a
    report, so the load resultant is drawn INTO the pixels. `bottom` is the caller's, because
    the scale bar owns the last ~46 px of the frame. Returns the band rect (or None)."""
    if not lines:
        return None
    heights: list = []
    for t in lines:
        try:
            b = draw.textbbox((0, 0), t, font=font)
            heights.append(b[3] - b[1] + 5)
        except Exception:  # noqa: BLE001
            heights.append(17)
    y0 = max(0, int(bottom) - (sum(heights) + 9))
    rect = (0, y0, W, int(bottom))
    draw.rectangle(list(rect), fill=(18, 20, 26))
    draw.line([(0, y0), (W, y0)], fill=rgb, width=2)
    y = y0 + 5
    for t, h in zip(lines, heights):
        draw.text((8, y), t, font=font, fill=rgb)
        y += h
    return rect


_TOPO_CACHE: dict = {}            # doc_id -> (aicm_handle, _doc_topo_maps result)


def _doc_topo_maps(doc_id: str) -> dict:
    """{"face": [match,...], "edge": [match,...]} in doc_query enumeration order, so index i
    is topo id i+1 — the id the whole document is addressed by. On a multi-body doc that is
    NOT the AICM per-body face_id/edge_id; _mesh_topo_index does that join. Also gives
    sid -> topo lookups. Cached per revision the way _doc_mesh is (the tessellation handle
    carries it): one planner grid asks for these maps once per tile, and the doc_query pair
    costs ~40 ms on a dense part. Callers must treat the result as read-only."""
    try:
        handle = ((cb.call("doc_rebuild", {"doc_id": doc_id}) or {}).get("tess")
                  or {}).get("aicm_path") or ""
    except Exception:  # noqa: BLE001 — no revision to key on: never serve a stale map
        handle = ""
    cached = _TOPO_CACHE.get(doc_id)
    if handle and cached and cached[0] == handle:
        return cached[1]
    out = {"face": [], "edge": [], "face_sid": {}, "edge_sid": {}}
    complete = True
    for of in ("face", "edge"):
        try:
            ms = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": of}})
                  or {}).get("matches") or []
        except Exception:  # noqa: BLE001
            ms = []
            complete = False
        out[of] = ms
        out[of + "_sid"] = {str(m.get("sid")): i + 1 for i, m in enumerate(ms) if m.get("sid")}
    if handle and complete:
        _TOPO_CACHE.clear()       # one document at a time, like _MESH_CACHE
        _TOPO_CACHE[doc_id] = (handle, out)
    return out


def _feature_roster(doc_id: str) -> list:
    """[{id, name, type, face_topos}] for the LAST body — _face_attribution's `created` view
    reshaped for the render overlay, so a callout is drawn on the faces a feature actually
    made rather than on every face the kernel credited to it.

    Owned EDGES are deliberately dropped: a fuse re-creates the neighbour edges it splits and
    the kernel attributes them to the NEW feature, so an edge-derived anchor lands on unrelated
    geometry. A feature left owning no face of its own gets NO callout, rather than a callout
    parked on somebody else's face. The callout still anchors on the SMALLEST visible owned
    face — the most specific one."""
    feats, owner_map = _target_feature_roster(doc_id)
    att = _face_attribution(doc_id, feats, owner_map)
    sid_topo = {id(m): i + 1 for i, m in enumerate(att["faces"])}
    out = []
    for f in feats:
        if f.get("suppressed"):
            continue
        fid = f.get("id")
        topos = {sid_topo[id(m)] for m in (att["created"].get(fid) or [])}
        if not topos:
            continue
        out.append({"id": fid, "name": f.get("name") or f.get("type") or fid,
                    "type": f.get("type"), "face_topos": topos})
    return out


def _resolve_highlight_specs(doc_id: str, highlight) -> list:
    """Resolve the `highlight` argument to [{label, matches, kind, colour, colour_name}].

    Accepts the batch target vocabulary: a feature name ("Boss"), a feature id ("f2"), a sid
    ("F58db6d"), a geometry-query dict, or a list of any of those — all through the one
    resolver every other tool uses (_resolve_inspect_target). An unresolvable entry is
    reported in the returned record instead of failing the render."""
    if highlight is None:
        return []
    specs = highlight if isinstance(highlight, list) else [highlight]
    out = []
    for i, sp in enumerate(specs):
        rgb, cname = _HIGHLIGHT_PALETTE[i % len(_HIGHLIGHT_PALETTE)]
        rec = {"spec": sp, "colour": rgb, "colour_name": cname,
               "matches": [], "label": str(sp), "kind": None, "error": None}
        try:
            rsl = _resolve_inspect_target(doc_id, sp)
            rec["matches"] = list(rsl.get("matches") or [])
            rec["kind"] = rsl.get("kind")
            name = rsl.get("feature_name")
            fid = rsl.get("feature_id")
            if fid and name:
                rec["label"] = f"{fid} {name}"
            elif fid:
                rec["label"] = str(fid)
            else:
                rec["label"] = str(rsl.get("label") or sp)
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
        out.append(rec)
    return out


def _annotate_render(src_png: str, doc_id: str, view_dir: tuple,
                     highlight=None, callouts: bool = True, edges: bool = True,
                     scale: bool = True, frame: Optional[dict] = None,
                     out_png: Optional[str] = None,
                     emphasis: Optional[dict] = None,
                     arrows: Optional[list] = None,
                     banner: Optional[list] = None,
                     face_tags: Optional[dict] = None,
                     reserved: Optional[list] = None,
                     tint: bool = False) -> tuple:
    """Overlay B-rep edges, highlights, feature callouts and a scale bar onto a render.

    src_png MUST be the engine render taken along `view_dir` (this reprojects the SAME
    camera). Pass `frame` — the engine's own projection record from _render_view_png — whenever
    you have it; it is required for a FOCUSED close-up, where the Python guess is meaningless.

    `emphasis` paints visible target faces in their palette with shading preserved,
    desaturates context, and reports measured target coverage. `xray=True` renders
    the occluding shell at 35% opacity and re-rasterizes target triangles in front
    of it, opaque with their own surface normals/depth and solid/dashed B-rep edges.
    Without x-ray, hidden target pixels are not painted through opaque context.

    `arrows` draws world force/direction vectors through the SAME reprojection
    (_draw_load_arrows), and `banner` burns caption lines into a band at the bottom of the
    frame (_draw_bottom_banner), so a figure carries its own numbers into a report.

    `face_tags` ({"max", "priority": {topo: rank}, "min_px"}) draws SID tags on the most
    important visible faces (_draw_face_tags); info["tags"] lists only chips actually drawn.
    Chips avoid emphasized target pixels and removed-half ghosts. Every SID chip anchors on
    its own winning depth/x-ray pixels; hidden faces get none, and a highlighted SID replaces
    its plain chip. `reserved` rects (x0, y0, x1, y1) are kept clear of every chip.
    info["seen_topos"] lists doc_query topo ids with at least one opaque-render visible pixel
    (None when the depth pass did not run).

    `tint` blends the engine's orientation hues into principal planar faces
    (_apply_orientation_tint) — for an exact-camera render taken with annotate=False, which
    has no tints of its own; info["tinted_px"] counts the pixels.

    Returns (path, info) where `info` feeds _render_annotation_caption. On any failure the
    ORIGINAL png path is returned with info["available"] False — the caller's render is never
    lost to an overlay problem."""
    info: dict = {"available": False, "edges": False, "hlr": False, "highlights": [],
                  "callouts": [], "callouts_omitted": [], "hidden_features": [],
                  "hidden_faces": 0, "scale_bar_mm": None, "bbox_mm": None,
                  "note": None, "part_name": None, "emphasis": None, "arrows": [],
                  "tags": [], "seen_topos": None}
    try:
        import numpy as np  # noqa: PLC0415
        from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        info["note"] = f"overlay unavailable ({e})"
        return src_png, info

    hl_specs = _resolve_highlight_specs(doc_id, highlight)
    mesh = _doc_mesh(doc_id)
    if mesh is None:
        info["note"] = "tessellation unavailable — no edge outline, highlight or callouts"
        info["highlights"] = [{"label": h["label"], "colour_name": h["colour_name"],
                               "sids": [], "error": h.get("error") or "no mesh"}
                              for h in hl_specs]
        # The load vector is not a mesh overlay: with the engine's own frame in hand it can
        # still be drawn, so a setup figure never degrades to one that hides its own load.
        if frame and (arrows or banner):
            try:
                with PILImage.open(src_png) as im0:
                    im = im0.convert("RGB")
                draw = ImageDraw.Draw(im)
                W, H = im.size
                fnt = _annot_font(13)
                band = _draw_bottom_banner(draw, fnt, banner, W, H,
                                           tuple((arrows or [{}])[0].get("rgb")
                                                 or (240, 242, 248)), H - 4)
                info["arrows"] = _draw_load_arrows(draw, fnt, arrows, frame, W, H,
                                                   [band] if band else [])
                out_png = out_png or _tmp(".png")
                im.save(out_png, format="PNG")
                return out_png, info
            except Exception as e:  # noqa: BLE001
                info["note"] += f"; load overlay failed ({type(e).__name__}: {e})"
        return src_png, info

    try:
        with PILImage.open(src_png) as im0:
            im = im0.convert("RGB")
        W, H = im.size
        frame = frame or _mesh_render_frame(mesh, view_dir, W, H)
        lo = mesh["pos"].min(axis=0); hi = mesh["pos"].max(axis=0)
        info["bbox_mm"] = tuple(float(x) for x in (hi - lo))

        buf = _mesh_depth_buffer(mesh, frame)
        if buf is None:
            info["note"] = (f"edge outline skipped — mesh over the depth budget "
                            f"({mesh['idx'].shape[0]} triangles > {_HLR_TRI_BUDGET})")
            Z = FI = None
        else:
            Z, FI, _S = buf
            info["hlr"] = True

        arr = np.asarray(im, dtype=np.float64)
        if tint and FI is not None:
            info["tinted_px"] = _apply_orientation_tint(arr, FI, mesh)
        topo = _doc_topo_maps(doc_id)
        face_by_topo, edge_by_topo = _mesh_topo_index(mesh)

        def _topos_for(matches: list) -> tuple:
            ftop, etop = set(), set()
            for m in matches or []:
                sid = str(m.get("sid") or "")
                if sid in topo["face_sid"]:
                    ftop.add(topo["face_sid"][sid])
                elif sid in topo["edge_sid"]:
                    etop.add(topo["edge_sid"][sid])
            return ftop, etop

        # Exact visible masks; an explicit x-ray uses a second target-only depth pass
        # so hidden surfaces retain their own material shading rather than taking on
        # the occluder's grey luminance.
        emp_draw: list = []
        emp_ghost = None
        xray_depth = None
        xray_face_idx: set = set()
        emphasis_mask = None
        tag_faces = FI
        if emphasis:
            emp_matches = list(emphasis.get("matches") or [])
            emp_mode = emphasis.get("mode") or "emphasis"
            palette = emphasis.get("palette") or _build_emphasis_palette(emp_matches, emp_mode)
            rec = {"mode": palette.get("mode"), "palette": palette,
                   "label": emphasis.get("label"), "target_px": 0,
                   "frame_px": int(W * H), "coverage": 0.0,
                   "visible_sids": [], "hidden_sids": [], "edge_sids": []}
            if FI is not None:
                lum = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]) / 255.0
                target_any = np.zeros((H, W), dtype=bool)
                per_target = []
                all_fidx: set = set()
                for i, m in enumerate(emp_matches):
                    ftop, etop = _topos_for([m])
                    fidx = [face_by_topo[t] for t in sorted(ftop) if t in face_by_topo]
                    eidx = [edge_by_topo[t] for t in sorted(etop) if t in edge_by_topo]
                    entry = (palette["entries"][i] if i < len(palette["entries"])
                             else palette["entries"][-1] if palette["entries"] else None)
                    rgb = tuple(entry["rgb"]) if entry else _EMPHASIS_PALETTE[0]
                    all_fidx.update(fidx)
                    mask = (np.isin(FI, np.asarray(fidx, dtype=np.int32)) if fidx
                            else np.zeros((H, W), dtype=bool))
                    sid = str(m.get("sid") or f"m{i}")
                    if mask.any():
                        target_any |= mask
                        rec["visible_sids"].append(sid)
                    elif eidx:
                        rec["edge_sids"].append(sid)
                    else:
                        rec["hidden_sids"].append(sid)
                    per_target.append({"sid": sid, "mask": mask if mask.any() else None,
                                       "edge_idx": eidx, "rgb": rgb,
                                       "label": sid})
                emphasis_mask = target_any
                xray = bool(emphasis.get("xray")) and not frame.get("section")
                # In x-ray mode draw the occluding shell as a pale, desaturated
                # translucent skin over the background, not a red blend through grey.
                # The target's own z-buffer wins every pixel of its silhouette.
                ctx = ~target_any
                g = (0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2])
                for ch in range(3):
                    arr[..., ch] = np.where(
                        ctx, g * (1.0 - _CONTEXT_DESAT) + arr[..., ch] * _CONTEXT_DESAT,
                        arr[..., ch])
                if xray:
                    skin = (FI >= 0) & ctx
                    arr[skin] = arr[skin] * 0.35 + 245.0 * 0.65
                for t in per_target:
                    if t["mask"] is None:
                        continue
                    shade = 0.45 + 0.85 * lum[t["mask"]]
                    for ch in range(3):
                        arr[..., ch][t["mask"]] = np.clip(t["rgb"][ch] * shade, 0, 255)
                if all_fidx:
                    tsel = np.isin(mesh["tri_face"], np.asarray(sorted(all_fidx), dtype=np.int32))
                    tri_ids = np.flatnonzero(tsel)
                    target_mesh = {"pos": mesh["pos"], "idx": mesh["idx"][tsel],
                                   "tri_face": np.arange(len(tri_ids), dtype=np.int32)}
                    tbuf = _mesh_depth_buffer(target_mesh, frame) if tsel.any() else None
                    if tbuf is not None:
                        ghost = (tbuf[1] >= 0) & ~target_any
                        if ghost.any():
                            if xray:
                                # The winning target triangle supplies real surface
                                # normals/depth, including a distinct bore floor. Its
                                # material is opaque: the skin must never muddy the colour.
                                p = mesh["pos"][mesh["idx"][tri_ids]]
                                n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
                                n /= np.maximum(np.linalg.norm(n, axis=1)[:, None], 1e-12)
                                light = np.asarray((0.4, 0.5, 1.0))
                                light /= np.linalg.norm(light)
                                shades = 0.5 + 0.5 * np.abs(n @ light)
                                grgb = np.asarray(per_target[0]["rgb"] if per_target
                                                  else _EMPHASIS_PALETTE[0], dtype=np.float64)
                                entire = tbuf[1] >= 0
                                arr[entire] = grgb * shades[tbuf[1][entire], None]
                                emp_ghost = ghost
                                rec["xray"] = True
                        n_ghost = int(ghost.sum())
                        rec["occluded_px"] = n_ghost
                        rec["occluded_frac"] = round(
                            n_ghost / float(max(n_ghost + int(target_any.sum()), 1)), 4)
                        if xray:
                            entire = tbuf[1] >= 0
                            emphasis_mask = entire
                            # This pass stores target-local triangle IDs for shading.
                            # All face labels need the global face of the winning triangle.
                            tag_faces = FI.copy()
                            tag_faces[entire] = mesh["tri_face"][tri_ids[tbuf[1][entire]]]
                            rec["target_px"] = int((tbuf[1] >= 0).sum())
                            rec["coverage"] = round(rec["target_px"] / float(W * H), 4)
                            xray_depth = tbuf[0]
                            xray_face_idx = all_fidx
                if not xray:
                    rec["target_px"] = int(target_any.sum())
                    rec["coverage"] = round(rec["target_px"] / float(W * H), 4)
                emp_draw = per_target
            else:
                rec["note"] = "mesh over the depth budget — emphasis mask unavailable"
            info["emphasis"] = rec
        sid_labels: dict = {}
        for h in hl_specs:
            t = topo["face_sid"].get(str(h["label"]))
            if t is not None:
                sid_labels.setdefault(t, {"text": f"* {h['label']}", "rgb": h["colour"]})
        drawn_sids: set = set()

        # ---- highlight fills (tint the exact visible pixels of each target face) --------
        hl_draw: list = []
        for h in hl_specs:
            ftop, etop = _topos_for(h["matches"])
            fidx = [face_by_topo[t] for t in sorted(ftop) if t in face_by_topo]
            mask = None
            if tag_faces is not None and fidx:
                mask = np.isin(tag_faces, np.asarray(fidx, dtype=np.int32))
                if mask.any():
                    rgb = np.asarray(h["colour"], dtype=np.float64)
                    arr[mask] = arr[mask] * 0.42 + rgb * 0.58
                else:
                    mask = None
            hl_draw.append({"rec": h, "mask": mask, "edge_idx": [edge_by_topo[t]
                                                                for t in sorted(etop)
                                                                if t in edge_by_topo],
                            "face_idx": fidx})

        im = PILImage.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        draw = ImageDraw.Draw(im)
        font = _annot_font(13)

        # ---- B-rep edge outline (depth-tested: real hidden-line removal) ---------------
        if edges and Z is not None:
            zr = Z[Z > -1e29]
            span = float(zr.max() - zr.min()) if zr.size else 1.0
            eps = max(1e-9, span * 0.004)
            Zref = _edge_depth_ref(Z)
            hl_edge_ids = {i for h in hl_draw for i in h["edge_idx"]}
            hl_edge_ids |= {i for t in emp_draw for i in t["edge_idx"]}
            n_runs = 0
            for ei, e in enumerate(mesh["edges"]):
                if ei in hl_edge_ids:
                    continue
                for run in _visible_runs(e["pts"], frame, Zref, eps):
                    draw.line(run, fill=(112, 125, 139) if (emphasis and emphasis.get("xray"))
                              else _EDGE_RGB, width=1)
                    n_runs += 1
            info["edges"] = n_runs > 0
            if xray_depth is not None and xray_face_idx:
                # Target boundaries compare to its OWN depth, not opaque occluder depth.
                target_tris = mesh["idx"][np.isin(mesh["tri_face"], list(xray_face_idx))]
                target_vertices = {tuple(p) for p in
                                   np.round(mesh["pos"][np.unique(target_tris)], 4)}
                target_zref = _edge_depth_ref(xray_depth)
                covered = xray_depth[xray_depth > -1e29]
                target_eps = max(eps, float(np.ptp(covered)) * 0.004) if covered.size else eps
                for e in mesh["edges"]:
                    pts = e["pts"]
                    if (tuple(np.round(pts[0], 4)) not in target_vertices or
                            tuple(np.round(pts[-1], 4)) not in target_vertices):
                        continue
                    for run in _visible_runs(pts, frame, target_zref, target_eps):
                        draw.line(run, fill=(105, 19, 30), width=2)
                    for run in _visible_runs(pts, frame, target_zref, target_eps, hidden=True):
                        for q in range(0, len(run) - 1, 4):
                            draw.line(run[q:q + 3], fill=(180, 86, 91), width=1)
            # Highlighted EDGE targets are drawn last, fat, in their reserved colour.
            for h in hl_draw:
                for ei in h["edge_idx"]:
                    for run in _visible_runs(mesh["edges"][ei]["pts"], frame, Zref, eps):
                        draw.line(run, fill=h["rec"]["colour"], width=3)
            # An emphasized EDGE target (a fillet edge, a chamfer) has no face mask to paint —
            # stroke it fat in its palette colour instead so it is unmissable.
            for t in emp_draw:
                for ei in t["edge_idx"]:
                    for run in _visible_runs(mesh["edges"][ei]["pts"], frame, Zref, eps):
                        draw.line(run, fill=tuple(t["rgb"]), width=4)

        # ---- emphasis outlines + leader labels ----------------------------------------
        # Drawn before the highlight chips so an explicit `highlight` still wins the top layer.
        emp_taken: list = [tuple(r) for r in (reserved or [])]
        # The scale plate is drawn last but laid out now, so every chip routes around it.
        bar = (_scale_bar_layout(draw, _annot_font(12), frame, info["bbox_mm"], W, H)
               if scale and info["bbox_mm"] else None)
        if bar is not None:
            emp_taken.append(bar["rect"])
        if emp_ghost is not None:
            _mask_outline(draw, emp_ghost, (140, 26, 36) if (emphasis and emphasis.get("xray"))
                          else (150, 150, 160), width=1)
        # Shared-colour emphasis names the TARGET once (on its largest visible face); a chip
        # per sid ("Fc51694", "F3bae34") only says something in per_face mode, where each
        # colour needs its key.
        for t in emp_draw:
            if tag_faces is not None and t["sid"] in topo["face_sid"]:
                fi = face_by_topo.get(topo["face_sid"][t["sid"]])
                winning = tag_faces == fi if fi is not None else None
                t["mask"] = winning if winning is not None and winning.any() else None
        one_label = (emphasis or {}).get("label") if (
            emphasis and (emphasis.get("mode") or "emphasis") == "emphasis") else None
        if one_label:
            sized = [t for t in emp_draw if t["mask"] is not None]
            biggest = max(sized, key=lambda t: int(t["mask"].sum())) if sized else None
        for t in emp_draw:
            if t["mask"] is not None:
                _mask_outline(draw, t["mask"], (140, 26, 36) if (emphasis and
                              emphasis.get("xray")) else _EMPHASIS_GLOW,
                              width=1 if (emphasis and emphasis.get("xray")) else 2)
            if emphasis.get("draw_label") is False:
                continue   # the caller places its own label clear of the target
            sid_topo = topo["face_sid"].get(t["sid"])
            if sid_topo is not None and (face_tags or sid_topo in sid_labels):
                continue  # SID chips are drawn once; highlighted form takes precedence.
            if one_label and t is not biggest and not (biggest is None and t is emp_draw[0]):
                continue
            anchor = _largest_cluster_anchor(t["mask"]) if t["mask"] is not None else None
            if anchor is None and t["edge_idx"]:
                pts = mesh["edges"][t["edge_idx"][0]]["pts"]
                S = _screen_xyz(pts, frame)
                mid = S[len(S) // 2]
                if np.isfinite(mid[0]) and np.isfinite(mid[1]):
                    anchor = (int(mid[0]), int(mid[1]))
            if anchor is None:
                continue
            ax, ay = anchor
            text = str(one_label or t["label"])
            if text in drawn_sids:
                continue
            if _place_label(draw, font, (ax, ay), text, tuple(t["rgb"]),
                            W, H, emp_taken) is not None and text in topo["face_sid"]:
                drawn_sids.add(text)

        # ---- load vectors + burned-in banner -------------------------------------------
        # After the emphasis paint (an arrow the desaturation pass could grey out is no
        # arrow) and before the highlight/callout chips, which then route around both.
        band = _draw_bottom_banner(draw, _annot_font(12), banner, W, H,
                                   tuple((arrows or [{}])[0].get("rgb") or (240, 242, 248)),
                                   bar["rect"][1] - 2 if bar is not None else H - 4)
        if band:
            emp_taken.append(band)
        info["arrows"] = _draw_load_arrows(draw, font, arrows, frame, W, H, emp_taken)

        # ---- highlight outlines + leader labels ---------------------------------------
        taken: list = emp_taken   # one registry, so a chip never lands on an emphasis label
        for h in hl_draw:
            rec = h["rec"]
            sids = [str(m.get("sid")) for m in (rec["matches"] or []) if m.get("sid")]
            entry = {"label": rec["label"], "colour_name": rec["colour_name"],
                     "rgb": list(rec["colour"]), "sids": sids[:8],
                     "n_subshapes": len(rec["matches"] or []),
                     "error": rec.get("error"), "visible": False}
            anchor = None
            if h["mask"] is not None:
                _mask_outline(draw, h["mask"], rec["colour"], width=2)
                anchor = _largest_cluster_anchor(h["mask"])
            if anchor is None and h["edge_idx"]:
                pts = mesh["edges"][h["edge_idx"][0]]["pts"]
                mid = _screen_xyz(pts, frame)[len(pts) // 2]
                if np.isfinite(mid[0]) and np.isfinite(mid[1]):   # perspective: behind near
                    anchor = (int(mid[0]), int(mid[1]))
            sid = str(rec["label"])
            defer_sid = bool(face_tags) and sid in topo["face_sid"]
            if anchor is not None and not defer_sid and sid not in drawn_sids:
                if _place_label(draw, font, anchor, f"* {sid}", rec["colour"],
                                W, H, taken) is not None:
                    if sid in topo["face_sid"]:
                        drawn_sids.add(sid)
            entry["visible"] = h["mask"] is not None or bool(h["edge_idx"])
            info["highlights"].append(entry)

        # Keep SID chips off both emphasized surfaces and removed-half context ghosts.
        if face_tags and FI is not None:
            forbidden = emphasis_mask
            if frame.get("section"):
                opposite = dict(frame)
                plane = frame["section"]
                opposite["section"] = {
                    "point": plane["point"], "normal": [-x for x in plane["normal"]]}
                removed = _mesh_depth_buffer(mesh, opposite)
                if removed is not None:
                    ghost_mask = removed[1] >= 0
                    forbidden = (ghost_mask if forbidden is None else forbidden | ghost_mask)
            tag_spec = dict(face_tags, labels=sid_labels)
            info["tags"] = _draw_face_tags(draw, _annot_font(12), tag_faces, mesh, face_by_topo,
                                           topo, tag_spec, W, H, taken, forbidden)

        # ---- feature callouts (id + name on a leader, largest-first, capped) -----------
        # A feature that already carries a highlight chip — or IS the emphasis target — is
        # skipped: two chips naming the same thing in two colours is worse than one.
        if callouts and FI is not None:
            labelled = {str(h["label"]) for h in info["highlights"]}
            if info.get("emphasis") and info["emphasis"].get("label"):
                labelled.add(str(info["emphasis"]["label"]))
            scored = []
            for f in _feature_roster(doc_id):
                if f"{f['id']} {f['name']}" in labelled or str(f["id"]) in labelled:
                    continue
                per_face = []
                for t in sorted(f["face_topos"]):
                    fi = face_by_topo.get(t)
                    if fi is None:
                        continue
                    n = int((FI == fi).sum())
                    if n:
                        per_face.append((n, fi))
                scored.append((sum(n for n, _ in per_face), f, per_face))
            scored.sort(key=lambda s: -s[0])
            floor = _CALLOUT_MIN_PIXEL_FRAC * W * H
            shown = 0
            for px, f, per_face in scored:
                tag = f"{f['id']} {f['name']}"
                if px < floor or not per_face:
                    info["hidden_features"].append(tag)
                    continue
                if shown >= _MAX_CALLOUTS:
                    info["callouts_omitted"].append(tag)
                    continue
                # Anchor on the SMALLEST owned face that still holds a real share of the
                # frame: after a boolean, owner_map hands a cut feature the big host faces it
                # punched through, so the largest owned face (or their centroid) would point
                # the "f3 Bore" label at the middle of the plate instead of at the hole.
                keep = max(40.0, px * 0.005)
                cand = [p for p in per_face if p[0] >= keep] or per_face
                anchor = _mask_anchor(FI == min(cand)[1])
                if anchor is None:
                    info["hidden_features"].append(tag)
                    continue
                ax = max(2, min(int(anchor[0]), W - 3))
                ay = max(2, min(int(anchor[1]), H - 3))
                _place_label(draw, font, (ax, ay), tag, _CALLOUT_RGB, W, H, taken)
                info["callouts"].append(tag)
                shown += 1

        # ---- what the camera cannot see ------------------------------------------------
        if FI is not None:
            seen = set(np.unique(FI).tolist()) - {-1}
            topo_of_face = {fi: t for t, fi in face_by_topo.items()}
            hidden = []
            for i in range(len(mesh["faces"])):
                t = topo_of_face.get(i)
                if i in seen or t is None:
                    continue
                m = topo["face"][t - 1] if t - 1 < len(topo["face"]) else {}
                tag = str(m.get("sid") or f"face#{t}")
                bits = [b for b in (m.get("side"), m.get("surf"), m.get("concavity")) if b]
                hidden.append(f"{tag}" + (f" ({'/'.join(bits)})" if bits else ""))
            info["hidden_faces"] = len(hidden)
            info["hidden_face_list"] = hidden[:8]
            info["seen_topos"] = sorted(t for t, fi in face_by_topo.items() if fi in seen)

        if bar is not None:
            info["scale_bar_mm"] = _draw_scale_bar(draw, _annot_font(12), frame,
                                                   info["bbox_mm"], W, H, layout=bar)

        out_png = out_png or _tmp(".png")
        im.save(out_png, format="PNG")
        info["available"] = True
        info["part_name"] = _handoff_doc_name(doc_id) or None
        return out_png, info
    except Exception as e:  # noqa: BLE001
        info["note"] = f"overlay failed ({type(e).__name__}: {e})"
        return src_png, info


def _render_annotation_caption(view_name: str, info: dict) -> str:
    """Self-sufficient caption for an annotated render: a model that reads ONLY this knows
    what part it is, its size, what is in frame, what is highlighted and in what colour, what
    the colours mean, what it CANNOT see from here and which tool to call for that."""
    if not info:
        return ""
    bits: list = []
    if info.get("bbox_mm"):
        s = info["bbox_mm"]
        name = info.get("part_name")
        bits.append(f"Annotated {view_name} view of "
                    + (f"part {name!r}" if name else "the part")
                    + f", bbox {s[0]:.4g} x {s[1]:.4g} x {s[2]:.4g} mm"
                    + (f" (scale bar in image = {info['scale_bar_mm']:.4g} mm)."
                       if info.get("scale_bar_mm") else "."))
    if info.get("edges"):
        bits.append("Dark outlines are the B-rep edges, depth-tested against the "
                    "tessellation, so hidden lines are removed and only edges you could "
                    "actually see from this camera are drawn.")
    elif info.get("note"):
        bits.append(f"No edge outline: {info['note']}.")
    hl = info.get("highlights") or []
    if hl:
        parts = []
        for h in hl:
            if h.get("error"):
                parts.append(f"{h['label']} could NOT be resolved ({h['error']})")
                continue
            sids = ", ".join(h.get("sids") or []) or "no sid"
            seen = "" if h.get("visible") else ", NOT visible from this camera"
            parts.append(f"{h['colour_name']} = {h['label']} "
                         f"[{h.get('n_subshapes', 0)} subshape(s): {sids}]{seen}")
        bits.append("HIGHLIGHTED: " + "; ".join(parts)
                    + ". Highlight colours (magenta/violet/hot-pink/purple/white) are "
                      "reserved for emphasis and are never produced by the side palette.")
    if info.get("callouts"):
        bits.append("Labelled features in frame: " + ", ".join(info["callouts"]) + ".")
    if info.get("callouts_omitted"):
        bits.append(f"{len(info['callouts_omitted'])} further visible feature(s) were NOT "
                    f"labelled to keep the image legible: "
                    + ", ".join(info["callouts_omitted"]) + ".")
    if info.get("bbox_mm"):
        bits.append("Colour key: " + _SIDE_PALETTE_LEGEND + ".")
    cannot = []
    if info.get("hidden_features"):
        cannot.append("features with nothing visible here: "
                      + ", ".join(info["hidden_features"]))
    if info.get("hidden_faces"):
        lst = ", ".join(info.get("hidden_face_list") or [])
        cannot.append(f"{info['hidden_faces']} B-rep face(s) are fully occluded or "
                      f"back-facing" + (f" ({lst})" if lst else ""))
    if cannot:
        where = "in ANY tile of this sheet" if "tiled-sheet" in view_name else "from this camera"
        bits.append(f"CANNOT BE SEEN {where} — " + "; ".join(cannot)
                    + ". For those call inspect_feature(target=...) for a framed close-up, "
                      "inspect_section(...) for a cut through internal geometry, or "
                      "describe_part() for the full text roster.")
    else:
        bits.append("Internal geometry is never visible in a shaded exterior view: use "
                    "inspect_section(...) or describe_part() to confirm bores and pockets.")
    return " ".join(bits)


def _target_is_internal(matches: list, part_bbox: dict) -> bool:
    """Heuristic: target is internal if it's a bore/hole, or its AABB is strictly inside
    the part AABB with margin (pocket/internal geometry the exterior camera can't see)."""
    if not matches or not part_bbox:
        return False
    for m in matches:
        if (m.get("concavity") or "").lower() in ("bore", "concave", "hole"):
            return True
        surf = (m.get("surf") or "").lower()
        if surf in ("cylinder", "cone") and m.get("kind") == "face":
            # Cylindrical face alone isn't enough, but a bore-tagged cylinder is.
            if m.get("radius") is not None and m.get("area_or_len"):
                # Prefer explicit concavity; already handled above.
                pass
    # Geometry containment: target bbox fully inside part bbox with a surface margin.
    try:
        tmin = [1e30, 1e30, 1e30]
        tmax = [-1e30, -1e30, -1e30]
        any_c = False
        for m in matches:
            c = m.get("centroid")
            if not (isinstance(c, (list, tuple)) and len(c) >= 3):
                continue
            any_c = True
            for i in range(3):
                tmin[i] = min(tmin[i], float(c[i]))
                tmax[i] = max(tmax[i], float(c[i]))
        if not any_c:
            return False
        pmin = _v3(part_bbox.get("min"))
        pmax = _v3(part_bbox.get("max"))
        psize = _v3(part_bbox.get("size"), default=(
            pmax[0] - pmin[0], pmax[1] - pmin[1], pmax[2] - pmin[2]))
        margin = max(max(psize) * 0.08, 0.5)
        inside = all(
            tmin[i] > pmin[i] + margin and tmax[i] < pmax[i] - margin
            for i in range(3)
        )
        return bool(inside)
    except Exception:  # noqa: BLE001
        return False


def _auto_section_for_target(doc_id: str, matches: list, target_bbox: dict) -> dict:
    """Pick a section plane through the target and return eval_section_props + plane name.

    Reuses the existing eval_section_props machinery (inspect_section). Plane is the
    axis-aligned datum whose normal is closest to the dominant target extent (so the cut
    opens the largest face of the target AABB).
    """
    size = _v3(target_bbox.get("size"), default=(1, 1, 1))
    # Largest target extent axis → cut with a plane NORMAL to a perpendicular principal axis.
    # Prefer the plane whose normal aligns with the SMALLEST target size (cuts across the
    # feature's thin dimension) so internals open up.
    axes = [("right", 0), ("front", 1), ("top", 2)]  # plane name → axis index of its normal
    # Smallest size axis is the normal of the cutting plane.
    si = min(range(3), key=lambda i: size[i] if size[i] > 1e-9 else 1e9)
    plane = axes[si][0]
    # Override: if matches have a clear normal, cut perpendicular to it.
    for m in matches or []:
        n = m.get("normal") or m.get("axis")
        if isinstance(n, (list, tuple)) and len(n) >= 3:
            an = [abs(float(n[0])), abs(float(n[1])), abs(float(n[2]))]
            si = max(range(3), key=lambda i: an[i])
            plane = axes[si][0]
            break
    try:
        res = cb.call("eval_section_props", {"doc_id": doc_id, "plane": plane}) or {}
    except Exception as e:  # noqa: BLE001
        return {"plane": plane, "ok": False, "error": str(e)}
    area = res.get("area_mm2", 0.0)
    summary = (f"auto-section plane '{plane}' area={area:.4g} mm²"
               if area else f"auto-section plane '{plane}' produced empty cut")
    return {"plane": plane, "ok": True, "summary": summary, **res}


def _describe_part_excerpt(doc_id: str, max_chars: int = 900) -> str:
    """Best-effort describe_part outline excerpt for inspect_feature (no hard dep cycle)."""
    try:
        from services.mcp import gsl_tools as _gsl  # noqa: PLC0415
        d = _gsl._digest(doc_id)
        text = _gsl._outline_text(d)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"
        return text
    except Exception as e:  # noqa: BLE001
        # Fallback: compact tree + bbox.
        try:
            tree = cb.call("doc_tree", {"doc_id": doc_id}) or {}
            feats = ((tree.get("tree") or {}).get("features")) or []
            names = [f"{f.get('id')}:{f.get('type') or f.get('name')}"
                     for f in feats if isinstance(f, dict)][:12]
            bb = cb.call("doc_bbox", {"doc_id": doc_id}) or {}
            size = bb.get("size") or []
            dims = (f"{size[0]:.3g}×{size[1]:.3g}×{size[2]:.3g} mm"
                    if isinstance(size, (list, tuple)) and len(size) >= 3 else "?")
            return f"bbox {dims}; features: {', '.join(names) or '(none)'}"
        except Exception:  # noqa: BLE001
            return f"(describe_part unavailable: {e})"


def _normalize_view_list(views: Optional[list]) -> list:
    """Normalize render_views `views` arg to a list of named specs (default 6-ortho+iso)."""
    if not views:
        return list(_STANDARD_ORTHO_VIEWS)
    out = []
    for v in views:
        if isinstance(v, str) and v.strip():
            name = v.strip().lower()
            if name in ("isometric", "isometry"):
                name = "iso"
            out.append(name)
        elif isinstance(v, dict):
            out.append(v)
        else:
            out.append(v)
    return out or list(_STANDARD_ORTHO_VIEWS)


def _palette_caption_block(palette: Optional[dict]) -> str:
    if not palette:
        return ""
    return (f" Emphasis palette ({palette.get('mode')}): {palette.get('legend')}. "
            f"[palette_json={json.dumps({'mode': palette.get('mode'), 'entries': palette.get('entries'), 'glow_rgb': palette.get('glow_rgb')}, separators=(',', ':'))}]")


_EMPHASIS_COLOR_NAMES = {
    (255, 64, 64): "red",
    (64, 180, 255): "cyan",
    (255, 200, 40): "amber",
    (80, 220, 120): "green",
    (220, 100, 255): "magenta",
    (255, 140, 60): "orange",
    (100, 160, 255): "sky",
    (255, 100, 160): "pink",
    (180, 255, 80): "lime",
    (120, 220, 220): "teal",
}


def _emphasis_color_name(rgb) -> str:
    """Map an emphasis palette RGB triple to a short colour name for captions."""
    try:
        t = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except (TypeError, ValueError, IndexError):
        return "red"
    return _EMPHASIS_COLOR_NAMES.get(t, f"#{t[0]:02X}{t[1]:02X}{t[2]:02X}")


def already_resolved_selector(target: Any) -> Optional[dict]:
    """*target* when it is an ENGINE-produced topology selector, else None.

    A readback selector names one element outright and must go to eval_measure as-is.
    Routing it through the query resolver instead is how
    ``measure_between(a_query={"sid": "F1b1b80"})`` came back "matched 7 candidates":
    picks.cpp reads no `sid` predicate, so the query degenerated to every face.
    Only the COMPLETE shape passes here — eval_measure wants kind+centroid, and a bare
    ``{"sid": …}`` reached it as «measure ref kind '' unsupported». Completing a partial
    one needs the document, so that is ``resolve_readback_selector``'s job.
    """
    if not isinstance(target, dict):
        return None
    has_geom_pred = any(k in target for k in (
        "of", "side", "surf", "pick", "parallel", "near", "index", "count",
        "concave", "convex", "owner_feature"))
    if has_geom_pred:
        return None
    if target.get("kind") in ("face", "edge", "vertex") and target.get("centroid") is not None:
        return target
    return None


def resolve_readback_selector(doc_id: str, target: Any) -> Optional[dict]:
    """A selector carrying a `sid` -> the live match for it. None when there is no sid.

    query_geometry / describe_part hand out a stable sid per element and the docs tell the
    model to address that element by it, so the sid has to actually select. A sid that no
    longer exists is a real error (the tree moved under it), never a silent widening.
    """
    if not isinstance(target, dict):
        return None
    sid = target.get("sid")
    if not isinstance(sid, str) or not sid.strip():
        return None
    match = _sid_match(doc_id, sid.strip())
    if match is None:
        raise ValueError(
            f"no face or edge in this document carries the sid {sid!r} — a sid is stable "
            f"only while the geometry is: rebuild the tree and it moves. Re-run "
            f"query_geometry/describe_part and use a sid from THAT reply, or name the "
            f"element with a query ({_QVOCAB_HELP}).")
    return match


def _resolve_measure_target(doc_id: str, target: Union[str, dict]) -> tuple:
    """Keep owner-scoped picks intact; the native measure resolver binds live geometry.

    Readbacks are not rerouted through doc_query: doing that loses the body owner,
    and selecting the first SID match can bind an identical assembly instance.
    """
    if isinstance(target, str):
        label = target.strip()
        if _SID_RE.fullmatch(label):
            return {"sid": label}, label
        try:
            ref = _as_face_query(target)
        except ValueError:
            ref = _resolve_one_selector(doc_id, target)
    elif isinstance(target, dict):
        ref = target
        selector = target.get("selector", target)
        if not isinstance(selector, dict):
            raise ValueError("A body-scoped measure ref requires a selector object.")
        label = str(selector.get("sid") or selector.get("side") or selector.get("kind") or "ref")
    else:
        raise ValueError("A measure target must be a selector object or plain-English query.")
    if isinstance(ref, dict) and "selector" not in ref and not ref.get("sid"):
        if not ref.get("centroid"):
            ref = {"of": ref.get("of", ref.get("kind", "face")), **ref}
        if is_geom_query(ref):
            ref, _notes = normalize_geom_query(ref, where="measure()")
    return ref, label


def _reading_from_measure(mres: dict, label: str, color_name: str, rgb,
                          sid: Optional[str] = None) -> dict:
    """Pack one eval_measure result into a structured reading for the measure caption."""
    kind = (mres or {}).get("kind") or "unknown"
    value: Any = None
    unit: Optional[str] = None
    # Prefer diameter over radius when both present (circle edges).
    if kind == "radius" and mres.get("diameter") is not None:
        kind, value, unit = "diameter", mres.get("diameter"), "mm"
    elif kind == "radius":
        value, unit = mres.get("radius"), "mm"
    elif kind == "length":
        value, unit = mres.get("length"), "mm"
    elif kind == "area":
        value, unit = mres.get("area"), "mm^2"
    elif kind == "volume":
        value, unit = mres.get("volume"), "mm^3"
    elif kind == "point":
        pts = mres.get("points")
        value, unit = (pts[0] if isinstance(pts, list) and pts else pts), "mm"
    elif kind == "distance":
        value, unit = mres.get("distance"), "mm"
    elif kind == "connected_edges":
        value, unit = mres.get("total_length"), "mm"
    elif kind == "multi_distance":
        value, unit = mres.get("total_distance"), "mm"
    elif kind == "angle":
        value, unit = mres.get("angle"), "deg"
    elif kind == "error":
        value, unit = mres.get("error"), None
    else:
        # Fall through: first numeric field that looks like a reading.
        for k, u in (("diameter", "mm"), ("radius", "mm"), ("length", "mm"),
                     ("area", "mm^2"), ("distance", "mm"), ("volume", "mm^3"),
                     ("angle", "deg"), ("perimeter", "mm")):
            if mres.get(k) is not None:
                kind, value, unit = k, mres.get(k), u
                break

    out = {
        "label": label,
        "kind": kind,
        "value": value,
        "unit": unit,
        "color": color_name,
        "rgb": list(rgb) if rgb is not None else None,
    }
    if sid:
        out["sid"] = sid
    if mres.get("anchor") is not None:
        out["anchor"] = mres.get("anchor")
    if mres.get("polyline") is not None:
        out["polyline"] = mres.get("polyline")
    return out


def _between_from_batch(batch: dict) -> Optional[dict]:
    """Extract a pair/multi distance summary from a multi-ref eval_measure result."""
    if not isinstance(batch, dict):
        return None
    kind = batch.get("kind")
    if batch.get("distance") is not None:
        out = {
            "kind": kind or "distance",
            "value": batch.get("distance"),
            "unit": "mm",
            "distance": batch.get("distance"),
        }
        if batch.get("angle") is not None:
            out["angle"] = batch.get("angle")
        if batch.get("points") is not None:
            out["points"] = batch.get("points")
        if kind == "connected_edges":
            out["total_length"] = batch.get("total_length")
            out["segment_lengths"] = batch.get("segment_lengths")
            if out.get("value") is None and batch.get("total_length") is not None:
                out["value"] = batch.get("total_length")
        return out
    if kind == "multi_distance" or batch.get("total_distance") is not None:
        return {
            "kind": "multi_distance",
            "value": batch.get("total_distance"),
            "unit": "mm",
            "total_distance": batch.get("total_distance"),
            "distances": batch.get("distances"),
            "points": batch.get("points"),
        }
    if kind == "angle" and batch.get("angle") is not None:
        return {"kind": "angle", "value": batch.get("angle"), "unit": "deg",
                "angle": batch.get("angle")}
    return None





def _resolve_drawing_view_id(doc_id: str, view_id: str) -> str:
    """D7: Accept a numeric view-id (v1, v2, …) OR an orientation string ("front", "top", …).

    When the caller passes an orientation string we call drawing_get and find the first
    non-section/non-detail view whose 'orientation' field matches (case-insensitive).
    This mirrors how add_section_view's `parent` resolver already works, so ALL drawing
    annotation tools accept the same flexible identifier.

    Returns the resolved view_id string, or raises ValueError if no match is found.
    """
    if not view_id:
        raise ValueError("view_id must be a non-empty string")
    # If it already looks like a numeric view id (starts with 'v' + digits, or is all digits)
    # return it unchanged.
    if view_id.lower().startswith("v") and view_id[1:].isdigit():
        return view_id
    if view_id.isdigit():
        return view_id
    # Try to resolve as an orientation string.
    try:
        full = cb.call("drawing_get", {"doc_id": doc_id})
    except Exception:
        # Can't query the drawing — pass through and let the engine reject it.
        return view_id
    target = view_id.lower().strip()
    for sh in full.get("sheets", []):
        for v in sh.get("views", []):
            if v.get("type") in ("section", "detail"):
                continue
            if (v.get("orientation", "").lower() == target
                    or v.get("view_id", "") == view_id
                    or v.get("id", "") == view_id):
                return v.get("view_id") or v.get("id") or view_id
    # No match — return original so the engine surfaces a meaningful error.
    return view_id


def _resolve_named_one_selector(doc_id: str, target, kind: Optional[str]) -> dict:
    """A sid / feature id / feature NAME -> the ONE resolved face-or-edge match it designates.

    Shares _resolve_inspect_target's precedence (sid, then feature id, then feature name), so a
    handle that hover() accepts is a handle measuring accepts. Measuring is stricter in one way:
    it needs exactly ONE face/edge, while a FEATURE legitimately owns many (a hole owns its
    barrel + both rims). That is not an error to swallow — it is a pick, so the miss names the
    candidate sids and tells the model to pass one.
    """
    got = _resolve_inspect_target(doc_id, target)  # raises, teaching, on a genuine miss
    matches = [m for m in (got.get("matches") or []) if isinstance(m, dict)]
    if kind:
        matches = [m for m in matches if m.get("kind") == kind]
    label = got.get("feature_name") or got.get("feature_id") or got.get("label") or target
    if not matches:
        raise ValueError(
            f"{target!r} resolved to {label!r} but carries no {kind or 'face/edge'} to measure. "
            f"Call describe_part(doc_id) for the face/edge roster and pass one of its sids.")
    if len(matches) > 1:
        sids = [str(m.get("sid")) for m in matches if m.get("sid")][:8]
        raise ValueError(
            f"{target!r} names {label!r}, which owns {len(matches)} faces/edges — measuring "
            f"needs exactly one. Pass one of its sids"
            + (f" ({', '.join(sids)})" if sids else "")
            + ", or a query that singles one out (e.g. {\"of\":\"face\",\"pick\":\"largest\"}).")
    return matches[0]


def _resolve_one_selector(doc_id: str, query: Union[str, dict], kind: Optional[str] = None) -> dict:
    """Resolve a plain-English face/edge query to a SINGLE unambiguous topology selector dict.

    eval_measure (and friends) re-resolve their refs against the whole body, so they need a
    selector that pins down exactly ONE face/edge — the RESOLVED match dict that doc_query
    returns (it carries the centroid/axis/area_or_len the scorer keys on), not the raw
    plain-English predicates ({"side":"top"}). This runs doc_query, then:
      * 1 match  -> return it (unambiguous).
      * >1 match -> raise, because passing one of several tied matches would dangle on
                    re-resolution ("symmetric ambiguity"). The caller should add a predicate
                    (e.g. {"of":"edge","parallel":"z","pick":"largest"}) to single one out.
      * 0 match  -> raise.
    kind, when given ("face"/"edge"), filters the matches to that topology kind first.

    A string that is NOT query vocabulary is not automatically a mistake: describe_part and
    query_geometry both hand the model back stable sids ("Fedecf8"), and the feature tree hands
    back ids/names ("f2", "Boss") — all three name ONE face/edge, and both tools' own docstrings
    tell the model to look them up there first. Rejecting them broke that documented loop
    (measured live 2026-07-29: feeding query_geometry's own sid into measure_between raised
    "Can't read 'Fedecf8' as a geometry query"). Those spellings are resolved through
    _resolve_inspect_target — the SAME full-precedence resolver hover/inspect use — rather than
    growing a second grammar here.
    """
    if isinstance(query, dict):
        q = query
    else:
        try:
            q = _as_face_query(query)
        except ValueError:
            return _resolve_named_one_selector(doc_id, query, kind)
    res = cb.call("doc_query", {"doc_id": doc_id, "query": q})
    matches = res.get("matches", [])
    if kind:
        matches = [m for m in matches if m.get("kind") == kind]
    if not matches:
        raise ValueError(f"No {kind or 'geometry'} matched query {query!r} "
                         f"(use query_geometry to explore).")
    if len(matches) > 1:
        # The C++ eval_measure (eval_resolve_ref) is now predicate-aware and breaks ties
        # deterministically. If the query carries a TIE-BREAKING predicate (pick/index/
        # count/near), hand it the RAW query so eval_measure applies that predicate and
        # singles one out — e.g. pick:"longest" on a box's four equal vertical edges, which
        # doc_query leaves tied. Without such a predicate, keep the friendly "narrow it"
        # guidance rather than silently measuring one of several tied candidates.
        if isinstance(q, dict) and any(k in q for k in ("pick", "index", "count", "near")):
            return q
        raise ValueError(
            f"Query {query!r} matched {len(matches)} candidates, not one — measuring needs a "
            f"single face/edge. Narrow it with a predicate (e.g. add \"pick\":\"largest\", "
            f"\"side\":\"top\", or \"parallel\":\"z\"). query_geometry({query!r}) lists them.")
    return matches[0]


def _resolve_face_points(doc_id: str, query: Union[str, dict]) -> list:
    """Plain-English face query -> a list of {points:[...]} face-point sets (one per face)."""
    res = cb.call("doc_query", {"doc_id": doc_id, "query": _as_face_query(query)})
    matches = [m for m in res.get("matches", []) if m.get("kind") == "face"]
    if not matches:
        raise ValueError(f"No face matched query {query!r} (try query_geometry to explore).")
    out = []
    for sel in matches:
        fp = cb.call("doc_face_points", {"doc_id": doc_id, "selector": sel})
        if fp.get("points"):
            out.append(fp)
    if not out:
        raise ValueError(f"Query {query!r} matched faces but no surface points resolved.")
    return out


def _merge_face_sets(faces: list) -> dict:
    """One boundary/load over the union of the resolved CAD faces.

    A singleton keeps the terminal native encoding. Multiple faces retain each
    complete point cloud and its own tolerances, normal and analytic surface in
    ``facesets``; concatenating clouds loses heterogeneous oracle hints and can
    select geometry between the faces. Native membership/quadrature deduplicates
    the union, so the authored force is applied once, not once per child.
    """
    if not faces:
        raise ValueError("A simulation boundary requires at least one resolved face.")
    if len(faces) == 1:
        return dict(faces[0])
    return {"facesets": [dict(face) for face in faces]}


def _persist_study(doc_id: str, record: dict) -> Optional[str]:
    """Record a SOLVED simulation study (setup + RESULTS) into the document's `studies`
    array so it round-trips inside the native .step+ (save_part). Without this the FEA/CFD
    numbers were only ever returned to the caller and lost on save — the file claimed to
    carry "simulation studies + results" but never did. We store the human-readable SETUP
    (material, fixtures/loads as the original plain-English queries) plus the computed
    RESULTS, NOT the resolved point clouds, so the entry stays small and re-runnable.

    Re-running a study of the same `type` REPLACES the prior one (keyed by type+name) so
    repeated solves don't pile up duplicates. Best-effort: a persistence failure never
    breaks the solve that already succeeded — the numbers are still returned to the caller.
    """
    try:
        rid = record.get("id")
        name = record.get("name", "")
        rtype = record.get("type", "")
        existing = cb.call("sim_study_list", {"doc_id": doc_id}).get("studies", [])
        for s in existing:
            if not rid and s.get("type") == rtype and s.get("name") == name:
                rid = s.get("id")
                break
        if rid:
            record["id"] = rid
            res = cb.call("sim_study_edit", {
                "doc_id": doc_id, "study_id": rid, "study": record})
            # Editing a legacy colliding ID may assign this entry its own stable ID.
            rid = res.get("study_id", rid) if isinstance(res, dict) else rid
            record["id"] = rid
            return rid
        res = cb.call("sim_study_add", {"doc_id": doc_id, "study": record})
        return res.get("study_id") if isinstance(res, dict) else None
    except Exception:  # noqa: BLE001 — persistence is best-effort, never fail the solve
        return None


# --------------------------------------------------- thermal helpers

# Typical thermal conductivity (W/m·K) by material family — used by run_fea_thermal
# when neither the caller nor the material dict supplies one. Engineering nominals
# (clean-room, standard handbook values), only for the box-proxy conduction estimate.
_DEFAULT_CONDUCTIVITY_WMK: dict = {
    "steel": 50.0, "stainless": 16.0, "aluminum": 205.0, "titanium": 22.0,
    "brass": 109.0, "abs": 0.17, "pla": 0.13,
}


def _default_conductivity(mat: dict, material_arg: Any) -> float:
    """Best-effort thermal conductivity (W/m·K) for thermal studies."""
    name = str(mat.get("name", material_arg) or "").lower()
    for key, val in _DEFAULT_CONDUCTIVITY_WMK.items():
        if key in name:
            return val
    return 50.0  # mild-steel default


# Typical specific heat (J/(kg·K)) by material family — used by the TRANSIENT thermal
# path when neither the caller nor the material dict supplies cp. Same
# engineering-nominal spirit as the conductivity table above.
_DEFAULT_SPECIFIC_HEAT_JKGK: dict = {
    "steel": 490.0, "stainless": 500.0, "aluminum": 900.0, "titanium": 520.0,
    "brass": 380.0, "abs": 1400.0, "pla": 1800.0,
}


def _default_specific_heat(mat: dict, material_arg: Any) -> float:
    """Best-effort specific heat (J/kg·K) for the transient thermal solve."""
    name = str(mat.get("name", material_arg) or "").lower()
    for key, val in _DEFAULT_SPECIFIC_HEAT_JKGK.items():
        if key in name:
            return val
    return 490.0  # mild-steel default


def _first_temperature(faces: list, label: str) -> float:
    """Pull the temperature (°C) from the first face spec in a thermal face list."""
    for f in faces or []:
        if isinstance(f, dict) and "temperature" in f:
            return float(f["temperature"])
    raise ValueError(f"{label} needs at least one entry with a 'temperature' (°C), "
                     f"e.g. [{{\"face\": \"+x\", \"temperature\": 100}}]")


# ── geometry-query key guard ─────────────────────────────────────────────────
# core/doc/picks.cpp::resolve_geom_query reads the keys it knows and IGNORES the
# rest, so a predicate the engine has no concept of selects EVERYTHING silently.
# Measured 2026-08-04 on a 50 mm cube with one bore:
#   query_geometry({"of":"face","shape":"cylinder"})   -> 7 faces (shape is the
#       documented spelling of surf; the flat-kwargs path canonicalises it, the
#       dict path never did)
#   query_geometry({"of":"face","sid":"F1b1b80"})      -> 7 faces (a sid comes
#       straight out of the readback and does not narrow anything)
#   add_features(fillet, refs.edges=[{"of":"edge","sid":"NOPE_NOT_A_SID"}])
#       -> succeeded and rounded EVERY edge on the body.
# The last one is the reason this exists: a model asking for one edge got the
# whole part filleted, with no error anywhere. A key that does not select must
# say so.

#: Keys that actually NARROW a geometry query (picks.cpp predicates).
GEOM_QUERY_NARROWING_KEYS = frozenset({
    "side", "surf", "parallel", "perpendicular", "pick", "near", "index",
    "count", "body_id",
})
#: The domain keys — which topology the query runs over. They pick the pool,
#: they do not narrow it, so they never satisfy the "must select something" rule.
GEOM_QUERY_DOMAIN_KEYS = frozenset({"of", "kind"})
#: Spellings of a narrowing key. Same table the flat-kwargs path already applied,
#: now applied inside a query DICT too.
GEOM_QUERY_ALIASES = {
    "surface": "surf", "shape": "surf",
    "limit": "count", "max": "count", "max_results": "count",
    # A coordinate in a QUERY can only mean "the one nearest here" — the engine's `near`.
    # (`at` means PLACEMENT on a feature, but this runs on geometry queries only.)
    # Live claude-opus-5, 2026-08-04: refs.edges={"of":"edge","at":[…]} on a chamfer.
    "at": "near", "position": "near", "point": "near", "nearest": "near",
}
#: Fields query_geometry / describe_part put ON a match. Handing a whole match
#: object back as a query is a documented workflow, so they are tolerated — but
#: only alongside a real predicate, because on their own they select nothing.
GEOM_QUERY_READBACK_KEYS = frozenset({
    "sid", "centroid", "normal", "axis", "radius", "area_or_len", "concavity",
    "hint_index", "owner_feature", "body", "topo", "axis_point",
    "major_radius", "minor_radius", "torus_axis", "torus_center",
})

GEOM_QUERY_VOCAB_HELP = (
    'of ("face"/"edge"), side (top/bottom/left/right/front/back or ±x/±y/±z), '
    'surf (flat/round/cylindrical/conical/spherical/toroidal; edges: '
    'straight/round/circular/elliptical), parallel/perpendicular ("x"/"y"/"z"), '
    'pick (largest/smallest/longest/shortest/inner/outer), near [x,y,z], '
    "count, index"
)


def is_geom_query(value: Any) -> bool:
    """Mirror of core/doc/picks.cpp::is_geom_query — what the engine resolves as a query."""
    return isinstance(value, dict) and (value.get("kind") == "query" or "of" in value)


def normalize_geom_query(query: dict, *, where: str, defer_sid: bool = False) -> tuple:
    """Canonicalise a geometry-query dict's keys; reject one that cannot select.

    Returns ``(query, notes)``. Raises ``ValueError`` with the real vocabulary for
    an unknown key, and for a query whose only non-domain keys came off a readback
    (sid/centroid/…) — that query matches the entire body, which is never what the
    caller meant by naming one element.
    """
    out: dict = {}
    notes: list = []
    unknown: list = []
    for key, value in query.items():
        canon = GEOM_QUERY_ALIASES.get(key, key)
        if (canon not in GEOM_QUERY_NARROWING_KEYS
                and canon not in GEOM_QUERY_DOMAIN_KEYS
                and canon not in GEOM_QUERY_READBACK_KEYS):
            unknown.append(key)
            continue
        if canon != key:
            if canon in query and query[canon] != value:
                raise ValueError(
                    f"{where}: the query key {canon!r} arrived twice with different "
                    f"values ({query[canon]!r} as {canon!r}, {value!r} as {key!r}) — "
                    f"that is one key, so pass it once.")
            notes.append(f"{where}: read the query key {key!r} as {canon!r}")
        out[canon] = value
    if unknown:
        raise ValueError(
            f"{where}: the geometry query has no key(s) "
            + ", ".join(repr(k) for k in sorted(unknown))
            + " — the engine ignores a key it does not know, so this query would have "
              f"matched EVERY candidate instead of the one you named. Query keys: "
              f"{GEOM_QUERY_VOCAB_HELP}")
    readback = sorted(k for k in out if k in GEOM_QUERY_READBACK_KEYS)
    if defer_sid and isinstance(out.get("sid"), str) and _SID_RE.fullmatch(out["sid"].strip()):
        # add_features resolves a real sid against the live document (_resolve_sid_refs),
        # which both selects the right element and gives a better error for a stale one.
        # A value that is not sid-SHAPED is not a sid at all — it falls through to the
        # rejection below rather than being handed on to a resolver that will skip it.
        return out, notes
    if readback and not (set(out) & GEOM_QUERY_NARROWING_KEYS):
        names = ", ".join(repr(k) for k in readback)
        what = ("is a readback field, not a query predicate" if len(readback) == 1
                else "are readback fields, not query predicates")
        raise ValueError(
            f"{where}: {names} {what}. This query selects every candidate, not the element "
            f"you want. Narrow it with {GEOM_QUERY_VOCAB_HELP}. Or give back the full "
            "selector object that query_geometry or describe_part returned.")
    return out, notes
