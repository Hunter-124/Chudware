"""Worker-RPC seam for the reusable embedded browser (Parts Library V2, workstream 3).

Thin wrappers over :mod:`services.browser.embed`'s :class:`EmbedManager` so any
GUI surface (the parts-library browser panel today, anything else tomorrow)
reaches a live CDP-screencast Chromium over the SAME newline-JSON worker seam
(``WorkerClient``/``AsyncRpc``) as every other service — no bespoke transport.

Envelopes (all degrade to ``{ok:false, reason, hint}``, never a traceback):

- ``browser_embed_start    {site?|url?, max_width=900, max_height=700, quality=60}``
  → ``{ok, id, w, h, url, login_state}``
- ``browser_embed_frame    {id, since_seq?}``
  → ``{ok, seq, frame_b64?, w, h, url, login_state}`` (``frame_b64`` omitted
  when the caller's ``since_seq`` is already current — poll cheaply at paint rate)
- ``browser_embed_input    {id, events:[{type,x,y,button,delta_y,key,text,modifiers}...]}`` → ``{ok}``
- ``browser_embed_navigate {id, url}`` → ``{ok, url}``
- ``browser_embed_stop     {id}`` → ``{ok}``
- ``login_wizard_start {sites?}`` / ``login_wizard_status {}`` /
  ``login_wizard_next {skip?}`` / ``login_wizard_stop {}``

Everything lazy-imports the embed backend so a worker without Playwright still
registers these methods and answers with a structured failure.

Registered append-only from ``engine_oracle/parts_hub/worker_rpc.py`` (same
try/except pattern as ``GUI_WORKER_METHODS``).
"""
from __future__ import annotations

import base64
from typing import Callable


def _manager():
    from services.browser.embed import get_manager
    return get_manager()


def _fail(reason: str, hint: str = "") -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _wrap_exc(exc: Exception) -> dict:
    if isinstance(exc, ImportError):
        return _fail("playwright_not_installed",
                     "pip install playwright && playwright install chromium")
    return _fail(f"embed_rpc_failed: {exc}", "")


def _browser_embed_start(params: dict, _emit: Callable) -> dict:
    try:
        p = params or {}
        return _manager().start(
            site=p.get("site") or None,
            url=p.get("url") or None,
            max_width=int(p.get("max_width", 900)),
            max_height=int(p.get("max_height", 700)),
            quality=int(p.get("quality", 60)),
        )
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _browser_embed_frame(params: dict, _emit: Callable) -> dict:
    try:
        p = params or {}
        since = p.get("since_seq")
        res = _manager().frame(str(p.get("id", "")),
                               since_seq=int(since) if since is not None else None)
        if not res.get("ok"):
            return res
        out = {"ok": True, "seq": res["seq"], "w": res["w"], "h": res["h"],
               "url": res["url"], "login_state": res["login_state"]}
        frame = res.get("frame")
        if frame is not None:
            out["frame_b64"] = base64.b64encode(frame).decode("ascii")
        return out
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _browser_embed_input(params: dict, _emit: Callable) -> dict:
    try:
        p = params or {}
        return _manager().input(str(p.get("id", "")), list(p.get("events") or []))
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _browser_embed_navigate(params: dict, _emit: Callable) -> dict:
    try:
        p = params or {}
        return _manager().navigate(str(p.get("id", "")), str(p.get("url", "")))
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _browser_embed_stop(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().stop(str((params or {}).get("id", "")))
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_start(params: dict, _emit: Callable) -> dict:
    try:
        sites = (params or {}).get("sites")
        return _manager().wizard_start(list(sites) if sites else None)
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_status(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().wizard_status()
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_next(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().wizard_next(skip=bool((params or {}).get("skip", False)))
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


def _login_wizard_stop(params: dict, _emit: Callable) -> dict:
    try:
        return _manager().wizard_stop()
    except Exception as exc:  # noqa: BLE001
        return _wrap_exc(exc)


EMBED_WORKER_METHODS: dict[str, Callable] = {
    "browser_embed_start": _browser_embed_start,
    "browser_embed_frame": _browser_embed_frame,
    "browser_embed_input": _browser_embed_input,
    "browser_embed_navigate": _browser_embed_navigate,
    "browser_embed_stop": _browser_embed_stop,
    "login_wizard_start": _login_wizard_start,
    "login_wizard_status": _login_wizard_status,
    "login_wizard_next": _login_wizard_next,
    "login_wizard_stop": _login_wizard_stop,
}
