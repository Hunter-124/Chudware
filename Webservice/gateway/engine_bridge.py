"""WS /engine — bridge browser clients to the shared chudware-webd unix socket.

One WebSocket connection carries newline-delimited JSON-RPC in both directions.
The bridge asserts authenticated browser identity, jails filesystem paths, reserves
per-user daemon slots, keeps host credentials on canonical provider origins,
and blocks the administrator-only provider-manager surface for standard users.
Backpressure is natural: each side awaits the other's write.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

try:
    from .config import settings
    from .limits import user_slug
    from .sessions import manager
except ImportError:  # uvicorn app:app from package dir
    from config import settings
    from limits import user_slug
    from sessions import manager

log = logging.getLogger("chudware-web.engine")

router = APIRouter()

# Live engine-bridge count (one per reserved web client). Read by /healthz so the
# deploy path (install_webd.sh --restart) can hold off restarting the SHARED daemon
# while users are mid-handshake or mid-session — a restart tears every bridge down.
_active_bridges = 0
# email -> concurrent reserved bridges, so one account cannot fan out across the
# shared daemon. The lock makes the check-and-increment atomic even under threaded
# ASGI test runners; production never awaits inside either mutation.
_bridge_owners: dict[str, int] = {}
_bridge_lock = threading.Lock()


def active_bridge_count() -> int:
    with _bridge_lock:
        return _active_bridges


def _reserve_bridge(email: str) -> tuple[bool, int]:
    """Atomically reserve one per-user/global bridge slot."""
    global _active_bridges
    with _bridge_lock:
        mine = _bridge_owners.get(email, 0)
        if mine >= settings.max_engine_bridges_per_user:
            return False, mine
        _bridge_owners[email] = mine + 1
        _active_bridges += 1
        return True, mine + 1


def _release_bridge(email: str) -> tuple[int, int]:
    """Release one previously reserved slot and return (user, global) counts."""
    global _active_bridges
    with _bridge_lock:
        mine = _bridge_owners[email] - 1
        if mine:
            _bridge_owners[email] = mine
        else:
            _bridge_owners.pop(email)
        _active_bridges -= 1
        return mine, _active_bridges


# Engine responses can carry whole doc-JSON snapshots; the default 64 KiB
# StreamReader limit would kill the bridge mid-session. Cap at the configured
# per-frame maximum — anything near the old 256 MiB limit OOM-kills a 1 GiB
# gateway (MemoryMax=1G).
def _line_limit() -> int:
    return settings.ws_max_message_bytes


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


# Daemon RPCs address files by ABSOLUTE host path (host/src/core/fs_backend.h:
# doc_open/doc_save/export_file take the same abs paths /workspace/list hands
# out). The daemon is SHARED by every web user and resolves whatever path it is
# handed, so the gateway is the only place per-account containment can be
# enforced: without this check any authenticated user could doc_open another
# account's part by guessing its path, defeating the /workspace isolation
# entirely. Audit 2026-07-28, "engine bridge is a shared daemon pipe".
_PATH_KEYS = frozenset({
    "path", "src", "dst", "dest", "file", "filename", "out", "out_path",
    "output", "output_path", "source", "target", "aicm_path", "export_path",
    "doc_path", "save_path", "project_dir", "dir",
})


def _collect_paths(node: Any, found: list[str]) -> None:
    """Gather every string sitting under a path-ish key, at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and key.lower() in _PATH_KEYS:
                found.append(value)
            else:
                _collect_paths(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_paths(item, found)


def path_violation(line: str, allowed_roots: list[PurePosixPath]) -> str | None:
    """Return the offending path when an RPC addresses a file outside the caller's roots.

    Only ABSOLUTE paths are checked: a relative path is resolved by the daemon
    against its own working directory, never against another account's tree, so
    it is not a cross-tenant vector. A non-JSON or path-free line is fine.
    """
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    found: list[str] = []
    _collect_paths(msg, found)
    for raw in found:
        if not raw or not raw.startswith("/"):
            continue
        # normpath, not resolve() — the path names a file on the daemon's host,
        # may legitimately not exist yet (doc_save), and resolving would do
        # filesystem I/O plus symlink following on the event loop.
        candidate = PurePosixPath(os.path.normpath(raw))
        if not any(candidate == root or root in candidate.parents for root in allowed_roots):
            return raw
    return None


# Agent-turn RPCs may pin provider/model (and sometimes credentials) from the web
# client's local settings. Credentials MUST stay server-side: the shared hostd already
# resolves keys/tokens from the host user's config.json by provider name, so we drop
# secret-bearing fields only. provider + model SELECTION is allowed through so a web
# user can use the host's administrator-managed shared providers.
#
# Multi-tenant caveat: all web turns still spend the host user's accounts; per-user
# billing isolation is out of scope. If no provider is selected, hostd keeps using its
# default/active account (same as before).
_AGENT_TURN_PROVIDER_METHODS = frozenset({
    "agent_run_v2", "agent_ask_v2", "agent_clarify_v1", "agent_mission_v1",
    "session_btw", "session_handoff",
})
# Secrets / server-owned endpoint config — never accept from the browser.
_STRIP_SECRET_FIELDS = ("api_key", "base_url", "fallback_api_keys", "auth_mode")

# agent_list_models is a C++ probe in libchudware — it does NOT load host config.json.
# The WASM client has empty/IDBFS keys, so without a server-side inject the probe
# returns "no api key" / HTTP 401 and the picker looks disconnected from hostd.
# Fill api_key + base_url from the host config when the client omitted them (or
# sent a non-secret presence sentinel). Credentials never flow back to the browser.
_LIST_MODELS_METHOD = "agent_list_models"
_PRESENCE_SENTINELS = frozenset({"", "\x01", "host", "shared", "1"})


def _load_host_keys() -> dict[str, str]:
    """Return keys.* string slots from host config (+ ai_providers.env fallback)."""
    out: dict[str, str] = {}
    try:
        try:
            from .providers import _load_host_config  # type: ignore
        except ImportError:
            from providers import _load_host_config  # type: ignore
        cfg = _load_host_config()
    except Exception:  # noqa: BLE001
        # Exception text from credential loaders is not safe to reflect into logs.
        log.warning("list_models inject: host credential config unavailable")
        cfg = {}
    keys = cfg.get("keys") if isinstance(cfg, dict) else None
    if isinstance(keys, dict):
        for k, v in keys.items():
            if isinstance(v, str) and v.strip():
                out[str(k)] = v.strip()

    # Fallback: ~/chudware-web-data/ai_providers.env (shared session keys).
    # Map ENV names → keys.* slots used by list_models inject.
    env_to_slot = {
        "OPENROUTER_API_KEY": "openrouter",
        "GEMINI_API_KEY": "gemini",
        "ANTHROPIC_API_KEY": "anthropic",
        "OPENAI_API_KEY": "openai",
        "XAI_API_KEY": "xai",
        "MOONSHOT_API_KEY": "moonshot",
        "CLOUD_COMPAT_API_KEY": "cloud_compat",
        "FREELLMAPI_API_KEY": "freellmapi",
    }
    try:
        env_vars = settings.ai_provider_vars()
    except Exception:  # noqa: BLE001
        env_vars = {}
    for env_name, slot in env_to_slot.items():
        if slot in out:
            continue
        val = env_vars.get(env_name)
        if isinstance(val, str) and val.strip():
            out[slot] = val.strip()
    return out


_CANONICAL_MODEL_ENDPOINTS: dict[
    tuple[str, str, int, str],
    tuple[str, tuple[str, ...], frozenset[str]],
] = {
    ("https", "openrouter.ai", 443, "/api/v1"): (
        "https://openrouter.ai/api/v1",
        ("openrouter", "cloud_compat"),
        frozenset({"openai", "openrouter", "cloud_compat"}),
    ),
    ("https", "api.openai.com", 443, "/v1"): (
        "https://api.openai.com/v1",
        ("openai",),
        frozenset({"openai"}),
    ),
    ("https", "api.x.ai", 443, "/v1"): (
        "https://api.x.ai/v1",
        ("xai",),
        frozenset({"openai", "xai"}),
    ),
    ("https", "api.moonshot.ai", 443, "/v1"): (
        "https://api.moonshot.ai/v1",
        ("moonshot",),
        frozenset({"openai", "moonshot", "cloud_compat"}),
    ),
    ("https", "api.deepseek.com", 443, "/v1"): (
        "https://api.deepseek.com/v1",
        ("cloud_compat",),
        frozenset({"openai", "cloud_compat"}),
    ),
    ("https", "dashscope-intl.aliyuncs.com", 443, "/compatible-mode/v1"): (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ("cloud_compat",),
        frozenset({"openai", "cloud_compat"}),
    ),
    ("https", "generativelanguage.googleapis.com", 443, ""): (
        "https://generativelanguage.googleapis.com",
        ("gemini",),
        frozenset({"gemini", "google"}),
    ),
    ("https", "generativelanguage.googleapis.com", 443, "/v1beta"): (
        "https://generativelanguage.googleapis.com/v1beta",
        ("gemini",),
        frozenset({"gemini", "google"}),
    ),
    ("https", "api.anthropic.com", 443, ""): (
        "https://api.anthropic.com",
        ("anthropic",),
        frozenset({"anthropic"}),
    ),
    ("https", "api.anthropic.com", 443, "/v1"): (
        "https://api.anthropic.com/v1",
        ("anthropic",),
        frozenset({"anthropic"}),
    ),
    ("http", "127.0.0.1", 1234, "/v1"): (
        "http://127.0.0.1:1234/v1",
        (),
        frozenset({"openai", "llamacpp", "local"}),
    ),
    ("http", "localhost", 1234, "/v1"): (
        "http://localhost:1234/v1",
        (),
        frozenset({"openai", "llamacpp", "local"}),
    ),
    ("http", "::1", 1234, "/v1"): (
        "http://[::1]:1234/v1",
        (),
        frozenset({"openai", "llamacpp", "local"}),
    ),
    ("http", "127.0.0.1", 3001, "/v1"): (
        "http://127.0.0.1:3001/v1",
        ("freellmapi",),
        frozenset({"freellmapi"}),
    ),
    ("http", "localhost", 3001, "/v1"): (
        "http://localhost:3001/v1",
        ("freellmapi",),
        frozenset({"freellmapi"}),
    ),
    ("http", "::1", 3001, "/v1"): (
        "http://[::1]:3001/v1",
        ("freellmapi",),
        frozenset({"freellmapi"}),
    ),
}


def _canonical_models_endpoint(
    provider: str,
    base_url: str,
) -> tuple[str, tuple[str, ...]] | None:
    """Resolve an explicit model-probe URL to one exact, provider-compatible endpoint."""
    url = (base_url or "").strip()
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider endpoint not allowed") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
    ):
        raise ValueError("provider endpoint not allowed")
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if port is None:
        port = 443 if scheme == "https" else 80
    path = parsed.path
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path[:-1]
    endpoint = _CANONICAL_MODEL_ENDPOINTS.get((scheme, host, port, path))
    prov = (provider or "").strip().lower()
    if endpoint is None or prov not in endpoint[2]:
        raise ValueError("provider endpoint not allowed")
    return endpoint[0], endpoint[1]


def _key_for_list_models(provider: str, base_url: str, keys: dict[str, str]) -> tuple[str, str]:
    """Pick a host key only for a canonical endpoint compatible with the provider."""
    prov = (provider or "").strip().lower()
    explicit = _canonical_models_endpoint(prov, base_url)
    if explicit is not None:
        canonical, slots = explicit
        return next((keys[slot] for slot in slots if keys.get(slot)), ""), canonical

    if prov in {"gemini", "google"}:
        return keys.get("gemini") or "", ""
    if prov == "anthropic":
        return keys.get("anthropic") or "", ""
    if prov == "xai":
        return keys.get("xai") or "", "https://api.x.ai/v1"
    if prov == "openai":
        # Preserve the host's existing OpenRouter-backed default when no endpoint
        # was supplied, while fixing the destination to its canonical origin.
        if keys.get("openrouter"):
            return keys["openrouter"], "https://openrouter.ai/api/v1"
        if keys.get("cloud_compat"):
            return keys["cloud_compat"], "https://openrouter.ai/api/v1"
        return keys.get("openai") or "", "https://api.openai.com/v1"
    if prov in {"llamacpp", "local"}:
        return "", "http://127.0.0.1:1234/v1"
    if prov == "freellmapi":
        return keys.get("freellmapi") or "", "http://localhost:3001/v1"
    if prov == "moonshot":
        return keys.get("moonshot") or "", "https://api.moonshot.ai/v1"
    if prov == "openrouter":
        return (
            keys.get("openrouter") or keys.get("cloud_compat") or "",
            "https://openrouter.ai/api/v1",
        )
    return "", ""


def _inject_list_models_creds(line: str) -> tuple[str, str | None]:
    """Return a credential-filled line, or a generic rejection safe to send to the client."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line, None
    if not isinstance(msg, dict) or msg.get("method") != _LIST_MODELS_METHOD:
        return line, None
    params = msg.get("params")
    if not isinstance(params, dict):
        return line, None

    provider = str(params.get("provider") or "")
    base_url = str(params.get("base_url") or "")
    api_key = str(params.get("api_key") or "")
    try:
        # Validation happens even when the caller supplied a non-sentinel key:
        # arbitrary endpoints are never forwarded through this privileged probe.
        keys = _load_host_keys()
        inj_key, inj_url = _key_for_list_models(provider, base_url, keys)
    except ValueError:
        return line, "provider endpoint not allowed"

    need_key = api_key.strip() in _PRESENCE_SENTINELS
    changed = False
    if need_key and inj_key:
        params["api_key"] = inj_key
        changed = True
    if inj_url and inj_url != base_url:
        params["base_url"] = inj_url
        changed = True
    if not changed:
        return line, None
    msg["params"] = params
    return json.dumps(msg, separators=(",", ":")), None


def _rpc_method(line: str) -> str:
    try:
        msg = json.loads(line)
    except ValueError:
        return ""
    method = msg.get("method") if isinstance(msg, dict) else None
    return method if isinstance(method, str) else ""


def _rpc_error(line: str, code: int, message: str) -> str:
    call_id: Any = None
    try:
        msg = json.loads(line)
        if isinstance(msg, dict):
            call_id = msg.get("id")
    except ValueError:
        pass
    return json.dumps(
        {"id": call_id, "error": {"code": code, "message": message}},
        separators=(",", ":"),
    )


def _sanitize_agent_provider(line: str) -> str:
    """Drop client-supplied secrets from agent-turn RPCs; keep provider/model selection."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict) or msg.get("method") not in _AGENT_TURN_PROVIDER_METHODS:
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
    # AUDIT (multi-tenant): this bridge attaches the caller to the ONE shared
    # webd daemon. It deliberately does NOT require a podman seat — /app is the
    # browser-native WASM client and is a separate delivery mode from the
    # streamed desktop, so demanding a container session here takes the whole
    # web client offline (regression 2026-07-28: "Engine daemon unreachable").
    # Isolation is enforced where it actually belongs: authenticated identity,
    # per-account path roots, provider-manager authorization, canonical provider
    # endpoints, bounded frames, and an atomic per-account connection cap.
    reserved, mine = _reserve_bridge(user.email)
    if not reserved:
        log.warning("engine bridge refused for %s: %d already open", user.email, mine)
        await ws.close(code=4429, reason="too many engine connections")
        return

    writer: asyncio.StreamWriter | None = None
    tasks: tuple[asyncio.Task[None], ...] = ()
    client_gone = False
    try:
        # The slot is already visible before this first await, so two handshakes
        # cannot both pass the cap while accept/connect is suspended.
        await ws.accept()
        sock = str(settings.webd_socket)
        try:
            reader, writer = await asyncio.open_unix_connection(sock, limit=_line_limit())
        except OSError:
            # Avoid reflecting exception text from lower credential/config layers.
            log.warning("engine bridge: webd connect failed for %s", user.email)
            await ws.close(code=1013, reason="engine unavailable")
            return
        log.info("engine bridge open: %s -> %s", user.email, sock)

        # The absolute paths this caller may name in a daemon RPC: their own
        # workspace + bucket trees, plus the shared scratch dirs the client uses
        # for mesh/tess payloads (those carry no other account's data).
        slug = user_slug(user.email)
        allowed_roots = [
            PurePosixPath(os.path.normpath(str(settings.user_workspace_root(slug)))),
            PurePosixPath(os.path.normpath(str(settings.user_bucket_root(slug)))),
            PurePosixPath("/dev/shm"),
            PurePosixPath("/tmp"),
        ]

        async def ws_to_daemon() -> None:
            max_frame = settings.ws_max_message_bytes
            while True:
                line = await ws.receive_text()
                if len(line.encode("utf-8")) > max_frame:
                    # One oversized frame can OOM the gateway; never forward it.
                    log.warning(
                        "engine bridge %s: frame exceeds %d bytes; closing",
                        user.email,
                        max_frame,
                    )
                    await ws.close(code=1009, reason="frame too large")
                    return

                method = _rpc_method(line)
                if method.startswith("agent_provider_") and user.role != "admin":
                    # Provider login/state mutates or reveals the administrator's
                    # shared-host auth store. Role comes only from AuthMiddleware.
                    log.warning(
                        "engine bridge %s: rejected administrator provider RPC",
                        user.email,
                    )
                    await ws.send_text(_rpc_error(
                        line,
                        -32003,
                        "administrator authorization required",
                    ))
                    continue

                offender = path_violation(line, allowed_roots)
                if offender is not None:
                    # Do NOT tear the socket down: one bad path is a client bug or a
                    # probe, and killing the bridge would lose the user's live document.
                    log.warning(
                        "engine bridge %s: rejected out-of-jail path %r",
                        user.email,
                        offender,
                    )
                    await ws.send_text(_rpc_error(
                        line,
                        -32001,
                        "path outside your workspace",
                    ))
                    continue

                if method == "hello":
                    line = _inject_identity(line, user.email, user.display)
                elif method == _LIST_MODELS_METHOD:
                    line, error = _inject_list_models_creds(line)
                    if error is not None:
                        # Never log the caller URL or any loaded host credential.
                        log.warning(
                            "engine bridge %s: rejected non-canonical model endpoint",
                            user.email,
                        )
                        await ws.send_text(_rpc_error(line, -32002, error))
                        continue
                elif method in _AGENT_TURN_PROVIDER_METHODS:
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
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:  # surface unexpected errors (disconnects are normal)
            exc = task.exception()
            if isinstance(exc, WebSocketDisconnect):
                client_gone = True
            elif exc and not isinstance(exc, ConnectionError):
                log.warning("engine bridge %s: %r", user.email, exc)
    except asyncio.CancelledError:
        client_gone = True  # session torn down under us — cleanup is all we owe
    finally:
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    pass
            if writer is not None:
                writer.close()  # sync; skip wait_closed — nothing left to flush
                # Only close the WS when the daemon side ended it; racing a close
                # against a client-initiated disconnect trips ASGI double-close.
                if not client_gone:
                    try:
                        await ws.close(code=1012)  # daemon gone: client should reconnect
                    except Exception:
                        pass  # peer vanished mid-close
        finally:
            # One reservation, one release: this runs for accept, connect, forward,
            # cancellation, and cleanup failures, including returns above.
            _, active = _release_bridge(user.email)
            log.info("engine bridge closed: %s (%d active)", user.email, active)
