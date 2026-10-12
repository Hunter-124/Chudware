"""WS /engine — bridge browser clients to the shared chudware-webd unix socket.

One WebSocket connection carries newline-delimited JSON-RPC in both directions.
The bridge asserts authenticated browser identity, jails filesystem paths, reserves
per-user daemon slots, keeps host credentials on canonical provider origins,
and blocks the administrator-only provider-manager surface for standard users.
Backpressure is natural: each side awaits the other's write.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

try:
    from . import orgs
    from . import authn
    from .accounts import get_account
    from .auth import AuthError, revalidate_websocket_user
    from .providers import default_model_for
    from .credits import actor_for, reconcile_reservation, reserve_turn, usage_cost
    from .config import settings
    from .provider_keys import resolve_validated_key
    from .provider_accounts import resolve_subscription_token
    from .limits import user_slug
    from .plans import limits_for
except ImportError:  # uvicorn app:app from package dir
    import orgs
    import authn
    from accounts import get_account
    from auth import AuthError, revalidate_websocket_user
    from credits import actor_for, reconcile_reservation, reserve_turn, usage_cost
    from config import settings
    from providers import default_model_for
    from provider_keys import resolve_validated_key
    from provider_accounts import resolve_subscription_token
    from limits import user_slug
    from plans import limits_for

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

# HTTP mesh fetches outlive their WS bridge. Only daemon-confirmed document
# grants enter this process-local, bounded capability table.
_mesh_grants: OrderedDict[tuple[str, str], float] = OrderedDict()
_mesh_generation: tuple[int, int, int] | None = None
_mesh_grants_lock = threading.Lock()
_MESH_GRANT_TTL = 24 * 60 * 60
_MESH_GRANT_LIMIT = 8192
# aicm_handoff scopes every published name by the publishing engine's process id
# (chudware_p<pid>_<doc>_r<rev>, fallback p<pid>_<doc>_r<rev>.aicm); the doc id follows it.
_FILE_MESH_RE = re.compile(r"^p\d+_(?P<doc>[A-Za-z0-9_.-]{1,180})_r\d+\.aicm$")
_SHM_MESH_RE = re.compile(r"^chudware_p\d+_(?P<doc>[A-Za-z0-9_.-]{1,180})_r\d+$")
_SHM_MESH_DIR = PurePosixPath("/dev/shm")


@functools.lru_cache(maxsize=8)
def _mesh_file_dirs_for(tempdir: str) -> frozenset[PurePosixPath]:
    leaf = "chudware_cpp_docs"
    return frozenset({PurePosixPath(os.path.normpath(tempdir)) / leaf,
                      PurePosixPath(os.path.realpath(tempdir)) / leaf})


def mesh_file_dirs() -> frozenset[PurePosixPath]:
    """aicm_handoff's file-fallback directory under the effective temp root.

    The engine writes <temp_directory_path()>/chudware_cpp_docs, i.e. $TMPDIR (else
    /tmp) — a private service account's own TMPDIR, not a fixed /tmp. Both the
    spelled and the symlink-resolved form name that one directory.
    """
    return _mesh_file_dirs_for(tempfile.gettempdir())


def mesh_doc_id(name: str, *, shm: bool = False) -> str | None:
    match = (_SHM_MESH_RE if shm else _FILE_MESH_RE).fullmatch(name)
    return match.group("doc") if match else None


def _mesh_socket_generation() -> tuple[int, int, int] | None:
    try:
        stat = os.stat(settings.webd_socket)
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_ctime_ns


def grant_mesh_document(email: str, doc_id: str) -> None:
    global _mesh_generation
    generation = _mesh_socket_generation()
    if generation is None:
        return
    now = time.monotonic()
    key = (email.strip().lower(), doc_id)
    with _mesh_grants_lock:
        if generation != _mesh_generation:
            _mesh_grants.clear()
            _mesh_generation = generation
        for old_key, expires in list(_mesh_grants.items()):
            if expires <= now:
                del _mesh_grants[old_key]
        _mesh_grants.pop(key, None)
        _mesh_grants[key] = now + _MESH_GRANT_TTL
        while len(_mesh_grants) > _MESH_GRANT_LIMIT:
            _mesh_grants.popitem(last=False)


def mesh_document_authorized(email: str, doc_id: str) -> bool:
    global _mesh_generation
    generation = _mesh_socket_generation()
    if generation is None:
        return False
    key = (email.strip().lower(), doc_id)
    now = time.monotonic()
    with _mesh_grants_lock:
        if generation != _mesh_generation:
            _mesh_grants.clear()
            _mesh_generation = generation
            return False
        expiry = _mesh_grants.get(key, 0)
        if expiry <= now:
            _mesh_grants.pop(key, None)
            return False
        _mesh_grants.move_to_end(key)
        return True


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
    "doc_path", "save_path", "project_dir", "dir", "workspace_dir",
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


def path_violation(
    line: str, allowed_roots: list[PurePosixPath],
    document_capabilities: set[str] | frozenset[str] = frozenset(),
) -> str | None:
    """Return a path value that is relative or outside the caller's roots.

    Daemon path-bearing RPC fields must always carry canonical absolute paths:
    allowing relative values delegates resolution to the shared daemon's working
    directory and bypasses the caller-root containment check. A non-JSON or
    path-free line is fine.
    """
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    found: list[str] = []
    _collect_paths(msg, found)
    for raw in found:
        if not raw or not raw.startswith("/"):
            return raw
        # normpath, not resolve() — the path names a file on the daemon's host,
        # may legitimately not exist yet (doc_save), and resolving would do
        # filesystem I/O plus symlink following on the event loop.
        candidate = PurePosixPath(os.path.normpath(raw))
        # Broad /tmp and /dev/shm roots must never grant access to arbitrary
        # daemon-host files. Personal workspace roots may themselves live in
        # /tmp (development and tests), and remain authorized.
        if any(candidate == root or root in candidate.parents for root in allowed_roots
               if root not in {PurePosixPath("/tmp"), _SHM_MESH_DIR}):
            continue
        if candidate.parent in mesh_file_dirs():
            doc_id = mesh_doc_id(candidate.name)
        elif candidate.parent == _SHM_MESH_DIR:
            doc_id = mesh_doc_id(candidate.name, shm=True)
        else:
            return raw
        if doc_id is None or doc_id not in document_capabilities:
            return raw
    return None


# Agent RPCs may pin provider/model (and sometimes credentials) from the web
# client's local settings. Credentials MUST stay server-side. The gateway also
# owns the billing actor: a browser-supplied copy is removed before the current
# account state is injected under options, the object run.cpp actually receives.
_AGENT_TURN_PROVIDER_METHODS = frozenset({
    "agent_run_v2", "agent_ask_v2", "agent_clarify_v1", "agent_mission_v1",
    "session_btw", "session_handoff",
})
# The subscription (OAuth) credentials these RPCs read and write live in the ONE
# shared chudware-hostd process, in its single process-wide sub_auth store: there
# is no per-tenant copy. A tenant calling subscription_login therefore replaces
# the operator's Claude/Codex/Cursor/xAI/Kimi credential for everybody, and the
# flow runs on the server — anthropic_sub_provider/codex_sub_provider call
# webbrowser.open() and bind a loopback callback (localhost:54545,
# localhost:1455) on the gateway host, which no remote caller's browser reaches.
# The read-only three leak operator identity: providers.py withholds account
# emails and labels from non-admins (include_operator_detail), and raw
# subscription_status / _accounts / _usage answer with none of that scrubbing.
# Named one by one, never a "subscription_" prefix test, so a future
# subscription RPC has to be classified deliberately at review instead of
# silently inheriting whichever policy the prefix happened to imply.
_OPERATOR_SUBSCRIPTION_METHODS = frozenset({
    "subscription_login", "subscription_logout", "subscription_switch",
    "subscription_status", "subscription_accounts", "subscription_usage",
})
# Secrets / server-owned endpoint config — never accept from the browser.
_STRIP_SECRET_FIELDS = (
    "api_key", "base_url", "fallback_api_keys", "auth_mode", "subscription",
)

# agent_list_models is a C++ probe in libchudware — it does NOT load host config.json.
# A single-tenant operator may explicitly share the host profile so the WASM
# model picker works. Multi-tenant deployments must leave sharing disabled:
# model probes then carry no operator credential, just like inference turns.
_LIST_MODELS_METHOD = "agent_list_models"
_PRESENCE_SENTINELS = frozenset({"", "\x01", "host", "shared", "1"})


def _load_host_keys() -> dict[str, str]:
    """Load explicitly shared host keys; return empty in multi-tenant mode."""
    if not settings.share_provider_keys:
        # Do not even read host credential stores when the security boundary is
        # closed. The metered pool resolves its key downstream from the
        # server-authoritative actor path and never enters this compatibility loader.
        return {}
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
    """Validate a model probe and optionally add single-tenant host credentials."""
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
        # Validation happens even when sharing is disabled or the caller supplied
        # a non-sentinel key: arbitrary endpoints never reach this privileged probe.
        keys = _load_host_keys() if settings.share_provider_keys else {}
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

def _rpc_result(line: str, result: Mapping[str, Any]) -> str:
    call_id: Any = None
    try:
        msg = json.loads(line)
        if isinstance(msg, dict):
            call_id = msg.get("id")
    except ValueError:
        pass
    return json.dumps({"id": call_id, "result": dict(result)}, separators=(",", ":"))

# The native daemon owns one process-wide document registry. A web socket may
# address only documents it minted/opened itself, or documents whose canonical
# saved path proves they belong to the authenticated account.
_DOC_ID_KEYS = frozenset({"doc_id", "document_id", "source_doc_id", "target_doc_id"})
# `import_file` is the daemon wire name of doc_import_file: without `into` it mints
# a new document (the import lane for STEP/IGES/DWG/DXF/SAT/mesh files), so the
# caller must be granted the doc it gets back exactly like doc_open.
_DOC_MINT_METHODS = frozenset(
    {"doc_new", "doc_open", "import_file", "doc_import", "doc_import_file"}
)


def _collect_doc_ids(node: Any, found: list[str]) -> None:
    """Gather document identifiers at any depth, including batch requests."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _DOC_ID_KEYS and isinstance(value, str) and value:
                found.append(value)
            _collect_doc_ids(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_doc_ids(item, found)


def _request_doc_ids(line: str) -> set[str]:
    try:
        message = json.loads(line)
    except ValueError:
        return set()
    found: list[str] = []
    if isinstance(message, dict):
        params = message.get("params")
        _collect_doc_ids(params, found)
        # A top-level `into` names the TARGET document of import_file and
        # vendor_part_insert; importing into another account's doc is a write to it.
        into = params.get("into") if isinstance(params, dict) else None
        if isinstance(into, str) and into:
            found.append(into)
    return set(found)


def _owned_doc_entry(entry: Any, allowed_roots: list[PurePosixPath]) -> bool:
    """Prove a doc_list entry belongs to this caller from its canonical path."""
    if not isinstance(entry, dict):
        return False
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        return False
    candidate = PurePosixPath(os.path.normpath(raw))
    return any(
        (candidate == root or candidate.is_relative_to(root))
        for root in allowed_roots if str(root) not in {"/tmp", "/dev/shm"}
    )


def _authorize_daemon_response(
    line: str,
    pending: dict[Any, tuple[str, set[str]]],
    capabilities: set[str],
    allowed_roots: list[PurePosixPath],
    account_email: str = "",
) -> str | None:
    """Update capabilities and filter registry output; None drops an unsafe reply."""
    try:
        message = json.loads(line)
    except ValueError:
        return line
    if not isinstance(message, dict):
        return line
    if "id" not in message:
        # Registry notifications are broadcast by the shared daemon. Apply the
        # same document capabilities as requests before exposing titles/snapshots.
        event_doc_ids: list[str] = []
        _collect_doc_ids(message, event_doc_ids)
        if any(doc_id not in capabilities for doc_id in event_doc_ids):
            return None
        return line
    request = pending.pop(message.get("id"), None)
    if request is None:
        return None
    method, request_ids = request
    if "error" in message:
        return line
    result = message.get("result")
    if method == "doc_list":
        if not isinstance(result, dict) or not isinstance(result.get("docs"), list):
            return None
        kept: list[dict[str, Any]] = []
        for entry in result["docs"]:
            doc_id = entry.get("doc_id") if isinstance(entry, dict) else None
            if not isinstance(doc_id, str) or not doc_id:
                continue
            if doc_id in capabilities or _owned_doc_entry(entry, allowed_roots):
                capabilities.add(doc_id)
                if account_email:
                    grant_mesh_document(account_email, doc_id)
                kept.append(entry)
        result["docs"] = kept
        return json.dumps(message, separators=(",", ":"))
    if method in _DOC_MINT_METHODS:
        if not isinstance(result, dict):
            return None
        doc_id = result.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id:
            return None
        capabilities.add(doc_id)
        if account_email:
            grant_mesh_document(account_email, doc_id)
    elif method == "doc_close":
        capabilities.difference_update(request_ids)
    return line


def _agent_provider(line: str) -> str:
    """Provider explicitly selected for this turn, never inferred from a model id."""
    try:
        message = json.loads(line)
    except ValueError:
        return ""
    params = message.get("params") if isinstance(message, dict) else None
    if not isinstance(params, dict):
        return ""
    options = params.get("options")
    if not isinstance(options, dict):
        options = {}
    return str(params.get("provider") or options.get("provider") or "").strip().lower()


def _actor_for_turn(email: str, line: str) -> dict[str, Any]:
    """Prefer BYOK, then a supported own subscription, then trial sponsorship.

    Active verified trial-domain users may run on an active admin's Claude
    subscription without a pool reservation or spend cap. A matching BYOK key
    always wins; only the metered pool falls through after subscription lookup.
    """
    actor = actor_for(email)
    provider = _agent_provider(line)
    key = resolve_validated_key(email, provider) if provider else None
    actor["_byok_validated"] = key is not None
    if key is not None:
        actor["key_source"] = "byok"
        actor["_byok_api_key"] = key
        return actor
    claude_provider = provider in ("anthropic", "anthropic-sub", "claude")
    grant = resolve_subscription_token(
        email, provider if claude_provider else "anthropic",
        trial_provider_override=not claude_provider,
    )
    if grant is not None:
        actor["key_source"] = "subscription"
        actor["_subscription"] = {
            "provider": grant["provider"],
            "access_token": grant["access_token"],
        }
        if grant.get("sponsored"):
            actor["_sponsor"] = {
                "email": grant["owner_email"],
                "scope": grant["scope"],
            }
        if grant.get("sponsored") or provider not in ("anthropic", "anthropic-sub", "claude"):
            actor["_claude_provider_override"] = True
    if actor["key_source"] == "pool":
        actor["_pool"] = {
            "provider": os.environ.get("CHUDWARE_WEB_POOL_PROVIDER", "openrouter").strip().lower(),
            "api_key": os.environ.get("CHUDWARE_WEB_POOL_API_KEY", "").strip(),
            "model": os.environ.get("CHUDWARE_WEB_POOL_MODEL", "").strip(),
        }
    return actor


def _agent_cost_inputs(line: str) -> tuple[str, int, int]:
    """Return model, prompt estimate, requested output cap from an agent RPC."""
    try:
        msg = json.loads(line)
    except ValueError:
        return "", 1, 8192
    params = msg.get("params") if isinstance(msg, dict) else {}
    if not isinstance(params, dict):
        params = {}
    options = params.get("options")
    if not isinstance(options, dict):
        options = {}
    model = str(params.get("model") or options.get("model") or "")
    text = "".join(
        str(params.get(key) or "") for key in ("request", "question", "prompt")
    )
    history = options.get("history") or params.get("history")
    if isinstance(history, list):
        text += "".join(
            str(item.get("text") or item.get("content") or "")
            for item in history if isinstance(item, dict)
        )
    prompt_estimate = max(1, (len(text.encode("utf-8")) + 3) // 4)
    raw_max = (
        options.get("max_output_tokens")
        or options.get("max_tokens")
        or params.get("max_output_tokens")
        or params.get("max_tokens")
        or 8192
    )
    try:
        max_output = max(0, int(raw_max))
    except (TypeError, ValueError):
        max_output = 8192
    return model, prompt_estimate, max_output


def _provider_usage_payload(line: str) -> dict[str, Any] | None:
    try:
        message = json.loads(line)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("event") != "agent.event":
        return None
    data = message.get("data")
    if not isinstance(data, dict) or data.get("kind") != "agent.provider_usage":
        return None
    return data



def _sanitize_agent_rpc(line: str, actor: Mapping[str, Any]) -> str:
    """Drop forged server fields and inject the authenticated account actor."""
    try:
        msg = json.loads(line)
    except ValueError:
        return line
    if not isinstance(msg, dict):
        return line
    method = str(msg.get("method") or "")
    if not (method.startswith("agent_") or method in _AGENT_TURN_PROVIDER_METHODS):
        return line
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
        msg["params"] = params
    # Only inference turns accept provider selection from the browser. Other
    # agent RPCs (notably list_models) may already carry gateway-injected
    # credentials and must retain them.
    params.pop("_chudware_actor", None)
    if method in _AGENT_TURN_PROVIDER_METHODS:
        for key in _STRIP_SECRET_FIELDS:
            params.pop(key, None)
        # Browser credentials were removed above. Only the credential decrypted
        # by this gateway for the authenticated account may enter the private
        # hostd-to-worker run channel.
        if actor.get("_byok_validated"):
            params["api_key"] = str(actor["_byok_api_key"])
    options = params.get("options")
    if not isinstance(options, dict):
        options = {}
        params["options"] = options
    options.pop("_chudware_actor", None)
    if method in _AGENT_TURN_PROVIDER_METHODS:
        for key in _STRIP_SECRET_FIELDS:
            options.pop(key, None)
    pool = actor.get("_pool")
    if method in _AGENT_TURN_PROVIDER_METHODS and isinstance(pool, Mapping):
        params["provider"] = str(pool["provider"])
        if pool.get("model"):
            params["model"] = str(pool["model"])
        options.pop("provider", None)
        options.pop("model", None)
    if method in _AGENT_TURN_PROVIDER_METHODS and actor.get("_claude_provider_override"):
        params["provider"] = "anthropic"
        selected_model = str(params.get("model") or options.get("model") or "").strip()
        params["model"] = (
            selected_model if selected_model.startswith("claude-")
            else default_model_for("anthropic-sub")
        )
        options.pop("provider", None)
        options.pop("model", None)
    params["options"] = options
    injected = {
        "email": str(actor["email"]),
        "plan": str(actor["plan"]),
        "credits_remaining": max(0, int(actor["credits_remaining"])),
        "key_source": str(actor["key_source"]),
    }
    if injected["key_source"] == "byok" and actor.get("_byok_validated"):
        injected["_byok_key_sha256"] = hashlib.sha256(
            str(actor["_byok_api_key"]).encode("utf-8")
        ).hexdigest()
    if injected["key_source"] == "pool" and isinstance(pool, Mapping):
        injected["_pool"] = {
            "provider": str(pool["provider"]),
            "api_key": str(pool["api_key"]),
        }
        injected["_pool_key_sha256"] = hashlib.sha256(
            str(pool["api_key"]).encode("utf-8")
        ).hexdigest()
    rate = actor.get("_rate")
    if isinstance(rate, Mapping):
        injected["_rate"] = {
            "model": str(rate.get("model") or ""),
            "prompt_usd_per_mtok": str(rate.get("prompt_usd_per_mtok") or "0"),
            "completion_usd_per_mtok": str(
                rate.get("completion_usd_per_mtok") or "0"
            ),
        }
    if actor.get("_reservation_credits") is not None:
        injected["_reservation_credits"] = max(
            0, int(actor.get("_reservation_credits") or 0)
        )
    grant = actor.get("_subscription")
    if isinstance(grant, Mapping):
        # Rides inside the actor rather than as a sibling param on purpose. The
        # actor is the one object hostd forwards to the worker verbatim
        # (core/agent/run.cpp:825), so the grant and the key_source decision it
        # belongs to cannot be separated in transit, and no hostd change is
        # needed to carry it.
        injected["_subscription"] = {
            "provider": str(grant["provider"]),
            "access_token": str(grant["access_token"]),
        }
        injected["_subscription_token_sha256"] = hashlib.sha256(
            str(grant["access_token"]).encode("utf-8")
        ).hexdigest()
    options["_chudware_actor"] = injected
    return json.dumps(msg, separators=(",", ":"))


@router.websocket("/engine")
async def engine_ws(ws: WebSocket) -> None:
    user = ws.state.user  # AuthMiddleware closed 4401/4403 already if unauthenticated
    account = get_account(user.email)
    try:
        cloud_allowed = bool(account and limits_for(account.plan).cloud)
    except ValueError:
        cloud_allowed = False
    try:
        selected_team = orgs.selected_workspace_team(ws, user.email)
        if selected_team:
            orgs.resolve_team_bucket(user.email, selected_team, write=True)
    except HTTPException:
        await ws.close(code=4403, reason="workspace unavailable")
        return
    university_access = (orgs.university_eligible(user.email)
                         and selected_team == settings.university_team_id)
    expired_trial = bool(account and account.plan == "trial" and authn.trial_expired(user.email))
    if (not cloud_allowed or expired_trial) and not university_access:
        log.info(
            "engine bridge refused for %s: plan %s has no cloud access",
            user.email,
            getattr(account, "plan", ""),
        )
        await ws.close(code=4403, reason="plan_no_cloud")
        return
    # AUDIT (multi-tenant): this bridge attaches the caller to the ONE shared
    # webd daemon. /app is the browser-native WASM client and is now the only
    # delivery mode, so there is no per-caller sandbox to hang authorization on.
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

        # Per-account roots are the only general-purpose path capabilities.
        # Scratch handoffs are admitted separately by exact document/revision
        # naming and a bridge-granted document capability.
        slug = user_slug(user.email)
        personal_roots = [
            PurePosixPath(os.path.normpath(str(settings.user_workspace_root(slug)))),
            PurePosixPath(os.path.normpath(str(settings.user_bucket_root(slug)))),
        ]
        allowed_roots = personal_roots
        active_actor: dict[str, Any] | None = None
        active_reservation: dict[str, Any] | None = None
        document_capabilities: set[str] = set()
        pending_requests: dict[Any, tuple[str, set[str]]] = {}

        async def ws_to_daemon() -> None:
            nonlocal active_actor, active_reservation, allowed_roots
            max_frame = settings.ws_max_message_bytes
            while True:
                line = await ws.receive_text()
                try:
                    current_user = revalidate_websocket_user(ws.scope, require_cloud=True)
                except AuthError as exc:
                    await ws.close(code=4401 if exc.status == 401 else 4403)
                    return
                try:
                    team_bucket = orgs.resolve_team_bucket(
                        current_user.email, selected_team, write=True
                    ) if selected_team else None
                except HTTPException:
                    await ws.close(code=4403, reason="workspace no longer available")
                    return
                workspace_root = (team_bucket.root if team_bucket else
                                  settings.user_workspace_root(slug))
                allowed_roots = personal_roots + (
                    [PurePosixPath(str(workspace_root))] if team_bucket else []
                )
                current_account = get_account(current_user.email)
                expired = bool(current_account and current_account.plan == "trial"
                               and authn.trial_expired(current_user.email))
                if expired and selected_team != settings.university_team_id:
                    await ws.close(code=4403, reason="trial expired")
                    return
                if len(line.encode("utf-8")) > max_frame:
                    log.warning(
                        "engine bridge %s: frame exceeds %d bytes; closing",
                        user.email,
                        max_frame,
                    )
                    await ws.close(code=1009, reason="frame too large")
                    return

                method = _rpc_method(line)
                request_doc_ids = _request_doc_ids(line)
                forbidden = request_doc_ids - document_capabilities
                if forbidden:
                    log.warning(
                        "engine bridge %s: rejected unauthorized document IDs",
                        user.email,
                    )
                    await ws.send_text(_rpc_error(
                        line, -32004, "document not authorized for this connection"
                    ))
                    continue
                if method in {"agent_admin_turn", "doc_file_operations"}:
                    await ws.send_text(_rpc_error(
                        line, -32601, "method unavailable on web bridge"
                    ))
                    continue
                if method.startswith("agent_provider_") and current_user.role != "admin":
                    log.warning(
                        "engine bridge %s: rejected administrator provider RPC",
                        user.email,
                    )
                    await ws.send_text(_rpc_error(
                        line, -32003, "administrator authorization required"
                    ))
                    continue
                if (
                    method in _OPERATOR_SUBSCRIPTION_METHODS
                    and current_user.role != "admin"
                ):
                    log.warning(
                        "engine bridge %s: rejected operator subscription RPC",
                        user.email,
                    )
                    await ws.send_text(_rpc_error(
                        line, -32003, "administrator authorization required"
                    ))
                    continue
                if method in _AGENT_TURN_PROVIDER_METHODS:
                    if expired:
                        await ws.send_text(_rpc_error(line, -32003, "trial expired; university access covers workspace only"))
                        continue
                    message = json.loads(line)
                    params = message.setdefault("params", {})
                    if not isinstance(params, dict):
                        await ws.send_text(_rpc_error(line, -32602, "params must be an object"))
                        continue
                    options = params.get("options")
                    if not isinstance(options, dict):
                        options = {}
                        params["options"] = options
                    options["workspace_dir"] = str(workspace_root)
                    line = json.dumps(message, separators=(",", ":"))
                offender = path_violation(line, allowed_roots, document_capabilities)
                if offender is not None:
                    log.warning(
                        "engine bridge %s: rejected out-of-jail path %r",
                        user.email,
                        offender,
                    )
                    await ws.send_text(_rpc_error(
                        line, -32001, "path outside your workspace"
                    ))
                    continue

                if method == "hello":
                    line = _inject_identity(line, user.email, user.display)
                elif method == _LIST_MODELS_METHOD:
                    line, error = _inject_list_models_creds(line)
                    if error is not None:
                        log.warning(
                            "engine bridge %s: rejected non-canonical model endpoint",
                            user.email,
                        )
                        await ws.send_text(_rpc_error(line, -32002, error))
                        continue

                if method.startswith("agent_") or method in _AGENT_TURN_PROVIDER_METHODS:
                    if method in _AGENT_TURN_PROVIDER_METHODS:
                        actor = await asyncio.to_thread(
                            _actor_for_turn, user.email, line
                        )
                    else:
                        actor = await asyncio.to_thread(actor_for, user.email)
                    if (
                        method in _AGENT_TURN_PROVIDER_METHODS
                        and actor.get("key_source") != "pool"
                        and not actor.get("_byok_validated")
                        and not actor.get("_subscription")
                        and not settings.share_provider_keys
                    ):
                        # A shared hostd's ambient config belongs to the operator.
                        # Only a server-verified account credential may take the
                        # unmetered BYOK path when host-key sharing is disabled.
                        await ws.send_text(_rpc_result(
                            line,
                            {"ok": False, "reason": "provider_credentials_required"},
                        ))
                        continue
                    if (
                        actor.get("key_source") == "pool"
                        and method in _AGENT_TURN_PROVIDER_METHODS
                        and not actor.get("_pool", {}).get("api_key")
                    ):
                        await ws.send_text(_rpc_result(line, {
                            "ok": False,
                            "reason": "pool_credential_unconfigured",
                            "hint": "The managed AI pool is not configured. Contact the site operator or connect your own provider.",
                        }))
                        continue
                    if (
                        actor.get("key_source") == "pool"
                        and method in _AGENT_TURN_PROVIDER_METHODS
                    ):
                        if int(actor.get("credits_remaining") or 0) <= 0:
                            # Absolute stop: a zero-balance pool actor never
                            # reaches provider dispatch, even if a missing or
                            # temporarily zero rate would estimate this turn at 0.
                            await ws.send_text(_rpc_result(
                                line,
                                {"ok": False, "reason": "insufficient_credits"},
                            ))
                            continue
                        model, prompt_estimate, max_output = _agent_cost_inputs(line)
                        if actor.get("_pool", {}).get("model"):
                            model = str(actor["_pool"]["model"])
                        reservation = await asyncio.to_thread(
                            reserve_turn,
                            actor,
                            model=model,
                            prompt_tokens_estimate=prompt_estimate,
                            max_output_tokens=max_output,
                        )
                        if not reservation.get("ok"):
                            await ws.send_text(_rpc_result(
                                line,
                                {"ok": False, "reason": "insufficient_credits"},
                            ))
                            continue
                        actor["credits_remaining"] = int(
                            reservation["credits_remaining"]
                        )
                        actor["_reservation_credits"] = int(
                            reservation["estimated_credits"]
                        )
                        actor["_rate"] = {
                            "model": model,
                            "prompt_usd_per_mtok": str(
                                reservation["prompt_usd_per_mtok"]
                            ),
                            "completion_usd_per_mtok": str(
                                reservation["completion_usd_per_mtok"]
                            ),
                        }
                        try:
                            request_id = json.loads(line).get("id")
                        except (ValueError, AttributeError):
                            request_id = None
                        active_reservation = {
                            "id": reservation["reservation_id"],
                            "request_id": request_id,
                            "actual_credits": 0,
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "reported": False,
                        }
                    if method in _AGENT_TURN_PROVIDER_METHODS:
                        active_actor = actor
                    line = _sanitize_agent_rpc(line, actor)
                try:
                    request_message = json.loads(line)
                    request_id = request_message.get("id") if isinstance(request_message, dict) else None
                except ValueError:
                    request_message = None
                    request_id = None
                if (
                    method in _AGENT_TURN_PROVIDER_METHODS
                    and active_actor is not None
                    and active_actor.get("_sponsor")
                    and isinstance(request_message, dict)
                ):
                    request_params = request_message.get("params")
                    model = request_params.get("model", "") if isinstance(request_params, dict) else ""
                    log.info(
                        "sponsored_turn email=%s sponsor=%s scope=%s model=%s",
                        user.email, active_actor["_sponsor"]["email"],
                        active_actor["_sponsor"]["scope"], model,
                    )
                if request_id is not None:
                    pending_requests[request_id] = (method, request_doc_ids)
                writer.write(line.encode("utf-8") + b"\n")
                await writer.drain()

        async def daemon_to_ws() -> None:
            nonlocal active_actor, active_reservation
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                authorized = _authorize_daemon_response(
                    line, pending_requests, document_capabilities, allowed_roots,
                    user.email,
                )
                if authorized is None:
                    log.warning(
                        "engine bridge %s: dropped response with unprovable document ownership",
                        user.email,
                    )
                    continue
                line = authorized
                usage = _provider_usage_payload(line)
                if usage is not None and active_actor is not None and active_actor.get("_sponsor"):
                    log.info(
                        "sponsored_usage email=%s sponsor=%s model=%s prompt_tokens=%s completion_tokens=%s",
                        user.email, active_actor["_sponsor"]["email"],
                        str(usage.get("model") or ""),
                        usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    )
                if (
                    usage is not None
                    and active_actor is not None
                    and active_actor.get("key_source") == "pool"
                    and active_reservation is not None
                ):
                    source = str(usage.get("source") or "")
                    if "unreported" not in source:
                        try:
                            actual = await asyncio.to_thread(
                                usage_cost,
                                str(usage.get("model") or ""),
                                int(usage.get("prompt_tokens") or 0),
                                int(usage.get("completion_tokens") or 0),
                            )
                            active_reservation["actual_credits"] += actual
                            active_reservation["prompt_tokens"] += int(
                                usage.get("prompt_tokens") or 0
                            )
                            active_reservation["completion_tokens"] += int(
                                usage.get("completion_tokens") or 0
                            )
                            active_reservation["reported"] = True
                        except Exception:
                            log.exception(
                                "usage pricing failed; reservation stands for %s",
                                user.email,
                            )
                try:
                    terminal = json.loads(line)
                except ValueError:
                    terminal = None
                if (
                    isinstance(terminal, dict)
                    and active_reservation is not None
                    and terminal.get("id") == active_reservation["request_id"]
                    and ("result" in terminal or "error" in terminal)
                ):
                    if active_reservation["reported"]:
                        try:
                            await asyncio.to_thread(
                                reconcile_reservation,
                                active_reservation["id"],
                                actual_credits=active_reservation["actual_credits"],
                                prompt_tokens=active_reservation["prompt_tokens"],
                                completion_tokens=active_reservation["completion_tokens"],
                            )
                        except Exception:
                            log.exception(
                                "reservation reconciliation failed for %s",
                                user.email,
                            )
                    active_reservation = None
                    active_actor = None
                await ws.send_text(line)

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
