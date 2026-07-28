"""Shared state + helpers for the mechanical-CAD MCP tool family modules (T-RF8 split
of the former cad_tools.py god file): doc registry/aliases, handoff publish, autosave +
recovery, background jobs, materials/appearance, inspect/measure/render helper machinery.
Imported by the cad_*_tools family modules; re-exported through the cad_tools facade."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from . import simhandoff as sh
from .core_bridge import CoreError

# The "current part" so single-argument calls just work (every tool also takes an explicit
# doc_id). Mirrors the old server's _STATE, but the doc lives in the C++ core, not here.
_CUR: dict = {"doc_id": None}


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
# (save_part / export_part to a file). The engine `dirty` flag is NOT a usable "undelivered
# work" signal here — the MCP autosave timer calls doc_save to a temp path, which clears dirty
# without the agent having delivered anything. Revision IS reliable: it's monotonic and bumped
# only by mutating ops (never by a save), so `current_revision > _LAST_DELIVER_REV[doc]` means
# "edited since the last real save". close_part uses it to nudge the agent to save its work.
_LAST_DELIVER_REV: dict = {}


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


def _face_hist(doc_id: str) -> dict:
    """GSL Stage 1a — a cheap topological snapshot for the add_features write-diff: count faces
    by shape (surf) and by bore/boss concavity (the Stage-0 semantics now in the read-back), so
    a build op can confirm WHAT it changed, not just that the volume moved. Best-effort: returns
    an empty hist on any error (the write-diff degrades to a volume-only signal)."""
    try:
        res = cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "face"}})
    except Exception:  # noqa: BLE001
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


def _write_diff(before: dict, after: dict, vol_before, vol_after) -> dict:
    """GSL Stage 1a (W2) — compose a SEMANTIC confirmation of what an op actually did, so the
    return is no longer a bare ok:true + a volume number the model must diff itself. Reports the
    net face change (by shape, and how many are bores/holes) and the volume delta with its sign;
    a no-op (a cut/boolean that missed the body, a feature that altered nothing) reads as an
    explicit 'no geometry change' warning instead of looking successful."""
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
            if abs(dv) >= 1e-6:
                parts.append(f"volume {vol_before:.1f}->{vol_after:.1f} mm^3 "
                             f"({'+' if dv >= 0 else ''}{dv:.1f})")
        elif vol_before is None and abs(vol_after) >= 1e-6:
            dv = float(vol_after)
            parts.append(f"volume now {vol_after:.1f} mm^3")
    if parts:
        summary = "; ".join(parts)
    else:
        summary = ("no geometry change - no faces added/removed and volume unchanged; the op "
                   "did not alter the body (a cut/boolean may have missed, or a boss/edit was a "
                   "no-op). Re-check the placement/size against the request.")
    out = {"summary": summary, "faces_delta": dface, "bores_delta": dbore}
    if dv is not None:
        out["volume_delta_mm3"] = round(dv, 4)
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
    offending value), the FULL open-document set, and the corrected call."""
    return DocRefError(
        f"{problem} Open parts: {_doc_listing(docs)}. {hint}",
        code=code,
        detail={"open_documents": [{"doc_id": e.get("doc_id"), "name": e.get("name") or ""}
                                   for e in docs[:_DOC_LIST_MAX]],
                "hint": hint})


def _docs_named(docs: list, value: str) -> list:
    """Open docs whose NAME is `value` — an exact match wins outright, else case-insensitive."""
    v = value.strip()
    exact = [e for e in docs if (e.get("name") or "") == v]
    if exact:
        return exact
    folded = v.casefold()
    return [e for e in docs if (e.get("name") or "").strip().casefold() == folded]


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


def _doc(doc_id: Optional[str]) -> str:
    # `_CUR` is a single PROCESS-WIDE "current part", and it is only ever set by a doc THIS
    # process created or opened. A part the human already had open in the GUI — or one another
    # client made — never touches it, so trusting `_CUR` alone reports "no current part" while
    # the part sits right there in the viewport. Always ask the engine what is actually open and
    # reconcile against it. The same listing is what resolves an imperfect supplied handle and
    # what every teaching error below quotes back.
    try:
        docs = [e for e in (cb.call("doc_list", {}) or {}).get("docs", [])
                if isinstance(e, dict) and e.get("doc_id")]
        listed = True
    except Exception:  # noqa: BLE001 — doc_list unavailable: fall back to legacy behavior
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
                      'becomes your default), or name the one you mean, e.g. '
                      f'doc_id="{docs[0].get("doc_id")}" — an open part\'s name works too.'))
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
        if not docs:
            raise _doc_error(
                f"no such document {raw!r}: ZERO documents are open in this engine session.",
                docs, code="doc_not_found",
                hint='call new_part(name="…") first, then pass the doc_id it returns.')
        near = _doc_near_misses(docs, raw)
        raise _doc_error(
            f"no such document {raw!r}: it matches no open document's id or name.",
            docs, code="doc_not_found",
            hint=(f'did you mean doc_id="{near[0].get("doc_id")}" (the part named '
                  f'"{near[0].get("name")}")?' if len(near) == 1 else
                  "pass one of the ids above, or the exact name of an open part."))

    # The current part is whatever THIS process last created or opened, and that claim is
    # authoritative however many other documents happen to be open. The app's untitled
    # "Part1" and another agent's part in another process are not ours: an omitted doc_id
    # cannot have meant either, so counting them was only ever a way to reject a call we
    # could already answer. A sub-agent sharing this process is scoped by `doc_scope()`,
    # so its part never becomes its parent's.
    d = _resolve_alias(_CUR["doc_id"]) if _CUR["doc_id"] else None
    if d and listed and d not in {e.get("doc_id") for e in docs}:
        # Our part is gone — closed in the app, or reaped by doc-GC. Holding the dead
        # handle only defers the failure to a confusing "no such document"; drop it and
        # resolve from what is actually open.
        _CUR["doc_id"] = None
        d = None
    if d:
        return d
    if len(docs) == 1:
        # Nothing claimed, one part open: adopt it, so every read/measure/edit tool acts on
        # the part the user is actually looking at instead of demanding a redundant
        # new_part().
        _CUR["doc_id"] = docs[0]["doc_id"]
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
    """Render an engine error as a structured, fixable result (not a transport failure)."""
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
        from mcp_server.gsl_tools import _scene_caption
        caption = _scene_caption(doc_id, views)
        return [Image(path=png), (caption + " " + tail).strip() if tail else caption]
    except Exception:  # noqa: BLE001
        if tail:
            return [Image(path=png), tail]
        return Image(path=png)


def _sanitize_output_path(path: str) -> str:
    """K3: Normalize an output path so callers see (and the engine writes to) the real,
    resolved absolute path.

    We canonicalise via os.path.abspath (which resolves '..' components and symlinks so
    path-traversal sequences like '../../etc/passwd' are fully collapsed).  We do NOT
    impose an allowlist root here — the MCP server runs under the user's own account so
    writing anywhere they have permission is intentional.  What we DO guarantee is that
    the returned path is fully resolved (no '..' components remain) so the caller always
    knows exactly where the file will land, and the engine can't be fooled by a crafted
    relative sequence that navigates outside an expected directory.

    Raises ValueError for obviously invalid inputs (empty string, non-string)."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("output path must be a non-empty string")
    return os.path.abspath(path)


# --- live viewport handoff -------------------------------------------------------------
# The MCP runs in its OWN process with its own engine, so the running desktop app can't see
# its doc sessions. To make the AI's part visible in the app's viewport, we publish it as a
# native .step+ doc plus a tiny live.json {rev, step} into <appdata>/<app>/handoff/; the GUI
# polls that control file each frame (ui::pollAiCadHandoff) and opens/refreshes one tab from it.
# <app> follows the T-G10 alias rule (Chudware, or the legacy Chudware dir when it already
# exists), matching platform::appdataRoot() so publisher and reader agree.
_PUB: dict = {"rev": None, "docs": {}, "active": None}


def _handoff_dir() -> Path:
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
        cb.call("doc_save", {"doc_id": doc_id, "path": str(step)})
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
    """True if `pid` is a live process on this machine. os.kill(pid, 0) is the POSIX probe:
    ESRCH -> dead, EPERM -> alive but not ours. Best-effort; assume alive if we can't tell."""
    if not pid or pid < 0:
        return True  # unknown owner (legacy fragment) — don't evict on liveness grounds
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001 — non-POSIX / can't tell -> fall back to mtime TTL
        return True


import atexit as _atexit  # noqa: E402


@_atexit.register
def _cleanup_handoff_fragment() -> None:
    """On clean shutdown drop our fragment and re-merge so survivors close our tabs at once
    (rather than waiting for the PID probe on their next publish). Best-effort."""
    try:
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
_autosave_pruned = False


def _autosave(doc_id: Optional[str], name: Optional[str] = None) -> None:
    """Persist a doc to a recoverable .step+ (plus a sidecar tagging the session) so a
    server restart — e.g. after a client timeout killed it — can reload THIS session's
    work instead of dropping every open document. Best-effort; never raises."""
    if not doc_id:
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
        cb.call("doc_save", {"doc_id": doc_id, "path": str(path)})
        meta = {"session": _SESSION_ID, "doc_id": doc_id, "name": name or "",
                "saved_at": time.time()}
        (_autosave_dir() / f"{stem}.json").write_text(json.dumps(meta))
        _prune_autosaves()  # once per process, after our own snapshot is safely down
    except Exception:  # noqa: BLE001
        pass


def _project_save_stem(name: Optional[str], doc_id: str) -> str:
    """Sanitize a doc name into a filesystem-safe stem, mirroring the host's rule:
    spaces -> '_', keep alnum/'_'/'-', drop everything else. Falls back to the
    doc_id when the name yields an empty stem."""
    stem = re.sub(r"\s+", "_", name or "")
    stem = "".join(c for c in stem if c.isalnum() or c in "_-")
    return stem or doc_id


def _project_save(doc_id: Optional[str], name: Optional[str] = None) -> Optional[str]:
    """Save the doc as a DELIVERABLE native .step+ in the user's project folder
    (<CHUDWARE_PROJECT_SAVE_DIR>/<safe_stem>.step+) and return the written path.

    Unlike _autosave (crash recovery, session-prefixed, hidden appdata dir) this is the
    user's own file: it is NOT session-prefixed, so it stays stable and openable across
    runs and overwrites itself as the part is revised. Returns None when the env var is
    unset (the default — nothing changes for existing callers/tests). Best-effort; a
    failed deliverable save must NEVER fail the tool call, so this never raises."""
    if not doc_id:
        return None
    try:
        root = os.environ.get("CHUDWARE_PROJECT_SAVE_DIR")
        if not root:
            return None
        d = Path(root)
        d.mkdir(parents=True, exist_ok=True)
        if not d.is_dir():
            return None
        # Resolve the doc's real name from the engine when the caller didn't pass one.
        if not name:
            try:
                for e in (cb.call("doc_list", {}) or {}).get("docs", []):
                    if isinstance(e, dict) and e.get("doc_id") == doc_id:
                        name = e.get("name") or ""
                        break
            except Exception:  # noqa: BLE001
                pass
        path = d / f"{_project_save_stem(name, doc_id)}.step+"
        r = cb.call("doc_save", {"doc_id": doc_id, "path": str(path)})
        written = r.get("path", str(path)) if isinstance(r, dict) else str(path)
        return written if os.path.exists(written) else None
    except Exception:  # noqa: BLE001
        return None


# doc_id -> engine revision at the doc's last autosave. Each autosave serializes the
# full doc to disk; skipping unchanged docs turns the idle 60s sweep into a no-op.
_AUTOSAVE_LAST_REV: dict = {}


def _autosave_all_open() -> None:
    """Autosave every open document THAT CHANGED since its last autosave (the engine's
    monotonic per-doc revision is the change signal; docs without one are always saved).
    Called on the periodic timer and before a heavy solve so a transport timeout that
    kills the server can never lose an unrelated open part (Bug R-1)."""
    if cb._daemon_mode():
        # Daemon mode: chudware-hostd is THE single autosave authority (start_periodic_autosave
        # is gated off here too). A per-doc doc_save to the MCP autosave dir would repoint the
        # daemon doc's `path` OUTSIDE the daemon's recovery tier AND clear its dirty flag —
        # which DEFEATS the daemon's retire-if-unsaved GC gate (server.cpp gcCloseDoc:
        # isAutosavePath only recognizes the daemon's own dir) and could silently drop an
        # abandoned, never-delivered agent doc when its mirror auto-closes. The daemon holds
        # the doc across an MCP-server death anyway, so this pre-solve guard is redundant here.
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


def _prune_autosaves(max_age_days: float = _AUTOSAVE_MAX_AGE_DAYS,
                     max_files: int = 0) -> int:
    """Delete autosave .step+ / sidecars older than max_age_days (by mtime).

    *max_files* (0 = use _AUTOSAVE_MAX_FILES) additionally caps how many snapshots
    may remain, deleting OLDEST FIRST. The age rule alone never bounded anything in
    practice — the worker forks per turn, so every snapshot on disk is young — which
    is how the directory reached 39k files in one day. Two things are never removed
    by the count cap: this process's own snapshots (live crash recovery) and the
    newest surviving snapshot of any doc_id, so the most recent recoverable copy of
    a document always stays on disk. Returns the number of snapshots deleted.
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
            entries.sort(key=lambda e: e[0], reverse=True)  # newest first
            keep_doc: set[str] = set()
            protected: set[Path] = set()
            for _mt, p in entries:
                stem = p.name[:-6]
                doc = stem.split("-", 1)[1] if "-" in stem else stem
                if doc not in keep_doc:
                    keep_doc.add(doc)
                    protected.add(p)          # newest snapshot of this document
                if stem.startswith(f"{_SESSION_ID}-"):
                    protected.add(p)          # our own live recovery data
            for _mt, p in reversed(entries):  # oldest first
                if len(entries) - n <= cap:
                    break
                if p in protected:
                    continue
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
        info = c.call("hello", {"client": "mcp", "pid": os.getpid(), "proto": 1})
        if isinstance(info, dict) and not info.get("_is_error"):
            c.server_info = info
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


def _recover_orphans_via_daemon() -> list:
    """Open the daemon's orphan-tier docs (retired by clean shutdown / doc-GC —
    never auto-opened; see core/hostd/autosave.h). Only called from the explicit
    recover_parts tool, never at startup. Best-effort; never raises."""
    out: list = []
    try:
        from . import daemon_bridge
        c = daemon_bridge.client()
        entries = (c.call("recovery_list") or {}).get("entries") or []
        if not entries:
            return out
        opened = (c.call("recovery_open", {"all": True}) or {}).get("opened") or []
        names = {e.get("id"): e for e in entries if isinstance(e, dict)}
        for o in opened:
            if not isinstance(o, dict):
                continue
            meta = names.get(o.get("id"), {})
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


def recover_autosaved(scope: str = "recent", include_orphaned: bool = False) -> list:
    """Reopen autosaved .step+ docs into the engine. A reopened doc gets a FRESH doc_id;
    returns [{path, doc_id, name, original_doc_id}] for the ones that loaded.

    scope="recent" (default): only the SINGLE most-recent prior session's docs — the
    crash we're recovering from — never the whole history. scope="all": every autosave.
    include_orphaned: open the daemon's on-disk recovery tier (crash-retired docs) —
    only the explicit recover_parts tool passes this. Startup must NOT (the tier keeps a
    deep history of crash recoveries; auto-opening all of it would flood the engine).
    Prunes stale autosaves first.

    Daemon mode: recovery lives in the orphan tier — a crashed daemon's autosaves and
    any crash-abandoned doc (GC-retired) are REGISTERED there, never auto-opened. A
    clean app quit sends doc_discard, so the tier holds only genuine crash survivors.
    recover_parts opens them DELIBERATELY; startup recovers nothing here (the hello
    id_remap fallback is empty on a current daemon)."""
    if cb._daemon_mode():
        out = _recover_via_daemon()  # [] on a current daemon (no auto-open)
        if include_orphaned:
            seen = {r.get("original_doc_id") for r in out}
            for r in _recover_orphans_via_daemon():
                if r.get("original_doc_id") not in seen:
                    out.append(r)
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

    for p, _sess, _ts, name, orig_id in info:
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
    return out


# --- async jobs ------------------------------------------------------------------------
# Heavy solves (CFD, FEA) can exceed the MCP transport's per-call timeout; when the client gives
# up it may KILL the server and lose every open doc. So the sim tools can run in the BACKGROUND:
# return a job_id immediately, run the engine call on a worker thread (the LBM/FEA solve itself
# does not hold the engine lock), and let the caller poll get_job(job_id). Default stays synchronous
# for simple use; pass background=True for the heavy runs.
import threading as _threading  # noqa: E402
import uuid as _uuid  # noqa: E402

_JOBS: dict = {}
_JOB_LOCK = _threading.Lock()

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

    # Daemon thread: a long OpenFOAM/FEA solve must never block server (or test) shutdown.
    _threading.Thread(target=_run, name=f"chudware-job-{kind}", daemon=True).start()
    return jid


# How long a "synchronous" heavy solve may block the MCP call before we hand it off to
# the background so the call returns BEFORE the transport timeout can kill the server
# (Bug R-1). Stay safely under a typical 60s transport timeout.
_SYNC_SOLVE_BUDGET_S = float(os.environ.get("CHUDWARE_MCP_SYNC_BUDGET_S", "45") or 45)


def _solve_sync_or_handoff(kind: str, fn, progress: Optional[dict] = None,
                           budget_s: float = _SYNC_SOLVE_BUDGET_S) -> dict:
    """Run a heavy solve so it can NEVER time out the transport and drop the session.

    Always runs on a worker thread (via _start_job). For a synchronous request we poll
    the job up to `budget_s`; if it finishes we return the result inline (small solves
    behave exactly as before). If it overruns, we LEAVE it running and return the job_id
    so the call returns in time — the agent polls get_job() instead of losing the session.
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
            return {"ok": False, "error": snap.get("error", "solve failed")}
        time.sleep(interval)
        if interval < 0.1:
            interval = min(0.1, interval * 1.5)
    return {"ok": True, "job_id": jid, "status": "running", "auto_backgrounded": True,
            "note": (f"solve still running after {budget_s:.0f}s — continued in the BACKGROUND "
                     f"to avoid a transport timeout (which would drop open documents). "
                     f"Open parts were autosaved. Poll get_job('{jid}').")}


# A small material library so FEA tools take a friendly name. Fields are exactly what the
# host's study expects (host/src/ui/sim_manager.cpp): E in MPa, density g/cm^3, strengths MPa.
# T-F2: these are sourced from the unified materials registry — the single provenance-stamped
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


def _resolve_material(material: Union[str, dict, None]) -> dict:
    """Resolve a material name or explicit dict to a validated properties dict.

    Always returns a FRESH copy so callers cannot mutate the shared _MATERIALS cache
    between runs (which would silently corrupt subsequent solves with wrong E values).
    The returned dict is validated: E_mpa and nu must be physically plausible so a bad
    entry is caught here rather than inside the C++ solver.
    """
    if isinstance(material, dict):
        mat = dict(material)  # copy: caller-supplied dicts are returned as a new copy too
    else:
        _ensure_materials()
        key = (material or "steel").strip().lower()
        key = _MATERIAL_ALIASES.get(key, key)
        if key not in _MATERIALS:
            raise ValueError(
                f"Unknown material {material!r}. Known: {sorted(_MATERIALS)} "
                f"(or pass an explicit dict with E_mpa/nu/density_g_cm3/yield_mpa)."
            )
        mat = dict(_MATERIALS[key])  # F4: always return a COPY, never the shared dict
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
    "black":     {"color": [0.09, 0.10, 0.12], "metallic": 0.0,  "roughness": 0.50},
}
_APPEARANCE_ALIASES = {
    "aluminium": "aluminum", "al": "aluminum", "ss": "stainless",
    "stainless steel": "stainless", "pla": "plastic", "abs plastic": "abs",
    "dark metal": "black",
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
    key = str(material).strip().lower()
    key = _APPEARANCE_ALIASES.get(key, key)
    if key not in _APPEARANCE_PRESETS:
        # Engineering designations are what a CAD model actually says: "A36
        # steel", "6061-T6 Al", "304 stainless", "PLA filament". These are
        # APPEARANCE presets, so the right answer is the closest visual match,
        # not a rejection (live 2026-07-27: haiku lost a call to each of
        # "A36 steel" and "6061-T6 Al"). Longest name first so "stainless
        # steel" never resolves as plain "steel".
        import re as _re  # noqa: PLC0415
        tokens = set(_re.findall(r"[a-z]+", key))
        candidates = sorted(
            set(_APPEARANCE_PRESETS) | set(_APPEARANCE_ALIASES),
            key=len, reverse=True)
        for cand in candidates:
            hit = cand in key if " " in cand else cand in tokens
            if hit:
                key = _APPEARANCE_ALIASES.get(cand, cand)
                break
    if key not in _APPEARANCE_PRESETS:
        raise ValueError(
            f"Unknown appearance material {material!r}. Known: {sorted(_APPEARANCE_PRESETS)} "
            f"(or pass {{'color':[r,g,b], 'metallic':0..1, 'roughness':0..1}})."
        )
    return dict(_APPEARANCE_PRESETS[key])


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
    """Who owns each face of the LAST body, de-smeared — the one feature->face join.

    owner_map keys each feature's topology as [body_id, topo_id] (1-based TopExp index) and
    doc_query enumerates the same body in the same first-encounter order, so topo_id ==
    match_index + 1. The join is taken against the highest body_id owner_map mentions: a
    feature whose only pairs sit on a superseded body was consumed by a later boolean and owns
    nothing addressable now, and joining its stale topo ids against the live body would frame
    the WRONG faces.

    Each feature's claimed faces then go through _created_face_matches. A face it did not
    create is re-credited to the latest EARLIER feature whose tool could have authored it, and
    left unowned when none could.

    Returns {
      "faces":   [match, ...] in doc_query order — index i is topo id i+1 on "last_body",
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
      "last_body": int}
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
    for fid, om in owner_map.items():
        for pair in (om.get("faces") or []):
            if not (isinstance(pair, list) and len(pair) == 2 and int(pair[0]) == last_body):
                continue
            i = int(pair[1]) - 1
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
            "reattributed": reattributed, "last_body": last_body}


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

    om = (owner_map or {}).get(fid) or {}
    face_topos = {int(p[1]) for p in (om.get("faces") or [])
                  if isinstance(p, list) and len(p) == 2 and int(p[0]) == last_body}
    edge_topos = {int(p[1]) for p in (om.get("edges") or [])
                  if isinstance(p, list) and len(p) == 2 and int(p[0]) == last_body}
    if not face_topos and not edge_topos:
        raise ValueError(
            f"{label} owns no faces/edges on the current body — a later boolean/fillet consumed "
            f"it, so there is nothing to frame. describe_part(doc_id) marks consumed features "
            f"and lists the sids that DO still exist.")

    out = list(att["created"].get(fid) or [])
    if not out and edge_topos:
        edges = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": "edge"}})
                 .get("matches") or [])
        out = [m for i, m in enumerate(edges) if (i + 1) in edge_topos]
    if not out:
        feat = next((f for f in (feats or [])
                     if isinstance(f, dict) and str(f.get("id")) == fid), None)
        verb = "cut" if _feature_is_subtractive(feat, feats) else "fuse"
        raise ValueError(
            f"{label} created no face of its own: every one of the {len(face_topos)} face(s) "
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
    except Exception:  # noqa: BLE001
        pass
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
    """Tile PNGs into a labelled contact-sheet grid with an explicit column count."""
    from PIL import Image as PILImage, ImageDraw  # noqa: PLC0415
    if not png_paths:
        raise ValueError("no tiles to compose")
    imgs = [PILImage.open(p).convert("RGB") for p in png_paths]
    try:
        tw, th = imgs[0].size
        cols = max(int(cols), 1)
        rows = (len(imgs) + cols - 1) // cols
        gap = 4
        sheet = PILImage.new("RGB",
                             (tw * cols + gap * (cols - 1),
                              th * rows + gap * (rows - 1)),
                             (30, 30, 32))
        draw = ImageDraw.Draw(sheet)
        for i, im in enumerate(imgs):
            r, c = divmod(i, cols)
            x = c * (tw + gap)
            y = r * (th + gap)
            sheet.paste(im.resize((tw, th)), (x, y))
            if labels and i < len(labels):
                # Dark halo + light label so the tile name is always readable.
                label = str(labels[i]).upper()
                draw.text((x + 7, y + 5), label, fill=(10, 10, 12))
                draw.text((x + 6, y + 4), label, fill=(235, 240, 248))
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


def _project_match_pixels(doc_id: str, match: dict, part_bbox: dict, view_dir: tuple,
                          W: int = _RENDER_W, H: int = _RENDER_H) -> list:
    """Screen-space sample pixels for a match (face points projected through the render frame)."""
    frame = _part_render_frame(part_bbox, view_dir, W, H)
    pts = _match_point_cloud(doc_id, match)
    pixels = []
    for p in pts:
        sx, sy = _world_to_screen(p, frame)
        ix, iy = int(round(sx)), int(round(sy))
        if 0 <= ix < W and 0 <= iy < H:
            pixels.append((ix, iy))
    return pixels


def _apply_emphasis_overlay(src_png: str, doc_id: str, matches: list, part_bbox: dict,
                            view_dir: tuple, mode: str = "emphasis",
                            out_png: Optional[str] = None,
                            W: int = _RENDER_W, H: int = _RENDER_H,
                            palette: Optional[dict] = None) -> tuple:
    """Desaturate context, paint emphasized faces, glow outline. Returns (path, palette).

    palette: optional pre-built emphasis palette (from _build_emphasis_palette). When
    omitted, built from matches + mode. Callers that need index-stable distinct colours
    (measure) pass their own palette so caption colours match the painted overlay.
    """
    from PIL import Image as PILImage, ImageDraw, ImageFilter, ImageEnhance  # noqa: PLC0415

    palette = palette or _build_emphasis_palette(matches, mode=mode)
    out_png = out_png or _tmp(".png")
    with PILImage.open(src_png) as im0:
        im = im0.convert("RGB")
        # If the source is a crop/upscale already sized differently, use its size.
        W, H = im.size
        # Desaturate whole image for context.
        grey = ImageEnhance.Color(im).enhance(_CONTEXT_DESAT)
        base = grey.convert("RGBA")
        overlay = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        color_by_key = {e["key"]: tuple(e["rgb"]) for e in palette["entries"]}
        default_rgb = tuple(palette["entries"][0]["rgb"]) if palette["entries"] else _EMPHASIS_PALETTE[0]

        all_pixels: list = []
        for i, m in enumerate(matches or []):
            # Prefer index-aligned palette entry (distinct sequential colours for measure).
            if i < len(palette["entries"]):
                rgb = tuple(palette["entries"][i]["rgb"])
            else:
                key = _match_color_key(m, i)
                rgb = color_by_key.get(key, default_rgb)
            pixels = _project_match_pixels(doc_id, m, part_bbox, view_dir, W, H)
            all_pixels.extend(pixels)
            for (ix, iy) in pixels:
                # Fat brush so sparse face samples still read as a filled region.
                r = 4
                draw.ellipse([ix - r, iy - r, ix + r, iy + r], fill=rgb + (220,))

        # Glow / outline around the union of emphasized pixels.
        if all_pixels:
            mask = PILImage.new("L", (W, H), 0)
            md = ImageDraw.Draw(mask)
            for (ix, iy) in all_pixels:
                md.ellipse([ix - 5, iy - 5, ix + 5, iy + 5], fill=255)
            glow = mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(1.5))
            glow_rgba = PILImage.new("RGBA", (W, H), tuple(_EMPHASIS_GLOW) + (0,))
            # Put glow only on dilated ring (glow - original mask).
            ring = ImageEnhance.Brightness(glow).enhance(1.0)
            # Build ring: dilated where original was empty-ish.
            from PIL import ImageChops  # noqa: PLC0415
            ring_mask = ImageChops.subtract(glow, mask)
            glow_rgba.putalpha(ring_mask)
            base = PILImage.alpha_composite(base, glow_rgba)

        base = PILImage.alpha_composite(base, overlay).convert("RGB")
        # Legend strip at bottom so the image is self-describing.
        legend = palette.get("legend") or ""
        if legend:
            strip_h = 18
            sheet = PILImage.new("RGB", (W, H + strip_h), (28, 30, 34))
            sheet.paste(base, (0, 0))
            d2 = ImageDraw.Draw(sheet)
            d2.text((6, H + 2), f"EMPHASIS [{palette['mode']}]: {legend}"[:120],
                    fill=(230, 230, 235))
            base = sheet
        base.save(out_png, format="PNG")
    return out_png, palette


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
_HLR_TRI_BUDGET = 45000           # above this the depth pass is skipped (caption says so)
_MESH_CACHE: dict = {}            # doc_id -> (aicm_handle, mesh)


def _annot_font(size: int = 13):
    """A scalable font for overlay labels. Pillow's bundled default is scalable from 10.1;
    a system sans is preferred when present. Never raises."""
    from PIL import ImageFont  # noqa: PLC0415
    for p in ("/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/google-noto/NotoSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _aicm_payload(handle: str) -> bytes:
    """AICM bytes for an engine tess handle: an "aicm-shm:///name" POSIX shared-memory object
    (8-byte little-endian size header + payload, core/util/aicm_handoff.cpp) or a plain file
    path (older engine / Windows fallback)."""
    prefix = "aicm-shm://"
    if handle.startswith(prefix):
        name = handle[len(prefix):].lstrip("/")
        with open(os.path.join("/dev/shm", name), "rb") as f:
            raw = f.read()
        if len(raw) < 8:
            raise ValueError(f"short AICM shm object {name!r}")
        n = int.from_bytes(raw[:8], "little")
        return raw[8:8 + n]
    with open(handle, "rb") as f:
        return f.read()


def _doc_mesh(doc_id: str) -> Optional[dict]:
    """Current tessellation of `doc_id`, or None when it cannot be read.

    Returns {"pos": (N,3) float64, "idx": (M,3) int32, "tri_face": (M,) int32 (index into
    "faces", -1 for raw-mesh triangles), "faces": [{body, face_id, first_tri, tri_count,
    surf_type}] with GLOBAL triangle indices, "edges": [{body, edge_id, pts (K,3)}],
    "last_body": int}. `face_id`/`edge_id` are the OCCT topo ids, which are exactly the
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
        from services.sim.tessellate import read_aicm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        try:
            from sim.tessellate import read_aicm  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return None
    try:
        parsed = read_aicm(_aicm_payload(handle))
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
            "edges": edges, "last_body": last_body}
    _MESH_CACHE.clear()  # one doc's mesh at a time — these arrays are the big allocation
    _MESH_CACHE[doc_id] = (handle, mesh)
    return mesh


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
    _crop_frame) lets the same code project into a cropped-and-rescaled image."""
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


def _mesh_depth_buffer(mesh: dict, frame: dict) -> Optional[tuple]:
    """Rasterize the mesh into (depth, face_index) buffers matching the engine's render.

    Reproduces render_view_to_rgb's barycentric span fill and `depth > zbuf` test exactly,
    so a pixel's winning triangle here is the pixel the engine shaded. Back-facing triangles
    are culled first (AICM guarantees outward winding, so for a closed solid this is exact
    and halves the work). Returns (Z, FI, S) or None when the mesh is over the triangle
    budget — the caller then reports that edges were skipped rather than lying about them."""
    import numpy as np  # noqa: PLC0415
    idx = mesh["idx"]
    if idx.shape[0] > _HLR_TRI_BUDGET:
        return None
    W, H = int(frame["W"]), int(frame["H"])
    pos = mesh["pos"]
    S = _screen_xyz(pos, frame)
    d = np.asarray(frame["d"])
    p0, p1, p2 = pos[idx[:, 0]], pos[idx[:, 1]], pos[idx[:, 2]]
    front = np.cross(p1 - p0, p2 - p0) @ d > 0.0
    tri_ids = np.nonzero(front)[0]
    tri_face = mesh["tri_face"]

    Z = np.full((H, W), -1e30, dtype=np.float64)
    FI = np.full((H, W), -1, dtype=np.int32)
    for t in tri_ids:
        s0, s1, s2 = S[idx[t, 0]], S[idx[t, 1]], S[idx[t, 2]]
        minx = max(int(math.floor(min(s0[0], s1[0], s2[0]))), 0)
        maxx = min(int(math.ceil(max(s0[0], s1[0], s2[0]))), W - 1)
        miny = max(int(math.floor(min(s0[1], s1[1], s2[1]))), 0)
        maxy = min(int(math.ceil(max(s0[1], s1[1], s2[1]))), H - 1)
        if maxx < minx or maxy < miny:
            continue
        area = (s1[0] - s0[0]) * (s2[1] - s0[1]) - (s2[0] - s0[0]) * (s1[1] - s0[1])
        if abs(area) < 1e-9:
            continue
        fx = np.arange(minx, maxx + 1, dtype=np.float64)[None, :] + 0.5
        fy = np.arange(miny, maxy + 1, dtype=np.float64)[:, None] + 0.5
        a0 = ((s1[0] - fx) * (s2[1] - fy) - (s2[0] - fx) * (s1[1] - fy)) / area
        a1 = ((s2[0] - fx) * (s0[1] - fy) - (s0[0] - fx) * (s2[1] - fy)) / area
        a2 = 1.0 - a0 - a1
        inside = (a0 >= 0) & (a1 >= 0) & (a2 >= 0)
        if not inside.any():
            continue
        dep = a0 * s0[2] + a1 * s1[2] + a2 * s2[2]
        zs = Z[miny:maxy + 1, minx:maxx + 1]
        upd = inside & (dep > zs)
        if not upd.any():
            continue
        zs[upd] = dep[upd]
        FI[miny:maxy + 1, minx:maxx + 1][upd] = tri_face[t]
    return Z, FI, S


def _dilate_max3(Z):
    """3x3 maximum of a depth buffer — the half-pixel slack an edge lying exactly ON a face
    needs to survive the depth test at a silhouette."""
    import numpy as np  # noqa: PLC0415
    P = np.pad(Z, 1, mode="edge")
    h, w = Z.shape
    out = P[0:h, 0:w].copy()
    for a in range(3):
        for c in range(3):
            np.maximum(out, P[a:a + h, c:c + w], out=out)
    return out


def _visible_runs(pts, frame: dict, Zmax, eps: float) -> list:
    """Split a world polyline into the screen-space runs that survive the depth test.

    Each run is a list of (x, y) floats ready for ImageDraw.line. Points are resampled to
    ~2 px so a long edge disappearing behind a boss is cut at the right place."""
    import numpy as np  # noqa: PLC0415
    S = _screen_xyz(pts, frame)
    if S.shape[0] < 2:
        return []
    H, W = Zmax.shape
    runs: list = []
    run: list = []
    for k in range(S.shape[0] - 1):
        a, b = S[k], S[k + 1]
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 2.0) + 2)
        ts = np.linspace(0.0, 1.0, n)[:, None]
        seg = a[None, :] * (1.0 - ts) + b[None, :] * ts
        xi = np.clip(seg[:, 0].astype(np.int64), 0, W - 1)
        yi = np.clip(seg[:, 1].astype(np.int64), 0, H - 1)
        onscreen = ((seg[:, 0] >= -2) & (seg[:, 0] < W + 2)
                    & (seg[:, 1] >= -2) & (seg[:, 1] < H + 2))
        vis = onscreen & (seg[:, 2] >= Zmax[yi, xi] - eps)
        for j, v in enumerate(vis):
            if v:
                run.append((float(seg[j, 0]), float(seg[j, 1])))
            elif run:
                if len(run) > 1:
                    runs.append(run)
                run = []
    if len(run) > 1:
        runs.append(run)
    return runs


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
    ys, xs = np.nonzero(b)
    r = max(1, width) - 1
    for x, y in zip(xs.tolist(), ys.tolist()):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rgb)


def _place_label(draw, font, anchor: tuple, text: str, rgb: tuple,
                 W: int, H: int, taken: list) -> tuple:
    """Draw a leader line from `anchor` to a non-overlapping text chip and record its rect.

    Candidate offsets fan out from the anchor; the first that fits the frame and clears every
    rect in `taken` wins, so N callouts never stack on top of each other."""
    try:
        tb = draw.textbbox((0, 0), text, font=font)
    except Exception:  # noqa: BLE001
        tb = (0, 0, 7 * len(text), 12)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    pad = 3
    bw, bh = tw + 2 * pad, th + 2 * pad
    ax, ay = anchor
    best = None
    for dist in (26, 46, 70, 96, 124):
        for dx, dy in ((1, -1), (1, 1), (-1, -1), (-1, 1), (1, 0), (-1, 0), (0, -1), (0, 1)):
            x = ax + dx * dist - (bw if dx < 0 else 0)
            y = ay + dy * dist - (bh // 2 if dy == 0 else (bh if dy < 0 else 0))
            x = max(2, min(int(x), W - bw - 2))
            y = max(2, min(int(y), H - bh - 2))
            rect = (x, y, x + bw, y + bh)
            if any(not (rect[2] < o[0] or rect[0] > o[2]
                        or rect[3] < o[1] or rect[1] > o[3]) for o in taken):
                continue
            best = rect
            break
        if best:
            break
    if best is None:
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


def _draw_scale_bar(draw, font, frame: dict, size_mm: tuple, W: int, H: int) -> float:
    """Bottom-right scale bar + exact bbox readout, so the image itself carries scale.

    Returns the bar's length in mm (also reported in the caption)."""
    mm = _nice_step((0.22 * W) / max(frame["scale"], 1e-9))
    px = int(round(mm * frame["scale"]))
    px = max(24, min(px, W // 2))
    x1, y1 = W - 12, H - 16
    x0 = x1 - px
    dims = f"bbox {size_mm[0]:.4g} x {size_mm[1]:.4g} x {size_mm[2]:.4g} mm"
    label = f"{mm:.4g} mm"
    draw.rectangle([x0 - 8, y1 - 30, W - 4, H - 4], fill=(24, 26, 32))
    draw.line([(x0, y1), (x1, y1)], fill=(240, 242, 248), width=2)
    for x in (x0, x1):
        draw.line([(x, y1 - 5), (x, y1 + 5)], fill=(240, 242, 248), width=2)
    draw.text((x0, y1 + 2), label, font=font, fill=(240, 242, 248))
    try:
        db = draw.textbbox((0, 0), dims, font=font)
        draw.text((W - 8 - (db[2] - db[0]), y1 - 26), dims, font=font, fill=(196, 202, 214))
    except Exception:  # noqa: BLE001
        draw.text((x0 - 4, y1 - 26), dims, font=font, fill=(196, 202, 214))
    return mm


def _doc_topo_maps(doc_id: str) -> dict:
    """{"face": [match,...], "edge": [match,...]} in doc_query enumeration order, so index i
    is topo id i+1 == the AICM face_id/edge_id. Also gives sid -> topo lookups."""
    out = {"face": [], "edge": [], "face_sid": {}, "edge_sid": {}}
    for of in ("face", "edge"):
        try:
            ms = (cb.call("doc_query", {"doc_id": doc_id, "query": {"of": of}})
                  or {}).get("matches") or []
        except Exception:  # noqa: BLE001
            ms = []
        out[of] = ms
        out[of + "_sid"] = {str(m.get("sid")): i + 1 for i, m in enumerate(ms) if m.get("sid")}
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
                     out_png: Optional[str] = None) -> tuple:
    """Overlay B-rep edges, highlights, feature callouts and a scale bar onto a render.

    src_png MUST be the engine render taken along `view_dir` (this reprojects the SAME
    camera). Pass `frame` (from _crop_frame) when src_png has already been cropped/rescaled.
    Returns (path, info) where `info` feeds _render_annotation_caption. On any failure the
    ORIGINAL png path is returned with info["available"] False — the caller's render is never
    lost to an overlay problem."""
    info: dict = {"available": False, "edges": False, "hlr": False, "highlights": [],
                  "callouts": [], "callouts_omitted": [], "hidden_features": [],
                  "hidden_faces": 0, "scale_bar_mm": None, "bbox_mm": None,
                  "note": None, "part_name": None}
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
        topo = _doc_topo_maps(doc_id)
        face_by_topo = {f["face_id"]: i for i, f in enumerate(mesh["faces"])
                        if f["body"] == mesh["last_body"]}
        edge_by_topo = {e["edge_id"]: i for i, e in enumerate(mesh["edges"])
                        if e["body"] == mesh["last_body"]}

        def _topos_for(matches: list) -> tuple:
            ftop, etop = set(), set()
            for m in matches or []:
                sid = str(m.get("sid") or "")
                if sid in topo["face_sid"]:
                    ftop.add(topo["face_sid"][sid])
                elif sid in topo["edge_sid"]:
                    etop.add(topo["edge_sid"][sid])
            return ftop, etop

        # ---- highlight fills (tint the exact visible pixels of each target face) --------
        hl_draw: list = []
        for h in hl_specs:
            ftop, etop = _topos_for(h["matches"])
            fidx = [face_by_topo[t] for t in sorted(ftop) if t in face_by_topo]
            mask = None
            if FI is not None and fidx:
                mask = np.isin(FI, np.asarray(fidx, dtype=np.int32))
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
            Zmax = _dilate_max3(Z)
            hl_edge_ids = {i for h in hl_draw for i in h["edge_idx"]}
            n_runs = 0
            for ei, e in enumerate(mesh["edges"]):
                if ei in hl_edge_ids:
                    continue
                for run in _visible_runs(e["pts"], frame, Zmax, eps):
                    draw.line(run, fill=_EDGE_RGB, width=1)
                    n_runs += 1
            info["edges"] = n_runs > 0
            # Highlighted EDGE targets are drawn last, fat, in their reserved colour.
            for h in hl_draw:
                for ei in h["edge_idx"]:
                    for run in _visible_runs(mesh["edges"][ei]["pts"], frame, Zmax, eps):
                        draw.line(run, fill=h["rec"]["colour"], width=3)

        # ---- highlight outlines + leader labels ---------------------------------------
        taken: list = []
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
                anchor = _mask_anchor(h["mask"])
            if anchor is None and h["edge_idx"]:
                pts = mesh["edges"][h["edge_idx"][0]]["pts"]
                S = _screen_xyz(pts, frame)
                anchor = (int(S[len(S) // 2, 0]), int(S[len(S) // 2, 1]))
            if anchor is None and rec["matches"]:
                c = rec["matches"][0].get("centroid")
                if isinstance(c, (list, tuple)) and len(c) >= 3:
                    S = _screen_xyz([c], frame)
                    anchor = (int(S[0, 0]), int(S[0, 1]))
            if anchor is not None:
                ax = max(2, min(int(anchor[0]), W - 3))
                ay = max(2, min(int(anchor[1]), H - 3))
                _place_label(draw, font, (ax, ay),
                             f"* {rec['label']}", rec["colour"], W, H, taken)
                entry["visible"] = h["mask"] is not None or bool(h["edge_idx"])
            info["highlights"].append(entry)

        # ---- feature callouts (id + name on a leader, largest-first, capped) -----------
        # A feature that already carries a highlight chip is skipped — two chips naming the
        # same thing in two colours is worse than one.
        if callouts and FI is not None:
            labelled = {str(h["label"]) for h in info["highlights"]}
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
            hidden = []
            for i, fr in enumerate(mesh["faces"]):
                if i in seen or fr["body"] != mesh["last_body"]:
                    continue
                m = topo["face"][fr["face_id"] - 1] if fr["face_id"] - 1 < len(topo["face"]) else {}
                tag = str(m.get("sid") or f"face#{fr['face_id']}")
                bits = [b for b in (m.get("side"), m.get("surf"), m.get("concavity")) if b]
                hidden.append(f"{tag}" + (f" ({'/'.join(bits)})" if bits else ""))
            info["hidden_faces"] = len(hidden)
            info["hidden_face_list"] = hidden[:8]

        if scale and info["bbox_mm"]:
            info["scale_bar_mm"] = _draw_scale_bar(draw, _annot_font(12), frame,
                                                   info["bbox_mm"], W, H)

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


def _resolve_measure_target(doc_id: str, target: Union[str, dict]) -> tuple:
    """Resolve one measure target → (ref_dict, label).

    Prefer the single-selector path (measure_between / eval_measure). Fall back to the
    inspect_part resolver for feature ids / multi-match queries, taking the first match.
    """
    # Already-resolved selector (sid / kind+centroid) — pass straight to eval_measure.
    if isinstance(target, dict):
        has_geom_pred = any(k in target for k in (
            "of", "side", "surf", "pick", "parallel", "near", "index", "count",
            "concave", "convex", "owner_feature"))
        if not has_geom_pred and (
                target.get("sid") or target.get("kind") in ("face", "edge", "vertex")):
            label = str(target.get("sid") or target.get("kind") or "ref")
            return target, label

    try:
        ref = _resolve_one_selector(doc_id, target)
        if isinstance(target, str):
            label = target.strip()
        else:
            label = str(ref.get("sid") or (target.get("side") if isinstance(target, dict)
                                           else None) or ref.get("kind") or "ref")
        return ref, label
    except ValueError:
        resolved = _resolve_inspect_target(doc_id, target)
        ms = list(resolved.get("matches") or [])
        if not ms:
            raise ValueError(
                f"No geometry matched measure target {target!r}. "
                f"Use query_geometry(...) to explore, or narrow with pick/side/near.")
        ref = ms[0]
        label = resolved.get("label") or (
            target.strip() if isinstance(target, str) else str(ref.get("sid") or "ref"))
        if len(ms) > 1 and ref.get("sid"):
            # A feature/multi-match target measures ONE element — say WHICH, so the caption
            # can't read as "the whole feature is 9345 mm²".
            label = f'{label} → {ref["sid"]} (1 of {len(ms)})'
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
    """
    q = query if isinstance(query, dict) else _as_face_query(query)
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
    """Merge multiple doc_face_points sets into ONE faceset dict for a single load/BC.

    Points concatenate. Geometry hints survive only where they stay valid for EVERY
    merged face: face_normal/perp_tol from the first set (matches of one query share
    orientation), and the analytic ``surface`` descriptor only when ALL sets carry an
    IDENTICAL one (e.g. a hole split into two half-cylinder faces at the seam — the same
    cylinder), because keeping one face's surface would make the tight distance-to-surface
    test silently reject every node of the other faces. tol_mm takes the MAX of the sets
    so the trimmed-extent proximity test still bridges each face's sample spacing."""
    merged: dict = {"points": []}
    for f in faces:
        merged["points"].extend(f.get("points", []))
    if not faces:
        return merged
    first = faces[0]
    tols = [f["tol_mm"] for f in faces if "tol_mm" in f]
    if tols:
        merged["tol_mm"] = max(tols)
    if "face_normal" in first:
        merged["face_normal"] = first["face_normal"]
    if "perp_tol_mm" in first:
        merged["perp_tol_mm"] = first["perp_tol_mm"]
    surfs = [f.get("surface") for f in faces]
    if surfs[0] is not None and all(s == surfs[0] for s in surfs):
        merged["surface"] = surfs[0]
    return merged


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
            cb.call("sim_study_edit", {"doc_id": doc_id, "study_id": rid, "study": record})
            return rid
        res = cb.call("sim_study_add", {"doc_id": doc_id, "study": record})
        return res.get("study_id") if isinstance(res, dict) else None
    except Exception:  # noqa: BLE001 — persistence is best-effort, never fail the solve
        return None


# --------------------------------------------------- T-BL04 thermal helpers

# Typical thermal conductivity (W/m·K) by material family — used by run_fea_thermal
# when neither the caller nor the material dict supplies one. Engineering nominals
# (clean-room, standard handbook values), only for the box-proxy conduction estimate.
_DEFAULT_CONDUCTIVITY_WMK: dict = {
    "steel": 50.0, "stainless": 16.0, "aluminum": 205.0, "titanium": 22.0,
    "brass": 109.0, "abs": 0.17, "pla": 0.13,
}


def _default_conductivity(mat: dict, material_arg: Any) -> float:
    """Best-effort thermal conductivity (W/m·K) for the box-proxy thermal solve."""
    name = str(mat.get("name", material_arg) or "").lower()
    for key, val in _DEFAULT_CONDUCTIVITY_WMK.items():
        if key in name:
            return val
    return 50.0  # mild-steel default


# Typical specific heat (J/(kg·K)) by material family — used by the TRANSIENT thermal
# path (T-FIN18) when neither the caller nor the material dict supplies cp. Same
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
