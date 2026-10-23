"""services.plm.remote — folder/ssh/https remotes, sync, and advisory locks (contract §6).

Team concurrency v1 = advisory locks + sync, no merging (``aicad.plm/1`` §6).
The vault (``.aicadvault/``) is a plain git repo (dulwich primary), so every
non-``chudware-cloud`` remote kind — ``folder`` (bare repo on a filesystem
path), ``ssh``, and ``https`` — shares one fetch/CAS-push implementation built
on ``dulwich.client.get_transport_and_path``: dulwich already dispatches the
wire protocol (local, git+ssh, smart-http) purely from the URL string, so the
manifest's ``kind`` field is a user-facing label only, never a transport
switch (see ``_fetch_folder``/``_cas_push`` below). Destinations require explicit
local approval via ``add_remote``; fetched manifest edits never authorize a new
destination. URLs are credential-free; auth uses local transport credentials.
Auth failures surface as ``{ok:false, reason:"remote_auth"}``, never a traceback.
``chudware-cloud`` stays reserved/config-only (contract §6.1): every sync/lock
call against it returns ``{ok:false, reason:"reserved_remote"}``.

Public surface (used by MCP / worker_rpc and by chudware-web session bootstrap)::

    seed_remote(project_dir, remote_path)
    clone_from_remote(remote_path, dest_project_dir)
    add_remote(project_dir, name, path, *, kind="folder")
    sync(project_dir, remote="origin")
    plm_lock(project_dir, item_id, remote="origin")
    plm_unlock(project_dir, item_id, remote="origin")

All public functions return the flat ``{ok:…}`` envelope (contract §0 / §7).
"""
from __future__ import annotations

import getpass
import ipaddress
import io
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dulwich import porcelain
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from services.plm import doctypes, generic_snapshot, gitbind, history as hist, manifest, snapshot
from services.plm.vault import Vault, VaultError, _user_author
from services.plm.git_security import SECRET_FIELDS, reject_location_credentials, valid_name, validate_location

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

_DEFAULT_BRANCH = b"refs/heads/master"
_LOCK_REF = b"refs/aicad/locks"
_LOCK_SCHEMA = "aicad.plm.locks/1"
_LOCK_TTL_SEC = 4 * 3600  # advisory soft-expiry (contract §6.2)
_CAS_RETRIES = 6
_NULL = io.BytesIO
_NULL_TEXT = io.StringIO


# ---------------------------------------------------------------------------
# envelope helpers
# ---------------------------------------------------------------------------

def _ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def _err(reason: str, hint: str = "") -> dict[str, Any]:
    return {"ok": False, "reason": reason, "hint": hint}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _vault_dir(project_dir: str | os.PathLike) -> Path:
    return Path(project_dir) / gitbind.VAULT_DIRNAME


def _open_vault_repo(project_dir: str | os.PathLike) -> Repo:
    vdir = _vault_dir(project_dir)
    if not (vdir / "objects").is_dir() and not (vdir / "HEAD").is_file():
        raise VaultError("no_vault", "this project has no version history yet")
    return Repo(str(vdir))


def _hex(sha: bytes | str | None) -> str | None:
    if sha is None:
        return None
    if isinstance(sha, bytes):
        return sha.decode("ascii")
    return str(sha)


def _ensure_branch_and_head(repo: Repo, commit_id: bytes | None = None) -> None:
    """After seed/fetch, force ``refs/heads/master`` + symbolic HEAD.

    Dulwich can leave a bare clone with a symbolic HEAD that does not resolve a
    branch tip; callers MUST set both explicitly.
    """
    if commit_id is None:
        try:
            commit_id = repo.head()
        except KeyError:
            return
    if commit_id is None:
        return
    repo.refs[_DEFAULT_BRANCH] = commit_id
    try:
        repo.refs.set_symbolic_ref(b"HEAD", _DEFAULT_BRANCH)
    except Exception:
        # some dulwich builds want set_symbolic_ref; fall back to direct write
        repo.refs[b"HEAD"] = b"ref: " + _DEFAULT_BRANCH


def _branch_tip(repo: Repo) -> bytes | None:
    """Resolved tip of the primary branch (or HEAD), or None if empty."""
    if _DEFAULT_BRANCH in repo.refs:
        return repo.refs[_DEFAULT_BRANCH]
    try:
        return repo.head()
    except KeyError:
        return None


def _is_ancestor(repo: Repo, anc: bytes, desc: bytes) -> bool:
    """True iff *anc* is reachable walking parents from *desc* (inclusive)."""
    if anc == desc:
        return True
    seen: set[bytes] = set()
    stack = [desc]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == anc:
            return True
        try:
            obj = repo[cur]
        except KeyError:
            continue
        if isinstance(obj, Commit):
            stack.extend(obj.parents or [])
    return False


def _merge_base(repo: Repo, a: bytes, b: bytes) -> bytes | None:
    """Best-effort linear merge base (walk a's parents; first also ancestor of b)."""
    # collect ancestors of b
    b_anc: set[bytes] = set()
    stack = [b]
    while stack:
        cur = stack.pop()
        if cur in b_anc:
            continue
        b_anc.add(cur)
        try:
            obj = repo[cur]
        except KeyError:
            continue
        if isinstance(obj, Commit):
            stack.extend(obj.parents or [])
    # walk a
    stack = [a]
    seen: set[bytes] = set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur in b_anc:
            return cur
        try:
            obj = repo[cur]
        except KeyError:
            continue
        if isinstance(obj, Commit):
            stack.extend(obj.parents or [])
    return None


def _flatten_tree(repo: Repo, tree_id: bytes, prefix: str = "") -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    tree = repo[tree_id]
    for entry in tree.items():
        name = entry.path.decode("utf-8")
        full = f"{prefix}{name}"
        obj = repo[entry.sha]
        if isinstance(obj, Tree):
            out.update(_flatten_tree(repo, entry.sha, prefix=f"{full}/"))
        else:
            out[full] = obj.data
    return out


def _build_tree(repo: Repo, files: dict[str, bytes]) -> Tree:
    obj_store = repo.object_store
    nested: dict[str, Any] = {}
    for path, data in files.items():
        parts = path.split("/")
        node = nested
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = data

    def build(node: dict[str, Any]) -> Tree:
        tree = Tree()
        for name, val in sorted(node.items()):
            if isinstance(val, dict):
                sub = build(val)
                obj_store.add_object(sub)
                tree.add(name.encode("utf-8"), 0o040000, sub.id)
            else:
                blob = Blob.from_string(val)
                obj_store.add_object(blob)
                tree.add(name.encode("utf-8"), 0o100644, blob.id)
        return tree

    root = build(nested)
    obj_store.add_object(root)
    return root


def _commit_files(
    repo: Repo,
    files: dict[str, bytes],
    *,
    parent: bytes | None,
    message: str,
    author: dict[str, Any] | None = None,
    kind: str = hist.KIND_AUTO,
) -> bytes:
    """Write *files* as a whole tree and commit with *parent*. Returns commit id."""
    author = author or _user_author()
    root = _build_tree(repo, files)
    c = Commit()
    c.tree = root.id
    c.parents = [parent] if parent else []
    who = f'{author.get("display") or "User"} <{author.get("id") or "user"}@aicad.local>'
    c.author = c.committer = who.encode("utf-8")
    when = int(time.time())
    c.author_time = c.commit_time = when
    c.author_timezone = c.commit_timezone = 0
    trailer = f"chudware-plm-kind: {kind}\n"
    c.message = (f"{message}\n\n{trailer}").encode("utf-8")
    repo.object_store.add_object(c)
    return c.id


def _materialize_working_tree(project_dir: Path, repo: Repo, commit_id: bytes) -> None:
    """Write working containers from a shadow-tree commit (like Vault.restore)."""
    commit = repo[commit_id]
    files = _flatten_tree(repo, commit.tree)
    m_raw = files.get("manifest.json")
    if not m_raw:
        return
    m = manifest.loads(m_raw)
    # Reject the entire incoming manifest before touching any working file.
    for it in m.get("items", []):
        if it.get("path"):
            dest = generic_snapshot.contained_path(project_dir, it["path"])
            if dest.exists():
                expects_directory = doctypes.storage_form(it.get("doc_type")) == "tree"
                if (expects_directory and not dest.is_dir()) or (not expects_directory and not dest.is_file()):
                    raise VaultError("invalid_mode", f"item destination has incompatible type: {it['path']}")
    for it in m.get("items", []):
        iid = it["item_id"]
        path = it.get("path") or ""
        if not path:
            continue
        # only CAD containers for now
        try:
            exploded = snapshot.read_item_from_tree(files, iid)
        except KeyError:
            continue
        dest = generic_snapshot.contained_path(project_dir, path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(snapshot.reassemble_container(exploded))


def _remote_entry_by_name(m: dict[str, Any], name: str) -> dict[str, Any] | None:
    for r in m.get("remotes", []) or []:
        if r.get("name") == name or r.get("id") == name:
            return r
    return None


def approved_remote_url(project_dir: str | os.PathLike, entry: dict[str, Any]) -> str:
    """Only a local add_remote call can approve a versioned destination."""
    if entry.get("credential_repair_required"):
        raise VaultError(
            "remote_destination_unapproved",
            "This remote contains legacy credentials. Replace it explicitly before sharing; "
            "rotate exposed credentials and handle old history separately.",
        )
    v = Vault.open(project_dir)
    try:
        approvals = v.get_local_config().get("approved_remotes") or {}
    finally:
        v.close()
    name = entry.get("name")
    expected = {"kind": entry.get("kind"), "url": entry.get("url")}
    if not valid_name(name) or approvals.get(name) != expected:
        raise VaultError("remote_destination_unapproved",
                         "This remote destination needs local approval. Configure it explicitly before syncing.")
    try:
        if entry.get("kind") == "plm-server":
            return _normalize_live_url(entry["url"])
        return validate_location(entry["url"])
    except (ValueError, TypeError, KeyError) as exc:
        raise VaultError("invalid_mode", str(exc)) from exc


def _resolve_remote(
    project_dir: str | os.PathLike, remote: str
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Return (manifest, remote_url, remote_entry) or error pieces.

    *remote* may be a remote name (``origin``) or a remote id (``r1``).
    """
    try:
        v = Vault.open(project_dir)
    except VaultError as e:
        return None, None, None  # caller maps
    try:
        m = v.manifest()
    finally:
        v.close()
    entry = _remote_entry_by_name(m, remote)
    if entry is None and remote == "origin":
        # fall back to first folder remote
        for r in m.get("remotes", []) or []:
            if r.get("kind") == "folder":
                entry = r
                break
    return m, approved_remote_url(project_dir, entry) if entry else None, entry


def _ensure_dulwich_remote(repo: Repo, name: str, url: str) -> None:
    """Idempotently register a dulwich remote in the vault config."""
    if not valid_name(name):
        raise VaultError("invalid_mode", "invalid remote name")
    try:
        validate_location(url)
    except ValueError as exc:
        raise VaultError("invalid_mode", str(exc)) from exc
    try:
        porcelain.remote_add(repo, name, url)
    except Exception:
        # already exists or config race — update url in place
        cfg = repo.get_config()
        section = (b"remote", name.encode("utf-8") if isinstance(name, str) else name)
        try:
            cfg.set(section, b"url", url.encode("utf-8"))
            cfg.write_to_path()
        except Exception:
            pass


def _classify_transport_error(e: Exception, remote_url: str) -> VaultError:
    """Map a transport failure to a typed, user-safe reason."""
    from dulwich.client import HTTPProxyUnauthorized, HTTPUnauthorized

    # Keep transport/vendor details on the service side. Locations may contain
    # credentials, so log the exception without echoing the remote URL.
    _LOG.warning(
        "PLM remote transport failure (%s): %s", type(e).__name__, e
    )
    if isinstance(e, (HTTPUnauthorized, HTTPProxyUnauthorized)):
        return VaultError(
            "remote_auth",
            "Authentication for the shared project failed. Sign in and try again.",
        )
    msg = str(e).lower()
    if any(
        hint in msg
        for hint in (
            "permission denied",
            "publickey",
            "authentication failed",
            "auth fail",
            "401 unauthorized",
            "403 forbidden",
            " unauthorized",
        )
    ):
        return VaultError(
            "remote_auth",
            "Authentication for the shared project failed. Sign in and try again.",
        )
    return VaultError(
        "remote_unreachable",
        "The shared project could not be reached. Check its location and connection, then try again.",
    )


def _fetch_folder(repo: Repo, remote_url: str, remote_name: str = "origin") -> dict[bytes, bytes]:
    """Fetch all refs from a remote; return remote ref map {ref: sha}.

    ``remote_url`` may be a folder path, an ``ssh://``/``git@host:path``
    location, or an ``https://`` location — ``porcelain.fetch`` resolves the
    transport via ``dulwich.client.get_transport_and_path`` purely from the
    URL string, so this one implementation already covers contract §6.1's
    ``folder | ssh | https`` remote kinds. (``chudware-cloud`` never reaches
    here; callers gate it before calling in.)

    Also imports tracking refs under ``refs/remotes/<name>/`` and ensures
    ``refs/heads/*`` + HEAD are present after the fetch.
    """
    try:
        validate_location(remote_url)
    except ValueError as exc:
        raise VaultError("invalid_mode", str(exc)) from exc
    _ensure_dulwich_remote(repo, remote_name, remote_url)
    try:
        result = porcelain.fetch(
            repo,
            remote_url,
            errstream=io.BytesIO(),
            outstream=io.StringIO(),
        )
    except Exception as e:
        raise _classify_transport_error(e, remote_url)
    refs: dict[bytes, bytes] = {}
    for ref, sha in (result.refs or {}).items():
        if sha is None:
            continue
        if ref == b"HEAD":
            continue
        refs[ref] = sha
        # import tracking + local branch tips for heads
        if ref.startswith(b"refs/heads/"):
            track = b"refs/remotes/" + remote_name.encode("utf-8") + b"/" + ref[len(b"refs/heads/"):]
            repo.refs[track] = sha
            # do NOT clobber local branch here — sync decides FF vs replay
        elif ref.startswith(b"refs/aicad/"):
            # leave lock ref alone on local unless sync/lock ops set it
            pass
    return refs


def _remote_head_from_refs(refs: dict[bytes, bytes]) -> bytes | None:
    if _DEFAULT_BRANCH in refs:
        return refs[_DEFAULT_BRANCH]
    for ref, sha in refs.items():
        if ref.startswith(b"refs/heads/"):
            return sha
    return None


def _cas_push(
    repo: Repo,
    remote_url: str,
    local_sha: bytes,
    expected_old: bytes | None,
    ref: bytes = _DEFAULT_BRANCH,
) -> bool:
    """Push *local_sha* to *ref* with expected-old CAS. True on accept.

    Uses dulwich ``send_pack`` directly so lock refs (blob tips) work — porcelain
    ``push`` calls ``can_fast_forward`` which asserts Commit objects.

    Atomicity actually delivered (contract §6.3 requires this work
    "identically on dulwich and subprocess-git" — verified below, not assumed):

    * **ssh / https / git** (``TraditionalGitClient`` / smart-HTTP) — real
      server-side CAS. The ``old_refs`` seen by *update_refs* come from the
      server's live ref advertisement on *this* connection, not a value we
      cached earlier; when we detect our ``expected_old`` no longer matches,
      *update_refs* returns a no-op and no update command is even sent. If a
      command IS sent, it still carries the server's own just-advertised old
      value, and receive-pack rejects an update whose old value no longer
      matches current state — compare and swap happen in one server-side
      transaction.
    * **folder** (local path, ``LocalGitClient``) — same guarantee, different
      mechanism: ``target.refs.set_if_equals(ref, old, new)`` is a
      lockfile-guarded compare-and-swap on the on-disk ref, so a concurrent
      local/NFS writer racing us fails exactly like a remote push would.

    Either way the *real* compare happens inside the transport call; our own
    ``expected_old`` snapshot only lets us skip an already-doomed round trip
    (the ``cas_fail`` no-op path below) and lets the retry loops in
    :func:`sync` / :func:`_lock_mutate` tell "definitely raced" apart from
    "server unreachable".
    """
    from dulwich.client import get_transport_and_path

    # mirror onto local ref so generate_pack_data can see it
    if ref.startswith(b"refs/heads/"):
        repo.refs[ref] = local_sha
        _ensure_branch_and_head(repo, local_sha)
    else:
        repo.refs[ref] = local_sha

    try:
        validate_location(remote_url)
        client, path = get_transport_and_path(str(remote_url))
        path_b = path.encode("utf-8") if isinstance(path, str) else path
    except Exception as e:
        raise _classify_transport_error(e, remote_url)

    cas_fail = {"v": False}

    def update_refs(old_refs: dict) -> dict:
        current = old_refs.get(ref)
        if expected_old is None:
            if current is not None:
                cas_fail["v"] = True
                return dict(old_refs)  # no-op → abort update
        else:
            if current != expected_old:
                cas_fail["v"] = True
                return dict(old_refs)
        new_refs = dict(old_refs)
        new_refs[ref] = local_sha
        return new_refs

    def generate_pack_data(have, want, ofs_delta=False, progress=None):
        return repo.generate_pack_data(
            have, want, progress=progress, ofs_delta=ofs_delta
        )

    try:
        client.send_pack(path_b, update_refs, generate_pack_data)
    except Exception as e:
        msg = str(e).lower()
        if "non-fast-forward" in msg or "diverged" in msg or "rejected" in msg:
            return False
        raise _classify_transport_error(e, remote_url)

    if cas_fail["v"]:
        return False
    after = _ls_remote(remote_url).get(ref)
    return after == local_sha


def _ls_remote(remote_url: str) -> dict[bytes, bytes]:
    try:
        validate_location(remote_url)
    except ValueError as exc:
        raise VaultError("invalid_mode", str(exc)) from exc
    try:
        from dulwich.client import get_transport_and_path
        client, path = get_transport_and_path(remote_url)
        result = client.get_refs(path.encode("utf-8") if isinstance(path, str) else path)
        # get_refs may return dict directly or an object
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if v is not None and k != b"HEAD"}
        refs = getattr(result, "refs", result)
        return {k: v for k, v in dict(refs).items() if v is not None and k != b"HEAD"}
    except Exception as e:
        raise _classify_transport_error(e, remote_url)


# ---------------------------------------------------------------------------
# seed / clone
# ---------------------------------------------------------------------------

def seed_remote(project_dir: str | os.PathLike, remote_path: str | os.PathLike) -> dict[str, Any]:
    """Create a central bare folder remote from an existing vault (or empty).

    *remote_path* becomes a bare ``.git`` directory. If *project_dir* already
    has a vault with history, its tip is pushed; otherwise an empty bare repo
    with symbolic HEAD is created. Also registers the remote as ``origin`` on
    the local vault when a project vault exists.
    """
    remote_path = Path(remote_path)
    project_dir = Path(project_dir)
    try:
        if remote_path.exists() and any(remote_path.iterdir()):
            # already seeded? accept if it looks like a bare repo
            if (remote_path / "objects").is_dir() or (remote_path / "HEAD").is_file():
                # ensure HEAD is well-formed
                try:
                    rr = Repo(str(remote_path))
                    tip = _branch_tip(rr)
                    if tip is not None:
                        _ensure_branch_and_head(rr, tip)
                    rr.close()
                except Exception:
                    pass
            else:
                return _err("invalid_project", f"remote path is not empty: {remote_path}")
        else:
            remote_path.mkdir(parents=True, exist_ok=True)
            Repo.init_bare(str(remote_path))

        has_vault = gitbind.GitBinding.exists(project_dir)
        if has_vault:
            local = _open_vault_repo(project_dir)
            try:
                tip = _branch_tip(local)
                if tip is not None:
                    _ensure_branch_and_head(local, tip)
                    # push all heads + any chudware refs
                    refspecs = []
                    for ref in local.refs.keys():
                        if ref.startswith(b"refs/heads/") or ref.startswith(b"refs/aicad/"):
                            refspecs.append(ref)
                    if not refspecs:
                        refspecs = [_DEFAULT_BRANCH]
                    try:
                        porcelain.push(
                            local,
                            str(remote_path),
                            refspecs=refspecs,
                            errstream=io.BytesIO(),
                            outstream=io.BytesIO(),
                        )
                    except Exception:  # noqa: BLE001
                        _LOG.exception("Could not seed PLM remote")
                        return _err(
                            "remote_unreachable",
                            "The shared project could not be created at that location.",
                        )
                rr = Repo(str(remote_path))
                try:
                    rtip = _branch_tip(rr) or tip
                    if rtip is not None:
                        _ensure_branch_and_head(rr, rtip)
                finally:
                    rr.close()
                # register origin on the local vault
                add_remote(str(project_dir), "origin", str(remote_path), kind="folder")
            finally:
                local.close()
        else:
            # empty bare — set symbolic HEAD so later clones have a branch target
            rr = Repo(str(remote_path))
            try:
                try:
                    rr.refs.set_symbolic_ref(b"HEAD", _DEFAULT_BRANCH)
                except Exception:
                    pass
            finally:
                rr.close()

        return _ok(remote_path=str(remote_path), seeded=True)
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception:  # noqa: BLE001
        _LOG.exception("Could not seed PLM remote")
        return _err("internal", "Team sharing could not create the shared project.")


def clone_from_remote(
    remote_path: str | os.PathLike, dest_project_dir: str | os.PathLike
) -> dict[str, Any]:
    """Clone a bare folder remote into a private project directory.

    Creates ``dest_project_dir/.aicadvault`` (bare, vault-style), materializes
    working containers from HEAD, and registers ``origin`` → *remote_path*.
    """
    remote_path = Path(remote_path)
    dest = Path(dest_project_dir)
    try:
        if not remote_path.exists():
            return _err("remote_unreachable", f"shared project not found: {remote_path}")
        dest.mkdir(parents=True, exist_ok=True)
        vdir = dest / gitbind.VAULT_DIRNAME
        if vdir.exists():
            return _err("invalid_project", "destination already has a vault")

        try:
            porcelain.clone(str(remote_path), str(vdir), bare=True)
        except Exception:  # noqa: BLE001
            _LOG.exception("Could not clone PLM remote")
            return _err(
                "remote_unreachable",
                "The shared project could not be copied to this device.",
            )

        repo = Repo(str(vdir))
        try:
            tip = _branch_tip(repo)
            if tip is not None:
                _ensure_branch_and_head(repo, tip)
                _materialize_working_tree(dest, repo, tip)
            _ensure_dulwich_remote(repo, "origin", str(remote_path))
        finally:
            repo.close()

        # ensure origin is in the versioned manifest (forward entry if needed)
        if tip is not None:
            add_remote(str(dest), "origin", str(remote_path), kind="folder")
            # re-materialize is unnecessary; add_remote may cut a manifest commit

        # project_id from manifest if present
        project_id = None
        try:
            v = Vault.open(dest)
            try:
                project_id = v.manifest()["project"]["id"]
            finally:
                v.close()
        except Exception:
            pass

        return _ok(project_dir=str(dest), project_id=project_id, remote_path=str(remote_path))
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception:  # noqa: BLE001
        _LOG.exception("Could not clone PLM remote")
        return _err("internal", "Team sharing could not copy the shared project.")


# ---------------------------------------------------------------------------
# add_remote
# ---------------------------------------------------------------------------

def _remote_identity(
    display_name: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Identity stored with a local remote profile, with an OS-user fallback."""
    author = _user_author()
    user_id = str(author.get("id") or "").strip()
    display = str(display_name or author.get("display") or "").strip()
    if not user_id or user_id == "user":
        user_id = getpass.getuser() or "user"
    if not display or display == "User":
        display = user_id
    resolved_account = account_id
    if resolved_account is None:
        resolved_account = author.get("account_id")
    return {
        "user_id": user_id,
        "display": display,
        "account_id": str(resolved_account).strip() if resolved_account else None,
    }


def _normalize_live_url(value: str) -> str:
    """Return a credential-free ws/wss endpoint for a live remote."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("a PLM server remote needs a WebSocket URL")
    if "://" not in raw:
        authority = raw.split("/", 1)[0].lower()
        local = (
            authority == "localhost"
            or authority.startswith("localhost:")
            or authority.startswith("127.")
            or authority.startswith("[::1]")
        )
        raw = ("ws://" if local else "wss://") + raw
    reject_location_credentials(raw)
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"
    if scheme not in ("ws", "wss"):
        raise ValueError("a PLM server location must use ws:// or wss://")
    host = parsed.hostname
    if not host:
        raise ValueError("a PLM server URL needs a host")
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if scheme == "ws" and not is_loopback:
        raise ValueError("ws:// is only allowed for a local PLM server; use wss://")
    lowered_host = host.lower()
    authority = f"[{lowered_host}]" if ":" in lowered_host else lowered_host
    if parsed.port is not None:
        authority += f":{parsed.port}"
    return urlunsplit((scheme, authority, parsed.path or "", parsed.query, ""))


def add_remote(
    project_dir: str | os.PathLike,
    name: str,
    path: str | os.PathLike | None = None,
    *,
    kind: str = "folder",
    url: str | None = None,
    display_name: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Register a remote profile in the project manifest (contract §6.1 / §7).

    ``chudware-cloud`` remains parseable but reserved. Live-server profiles use
    the frozen WebSocket mode and keep bearer tokens outside the manifest.
    """
    try:
        kind = (kind or "folder").strip()
        if kind not in manifest.REMOTE_KINDS:
            return _err("invalid_mode", f"unknown remote kind: {kind}")
        name = (name or "").strip() or "origin"
        if not valid_name(name):
            return _err("invalid_mode", "remote name must not be an option or invalid ref")
        remote_url = url if url is not None else (str(path) if path is not None else None)

        if kind == "chudware-cloud":
            remote_url = None
        elif kind == "folder":
            if not remote_url:
                return _err("invalid_mode", "a folder remote needs a path")
            try:
                remote_url = validate_location(str(remote_url))
            except ValueError as exc:
                return _err("invalid_mode", str(exc))
        elif kind == "plm-server":
            if not remote_url:
                return _err("invalid_mode", "a PLM server remote needs a WebSocket URL")
            try:
                remote_url = _normalize_live_url(str(remote_url))
            except ValueError as exc:
                return _err("invalid_mode", str(exc))
        else:
            # ssh/https: sync/lock route through the same dulwich transport
            # layer as folder remotes (kind is a UI label; dulwich dispatches
            # the wire protocol purely from the URL string — see _fetch_folder).
            if not remote_url:
                return _err("invalid_mode", "this remote needs a location")
            try:
                remote_url = validate_location(str(remote_url))
            except ValueError as exc:
                return _err("invalid_mode", str(exc))

        v = Vault.open(project_dir)
        try:
            m = v.manifest()
            remotes = list(m.get("remotes") or [])
            # update-in-place if same name
            existing = None
            for r in remotes:
                if r.get("name") == name:
                    existing = r
                    break
            identity = (existing or {}).get("identity")
            if isinstance(identity, dict):
                identity = dict(identity)
                if display_name is not None:
                    identity["display"] = str(display_name).strip()
                if account_id is not None:
                    identity["account_id"] = str(account_id).strip() or None
            else:
                identity = _remote_identity(display_name, account_id)
            entry = {
                "id": (existing or {}).get("id") or ("r" + uuid.uuid4().hex[:8]),
                "kind": kind,
                "url": remote_url,
                "name": name,
                "identity": identity,
                "capabilities": list(
                    manifest.LIVE_REMOTE_CAPABILITIES
                    if kind == "plm-server"
                    else manifest.REMOTE_CAPABILITIES
                ),
            }
            if kind == "chudware-cloud":
                entry["reserved"] = True
            if existing is not None:
                existing.update(entry)
                for secret_key in list(existing):
                    if secret_key.casefold() in SECRET_FIELDS:
                        existing.pop(secret_key, None)
                existing.pop("credential_repair_required", None)
                if kind != "chudware-cloud":
                    existing.pop("reserved", None)
                entry = existing
            else:
                remotes.append(entry)
            m["remotes"] = remotes
            if not m.get("lock_ref"):
                m["lock_ref"] = "refs/aicad/locks"
            v._commit_manifest_change(m, remote_repair=True)
            v.set_local_config({"approved_remotes": {
                name: {"kind": kind, "url": remote_url},
            }})

            if kind in ("folder", "ssh", "https") and remote_url:
                repo = _open_vault_repo(project_dir)
                try:
                    _ensure_dulwich_remote(repo, name, remote_url)
                finally:
                    repo.close()

            return _ok(remote=entry)
        finally:
            v.close()
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception as e:  # noqa: BLE001
        _LOG.exception("Could not register PLM remote")
        return _err("internal", "Team sharing could not save this remote.")


# ---------------------------------------------------------------------------
# sync (fetch + FF / CAS push / diverge → copy-as-variant)
# ---------------------------------------------------------------------------

def sync(project_dir: str | os.PathLike, remote: str = "origin") -> dict[str, Any]:
    """One-button sync (contract §6.3–6.4).

    Returns ``{ok, pulled, pushed, conflicts_copied:[…]}``.
    """
    project_dir = Path(project_dir)
    try:
        v = Vault.open(project_dir)
        try:
            m = v.manifest()
        finally:
            v.close()

        entry = _remote_entry_by_name(m, remote)
        if entry is None and remote == "origin":
            for r in m.get("remotes") or []:
                if r.get("kind") == "folder":
                    entry = r
                    break
        if entry is None:
            return _err("not_found", f"no remote named {remote!r}")
        remote_kind = entry.get("kind")
        if remote_kind == "chudware-cloud":
            return _err("reserved_remote", "Chudware Cloud is not available yet")
        if manifest.remote_mode(entry) == "live":
            return _err(
                "live_join_required",
                "This remote uses live editing; join the item with plm_live_join instead.",
            )
        if remote_kind not in ("folder", "ssh", "https"):
            return _err("invalid_mode", f"unknown remote kind: {remote_kind}")
        remote_url = approved_remote_url(project_dir, entry)
        if not remote_url:
            return _err("remote_unreachable", "this remote has no location")
        remote_name = entry.get("name") or "origin"

        for _attempt in range(_CAS_RETRIES):
            result = _sync_once(project_dir, remote_url, remote_name)
            if result.get("ok") or result.get("reason") != "contended":
                return result
        return _err("contended", "the shared project kept changing; try sync again")
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception:  # noqa: BLE001
        _LOG.exception("Could not sync PLM remote")
        return _err("internal", "The shared project could not be synchronized.")


def _sync_once(project_dir: Path, remote_url: str, remote_name: str) -> dict[str, Any]:
    repo = _open_vault_repo(project_dir)
    try:
        local_head = _branch_tip(repo)
        try:
            remote_refs = _fetch_folder(repo, remote_url, remote_name)
        except VaultError as e:
            return _err(e.reason, e.hint)
        remote_head = _remote_head_from_refs(remote_refs)

        pulled = False
        pushed = False
        conflicts: list[dict[str, Any]] = []

        def refuse_dirty_work() -> dict[str, Any] | None:
            vault = Vault.open(project_dir)
            try:
                dirty = vault._dirty_items(vault.manifest())
            finally:
                vault.close()
            if dirty:
                return _err(
                    "unsaved_changes",
                    "Save or checkpoint local changes before pulling shared changes.",
                )
            return None

        # empty remote, local has history → just push
        if remote_head is None and local_head is not None:
            ok = _cas_push(repo, remote_url, local_head, expected_old=None)
            if not ok:
                return _err("contended", "could not publish history to the shared project")
            return _ok(pulled=False, pushed=True, conflicts_copied=[])

        # empty local, remote has history → FF
        if local_head is None and remote_head is not None:
            refusal = refuse_dirty_work()
            if refusal is not None:
                return refusal
            # ensure objects present (fetch already did)
            _materialize_working_tree(project_dir, repo, remote_head)
            _ensure_branch_and_head(repo, remote_head)
            return _ok(pulled=True, pushed=False, conflicts_copied=[])

        if local_head is None and remote_head is None:
            return _ok(pulled=False, pushed=False, conflicts_copied=[])

        assert local_head is not None and remote_head is not None

        if local_head == remote_head:
            return _ok(pulled=False, pushed=False, conflicts_copied=[])

        if _is_ancestor(repo, local_head, remote_head):
            refusal = refuse_dirty_work()
            if refusal is not None:
                return refusal
            # behind: fast-forward local to remote
            _materialize_working_tree(project_dir, repo, remote_head)
            _ensure_branch_and_head(repo, remote_head)
            pulled = True
            return _ok(pulled=pulled, pushed=False, conflicts_copied=[])

        if _is_ancestor(repo, remote_head, local_head):
            # ahead: CAS push
            ok = _cas_push(repo, remote_url, local_head, expected_old=remote_head)
            if not ok:
                return _err("contended", "someone else published changes; sync again")
            return _ok(pulled=False, pushed=True, conflicts_copied=[])

        # diverged: replay local tip onto remote with copy-as-variant (§6.4)
        refusal = refuse_dirty_work()
        if refusal is not None:
            return refusal
        base = _merge_base(repo, local_head, remote_head)
        new_files, conflicts = _replay_divergent(
            repo, base=base, local_head=local_head, remote_head=remote_head
        )
        author = _user_author()
        new_sha = _commit_files(
            repo,
            new_files,
            parent=remote_head,
            message="Synced shared changes",
            author=author,
            kind=hist.KIND_AUTO,
        )
        _materialize_working_tree(project_dir, repo, new_sha)
        _ensure_branch_and_head(repo, new_sha)
        ok = _cas_push(repo, remote_url, new_sha, expected_old=remote_head)
        if not ok:
            return _err("contended", "someone else published changes during sync; try again")
        pulled = True
        pushed = True
        return _ok(pulled=pulled, pushed=pushed, conflicts_copied=conflicts)
    finally:
        repo.close()


def _replay_divergent(
    repo: Repo,
    *,
    base: bytes | None,
    local_head: bytes,
    remote_head: bytes,
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Build a new shadow tree: remote wins collisions; local preserved as new items.

    Contract §6.4: same ``item_id`` edited on both sides → remote keeps the
    canonical path; local version becomes a new item (``name (Display's edits)``).
    """
    local_tree = _flatten_tree(repo, repo[local_head].tree)
    remote_tree = _flatten_tree(repo, repo[remote_head].tree)
    base_tree: dict[str, bytes] = {}
    if base is not None:
        try:
            base_tree = _flatten_tree(repo, repo[base].tree)
        except Exception:
            base_tree = {}

    result = dict(remote_tree)
    conflicts: list[dict[str, Any]] = []

    def _item_ids(tree: dict[str, bytes]) -> set[str]:
        ids: set[str] = set()
        for p in tree:
            if p.startswith("items/") and p.endswith("/document.json"):
                ids.add(p.split("/")[1])
        return ids

    local_ids = _item_ids(local_tree)
    remote_ids = _item_ids(remote_tree)
    base_ids = _item_ids(base_tree)

    # parse manifests
    def _load_m(tree: dict[str, bytes]) -> dict[str, Any]:
        raw = tree.get("manifest.json")
        if not raw:
            return manifest.new_manifest("Project", _now_iso())
        return manifest.loads(raw)

    local_m = _load_m(local_tree)
    remote_m = _load_m(remote_tree)
    out_m = _load_m(remote_tree)  # start from remote registry

    author = _user_author()
    display = author.get("display") or "User"

    for iid in sorted(local_ids):
        ldoc = local_tree.get(f"items/{iid}/document.json")
        rdoc = remote_tree.get(f"items/{iid}/document.json")
        bdoc = base_tree.get(f"items/{iid}/document.json")
        local_changed = ldoc is not None and ldoc != bdoc
        remote_changed = rdoc is not None and rdoc != bdoc

        if not local_changed:
            continue

        if iid in remote_ids and remote_changed and ldoc != rdoc:
            # collision → copy-as-new-item for LOCAL version
            new_id = manifest.new_item_id()
            local_item = manifest.find_item(local_m, iid) or {
                "item_id": iid, "path": f"{iid}.step+", "kind": "part",
                "doc_uuid": "", "rev": None, "lifecycle": manifest.LIFECYCLE_IN_WORK,
                "released_revs": [], "part_number": "",
            }
            # rename working path
            old_path = local_item.get("path") or f"{iid}.step+"
            stem = Path(old_path).stem
            suffix = Path(old_path).suffix  # ".step+" is multi-suffix; keep name
            # Path.suffix on "Bracket.step+" → "+" — handle carefully
            if old_path.endswith(".step+"):
                base_name = old_path[: -len(".step+")]
                new_path = f"{base_name} ({display}'s edits).step+"
            elif old_path.endswith(".ecad+"):
                base_name = old_path[: -len(".ecad+")]
                new_path = f"{base_name} ({display}'s edits).ecad+"
            else:
                new_path = f"{stem} ({display}'s edits){suffix}"

            # copy all local item blobs under new_id
            prefix = f"items/{iid}/"
            new_prefix = f"items/{new_id}/"
            for path, data in local_tree.items():
                if path.startswith(prefix):
                    tail = path[len(prefix):]
                    new_data = data
                    if tail == "meta.json":
                        try:
                            md = json.loads(data.decode("utf-8"))
                            md["item_id"] = new_id
                            md["author"] = {
                                "kind": author.get("kind", "user"),
                                "id": author.get("id", "user"),
                                "display": display,
                                "account_id": author.get("account_id"),
                            }
                            new_data = json.dumps(md, indent=2, sort_keys=True).encode("utf-8")
                        except Exception:
                            pass
                    result[new_prefix + tail] = new_data

            # register new item in out_m
            new_item = {
                "item_id": new_id,
                "doc_uuid": local_item.get("doc_uuid", ""),
                "kind": local_item.get("kind", "part"),
                "path": new_path,
                "part_number": manifest.next_part_number(out_m),
                "rev": None,
                "lifecycle": manifest.LIFECYCLE_IN_WORK,
                "released_revs": [],
            }
            if local_item.get("doc_type"):
                new_item["doc_type"] = local_item["doc_type"]
            out_m.setdefault("items", []).append(new_item)
            conflicts.append({
                "original_item_id": iid,
                "new_item_id": new_id,
                "path": new_path,
                "name": Path(new_path).name,
            })
        else:
            # no collision: apply local version of this item onto result
            prefix = f"items/{iid}/"
            for path, data in local_tree.items():
                if path.startswith(prefix):
                    result[path] = data
            # ensure registry entry exists (new local-only item)
            if manifest.find_item(out_m, iid) is None:
                li = manifest.find_item(local_m, iid)
                if li is not None:
                    # re-number if part_number collides
                    entry = dict(li)
                    used = {it.get("part_number") for it in out_m.get("items", [])}
                    if entry.get("part_number") in used:
                        entry["part_number"] = manifest.next_part_number(out_m)
                    out_m.setdefault("items", []).append(entry)

    # carry remotes / lock_ref from local if remote missing them
    if not out_m.get("remotes") and local_m.get("remotes"):
        out_m["remotes"] = local_m["remotes"]
    if not out_m.get("lock_ref"):
        out_m["lock_ref"] = local_m.get("lock_ref") or "refs/aicad/locks"

    result["manifest.json"] = manifest.dumps(out_m)
    return result, conflicts


# ---------------------------------------------------------------------------
# locks (CAS on refs/aicad/locks)
# ---------------------------------------------------------------------------

def _empty_lock_manifest() -> dict[str, Any]:
    return {"schema": _LOCK_SCHEMA, "locks": []}


def _read_lock_blob(repo: Repo, sha: bytes | None) -> dict[str, Any]:
    if sha is None:
        return _empty_lock_manifest()
    try:
        obj = repo[sha]
        data = obj.data if hasattr(obj, "data") else bytes(obj)
        doc = json.loads(data.decode("utf-8"))
        if not isinstance(doc, dict):
            return _empty_lock_manifest()
        doc.setdefault("schema", _LOCK_SCHEMA)
        doc.setdefault("locks", [])
        return doc
    except Exception:
        return _empty_lock_manifest()


def _write_lock_blob(repo: Repo, doc: dict[str, Any]) -> bytes:
    raw = json.dumps(doc, indent=2, sort_keys=True).encode("utf-8")
    blob = Blob.from_string(raw)
    repo.object_store.add_object(blob)
    return blob.id


def _lock_holder() -> tuple[str, str, str | None]:
    author = _user_author()
    return (
        str(author.get("id") or "user"),
        str(author.get("display") or "User"),
        str(author["account_id"]) if author.get("account_id") else None,
    )


def _live_lock(entry: dict[str, Any], now: float | None = None) -> bool:
    """True if lock has not soft-expired."""
    exp = entry.get("expires_at")
    if not exp:
        return True
    try:
        # Zulu ISO
        t = time.strptime(exp, "%Y-%m-%dT%H:%M:%SZ")
        return time.mktime(t) > (now or time.time())
    except Exception:
        return True

def _lock_user_id(entry: dict[str, Any]) -> str:
    return str(entry.get("user_id") or entry.get("holder") or "user")


def _public_lock(entry: dict[str, Any]) -> dict[str, Any]:
    """Stable status/override shape, tolerant of pre-account lock blobs."""
    return {
        "item_id": str(entry.get("item_id") or ""),
        "user_id": _lock_user_id(entry),
        "display": str(entry.get("display") or _lock_user_id(entry)),
        "account_id": entry.get("account_id"),
        "acquired_at": str(entry.get("acquired_at") or ""),
        "expires_at": str(entry.get("expires_at") or ""),
    }


def list_locks(
    project_dir: str | os.PathLike,
    remote: str = "origin",
) -> dict[str, Any]:
    """Read the configured remote lock ref without mutating it."""
    project_dir = Path(project_dir)
    try:
        v = Vault.open(project_dir)
        try:
            m = v.manifest()
        finally:
            v.close()
        entry = _remote_entry_by_name(m, remote)
        if entry is None and remote == "origin":
            entry = next(
                (r for r in m.get("remotes", []) or []
                 if r.get("kind") in ("folder", "ssh", "https")),
                None,
            )
        if entry is None:
            return _ok(locks=[])
        kind = entry.get("kind")
        if kind == "chudware-cloud":
            return _err("reserved_remote", "Chudware Cloud is not available yet")
        if manifest.remote_mode(entry) == "live":
            return _err(
                "live_join_required",
                "This remote uses live editing; join the item with plm_live_join instead.",
            )
        if kind not in ("folder", "ssh", "https"):
            return _err("invalid_mode", "This remote type cannot provide shared locks.")
        remote_url = approved_remote_url(project_dir, entry)
        if not remote_url:
            return _err("remote_unreachable", "This remote has no shared location.")
        lock_ref = str(m.get("lock_ref") or "refs/aicad/locks").encode("utf-8")
        repo = _open_vault_repo(project_dir)
        try:
            _fetch_folder(repo, str(remote_url), str(entry.get("name") or "origin"))
            lock_sha = _ls_remote(str(remote_url)).get(lock_ref)
            doc = _read_lock_blob(repo, lock_sha)
            now = time.time()
            locks = [
                _public_lock(lock)
                for lock in doc.get("locks", []) or []
                if isinstance(lock, dict) and _live_lock(lock, now)
            ]
            return _ok(locks=locks)
        finally:
            repo.close()
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception:  # noqa: BLE001
        _LOG.exception("Could not read PLM remote locks")
        return _err("internal", "Shared editing status is temporarily unavailable.")

def plm_lock(
    project_dir: str | os.PathLike,
    item_id: str,
    remote: str = "origin",
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Acquire an advisory lock, optionally replacing another live holder."""
    return _lock_mutate(
        project_dir, item_id, remote, acquire=True, force=bool(force)
    )

def plm_unlock(
    project_dir: str | os.PathLike,
    item_id: str,
    remote: str = "origin",
) -> dict[str, Any]:
    """Release an advisory lock via CAS (contract §6.3)."""
    return _lock_mutate(project_dir, item_id, remote, acquire=False)


def _lock_mutate(
    project_dir: str | os.PathLike,
    item_id: str,
    remote: str,
    *,
    acquire: bool,
    force: bool = False,
) -> dict[str, Any]:
    project_dir = Path(project_dir)
    if not item_id:
        return _err("invalid_item", "no item given")
    try:
        v = Vault.open(project_dir)
        try:
            m = v.manifest()
            entry = _remote_entry_by_name(m, remote)
            if entry is None and remote == "origin":
                entry = next(
                    (r for r in m.get("remotes", []) or []
                     if r.get("kind") in ("folder", "ssh", "https")),
                    None,
                )
            if entry is None:
                return _err("not_found", f"no remote named {remote!r}")
            remote_kind = entry.get("kind")
            if remote_kind == "chudware-cloud":
                return _err("reserved_remote", "Chudware Cloud is not available yet")
            if manifest.remote_mode(entry) == "live":
                return _err(
                    "live_join_required",
                    "This remote uses live editing; join the item with plm_live_join instead.",
                )
            if remote_kind not in ("folder", "ssh", "https"):
                return _err("invalid_mode", "This remote type cannot provide shared locks.")
            remote_url = approved_remote_url(project_dir, entry)
            if not remote_url:
                return _err("remote_unreachable", "This remote has no shared location.")
            lock_ref = str(m.get("lock_ref") or "refs/aicad/locks").encode("utf-8")
            remote_name = str(entry.get("name") or "origin")
        finally:
            v.close()

        holder_id, holder_display, holder_account_id = _lock_holder()
        for _attempt in range(_CAS_RETRIES):
            repo = _open_vault_repo(project_dir)
            try:
                try:
                    _fetch_folder(repo, str(remote_url), remote_name)
                    remote_lock = _ls_remote(str(remote_url)).get(lock_ref)
                except VaultError as e:
                    return _err(e.reason, e.hint)
                if remote_lock is not None:
                    expected_old = remote_lock
                    doc = _read_lock_blob(repo, remote_lock)
                else:
                    expected_old = None
                    doc = _empty_lock_manifest()

                now = time.time()
                locks = [
                    lk for lk in list(doc.get("locks") or [])
                    if isinstance(lk, dict) and _live_lock(lk, now)
                ]
                existing = next(
                    (lk for lk in locks if lk.get("item_id") == item_id),
                    None,
                )
                if acquire:
                    another_holder = (
                        existing is not None
                        and _lock_user_id(existing) != holder_id
                    )
                    if another_holder and not force:
                        who = existing.get("display") or _lock_user_id(existing)
                        return _err("locked", f"{who} is editing this part")
                    overrode = _public_lock(existing) if another_holder else None
                    new_lock = {
                        "item_id": item_id,
                        "holder": holder_id,
                        "user_id": holder_id,
                        "display": holder_display,
                        "account_id": holder_account_id,
                        "acquired_at": _now_iso(),
                        "expires_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(now + _LOCK_TTL_SEC),
                        ),
                    }
                    if existing is None:
                        locks.append(new_lock)
                    else:
                        locks = [
                            new_lock if lk.get("item_id") == item_id else lk
                            for lk in locks
                        ]
                    new_sha = _write_lock_blob(
                        repo, {"schema": _LOCK_SCHEMA, "locks": locks}
                    )
                    if not _cas_push(
                        repo, str(remote_url), new_sha,
                        expected_old=expected_old, ref=lock_ref,
                    ):
                        continue
                    fields: dict[str, Any] = {"lock": new_lock}
                    if overrode is not None:
                        fields["overrode"] = overrode
                    return _ok(**fields)

                if existing is None:
                    return _ok()
                if _lock_user_id(existing) != holder_id:
                    who = existing.get("display") or _lock_user_id(existing)
                    return _err("locked", f"{who} holds this lock")
                locks = [
                    lk for lk in locks if lk.get("item_id") != item_id
                ]
                new_sha = _write_lock_blob(
                    repo, {"schema": _LOCK_SCHEMA, "locks": locks}
                )
                if not _cas_push(
                    repo, str(remote_url), new_sha,
                    expected_old=expected_old, ref=lock_ref,
                ):
                    continue
                return _ok()
            finally:
                repo.close()
        return _err("contended", "The shared lock kept changing. Try again.")
    except VaultError as e:
        return _err(e.reason, e.hint)
    except Exception:  # noqa: BLE001
        _LOG.exception("Could not update PLM remote lock")
        return _err("internal", "The shared lock could not be updated.")


# ---------------------------------------------------------------------------
# convenience aliases matching tool names
# ---------------------------------------------------------------------------

def plm_add_remote(
    project_dir: str | os.PathLike,
    kind: str,
    name: str,
    url: str | None = None,
    display_name: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    return add_remote(
        project_dir,
        name,
        path=url,
        kind=kind,
        url=url,
        display_name=display_name,
        account_id=account_id,
    )


def plm_sync(project_dir: str | os.PathLike, remote_id: str = "origin") -> dict[str, Any]:
    return sync(project_dir, remote=remote_id)
