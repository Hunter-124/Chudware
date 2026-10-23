"""Worker-RPC bridge for the PLM vault — the in-editor GUI seam.

Exposes the ``plm_*`` family as **engine-worker methods** so the C++/ImGui
editor (history timeline panel, checkpoint/release toolbar, compare view,
doc-tab revision badges — ``ui/imgui/src/ui/plm_panel.cpp``) reaches the vault
through the same newline-delimited-JSON worker seam (``WorkerClient`` /
``AsyncRpc``, ``services/worker/``) it already uses for every other service —
no bespoke transport.

These are thin wrappers over :class:`services.plm.vault.Vault` and the change-
intelligence/release modules — the SAME single implementation behind the ``plm_*`` MCP tools
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
  release flow can pre-fill the suggested number.
* ``plm_draft`` — checkpoint/release message drafting for the toolbar. The
  LOCAL model tier phrases a message from the deterministic change
  summary when (and only when) a local, loopback-served model is provisioned;
  otherwise it reports ``source:"manual"`` and the GUI shows a plain manual
  box. Frontier/cloud providers are never used here.
* ``plm_scheme_set`` / ``plm_discard_draft`` — the vault seams
  (configurable part-number scheme; "back to rev X (discard draft)").

Ease-of-use law (contract §8.1): no git vocabulary in any string a user could
see. Registered append-only in ``engine/worker.py``, following the
services.deliverables.worker_rpc / services.parts.worker_rpc / dag.worker_rpc precedent.
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
                     "Restart Chudware to finish setting it up."}


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


# Anti-bulk: loose git objects don't delta-compress until packed. Under frequent
# auto-commits they accumulate, so pack every _REPACK_EVERY commits.
_REPACK_EVERY = 25
_commit_count = 0


def _bump_and_maybe_repack(v: Any) -> None:
    """Count a fresh commit; pack loose objects when the threshold is reached."""
    global _commit_count
    _commit_count += 1
    if _commit_count >= _REPACK_EVERY:
        _commit_count = 0
        try:
            v.repack()
        except Exception:  # noqa: BLE001 — packing never blocks a save
            pass

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
        item_id = (params or {}).get("item_id") or None
        limit = int((params or {}).get("limit") or 0) or None
        scope = str((params or {}).get("scope", "design") or "design")
        commit = str((params or {}).get("commit") or "")
        entries = [v._entry_view_light(commit, item_id=item_id)] if commit else v.history(item_id, limit, scope=scope)
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
                        raw = v._bind.read_blob_at(e["commit"], ref)  # noqa: SLF001
                        if raw:
                            dest.write_bytes(raw)
                    except Exception:  # noqa: BLE001 — thumbnails are best-effort
                        continue
                if dest.exists():
                    e["thumbnail_file"] = str(dest)
        return {"ok": True, "entries": entries, "head": v._bind.head_commit()}
    return _with_vault(params, run)

def _plm_version_graph(params: dict, _emit: Callable) -> dict:
    def run(v: Any) -> dict:
        limit = int((params or {}).get("limit") or 400)
        scope = str((params or {}).get("scope") or "all")
        return {"ok": True, **v.version_graph(limit=limit, scope=scope)}
    return _with_vault({"path": _path(params)}, run)


def _plm_version_tag(params: dict, _emit: Callable) -> dict:
    def run(v: Any) -> dict:
        result = v.tag_version(
            commit=(params or {}).get("commit") or None,
            name=(params or {}).get("name") or None,
            message=str((params or {}).get("message") or ""),
        )
        return {"ok": True, **result}
    return _with_vault({"path": _path(params)}, run)

def _plm_version_list(params: dict, _emit: Callable) -> dict:
    def run(v: Any) -> dict:
        return {"ok": True, "versions": v.list_versions()}
    return _with_vault({"path": _path(params)}, run)

def _plm_save(params: dict, _emit: Callable) -> dict:
    """An explicit save → one auto (or checkpoint) history entry."""
    kind = str((params or {}).get("kind", "auto") or "auto")
    if kind not in ("auto", "checkpoint"):
        return {"ok": False, "reason": "invalid_mode",
                "hint": "a save can only be a save or a named checkpoint"}

    def run(v: Any) -> dict:
        res = v.save(kind)
        _bump_and_maybe_repack(v)
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
    return _with_vault(params, run)


def _plm_checkpoint(params: dict, _emit: Callable) -> dict:
    """Save a named checkpoint the user can return to."""
    def run(v: Any) -> dict:
        res = v.checkpoint(str((params or {}).get("message", "") or ""),
                           item_id=(params or {}).get("item_id") or None)
        _bump_and_maybe_repack(v)
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
    return _with_vault(params, run)


def _plm_release(params: dict, _emit: Callable) -> dict:
    """Release an item: pre-flight, rev stamp, geometry + BOM freeze."""
    def run(v: Any) -> dict:
        from services.plm import release as release_mod

        res = release_mod.release(v, str((params or {}).get("item_id", "")),
                                  str((params or {}).get("message", "") or ""),
                                  confirm=bool((params or {}).get("confirm")))
        return {"ok": True, "commit": res["commit"], "rev": res["rev"],
                "entry": res["entry"], "preflight": res["preflight"],
                "bom_freeze": res.get("bom_freeze"),
                "geometry_frozen": bool(res.get("geometry_frozen")),
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
    """List registered items and discover working containers not yet checked in.

    ``change`` compares each working item with the vault's current history
    snapshot; discovery never registers or saves a file.
    """
    def run(v: Any) -> dict:
        from services.plm import items as items_mod

        m = v.manifest()
        want_dt = str((params or {}).get("doc_type", "") or "")
        want_kind = str((params or {}).get("kind", "") or "")
        dirty_ids, changes, untracked = v.working_changes(
            m, doc_type=want_dt, kind=want_kind)
        dirty = set(dirty_ids)
        out: list[dict[str, Any]] = []
        for it in m["items"]:
            if want_dt and it.get("doc_type") != want_dt:
                continue
            if want_kind and it.get("kind") != want_kind:
                continue
            view = items_mod.item_view(it)
            view["dirty"] = it["item_id"] in dirty
            view["change"] = changes[it["item_id"]]
            out.append(view)
        return {"ok": True, "items": out, "untracked": untracked}
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
    """Per-machine vault-local settings + the project auto-record flag.

    Also surfaces ``ai_mode`` and bridge enablement hints so Settings can show
    that Managed mode turns on AI→vault checkpoints without CHUDWARE_PLM_BRIDGE.
    Precedence: managed ai_mode > CHUDWARE_PLM_BRIDGE env > off.
    """
    def run(v: Any) -> dict:
        mode = v.ai_mode()
        env_on = os.environ.get("CHUDWARE_PLM_BRIDGE", "").strip() == "1"
        return {
            "ok": True,
            "settings": v.get_local_config(),
            "auto_commit": v.auto_commit(),
            "ai_mode": mode,
            # Additive hint fields (UI may ignore): bridge is live when managed
            # OR the env flag is set. AI-authored cuts still require managed.
            "bridge_env": env_on,
            "bridge_active": (mode == "managed") or env_on,
        }
    return _with_vault(params, run)


def _plm_settings_set(params: dict, _emit: Callable) -> dict:
    """Merge a patch into the vault-local settings; set ``auto_commit`` when given."""
    def run(v: Any) -> dict:
        p = params or {}
        if "auto_commit" in p:
            v.set_auto_commit(bool(p.get("auto_commit")))
        patch = p.get("settings")
        settings = v.get_local_config()
        if patch is not None:
            if not isinstance(patch, dict):
                return {"ok": False, "reason": "invalid_mode",
                        "hint": "settings must be an object"}
            settings = v.set_local_config(patch)
        return {"ok": True, "settings": settings, "auto_commit": v.auto_commit()}
    return _with_vault(params, run)


def _plm_record_outside(params: dict, _emit: Callable) -> dict:
    """Capture on-disk changes made outside the editor (e.g. files copied in, an
    external tool re-saved a part) as one auto history entry. No-op when the
    working tree already matches history — returns ``committed:false``."""
    def run(v: Any) -> dict:
        m = v.manifest()
        tracked = {it["path"] for it in m.get("items", [])}
        # A clean no-op is only trustworthy after both discovery and dirty
        # comparison complete. Never reinterpret an unreadable project as empty.
        try:
            on_disk = {p.relative_to(v.project_dir).as_posix()
                       for p in v._project_containers()}
            dirty = v._dirty_items(m)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "reason": "scan_failed",
                "hint": f"could not scan project changes: {type(exc).__name__}: {exc}",
            }
        new_containers = on_disk - tracked
        if not dirty and not new_containers:
            return {"ok": True, "committed": False}
        res = v.save("auto")
        _bump_and_maybe_repack(v)
        return {"ok": True, "committed": True,
                "commit": res["commit"], "entry": res["entry"]}
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
    project_dir = ((params or {}).get("path")
                   or (params or {}).get("project_dir"))
    try:
        store = ai_vc.resolve_store(project_dir)
        return ai_vc.plm_timeline(doc_id, store=store)
    except ai_vc.StoreResolutionError as e:
        return e.envelope()
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
    project_dir = ((params or {}).get("path")
                   or (params or {}).get("project_dir"))
    try:
        store = ai_vc.resolve_store(project_dir)
        return ai_vc.plm_rollback(doc_id, to, store=store)
    except ai_vc.StoreResolutionError as e:
        return e.envelope()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(e).__name__}: {e}"}


def _plm_rollback_check(params: dict, _emit: Callable) -> dict:
    """AI-VC rollback preview: the confirm-dialog warning ladder, non-destructive.

    Params: ``doc_id?``, ``to``, ``file_path?`` (doc's backing file, fallback
    when the checkpoint predates file provenance), ``chat_size?`` (for the
    discarded-messages count). Shape: ``{ok, discarded_checkpoints,
    discarded_turns, discarded_messages, geometry_rollback, chat_truncate_to,
    file:{path, provenance_known, externally_modified, ...}}``.
    """
    try:
        from services.plm import ai_vc
    except ImportError:
        return dict(_NO_VAULT)
    doc_id = (params or {}).get("doc_id") or None
    if doc_id is not None:
        doc_id = str(doc_id) or None
    to = str((params or {}).get("to", "") or "")
    file_path = str((params or {}).get("file_path") or "") or None
    chat_size = (params or {}).get("chat_size")
    project_dir = ((params or {}).get("path")
                   or (params or {}).get("project_dir"))
    try:
        store = ai_vc.resolve_store(project_dir)
    except ai_vc.StoreResolutionError as e:
        return e.envelope()
    try:
        chat_size = int(chat_size) if chat_size is not None else None
    except (TypeError, ValueError):
        chat_size = None
    try:
        return ai_vc.plm_rollback_check(doc_id, to, store=store,
                                        file_path=file_path, chat_size=chat_size)
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
            od = od or tempfile.mkdtemp(prefix="chudware_plm_compare_")
            raw_views = (params or {}).get("views")
            views = ([str(x) for x in raw_views]
                     if isinstance(raw_views, (list, tuple)) and raw_views else None)
            # annotate=false gives clean images for a caller that draws the view
            # name, legend and dimensions as real widgets (the GUI). Agents keep
            # the default so a bare PNG still describes itself.
            annotate = bool((params or {}).get("annotate", True))
            try:
                out["eval_compare"] = compare_mod.visual_compare(
                    doc_a, doc_b, od, views, annotate)
            except Exception as e:  # noqa: BLE001 — visual half degrades gracefully
                out["visual_unavailable"] = f"{type(e).__name__}: {e}"
            out["out_dir"] = od
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
# Vault seams: scheme config + discard draft
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
# plm_draft — LOCAL model tier message drafting
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
    if os.environ.get("CHUDWARE_PLM_DRAFT_LOCAL", "1") == "0":
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
    deterministic change summary) or ``"manual"`` (no local model — the
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



# ---------------------------------------------------------------------------
# Remotes / locks (services.plm.remote)
# ---------------------------------------------------------------------------

def _plm_add_remote(params: dict, _emit: Callable) -> dict:
    """Register a remote profile (folder/ssh/https/chudware-cloud config)."""
    try:
        from services.plm import remote as remote_mod
    except ImportError:
        return dict(_NO_VAULT)
    p = params or {}
    path = str(p.get("path", "") or "")
    kind = str(p.get("kind", "folder") or "folder")
    name = str(p.get("name", "origin") or "origin")
    url = p.get("url")
    if url is None:
        url = p.get("remote_path") or p.get("remote_url")
    return remote_mod.add_remote(
        path,
        name,
        path=url,
        kind=kind,
        url=url,
        display_name=p.get("display_name"),
        account_id=p.get("account_id"),
    )


def _live_participant(params: dict) -> dict[str, Any] | None:
    value = (params or {}).get("participant")
    if value is not None:
        return value if isinstance(value, dict) else {"id": "", "display": "", "account_id": None}
    participant_id = str((params or {}).get("participant_id") or "").strip()
    display = str(
        (params or {}).get("participant_display")
        or (params or {}).get("display_name")
        or ""
    ).strip()
    if not participant_id and not display:
        return None
    return {
        "id": participant_id,
        "display": display,
        "account_id": (params or {}).get("account_id") or None,
    }


def _live_call(fn: Callable[[], dict]) -> dict:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — worker RPCs are plain envelopes
        try:
            from services.plm.live_client import LiveClientError

            if isinstance(exc, LiveClientError):
                return exc.envelope()
        except ImportError:
            pass
        return {
            "ok": False,
            "reason": "internal",
            "hint": "The live-session request could not be completed.",
        }


def _live_route(params: dict) -> tuple[Any, str, str, str, dict[str, Any] | None]:
    from services.plm.live_client import get_live_manager

    p = params or {}
    return (
        get_live_manager(),
        str(p.get("path") or p.get("project_dir") or ""),
        str(p.get("remote_id") or p.get("remote") or "origin"),
        str(p.get("item_id") or p.get("item") or ""),
        _live_participant(p),
    )


def _plm_live_join(params: dict, _emit: Callable) -> dict:
    def run() -> dict:
        manager, path, remote_id, item_id, participant = _live_route(params)
        p = params or {}
        return manager.join(
            path,
            remote_id,
            item_id,
            participant,
            token=p.get("token"),
            local_doc_id=p.get("local_doc_id") or None,
        )

    return _live_call(run)


def _plm_live_propose(params: dict, _emit: Callable) -> dict:
    def run() -> dict:
        manager, path, remote_id, item_id, participant = _live_route(params)
        p = params or {}
        return manager.propose(
            path,
            remote_id,
            item_id,
            participant,
            p.get("ops"),
            p.get("client_op_id") or None,
        )

    return _live_call(run)


def _plm_live_poll(params: dict, _emit: Callable) -> dict:
    def run() -> dict:
        manager, path, remote_id, item_id, participant = _live_route(params)
        return manager.poll(
            path,
            remote_id,
            item_id,
            participant,
            int((params or {}).get("limit") or 100),
        )

    return _live_call(run)


def _plm_live_presence(params: dict, _emit: Callable) -> dict:
    def run() -> dict:
        manager, path, remote_id, item_id, participant = _live_route(params)
        p = params or {}
        return manager.presence(
            path,
            remote_id,
            item_id,
            participant,
            cursor=p.get("cursor"),
            selection=p.get("selection"),
            viewport=p.get("viewport"),
        )

    return _live_call(run)


def _plm_live_leave(params: dict, _emit: Callable) -> dict:
    def run() -> dict:
        manager, path, remote_id, item_id, participant = _live_route(params)
        return manager.leave(path, remote_id, item_id, participant)

    return _live_call(run)


def _plm_sync(params: dict, _emit: Callable) -> dict:
    """One-button sync against a folder remote (fetch + FF / CAS push)."""
    try:
        from services.plm import remote as remote_mod
    except ImportError:
        return dict(_NO_VAULT)
    path = str((params or {}).get("path", "") or "")
    remote = str(
        (params or {}).get("remote_id")
        or (params or {}).get("remote")
        or "origin"
    )
    return remote_mod.sync(path, remote=remote)


def _plm_lock(params: dict, _emit: Callable) -> dict:
    """Advisory lock via CAS on refs/aicad/locks."""
    try:
        from services.plm import remote as remote_mod
    except ImportError:
        return dict(_NO_VAULT)
    p = params or {}
    path = str(p.get("path", "") or "")
    item_id = str(p.get("item_id", "") or "")
    remote = str(p.get("remote_id") or p.get("remote") or "origin")
    return remote_mod.plm_lock(
        path, item_id, remote=remote, force=bool(p.get("force", False))
    )


def _plm_unlock(params: dict, _emit: Callable) -> dict:
    """Release an advisory lock via CAS."""
    try:
        from services.plm import remote as remote_mod
    except ImportError:
        return dict(_NO_VAULT)
    path = str((params or {}).get("path", "") or "")
    item_id = str((params or {}).get("item_id", "") or "")
    remote = str(
        (params or {}).get("remote_id")
        or (params or {}).get("remote")
        or "origin"
    )
    return remote_mod.plm_unlock(path, item_id, remote=remote)


# ---- Box.com mirror (services/plm/box_mirror.py) ---------------------------

_NO_BOX = {"ok": False, "reason": "no_box",
           "hint": "the Box mirror is not available on this machine"}


def _box_status(params: dict, _emit: Callable) -> dict:
    """Box mirror status for the Settings → Integrations surface."""
    try:
        from services.plm import box_mirror
    except ImportError:
        return dict(_NO_BOX)
    path = str((params or {}).get("path", "") or "") or None
    try:
        return box_mirror.status(path, live=bool((params or {}).get("live")))
    except Exception as e:  # noqa: BLE001 — status must always answer
        return {"ok": False, "reason": "internal", "hint": f"{type(e).__name__}: {e}"}


def _box_sync(params: dict, _emit: Callable) -> dict:
    """Mirror a project's saved files up to Box now (GUI 'Sync now')."""
    try:
        from services.plm import box_mirror
    except ImportError:
        return dict(_NO_BOX)
    path = str((params or {}).get("path", "") or "")
    if not path:
        return {"ok": False, "reason": "bad_args",
                "hint": "path (the project root) is required"}
    try:
        return box_mirror.sync_project(path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal", "hint": f"{type(e).__name__}: {e}"}


def _box_notify_save(params: dict, _emit: Callable) -> dict:
    """A document was saved from the GUI — spool its project, drain async."""
    try:
        from services.plm import box_mirror
    except ImportError:
        return dict(_NO_BOX)
    box_mirror.notify_saved_path(str((params or {}).get("path", "") or ""))
    return {"ok": True}


def _box_oauth_login(params: dict, _emit: Callable) -> dict:
    """Begin the loopback OAuth connect flow; returns ``{ok, authorize_url}``."""
    try:
        from services.plm import box_mirror
    except ImportError:
        return dict(_NO_BOX)
    try:
        return box_mirror.oauth_begin()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "internal", "hint": f"{type(e).__name__}: {e}"}


WORKER_METHODS = {
    "plm_open": _plm_open,
    "plm_status": _plm_status,
    "plm_history": _plm_history,
    "plm_version_graph": _plm_version_graph,
    "plm_version_tag": _plm_version_tag,
    "plm_version_list": _plm_version_list,
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
    "plm_record_outside": _plm_record_outside,
    "plm_scheme_set": _plm_scheme_set,
    "plm_discard_draft": _plm_discard_draft,
    "plm_draft": _plm_draft,
    "plm_timeline": _plm_timeline,
    "plm_rollback": _plm_rollback,
    "plm_rollback_check": _plm_rollback_check,
    "plm_add_remote": _plm_add_remote,
    "plm_sync": _plm_sync,
    "plm_lock": _plm_lock,
    "plm_unlock": _plm_unlock,
    "plm_live_join": _plm_live_join,
    "plm_live_propose": _plm_live_propose,
    "plm_live_poll": _plm_live_poll,
    "plm_live_presence": _plm_live_presence,
    "plm_live_leave": _plm_live_leave,
    # Box.com mirror — Settings → Integrations + post-save notification
    "box_status": _box_status,
    "box_sync": _box_sync,
    "box_notify_save": _box_notify_save,
    "box_oauth_login": _box_oauth_login,
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


# ──── CAD project graph (contracts/cad_graph.md v1) ────
# Folded in from engine_oracle/parts_hub/cad_graph_rpc.py — a squatter
# with zero parts coupling; every function is a thin wrapper over services.plm.*.


def _cad_graph_get(params: dict, _emit: Callable) -> dict:
    """``cad_graph_get {project_dir?|doc_id?} -> {ok, nodes, edges, generated_at, …}``.

    Additive optional params (contracts/cad_graph.md addenda):
      force, include_archive, max_nodes, max_edges, open_docs.
    Response always includes node_count/edge_count/truncated/filter.
    """
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or None
        doc_id = p.get("doc_id") or None
        force = bool(p.get("force", False))
        include_archive = bool(p.get("include_archive", False))
        open_docs = p.get("open_docs") if isinstance(p.get("open_docs"), dict) else None
        max_nodes = p.get("max_nodes", None)
        max_edges = p.get("max_edges", None)
        if max_nodes is not None:
            try:
                max_nodes = int(max_nodes)
            except (TypeError, ValueError):
                max_nodes = None
        if max_edges is not None:
            try:
                max_edges = int(max_edges)
            except (TypeError, ValueError):
                max_edges = None
        # Change-probe pass-through (contracts/cad_graph.md 2026-07 addendum).
        if_mtimes_max = p.get("if_mtimes_max", None)
        if if_mtimes_max is not None:
            try:
                if_mtimes_max = float(if_mtimes_max)
            except (TypeError, ValueError):
                if_mtimes_max = None
        if_sources_count = p.get("if_sources_count", None)
        if if_sources_count is not None:
            try:
                if_sources_count = int(if_sources_count)
            except (TypeError, ValueError):
                if_sources_count = None
        return cg.get_graph(
            project_dir, doc_id=doc_id, open_docs=open_docs, force=force,
            include_archive=include_archive,
            max_nodes=max_nodes, max_edges=max_edges,
            if_mtimes_max=if_mtimes_max,
            if_sources_count=if_sources_count,
            linked=bool(p.get("linked", False)),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_get failed: {exc}", "hint": ""}


def _cad_graph_blast(params: dict, _emit: Callable) -> dict:
    """``cad_graph_blast {node_id, project_dir?} -> {ok, affected, edges}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        node_id = str(p.get("node_id") or "").strip()
        if not node_id:
            return {"ok": False, "reason": "node_id required",
                    "hint": "pass the graph node id to blast from",
                    "affected": [], "edges": []}
        project_dir = p.get("project_dir") or p.get("path") or None
        force = bool(p.get("force", False))
        graph = None
        if project_dir:
            graph = cg.build_graph(project_dir, force=force)
        return cg.blast_radius(node_id, graph, project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_blast failed: {exc}",
                "hint": "", "affected": [], "edges": []}


def _cad_graph_path(params: dict, _emit: Callable) -> dict:
    """``cad_graph_path {src, dst, project_dir?} -> {ok, path}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        src = str(p.get("src") or "").strip()
        dst = str(p.get("dst") or "").strip()
        if not src or not dst:
            return {"ok": False, "reason": "src and dst required",
                    "hint": "pass graph node ids for both ends", "path": []}
        project_dir = p.get("project_dir") or p.get("path") or None
        force = bool(p.get("force", False))
        graph = None
        if project_dir:
            graph = cg.build_graph(project_dir, force=force)
        return cg.find_path(src, dst, graph, project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_path failed: {exc}",
                "hint": "", "path": []}


def _cad_graph_link_add(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_add {project_dir, src, dst, kind, note?, origin?}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return gl.add_link(
            project_dir,
            str(p.get("src") or ""), str(p.get("dst") or ""),
            str(p.get("kind") or ""),
            note=str(p.get("note") or ""),
            origin=str(p.get("origin") or "manual"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_add failed: {exc}", "hint": ""}


def _cad_graph_link_remove(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_remove {project_dir, link_id}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return gl.remove_link(project_dir, str(p.get("link_id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_remove failed: {exc}", "hint": ""}


def _cad_graph_link_list(params: dict, _emit: Callable) -> dict:
    """``cad_graph_link_list {project_dir} -> {ok, links}``."""
    try:
        from services.plm import graph_links as gl
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_links unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or ""
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": "", "links": []}
        return {"ok": True, "links": gl.list_links(project_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_link_list failed: {exc}",
                "hint": "", "links": []}


def _cad_graph_assembly_tree(params: dict, _emit: Callable) -> dict:
    """``cad_graph_assembly_tree {project_dir, doc_id?, max_depth?} -> {ok, trees}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = p.get("project_dir") or p.get("path") or None
        doc_id = p.get("doc_id") or None
        include_archive = bool(p.get("include_archive", False))
        max_depth = p.get("max_depth", 12)
        try:
            max_depth = max(1, int(max_depth))
        except (TypeError, ValueError):
            max_depth = 12
        return cg.assembly_tree(project_dir, doc_id=doc_id,
                                include_archive=include_archive,
                                max_depth=max_depth)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_assembly_tree failed: {exc}",
                "hint": "", "trees": []}


# ──── keyword / watch / search / config (contracts/cad_graph.md 2026-07) ────


def _require_project_dir(p: dict) -> str:
    return str(p.get("project_dir") or p.get("path") or "")


def _cad_graph_keyword_add(params: dict, _emit: Callable) -> dict:
    """``cad_graph_keyword_add {project_dir, target, keyword, origin?}``."""
    try:
        from services.plm import keywords as kw
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"keywords unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return kw.add_keyword(
            project_dir, str(p.get("target") or ""),
            str(p.get("keyword") or ""),
            origin=str(p.get("origin") or "manual"),
            author=p.get("author") if isinstance(p.get("author"), dict) else None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_keyword_add failed: {exc}",
                "hint": ""}


def _cad_graph_keyword_remove(params: dict, _emit: Callable) -> dict:
    """``cad_graph_keyword_remove {project_dir, entry_id?|target+keyword}``."""
    try:
        from services.plm import keywords as kw
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"keywords unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        entry_id = str(p.get("entry_id") or "").strip()
        if entry_id:
            return kw.remove_keyword(project_dir, entry_id)
        target = str(p.get("target") or "").strip()
        keyword = str(p.get("keyword") or "").strip()
        if not target or not keyword:
            return {"ok": False, "reason": "entry_id or target+keyword required",
                    "hint": "pass entry_id, or both target and keyword"}
        return kw.remove_pair(project_dir, target, keyword)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_keyword_remove failed: {exc}",
                "hint": ""}


def _cad_graph_keyword_list(params: dict, _emit: Callable) -> dict:
    """``cad_graph_keyword_list {project_dir, target?} -> {ok, entries}``."""
    try:
        from services.plm import keywords as kw
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"keywords unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required",
                    "hint": "", "entries": []}
        entries = kw.list_entries(project_dir)
        target = str(p.get("target") or "").strip()
        if target:
            entries = [e for e in entries if e.get("target") == target]
        return {"ok": True, "entries": entries}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_keyword_list failed: {exc}",
                "hint": "", "entries": []}


def _graph_for_query(p: dict):
    """Shared loader: graph dict honoring the optional ``linked`` flag.

    Returns ``(project_dir, None)`` when no project_dir was given — callers
    must turn that into the flat ``project_dir required`` envelope.
    """
    from services.plm import cad_graph as cg
    project_dir = _require_project_dir(p)
    if not project_dir:
        return "", None
    force = bool(p.get("force", False))
    include_archive = bool(p.get("include_archive", False))
    g = cg.get_graph(project_dir, force=force,
                     include_archive=include_archive,
                     max_nodes=0, max_edges=0,
                     linked=bool(p.get("linked", False)))
    return project_dir, g


def _cad_graph_search(params: dict, _emit: Callable) -> dict:
    """``cad_graph_search {project_dir, q?, kinds?, keyword?, linked?}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        _project_dir, g = _graph_for_query(p)
        if g is None:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        kinds = p.get("kinds")
        if isinstance(kinds, str):
            kinds = [k.strip() for k in kinds.split(",") if k.strip()]
        elif not isinstance(kinds, list):
            kinds = None
        out = cg.search_graph(g, q=str(p.get("q") or ""), kinds=kinds,
                              keyword=str(p.get("keyword") or ""))
        out["nodes"] = out["nodes"][:400]
        keep = {n["id"] for n in out["nodes"]}
        out["edges"] = [e for e in out["edges"]
                        if e.get("src") in keep and e.get("dst") in keep][:400]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_search failed: {exc}",
                "hint": ""}


def _cad_graph_neighbors(params: dict, _emit: Callable) -> dict:
    """``cad_graph_neighbors {project_dir, node_id, depth?, linked?}``."""
    try:
        from services.plm import cad_graph as cg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        node_id = str(p.get("node_id") or "").strip()
        if not node_id:
            return {"ok": False, "reason": "node_id required", "hint": ""}
        _project_dir, g = _graph_for_query(p)
        if g is None:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        try:
            depth = int(p.get("depth", 1))
        except (TypeError, ValueError):
            depth = 1
        return cg.neighbors(g, node_id, depth)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_neighbors failed: {exc}",
                "hint": ""}


def _cad_graph_watch_add(params: dict, _emit: Callable) -> dict:
    """``cad_graph_watch_add {project_dir, keyword?|node_id?, note?, origin?}``."""
    try:
        from services.plm import watch as w
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"watch unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return w.watch_add(
            project_dir, keyword=str(p.get("keyword") or ""),
            node_id=str(p.get("node_id") or ""),
            note=str(p.get("note") or ""),
            origin=str(p.get("origin") or "manual"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_watch_add failed: {exc}",
                "hint": ""}


def _cad_graph_watch_remove(params: dict, _emit: Callable) -> dict:
    """``cad_graph_watch_remove {project_dir, entry_id}``."""
    try:
        from services.plm import watch as w
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"watch unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return w.watch_remove(project_dir, str(p.get("entry_id") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_watch_remove failed: {exc}",
                "hint": ""}


def _cad_graph_watch_list(params: dict, _emit: Callable) -> dict:
    """``cad_graph_watch_list {project_dir} -> {ok, watch}``."""
    try:
        from services.plm import watch as w
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"watch unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required",
                    "hint": "", "watch": []}
        return {"ok": True, "watch": w.list_watch(project_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_watch_list failed: {exc}",
                "hint": "", "watch": []}


def _cad_graph_changes(params: dict, _emit: Callable) -> dict:
    """``cad_graph_changes {project_dir, linked?} -> diff vs cached graph``.

    Diffs the on-disk cached graph (pre-rebuild) against a fresh build and
    annotates added/removed nodes with ``watched: true`` when they match a
    watch-overlay entry (keyword or node id).
    """
    try:
        from services.plm import cad_graph as cg
        from services.plm import watch as w
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph unavailable: {exc}", "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        old = cg.load_cached_graph(project_dir) or {"nodes": [], "edges": []}
        linked = bool(p.get("linked", False))
        new = cg.get_graph(project_dir, force=True, max_nodes=0, max_edges=0,
                           linked=linked)
        diff = cg.graph_diff(old, new)
        watch_entries = w.list_watch(project_dir)
        matched = w.watched_matches(new, watch_entries) if watch_entries else set()
        matched_old = w.watched_matches(old, watch_entries) if watch_entries else set()
        for n in diff["nodes_added"]:
            if n.get("id") in matched:
                n["watched"] = True
        for n in diff["nodes_removed"]:
            if n.get("id") in matched_old:
                n["watched"] = True
        watched_changed = sorted({
            n["id"] for n in diff["nodes_added"] + diff["nodes_removed"]
            if n.get("watched")})
        diff["watched"] = watched_changed
        diff["generated_at"] = new.get("generated_at")
        return diff
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_changes failed: {exc}",
                "hint": ""}


def _cad_graph_config_get(params: dict, _emit: Callable) -> dict:
    """``cad_graph_config_get {project_dir} -> {ok, config, project_key}``."""
    try:
        from services.plm import graph_config as gcfg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_config unavailable: {exc}",
                "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        return {"ok": True, "config": gcfg.get_config(project_dir),
                "project_key": gcfg.project_key(project_dir)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_config_get failed: {exc}",
                "hint": ""}


def _cad_graph_config_set(params: dict, _emit: Callable) -> dict:
    """``cad_graph_config_set {project_dir, fields...}`` — validates via module."""
    try:
        from services.plm import graph_config as gcfg
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"graph_config unavailable: {exc}",
                "hint": ""}
    try:
        p = params or {}
        project_dir = _require_project_dir(p)
        if not project_dir:
            return {"ok": False, "reason": "project_dir required", "hint": ""}
        fields = {k: v for k, v in p.items()
                  if k in ("office_content", "office_max_chars", "office_kinds",
                           "linked_projects", "project_key")}
        if not fields:
            return {"ok": False, "reason": "no config fields given",
                    "hint": "office_content|office_max_chars|office_kinds|"
                            "linked_projects|project_key"}
        return gcfg.set_config(project_dir, **fields)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cad_graph_config_set failed: {exc}",
                "hint": ""}


CAD_GRAPH_METHODS: dict[str, Callable[..., Any]] = {
    "cad_graph_get": _cad_graph_get,
    "cad_graph_blast": _cad_graph_blast,
    "cad_graph_path": _cad_graph_path,
    "cad_graph_link_add": _cad_graph_link_add,
    "cad_graph_link_remove": _cad_graph_link_remove,
    "cad_graph_link_list": _cad_graph_link_list,
    "cad_graph_assembly_tree": _cad_graph_assembly_tree,
    "cad_graph_keyword_add": _cad_graph_keyword_add,
    "cad_graph_keyword_remove": _cad_graph_keyword_remove,
    "cad_graph_keyword_list": _cad_graph_keyword_list,
    "cad_graph_search": _cad_graph_search,
    "cad_graph_neighbors": _cad_graph_neighbors,
    "cad_graph_watch_add": _cad_graph_watch_add,
    "cad_graph_watch_remove": _cad_graph_watch_remove,
    "cad_graph_watch_list": _cad_graph_watch_list,
    "cad_graph_changes": _cad_graph_changes,
    "cad_graph_config_get": _cad_graph_config_get,
    "cad_graph_config_set": _cad_graph_config_set,
}


# ──── Lightweight PLM branching (wave-2) ────
# Folded in from engine_oracle/parts_hub/plm_branch_rpc.py. This is now
# the single registration path (the parts_hub copy is deleted).


def _path(params: dict | None) -> str:
    p = params or {}
    return str(p.get("path") or p.get("project_dir") or "").strip()


def _plm_branch_create(params: dict, _emit: Callable) -> dict:
    """Create a named branch from HEAD (or ``from`` branch/commit)."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    name = str((params or {}).get("name") or "").strip()
    from_ref = (params or {}).get("from") or (params or {}).get("from_ref") or None
    if from_ref is not None:
        from_ref = str(from_ref).strip() or None
    try:
        return branches.branch_create(path, name, from_ref=from_ref)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_list(params: dict, _emit: Callable) -> dict:
    """List project branches + the active one."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    try:
        return branches.branch_list(path)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_switch(params: dict, _emit: Callable) -> dict:
    """Switch the active branch and materialize its working files."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    name = str((params or {}).get("name") or "").strip()
    try:
        return branches.branch_switch(path, name)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


def _plm_branch_merge(params: dict, _emit: Callable) -> dict:
    """Merge *src* into *dst*. Returns structured conflicts (never silent)."""
    try:
        from services.plm import branches
        from services.plm.vault import VaultError
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"branches unavailable: {exc}", "hint": ""}
    path = _path(params)
    if not path:
        return {"ok": False, "reason": "invalid_project",
                "hint": "no project folder given"}
    src = str((params or {}).get("src") or "").strip()
    dst = str((params or {}).get("dst") or "").strip()
    message = str((params or {}).get("message") or "")
    if not src or not dst:
        return {"ok": False, "reason": "invalid_mode",
                "hint": "both src and dst branch names are required"}
    try:
        return branches.branch_merge(path, src, dst, message=message)
    except VaultError as e:
        return {"ok": False, "reason": e.reason, "hint": e.hint}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal",
                "hint": f"{type(exc).__name__}: {exc}"}


PLM_BRANCH_METHODS: dict[str, Callable[..., Any]] = {
    "plm_branch_create": _plm_branch_create,
    "plm_branch_list": _plm_branch_list,
    "plm_branch_switch": _plm_branch_switch,
    "plm_branch_merge": _plm_branch_merge,
}


WORKER_METHODS.update(CAD_GRAPH_METHODS)
WORKER_METHODS.update(PLM_BRANCH_METHODS)
