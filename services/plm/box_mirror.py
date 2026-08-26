"""Box.com mirror — optional cloud copy of a project's saved files.

Live edits stay entirely local (documents, autosaves, the PLM vault). When
enabled, a successful project save/checkpoint enqueues the project into a
durable on-disk spool, and a drain pass mirrors the project's visible files up
to a Box folder via the public ``api.box.com`` REST surface. Box is a MIRROR,
never a source of truth: nothing here reads content back from Box, and a save
never blocks on the network — the save-path hooks write one tiny local JSON
record and hand the uploads to a background drain. The spool survives process
exits (the agent worker is a fresh fork per turn), so any later drain — the
long-lived GUI worker, the next agent turn's terminal sweep, or a manual
``box_sync`` — picks up whatever an exiting process left behind.

Auth (config.json ``box.auth``):
  * ``method = "developer_token"`` — paste-in token from the Box developer
    console (60-minute lifetime; bring-up/testing only).
  * ``method = "oauth"`` — authorization-code flow against a Box Platform app
    (client_id/client_secret, loopback redirect). Access tokens refresh on
    demand; Box ROTATES the refresh token on every refresh, so the new pair is
    persisted immediately under the shared config lock and re-checked inside
    the lock (a reused rotated-out refresh token kills the whole grant).

Config shape — the GUI owns the marked fields and must nested-overlay the
``box`` object (copy-then-set, exactly like the ``keys``/``experimental``
handling in app_state.cpp), NEVER replace it wholesale, because Python owns the
rotating token fields:

  "box": {
    "enabled": false,            // GUI-owned
    "folder_id": "0",            // GUI-owned — Box parent folder for projects
    "max_file_mb": 512,          // GUI-owned — per-file mirror cap
    "auth": {
      "method": "developer_token",   // GUI-owned: developer_token | oauth
      "developer_token": "",         // GUI-owned
      "client_id": "",               // GUI-owned
      "client_secret": "",           // GUI-owned
      "access_token": "",            // python-owned (rotated)
      "refresh_token": "",           // python-owned (rotated)
      "access_token_expires_at": 0   // python-owned (epoch seconds)
    }
  }

Mirror layout: ``<folder_id>/<project-name>/<project-relative path>``. Dot
files/directories (``.aicadvault`` above all) are never mirrored. Per-project
mirror state (Box file/folder ids + content sha1s) lives under the appdata
root at ``box_mirror/state-<digest>.json`` so unchanged files cost nothing.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from services import paths

_API = "https://api.box.com/2.0"
_UPLOAD = "https://upload.box.com/api/2.0"
_TOKEN_URL = "https://api.box.com/oauth2/token"
_AUTHORIZE_URL = "https://account.box.com/api/oauth2/authorize"

# The loopback redirect a Box Platform app must whitelist for the connect flow.
OAUTH_REDIRECT_PORT = 53682
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_REDIRECT_PORT}/box"

_DEFAULT_MAX_FILE_MB = 512
_HTTP_TIMEOUT_S = 30.0
_UPLOAD_TIMEOUT_S = 180.0
# Refresh when the access token has less than this many seconds left — covers
# clock skew plus the time a long multi-file drain spends between requests.
_TOKEN_SLACK_S = 120.0


class BoxError(Exception):
    """A Box mirror failure with a machine ``reason`` and a plain-prose ``hint``."""

    def __init__(self, reason: str, hint: str = "") -> None:
        super().__init__(f"{reason}: {hint}" if hint else reason)
        self.reason = reason
        self.hint = hint


# --------------------------------------------------------------------------- config

def get_box_config(cfg: dict | None = None) -> dict:
    """Typed, defaulted view of config.json's optional ``box`` block."""
    from services.agent.config import load_config

    if cfg is None:
        cfg = load_config()
    raw = cfg.get("box") if isinstance(cfg.get("box"), dict) else {}
    auth_raw = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}

    def _s(d: dict, key: str) -> str:
        v = d.get(key)
        return v.strip() if isinstance(v, str) else ""

    method = _s(auth_raw, "method") or "developer_token"
    if method not in ("developer_token", "oauth"):
        method = "developer_token"
    try:
        max_mb = int(raw.get("max_file_mb") or _DEFAULT_MAX_FILE_MB)
    except (TypeError, ValueError):
        max_mb = _DEFAULT_MAX_FILE_MB
    max_mb = min(max(1, max_mb), 4096)
    try:
        expires = float(auth_raw.get("access_token_expires_at") or 0)
    except (TypeError, ValueError):
        expires = 0.0
    return {
        "enabled": bool(raw.get("enabled")),
        "folder_id": _s(raw, "folder_id") or "0",
        "max_file_mb": max_mb,
        "auth": {
            "method": method,
            "developer_token": _s(auth_raw, "developer_token"),
            "client_id": _s(auth_raw, "client_id"),
            "client_secret": _s(auth_raw, "client_secret"),
            "access_token": _s(auth_raw, "access_token"),
            "refresh_token": _s(auth_raw, "refresh_token"),
            "access_token_expires_at": expires,
        },
    }


def is_enabled(cfg: dict | None = None) -> bool:
    return get_box_config(cfg)["enabled"]


# --------------------------------------------------------------------------- HTTP

def _http(method: str, url: str, *, token: str | None = None,
          json_body: dict | None = None, form: dict | None = None,
          data: bytes | None = None, content_type: str | None = None,
          timeout: float = _HTTP_TIMEOUT_S) -> tuple[int, Any]:
    """One bounded HTTPS request. Returns ``(status, parsed-json-or-text)``.

    HTTP error statuses are returned, not raised, so callers can read Box's
    structured error envelopes (409 conflicts carry the existing item's id).
    Transport failures raise :class:`BoxError` ``network_error``.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: bytes | None = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = data
        if content_type:
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https hosts
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise BoxError("network_error", f"{type(e).__name__}: {e}") from e
    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 — non-JSON error page
            parsed = raw.decode("utf-8", "replace")[:500]
    return status, parsed


def _multipart(attributes: dict, filename: str, payload: bytes) -> tuple[bytes, str]:
    """Box upload multipart body: an ``attributes`` JSON field then the file."""
    boundary = "----chudware-" + uuid.uuid4().hex
    safe_name = filename.replace('"', "_")
    parts = [
        (f"--{boundary}\r\n"
         'Content-Disposition: form-data; name="attributes"\r\n\r\n'
         f"{json.dumps(attributes)}\r\n").encode("utf-8"),
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"),
        payload,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _conflict_id(body: Any) -> str:
    """The existing item's id from a Box 409 ``item_name_in_use`` envelope.

    Folder conflicts carry ``context_info.conflicts`` as a LIST, file-upload
    conflicts as a single OBJECT — accept both.
    """
    try:
        c = body["context_info"]["conflicts"]
        if isinstance(c, list):
            c = c[0]
        return str(c.get("id") or "")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- auth

_token_lock = threading.Lock()


def _persist_tokens(body: dict) -> None:
    from services.agent.config import save_config

    save_config({"box": {"auth": {
        "method": "oauth",
        "access_token": str(body.get("access_token") or ""),
        "refresh_token": str(body.get("refresh_token") or ""),
        "access_token_expires_at": time.time() + float(body.get("expires_in") or 3600),
    }}})


def _access_token(box: dict | None = None) -> str:
    """A usable bearer token; refreshes (and persists) OAuth tokens on demand."""
    box = box or get_box_config()
    auth = box["auth"]
    if auth["method"] == "developer_token":
        if not auth["developer_token"]:
            raise BoxError("not_configured",
                           "paste a Box developer token (Settings → Integrations → Box) "
                           "or switch to a connected OAuth app")
        return auth["developer_token"]
    if auth["access_token"] and auth["access_token_expires_at"] > time.time() + _TOKEN_SLACK_S:
        return auth["access_token"]
    if not (auth["client_id"] and auth["client_secret"]):
        raise BoxError("not_configured",
                       "set the Box app client_id/client_secret, then connect the account")
    from services.agent.config import config_lock, read_config_uncached

    with _token_lock, config_lock():
        # Another thread/process may have refreshed while we waited. Box refresh
        # tokens are SINGLE-USE: replaying a rotated-out one revokes the grant,
        # so re-read the live config inside the lock before spending ours.
        cur = get_box_config(read_config_uncached())["auth"]
        if cur["access_token"] and cur["access_token_expires_at"] > time.time() + _TOKEN_SLACK_S:
            return cur["access_token"]
        refresh = cur["refresh_token"] or auth["refresh_token"]
        if not refresh:
            raise BoxError("not_connected",
                           "no Box account is connected yet — run the Box connect flow")
        status, body = _http("POST", _TOKEN_URL, form={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": auth["client_id"],
            "client_secret": auth["client_secret"],
        })
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise BoxError("auth_failed", f"Box token refresh failed (HTTP {status}): {body}")
        _persist_tokens(body)
        return str(body["access_token"])


def oauth_authorize_url(state: str) -> str:
    auth = get_box_config()["auth"]
    if not auth["client_id"]:
        raise BoxError("not_configured",
                       "create a Box Platform app and set box.auth.client_id first")
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": auth["client_id"],
        "redirect_uri": OAUTH_REDIRECT_URI,
        "state": state,
    })
    return f"{_AUTHORIZE_URL}?{q}"


def _exchange_code(code: str) -> None:
    auth = get_box_config()["auth"]
    status, body = _http("POST", _TOKEN_URL, form={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": auth["client_id"],
        "client_secret": auth["client_secret"],
        "redirect_uri": OAUTH_REDIRECT_URI,
    })
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise BoxError("auth_failed", f"Box code exchange failed (HTTP {status}): {body}")
    _persist_tokens(body)


def oauth_begin(timeout_s: float = 300.0) -> dict:
    """Start the loopback authorization-code flow.

    Returns ``{ok, authorize_url, redirect_uri}`` immediately; a daemon thread
    listens on the loopback redirect, exchanges the code, and persists the
    token pair. The caller (GUI/tool) opens ``authorize_url`` in a browser and
    then polls :func:`status` with ``live=True`` until ``connected``.
    """
    auth = get_box_config()["auth"]
    if not (auth["client_id"] and auth["client_secret"]):
        return {"ok": False, "reason": "not_configured",
                "hint": "create a Box Platform app (custom app, OAuth 2.0), whitelist "
                        f"redirect {OAUTH_REDIRECT_URI}, then set box.auth.client_id "
                        "and box.auth.client_secret"}
    state = secrets.token_urlsafe(16)
    try:
        url = oauth_authorize_url(state)
    except BoxError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict[str, str] = {}
    expected_path = urllib.parse.urlparse(OAUTH_REDIRECT_URI).path

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            ok = (u.path == expected_path
                  and qs.get("state", [""])[0] == state
                  and bool(qs.get("code", [""])[0]))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h3>Chudware: Box account connected &mdash; you can "
                b"close this tab.</h3></body></html>" if ok else
                b"<html><body><h3>Chudware: Box authorization failed.</h3></body></html>")
            if ok:
                result["code"] = qs["code"][0]

        def log_message(self, *args: Any) -> None:  # noqa: D102 — silence stderr
            pass

    try:
        srv = HTTPServer(("127.0.0.1", OAUTH_REDIRECT_PORT), _Handler)
    except OSError as e:
        return {"ok": False, "reason": "port_busy",
                "hint": f"cannot listen on {OAUTH_REDIRECT_URI}: {e}"}
    srv.timeout = 1.0

    def _wait() -> None:
        deadline = time.time() + timeout_s
        try:
            while time.time() < deadline and "code" not in result:
                srv.handle_request()
        finally:
            with contextlib.suppress(Exception):
                srv.server_close()
        code = result.get("code")
        if code:
            with contextlib.suppress(Exception):
                _exchange_code(code)

    threading.Thread(target=_wait, name="box-oauth-wait", daemon=True).start()
    return {"ok": True, "authorize_url": url, "redirect_uri": OAUTH_REDIRECT_URI,
            "hint": "open authorize_url in a browser and approve access; the token "
                    "is stored automatically once Box redirects back"}


# --------------------------------------------------------------------------- spool + state

def _mirror_root() -> Path:
    return paths.ROOT / "box_mirror"


def _spool_dir() -> Path:
    d = _mirror_root() / "spool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _digest(project: str) -> str:
    return hashlib.sha1(os.path.realpath(project).encode("utf-8")).hexdigest()[:16]


def _state_path(project: str) -> Path:
    return _mirror_root() / f"state-{_digest(project)}.json"


def _load_state(project: str) -> dict:
    try:
        st = json.loads(_state_path(project).read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("folders", {})
            st.setdefault("files", {})
            return st
    except Exception:  # noqa: BLE001 — missing/corrupt state starts fresh
        pass
    return {"project": os.path.realpath(project), "folders": {}, "files": {}}


def _save_state(project: str, state: dict) -> None:
    p = _state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def _note_error(project: str, msg: str) -> None:
    with contextlib.suppress(Exception):
        st = _load_state(project)
        st["last_error"] = msg
        _save_state(project, st)


def enqueue_project(project: str, *, note: str = "") -> str | None:
    """Durably record that *project* needs mirroring. No network; never raises.

    One spool file per project (name keyed on the real path digest), so bursts
    of saves coalesce into a single pending entry.
    """
    try:
        p = Path(project)
        if not p.is_dir():
            return None
        d = _spool_dir()
        tmp = d / f".tmp-{uuid.uuid4().hex}"
        tmp.write_text(json.dumps({"project": str(p.resolve()),
                                   "queued_at": time.time(),
                                   "note": note}), encoding="utf-8")
        dest = d / f"{_digest(str(p))}.json"
        os.replace(tmp, dest)
        return str(dest)
    except Exception:  # noqa: BLE001 — a mirror must never fail a save
        return None


# --------------------------------------------------------------------------- mirroring

def _sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_project_files(root: Path):
    """Yield ``(relpath, path, size)`` for every visible file under *root*.

    Skips dot files/dirs at any depth — the PLM vault (``.aicadvault``) and
    editor droppings are local-only.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            yield p.relative_to(root).as_posix(), p, size


def _ensure_folder(token: str, state: dict, parent_id: str, key: str, name: str) -> str:
    cached = state["folders"].get(key)
    if cached:
        return str(cached)
    status, body = _http("POST", f"{_API}/folders", token=token,
                         json_body={"name": name, "parent": {"id": str(parent_id)}})
    if status == 201 and isinstance(body, dict) and body.get("id"):
        fid = str(body["id"])
    elif status == 409:
        fid = _conflict_id(body)
        if not fid:
            raise BoxError("folder_conflict",
                           f"Box folder {name!r} exists but its id was not returned: {body}")
    elif status == 401:
        raise BoxError("auth_failed",
                       "Box rejected the token (expired developer token? revoked grant?)")
    else:
        raise BoxError("folder_create_failed", f"{name!r}: HTTP {status}: {body}")
    state["folders"][key] = fid
    return fid


def _raise_upload(status: int, body: Any, name: str) -> None:
    if status == 401:
        raise BoxError("auth_failed",
                       "Box rejected the token (expired developer token? revoked grant?)")
    if status == 413:
        raise BoxError("too_large", f"{name!r} exceeds the Box account's file-size limit")
    raise BoxError("upload_failed", f"{name!r}: HTTP {status}: {body}")


def _upload(token: str, file_id: str | None, parent_id: str, name: str,
            path: Path) -> tuple[str, bool]:
    """Upload *path*; returns ``(box_file_id, existed_before)``."""
    payload = path.read_bytes()
    if file_id:
        data, ctype = _multipart({"name": name}, name, payload)
        status, body = _http("POST", f"{_UPLOAD}/files/{file_id}/content", token=token,
                             data=data, content_type=ctype, timeout=_UPLOAD_TIMEOUT_S)
        if status in (200, 201):
            return str(file_id), True
        if status != 404:  # 404 → deleted on Box; fall through to a fresh upload
            _raise_upload(status, body, name)
    data, ctype = _multipart({"name": name, "parent": {"id": str(parent_id)}}, name, payload)
    status, body = _http("POST", f"{_UPLOAD}/files/content", token=token,
                         data=data, content_type=ctype, timeout=_UPLOAD_TIMEOUT_S)
    if status in (200, 201):
        try:
            return str(body["entries"][0]["id"]), False
        except Exception as e:  # noqa: BLE001
            raise BoxError("upload_failed", f"unexpected upload response: {body}") from e
    if status == 409:
        conflict = _conflict_id(body)
        if conflict:  # someone else created it: upload as a new version instead
            data, ctype = _multipart({"name": name}, name, payload)
            status2, body2 = _http("POST", f"{_UPLOAD}/files/{conflict}/content",
                                   token=token, data=data, content_type=ctype,
                                   timeout=_UPLOAD_TIMEOUT_S)
            if status2 in (200, 201):
                return conflict, True
            _raise_upload(status2, body2, name)
    _raise_upload(status, body, name)
    raise AssertionError("unreachable")


def _mirror_project(project: str, box: dict, deadline: float) -> dict:
    root = Path(project)
    if not root.is_dir():
        raise BoxError("missing_project", project)
    token = _access_token(box)
    state = _load_state(project)
    if state.get("parent_folder_id") != box["folder_id"]:
        # The mirror destination moved: every cached Box id points into the old
        # tree, so start over under the new parent (old copies stay on Box).
        state["folders"] = {}
        state["files"] = {}
        state["parent_folder_id"] = box["folder_id"]
    max_bytes = box["max_file_mb"] * 1024 * 1024
    uploaded = updated = unchanged = skipped_large = 0
    proj_folder = _ensure_folder(token, state, box["folder_id"], "", root.name)
    for rel, p, size in _iter_project_files(root):
        if time.time() > deadline:
            _save_state(project, state)
            raise BoxError("budget_exhausted", f"stopped before {rel}; re-run box_sync")
        if size > max_bytes:
            skipped_large += 1
            continue
        try:
            digest = _sha1_file(p)
        except OSError:
            continue  # deleted/unreadable mid-scan
        rec = state["files"].get(rel) or {}
        if rec.get("sha1") == digest and rec.get("id"):
            unchanged += 1
            continue
        parent_id = proj_folder
        walked = ""
        for part in rel.split("/")[:-1]:
            walked = f"{walked}/{part}" if walked else part
            parent_id = _ensure_folder(token, state, parent_id, walked, part)
        file_id, existed = _upload(token, rec.get("id"), parent_id, p.name, p)
        state["files"][rel] = {"id": file_id, "sha1": digest, "size": size}
        if existed:
            updated += 1
        else:
            uploaded += 1
        _save_state(project, state)  # per-file persist: a crash re-uploads at most one
    state["last_sync"] = time.time()
    state["last_error"] = ""
    _save_state(project, state)
    return {"uploaded": uploaded, "updated": updated, "unchanged": unchanged,
            "skipped_large": skipped_large}


# --------------------------------------------------------------------------- drain

_drain_lock = threading.Lock()


def drain(*, block: bool = False, budget_s: float = 300.0) -> dict:
    """Mirror every spooled project up to Box.

    ``block=False`` starts a named daemon thread and returns immediately —
    correct for the long-lived GUI worker; a fresh-per-turn agent process must
    pass ``block=True`` (the spool survives either way, so a killed drain only
    defers work to the next one).
    """
    if not block:
        threading.Thread(target=lambda: drain(block=True, budget_s=budget_s),
                         name="box-mirror-drain", daemon=True).start()
        return {"ok": True, "started": True}
    if not _drain_lock.acquire(timeout=min(budget_s, 30.0)):
        return {"ok": False, "reason": "busy",
                "hint": "another Box drain is already running; the spool keeps the work"}
    try:
        box = get_box_config()
        if not box["enabled"]:
            return {"ok": True, "skipped": "disabled"}
        deadline = time.time() + budget_s
        out: dict = {"ok": True, "projects": [], "errors": []}
        for ent in sorted(_spool_dir().glob("*.json")):
            if time.time() > deadline:
                out["ok"] = False
                out["errors"].append("budget_exhausted: remaining projects stay spooled")
                break
            try:
                stamp = ent.stat().st_mtime_ns
                project = str(json.loads(ent.read_text(encoding="utf-8")).get("project") or "")
            except Exception:  # noqa: BLE001 — unreadable entry: drop it
                with contextlib.suppress(OSError):
                    ent.unlink()
                continue
            if not project:
                with contextlib.suppress(OSError):
                    ent.unlink()
                continue
            try:
                summary = _mirror_project(project, box, deadline)
                out["projects"].append({"project": project, **summary})
                with contextlib.suppress(OSError):
                    # A save that landed DURING the mirror re-wrote the entry;
                    # only consume it if it is still the one we drained.
                    if ent.stat().st_mtime_ns == stamp:
                        ent.unlink()
            except BoxError as e:
                out["ok"] = False
                out["errors"].append(f"{project}: {e.reason}: {e.hint}")
                _note_error(project, f"{e.reason}: {e.hint}")
            except Exception as e:  # noqa: BLE001 — one project never kills the drain
                out["ok"] = False
                out["errors"].append(f"{project}: {type(e).__name__}: {e}")
                _note_error(project, f"{type(e).__name__}: {e}")
        return out
    finally:
        _drain_lock.release()


def sync_project(project: str, *, budget_s: float = 600.0) -> dict:
    """Enqueue *project* and drain now (manual ``box_sync`` / GUI button)."""
    box = get_box_config()
    if not box["enabled"]:
        return {"ok": False, "reason": "disabled",
                "hint": "enable Box mirroring (Settings → Integrations → Box) first"}
    if not enqueue_project(project, note="manual"):
        return {"ok": False, "reason": "bad_project", "hint": f"not a folder: {project}"}
    return drain(block=True, budget_s=budget_s)


# --------------------------------------------------------------------------- save-path hooks

def on_vault_saved(project: str) -> None:
    """Post-``Vault.save`` hook. Never raises; never blocks on the network."""
    try:
        if not is_enabled():
            return
        if enqueue_project(project, note="vault_save"):
            drain(block=False)
    except Exception:  # noqa: BLE001 — a mirror must never fail a save
        pass


def notify_saved_path(path: str) -> None:
    """A document was saved to *path* — spool its project for mirroring."""
    try:
        if not path or not is_enabled():
            return
        from services.agent.project_context import resolve_project_root

        root = resolve_project_root(os.path.dirname(path))
        if root.is_dir() and enqueue_project(str(root), note="doc_save"):
            drain(block=False)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- status

def status(path: str | None = None, *, live: bool = False) -> dict:
    """Config/spool/mirror status; ``live=True`` also verifies the token."""
    box = get_box_config()
    auth = box["auth"]
    if auth["method"] == "developer_token":
        configured = bool(auth["developer_token"])
    else:
        configured = bool(auth["client_id"] and auth["client_secret"]
                          and (auth["refresh_token"] or auth["access_token"]))
    out: dict = {"ok": True, "enabled": box["enabled"], "method": auth["method"],
                 "configured": configured, "folder_id": box["folder_id"],
                 "pending": len(list(_spool_dir().glob("*.json")))}
    if path:
        st = _load_state(path)
        out["project"] = {"last_sync": st.get("last_sync"),
                          "last_error": st.get("last_error", ""),
                          "mirrored_files": len(st.get("files", {}))}
    if live:
        if not configured:
            out["connected"] = False
        else:
            try:
                token = _access_token(box)
                s, body = _http("GET", f"{_API}/users/me", token=token, timeout=15)
                if s == 200 and isinstance(body, dict):
                    out["connected"] = True
                    out["account"] = {"id": str(body.get("id") or ""),
                                      "name": body.get("name"),
                                      "login": body.get("login")}
                else:
                    out["connected"] = False
                    out["error"] = f"HTTP {s}: {body}"
            except BoxError as e:
                out["connected"] = False
                out["error"] = f"{e.reason}: {e.hint}"
    return out
