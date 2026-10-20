"""Worker-RPC transport adapter for the Reverse Engineering suite (GUI ↔ reveng backend).

This module is a THIN TRANSPORT ADAPTER over the reveng MCP tool registry — it is
NOT a parallel implementation of it. Every reverse-engineering operation lives
exactly once, as an ``@mcp.tool()`` function in
:mod:`services.mcp.reveng_session_tools`, :mod:`services.mcp.reveng_build_tools` and
:mod:`services.mcp.recon_tools`. Agents reach those over the MCP transport; the
desktop GUI reaches them over the newline-JSON ``WorkerClient``/``AsyncRpc`` seam
(``core/hostd/worker_bridge.cpp`` → ``python -m services.worker_main`` →
:data:`REVENG_WORKER_METHODS`). This file is the second doorway onto the SAME
functions — nothing more. Both transports therefore expose one identical surface;
a GUI button can never drift from what an agent can do.

How the doorway works: the reveng tool families are registered onto one throwaway
``FastMCP`` and the underlying callable is pulled back off its tool manager — the
pattern already proven by ``services/mcp/reveng_common.py:_call_reverse_engineer``
and ``services/dag/verify.py:_get_fn``. MCP tools return JSON *strings* while worker
RPC handlers return dicts, so :func:`_call_tool` parses the string and normalises it
onto the honest ``{ok, reason, hint}`` envelope. Tool state is module-level (the
on-disk session store, ``recon_tools._SESSIONS``), so both transports share ONE
registry: a capture session started by an agent is visible to the GUI and vice versa.

Adding a tool to the GUI surface is one line in :data:`_BRIDGED_TOOLS`.

The only hand-written handler left is ``reveng_start_from_product`` — the §2b
parts-hub product-media hand-off, which has no MCP equivalent. Even that composes
the bridged tools instead of reimplementing session logic.

Envelopes always degrade to ``{ok: false, reason, hint}`` — never a traceback, never
an escaping exception. Registered from ``services/worker_main.py`` (same try/except
pattern as the parts + embed method tables). (Moved from engine_oracle/reveng_rpc.py
to services/reveng/ in T-RF19.)
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Callable


def _fail(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _wrap_exc(exc: Exception) -> dict:
    if isinstance(exc, KeyError) and exc.args == ("not_found",):
        from services.agent.request_context import in_cell
        out = {"ok": False, "error": "not_found", "reason": "not_found"}
        if in_cell():
            out["code"] = "not_found"
        return out
    if isinstance(exc, ValueError) and str(exc) == "forbidden_right":
        return {"ok": False, "error": "forbidden_right", "reason": "forbidden_right"}
    if isinstance(exc, ImportError):
        return _fail("reveng_unavailable",
                     "reverse-engineering tools not importable; check engine install")
    return _fail(f"reveng_rpc_failed: {exc}", "")


# ── MCP tool bridge ──────────────────────────────────────────────────────────

# Lazily-built throwaway FastMCP tool manager carrying the whole reveng surface.
# Built once per worker process; the tools themselves hold no per-instance state.
_TOOL_MANAGER: Any = None


def _tool_manager() -> Any:
    """Register the reveng tool families on a throwaway FastMCP and hand back its
    tool manager (keeps the worker importable even if the RE suite is absent —
    the ImportError surfaces as ``reveng_unavailable`` on the first call)."""
    global _TOOL_MANAGER
    if _TOOL_MANAGER is None:
        from mcp.server.fastmcp import FastMCP

        from services.mcp.recon_tools import register_photo_recon_tools
        from services.mcp.reveng_tools import register_reveng_tools

        mcp = FastMCP("_reveng_worker_rpc")
        register_reveng_tools(mcp)
        # start_capture_session / stop_capture_session live with the recon tools and
        # keep their session registry in that module — registering them here is what
        # gives the GUI and the agent ONE capture-session registry.
        register_photo_recon_tools(mcp)
        _TOOL_MANAGER = mcp._tool_manager
    return _TOOL_MANAGER


def _tool_fn(method: str) -> Callable | None:
    """The plain Python callable behind an ``@mcp.tool()``-registered function."""
    tool = _tool_manager().get_tool(method)
    return getattr(tool, "fn", None) if tool is not None else None


def _envelope(raw: Any, method: str) -> dict:
    """Normalise an MCP tool's return value onto the worker's honest envelope.

    The tools answer with a JSON string; failures come back either as the reveng
    ``{ok:false, reason, hint}`` shape or as ``cad_common._err``'s
    ``{ok:false, error}``. The GUI reads ``reason``/``hint``, so fill those in
    without discarding anything the tool reported.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return _fail(f"{method}_bad_response",
                         "tool returned text that is not JSON")
    if not isinstance(raw, dict):
        return _fail(f"{method}_bad_response",
                     f"tool returned {type(raw).__name__}, expected an object")
    if raw.get("ok", True):
        raw.setdefault("ok", True)
        return raw
    out = dict(raw)
    out.setdefault("reason", str(out.get("error") or f"{method} failed"))
    out.setdefault("hint", "")
    return out


def _call_tool(method: str, params: dict) -> dict:
    """Invoke the MCP tool *method* with *params* and return its dict envelope.

    Params the tool does not declare are dropped (the GUI sends a few extras); a
    missing required argument is reported honestly instead of raising TypeError.
    """
    from services.agent import request_context
    request_context.control_delegate("cancel" if method == "reveng_cancel_splat" else method)
    if request_context.in_cell() and method in (
            "start_capture_session", "stop_capture_session"):
        return {"ok": False, "reason": "cell_refused", "error": "cell_refused"}
    if request_context.in_cell() and "session_id" in (params or {}):
        from services.mcp.reveng_common import _load_session
        _load_session(params["session_id"], merge_gaussian=False)
    fn = _tool_fn(method)
    if fn is None:
        return _fail(f"tool_not_registered: {method}",
                     "the reveng MCP tool families did not register this tool")
    supplied = dict(params or {})
    sig = inspect.signature(fn)
    named = [p for p in sig.parameters.values()
             if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)]
    if request_context.in_cell() and any(p.name == "session_id" for p in named):
        from services.mcp.reveng_common import _load_session
        if "session_id" not in supplied:
            _load_session(None, merge_gaussian=False)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        kwargs = supplied
    else:
        kwargs = {p.name: supplied[p.name] for p in named if p.name in supplied}
    missing = [p.name for p in named
               if p.default is inspect.Parameter.empty and p.name not in kwargs]
    if missing:
        return _fail(f"missing_param: {', '.join(missing)}",
                     f"{method} requires {', '.join(missing)}")
    return _envelope(fn(**kwargs), method)


def _bridge(method: str, **pinned: Any) -> Callable[[dict, Callable], dict]:
    """Build the worker-RPC handler that forwards to the same-named MCP tool.

    ``pinned`` supplies transport-specific defaults the caller may still override
    (see :data:`_PINNED_ARGS`). No handler built here can raise.
    """
    def _handler(params: dict, _emit: Callable) -> dict:
        try:
            merged = dict(params or {})
            for key, value in pinned.items():
                merged.setdefault(key, value)
            result = _call_tool(method, merged)
            from services.agent.request_context import control_delegate
            delegate = control_delegate("cancel" if method == "reveng_cancel_splat" else method)
            if delegate and result.get("ok"):
                from services.mcp.reveng_common import _session_path
                from datetime import datetime, timezone
                event = {"kind": "run_stopped", "reason": "manager_stop",
                         "control_delegate": delegate,
                         "session_id": merged["session_id"],
                         "ts": datetime.now(timezone.utc).isoformat()}
                with (_session_path(merged["session_id"]) / "control-events.jsonl").open("a") as log:
                    log.write(json.dumps(event) + "\n")
                    log.flush()
                _emit(event)
                return {"ok": True, "session_id": merged["session_id"],
                        "status": result.get("status", "cancelled"),
                        "reason": "manager_stop", "control_delegate": delegate}
            return result
        except Exception as exc:  # noqa: BLE001
            return _wrap_exc(exc)

    _handler.__name__ = f"_rpc_{method}"
    _handler.__doc__ = f"Worker-RPC transport for the {method!r} MCP tool."
    return _handler


# Every reveng tool reachable over the GUI transport. One line per tool — this is
# the whole registration surface. Names must match the @mcp.tool() function names.
_BRIDGED_TOOLS: tuple[str, ...] = (
    # ── §8.1 READ — services.mcp.reveng_session_tools ────────────────────────
    "list_reveng_sessions",
    "get_reveng_session",
    "get_reveng_coverage",
    "get_reveng_deviation",
    "list_reveng_dims",
    "describe_reveng_result",
    "reveng_status",
    # ── §8.2 WRITE — ingest + calibration (reveng_session_tools) ─────────────
    "reveng_start_session",
    "reveng_add_images",
    "reveng_add_video",
    "reveng_ingest_datasheet",
    "reveng_set_scale",
    "reveng_print_scale_mat",
    # ── §8.2 WRITE — reconstruction + modelling (reveng_build_tools) ─────────
    "reveng_reconstruct",
    "reveng_start_splat",
    "reveng_splat_status",
    "reveng_finish_splat",
    "reveng_cancel_splat",
    "reveng_crop_splat",
    # Mission gates. reveng_reconstruct returns a run_id immediately and the mission
    # suspends at its scale-confirm / dimension-confirm gates; without these two the
    # GUI's Reconstruct button dead-ends at the first pause.
    "reveng_gate_status",
    "reveng_resolve_gate",
    "reveng_single_photo",
    "reveng_fit_primitives",
    "reveng_build_parametric",
    "reveng_attach_result",
    "reveng_research_source",
    "reveng_make_part_from_library",
    # ── §8.3 EXTERNAL — import/export (reveng_build_tools) ───────────────────
    "import_scan_mesh",
    "export_reveng_bundle",
    # ── phone capture — services.mcp.recon_tools (shared _SESSIONS registry) ─
    "start_capture_session",
    "stop_capture_session",
)

# Transport-specific defaults. The RE tab adds the collected photos to the session
# itself (via reveng_add_images), so the GUI must NOT trigger photogrammetry on
# stop — whereas an agent calling the MCP tool directly still gets the historical
# run_reconstruction=True. Same implementation, same registry, one pinned default.
_PINNED_ARGS: dict[str, dict] = {
    "stop_capture_session": {"run_reconstruction": False},
}


# ── §2b product-media hand-off (no MCP equivalent) ───────────────────────────

def _site_of(url_or_part_id: str) -> str:
    """Best-effort session-profile key for a download (mirrors product_media)."""
    s = (url_or_part_id or "").strip()
    if s.startswith("catalog:"):
        return s[len("catalog:"):].split("::", 1)[0]
    try:
        from urllib.parse import urlparse
        netloc = urlparse(s).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        parts = netloc.split(".")
        return parts[-2] if len(parts) >= 2 else (parts[0] if parts else netloc)
    except Exception:  # noqa: BLE001
        return "product"


#: Ceiling on media items this call will ATTEMPT to download, images + documents.
#:
#: ARITHMETIC: ``reverse_engineer_product`` carries ``timeout_s: 300`` (see
#: ``services/agent/tool_guidance/parts.py``). Each item pays
#: ``ratelimit.DEFAULT_MIN_INTERVAL_S`` = 3 s of mandatory same-site spacing plus a
#: real vendor image/PDF body, measured 1-4 s — call it ~7 s worst case per item.
#: 24 * 7 s = 168 s, leaving ~130 s of the 300 s for reveng_start_session, the
#: per-document ingest lane, and the session write. Without a bound the worst case
#: was unbounded: product pages routinely list 30-60 media URLs (Amazon, Home Depot
#: galleries), so a full harvest could not fit in ANY tool budget and the harness
#: abandoned the call mid-download, discarding every item already fetched.
_MAX_HARVEST_ITEMS = 24

#: Browser refusals that mean "stop asking THIS site" rather than "that one URL
#: failed". ``cooldown_active`` is the site's own 429/403 backoff, which is
#: per-site and persisted, so every remaining same-site URL would be refused too —
#: continuing just burns the tool budget one pointless call at a time.
_SITE_STOP_REASONS = frozenset({"cooldown_active"})


def _download_into(url: str, site: str, dest_dir: Path) -> tuple[Path | None, dict | None]:
    """Download one media URL into ``dest_dir`` under the user's session.

    Returns ``(local_path, refusal)``: the path on success, or ``(None, envelope)``
    when the browser declined with a reason the harvest must ACT on
    (:data:`_SITE_STOP_REASONS`). Every other failure stays ``(None, None)`` —
    best-effort, never raises.
    """
    try:
        from services.browser import session as _bsession
        from services.net.safe_http import safe_download_path
        dest = safe_download_path(dest_dir, url, default="file")
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = _bsession.browser_download(url, site, dest)
        if out.get("ok") and dest.is_file():
            return dest, None
        if out.get("reason") in _SITE_STOP_REASONS:
            return None, out
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _harvest_downloads(urls: list[str], site: str, dest_dir: Path, budget: int) -> dict:
    """Download at most ``budget`` of ``urls``, keeping whatever it gets.

    Returns ``{paths, attempts, skipped, skip_reason, retry_after_s}``. The loop
    stops early on the ``budget`` ceiling or on a site-level refusal, and ``skipped``
    counts every URL left untried at that point — a partial harvest reported honestly
    beats a whole call abandoned by the harness on media item #1.
    """
    paths: list[Path] = []
    attempts = 0
    for index, url in enumerate(urls):
        if attempts >= budget:
            return {"paths": paths, "attempts": attempts, "skipped": len(urls) - index,
                    "skip_reason": "harvest_item_cap", "retry_after_s": None}
        attempts += 1
        path, refusal = _download_into(url, site, dest_dir)
        if path is not None:
            paths.append(path)
            continue
        if refusal is not None:
            retry_after = refusal.get("retry_after_s")
            return {"paths": paths, "attempts": attempts, "skipped": len(urls) - index,
                    "skip_reason": str(refusal.get("reason")),
                    "retry_after_s": (float(retry_after)
                                      if isinstance(retry_after, (int, float)) else None)}
    return {"paths": paths, "attempts": attempts, "skipped": 0,
            "skip_reason": None, "retry_after_s": None}


def _harvest_skip_hint(site: str, sid: str, skipped: int, reason: str,
                       retry_after_s: float | None) -> str:
    """Teaching note for a harvest that returned PARTIAL media, in the same posture
    as ``agent_turn._call_cap_error``: name the next action, not the config knob.

    The session exists and holds real items, so the model must not read this as a
    failed call and re-issue it — re-calling into an armed cooldown is refused
    identically and costs another tool call for nothing.
    """
    head = (f"{skipped} media item(s) were NOT downloaded, but session {sid} is live "
            f"with everything that was. ")
    if reason in _SITE_STOP_REASONS:
        when = (f" after ~{retry_after_s:.0f}s" if retry_after_s is not None
                else " once its cooldown expires")
        return (head + f"{site} started rate-limiting us mid-harvest (429/403 backoff), "
                       f"so the remaining URLs were abandoned rather than waited on — "
                       f"waiting is impossible inside one call. Do NOT re-call "
                       f"reverse_engineer_product now; it will be refused the same way. "
                       f"Work the session you have (reveng_reconstruct / "
                       f"reveng_single_photo), add your own photos with "
                       f"reveng_add_images(session_id, paths), and only revisit {site}"
                       f"{when}.")
    return (head + f"the harvest stopped at its {_MAX_HARVEST_ITEMS}-item ceiling, which "
                   f"keeps this call inside its time budget. Continue with the media the "
                   f"session already holds; if a specific missing view matters, add just "
                   f"that file with reveng_add_images(session_id, paths).")


def _reveng_start_from_product(params: dict, _emit: Callable) -> dict:
    """§2b hand-off: harvest a product's images+documents, then stand up an RE
    session pre-populated with them. The parts-hub harvest has no MCP tool of its
    own; the session work itself routes through the bridged tools, so nothing is
    reimplemented here. A zero-media harvest STILL creates a session (the user adds
    their own photos). Each download/ingest is guarded — one failure never fails
    the whole call.

    The media harvest is BOUNDED and INTERRUPTIBLE: at most
    :data:`_MAX_HARVEST_ITEMS` items are attempted, and a site-level refusal
    (:data:`_SITE_STOP_REASONS`) abandons the remaining same-site URLs instead of
    blocking on them. Such a call still succeeds, reporting ``media_skipped``,
    ``media_skipped_reason``, ``retry_after_s`` and an actionable ``hint`` alongside
    the items it did collect."""
    try:
        from services.mcp.reveng_tools import _session_path

        p = params or {}
        ref = str(p.get("part_id") or p.get("url") or "").strip()
        if not ref:
            return _fail("no_reference", "pass a part_id or url")

        from services.parts.product_media import harvest_product_media
        harvest = harvest_product_media(ref)
        images = harvest.get("images", []) if harvest.get("ok") else []
        documents = harvest.get("documents", []) if harvest.get("ok") else []
        source_url = harvest.get("source_url") if harvest.get("ok") else (
            ref if ref.startswith("http") else None)
        site = _site_of(ref)

        # Start the session seeded with the product's identity (RE-23 seed lane).
        seed_bits = []
        for key in ("title", "mpn", "brand"):
            if p.get(key):
                seed_bits.append(f"{key}={p[key]}")
        if source_url:
            seed_bits.append(source_url)
        started = _call_tool("reveng_start_session", {
            "name": p.get("title") or None,
            "research": " ".join(seed_bits) if seed_bits else None,
        })
        if not started.get("ok"):
            return started
        sid = started["session_id"]

        # Bounded, interruptible harvest: images then documents share ONE item
        # budget (:data:`_MAX_HARVEST_ITEMS`), and a site-level refusal abandons the
        # rest of the SITE while keeping everything already collected. A single
        # backed-off vendor used to spend the tool's whole 300 s on media item #1 and
        # take the entire harvest down with it.
        budget = _MAX_HARVEST_ITEMS
        skipped = 0
        skip_reason: str | None = None
        retry_after_s: float | None = None

        images_added = 0
        if images:
            got = _harvest_downloads(images, site,
                                     _session_path(sid) / "_harvest_images", budget)
            budget -= got["attempts"]
            skipped += got["skipped"]
            skip_reason = got["skip_reason"]
            retry_after_s = got["retry_after_s"]
            if got["paths"]:
                added = _call_tool("reveng_add_images",
                                   {"session_id": sid,
                                    "paths": [str(pth) for pth in got["paths"]]})
                if added.get("ok"):
                    images_added = added.get("added", 0)

        # Download each document, then ingest via the existing datasheet lane.
        documents_ingested = 0
        if documents and skip_reason in _SITE_STOP_REASONS:
            skipped += len(documents)  # same site, same cooldown — don't even try
        elif documents:
            got = _harvest_downloads(documents, site,
                                     _session_path(sid) / "_harvest_docs", budget)
            skipped += got["skipped"]
            skip_reason = got["skip_reason"] or skip_reason
            retry_after_s = got["retry_after_s"] if retry_after_s is None else retry_after_s
            for local in got["paths"]:
                ing = _call_tool("reveng_ingest_datasheet",
                                 {"session_id": sid, "path": str(local)})
                if ing.get("ok"):
                    documents_ingested += 1

        out = {"ok": True, "session_id": sid,
               "images_added": images_added,
               "documents_ingested": documents_ingested,
               "media_skipped": skipped,
               "source_url": source_url,
               "activeWorkspace": "ReverseEng"}
        if skip_reason:
            out["media_skipped_reason"] = skip_reason
            out["hint"] = _harvest_skip_hint(site, sid, skipped, skip_reason,
                                             retry_after_s)
        if retry_after_s is not None:
            out["retry_after_s"] = round(retry_after_s, 1)
        return out
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


REVENG_WORKER_METHODS: dict[str, Callable] = {
    name: _bridge(name, **_PINNED_ARGS.get(name, {})) for name in _BRIDGED_TOOLS
}
REVENG_WORKER_METHODS["reveng_start_from_product"] = _reveng_start_from_product
