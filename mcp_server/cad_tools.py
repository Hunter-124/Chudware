"""C++ feature-engine + simulation MCP tools — parity with the in-app AI-CAD agent.

These tools drive the SAME engine the desktop app uses (``libaicad.dll`` via
``core_bridge``): a real parametric feature kernel with an editable feature tree, the
plain-English geometry vocabulary (address faces/edges in words — "the top face", "the
vertical edges" — no coordinates or face numbers), FEA (static + modal), the LBM
wind-tunnel CFD, the labelled/colour-coded vision render, 2D drawings, and the
standard-parts library.

The intended driver is Claude itself (in Claude cowork/chat or Claude Code) — it is the
"brain" the in-app agent delegates to a local LLM. So the first tool, ``cad_guide``,
hands Claude the engine's own modeling cheatsheet (the closed op vocabulary + worked
examples). Typical loop:

    cad_guide()                  # learn the op vocabulary (call this FIRST)
    new_part("bracket")
    add_features([...ops...])    # build with standard editable features
    render_part()                # SEE it — labelled, colour-coded; self-review
    ... iterate ...
    run_fea_static(...)          # prove it holds
    export_part("step")          # or make_drawing()

Registered onto the FastMCP instance by ``register_cad_tools(mcp)``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Union

from mcp.server.fastmcp import FastMCP, Image

from . import core_bridge as cb
from .core_bridge import CoreError

# The "current part" so single-argument calls just work (every tool also takes an explicit
# doc_id). Mirrors the old server's _STATE, but the doc lives in the C++ core, not here.
_CUR: dict = {"doc_id": None}

# doc_id STABILITY across an autosave-recovery (Issue 1). When a transport timeout kills the
# server and it restarts, recovery reopens each autosaved .step+ — which the engine assigns a
# FRESH doc_id (the per-process counter doesn't reset). Any doc_id the agent was holding would
# then be dead. We map the ORIGINAL id (the autosave filename stem) -> the new live id here, so
# an old handle the agent passes still resolves to the recovered document instead of erroring.
_DOC_ALIASES: dict = {}

# GSL Stage 1a — last-known solid volume per doc, so add_features can report a volume DELTA
# (material added / removed / unchanged) in its write-diff confirmation. Best-effort cache.
_LAST_VOLUME: dict = {}


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


def _doc(doc_id: Optional[str]) -> str:
    d = doc_id or _CUR["doc_id"]
    if not d:
        raise ValueError("No current part. Call new_part(...) first (or pass doc_id=).")
    return _resolve_alias(d)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2)


def _err(e: Exception) -> str:
    """Render an engine error as a structured, fixable result (not a transport failure)."""
    if isinstance(e, CoreError):
        out = {"ok": False, "error": str(e)}
        if e.code:
            out["code"] = e.code
        if e.detail is not None:
            out["detail"] = e.detail
        return _dumps(out)
    return _dumps({"ok": False, "error": str(e)})


def _tmp(suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="aicad_mcp_")
    os.close(fd)
    return path


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
# native .step+ doc plus a tiny live.json {rev, step} into %APPDATA%\AI-CAD\handoff\; the host
# polls that control file each frame (ui::pollAiCadHandoff) and opens/refreshes one tab from it.
_PUB: dict = {"rev": None}


def _handoff_dir() -> Path:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    d = Path(base) / "AI-CAD" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _publish_current(doc_id: str) -> Optional[dict]:
    """Publish `doc_id` to the handoff dir so the open app shows it. Best-effort; never raises."""
    try:
        d = _handoff_dir()
        step = d / "live.step+"
        cb.call("doc_save", {"doc_id": doc_id, "path": str(step)})
        if _PUB["rev"] is None:
            # Seed from any existing control file so the rev keeps rising across MCP restarts.
            try:
                _PUB["rev"] = int(json.loads((d / "live.json").read_text()).get("rev", 0))
            except Exception:
                _PUB["rev"] = 0
        _PUB["rev"] += 1
        ctrl = {"rev": _PUB["rev"], "step": str(step), "doc_id": doc_id}
        tmp = d / "live.json.tmp"
        tmp.write_text(json.dumps(ctrl))
        os.replace(tmp, d / "live.json")  # atomic: the host never reads a half-written control file
        return {"shown_in_app": True, "rev": _PUB["rev"]}
    except Exception:
        return None


# A per-process session id stamped onto every autosave. Recovery is scoped to the
# SINGLE most-recent PRIOR session (the one that crashed), not the whole autosave
# history, so a restart doesn't drag in 19 unrelated old docs (Bug S-1).
import uuid as _uuid_session  # noqa: E402
_SESSION_ID = _uuid_session.uuid4().hex[:12]
# Autosaves older than this are pruned at startup so the directory can't grow forever.
_AUTOSAVE_MAX_AGE_DAYS = float(os.environ.get("AI_CAD_AUTOSAVE_MAX_AGE_DAYS", "7") or 7)


def _autosave_dir() -> Path:
    base = os.environ.get("APPDATA") or tempfile.gettempdir()
    d = Path(base) / "AI-CAD" / "mcp_autosave"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
        path = _autosave_dir() / f"{doc_id}.step+"
        cb.call("doc_save", {"doc_id": doc_id, "path": str(path)})
        meta = {"session": _SESSION_ID, "doc_id": doc_id, "name": name or "",
                "saved_at": time.time()}
        (_autosave_dir() / f"{doc_id}.json").write_text(json.dumps(meta))
    except Exception:  # noqa: BLE001
        pass


def _autosave_all_open() -> None:
    """Autosave EVERY open document. Called before a heavy solve so a transport timeout
    that kills the server can never lose an unrelated open part (Bug R-1)."""
    try:
        docs = (cb.call("doc_list", {}) or {}).get("docs", [])
    except Exception:  # noqa: BLE001
        return
    for d in docs:
        did = d.get("doc_id") if isinstance(d, dict) else None
        if did:
            _autosave(did, d.get("name") if isinstance(d, dict) else None)


def _prune_autosaves(max_age_days: float = _AUTOSAVE_MAX_AGE_DAYS) -> int:
    """Delete autosave .step+ / sidecars older than max_age_days (by mtime). Returns count."""
    n = 0
    cutoff = time.time() - max_age_days * 86400.0
    try:
        for p in _autosave_dir().glob("*.step+"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    p.with_suffix(".json").unlink(missing_ok=True)
                    n += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return n


def _autosave_session_of(step_path: Path) -> tuple[Optional[str], float, str]:
    """(session_id, saved_at, name) for an autosave from its sidecar, falling back to
    file mtime when there's no sidecar (legacy autosaves)."""
    side = step_path.with_suffix(".json")
    try:
        m = json.loads(side.read_text())
        return m.get("session"), float(m.get("saved_at", step_path.stat().st_mtime)), m.get("name", "")
    except Exception:  # noqa: BLE001
        try:
            return None, step_path.stat().st_mtime, ""
        except OSError:
            return None, 0.0, ""


def recover_autosaved(scope: str = "recent") -> list:
    """Reopen autosaved .step+ docs into the engine. A reopened doc gets a FRESH doc_id;
    returns [{path, doc_id, name, original_doc_id}] for the ones that loaded.

    scope="recent" (default): only the SINGLE most-recent prior session's docs — the
    crash we're recovering from — never the whole history. scope="all": every autosave.
    Prunes stale autosaves first. Called at server startup and by recover_parts."""
    _prune_autosaves()
    out: list = []
    try:
        files = sorted(_autosave_dir().glob("*.step+"))
    except Exception:  # noqa: BLE001
        return out

    info = [(p, *_autosave_session_of(p)) for p in files]  # (path, session, saved_at, name)
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

    for p, _sess, _ts, name in info:
        try:
            r = cb.call("doc_open", {"path": str(p)})
            did = r.get("doc_id")
            if did:
                # Prefer the sidecar name; fall back to the reopened doc's own name so an
                # untagged/legacy autosave still recovers with a real name (Bug N-6).
                original = p.stem
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
_JOB_DONE_TTL_S: float = float(os.environ.get("AI_CAD_JOB_TTL_S", "600") or 600)  # 10 min


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
    _threading.Thread(target=_run, name=f"aicad-job-{kind}", daemon=True).start()
    return jid


# How long a "synchronous" heavy solve may block the MCP call before we hand it off to
# the background so the call returns BEFORE the transport timeout can kill the server
# (Bug R-1). Stay safely under a typical 60s transport timeout.
_SYNC_SOLVE_BUDGET_S = float(os.environ.get("AI_CAD_MCP_SYNC_BUDGET_S", "45") or 45)


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
_MATERIALS: dict = {
    "steel": {"name": "Mild steel (A36)", "E_mpa": 200000, "nu": 0.29, "density_g_cm3": 7.85,
              "yield_mpa": 250, "ultimate_mpa": 400, "endurance_mpa": 200},
    "stainless": {"name": "Stainless 304", "E_mpa": 193000, "nu": 0.29, "density_g_cm3": 8.0,
                  "yield_mpa": 215, "ultimate_mpa": 505, "endurance_mpa": 240},
    "aluminum": {"name": "Aluminium 6061-T6", "E_mpa": 69000, "nu": 0.33, "density_g_cm3": 2.70,
                 "yield_mpa": 276, "ultimate_mpa": 310, "endurance_mpa": 96},
    "titanium": {"name": "Titanium Ti-6Al-4V", "E_mpa": 113800, "nu": 0.342, "density_g_cm3": 4.43,
                 "yield_mpa": 880, "ultimate_mpa": 950, "endurance_mpa": 510},
    "brass": {"name": "Brass C360", "E_mpa": 97000, "nu": 0.34, "density_g_cm3": 8.5,
              "yield_mpa": 124, "ultimate_mpa": 338, "endurance_mpa": 120},
    "abs": {"name": "ABS plastic", "E_mpa": 2300, "nu": 0.35, "density_g_cm3": 1.04,
            "yield_mpa": 40, "ultimate_mpa": 44, "endurance_mpa": 15},
    "pla": {"name": "PLA plastic", "E_mpa": 3500, "nu": 0.36, "density_g_cm3": 1.24,
            "yield_mpa": 50, "ultimate_mpa": 50, "endurance_mpa": 18},
}
_MATERIAL_ALIASES: dict = {
    "mild steel": "steel", "carbon steel": "steel", "a36": "steel",
    "ss": "stainless", "304": "stainless", "stainless steel": "stainless",
    "aluminium": "aluminum", "al": "aluminum", "6061": "aluminum", "alu": "aluminum",
    "ti": "titanium", "ti-6al-4v": "titanium",
}


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


def _as_face_query(q: Union[str, dict]) -> dict:
    """Accept a full geometry query dict OR a bare side word ("top", "+z") -> a face query."""
    if isinstance(q, dict):
        return q
    return {"of": "face", "side": str(q)}


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


# --------------------------------------------------------------------- the guide text

_GUIDE_PROMPTS = ["overview", "review", "simulation"]


def _prompts_dir() -> Path:
    d = os.environ.get("AI_CAD_PROMPTS_DIR")
    if d and Path(d).is_dir():
        return Path(d)
    # Fall backs: the staged dist prompts, then the repo source prompts.
    staged = cb.dist_dir() / "app" / "agent" / "prompts"
    if staged.is_dir():
        return staged
    return cb._repo_root() / "agent" / "prompts"


def _read_prompt(rel: str) -> str:
    p = _prompts_dir() / rel
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


_SIM_GUIDE = """\
SIMULATION GUIDE — FEA + CFD on the live part.

Faces for fixtures and loads are named with the SAME plain-English geometry queries you
build with: a bare side word ("bottom", "+z") or a full query ({"of":"face","side":"bottom",
"pick":"largest"}). Use query_geometry(...) first if unsure what faces a part has.

run_fea_static(fixtures, loads, material) — linear static stress.
  fixtures : list of face queries that are held fixed (encastre). e.g. ["bottom"].
  loads    : list of dicts, each a face query plus a force or pressure:
               {"face": "top", "force": [0, 0, -500]}      # newtons, world XYZ
               {"face": "top", "pressure": 0.5}            # MPa, inward on the face
  material : "steel" | "aluminum" | "titanium" | "stainless" | "brass" | "abs" | "pla"
             (or an explicit dict with E_mpa / nu / density_g_cm3 / yield_mpa).
  Returns max von-Mises stress (MPa), max displacement (mm), and the min factor of safety
  (yield / max stress). FoS > 1 means it holds; design to a target margin.

run_fea_modal(fixtures, material, n_modes) — natural frequencies (Hz) of the held part.

run_cfd_flow(velocity, aoa, fluid) — LBM wind tunnel over the whole part/assembly.
  velocity m/s, aoa degrees, fluid "air"|"water". Returns drag (N), lift (N), Cd, frontal
  area, and Reynolds number. Coarse grid by default — a directional estimate, raise `cells`
  for more resolution.
  CONVENTIONS (Z-up): freestream +X, lift +Z, span Y; +aoa pitches nose up. `reynolds`/`cd`
  use the part's LONGEST span as reference length (reported as `ref_length_mm`).
  TRUST THE DIAGNOSTICS: the result reports `voxel_mm` / `min_feature_voxels` (thin features
  under ~8 voxels are unresolved → lift reads ~0, a `warning` is set — raise `cells` or run the
  thin part alone) and `converged` / `residual` (a non-converged run is under-iterated, not
  physically zero). For heavy runs pass background=True and poll get_job(job_id).

run_cfd_accurate(velocity, aoa, fluid, preset) — HIGH-FIDELITY OpenFOAM RANS (k-omega SST + prism
  layers). The TRUSTWORTHY absolute Cd/Cl path (validated ~10-15% of experiment) vs run_cfd_flow's
  coarse LBM estimate. Requires OpenFOAM (WSL/native); runs for MINUTES, always background → poll
  get_job(job_id) (live progress). preset coarse|medium|fine|xfine. Use run_cfd_flow first to scan
  trends fast, then run_cfd_accurate to certify the chosen design.

Heavy solves: run_cfd_flow / run_fea_* accept background=True (run_cfd_accurate is always
background) → return a job_id immediately; poll get_job(job_id) for the result. This keeps a long
solve from tripping the transport timeout (which would otherwise drop the session). After a
restart, recover_parts() reloads auto-saved docs.
"""

_OVERVIEW_HEADER = """\
AI-CAD MODELING GUIDE (C++ parametric engine — same as the desktop app).

WORKFLOW
  1. new_part(name)                         start an editable part document.
  2. add_features([ ...ops... ])            build with standard features (vocabulary below).
                                            Returns per-feature status + body count + volume
                                            so you can see a dropped cut or a stray body.
  3. render_part()                          SEE it — an annotated, colour-coded image (faces
                                            tinted + labelled TOP/FRONT/RIGHT...). Self-review
                                            against the request; iterate add_features to fix.
  4. measure_part() / query_geometry(q)     check mass/bbox, or explore faces/edges.
  5. run_fea_static(...) / run_cfd_flow(...) prove it (see cad_guide("simulation")).
  6. save_part()                            save the NATIVE .step+ — the editable feature tree
                                            PLUS its simulation studies + any drawings, in one
                                            file the desktop app reopens fully editable. This is
                                            AI-CAD's native format; save to it to hand the user a
                                            part they can keep working on (not a flattened mesh).
  7. export_part(fmt) / make_drawing()      hand off a NON-native interchange/mesh file
                                            (STEP/IGES/BREP exact, STL/OBJ/3MF mesh) or a
                                            dimensioned 2D drawing.

Address geometry in PLAIN WORDS (no coordinates, no face numbers): "the top face", "all
edges", "the vertical edges", "the largest round face". The engine resolves the words to
topology. cad_guide("review") explains how to read the annotated render. The full op
vocabulary, with worked examples, follows.

================================================================================
"""


_GSL_GUIDE = """\
GEOMETRY-SEMANTICS LAYER — read a part WITHOUT drowning in a flat face dump.

On a complex/imported part, do NOT page through query_geometry's whole face list. Instead:
  describe_part()        a compact NAMED outline: bbox, the feature groups, any detected
                         symmetry/instanced PATTERN (e.g. "6-fold rotational about +Z" with the
                         6 blade faces collapsed to one line), the bores/holes and flat faces by
                         side, and a stable `sid` for every addressable face/edge. Token cost
                         scales with the number of DISTINCT roles, not the raw face count.
  hover(sid)             expand ONE face/edge by the sid describe_part / query_geometry gave —
                         its full card (surf, side, signed normal, concavity bore/boss, radius).
  query_adjacency()      the face-adjacency graph: which faces touch, and whether each shared
                         edge is a CONVEX outside-corner or a CONCAVE inside/pocket edge. This is
                         how you reason about "the edge between the top and front face".

Every query_geometry/describe_part match now also carries: `side` (top/bottom/left/right/front/
back), `normal` (SIGNED outward dir — unlike the unsigned `axis`), `concavity` (bore vs boss),
`radius`, and a stable `sid`. add_features returns a `changes` write-diff ("added 1 cylinder; 1
new bore; volume -3927") so you can confirm an op did what you meant instead of guessing.
"""

_INTAKE_GUIDE = """\
INTAKE / FIRST-TRY-FIT — get real-world parts to fit on the first print.

NORTH STAR: never silently guess a dimension that decides whether parts FIT. Spend questions only
on the dims that gate a mating interface; default everything else and flag it.

WHEN the design must incorporate a real part you have NO model/datasheet for:
  1. LOOK IT UP FIRST — list_standard_parts / make_standard_part. Only a real miss triggers photos.
  2. resolve_process(request) — pick the manufacturing process (FDM 3D-printing is the default).
     If it returns ambiguous, ASK the user the one `clarification` question (e.g. sheet-metal vs
     CNC for a flat metal plate); then set_process(process). The process sets the fit clearances.
  3. request_part_photos(label, critical_features) — ask for photos WITH a scale reference (a
     printable scale card / calipers-in-frame / ruler) in the SAME PLANE as the feature, plus the
     ortho views + close-ups it lists. The user replies with photos next turn.
  4. submit_part_photos(claims) — report the dims YOU read from the photos as structured claims
     (with scale_source / in_plane / agrees_across_views / edge_quality). It scores each and
     returns `open_demands`: the fit-critical dims whose confidence is too low for their fit.
  5. confirm_dimensions([{key,value_mm}]) — get the user to TYPE a caliper number for each demand
     (a press/slip fit won't work off a photo guess). When the gate clears, build the geometry.
  6. Build the RECEPTACLE to fit, not a copy — the process clearance is already in the numbers.
  7. fit_report() to review modeled-vs-measured + clearances before export; dfm_check() for wall/
     draft vs the process minimums; make_fit_coupon(key) to print a tiny test of one interface
     first. All of this persists in the .step+ (reopens in-app intact).
"""


def cad_guide_text(topic: str = "overview") -> str:
    topic = (topic or "overview").strip().lower()
    if topic in ("overview", "features", "ops", "geometry", "vocabulary", "vocab"):
        return _OVERVIEW_HEADER + _read_prompt("shared/ops_cheatsheet.md")
    if topic in ("review", "render", "vision"):
        return _read_prompt("reviewer.md") or "(reviewer guide unavailable)"
    if topic in ("simulation", "sim", "fea", "cfd"):
        return _SIM_GUIDE
    if topic in ("intake", "fit", "measure", "manufacturing", "dfm", "photos"):
        return _INTAKE_GUIDE
    if topic in ("gsl", "describe", "outline", "digest", "adjacency"):
        return _GSL_GUIDE
    # Otherwise treat the topic as a per-feature cheatsheet name (hole, fillet, revolve, ...).
    sheet = _read_prompt(f"shared/cheatsheets/{topic}.md")
    if sheet:
        return sheet
    avail = sorted(p.stem for p in (_prompts_dir() / "shared" / "cheatsheets").glob("*.md")) \
        if (_prompts_dir() / "shared" / "cheatsheets").is_dir() else []
    return (f"No guide for {topic!r}. Topics: overview, simulation, review; or a feature "
            f"cheatsheet: {avail}")


# --------------------------------------------------------------------------- register

def register_cad_tools(mcp: FastMCP) -> None:
    """Attach the C++ feature-engine + simulation tools to the FastMCP server."""

    # ---------------------------------------------------------------- knowledge

    @mcp.tool()
    def cad_guide(topic: str = "overview") -> str:
        """Return the engine's modeling/simulation knowledge so you can drive it well. CALL FIRST.

        This is the same closed op vocabulary + worked examples the in-app agent's modeler is
        taught. topic="overview" (default) gives the workflow + full op cheatsheet (primitives,
        sketch+extrude/revolve/loft/sweep, fillet/chamfer/shell/draft, holes, bolt_circle,
        patterns/mirror, edits) and the plain-English geometry-query predicates. Other topics:
        "simulation" (FEA/CFD how-to), "review" (how to read the annotated render), or a single
        feature's cheatsheet by name ("hole", "fillet", "revolve", "loft", "sweep", "shell",
        "chamfer", "pattern", "mirror", "sketch", "airfoil", "drawing", "hardware", ...).
        """
        return cad_guide_text(topic)

    # ---------------------------------------------------------------- modeling

    @mcp.tool()
    def new_part(name: str = "part") -> str:
        """Create a new, empty editable part document and make it current. Returns its doc_id.

        Follow with add_features(...) to build geometry. Each part is a real parametric feature
        tree (undo/redo, editable parameters), not a one-shot mesh.
        """
        try:
            r = cb.call("doc_new", {"kind": "part", "name": name})
            did = r.get("doc_id")
            _CUR["doc_id"] = did
            # Seed the write-diff baseline: an empty part has zero volume, so the FIRST
            # add_features reports a real volume delta ("0.0 -> X (+X)") instead of degrading
            # to the volume-only "volume now X" branch of _write_diff.
            _LAST_VOLUME[did] = 0.0
            return _dumps({"ok": True, "doc_id": did, "name": name,
                           "revision": r.get("revision", 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_features(ops: list, doc_id: Optional[str] = None, background: bool = False) -> str:
        """Apply a list of document ops to build/edit the part (the core modeling tool).

        `ops` is a JSON array of ops in the vocabulary from cad_guide() — e.g.
          [{"op":"add_feature","feature":{"type":"box","name":"Block",
              "params":{"length":{"value":40,"expr":null},"width":{"value":30,"expr":null},
                        "height":{"value":20,"expr":null}}}},
           {"op":"add_feature","feature":{"type":"fillet","name":"Round",
              "params":{"radius":{"value":3,"expr":null}},"refs":{"edges":{"of":"edge"}}}}]
        Dimensions are objects {"value":<n>,"expr":null}. Geometry is addressed in plain words
        via `refs` (e.g. {"face":{"of":"face","side":"top"}}). Ops in one call may reference an
        earlier op in the SAME array as "$<index>" (0-based); existing tree features by id ("f1").

        Returns revision, body_count, volume_mm3, and per-feature status (ok / error / warning)
        so you can immediately tell a dropped cut, a failed reference, or a stray extra body.
        Then call render_part() to LOOK at the result. On a validation error the whole batch is
        rejected — read `error`, fix the ops, call again.

        HEAVY OPS: a costly op (a global fillet/chamfer/shell/boolean on a many-edged body) is
        run on a worker thread and AUTO-HANDED-OFF to the background if it overruns the call
        budget, so it can never block past the MCP transport timeout (which would otherwise drop
        the whole session). If that happens you get {job_id, status:"running"} — poll get_job().
        All open docs are autosaved BEFORE the op, so nothing is lost even on a hard timeout.
        Pass background=True to hand off immediately for an op you already know is heavy.
        """
        try:
            d = _doc(doc_id)

            def _apply() -> dict:
                # Transactional batch: snapshot first so a partial failure rolls the WHOLE batch
                # back (the engine otherwise leaves failed features in the tree). All-or-nothing.
                ckpt = None
                try:
                    ckpt = cb.call("doc_checkpoint", {"doc_id": d}).get("checkpoint")
                except Exception:  # noqa: BLE001
                    ckpt = None

                # GSL Stage 1a — snapshot the PRE-op topology for the write-diff (W2).
                before_hist = _face_hist(d)
                before_vol = _LAST_VOLUME.get(d)

                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": ops})
                fs = (r.get("rebuild") or {}).get("feature_status", [])
                feats = [{"id": s.get("id"), "ok": s.get("ok"),
                          "error": s.get("error"), "warning": s.get("warning")} for s in fs]
                bodies = (r.get("tess") or {}).get("bodies", [])
                ok = all(s.get("ok") for s in fs) if fs else True

                if not ok and ckpt:
                    # Roll the tree back to the pre-batch revision; report WHY each feature failed.
                    reverted = False
                    try:
                        cb.call("doc_revert", {"doc_id": d, "checkpoint": ckpt})
                        reverted = True
                    except Exception:  # noqa: BLE001
                        reverted = False
                    fail = [{"id": s.get("id"), "error": s.get("error")} for s in fs
                            if not s.get("ok")]
                    _publish_current(d)  # mirror the rolled-back (or current) state to the app
                    return {
                        "ok": False,
                        "doc_id": d,
                        "rolled_back": reverted,
                        "error": "batch rejected — no features were kept; fix and resend"
                                 if reverted else "batch failed and rollback failed",
                        "failed_features": fail,
                        "features": feats,
                    }

                out = {
                    "ok": ok,
                    "doc_id": d,
                    "revision": r.get("revision"),
                    "body_count": len(bodies),
                    "volume_mm3": r.get("volume"),
                    "features": feats,
                }
                # GSL Stage 1a — SEMANTIC write-confirmation (W2): say what the op actually did
                # ("added 2 faces (+2 cylinder); 2 new bore/hole(s); volume 24000→23435") or warn
                # "no geometry change" so the model isn't left diffing a bare volume number.
                vol_after = r.get("volume")
                if ok:
                    try:
                        out["changes"] = _write_diff(before_hist, _face_hist(d), before_vol,
                                                     vol_after)
                    except Exception:  # noqa: BLE001
                        out["changes"] = {
                            "summary": "geometry changed but write-diff unavailable",
                            "faces_delta": None,
                        }
                    if isinstance(vol_after, (int, float)):
                        _LAST_VOLUME[d] = vol_after
                # Push the updated part to the desktop app's viewport (if it's open) so the user
                # watches it build. Best-effort; only when something actually got built.
                if ok and bodies:
                    pub = _publish_current(d)
                    if pub:
                        out["shown_in_app"] = True
                    _autosave(d)  # persist so a server restart can recover this doc
                return out

            if background:
                jid = _start_job("add_features", _apply)
                return _dumps({"ok": True, "doc_id": d, "job_id": jid, "status": "running",
                               "note": f"applying {len(ops)} op(s) in the background — "
                                       f"poll get_job('{jid}')."})
            # Run on a worker thread with auto-handoff: a fast batch returns inline exactly as
            # before; a heavy one that overruns the budget keeps running and returns a job_id so
            # the transport call returns in time and the session survives.
            return _dumps(_solve_sync_or_handoff("add_features", _apply))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def get_part(doc_id: Optional[str] = None, summary: bool = False) -> str:
        """Return the feature tree (features, parameters, ids) + revision for editing.

        summary=True returns a LIGHTWEIGHT tree — feature id/name/type/suppressed + parameters,
        with heavy embedded geometry (the import features' base64 BREP blobs) and any oversized
        string value ELIDED. Use it on big assemblies/imported parts where the full tree blows
        the tool token limit (a 600k-char part lists in a few KB). The full (default) form
        carries the complete params verbatim for round-trip editing.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_tree", {"doc_id": d})
            if not summary:
                return _dumps(r)

            def _trim(value: Any) -> Any:
                # Elide base64 geometry blobs / very long strings that bloat the tree but carry
                # no design intent the caller would read in a summary.
                if isinstance(value, str) and len(value) > 200:
                    return f"<{len(value)} chars elided>"
                if isinstance(value, dict):
                    return {k: ("<elided>" if k in ("embedded_brep",) else _trim(v))
                            for k, v in value.items()}
                if isinstance(value, list):
                    return [_trim(v) for v in value] if len(value) <= 64 else \
                        f"<list of {len(value)} elided>"
                return value

            tree = r.get("tree") or {}
            feats = tree.get("features") or []
            out_feats = []
            for f in feats:
                if not isinstance(f, dict):
                    continue
                params = {k: ("<elided>" if k == "embedded_brep" else _trim(v))
                          for k, v in (f.get("params") or {}).items()}
                out_feats.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "suppressed": bool(f.get("suppressed", False)),
                    "params": params,
                })
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "summary": True, "feature_count": len(out_feats),
                           "features": out_feats})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def query_geometry(query: Union[str, dict], doc_id: Optional[str] = None) -> str:
        """Resolve a plain-English face/edge query against the part — explore/confirm geometry.

        `query` is a geometry query like {"of":"face","side":"top"}, {"of":"edge","parallel":"z"},
        {"of":"face","surf":"round","pick":"outer"} — or a bare side word ("top") for a face.
        Returns each matching face/edge with its shape (surf), side, centroid and size, so you can
        confirm "the top face" resolves before you reference it in a feature or a simulation.
        """
        try:
            q = query if isinstance(query, dict) else _as_face_query(query)
            return _dumps(cb.call("doc_query", {"doc_id": _doc(doc_id), "query": q}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def render_part(doc_id: Optional[str] = None) -> Image:
        """Render the current part to an ANNOTATED image and return it so you can SEE the result.

        The render is colour-coded and labelled exactly like the in-app vision reviewer: an XYZ
        orientation gizmo, flat faces tinted by direction with the principal ones labelled
        (TOP +Z blue, BOTTOM −Z teal, FRONT −Y yellow, BACK +Y green, RIGHT +X red, LEFT −X
        orange), curved faces left grey. Use it to verify features landed on the face the request
        named and the proportions look right. See cad_guide("review") for the full key.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        r = cb.call("doc_render_png", {"doc_id": d, "out_path": png})
        if (not r.get("ok") or not os.path.exists(png)):
            # A 2D DRAWING doc has no 3D bodies — rasterize its views instead so the AI
            # can still self-review the drawing it just made (Bug D-1).
            try:
                rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
            except Exception:  # noqa: BLE001
                rd = {"ok": False}
            if rd.get("ok") and os.path.exists(png):
                return Image(path=png)
            raise ValueError(f"Render failed: {r.get('reason', 'no bodies to render')}")
        return Image(path=png)

    @mcp.tool()
    def measure_part(doc_id: Optional[str] = None, density_g_cm3: float = 7.85) -> str:
        """Mass properties of the current part: volume (mm^3), mass (g), centre of mass, inertia.

        density_g_cm3 sets the material density used for mass (default 7.85 = steel). Also runs a
        geometry sanity check (closed/valid solid).

        Multi-body docs: the top-level volume/mass/com/inertia are the COMBINED total of ALL
        bodies, and `mass.bodies[]` lists each body's own properties (with `mass.body_count`).
        Pass body_id via a direct query if you need just one. (Previously this silently reported
        only the last body of an assembly.)
        """
        try:
            d = _doc(doc_id)
            mass = cb.call("eval_mass", {"doc_id": d, "density_g_cm3": density_g_cm3})
            out = {"ok": True, "doc_id": d, "mass": mass}
            try:
                out["check"] = cb.call("eval_check", {"doc_id": d})
            except CoreError:
                pass
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- inspection

    @mcp.tool()
    def measure_between(a_query: Union[str, dict], b_query: Optional[Union[str, dict]] = None,
                        doc_id: Optional[str] = None) -> str:
        """Measure a face/edge — or the distance + angle BETWEEN two of them — in plain words.

        This is the engineer's measure/probe tool. Address geometry the same way you do
        everywhere else (a side word like "top", or a full query {"of":"face","side":"top"} /
        {"of":"edge","parallel":"z","pick":"largest"}).

        One query (b_query omitted) -> that single feature's intrinsic measure:
          * a face   -> its area (mm^2)
          * an edge  -> its length (mm), or radius (mm) for a circular edge
          * a vertex -> its point coordinates
        Two queries -> the minimum DISTANCE (mm) between them (with the two closest points) and,
        when applicable (face↔face normals or edge↔edge tangents), the ANGLE (degrees, 0..90).

        Each query must resolve to exactly ONE face/edge. If it matches several (e.g. "all the
        vertical edges"), narrow it with a predicate ("pick":"largest"/"side":"top"/…) — call
        query_geometry(query) first to see the candidates. Returns a readable `summary` line.
        """
        try:
            d = _doc(doc_id)
            ref_a = _resolve_one_selector(d, a_query)
            refs = [ref_a]
            if b_query is not None:
                refs.append(_resolve_one_selector(d, b_query))
            res = cb.call("eval_measure", {"doc_id": d, "refs": refs})
            kind = res.get("kind")
            out = {"ok": True, "doc_id": d, "a_query": a_query, **res}
            if b_query is not None:
                out["b_query"] = b_query
                parts = []
                if "distance" in res:
                    parts.append(f"distance {res['distance']:.4g} mm")
                if "angle" in res:
                    parts.append(f"angle {res['angle']:.4g} deg")
                out["summary"] = ("between the two selections: " + ", ".join(parts)) if parts \
                    else "no distance/angle computed"
            else:
                if kind == "area":
                    out["summary"] = f"area = {res.get('area'):.6g} mm^2"
                elif kind == "length":
                    out["summary"] = f"length = {res.get('length'):.6g} mm"
                elif kind == "radius":
                    out["summary"] = f"radius = {res.get('radius'):.6g} mm"
                elif kind == "point":
                    out["summary"] = f"point = {res.get('points')}"
                elif kind == "volume":
                    out["summary"] = f"volume = {res.get('volume'):.6g} mm^3"
                else:
                    out["summary"] = f"measured ({kind})"
            return _dumps(out)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def check_interference(doc_id: Optional[str] = None) -> str:
        """Clash/clearance check — do the bodies OVERLAP, and if not, how tight is the gap?

        Runs a pairwise interference analysis over every body in the document (for an assembly
        doc, over the world-placed component bodies). For each pair it reports either an
        INTERFERENCE (overlap volume in mm^3 — a real collision the design must not have) or a
        CLEARANCE (minimum gap in mm). Use it to validate that mating/instanced parts fit without
        clashing, or to find the tightest gap in an assembly.

        Returns {ok, summary, parts:[{name,bbox,volume}], interferences:[{a,b,volume}],
        clearances:[{a,b,distance}]}. ok is True only when NOTHING interferes. A single-body part
        trivially reports no interference (build a multi-body part with the `transform` copy
        feature, or open an assembly, to get meaningful pairs).
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_interference", {"doc_id": d})
            return _dumps({"ok": res.get("ok", True), "doc_id": d, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def check_symmetry(plane: str = "top", doc_id: Optional[str] = None) -> str:
        """Is the part mirror-symmetric about a principal datum plane? Returns the deviation.

        plane : "top" (XY, mirror across Z), "front" (XZ, mirror across Y), or "right" (YZ,
                mirror across X) — the reserved AI-CAD datum planes (Z-up).
        Mirrors the active body across the plane and measures the symmetric-difference volume.
        Returns {symmetric: bool, deviation_mm3}: symmetric is True when the leftover volume is
        within a tiny tolerance (1e-3 of the part volume). Handy to confirm a feature you meant
        to place symmetrically really is, or to pick a sensible mirror plane.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_symmetry", {"doc_id": d, "plane": plane})
            sym = res.get("symmetric")
            dev = res.get("deviation_mm3")
            summary = (f"symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)" if sym
                       else f"NOT symmetric about plane '{plane}' (deviation {dev:.4g} mm^3)")
            return _dumps({"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def inspect_section(plane: str = "top", doc_id: Optional[str] = None) -> str:
        """Cut the part with a datum plane and report the CROSS-SECTION area + section moments.

        This is the "section the part to inspect its internals" probe. It slices the active
        body with one of the reserved datum planes and computes the real properties of the
        resulting cut face — the cross-section a draftsman would dimension.

        plane : "top" (XY, normal +Z), "front" (XZ, normal -Y), or "right" (YZ, normal +X) —
                the reserved AI-CAD datum planes (Z-up). The cut passes through the origin.

        Returns {ok, area_mm2, centroid:[x,y,z], Ixx, Iyy, Ixy, summary}:
          * area_mm2 — the cross-section area (mm^2), the headline number. For a solid this is
            the filled region; for a thin/hollow wall it's the material the plane cuts through.
          * centroid — the area centroid of the section, in world coordinates.
          * Ixx/Iyy/Ixy — the section second moments of area (mm^4), expressed in the cutting
            plane's frame (use for bending stiffness / section modulus reasoning).
        Use it to verify a beam's cross-section, check a wall's cut area, or feed a hand
        bending calc. A plane that misses the body (empty cut) returns area 0.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_section_props", {"doc_id": d, "plane": plane})
            area = res.get("area_mm2", 0.0)
            if area and area > 0:
                summary = f"cross-section area at plane '{plane}' = {area:.6g} mm^2"
            else:
                summary = (f"plane '{plane}' produced no closed cross-section (the cut may miss "
                           f"the body or only graze an edge) — area = 0")
            return _dumps({"ok": True, "doc_id": d, "plane": plane, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def wall_thickness(doc_id: Optional[str] = None) -> str:
        """How THIN is the thinnest wall? Inward ray-cast wall-thickness over the whole part.

        From every surface sample it casts a ray straight into the material and measures the
        distance to the far wall — the local wall thickness. The headline value is the MINIMUM
        ("the thinnest wall is X mm"), the number that governs whether a part can be molded,
        machined, or 3D-printed, and where it's likely to be weak or warp.

        Returns {ok, min_thickness_mm, mean_thickness_mm, max_thickness_mm, n_sampled,
        aicm_path, summary}:
          * min_thickness_mm — the thinnest wall found (the key value). null if no sample got a
            through-wall hit (e.g. an open sheet body with no opposite wall).
          * mean/max — the average and thickest sampled wall.
          * aicm_path — a temp field file for the in-app thickness colour-map viewer.
        Compare min_thickness_mm against your process minimum (e.g. ~1 mm injection wall,
        ~0.8 mm FDM) to flag thin spots before manufacturing.
        """
        try:
            d = _doc(doc_id)
            res = cb.call("eval_thickness", {"doc_id": d})
            mn = res.get("min_thickness_mm")
            if mn is not None:
                mean = res.get("mean_thickness_mm")
                summary = (f"thinnest wall = {mn:.4g} mm"
                           + (f" (mean {mean:.4g} mm)" if mean is not None else ""))
            else:
                summary = ("no through-wall thickness could be measured (open/sheet body, or no "
                           "opposite wall in line of sight)")
            return _dumps({"ok": True, "doc_id": d, "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def draft_analysis(pull_dir: Optional[list] = None, min_draft_deg: float = 3.0,
                       doc_id: Optional[str] = None) -> str:
        """Moldability check: which faces lack enough DRAFT to release from the mold?

        For a given mold-pull (ejection) direction it measures each face's draft angle — how far
        its surface tilts away from the pull axis. Faces whose worst-case draft is below
        min_draft_deg would drag on the tool during ejection and need a draft feature added.

        pull_dir : the mold-opening / ejection direction as [x,y,z] (defaults to +Z, [0,0,1]).
        min_draft_deg : the minimum acceptable draft angle in degrees (default 3°, a common
                        injection-molding minimum).

        Returns {ok, min_draft_deg, max_draft_deg, threshold_deg, n_faces_below_threshold,
        faces_below_threshold:[{face_id, min_draft_deg, area_mm2, centroid}], aicm_path,
        summary}. n_faces_below_threshold == 0 means every face clears the threshold (good to
        mold along that direction). The faces_below_threshold list (centroid + area) tells you
        WHERE to add draft. aicm_path is the colour-map field for the in-app viewer.
        """
        try:
            d = _doc(doc_id)
            pull = pull_dir if (isinstance(pull_dir, (list, tuple)) and len(pull_dir) == 3) \
                else [0.0, 0.0, 1.0]
            res = cb.call("eval_draft",
                          {"doc_id": d, "pull": [float(x) for x in pull],
                           "angle_deg": float(min_draft_deg)})
            n_bad = res.get("n_faces_below_threshold", 0)
            thr = res.get("threshold_deg", min_draft_deg)
            if n_bad == 0:
                summary = (f"all faces have >= {thr:g}deg draft along pull {list(pull)} — "
                           f"moldable in that direction")
            else:
                summary = (f"{n_bad} face(s) below the {thr:g}deg draft minimum along pull "
                           f"{list(pull)} — add draft there before molding")
            return _dumps({"ok": True, "doc_id": d, "pull_dir": list(pull),
                           "summary": summary, **res})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def undo(doc_id: Optional[str] = None) -> str:
        """Undo the last edit on the current part."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_undo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def redo(doc_id: Optional[str] = None) -> str:
        """Redo the last undone edit on the current part."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_redo", {"doc_id": d})
            _publish_current(d)
            return _dumps(r)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def show_in_app(doc_id: Optional[str] = None) -> str:
        """Push the current part to the OPEN AI-CAD desktop app's 3D viewport so the user can see it.

        Modeling tools already do this automatically after each change; call this to force a refresh
        (e.g. after switching the current part). No-op effect if the desktop app isn't running — the
        part is written to the handoff folder and shown whenever the app next opens.
        """
        try:
            d = _doc(doc_id)
            pub = _publish_current(d)
            if not pub:
                return _dumps({"ok": False, "error": "could not publish (no geometry yet?)"})
            return _dumps({"ok": True, "doc_id": d, **pub})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_parts() -> str:
        """List all open part/drawing documents in this engine session."""
        try:
            return _dumps(cb.call("doc_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def close_part(doc_id: Optional[str] = None) -> str:
        """Close a part document and free it. Clears the current part if it was current."""
        try:
            d = _doc(doc_id)
            r = cb.call("doc_close", {"doc_id": d})
            if _CUR["doc_id"] == d:
                _CUR["doc_id"] = None
            return _dumps({"ok": True, "closed": r.get("closed", True), "doc_id": d})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def save_part(path: Optional[str] = None, doc_id: Optional[str] = None) -> str:
        """Save the part as AI-CAD's NATIVE .step+ — the editable, reopenable format. PREFER THIS.

        .step+ is the only format that round-trips the full PARAMETRIC feature tree (so the user
        can keep editing parameters/features), its SIMULATION STUDIES (FEA/CFD setups + results),
        and any associated 2D drawings — all in one file the desktop app opens fully editable. Use
        this to deliver a part the user will keep working on. (export_part writes flat interchange
        or mesh files: a plain STEP/STL has geometry only, no feature history or studies.)

        path : where to write. If omitted, a temp .step+ is written. A ".step+" extension is added
               if missing. Returns the written path and bytes so you can confirm a real save.
        """
        try:
            d = _doc(doc_id)
            # K3: canonicalise the caller-supplied path so any '..' traversal sequences are
            # fully resolved before we pass them to the engine.  This means the caller sees
            # exactly where the file will be written (no hidden navigation).
            out = _sanitize_output_path(path) if path else _tmp(".step+")
            if not out.lower().endswith(".step+"):
                out += ".step+"
            r = cb.call("doc_save", {"doc_id": d, "path": out})
            written = r.get("path", out)
            ok = os.path.exists(written)
            return _dumps({"ok": ok, "format": "step+", "native": True, "doc_id": d,
                           "path": written,
                           "bytes": (os.path.getsize(written) if ok else 0)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def export_part(fmt: str = "step", path: Optional[str] = None,
                    doc_id: Optional[str] = None) -> str:
        """Export the current part to an interchange/mesh file; returns the written path.

        fmt: "step+" (native) | "step" | "stl" | "3mf" | "iges" | "brep" | "obj" | "ply" |
             "gltf" | "glb" | "3dm". If `path` is omitted, a temp file with the right extension is
             written. STEP/IGES/BREP are exact B-rep; STL/OBJ/PLY/glTF are tessellated meshes.

        NOTE: plain STEP/STL/etc. carry geometry only — NO editable feature tree or simulation
        studies. To hand the user a part they can keep editing, use save_part() (native .step+).
        fmt="step+" here is accepted as an alias and routed to the native saver.
        """
        try:
            d = _doc(doc_id)
            fmt = fmt.lower().lstrip(".")
            # The native editable container is written by doc_save, not the interchange writer.
            if fmt in ("step+", "stepplus", "stp+", "native"):
                # K3: canonicalise caller-supplied path before passing to the engine.
                out = _sanitize_output_path(path) if path else _tmp(".step+")
                if not out.lower().endswith(".step+"):
                    out += ".step+"
                r = cb.call("doc_save", {"doc_id": d, "path": out})
                written = r.get("path", out)
                return _dumps({"ok": os.path.exists(written), "format": "step+",
                               "native": True, "doc_id": d, "path": written,
                               "exists": os.path.exists(written)})
            # K3: canonicalise caller-supplied path before passing to the engine.
            out = _sanitize_output_path(path) if path else _tmp("." + fmt)
            # The engine round-trips B-rep formats (re-reads + BRepCheck) and THROWS if the file is
            # truncated/malformed, so a returned ok:True means it actually re-opens. It echoes real
            # metadata (solid_count, valid_solids, bytes, validated) — surface it so the caller can
            # trust the export instead of a bare exists flag.
            r = cb.call("export_file", {"doc_id": d, "path": out, "format": fmt})
            written = r.get("path", out)
            res = {"ok": True, "format": fmt, "doc_id": d, "path": written,
                   "exists": os.path.exists(written)}
            for k in ("bytes", "solid_count", "valid_solids", "expected_solids", "validated"):
                if k in r:
                    res[k] = r[k]
            return _dumps(res)
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def import_part(path: str, into: Optional[str] = None, as_solid: bool = False,
                    resolve_features: bool = True) -> str:
        """Import a CAD/mesh file (STEP/IGES/BREP/STL/3MF/OBJ/STEP+/…) as a body.

        Lets you assemble components built or exported separately, or recover geometry from a file
        after a session reset. `into` = an existing doc_id to add the body to the CURRENT part
        (for assembly); omit it to open the file as a NEW part (made current). `as_solid` sews a
        mesh (STL/OBJ/…) into a solid on import; B-rep formats already import as solids.

        SOLIDWORKS (.sldprt): with `resolve_features=True` (default) and no `into`, the part is
        opened by RECONSTRUCTING its SolidWorks feature tree (sketches with relations, reference
        planes, extrude/cut/revolve/loft, …) into a fully EDITABLE AI-CAD part — not a frozen
        body. This needs SolidWorks installed locally (driven via its COM API). If any feature
        can't be faithfully rebuilt the import is REFUSED with the exact list of what blocked it
        (so the gap can be fixed) — it does NOT silently fall back to a dumb solid. Set
        `resolve_features=False` to force the legacy COM→STEP single-body import instead.

        TIP: pair with the `transform` feature (in add_features) to POSITION each imported body
        into a shared coordinate frame — translate:[x,y,z] and/or angle:<deg> with rotate:[ax,ay,az].
        """
        try:
            # K2: validate the path exists BEFORE calling the engine so we never open a
            # zombie doc.  The engine's import_file does not distinguish "file missing" from
            # "format unsupported" and may silently create an empty document.
            if not os.path.isfile(path):
                return _dumps({"ok": False, "error": f"file not found: {path}"})
            # SolidWorks feature-tree resolution (opening as a NEW editable part/assembly).
            if resolve_features and not into and path.lower().endswith((".sldprt", ".sldasm")):
                from engine.doc.sw_feature_import import SolidWorksUnavailable
                from engine.doc.sw_translate import resolve as _sw_resolve
                try:
                    plan = _sw_resolve(path)
                except SolidWorksUnavailable as e:
                    return _dumps({"ok": False, "error": str(e),
                                   "hint": "Open the part in SolidWorks and Save As STEP, then "
                                           "import that — or call import_part(resolve_features="
                                           "False) for a single-body COM→STEP import."})
                except Exception as e:  # noqa: BLE001
                    return _dumps({"ok": False, "error":
                                   f"SolidWorks feature extraction failed: {e}"})
                if plan["blocking"]:
                    # Strict-fidelity contract: refuse rather than import a quietly-wrong model.
                    return _dumps({
                        "ok": False,
                        "error": "cannot fully resolve this SolidWorks part's feature tree — "
                                 f"{len(plan['blocking'])} feature(s) are not supported yet",
                        "unsupported": plan["blocking"],
                        "degraded": plan["degraded"],
                        "hint": "These features need importer support before this part can be "
                                "rebuilt faithfully. Use import_part(resolve_features=False) to "
                                "fall back to a single-body COM→STEP import in the meantime."})
                if plan.get("is_assembly"):
                    # Build each DISTINCT component part once -> save a .step+ (keeps its editable
                    # feature tree) -> place every instance in an ASSEMBLY doc via
                    # asm_insert_component with its 4x4 transform (the engine's native assembly model).
                    comps = plan["components"]
                    if not comps:
                        return _dumps({"ok": False, "error":
                                       "no reconstructable components in this assembly",
                                       "degraded": plan["degraded"]})
                    # Component .step+ sidecars live in a cache dir (NOT the user's source folder);
                    # the assembly references them, so they must persist for the session.
                    sidecar = os.path.join(tempfile.gettempdir(), "aicad_sw_components",
                                           plan["name"])
                    os.makedirs(sidecar, exist_ok=True)
                    built: dict = {}   # part_key -> .step+ path
                    for c in comps:
                        key = c["part_key"]
                        if key in built:
                            continue
                        cd = cb.call("doc_new", {"name": os.path.basename(key)}).get("doc_id")
                        cb.call("doc_apply_ops", {"doc_id": cd, "ops": c["ops"]})
                        stem = os.path.splitext(os.path.basename(key))[0]
                        cpath = os.path.join(sidecar, stem + ".step+")
                        cb.call("doc_save", {"doc_id": cd, "path": cpath})
                        cb.call("doc_close", {"doc_id": cd})
                        built[key] = cpath
                    ad = cb.call("doc_new", {"name": plan["name"],
                                             "kind": "assembly"}).get("doc_id")
                    asm_ops = [{"op": "asm_insert_component", "name": c["name"],
                                "source": {"path": built[c["part_key"]]},
                                "transform": c["xform16"]} for c in comps]
                    cb.call("doc_apply_ops", {"doc_id": ad, "ops": asm_ops})
                    try:
                        cb.call("doc_solve_mates", {"doc_id": ad})
                    except CoreError:
                        pass
                    _CUR["doc_id"] = ad
                    _publish_current(ad)
                    return _dumps({
                        "ok": True, "doc_id": ad, "imported_from": path, "format": "sldasm",
                        "feature_resolved": True, "is_assembly": True,
                        "component_count": len(comps), "distinct_parts": len(built),
                        "feature_count": plan["feature_count"], "degraded": plan["degraded"]})
                d = cb.call("doc_new", {"name": plan["name"]}).get("doc_id")
                r = cb.call("doc_apply_ops", {"doc_id": d, "ops": plan["ops"]})
                _CUR["doc_id"] = d
                _publish_current(d)
                return _dumps({
                    "ok": True, "doc_id": d, "imported_from": path, "format": "sldprt",
                    "feature_resolved": True, "feature_count": plan["feature_count"],
                    "degraded": plan["degraded"],
                    **{k: r[k] for k in ("revision", "body_count", "volume_mm3") if k in r}})
            # A3: .step+ is the engine's NATIVE format. With no `into`, route it through
            # doc_open (the same path used by recover_parts and the host's own file-open) to
            # open it as a fresh part. WITH `into`, the engine's import_file now extracts the
            # container's solids and bakes them as an embedded-BREP import feature on the target
            # doc (Issue 2 — native parts are assemblable without re-exporting to plain STEP).
            if path.lower().endswith(".step+") and not into:
                r = cb.call("doc_open", {"path": path})
                new_id = r.get("doc_id")
                if new_id:
                    _CUR["doc_id"] = new_id
                    _publish_current(new_id)
                    # Seed the write-diff baseline so the first add_features on a freshly
                    # opened part reports a volume delta, not a volume-only "volume now X".
                    vol = r.get("volume")
                    if isinstance(vol, (int, float)):
                        _LAST_VOLUME[new_id] = vol
                return _dumps({"ok": True, "doc_id": new_id, "imported_from": path,
                               "format": "step+", "native": True,
                               **{k: r[k] for k in ("revision", "name", "body_count")
                                  if k in r}})
            params: dict = {"path": path, "as_solid": bool(as_solid)}
            if into:
                params["into"] = into
            r = cb.call("import_file", params)
            new_id = r.get("doc_id") or into
            if new_id and not into:
                _CUR["doc_id"] = new_id
            if new_id:
                _publish_current(new_id)
                # Seed the write-diff baseline when the importer reports a volume, so the
                # first add_features on an imported part shows a delta instead of "volume now X".
                vol = r.get("volume")
                if isinstance(vol, (int, float)):
                    _LAST_VOLUME[new_id] = vol
            return _dumps({"ok": True, "doc_id": new_id, "imported_from": path,
                           "into": into, **{k: r[k] for k in ("revision", "name", "body_count")
                                            if k in r}})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- simulation

    @mcp.tool()
    def run_fea_static(fixtures: list, loads: list, material: Union[str, dict] = "steel",
                       mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                       background: bool = False) -> str:
        """Linear-static stress FEA on the current part. Returns max stress, displacement, and FoS.

        fixtures : list of face queries held fixed, e.g. ["bottom"] or
                   [{"of":"face","side":"bottom","pick":"largest"}].
        loads    : list of {face query + a force or pressure}, e.g.
                   [{"face":"top","force":[0,0,-500]}]   # newtons, world XYZ
                   [{"face":"top","pressure":0.5}]       # MPa, inward
        material : a name ("steel","aluminum","titanium","stainless","brass","abs","pla") or an
                   explicit dict {E_mpa, nu, density_g_cm3, yield_mpa, ...}.
        mesh_fineness : 0..1 (default 0.5). Second-order (tet10) elements are used for accuracy.
        background : if True, return a job_id immediately and solve on a worker thread — poll
                     get_job(job_id). Use it for fine meshes that may exceed the call timeout.

        Returns {max_von_mises_mpa, max_disp_mm, min_fos}. min_fos = yield / max stress; > 1 holds.
        Tip: query_geometry(...) first to confirm your fixture/load faces resolve.
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})

            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            if not fixture_sets:
                return _dumps({"ok": False, "error":
                               "no fixture faces resolved — the face queries you provided "
                               "didn't match any surface points. Static FEA requires at least "
                               "one fixed face. Use query_geometry() to confirm the face query."})

            load_specs: list = []
            for ld in loads:
                if not isinstance(ld, dict) or "face" not in ld:
                    raise ValueError(f"each load needs a 'face' query: {ld!r}")
                faces = _resolve_face_points(d, ld["face"])
                # Merge multiple face-point sets for this load. Keep face_normal + perp_tol
                # from the FIRST face set (they're geometry-based and won't differ between
                # matches of the same query). Without these, nodes_in_faceset falls back to
                # pure Euclidean tol and may bleed across thin-plate faces (Bug Z1/Z2).
                merged: dict = {"points": []}
                for f in faces:
                    merged["points"].extend(f.get("points", []))
                if faces:
                    first = faces[0]
                    if "tol_mm" in first:
                        merged["tol_mm"] = first["tol_mm"]
                    if "face_normal" in first:
                        merged["face_normal"] = first["face_normal"]
                    if "perp_tol_mm" in first:
                        merged["perp_tol_mm"] = first["perp_tol_mm"]
                spec: dict = {"faces": merged}
                if "pressure" in ld:
                    spec["type"] = "pressure"
                    spec["magnitude"] = float(ld["pressure"])
                elif "force" in ld:
                    spec["type"] = "force"
                    spec["vector"] = [float(x) for x in ld["force"]]
                else:
                    raise ValueError(f"load needs 'force':[x,y,z] or 'pressure':<MPa>: {ld!r}")
                load_specs.append(spec)

            study = {
                "step_path": step,
                "material": mat,
                "fixtures": fixture_sets,
                "loads": load_specs,
                "mesh_order": 2,        # tet10 captures bending
                "fineness": mesh_fineness,
            }

            def _solve() -> dict:
                res = cb.call("sim_run_static", {"study": study})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d, "error": res.get("error", "solve failed")}
                # Persist setup+results into the doc so they save inside the .step+.
                _persist_study(d, {
                    "type": "fea_static",
                    "name": f"Static stress ({mat.get('name', material)})",
                    "engine": "cpp",
                    "material": mat,
                    "fixtures": fixtures,
                    "loads": loads,
                    "mesh_fineness": mesh_fineness,
                    "results": res.get("summary", {}),
                    "n_dof": res.get("n_dof"),
                })
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "summary": res.get("summary", {}), "n_dof": res.get("n_dof"),
                        "saved_to_study": True}

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_static", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_static", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_fea_modal(fixtures: list, material: Union[str, dict] = "steel", n_modes: int = 6,
                      mesh_fineness: float = 0.5, doc_id: Optional[str] = None,
                      background: bool = False) -> str:
        """Modal FEA — the natural frequencies (Hz) of the held part. Returns the first n_modes.

        fixtures : list of face queries held fixed (e.g. ["bottom"]). An unconstrained part has
                   rigid-body modes near 0 Hz, so fix it like it is mounted.
        material : name or explicit dict (density drives the mode frequencies).
        background : if True, return a job_id and solve on a worker thread — poll get_job(job_id).
        """
        try:
            d = _doc(doc_id)
            mat = _resolve_material(material)
            step = _tmp(".step")
            cb.call("doc_export_step", {"doc_id": d, "path": step})
            fixture_sets: list = []
            for q in fixtures:
                fixture_sets.extend(_resolve_face_points(d, q))
            # F6 (free-free modal): when fixtures=[] the caller is requesting an unconstrained
            # solve — the C++ fea_modal_solve handles empty fixed_nodes by computing the full
            # 6 rigid-body modes near 0 Hz followed by the elastic modes. Only reject when the
            # caller DID supply fixture queries but NONE of them resolved to real face points
            # (which is almost certainly a geometry-query mistake, not intentional free-free).
            if fixtures and not fixture_sets:
                return _dumps({"ok": False, "error": "no fixture faces resolved — "
                               "the face queries you provided didn't match any surface points. "
                               "Use query_geometry() to find the right face query, or pass "
                               "fixtures=[] for a free-free (unconstrained) solve."})
            # 2nd-order (tet10) elements: quadratic bending accuracy — no spurious split on
            # a square section and no low-frequency bias the way tet4 had (Bug N-1).
            study = {"step_path": step, "material": mat, "fixtures": fixture_sets,
                     "mesh_order": 2, "fineness": mesh_fineness}

            def _solve() -> dict:
                res = cb.call("sim_run_modal", {"study": study, "n_modes": n_modes})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "modal solve failed")}
                _persist_study(d, {
                    "type": "fea_modal",
                    "name": f"Modal ({mat.get('name', material)})",
                    "engine": "cpp",
                    "material": mat,
                    "fixtures": fixtures,
                    "n_modes": n_modes,
                    "mesh_fineness": mesh_fineness,
                    "results": {"freqs_hz": res.get("freqs_hz", []),
                                "n_modes": res.get("n_modes")},
                })
                return {"ok": True, "doc_id": d, "material": mat.get("name", material),
                        "freqs_hz": res.get("freqs_hz", []), "n_modes": res.get("n_modes"),
                        "saved_to_study": True}

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("fea_modal", _solve)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("fea_modal", _solve))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_flow(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                     cells: int = 96, n_steps: int = 2000, doc_id: Optional[str] = None,
                     background: bool = False) -> str:
        """Wind-tunnel CFD over the current part/assembly (LBM). Returns drag, lift, Cd, Reynolds.

        CONVENTIONS (AI-CAD is Z-up): freestream is along +X; LIFT is the +Z (up) force; SPAN is Y.
        +aoa pitches the nose up (rotation about Y), so a cambered wing makes +lift. `reynolds` and
        `cd` use ref_length_mm = the part's LONGEST span (NOT chord) — for an airfoil, simulate the
        wing alone or read `ref_length_mm` and renormalise to chord yourself.

        velocity : free-stream speed (m/s).   aoa : angle of attack (degrees).
        fluid    : "air" or "water".          cells : grid pitch = domain's longest span / cells
                   (higher = finer + slower; practical 48-200).  n_steps : LBM iterations (~500-4000).
        background : if True, return a job_id immediately and solve on a worker thread (recommended
                     for cells>=120 or n_steps>=2500, which can exceed the call timeout) — then poll
                     get_job(job_id).

        RESOLUTION MATTERS: the result reports `voxel_mm`, `min_feature_mm`, and
        `min_feature_voxels` (how many cells span the thinnest part dimension). If a thin wing/tail
        edge spans < ~8 voxels it is UNDER-RESOLVED — lift especially reads near zero — and a
        `warning` is returned; raise `cells` or simulate the thin part on its own. Also reported:
        `converged` + `residual` (force settle over the last fifth of the run); a non-converged run
        is under-iterated, not physically zero.

        ⚠ ABSOLUTE Cd IS UNCALIBRATED. This is a coarse voxel LBM: the staircased surface
        over-counts force (more so on curved/angled faces), so the magnitude of drag_n/cd can be
        off by an order of magnitude and is resolution-dependent. Use it to COMPARE configurations
        and read TRENDS (does +aoa add lift? does a fillet cut drag? A vs B?), which it now gets
        right (drag scales as V^2, Cd is velocity-independent). For a TRUSTWORTHY absolute Cd/Cl,
        use run_cfd_accurate() (OpenFOAM RANS — validated, but minutes per run). Treat these LBM
        numbers as a fast directional estimate, not a certified coefficient.
        """
        try:
            d = _doc(doc_id)
            # Live-progress side-channel: the engine writes {pct,step,n_steps} to this file as the
            # LBM iterates; get_job reads it so a background run reports progress like OpenFOAM.
            prog_path = _tmp(".json")
            prog: dict = {"pct": 0, "stage": "lbm_solve", "progress_file": prog_path}

            def _solve() -> dict:
                res = cb.call("sim_run_flow", {"doc_id": d, "velocity": velocity, "aoa": aoa,
                                               "fluid": fluid, "cells": cells, "n_steps": n_steps,
                                               "progress_path": prog_path})
                if not res.get("ok"):
                    return {"ok": False, "doc_id": d,
                            "error": res.get("error", "flow sim failed")}
                out = {"ok": True, "doc_id": d}
                for k in ("drag_n", "lift_n", "cd", "frontal_area_mm2", "reynolds",
                          "ref_length_mm", "velocity_ms", "aoa_deg", "fluid", "grid",
                          "converged", "residual", "voxel_mm", "min_feature_mm",
                          "min_feature_voxels", "steps_run", "diverged", "warning"):
                    if k in res:
                        out[k] = res[k]
                _persist_study(d, {
                    "type": "cfd_flow",
                    "name": f"CFD wind tunnel ({fluid}, {velocity} m/s, aoa {aoa})",
                    "engine": "cpp-lbm",
                    "params": {"velocity": velocity, "aoa": aoa, "fluid": fluid,
                               "cells": cells, "n_steps": n_steps},
                    "results": {k: out[k] for k in out if k not in ("ok", "doc_id")},
                })
                out["saved_to_study"] = True
                return out

            _autosave_all_open()  # R-1 guard: protect every open doc before the solve starts
            if background:
                jid = _start_job("cfd_flow", _solve, progress=prog)
                return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                               "note": "solving on a worker thread — poll get_job(job_id)"})
            return _dumps(_solve_sync_or_handoff("cfd_flow", _solve, progress=prog))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def run_cfd_accurate(velocity: float = 10.0, aoa: float = 0.0, fluid: str = "air",
                         preset: str = "coarse", doc_id: Optional[str] = None) -> str:
        """HIGH-FIDELITY external-aero CFD via OpenFOAM (steady RANS, k-omega SST + prism layers).

        Use this when you need a TRUSTWORTHY absolute Cd/Cl, not the coarse directional estimate
        from run_cfd_flow (the built-in LBM). This is the validated path — sphere/cube land within
        ~10-15% of experiment. It body-fits a snappyHexMesh with boundary layers and solves to
        convergence, so it takes MINUTES and requires OpenFOAM installed (WSL openfoam2412 or native
        blueCFD). It ALWAYS runs in the background: returns a job_id immediately; poll get_job(job_id)
        (which reports live meshing/solve progress) for the result.

        CONVENTIONS match run_cfd_flow (Z-up): freestream +X, lift +Z, +aoa pitches nose up.
        velocity : m/s.   aoa : degrees.   fluid : "air" | "water".
        preset   : "coarse" (fastest, ~600 iters) | "medium" | "fine" | "xfine" (finest/slowest).
                   Start coarse to sanity-check, refine if you need the digits.

        Result (via get_job): {cd, cl, ld (lift/drag), drag_n, lift_n, reynolds, ref_area_mm2,
        vmax_mps, converged, iters, notes}. If `converged` is false, raise the preset / iterations.
        """
        try:
            from engine.sim.foam_runner import capabilities
            caps = capabilities()
            if not caps.get("openfoam"):
                return _dumps({"ok": False, "error":
                               "OpenFOAM is not available on this machine. Provision it with "
                               "`python -m tools.provision_openfoam --install` (WSL) or install "
                               "blueCFD-Core natively. The coarse LBM run_cfd_flow still works."})
            if preset not in ("coarse", "medium", "fine", "xfine"):
                return _dumps({"ok": False, "error": f"unknown preset {preset!r} — use "
                               "coarse|medium|fine|xfine"})
            d = _doc(doc_id)
            # OpenFOAM meshes a watertight STL surface (build_case handles mm -> m).
            stl = _tmp(".stl")
            cb.call("export_file", {"doc_id": d, "path": stl, "format": "stl"})
            fluid_dict = ({"nu": 1.0e-6, "rho": 1000.0} if fluid == "water"
                          else {"nu": 1.5e-5, "rho": 1.225})

            prog: dict = {"pct": 0, "stage": "start", "text": "queued"}

            def _on_progress(ev: dict) -> None:
                prog.update(pct=ev.get("pct", 0), stage=ev.get("stage", ""),
                            text=ev.get("text", ""))

            def _solve() -> dict:
                from engine.sim.openfoam import run_openfoam_tunnel
                r = run_openfoam_tunnel(stl, direction=(1.0, 0.0, 0.0),
                                        speed_mps=float(velocity), preset=preset,
                                        aoa_deg=float(aoa), fluid=fluid_dict,
                                        on_progress=_on_progress)
                return {"ok": True, "doc_id": d,
                        "solver": f"OpenFOAM RANS (k-omega SST, {caps.get('openfoam_kind')})",
                        "preset": preset, "cd": r.cd, "cl": r.cl, "ld": r.ld,
                        "drag_n": r.drag_n, "lift_n": r.lift_n, "side_n": r.side_n,
                        "reynolds": r.reynolds, "ref_area_mm2": r.ref_area_mm2,
                        "vmax_mps": r.vmax_mps, "converged": (not r.diverged),
                        "iters": r.steps_run, "notes": r.solver_notes,
                        "velocity_ms": float(velocity), "aoa_deg": float(aoa), "fluid": fluid}

            jid = _start_job("cfd_openfoam", _solve, progress=prog)
            return _dumps({"ok": True, "job_id": jid, "status": "running", "doc_id": d,
                           "solver": caps.get("openfoam_version"), "preset": preset,
                           "note": "OpenFOAM RANS runs for minutes — poll get_job(job_id); it "
                                   "reports live meshing/solve progress."})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def get_job(job_id: str) -> str:
        """Poll a background job started by run_cfd_flow/run_fea_*(background=True).

        Returns {status: "running"|"done"|"error"}; when done, the solve `result` is included.
        Polling a long solve this way never trips the per-call transport timeout that would
        otherwise drop the whole session.
        """
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return _dumps({"ok": False, "error": f"unknown job_id {job_id!r}"})
            snap = dict(job)
        out = {"ok": True, "job_id": job_id, "status": snap["status"], "kind": snap["kind"]}
        if snap["status"] == "running" and snap.get("progress"):
            progress = dict(snap["progress"])  # live {pct, stage, text} for long OpenFOAM runs
            # LBM reports progress via a file the engine writes each checkpoint — fold it in so the
            # built-in CFD path gets an ETA-style readout like OpenFOAM (report footnote).
            pf = progress.pop("progress_file", None)
            if pf:
                try:
                    with open(pf, encoding="utf-8") as fh:
                        progress.update(json.load(fh))
                except (OSError, ValueError):
                    pass  # not written yet / mid-write — keep the seed
            out["progress"] = progress
        if snap["status"] == "done":
            out["result"] = snap["result"]
        elif snap["status"] == "error":
            out["ok"] = False
            out["error"] = snap["error"]
        return _dumps(out)

    @mcp.tool()
    def recover_parts(all: bool = False) -> str:
        """Reopen auto-saved parts after a server restart (recover a dropped session).

        Every successful add_features auto-persists the part to a recoverable .step+; if the
        server was restarted (e.g. a client timeout killed it) the in-memory docs are gone — call
        this to reload them (each gets a fresh doc_id). list_parts() then shows them.

        By default this recovers ONLY the most-recent prior session's parts (the one that was
        dropped) — not the entire autosave history, so you don't drag in unrelated old docs.
        Pass all=True to reach every autosaved part. Each entry returns its fresh `doc_id` plus
        the saved `name` and `original_doc_id` so you can re-map old handles. Stale autosaves
        (older than AI_CAD_AUTOSAVE_MAX_AGE_DAYS, default 7) are pruned automatically.
        """
        try:
            recovered = recover_autosaved(scope="all" if all else "recent")
            return _dumps({"ok": True, "recovered": recovered, "count": len(recovered),
                           "scope": "all" if all else "recent"})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- configurations

    @mcp.tool()
    def list_configurations(doc_id: Optional[str] = None) -> str:
        """List the part's configurations (parametric variants) and which one is active.

        A configuration is a named variant of the SAME feature tree with its own parameter
        overrides and feature suppressions — e.g. "Long" / "Short" of one bracket, or a family of
        sizes. Returns {configurations:[names], active, detail:[{name, param_overrides,
        suppressions}]}. A fresh part has just the implicit "Default". Build variants with
        add_configuration + set_config_override / set_config_suppression, switch with
        activate_configuration; measure_part/render_part then reflect the active variant.
        """
        try:
            return _dumps(cb.call("config_list", {"doc_id": _doc(doc_id)}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_configuration(name: str, copy_from: Optional[str] = None,
                          doc_id: Optional[str] = None) -> str:
        """Add a new configuration (a named parametric variant) to the current part.

        name      : the new configuration's name (must be unique).
        copy_from : an existing configuration name to seed its overrides/suppressions from
                    (omit to start empty = identical to the base part). Then tailor it with
                    set_config_override / set_config_suppression, and activate_configuration to
                    make it current. The new config is created but NOT activated.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_add", {"doc_id": d, "name": name, "copy_from": copy_from})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "added": name,
                           "active": (r.get("tree") or {}).get("active_configuration"),
                           "configurations": [c.get("name") for c in
                                              (r.get("tree") or {}).get("configurations", [])]})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def activate_configuration(name: str, doc_id: Optional[str] = None) -> str:
        """Switch the active configuration. The part rebuilds to that variant's parameters.

        After this, measure_part / render_part / export_part reflect the chosen variant. Returns
        the new active configuration name and the rebuilt body count/volume so you can confirm the
        switch took (e.g. a suppressed feature drops a body, an override changes the volume).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("config_activate", {"doc_id": d, "name": name})
            bodies = (r.get("tess") or {}).get("bodies", [])
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "active": name,
                           "body_count": len(bodies),
                           "revision": r.get("revision")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_config_override(config: str, feature_id: str, param: str,
                            value: Optional[Union[float, list]] = None, clear: bool = False,
                            doc_id: Optional[str] = None) -> str:
        """Override one feature parameter in a configuration (the parametric-variant edit).

        config     : the configuration name to edit (see list_configurations).
        feature_id : the feature's id from get_part (e.g. "f1").
        param      : the parameter name on that feature (e.g. "length", "radius", "translate").
        value      : the override value for this configuration only — a plain NUMBER for a scalar
                     param (length/radius/angle…) OR a LIST for a vector param, e.g.
                     translate:[x,y,z] on a `transform` feature (so one assembly file can hold a
                     Default and a STOWED config that differ only in a placement vector).
        clear      : pass True (and omit value) to REMOVE the override so the config falls back to
                     the base value.
        The base/Default config and other configurations are untouched. Activate the configuration
        (or it may already be active) and measure_part to see the effect.
        """
        try:
            d = _doc(doc_id)
            params: dict = {"doc_id": d, "name": config, "feature_id": feature_id,
                            "param": param}
            if clear or value is None:
                params["clear"] = True
            elif isinstance(value, (list, tuple)):
                # Vector/array param (e.g. transform.translate): the engine reads it as a raw
                # array (opt_vec3), so the override must be the bare list — NOT wrapped in a
                # {value, expr} scalar object. Coerce numeric components to float.
                params["value"] = [float(x) if isinstance(x, (int, float)) else x for x in value]
            else:
                # Scalar params are {value, expr} objects in the engine; wrap a bare number.
                params["value"] = {"value": float(value), "expr": None}
            cb.call("config_set_override", params)
            _publish_current(d)
            _autosave(d)
            echoed = None
            if not (clear or value is None):
                echoed = (params["value"] if isinstance(value, (list, tuple)) else float(value))
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "param": param,
                           "value": echoed,
                           "cleared": bool(clear or value is None)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_config_suppression(config: str, feature_id: str, suppressed: bool = True,
                               doc_id: Optional[str] = None) -> str:
        """Suppress (or un-suppress) a feature in a configuration — turn a feature ON/OFF per variant.

        config     : the configuration name (see list_configurations).
        feature_id : the feature id from get_part (e.g. "f3" — a fillet, a hole, a boss).
        suppressed : True to remove that feature in this configuration; False to bring it back.
        Lets one variant include a feature (a mounting boss, a chamfer) that another omits, without
        forking the feature tree. Activate the configuration + render_part to confirm.
        """
        try:
            d = _doc(doc_id)
            cb.call("config_set_suppression", {"doc_id": d, "name": config,
                                               "feature_id": feature_id,
                                               "suppressed": bool(suppressed)})
            _publish_current(d)
            _autosave(d)
            return _dumps({"ok": True, "doc_id": d, "config": config,
                           "feature_id": feature_id, "suppressed": bool(suppressed)})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- assembly

    @mcp.tool()
    def solve_mates(doc_id: Optional[str] = None) -> str:
        """Solve the mate constraints of an ASSEMBLY document (positions its components).

        Only valid on an assembly doc (e.g. one opened from a .step+ assembly via import_part) —
        on a single part it returns a clear "not an assembly" error. Solves the mate network and
        returns the rebuilt assembly state. Follow with list_world_bodies to read where each
        component landed.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("doc_solve_mates", {"doc_id": d})
            _publish_current(d)
            return _dumps({"ok": True, "doc_id": d, "revision": r.get("revision"),
                           "status": (r.get("rebuild") or {}).get("doc_status")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_world_bodies(explode: bool = False, doc_id: Optional[str] = None) -> str:
        """List a doc's bodies with their WORLD-placed bounding boxes.

        Works on an ASSEMBLY doc (each placed component after mates are solved) AND on a
        multi-body PART — e.g. one assembled via import_part + transform (where the bodies are
        already placed absolutely). Returns each body {name, component, bbox:{min,max,size}};
        use it to see where every part sits in the shared frame, or pair with check_interference
        to validate fit. explode=True applies the exploded-view offsets on an assembly (ignored
        on a plain multi-body part).
        """
        try:
            d = _doc(doc_id)
            r = cb.call("asm_world_bodies", {"doc_id": d, "explode": bool(explode)})
            bodies = r.get("bodies", [])
            if r.get("multibody_part"):
                return _dumps({"ok": True, "doc_id": d, "multibody_part": True,
                               "body_count": len(bodies), "bodies": bodies})
            return _dumps({"ok": True, "doc_id": d, "body_count": len(bodies),
                           "bodies": bodies})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    # ---------------------------------------------------------------- drawings & parts

    @mcp.tool()
    def make_drawing(doc_id: Optional[str] = None, views: Optional[list] = None,
                     sections: Optional[list] = None, sheet: Optional[str] = None,
                     hole_callouts: bool = True, model_items: bool = True,
                     title: Optional[dict] = None) -> str:
        """Create a 2D engineering drawing of the current part in ONE shot, then refine it.

        Builds projected views (hidden-line removed), auto-dimensions the ortho views, calls out
        holes, and fills the title block. Returns the new drawing_doc_id.

        views   : list of orientations, e.g. ["front","top","right","iso"] (default this set).
        sections: list of auto-section requests, each a dict
                  {"parent":"front","axis":"vertical"|"horizontal","mode":"full"|"half",
                   "reverse":false,"label":"A"} — cuts the parent view through its centre, no
                  sheet-mm math needed.
        sheet   : paper size "A4".."A0" or "A".."E" (default A3).
        title   : title-block fields {"title":..,"material":..,"scale":"1:1","rev":"A",...}.

        AFTER making it: call review_drawing(drawing_doc_id) to SEE the sheet, then refine with
        add_dimension / add_gdt / add_datum / add_surface_finish / add_section_view /
        add_detail_view / set_view (move/scale) / set_titleblock, reviewing as you go. Deliver
        with export_drawing('pdf'|'dxf', drawing_doc_id).
        """
        try:
            d = _doc(doc_id)
            tree = cb.call("doc_tree", {"doc_id": d})
            tree_node = tree.get("tree") or {}
            name = tree_node.get("name", "") if isinstance(tree_node, dict) else ""

            # D2: reject make_drawing on a doc that has no geometry so we don't silently
            # draw a stale "last-used" part.  We consider the doc empty when it has no
            # features in the tree AND no tessellated bodies (body_count==0 or missing).
            features = tree_node.get("features", []) if isinstance(tree_node, dict) else []
            body_count = tree.get("body_count", tree_node.get("body_count", None))
            if body_count is None:
                # Some engine versions put body_count at the top level of doc_tree,
                # others bury it inside the tree node.  If we can't read it, check features.
                body_count = len(features)
            if not features and not body_count:
                return _dumps({"ok": False,
                               "error": f"make_drawing: document {d!r} has no geometry to draw"})

            spec = {"views": views or ["front", "top", "right", "iso"],
                    "model_items": bool(model_items), "hole_callouts": bool(hole_callouts)}
            if sections:
                spec["sections"] = sections
            if sheet:
                spec["sheet"] = sheet
            tb = dict(title) if isinstance(title, dict) else {}
            if name and "title" not in tb:
                tb["title"] = name
            if tb:
                spec["title"] = tb
            r = cb.call("drawing_make", {"source_doc_id": d, "spec": spec})
            drawing_doc_id = r.get("doc_id")
            # D7: include the view_id ↔ orientation map so callers can pass either a view_id
            # or an orientation string to add_gdt / add_datum / add_dimension without having
            # to call drawing_get first.
            view_map: dict = {}
            if drawing_doc_id:
                try:
                    full = cb.call("drawing_get", {"doc_id": drawing_doc_id})
                    for sh in full.get("sheets", []):
                        for v in sh.get("views", []):
                            vid = v.get("view_id") or v.get("id")
                            ori = v.get("orientation") or v.get("type", "")
                            if vid:
                                view_map[vid] = ori
                except Exception:  # noqa: BLE001
                    pass
            return _dumps({"ok": True, "source_doc_id": d,
                           "drawing_doc_id": drawing_doc_id, "name": name,
                           "views": view_map or None,
                           "next_actions": [
                               "review_drawing(drawing_doc_id)  # SEE the drawing, then refine",
                               "add_gdt / add_dimension / set_view / set_titleblock  # refine",
                               "export_drawing('pdf', drawing_doc_id)  # or 'dxf'"]})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def export_drawing(fmt: str = "pdf", doc_id: Optional[str] = None,
                       path: Optional[str] = None) -> str:
        """Export a 2D DRAWING document to a vector file so you can deliver it.

        fmt    : "pdf" (a viewable sheet) or "dxf" (CAD interchange). Pass the drawing_doc_id from
                 make_drawing as doc_id. To SEE a drawing inline use render_part(drawing_doc_id).
        Returns the written path + entity counts. (export_part is for 3D B-rep/mesh only — a
        drawing is 2D, so it has its own export here.)
        """
        try:
            d = _doc(doc_id)
            fmt = (fmt or "pdf").lower()
            if fmt not in ("pdf", "dxf"):
                return _dumps({"ok": False, "error": f"fmt must be 'pdf' or 'dxf', got {fmt!r}"})
            out_path = path or _tmp("." + fmt)
            r = cb.call("export_drawing", {"doc_id": d, "path": out_path, "fmt": fmt})
            return _dumps({"ok": True, "doc_id": d, "format": fmt,
                           "path": r.get("path", out_path),
                           "visible": r.get("visible"), "hidden": r.get("hidden"),
                           "entities": r.get("entities")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def review_drawing(doc_id: Optional[str] = None) -> Image:
        """Render a 2D DRAWING document to an image so you can SEE it and self-review.

        Pass the drawing_doc_id from make_drawing. The render shows the projected views, every
        dimension / hole callout / GD&T frame / surface-finish symbol / section hatch and the
        title block — exactly what the DXF/PDF export will contain. Use it to check for: missing
        or overlapping dimensions, views colliding on the sheet, GD&T placed off the feature, an
        empty title block. Then fix with the granular drawing tools (add_dimension, add_gdt,
        set_view to move/scale a view, set_titleblock) and review again.
        """
        d = _doc(doc_id)
        png = _tmp(".png")
        rd = cb.call("drawing_render_png", {"doc_id": d, "out_path": png})
        if not rd.get("ok") or not os.path.exists(png):
            raise ValueError(f"Drawing render failed: {rd.get('reason', 'not a drawing doc?')}")
        return Image(path=png)

    @mcp.tool()
    def add_drawing_view(orientation: str = "front", doc_id: Optional[str] = None,
                         view_type: str = "projected", x: float = 150.0, y: float = 150.0) -> str:
        """Add ONE projected view to a drawing. doc_id = the drawing_doc_id.

        orientation: front|back|top|bottom|left|right|iso. view_type: base (the first/anchor
        ortho view) | projected | iso. x,y = the view centre on the sheet in millimetres.
        """
        try:
            d = _doc(doc_id)
            r = cb.call("drawing_add_view", {"doc_id": d, "type": view_type,
                                             "orientation": orientation, "position": [x, y]})
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_section_view(parent: str = "front", axis: str = "vertical", mode: str = "full",
                         reverse: bool = False, label: Optional[str] = None,
                         doc_id: Optional[str] = None) -> str:
        """Add a SECTION view by cutting a parent view through its centre. doc_id = drawing_doc_id.

        parent: the orientation ("front"/"top"/...) or view_id of the view to cut.
        axis  : "vertical" or "horizontal" cut line through the part centre.
        mode  : "full" (whole part sectioned) or "half" (half sectioned, half external view).
        reverse: flip which side of the cut is removed.
        No sheet-mm math needed — the engine places the cut line across the parent's bbox.
        """
        try:
            d = _doc(doc_id)
            # Reuse make-style auto-section via a tiny spec on the existing drawing.
            full = cb.call("drawing_get", {"doc_id": d})
            vid, bbox = None, None
            for sh in full.get("sheets", []):
                for v in sh.get("views", []):
                    if v.get("type") in ("section", "detail"):
                        continue
                    if parent in (v.get("view_id"), v.get("id"), v.get("orientation")):
                        bb = v.get("bbox")
                        if isinstance(bb, list) and len(bb) == 4:
                            vid, bbox = (v.get("view_id") or v.get("id")), bb
            if not vid:
                return _dumps({"ok": False, "error": f"no parent view {parent!r} to cut"})
            x0, y0, x1, y1 = bbox
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            pad = 0.1 * max(x1 - x0, y1 - y0) + 2.0
            cut = ([[x0 - pad, cy], [x1 + pad, cy]] if axis == "horizontal"
                   else [[cx, y0 - pad], [cx, y1 + pad]])
            params = {"doc_id": d, "parent_view_id": vid, "cut_line": cut,
                      "opts": {"mode": mode, "reverse": reverse}}
            if label:
                params["label"] = label
            r = cb.call("drawing_add_section", params)
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_detail_view(parent_view_id: str, cx: float, cy: float, radius: float,
                        doc_id: Optional[str] = None) -> str:
        """Add a magnified DETAIL view of a circular region of a parent view. doc_id = drawing_doc_id.
        cx,cy,radius are in the parent view's sheet millimetres (read them from review_drawing)."""
        try:
            d = _doc(doc_id)
            r = cb.call("drawing_add_detail", {"doc_id": d, "parent_view_id": parent_view_id,
                                               "center": [cx, cy], "radius": radius})
            return _dumps({"ok": True, "view_id": r.get("view_id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_dimension(view_id: str, a: list, b: list, dtype: str = "linear_dim",
                      pos: Optional[list] = None, doc_id: Optional[str] = None) -> str:
        """Add a dimension to a view. doc_id = drawing_doc_id. a,b are sheet-mm points (snap to
        projected vertices you read from review_drawing). dtype: linear_dim|aligned_dim. pos is
        the optional text position. For auto-dimensioning a whole view use the drawing spec's
        model_items instead.

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front", "top", …)
        matching the view labels returned by review_drawing / make_drawing.
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            params = {"doc_id": d, "view_id": view_id, "type": dtype, "a": a, "b": b}
            if pos:
                params["pos"] = pos
            r = cb.call("drawing_add_dim", params)
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_gdt(view_id: str, symbol: str, tolerance: str, a: list, pos: list,
                datums: Optional[list] = None, diameter: bool = False,
                modifier: str = "none", doc_id: Optional[str] = None) -> str:
        """Add an ASME Y14.5 feature control frame. doc_id = drawing_doc_id.

        symbol  : position|flatness|perpendicularity|parallelism|angularity|concentricity|
                  symmetry|profile_surface|profile_line|circularity|cylindricity|straightness|
                  circular_runout|total_runout
        tolerance: the zone value, e.g. "0.1".  diameter=True for a ⌀ tolerance zone.
        modifier : none|mmc|lmc|projected (material condition on the tolerance).
        datums   : a list of datum letters ["A","B","C"] (or dicts {"ref":"A","modifier":"mmc"}).
        a   : the feature point on the view the frame points at (sheet mm, from review_drawing).
        pos : where the frame sits on the sheet (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            dl = []
            for it in (datums or []):
                dl.append(it if isinstance(it, dict) else {"ref": str(it)})
            spec = {"symbol": symbol, "tol_value": tolerance, "tol_diameter": bool(diameter),
                    "tol_modifier": modifier, "datums": dl, "a": a, "pos": pos}
            r = cb.call("drawing_add_gdt", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_datum(view_id: str, label: str, a: list, pos: list,
                  doc_id: Optional[str] = None) -> str:
        """Add a datum feature symbol (triangle + boxed letter). doc_id = drawing_doc_id.
        label is the datum letter ("A"); a is the feature point, pos the label box (sheet mm).

        view_id accepts a numeric id (v1, v2, …) OR an orientation string ("front"/"top"/…).
        """
        try:
            d = _doc(doc_id)
            # D7: resolve orientation strings ("front"/"top"/…) to numeric view ids.
            view_id = _resolve_drawing_view_id(d, view_id)
            spec = {"label": label, "a": a, "pos": pos}
            r = cb.call("drawing_add_datum", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def add_surface_finish(view_id: str, ra: str, a: list, pos: list, machining: str = "required",
                           all_around: bool = False, doc_id: Optional[str] = None) -> str:
        """Add a surface-finish symbol. doc_id = drawing_doc_id. ra is the roughness value ("1.6");
        machining: any|required|prohibited. a is the surface point, pos the symbol (sheet mm)."""
        try:
            d = _doc(doc_id)
            spec = {"value": ra, "machining": machining, "all_around": bool(all_around),
                    "a": a, "pos": pos}
            r = cb.call("drawing_add_surface_finish", {"doc_id": d, "view_id": view_id, "spec": spec})
            return _dumps({"ok": True, "annotation": (r.get("annotation") or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_view(view_id: str, x: Optional[float] = None, y: Optional[float] = None,
                 scale: Optional[float] = None, doc_id: Optional[str] = None) -> str:
        """Move and/or rescale a view. doc_id = drawing_doc_id. x,y reposition the view on the
        sheet (mm); scale sets the view scale (1.0 = 1:1, 0.5 = 1:2, 2.0 = 2:1). Use this to
        spread overlapping views apart after review_drawing."""
        try:
            d = _doc(doc_id)
            params = {"doc_id": d, "view_id": view_id}
            if x is not None and y is not None:
                params["position"] = [x, y]
            if scale is not None:
                params["scale"] = scale
            cb.call("drawing_set_view", params)
            return _dumps({"ok": True, "view_id": view_id})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def set_titleblock(fields: dict, doc_id: Optional[str] = None) -> str:
        """Fill the drawing's title block. doc_id = drawing_doc_id. fields is a dict of any of:
        title, drawn_by, date, material, finish, scale, rev, sheet, part_number, company."""
        try:
            d = _doc(doc_id)
            cb.call("drawing_set_titleblock", {"doc_id": d, "sheet_id": "s1", "fields": fields})
            return _dumps({"ok": True})
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def list_standard_parts() -> str:
        """List the parametric standard-parts catalog (screws, nuts, washers, bearings, ...):
        each family's key, sizes, and lengths. Feed a family + size to make_standard_part."""
        try:
            return _dumps(cb.call("parts_catalog_list", {}))
        except Exception as e:  # noqa: BLE001
            return _err(e)

    @mcp.tool()
    def make_standard_part(family: str, size: str, length_mm: Optional[float] = None,
                           name: Optional[str] = None, options: Optional[dict] = None) -> str:
        """Build a standard hardware part as a NEW editable part doc from the catalog.

        family : a catalog key from list_standard_parts (e.g. "shcs", "hex_nut", "washer").
        size   : a size string for that family (e.g. "M5"). length_mm where the family needs it.
        Returns the new part's doc_id (and makes it current) so you can drop it into an assembly
        or export it — PLUS `bbox` (min/max/size/center, mm) and a `datum` (base_center + axis)
        so you can position it with the `transform` feature WITHOUT a measure round-trip. Standard
        parts seat their base at z=0 with the axis along +Z, so translate `datum.base_center` to
        the seat point. (Tip: transform's `body` selector targets a named body — see cad_guide.)
        """
        try:
            params: dict = {"family": family, "size": size}
            if length_mm is not None:
                params["length_mm"] = length_mm
            if name:
                params["name"] = name
            if options:
                params["options"] = options
            r = cb.call("parts_make", params)
            if not r.get("ok"):
                return _dumps({"ok": False, "error": r.get("error", "parts_make failed")})
            _CUR["doc_id"] = r.get("doc_id")
            _publish_current(r.get("doc_id"))
            _autosave(r.get("doc_id"), r.get("name"))
            # bbox + datum let the caller position this part in an assembly without a
            # measure round-trip (A-1): standard parts seat base at z=0, axis +Z.
            return _dumps({"ok": True, "doc_id": r.get("doc_id"), "name": r.get("name"),
                           "meta": r.get("meta"), "volume": r.get("volume"),
                           "bbox": r.get("bbox"), "datum": r.get("datum")})
        except Exception as e:  # noqa: BLE001
            return _err(e)
