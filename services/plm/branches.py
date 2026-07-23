"""services.plm.branches — lightweight branching over the content-addressed vault.

Branches are named refs into the existing dulwich object store (cheap: a branch is
a single pointer; blobs/trees are shared). No git vocabulary is surfaced to users
— the public names are create / list / switch / merge.

Merge strategy is format-aware:

* ``.step+`` (text superset): feature-list 3-way merge. Features are ordered ops
  with stable ids — merge = id-based union; conflict when the same id was
  modified on both sides relative to the common base. Conflicts are returned as
  a structured list (never silent).
* ECAD boards: net / component (footprint) id-based merge, same rule.
* Binary / unknown paths: whole-file; conflict when both sides modified.

AI-VC checkpoints stay per-branch: the store root is
``.aicadvault/ai_vc/<branch>/``.

RPC surface (worker)::

    plm_branch_create  {path, name, from?}
    plm_branch_list    {path}
    plm_branch_switch  {path, name}
    plm_branch_merge   {path, src, dst}  → {ok, conflicts:[…], commit?}
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from typing import Any

from services.plm import gitbind, history as hist, manifest as manifest_mod, snapshot
from services.plm.vault import Vault, VaultError, _now_iso, _user_author

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

DEFAULT_BRANCH = "main"
_BRANCH_REF_PREFIX = b"refs/aicad/branches/"
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")

# Ordered list keys we merge id-based for CAD / ECAD documents.
_LIST_KEYS_STEP = ("features",)
_LIST_KEYS_ECAD = ("nets", "components", "footprints", "tracks", "vias", "zones", "pads")

# Paths that are PLM bookkeeping (per-entry meta/summary) — always take dst and
# never surface as user-facing merge conflicts.
_BOOKKEEPING_SUFFIXES = ("/meta.json", "/summary.txt", "meta.json", "summary.txt")

_LIST_KEYS_ASM = ("components", "mates")
_LIST_KEYS_DRAWING = ("views", "dims", "dimensions", "annotations")


# ---------------------------------------------------------------------------
# name / ref helpers
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    n = str(name or "").strip()
    if not n:
        raise VaultError("invalid_mode", "give the branch a name")
    if n in (".", "..") or n.startswith("/") or n.endswith("/"):
        raise VaultError("invalid_mode", "that branch name is not allowed")
    if not _BRANCH_NAME_RE.match(n):
        raise VaultError(
            "invalid_mode",
            "branch names use letters, numbers, dots, dashes, underscores, or slashes",
        )
    return n


def _ref_key(name: str) -> bytes:
    return _BRANCH_REF_PREFIX + name.encode("utf-8")


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# branch meta (side table: display names + created_at; tips live in git refs)
# ---------------------------------------------------------------------------

def _meta_path(vault: Vault) -> Path:
    return vault._bind.vault_dir / "branches" / "meta.json"


def _read_meta(vault: Vault) -> dict[str, Any]:
    p = _meta_path(vault)
    if not p.is_file():
        return {"active": DEFAULT_BRANCH, "branches": {}}
    try:
        data = json.loads(p.read_text("utf-8"))
        if not isinstance(data, dict):
            return {"active": DEFAULT_BRANCH, "branches": {}}
        data.setdefault("active", DEFAULT_BRANCH)
        data.setdefault("branches", {})
        if not isinstance(data["branches"], dict):
            data["branches"] = {}
        return data
    except Exception:  # noqa: BLE001
        return {"active": DEFAULT_BRANCH, "branches": {}}


def _write_meta(vault: Vault, meta: dict[str, Any]) -> None:
    p = _meta_path(vault)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", "utf-8")


# ---------------------------------------------------------------------------
# gitbind ref helpers (kept here so gitbind stays linear-history primary)
# ---------------------------------------------------------------------------

def _ensure_ref_api(bind: gitbind.GitBinding) -> None:
    """No-op marker — ref ops use the dulwich repo directly via bind._repo."""
    _ = bind


def _get_branch_tip(bind: gitbind.GitBinding, name: str) -> str | None:
    key = _ref_key(name)
    try:
        raw = bind._repo.refs[key]
    except KeyError:
        # fall back to legacy heads/master when bootstrapping main
        if name == DEFAULT_BRANCH:
            return bind.head_commit()
        return None
    if isinstance(raw, bytes):
        return raw.decode("ascii")
    return str(raw) if raw else None


def _set_branch_tip(bind: gitbind.GitBinding, name: str, commit_hex: str) -> None:
    bind._repo.refs[_ref_key(name)] = commit_hex.encode("ascii")


def _set_head_to(bind: gitbind.GitBinding, commit_hex: str) -> None:
    """Point HEAD at *commit_hex* (detached-style absolute ref)."""
    bind._repo.refs[b"HEAD"] = commit_hex.encode("ascii")


def _commit_with_parents(
    bind: gitbind.GitBinding,
    files: dict[str, bytes],
    meta: gitbind.EntryMeta,
    parents: list[str],
) -> str:
    """Commit *files* with an explicit parent list (1 = linear, 2 = merge)."""
    from dulwich.objects import Commit

    obj_store = bind._repo.object_store
    root = bind._build_tree(files)
    commit = Commit()
    commit.tree = root.id
    commit.parents = [p.encode("ascii") for p in parents if p]
    who = f"{meta.author_name} <{meta.author_email}>".encode("utf-8")
    commit.author = commit.committer = who
    when = meta.authored_at if meta.authored_at is not None else int(time.time())
    commit.author_time = commit.commit_time = when
    commit.author_timezone = commit.commit_timezone = 0
    body = meta.message or ""
    trailer = f"aicad-plm-kind: {meta.kind}\n"
    if meta.scope and meta.scope != "design":
        trailer += f"aicad-plm-scope: {meta.scope}\n"
    commit.message = (f"{body}\n\n{trailer}").encode("utf-8")
    obj_store.add_object(commit)
    cid = commit.id.decode("ascii")
    # Move HEAD to the new merge/commit tip (absolute).
    bind._repo.refs[b"HEAD"] = commit.id
    return cid


def _ancestor_set(bind: gitbind.GitBinding, commit_hex: str) -> set[str]:
    seen: set[str] = set()
    stack = [commit_hex]
    while stack:
        c = stack.pop()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            parents = bind.commit_meta(c).get("parents") or []
        except Exception:  # noqa: BLE001
            parents = []
        for p in parents:
            if p and p not in seen:
                stack.append(p)
    return seen


def _merge_base(bind: gitbind.GitBinding, a: str, b: str) -> str | None:
    """Best-effort common ancestor of *a* and *b*.

    Walks *a*'s parent chain; returns the first commit that is also an ancestor
    of *b*. If *a* itself is an ancestor of *b*, returns *a* (already merged).
    """
    if not a or not b:
        return None
    if a == b:
        return a
    b_anc = _ancestor_set(bind, b)
    # Prefer a proper ancestor: walk parents of a first (skip a itself unless
    # a is already on b's history — fast-forward case).
    if a in b_anc:
        return a
    stack = []
    try:
        parents = bind.commit_meta(a).get("parents") or []
    except Exception:  # noqa: BLE001
        parents = []
    stack.extend(parents)
    seen: set[str] = {a}
    while stack:
        c = stack.pop(0)
        if not c or c in seen:
            continue
        seen.add(c)
        if c in b_anc:
            return c
        try:
            parents = bind.commit_meta(c).get("parents") or []
        except Exception:  # noqa: BLE001
            parents = []
        for p in parents:
            if p and p not in seen:
                stack.append(p)
    return None


# ---------------------------------------------------------------------------
# bootstrap
# ---------------------------------------------------------------------------

def ensure_default_branch(vault: Vault) -> dict[str, Any]:
    """Ensure ``main`` exists. Idempotent.

    Never advances ``main`` to HEAD when another branch is active — only seeds
    a missing tip, or keeps the *active* branch tip in sync with HEAD.
    """
    meta = _read_meta(vault)
    head = vault._bind.head_commit()
    branches_meta = meta.setdefault("branches", {})
    if DEFAULT_BRANCH not in branches_meta:
        branches_meta[DEFAULT_BRANCH] = {
            "name": DEFAULT_BRANCH,
            "created_at": _iso_now(),
            "from": None,
        }
    active = str(meta.get("active") or DEFAULT_BRANCH)
    if not meta.get("active"):
        meta["active"] = DEFAULT_BRANCH
        active = DEFAULT_BRANCH

    main_tip = _get_branch_tip(vault._bind, DEFAULT_BRANCH)
    if head and main_tip is None:
        # first seed only
        _set_branch_tip(vault._bind, DEFAULT_BRANCH, head)
    if head and active:
        # only the *active* branch tip tracks HEAD
        _set_branch_tip(vault._bind, active, head)

    _write_meta(vault, meta)
    try:
        vault.set_local_config({"active_branch": meta["active"]})
    except Exception:  # noqa: BLE001
        pass
    return meta


def active_branch(vault: Vault) -> str:
    meta = ensure_default_branch(vault)
    return str(meta.get("active") or DEFAULT_BRANCH)


def ai_vc_root_for(project_dir: str | Path, branch: str | None = None) -> Path:
    """Per-branch AI-VC store root under ``.aicadvault/ai_vc/<branch>/``."""
    b = branch or DEFAULT_BRANCH
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in b) or DEFAULT_BRANCH
    p = Path(project_dir) / ".aicadvault" / "ai_vc" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def bind_ai_vc_to_branch(vault: Vault, branch: str | None = None) -> Path:
    """Point the process AI-VC store at the (active) branch root. Returns root."""
    from services.plm import ai_vc

    b = branch or active_branch(vault)
    root = ai_vc_root_for(vault.project_dir, b)
    ai_vc.get_store(root)
    return root


# ---------------------------------------------------------------------------
# public ops
# ---------------------------------------------------------------------------

def list_branches(vault: Vault) -> dict[str, Any]:
    """Return ``{ok, active, branches:[{name, head, created_at, from, current}]}``."""
    meta = ensure_default_branch(vault)
    active = str(meta.get("active") or DEFAULT_BRANCH)
    # keep active tip synced with HEAD so saves advance the branch tip
    head = vault._bind.head_commit()
    if head and active:
        _set_branch_tip(vault._bind, active, head)
    out: list[dict[str, Any]] = []
    names = set(meta.get("branches", {}).keys()) or {DEFAULT_BRANCH}
    # also surface any refs that landed without meta (defensive)
    try:
        for k in vault._bind._repo.refs.keys():
            if isinstance(k, bytes) and k.startswith(_BRANCH_REF_PREFIX):
                names.add(k[len(_BRANCH_REF_PREFIX):].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        pass
    for name in sorted(names):
        info = (meta.get("branches") or {}).get(name) or {"name": name}
        tip = _get_branch_tip(vault._bind, name)
        out.append({
            "name": name,
            "head": tip,
            "created_at": info.get("created_at") or "",
            "from": info.get("from"),
            "current": name == active,
        })
    return {"ok": True, "active": active, "branches": out}


def create_branch(
    vault: Vault,
    name: str,
    *,
    from_ref: str | None = None,
) -> dict[str, Any]:
    """Create a branch named *name* pointing at *from_ref* (name or commit) or HEAD.

    Params shape mirrors the RPC: ``from`` is accepted as *from_ref*.
    """
    name = _normalize_name(name)
    meta = ensure_default_branch(vault)
    if name in (meta.get("branches") or {}) or _get_branch_tip(vault._bind, name):
        raise VaultError("already_exists", f"a branch named {name!r} already exists")

    tip: str | None = None
    if from_ref:
        fr = str(from_ref).strip()
        # branch name?
        tip = _get_branch_tip(vault._bind, fr)
        if tip is None:
            # treat as commit id
            try:
                vault._bind.commit_meta(fr)
                tip = fr
            except Exception as e:  # noqa: BLE001
                raise VaultError(
                    "not_found",
                    f"starting point {fr!r} could not be found",
                ) from e
    else:
        tip = vault._bind.head_commit()
    if not tip:
        raise VaultError("not_found", "the project has no history to branch from yet")

    _set_branch_tip(vault._bind, name, tip)
    meta.setdefault("branches", {})[name] = {
        "name": name,
        "created_at": _iso_now(),
        "from": from_ref or meta.get("active") or DEFAULT_BRANCH,
    }
    _write_meta(vault, meta)
    # seed empty AI-VC root for the new branch (checkpoints stay per-branch)
    ai_vc_root_for(vault.project_dir, name)
    return {
        "ok": True,
        "name": name,
        "head": tip,
        "from": from_ref or meta.get("active") or DEFAULT_BRANCH,
        "created_at": meta["branches"][name]["created_at"],
    }


def switch_branch(vault: Vault, name: str, *, materialize: bool = True) -> dict[str, Any]:
    """Make *name* the active branch; optionally rebuild working files from its tip."""
    name = _normalize_name(name)
    meta = ensure_default_branch(vault)
    # flush current HEAD tip onto the outgoing active branch before leaving
    cur = str(meta.get("active") or DEFAULT_BRANCH)
    head = vault._bind.head_commit()
    if head and cur:
        _set_branch_tip(vault._bind, cur, head)

    tip = _get_branch_tip(vault._bind, name)
    if tip is None and name not in (meta.get("branches") or {}):
        raise VaultError("not_found", f"no branch named {name!r}")
    if tip is None:
        raise VaultError("not_found", f"branch {name!r} has no history yet")

    _set_head_to(vault._bind, tip)
    meta["active"] = name
    if name not in meta.setdefault("branches", {}):
        meta["branches"][name] = {
            "name": name,
            "created_at": _iso_now(),
            "from": None,
        }
    _write_meta(vault, meta)
    try:
        vault.set_local_config({"active_branch": name})
    except Exception:  # noqa: BLE001
        pass

    if materialize:
        _materialize_head(vault)

    # re-bind AI-VC store to the new branch
    try:
        bind_ai_vc_to_branch(vault, name)
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "name": name, "head": tip, "active": name}


def _materialize_head(vault: Vault) -> None:
    """Rewrite working containers from HEAD (branch tip)."""
    head = vault._bind.head_commit()
    if not head:
        return
    tree = vault._bind.read_tree(head)
    m_raw = tree.get("manifest.json")
    if not m_raw:
        return
    m = manifest_mod.loads(m_raw)
    for it in m.get("items") or []:
        iid = it.get("item_id")
        if not iid:
            continue
        path = it.get("path") or ""
        try:
            exploded = snapshot.read_item_from_tree(tree, iid)
        except KeyError:
            continue
        try:
            container = snapshot.reassemble_container(exploded)
            dest = vault.project_dir / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(container)
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------
# 3-way merge
# ---------------------------------------------------------------------------

def _as_doc(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        d = json.loads(raw.decode("utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _item_id(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for k in ("id", "feature_id", "uuid", "name"):
        v = entry.get(k)
        if v is not None and str(v) != "":
            return str(v)
    return None


def _canonical_entry(entry: Any) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _index_by_id(items: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        iid = _item_id(it)
        if iid is not None:
            out[iid] = it
    return out


def _list_keys_for_doc(doc: dict[str, Any]) -> tuple[str, ...]:
    schema = str(doc.get("schema") or "")
    head = schema.split("/", 1)[0]
    if head in ("aicad.pcb", "aicad.sch", "aicad.ecad"):
        return _LIST_KEYS_ECAD
    if head in ("aicad.asm",):
        return _LIST_KEYS_ASM
    if head in ("aicad.drawing",):
        return _LIST_KEYS_DRAWING
    if head in ("aicad.part", "aicad.cam"):
        return _LIST_KEYS_STEP
    # heuristic: prefer features, else nets/components
    keys: list[str] = []
    for k in _LIST_KEYS_STEP + _LIST_KEYS_ECAD + _LIST_KEYS_ASM + _LIST_KEYS_DRAWING:
        if k in doc and isinstance(doc.get(k), list) and k not in keys:
            keys.append(k)
    return tuple(keys) if keys else _LIST_KEYS_STEP


def merge_id_list(
    base_list: list[Any] | None,
    src_list: list[Any] | None,
    dst_list: list[Any] | None,
    *,
    path: str,
    kind: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Id-based 3-way merge of an ordered list of dicts with stable ids.

    Returns ``(merged_list, conflicts)``. Conflict when the same id was modified
    on both sides relative to base (and the two sides disagree).
    """
    base_list = list(base_list or [])
    src_list = list(src_list or [])
    dst_list = list(dst_list or [])
    base_i = _index_by_id(base_list)
    src_i = _index_by_id(src_list)
    dst_i = _index_by_id(dst_list)

    all_ids: list[str] = []
    seen: set[str] = set()
    # preserve dst order first (current branch), then src-only additions in src order
    for it in dst_list:
        iid = _item_id(it)
        if iid and iid not in seen:
            all_ids.append(iid)
            seen.add(iid)
    for it in src_list:
        iid = _item_id(it)
        if iid and iid not in seen:
            all_ids.append(iid)
            seen.add(iid)
    # base-only (deleted both sides already handled by absence)
    for it in base_list:
        iid = _item_id(it)
        if iid and iid not in seen:
            # present only in base → both deleted; skip
            pass

    merged: list[Any] = []
    conflicts: list[dict[str, Any]] = []

    for iid in all_ids:
        b = base_i.get(iid)
        s = src_i.get(iid)
        d = dst_i.get(iid)
        b_c = _canonical_entry(b) if b is not None else None
        s_c = _canonical_entry(s) if s is not None else None
        d_c = _canonical_entry(d) if d is not None else None

        if s is None and d is None:
            continue  # both deleted
        if s is None and d is not None:
            # src deleted; dst kept/modified
            if b is not None and d_c != b_c:
                # src deleted, dst modified → conflict
                conflicts.append({
                    "path": path,
                    "kind": kind,
                    "id": iid,
                    "reason": "deleted_src_modified_dst",
                    "base": b,
                    "src": None,
                    "dst": d,
                })
                merged.append(copy.deepcopy(d))  # keep dst, surface conflict
            elif b is not None and d_c == b_c:
                # src deleted, dst unchanged → take deletion
                pass
            else:
                # no base: dst-only addition, src never had it
                merged.append(copy.deepcopy(d))
            continue
        if d is None and s is not None:
            if b is not None and s_c != b_c:
                conflicts.append({
                    "path": path,
                    "kind": kind,
                    "id": iid,
                    "reason": "deleted_dst_modified_src",
                    "base": b,
                    "src": s,
                    "dst": None,
                })
                # keep src modification but flag
                merged.append(copy.deepcopy(s))
            elif b is not None and s_c == b_c:
                pass  # dst deleted, src unchanged → deletion wins
            else:
                merged.append(copy.deepcopy(s))  # src-only add
            continue

        # both present
        assert s is not None and d is not None
        if s_c == d_c:
            merged.append(copy.deepcopy(d))
            continue
        s_mod = b_c is None or s_c != b_c
        d_mod = b_c is None or d_c != b_c
        if s_mod and d_mod:
            conflicts.append({
                "path": path,
                "kind": kind,
                "id": iid,
                "reason": "both_modified",
                "base": b,
                "src": s,
                "dst": d,
            })
            # keep dst (active branch) value; conflict is structured for UI
            merged.append(copy.deepcopy(d))
        elif s_mod:
            merged.append(copy.deepcopy(s))
        else:
            merged.append(copy.deepcopy(d))

    return merged, conflicts


def merge_document(
    base_doc: dict[str, Any] | None,
    src_doc: dict[str, Any] | None,
    dst_doc: dict[str, Any] | None,
    *,
    path: str = "document.json",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """3-way merge two document.json dicts. Returns (merged, conflicts)."""
    if src_doc is None and dst_doc is None:
        return None, []
    if src_doc is None:
        return copy.deepcopy(dst_doc), []
    if dst_doc is None:
        return copy.deepcopy(src_doc), []

    # whole-doc identical?
    if _canonical_entry(src_doc) == _canonical_entry(dst_doc):
        return copy.deepcopy(dst_doc), []

    base_doc = base_doc if isinstance(base_doc, dict) else {}
    # start from dst (active) and overlay id-list merges
    merged = copy.deepcopy(dst_doc)
    conflicts: list[dict[str, Any]] = []

    keys = _list_keys_for_doc(dst_doc if isinstance(dst_doc, dict) else src_doc)
    # also include keys present only on src
    for k in _list_keys_for_doc(src_doc):
        if k not in keys:
            keys = keys + (k,)

    any_list = False
    for key in keys:
        if key not in src_doc and key not in dst_doc and key not in base_doc:
            continue
        if not (isinstance(src_doc.get(key), list) or isinstance(dst_doc.get(key), list)
                or isinstance(base_doc.get(key), list)):
            continue
        any_list = True
        mlist, cfl = merge_id_list(
            base_doc.get(key) if isinstance(base_doc.get(key), list) else [],
            src_doc.get(key) if isinstance(src_doc.get(key), list) else [],
            dst_doc.get(key) if isinstance(dst_doc.get(key), list) else [],
            path=f"{path}:{key}",
            kind=key.rstrip("s") if key.endswith("s") else key,
        )
        merged[key] = mlist
        conflicts.extend(cfl)

    # scalar / non-list keys: take dst unless only src changed
    all_keys = set(base_doc) | set(src_doc) | set(dst_doc)
    for key in all_keys:
        if key in keys:
            continue
        b = base_doc.get(key)
        s = src_doc.get(key)
        d = dst_doc.get(key)
        if s == d:
            merged[key] = copy.deepcopy(d)
            continue
        s_mod = s != b
        d_mod = d != b
        if s_mod and d_mod:
            # for non-list structured values that look like feature maps, still conflict
            conflicts.append({
                "path": f"{path}:{key}",
                "kind": "field",
                "id": key,
                "reason": "both_modified",
                "base": b,
                "src": s,
                "dst": d,
            })
            merged[key] = copy.deepcopy(d)
        elif s_mod:
            merged[key] = copy.deepcopy(s)
        else:
            merged[key] = copy.deepcopy(d)

    if not any_list and not keys:
        # pure binary-ish document with no known list keys — whole-doc conflict if both changed
        b_c = _canonical_entry(base_doc) if base_doc else None
        s_c = _canonical_entry(src_doc)
        d_c = _canonical_entry(dst_doc)
        if s_c != d_c and (b_c is None or (s_c != b_c and d_c != b_c)):
            conflicts.append({
                "path": path,
                "kind": "document",
                "id": None,
                "reason": "both_modified",
                "base": base_doc or None,
                "src": src_doc,
                "dst": dst_doc,
            })

    return merged, conflicts


def merge_blob(
    base: bytes | None,
    src: bytes | None,
    dst: bytes | None,
    *,
    path: str,
) -> tuple[bytes | None, list[dict[str, Any]]]:
    """Whole-file 3-way merge for binary/unknown blobs."""
    if src is None and dst is None:
        return None, []
    if src is None:
        return dst, []
    if dst is None:
        return src, []
    if src == dst:
        return dst, []
    s_mod = base is None or src != base
    d_mod = base is None or dst != base
    if s_mod and d_mod:
        return dst, [{
            "path": path,
            "kind": "binary",
            "id": None,
            "reason": "both_modified",
            "base_hash": _hash_hex(base) if base is not None else None,
            "src_hash": _hash_hex(src),
            "dst_hash": _hash_hex(dst),
        }]
    if s_mod:
        return src, []
    return dst, []


def _hash_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()



def _is_bookkeeping(path: str) -> bool:
    if path in ("meta.json", "summary.txt", "manifest.json"):
        return True
    return path.endswith("/meta.json") or path.endswith("/summary.txt")


def _merge_trees(
    base_tree: dict[str, bytes],
    src_tree: dict[str, bytes],
    dst_tree: dict[str, bytes],
) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Merge two shadow trees; document.json paths get semantic merge."""
    paths = set(base_tree) | set(src_tree) | set(dst_tree)
    out: dict[str, bytes] = {}
    conflicts: list[dict[str, Any]] = []

    for path in sorted(paths):
        b = base_tree.get(path)
        s = src_tree.get(path)
        d = dst_tree.get(path)
        if path.endswith("/document.json") or path == "document.json":
            bdoc = _as_doc(b)
            sdoc = _as_doc(s)
            ddoc = _as_doc(d)
            if sdoc is not None or ddoc is not None:
                merged_doc, cfl = merge_document(bdoc, sdoc, ddoc, path=path)
                conflicts.extend(cfl)
                if merged_doc is not None:
                    try:
                        from services.plm.canonical import canonical_dump
                        out[path] = canonical_dump(merged_doc).encode("utf-8")
                    except Exception:  # noqa: BLE001
                        out[path] = json.dumps(
                            merged_doc, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                continue
        # PLM bookkeeping: prefer dst, fall back to src; never conflict
        if _is_bookkeeping(path):
            if d is not None:
                out[path] = d
            elif s is not None:
                out[path] = s
            elif b is not None:
                out[path] = b
            continue
        # non-document: whole-file
        if s is None and d is None:
            continue
        if s is None:
            if d is not None:
                # src deleted
                if b is not None and d != b:
                    conflicts.append({
                        "path": path, "kind": "binary", "id": None,
                        "reason": "deleted_src_modified_dst",
                        "base_hash": _hash_hex(b),
                        "src_hash": None,
                        "dst_hash": _hash_hex(d),
                    })
                    out[path] = d
                elif b is not None and d == b:
                    pass  # deleted
                else:
                    out[path] = d
            continue
        if d is None:
            if b is not None and s != b:
                conflicts.append({
                    "path": path, "kind": "binary", "id": None,
                    "reason": "deleted_dst_modified_src",
                    "base_hash": _hash_hex(b),
                    "src_hash": _hash_hex(s),
                    "dst_hash": None,
                })
                out[path] = s
            elif b is not None and s == b:
                pass
            else:
                out[path] = s
            continue
        blob, cfl = merge_blob(b, s, d, path=path)
        conflicts.extend(cfl)
        if blob is not None:
            out[path] = blob
    return out, conflicts


def merge_branches(
    vault: Vault,
    src: str,
    dst: str,
    *,
    message: str = "",
) -> dict[str, Any]:
    """Merge *src* into *dst*. On conflicts, still writes dst tip with dst-favored
    resolution and returns the structured conflict list (never silent)."""
    src = _normalize_name(src)
    dst = _normalize_name(dst)
    meta = ensure_default_branch(vault)

    # sync active tip first
    head = vault._bind.head_commit()
    active = str(meta.get("active") or DEFAULT_BRANCH)
    if head and active:
        _set_branch_tip(vault._bind, active, head)

    src_tip = _get_branch_tip(vault._bind, src)
    dst_tip = _get_branch_tip(vault._bind, dst)
    if not src_tip:
        raise VaultError("not_found", f"no branch named {src!r}")
    if not dst_tip:
        raise VaultError("not_found", f"no branch named {dst!r}")

    if src_tip == dst_tip:
        return {
            "ok": True,
            "src": src,
            "dst": dst,
            "commit": dst_tip,
            "conflicts": [],
            "conflict_count": 0,
            "already_up_to_date": True,
        }

    base = _merge_base(vault._bind, src_tip, dst_tip)
    base_tree = vault._bind.read_tree(base) if base else {}
    src_tree = vault._bind.read_tree(src_tip)
    dst_tree = vault._bind.read_tree(dst_tip)

    merged_tree, conflicts = _merge_trees(base_tree, src_tree, dst_tree)

    author = _user_author()
    msg = message or f"Merged {src} into {dst}"
    if conflicts:
        msg = f"{msg} ({len(conflicts)} conflict{'s' if len(conflicts) != 1 else ''})"
    emeta = gitbind.EntryMeta(
        kind=hist.KIND_CHECKPOINT,
        message=msg,
        author_name=author["display"],
        author_email=f'{author["id"]}@aicad.local',
    )
    # land merge on dst: temporarily set HEAD to dst tip, commit with 2 parents
    _set_head_to(vault._bind, dst_tip)
    commit = _commit_with_parents(vault._bind, merged_tree, emeta, [dst_tip, src_tip])
    _set_branch_tip(vault._bind, dst, commit)

    # if dst is active, materialize working tree
    if active == dst:
        meta["active"] = dst
        _write_meta(vault, meta)
        _materialize_head(vault)
    else:
        # restore HEAD to the active branch tip
        act_tip = _get_branch_tip(vault._bind, active) or head
        if act_tip:
            _set_head_to(vault._bind, act_tip)
        _write_meta(vault, meta)

    return {
        "ok": True,
        "src": src,
        "dst": dst,
        "commit": commit,
        "base": base,
        "conflicts": conflicts,
        "conflict_count": len(conflicts),
        "already_up_to_date": False,
    }


# ---------------------------------------------------------------------------
# Vault-facing convenience (used by RPCs)
# ---------------------------------------------------------------------------

def branch_create(project_dir: str | Path, name: str, from_ref: str | None = None) -> dict[str, Any]:
    v = Vault.open(project_dir)
    try:
        return create_branch(v, name, from_ref=from_ref)
    finally:
        v.close()


def branch_list(project_dir: str | Path) -> dict[str, Any]:
    v = Vault.open(project_dir)
    try:
        return list_branches(v)
    finally:
        v.close()


def branch_switch(project_dir: str | Path, name: str) -> dict[str, Any]:
    v = Vault.open(project_dir)
    try:
        return switch_branch(v, name)
    finally:
        v.close()


def branch_merge(
    project_dir: str | Path, src: str, dst: str, *, message: str = "",
) -> dict[str, Any]:
    v = Vault.open(project_dir)
    try:
        return merge_branches(v, src, dst, message=message)
    finally:
        v.close()
