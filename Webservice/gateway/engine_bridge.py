"""WS /engine — bridge browser clients to the shared billet-webd unix socket.

One WebSocket connection == one daemon IPC connection (newline-delimited
JSON-RPC both ways; one WS text message == one JSON line). The bridge is a
dumb pipe except for ONE rewrite: the client's `hello` gets server-asserted
identity (`params.user` from the verified gateway User) and a forced
`params.client = "gui"` — a browser can never claim to be an agent or another
person. Backpressure is natural: each side awaits the other's write.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

try:
    from .config import settings
except ImportError:  # uvicorn app:app from package dir
    from config import settings

log = logging.getLogger("billet-web.engine")

router = APIRouter()

# Live engine-bridge count (one per connected web client). Read by /healthz so the
# deploy path (install_webd.sh --restart) can hold off restarting the SHARED daemon
# while users are mid-session — a restart tears every bridge down at once.
_active_bridges = 0


def active_bridge_count() -> int:
    return _active_bridges


# Engine responses can carry whole doc-JSON snapshots; the default 64 KiB
# StreamReader limit would kill the bridge mid-session.
_LINE_LIMIT = 256 * 1024 * 1024


def _inject_identity(line: str, email: str, display: str) -> str:
    """Rewrite a hello line with gateway-verified identity; pass others through."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict) or msg.get("method") != "hello":
        return line
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    params["client"] = "gui"  # web users are humans, never agent/mcp
    params["user"] = {"id": email, "display": display}
    msg["params"] = params
    return json.dumps(msg, separators=(",", ":"))


# Agent-turn RPCs may pin provider/model (and sometimes credentials) from the web
# client's local settings. Credentials MUST stay server-side: the shared hostd already
# resolves keys/tokens from the host user's config.json by provider name, so we drop
# secret-bearing fields only. provider + model SELECTION is allowed through so a web
# user can pick among the host's signed-in accounts (see GET /api/providers).
#
# Multi-tenant caveat: all web turns still spend the host user's accounts; per-user
# billing isolation is out of scope. If no provider is selected, hostd keeps using its
# default/active account (same as before).
_AGENT_PROVIDER_METHODS = frozenset({
    "agent_run_v2", "agent_ask_v2", "agent_clarify_v1", "agent_mission_v1",
    "session_btw", "session_handoff",
})
# Secrets / server-owned endpoint config — never accept from the browser.
_STRIP_SECRET_FIELDS = ("api_key", "base_url", "fallback_api_keys", "auth_mode")

# agent_list_models is a C++ probe in libaicad — it does NOT load host config.json.
# The WASM client has empty/IDBFS keys, so without a server-side inject the probe
# returns "no api key" / HTTP 401 and the picker looks disconnected from hostd.
# Fill api_key + base_url from the host config when the client omitted them (or
# sent a non-secret presence sentinel). Credentials never flow back to the browser.
_LIST_MODELS_METHOD = "agent_list_models"
_PRESENCE_SENTINELS = frozenset({"", "\x01", "host", "shared", "1"})


def _load_host_keys() -> dict[str, str]:
    """Return keys.* string slots from host config (empty dict on failure)."""
    try:
        try:
            from .providers import _load_host_config  # type: ignore
        except ImportError:
            from providers import _load_host_config  # type: ignore
        cfg = _load_host_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("list_models inject: config load failed: %s", exc)
        return {}
    keys = cfg.get("keys") if isinstance(cfg, dict) else None
    if not isinstance(keys, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in keys.items():
        if isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


def _key_for_list_models(provider: str, base_url: str, keys: dict[str, str]) -> tuple[str, str]:
    """Pick (api_key, base_url) for a list_models probe from host keys + URL hints."""
    prov = (provider or "").strip().lower()
    url = (base_url or "").strip()
    url_l = url.lower()

    # URL wins over bare provider name (client often sends provider=openai + OpenRouter URL).
    if "openrouter.ai" in url_l:
        return (
            keys.get("openrouter") or keys.get("cloud_compat") or "",
            url or "https://openrouter.ai/api/v1",
        )
    if "moonshot.ai" in url_l or "api.moonshot" in url_l:
        return keys.get("moonshot") or "", url or "https://api.moonshot.ai/v1"
    if "api.x.ai" in url_l:
        return keys.get("xai") or "", url or "https://api.x.ai/v1"
    if "generativelanguage.googleapis.com" in url_l or prov == "gemini":
        return keys.get("gemini") or "", url
    if "api.anthropic.com" in url_l or prov == "anthropic":
        return keys.get("anthropic") or "", url
    if "localhost" in url_l or "127.0.0.1" in url_l or "[::1]" in url_l:
        return keys.get("openai") or "", url or "http://127.0.0.1:1234/v1"

    if prov == "xai":
        return keys.get("xai") or "", url or "https://api.x.ai/v1"
    if prov == "openai":
        # Prefer dedicated openrouter key when that's the host's cloud path and no URL given.
        if not url and keys.get("openrouter"):
            return keys["openrouter"], "https://openrouter.ai/api/v1"
        if not url and keys.get("cloud_compat"):
            return keys["cloud_compat"], "https://openrouter.ai/api/v1"
        return (
            keys.get("openai") or keys.get("openrouter") or keys.get("cloud_compat") or "",
            url or "https://api.openai.com/v1",
        )
    if prov == "llamacpp":
        return "", url or "http://127.0.0.1:1234/v1"
    if prov == "moonshot":
        return keys.get("moonshot") or "", url or "https://api.moonshot.ai/v1"
    if prov == "openrouter":
        return (
            keys.get("openrouter") or keys.get("cloud_compat") or "",
            url or "https://openrouter.ai/api/v1",
        )
    return "", url


def _inject_list_models_creds(line: str) -> str:
    """Fill empty/sentinel api_key + base_url on agent_list_models from host config."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict) or msg.get("method") != _LIST_MODELS_METHOD:
        return line
    params = msg.get("params")
    if not isinstance(params, dict):
        return line

    provider = str(params.get("provider") or "")
    base_url = str(params.get("base_url") or "")
    api_key = str(params.get("api_key") or "")

    need_key = api_key.strip() in _PRESENCE_SENTINELS
    need_url = not base_url.strip()
    if not need_key and not need_url:
        return line

    keys = _load_host_keys()
    inj_key, inj_url = _key_for_list_models(provider, base_url, keys)
    changed = False
    if need_key and inj_key:
        params["api_key"] = inj_key
        changed = True
    if need_url and inj_url:
        params["base_url"] = inj_url
        changed = True
    if not changed:
        return line
    msg["params"] = params
    return json.dumps(msg, separators=(",", ":"))



def _sanitize_agent_provider(line: str) -> str:
    """Drop client-supplied secrets from agent-turn RPCs; keep provider/model selection."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict) or msg.get("method") not in _AGENT_PROVIDER_METHODS:
        return line
    params = msg.get("params")
    if not isinstance(params, dict):
        return line
    if not any(k in params for k in _STRIP_SECRET_FIELDS):
        return line
    for k in _STRIP_SECRET_FIELDS:
        params.pop(k, None)
    return json.dumps(msg, separators=(",", ":"))


@router.websocket("/engine")
async def engine_ws(ws: WebSocket) -> None:
    user = ws.state.user  # AuthMiddleware closed 4401/4403 already if unauthenticated
    await ws.accept()
    sock = str(settings.webd_socket)
    try:
        reader, writer = await asyncio.open_unix_connection(sock, limit=_LINE_LIMIT)
    except OSError as exc:
        log.warning("engine bridge: webd connect failed for %s: %s", user.email, exc)
        await ws.close(code=1013, reason="engine unavailable")
        return
    log.info("engine bridge open: %s -> %s", user.email, sock)
    global _active_bridges
    _active_bridges += 1

    async def ws_to_daemon() -> None:
        while True:
            line = await ws.receive_text()
            # Cheap pre-filters (method sits near the front of the JSON-RPC line);
            # the helpers re-check the method properly before rewriting.
            head = line[:96]
            if '"hello"' in head:
                line = _inject_identity(line, user.email, user.display)
            elif '"agent_list_models"' in head:
                line = _inject_list_models_creds(line)
            elif '"agent_' in head or '"session_btw"' in head or '"session_handoff"' in head:
                line = _sanitize_agent_provider(line)
            writer.write(line.encode("utf-8") + b"\n")
            await writer.drain()

    async def daemon_to_ws() -> None:
        while True:
            raw = await reader.readline()
            if not raw:  # daemon EOF (drain/restart)
                break
            await ws.send_text(raw.decode("utf-8", errors="replace").rstrip("\n"))

    tasks = (
        asyncio.create_task(ws_to_daemon(), name="engine-ws2d"),
        asyncio.create_task(daemon_to_ws(), name="engine-d2ws"),
    )
    client_gone = False
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:  # surface unexpected errors (disconnects are normal)
            exc = t.exception()
            if isinstance(exc, WebSocketDisconnect):
                client_gone = True
            elif exc and not isinstance(exc, ConnectionError):
                log.warning("engine bridge %s: %r", user.email, exc)
    except asyncio.CancelledError:
        client_gone = True  # session torn down under us — cleanup is all we owe
    finally:
        for t in tasks:
            t.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        writer.close()  # sync; skip wait_closed — nothing left to flush
        # Only close the WS when the daemon side ended it; racing a close
        # against a client-initiated disconnect trips ASGI double-close.
        if not client_gone:
            try:
                await ws.close(code=1012)  # daemon gone: client should reconnect
            except Exception:
                pass  # peer vanished mid-close
        _active_bridges -= 1
        log.info("engine bridge closed: %s (%d active)", user.email, _active_bridges)
