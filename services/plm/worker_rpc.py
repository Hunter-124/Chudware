"""Worker-RPC bridge for the PLM vault — the in-editor GUI seam (T-R5).

Exposes the ``plm_*`` family as **engine-worker methods** so the C++/ImGui
editor (history timeline panel, checkpoint/release toolbar, compare view,
doc-tab revision badges — ``ui/imgui/src/ui/plm_panel.cpp``) reaches the vault
through the same newline-delimited-JSON worker seam (``WorkerClient`` /
``AsyncRpc``, ``services/worker/``) it already uses for every other service —
no bespoke transport.

These are thin wrappers over :class:`services.plm.vault.Vault` and the T-R3/
T-R4 modules — the SAME single implementation behind the ``plm_*`` MCP tools
(``services/mcp/plm_tools.py``); no vault logic lives here. Responses are the
contract §0 flat envelope as a dict: ``{ok: true, …}`` / ``{ok: false, reason,
hint}`` — never a traceback (worker methods degrade gracefully when dulwich is
not provisioned).

GUI extras beyond the MCP twins (additive, worker-seam only):

* ``plm_history`` accepts ``thumb_dir`` — exports each entry's committed
  ``thumbnail.png`` blob to ``<thumb_dir>/<entry-id>.png`` and adds a
  ``thumbnail_file`` field, so the C++ timeline can texture-load real
  thumbnails without a second round trip.
* ``plm_status`` adds ``part_number_prefix`` + ``next_part_number`` so the
  release flow can pre-fill the suggested number (T-R5 locked decision).
* ``plm_draft`` — checkpoint/release message drafting for the toolbar. The
  LOCAL model tier phrases a message from the deterministic T-R3 change
  summary when (and only when) a local, loopback-served model is provisioned;
  otherwise it reports ``source:"manual"`` and the GUI shows a plain manual
  box. Frontier/cloud providers are never used here.
* ``plm_scheme_set`` / ``plm_discard_draft`` — the T-R5 vault seams
  (configurable part-number scheme; "back to rev X (discard draft)").

Ease-of-use law (contract §8.1): no git vocabulary in any string a user could
see. Registered append-only in ``engine/worker.py``, following the
deliverables.worker_rpc / parts_hub.worker_rpc / dag.worker_rpc precedent.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# envelope helpers
# ---------------------------------------------------------------------------

_NO_VAULT = {"ok": False, "reason": "no_vault",
             "hint": "Version history needs a one-time setup on this machine. "
                     "Restart AI-CAD to finish setting it up."}


def _with_vault(params: dict, fn: Callable[[Any], dict]) -> dict:
    """Open the project vault, run *fn(vault)*, close, and envelope errors."""
    try:
        from services.plm.vault import Vault, VaultError
    except ImportError:
        return dict(_NO_VAULT)
    path = str((params or {}).get("path", "") or "")
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    try:
        v = Vault.open(path)
        try:
            return fn(v)
        finally:
            v.close()
    except VaultError as e:
        out = {"ok": False, "reason": e.reason, "hint": e.hint}
        problems = getattr(e, "problems", None)
        if problems is not None:
            out["problems"] = problems
        return out
    except Exception as e:  # noqa: BLE001 — worker methods never raise
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# core surface (MCP twins)
# ---------------------------------------------------------------------------

def _plm_open(params: dict, _emit: Callable) -> dict:
    """Open (auto-creating) the vault for a project folder — zero-config."""
    def run(v: Any) -> dict:
        m = v.manifest()
        return {"ok": True, "project_id": m["project"]["id"], "manifest": m}
    return _with_vault(params, run)


def _plm_status(params: dict, _emit: Callable) -> dict:
    """Project state for badges/panel + part-number scheme extras."""
    def run(v: Any) -> dict:
        from services.plm import manifest as manifest_mod

        st = v.status()
        m = v.manifest()
        st["part_number_prefix"] = m.get("part_number_prefix", "")
        st["next_part_number"] = manifest_mod.next_part_number(m)
        return {"ok": True, **st}
    return _with_vault(params, run)


def _plm_history(params: dict, _emit: Callable) -> dict:
    """The timeline. With ``thumb_dir``: export per-entry thumbnails to files."""
    def run(v: Any) -> dict:
        from services.plm import snapshot

        item_id = (params or {}).get("item_id") or None
        limit = int((params or {}).get("limit") or 0) or None
        scope = str((params or {}).get("scope", "design") or "design")
        entries = v.history(item_id, limit, scope=scope)
        thumb_dir = str((params or {}).get("thumb_dir", "") or "")
        if thumb_dir:
            td = Path(thumb_dir)
            td.mkdir(parents=True, exist_ok=True)
            for e in entries:
                ref = e.get("thumbnail_ref")
                if not ref:
                    continue
                dest = td / f'{e["commit"]}.png'
                if not dest.exists():
                    try:
                        tree = v._bind.read_tree(e["commit"])  # noqa: SLF001 — same package
                        raw = tree.get(ref)
                        if raw:
                            dest.write_bytes(raw)
                    except Exception:  # noqa: BLE001 — thumbnails are best-effort
                        continue
                if dest.exists():
                    e["thumbnail_file"] = str(dest)
        return {"ok": True, "entries": entries, "head": v.status()["head"]}
    return _with_vault(params, run)


def _plm_save(params: dict, _emit: Callable) -> dict:
    """An explicit save → one auto (or checkpoint) history entry."""
    kind = str((params or {}).get("kind", "auto") or "auto")
    if kind not in ("auto", "checkpoint"):
        return {"ok": False, "reason": "invalid_mode",
                "hint": "a save can only be a save or a named checkpoint"}

    def run(v: Any) -> dict:
        res = v.save(kind)
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
    return _with_vault(params, run)


def _plm_checkpoint(params: dict, _emit: Callable) -> dict:
    """Save a named checkpoint the user can return to."""
    def run(v: Any) -> dict:
        res = v.checkpoint(str((params or {}).get("message", "") or ""),
                           item_id=(params or {}).get("item_id") or None)
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
    return _with_vault(params, run)


def _plm_release(params: dict, _emit: Callable) -> dict:
    """Release an item: pre-flight, rev stamp, geometry + BOM freeze (T-R4)."""
    def run(v: Any) -> dict:
        from services.plm import release as release_mod

        res = release_mod.release(v, str((params or {}).get("item_id", "")),
                                  str((params or {}).get("message", "") or ""),
                                  confirm=bool((params or {}).get("confirm")))
        return {"ok": True, "commit": res["commit"], "rev": res["rev"],
                "entry": res["entry"], "preflight": res["preflight"],
                "bom_freeze": res.get("bom_freeze"),
                "stamp_orders": res["stamp_orders"]}
    return _with_vault(params, run)


def _plm_restore(params: dict, _emit: Callable) -> dict:
    """Return the project (or one item) to an earlier point — forward-only."""
    def run(v: Any) -> dict:
        res = v.restore(str((params or {}).get("commit", "")),
                        (params or {}).get("item_id") or None)
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
    return _with_vault(params, run)


def _plm_item_list(params: dict, _emit: Callable) -> dict:
    """The full item registry for the Files/Items page: each item's view (with
    ``doc_type``), whether it has unsaved changes, and its thumbnail path when
    one exists. Optional ``doc_type`` / ``kind`` filters. Fixes the historical
    empty-param GUI call."""
    def run(v: Any) -> dict:
        from services.plm import items as items_mod
        from services.plm import manifest as manifest_mod

        m = v.manifest()
        dirty = set(v._dirty_items(m))  # noqa: SLF001 — same package
        want_dt = str((params or {}).get("doc_type", "") or "")
        want_kind = str((params or {}).get("kind", "") or "")
        out: list[dict[str, Any]] = []
        for it in m["items"]:
            if want_dt and it.get("doc_type") != want_dt:
                continue
            if want_kind and it.get("kind") != want_kind:
                continue
            view = items_mod.item_view(it)
            view["dirty"] = it["item_id"] in dirty
            out.append(view)
        _ = manifest_mod  # imported for parity with other methods
        return {"ok": True, "items": out}
    return _with_vault(params, run)


def _plm_item_add(params: dict, _emit: Callable) -> dict:
    """Track a non-CAD file/folder as a versioned item ("Track this file")."""
    def run(v: Any) -> dict:
        rel = str((params or {}).get("file_path") or (params or {}).get("folder_path") or "")
        if not rel:
            return {"ok": False, "reason": "invalid_mode",
                    "hint": "give a file or folder to track"}
        item = v.add_item(rel,
                          doc_type=(params or {}).get("doc_type") or None,
                          part_number=(params or {}).get("part_number") or None)
        return {"ok": True, "item": item}
    return _with_vault(params, run)


def _plm_item_untrack(params: dict, _emit: Callable) -> dict:
    """Stop tracking an item (history keeps its old versions)."""
    def run(v: Any) -> dict:
        item = v.untrack_item(str((params or {}).get("item_id", "")))
        return {"ok": True, "item": item}
    return _with_vault(params, run)


def _plm_link_repo(params: dict, _emit: Callable) -> dict:
    """Link an external source repository by reference (no code stored)."""
    def run(v: Any) -> dict:
        item = v.link_repo(str((params or {}).get("url", "")),
                           str((params or {}).get("name", "") or ""),
                           checkout_path=(params or {}).get("checkout_path") or None)
        return {"ok": True, "item": item}
    return _with_vault(params, run)


def _plm_where_used(params: dict, _emit: Callable) -> dict:
    """What consumes an item (assemblies/drawings/CAM), derived live."""
    def run(v: Any) -> dict:
        from services.plm import whereused

        m = v.manifest()
        docs = whereused.docs_at_head(v)
        try:
            consumers = whereused.where_used(
                m, docs, str((params or {}).get("item_id", "")),
                transitive=bool((params or {}).get("transitive")))
        except KeyError:
            return {"ok": False, "reason": "invalid_item",
                    "hint": "no such item"}
        return {"ok": True, "consumers": consumers}
    return _with_vault(params, run)


def _plm_set_ai_mode(params: dict, _emit: Callable) -> dict:
    """Set the assist tier (manual | assisted | managed) — worker twin."""
    def run(v: Any) -> dict:
        return {"ok": True, "ai_mode": v.set_ai_mode(str((params or {}).get("mode", "")))}
    return _with_vault(params, run)


def _plm_plan(params: dict, _emit: Callable) -> dict:
    """Read/mutate the planning store (tasks/issues/milestones). One multiplexed
    tool: ``op`` ∈ list|create|update|delete, ``entity`` ∈ task|issue|milestone."""
    def run(v: Any) -> dict:
        p = params or {}
        res = v.plan(str(p.get("op", "list")), str(p.get("entity", "")),
                     data=p.get("data") or None, ent_id=p.get("id") or None,
                     actor=p.get("actor") or None)
        return {"ok": True, **res}
    return _with_vault(params, run)


def _plm_overview(params: dict, _emit: Callable) -> dict:
    """One-round-trip hub dashboard aggregate."""
    def run(v: Any) -> dict:
        return {"ok": True, **v.overview(mine=(params or {}).get("mine") or None)}
    return _with_vault(params, run)


def _plm_settings_get(params: dict, _emit: Callable) -> dict:
    """Per-machine vault-local settings (visible columns, last page, …)."""
    def run(v: Any) -> dict:
        return {"ok": True, "settings": v.get_local_config()}
    return _with_vault(params, run)


def _plm_settings_set(params: dict, _emit: Callable) -> dict:
    """Merge a patch into the vault-local settings."""
    def run(v: Any) -> dict:
        patch = (params or {}).get("settings")
        if not isinstance(patch, dict):
            return {"ok": False, "reason": "invalid_mode",
                    "hint": "settings must be an object"}
        return {"ok": True, "settings": v.set_local_config(patch)}
    return _with_vault(params, run)


def _plm_timeline(params: dict, _emit: Callable) -> dict:
    """AI-VC timeline: checkpoints for a doc (or all). Newest first.

    Shape: ``{ok, checkpoints:[{id, turn, label, time, features_count}]}``.
    When ``path`` is given, the store is bound to the project's active branch
    so checkpoints stay per-branch.
    """
    try:
        from services.plm import ai_vc
    except ImportError:
        return dict(_NO_VAULT)
    doc_id = (params or {}).get("doc_id") or None
    if doc_id is not None:
        doc_id = str(doc_id) or None
    path = str((params or {}).get("path") or (params or {}).get("project_dir") or "").strip()
    store = None
    if path:
        try:
            from services.plm import branches
            from services.plm.vault import Vault
            v = Vault.open(path)
            try:
                root = branches.bind_ai_vc_to_branch(v)
                store = ai_vc.get_store(root)
            finally:
                v.close()
        except Exception:  # noqa: BLE001 — fall through to default store
            store = None
    try:
        return ai_vc.plm_timeline(doc_id, store=store)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


def _plm_rollback(params: dict, _emit: Callable) -> dict:
    """AI-VC rollback: restore doc to checkpoint id | turn:N | -N.

    Shape: ``{ok, checkpoint_id, restored_turn, content_hash, via}``.
    """
    try:
        from services.plm import ai_vc
    except ImportError:
        return dict(_NO_VAULT)
    doc_id = (params or {}).get("doc_id") or None
    if doc_id is not None:
        doc_id = str(doc_id) or None
    to = str((params or {}).get("to", "") or "")
    try:
        return ai_vc.plm_rollback(doc_id, to)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


def _plm_compare(params: dict, _emit: Callable) -> dict:
    """Semantic (+ optional visual) compare of one item between two entries.

    Generic (non-CAD) items route to the content/text-diff comparison instead
    of the geometric ``doc_diff``.
    """
    def run(v: Any) -> dict:
        from services.plm import compare as compare_mod

        item_id = str((params or {}).get("item_id", ""))
        if compare_mod.is_generic_item(v, item_id):
            return {"ok": True, **compare_mod.generic_compare(
                v, str((params or {}).get("commit_a", "")),
                str((params or {}).get("commit_b", "")), item_id)}
        doc_a, doc_b = compare_mod.docs_at(
            v, str((params or {}).get("commit_a", "")),
            str((params or {}).get("commit_b", "")),
            str((params or {}).get("item_id", "")))
        sem = compare_mod.semantic_compare(doc_a, doc_b)
        out: dict[str, Any] = {"ok": True, "doc_diff": sem["doc_diff"],
                               "summary": sem["summary"]}
        if bool((params or {}).get("visual")):
            import tempfile

            od = str((params or {}).get("out_dir", "") or "")
            od = od or tempfile.mkdtemp(prefix="aicad_plm_compare_")
            try:
                out["eval_compare"] = compare_mod.visual_compare(doc_a, doc_b, od)
            except Exception as e:  # noqa: BLE001 — visual half degrades gracefully
                out["visual_unavailable"] = f"{type(e).__name__}: {e}"
        return out
    return _with_vault(params, run)


def _plm_item_set(params: dict, _emit: Callable) -> dict:
    """Edit an item's registry entry: part number and/or start a new revision."""
    part_number = str((params or {}).get("part_number", "") or "")
    start_new_rev = bool((params or {}).get("start_new_rev"))
    if not part_number and not start_new_rev:
        return {"ok": False, "reason": "invalid_mode",
                "hint": "nothing to change — give a part number or ask to "
                        "start a new revision"}

    def run(v: Any) -> dict:
        item_id = str((params or {}).get("item_id", ""))
        out: dict[str, Any] = {"ok": True}
        if part_number:
            out["item"] = v.set_part_number(item_id, part_number)
        if start_new_rev:
            res = v.start_new_rev(item_id)
            out["item"] = res["item"]
            out["working_rev"] = res["working_rev"]
        return out
    return _with_vault(params, run)


# ---------------------------------------------------------------------------
# T-R5 seams: scheme config + discard draft
# ---------------------------------------------------------------------------

def _plm_scheme_set(params: dict, _emit: Callable) -> dict:
    """Configure the part-number scheme prefix (suggestions only)."""
    def run(v: Any) -> dict:
        res = v.set_part_number_prefix(str((params or {}).get("prefix", "")))
        return {"ok": True, **res}
    return _with_vault(params, run)


def _plm_discard_draft(params: dict, _emit: Callable) -> dict:
    """Undo a next-rev draft: back to the item's released form (forward entry)."""
    def run(v: Any) -> dict:
        res = v.discard_working_rev(str((params or {}).get("item_id", "")))
        return {"ok": True, "item": res["item"], "entry": res["entry"]}
    return _with_vault(params, run)


# ---------------------------------------------------------------------------
# plm_draft — LOCAL model tier message drafting (T-R5 locked decision)
# ---------------------------------------------------------------------------

# Test seam: inject a fake local-model phrasing function (summary -> message).
# None = resolve the configured provider; a callable bypasses provider lookup.
_local_draft_hook: Callable[[str], str] | None = None


def set_local_draft_hook(fn: Callable[[str], str] | None) -> None:
    """Register (or clear) a local-model phrasing hook — tests only."""
    global _local_draft_hook
    _local_draft_hook = fn


_DRAFT_SYSTEM = (
    "You write one-sentence design-history notes for a CAD app. Rephrase the "
    "change summary as a short, plain-language note. Copy every number "
    "exactly. Never use version-control jargon. Reply with the note only."
)


def _local_provider() -> Any | None:
    """The configured provider IFF it keeps traffic on this machine.

    Cloud providers (Anthropic, Gemini, cloud OpenAI, OpenRouter) are never
    used for drafting — this returns None for them, which the caller maps to
    the plain manual box. Only loopback-served local models qualify.
    """
    if os.environ.get("AICAD_PLM_DRAFT_LOCAL", "1") == "0":
        return None
    try:
        from services.agent import config as agent_config

        cfg = agent_config.load_config()
        name = (cfg.get("provider") or "").lower()
        if name not in ("llamacpp", "llama.cpp", "llama-cpp", "vllm", "openai"):
            return None
        provider = agent_config.get_provider(name, cfg)
        if not agent_config.provider_offline_safe(provider):
            return None
        return provider
    except Exception:  # noqa: BLE001 — unprovisioned/misconfigured -> manual box
        return None


def _validated(draft: str, summary: str) -> str:
    """Accept a model draft only if it is git-vocab-free and numeral-faithful."""
    from services.plm import summarize

    draft = " ".join(str(draft or "").split())
    if not draft or len(draft) > 400:
        return ""
    words = set(draft.lower().replace(".", " ").replace(",", " ").split())
    if words & summarize._GIT_VOCAB:  # noqa: SLF001 — same normative word list
        return ""
    if not summarize.numeral_tokens(draft) <= summarize.numeral_tokens(summary):
        return ""
    return draft


def _plm_draft(params: dict, _emit: Callable) -> dict:
    """Draft a checkpoint/release message for the toolbar.

    Returns ``{ok, message, summary, source}`` where ``source`` is
    ``"local_model"`` (a provisioned local model phrased the message from the
    deterministic T-R3 change summary) or ``"manual"`` (no local model — the
    GUI shows a plain manual box; ``summary`` is still returned for context).
    """
    mode = str((params or {}).get("mode", "checkpoint") or "checkpoint")
    if mode not in ("checkpoint", "release"):
        return {"ok": False, "reason": "invalid_mode",
                "hint": "a draft is for a checkpoint or a release"}

    def run(v: Any) -> dict:
        item_id = (params or {}).get("item_id") or None
        if mode == "release":
            summary = v.draft_release_note(str(item_id or ""))
        else:
            summary = v.draft_message(item_id=item_id)
        out = {"ok": True, "message": "", "summary": summary, "source": "manual"}
        if not summary:
            return out  # nothing changed — nothing to phrase
        hook = _local_draft_hook
        if hook is not None:
            message = _validated(hook(summary), summary)
            if message:
                return {"ok": True, "message": message, "summary": summary,
                        "source": "local_model"}
            return out
        provider = _local_provider()
        if provider is None:
            return out
        try:
            raw = provider.complete(
                _DRAFT_SYSTEM,
                [{"role": "user", "content": f"Change summary: {summary}"}],
                max_tokens=120)
        except Exception:  # noqa: BLE001 — local server down -> manual box
            return out
        message = _validated(raw, summary)
        if message:
            return {"ok": True, "message": message, "summary": summary,
                    "source": "local_model"}
        return out
    return _with_vault(params, run)


# ---------------------------------------------------------------------------
# Git tab — real git / GitHub client (services/plm/gitscm.py)
#
# Unlike the vault methods above, these are an EXPLICIT git surface for the PLM
# "Git" GUI tab: git vocabulary is expected here. Each takes {"path": <folder>}
# and any op-specific params; gitscm already returns the flat envelope, so these
# wrappers just route. gitscm never raises and degrades to
# {ok:false, reason:"git_missing"/"gh_missing"} when a binary is absent.
# ---------------------------------------------------------------------------

def _git_path(params: dict) -> str:
    return str((params or {}).get("path", "") or "")


def _git_rpc(params: dict, _emit: Callable, fn_name: str, *arg_keys) -> dict:
    from services.plm import gitscm
    path = _git_path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    fn = getattr(gitscm, fn_name)
    kwargs = {}
    for key, cast, default in arg_keys:
        val = (params or {}).get(key, default)
        kwargs[key] = cast(val) if val is not None else default
    try:
        return fn(path, **kwargs)
    except Exception as e:  # noqa: BLE001 — never raise across the seam
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


def _git_status(params, _emit):     return _git_rpc(params, _emit, "status")
def _git_log(params, _emit):        return _git_rpc(params, _emit, "log", ("limit", int, 50), ("ref", str, ""))
def _git_branches(params, _emit):   return _git_rpc(params, _emit, "branches")
def _git_remotes(params, _emit):    return _git_rpc(params, _emit, "remotes")
def _git_tree(params, _emit):       return _git_rpc(params, _emit, "tree", ("ref", str, "HEAD"))
def _git_show(params, _emit):       return _git_rpc(params, _emit, "file_at", ("file", str, ""), ("ref", str, "HEAD"))
def _git_diff(params, _emit):       return _git_rpc(params, _emit, "diff", ("file", str, ""), ("staged", bool, False))
def _git_is_repo(params, _emit):    return _git_rpc(params, _emit, "is_repo")
def _git_init(params, _emit):       return _git_rpc(params, _emit, "init")
def _git_unstage(params, _emit):    return _git_rpc(params, _emit, "unstage", ("files", list, None))
def _git_checkout(params, _emit):   return _git_rpc(params, _emit, "checkout_branch", ("name", str, ""))
def _git_fetch(params, _emit):      return _git_rpc(params, _emit, "fetch", ("remote", str, ""))
def _git_pull(params, _emit):       return _git_rpc(params, _emit, "pull")


def _git_stage(params, _emit):
    return _git_rpc(params, _emit, "stage", ("files", list, None))


def _git_commit(params, _emit):
    return _git_rpc(params, _emit, "commit", ("message", str, ""),
                    ("stage_all", bool, False))


def _git_push(params, _emit):
    return _git_rpc(params, _emit, "push", ("set_upstream", bool, False))


def _git_branch_create(params, _emit):
    return _git_rpc(params, _emit, "create_branch", ("name", str, ""),
                    ("checkout", bool, True))


def _git_repo_info(params, _emit):  return _git_rpc(params, _emit, "repo_info")
def _git_pr_list(params, _emit):    return _git_rpc(params, _emit, "pr_list", ("limit", int, 20))
def _git_issue_list(params, _emit): return _git_rpc(params, _emit, "issue_list", ("limit", int, 20))
def _git_gh_auth_status(params, _emit): return _git_rpc(params, _emit, "gh_auth_status")
def _git_gh_auth_login(params, _emit):  return _git_rpc(params, _emit, "gh_auth_login", ("hostname", str, ""))


WORKER_METHODS = {
    "plm_open": _plm_open,
    "plm_status": _plm_status,
    "plm_history": _plm_history,
    "plm_save": _plm_save,
    "plm_checkpoint": _plm_checkpoint,
    "plm_release": _plm_release,
    "plm_restore": _plm_restore,
    "plm_compare": _plm_compare,
    "plm_item_set": _plm_item_set,
    "plm_item_list": _plm_item_list,
    "plm_item_add": _plm_item_add,
    "plm_item_untrack": _plm_item_untrack,
    "plm_link_repo": _plm_link_repo,
    "plm_where_used": _plm_where_used,
    "plm_set_ai_mode": _plm_set_ai_mode,
    "plm_plan": _plm_plan,
    "plm_overview": _plm_overview,
    "plm_settings_get": _plm_settings_get,
    "plm_settings_set": _plm_settings_set,
    "plm_scheme_set": _plm_scheme_set,
    "plm_discard_draft": _plm_discard_draft,
    "plm_draft": _plm_draft,
    "plm_timeline": _plm_timeline,
    "plm_rollback": _plm_rollback,
    # Git tab — real git / GitHub client
    "git_status": _git_status,
    "git_log": _git_log,
    "git_branches": _git_branches,
    "git_remotes": _git_remotes,
    "git_tree": _git_tree,
    "git_show": _git_show,
    "git_diff": _git_diff,
    "git_is_repo": _git_is_repo,
    "git_init": _git_init,
    "git_stage": _git_stage,
    "git_unstage": _git_unstage,
    "git_commit": _git_commit,
    "git_push": _git_push,
    "git_pull": _git_pull,
    "git_fetch": _git_fetch,
    "git_checkout": _git_checkout,
    "git_branch_create": _git_branch_create,
    "git_repo_info": _git_repo_info,
    "git_pr_list": _git_pr_list,
    "git_issue_list": _git_issue_list,
    "git_gh_auth_status": _git_gh_auth_status,
    "git_gh_auth_login": _git_gh_auth_login,
}

# Lightweight PLM branching (wave-2) — also registered via parts_hub.worker_rpc;
# re-exported here so a plm-only import path still exposes them.
try:
    from engine_oracle.parts_hub.plm_branch_rpc import PLM_BRANCH_METHODS as _PLM_BRANCH_METHODS
    WORKER_METHODS.update(_PLM_BRANCH_METHODS)
except Exception:  # noqa: BLE001 — optional; parts_hub registration is the primary path
    pass
