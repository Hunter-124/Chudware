"""services.plm.vault — the PLM vault engine (contract §2–§5, aicad.plm/1).

The storage engine behind Pillar 19. A *project* is a folder; the *vault* is a
hidden git repo at ``.aicadvault/`` (contract §2). This class ties the pieces
together:

  * :mod:`gitbind`   — the dulwich-primary binding (linear, append-only)
  * :mod:`snapshot`  — explode/re-assemble ``.step+`` / ``.ecad+`` containers
  * :mod:`manifest`  — the aicad.plm/1 registry (items, revs, lifecycle)
  * :mod:`history`   — entry kinds + per-item meta.json (§3.4)

Ease-of-use law (contract §8): zero-config init (open == create if absent), no
git vocabulary surfaced, history never rewritten (restore = new forward entry).

Every public method returns plain Python; the ``plm_*`` MCP tools + the §7.1
``PLMVaultClient`` wrap these in the ``{ok, …}`` envelope.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from services.plm import assist, doctypes, generic_snapshot, gitbind, manifest, snapshot
from services.plm import history as hist  # aliased: Vault.history() method shadows the name
from services.plm import items as items_mod
from services.plm import permissions, planning, summarize

# document containers we version (contract §2)
_CONTAINER_SUFFIXES = (".step+", ".ecad+")

# versioned shadow-tree top-level dirs the container explode does not itself
# own but that MUST survive every whole-tree commit (carry-forward law). The
# planning store (aicad.planning/1) lives under planning/ and is written via a
# dedicated path; without carry-forward it would drop out of any container save.
_CARRY_FORWARD_TOPLEVEL = frozenset({"planning"})

# doc-kind inference from container extension (best-effort; refined by document.json)
_SUFFIX_KIND = {".step+": "part", ".ecad+": "ecad"}

# authoritative doc-kind from the document's schema string (T-R4: drawings and
# CAM jobs share the .step+ container, so the suffix alone is not enough)
_SCHEMA_KIND = {
    "aicad.part": "part",
    "aicad.asm": "assembly",
    "aicad.drawing": "drawing",
    "aicad.pcb": "ecad",
    "aicad.sch": "ecad",
    "aicad.cam": "cam",
    "aicad.req": "requirements",
}


def _kind_from_schema(schema: str) -> str | None:
    return _SCHEMA_KIND.get(str(schema).split("/", 1)[0])


class VaultError(Exception):
    """Raised for vault operations that fail with a contract reason code."""

    def __init__(self, reason: str, hint: str = "") -> None:
        super().__init__(hint or reason)
        self.reason = reason
        self.hint = hint


class Vault:
    """An open project vault (contract §2)."""

    def __init__(self, project_dir: str | os.PathLike, binding: gitbind.GitBinding) -> None:
        self.project_dir = Path(project_dir)
        self._bind = binding

    # -- lifecycle (contract §8.2 zero-config) -----------------------------

    @classmethod
    def open(cls, project_dir: str | os.PathLike) -> "Vault":
        """Open a project folder, silently creating the vault if absent.

        Zero-config: no prompt, no setup, no visible files beyond ``.aicadvault/``.
        On first open, existing container files in the folder are registered.
        """
        pdir = Path(project_dir)
        if not pdir.is_dir():
            raise VaultError("invalid_project", f"not a folder: {project_dir}")
        fresh = not gitbind.GitBinding.exists(pdir)
        binding = gitbind.open_binding(pdir)
        v = cls(pdir, binding)
        if fresh:
            v._seed_initial_project()
        return v

    @classmethod
    def exists(cls, project_dir: str | os.PathLike) -> bool:
        return gitbind.GitBinding.exists(project_dir)

    def close(self) -> None:
        self._bind.close()

    # -- reading -----------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        """Return the current project manifest (from HEAD, or a fresh empty one)."""
        head = self._bind.head_commit()
        if head is None:
            return manifest.new_manifest(self.project_dir.name, _now_iso())
        raw = self._bind.read_blob_at(head, "manifest.json")
        if raw is None:  # corrupted/empty — degrade gracefully (DoD)
            return manifest.new_manifest(self.project_dir.name, _now_iso())
        return manifest.loads(raw)

    def status(self) -> dict[str, Any]:
        """Project state for the Hub/badges (contract §7 plm_status shape)."""
        m = self.manifest()
        head = self._bind.head_commit()
        return {
            "project_id": m["project"]["id"],
            "head": head,
            "dirty": self._dirty_items(m),
            "ai_mode": m.get("ai_mode", "assisted"),
            "auto_commit": self.auto_commit(),
            "items": [self._item_view(it) for it in m["items"]],
            "remotes": m.get("remotes", []),
            "locks": [],  # lock manifest lives on refs/aicad/locks (T-R7)
        }

    def history(self, item_id: str | None = None, limit: int | None = None,
                scope: str = hist.SCOPE_DESIGN) -> list[dict[str, Any]]:
        """Return the timeline (filtered per item if given) — contract §7 plm_history.

        ``scope`` filters by entry scope: ``design`` (default — the curated
        timeline, hiding planning-store churn), ``planning`` (only task/issue/
        milestone changes), or ``all``.
        """
        entries: list[dict[str, Any]] = []
        prev_sig: object | None = None
        first = True
        commits = self._bind.log()
        for commit in commits:
            if scope != "all":
                cmeta = self._bind.commit_meta(commit)
                if hist.scope_from_commit_message(cmeta["message"]) != scope:
                    continue
            tree = self._bind.read_tree(commit)
            # per-item filter: include only commits where this item's content
            # changed. CAD items key on document.json; generic (non-container)
            # items key on their whole content-blob set (no document.json).
            if item_id is not None:
                cur = self._item_content_signature(tree, item_id)
                if not first and cur == prev_sig:
                    prev_sig = cur
                    continue
                prev_sig = cur
                first = False
            entries.append(self._entry_view(commit, tree, item_id))
            if limit is not None and len(entries) >= limit:
                break
        return entries

    def _item_content_signature(self, tree: dict[str, bytes], item_id: str) -> object:
        """A change signature for one item at a history point — the CAD
        document.json blob, or (for generic items) the sorted content-blob map
        excluding the per-commit-regenerated meta.json/summary.txt."""
        p = snapshot.item_prefix(item_id)
        doc = tree.get(p + "document.json")
        if doc is not None:
            return doc
        sig: list[tuple[str, bytes]] = []
        for path, data in tree.items():
            if not path.startswith(p):
                continue
            tail = path[len(p):]
            if tail in ("meta.json", "summary.txt"):
                continue
            sig.append((path, data))
        return tuple(sorted(sig))

    # -- writing -----------------------------------------------------------

    def save(self, kind: str = hist.KIND_AUTO, *, message: str = "",
             author: dict[str, Any] | None = None, ai: dict[str, Any] | None = None,
             item_id: str | None = None) -> dict[str, Any]:
        """Cut a history entry over the whole shadow tree (contract §3.1).

        ``kind`` ∈ auto | checkpoint | restore. ``release`` goes through
        :meth:`release`. Registers any new container files first (zero-config).
        """
        if kind not in (hist.KIND_AUTO, hist.KIND_CHECKPOINT, hist.KIND_RESTORE):
            raise VaultError("invalid_mode", f"save does not accept kind={kind!r}")
        # Optimistic concurrency: manifest() reads HEAD, we build the shadow tree from that
        # snapshot, then commit_tree CAS-swaps HEAD. If a concurrent session moved HEAD in between,
        # commit_tree raises ConcurrentUpdate; we rebuild from fresh HEAD and retry (bounded) so
        # neither writer is silently lost. No cross-process lock — same lock-free ethos as handoff.
        author = author or _user_author()
        emeta = gitbind.EntryMeta(
            kind=kind,
            message=message,
            author_name=author["display"],
            author_email=f'{author["id"]}@aicad.local',
        )
        for _attempt in range(6):
            m = self.manifest()
            # T-R4 lifecycle rule (contract §4): no edit-in-place of a Released
            # item — "start a new revision" first. Restore is exempt (§8.3: you
            # can always go back).
            files = self._explode_project_into_shadow(
                m, kind=kind, message=message, author=author, ai=ai,
                enforce_released_lock=(kind != hist.KIND_RESTORE))
            try:
                commit = self._bind.commit_tree(files, emeta)
            except gitbind.ConcurrentUpdate:
                continue  # HEAD advanced under us — rebuild against the new snapshot and retry
            return {"commit": commit, "kind": kind,
                    "entry": self._entry_view(commit, self._bind.read_tree(commit), item_id)}
        raise VaultError("write_conflict",
                         "vault HEAD kept moving under concurrent writers; retry the save")

    def checkpoint(self, message: str = "", *, author: dict[str, Any] | None = None,
                   ai: dict[str, Any] | None = None, item_id: str | None = None) -> dict[str, Any]:
        """Cut a named checkpoint entry (contract §3.2).

        When *message* is empty and the project's assist tier allows drafting
        (``assisted``/``managed``), a deterministic plain-language draft is
        templated from the semantic diff (contract §7 ``plm_checkpoint``:
        "drafts message from doc_diff if omitted"). At the ``manual`` floor
        the empty message stays empty — no assist behavior of any kind.
        """
        if not message and assist.allowed(self.ai_mode(), "draft_message"):
            message = self.draft_message(item_id=item_id)
        return self.save(hist.KIND_CHECKPOINT, message=message, author=author,
                         ai=ai, item_id=item_id)

    # -- assist tier (T-R3, contract §4 ai_mode) ----------------------------

    def ai_mode(self) -> str:
        """The project's assist tier: manual | assisted | managed (§4)."""
        return self.manifest().get("ai_mode", assist.DEFAULT_MODE)

    def set_ai_mode(self, mode: str) -> str:
        """Persist the assist tier for this project (contract §7 plm_set_ai_mode).

        The tier lives in ``manifest.json`` and is re-committed like any other
        manifest change, so it follows the project everywhere.
        """
        if mode not in manifest.AI_MODES:
            raise VaultError("invalid_mode",
                             "the assistance level can be manual, assisted, or managed")
        m = self.manifest()
        if m.get("ai_mode") == mode:
            return mode
        m["ai_mode"] = mode
        self._commit_manifest_change(m)
        return mode

    def auto_commit(self) -> bool:
        """Whether saves are auto-recorded to history (default true).

        Forward-tolerant: absent key reads as enabled. Lives in ``manifest.json``
        so the choice follows the project like ``ai_mode``.
        """
        return bool(self.manifest().get("auto_commit", True))

    def set_auto_commit(self, enabled: bool) -> bool:
        """Persist the auto-record-on-save flag for this project."""
        m = self.manifest()
        if bool(m.get("auto_commit", True)) == bool(enabled):
            return bool(enabled)
        m["auto_commit"] = bool(enabled)
        self._commit_manifest_change(m)
        return bool(enabled)

    # -- item registry (T-R4, contract §4) ----------------------------------

    def set_part_number(self, item_id: str, part_number: str) -> dict[str, Any]:
        """Edit an item's part number (auto-assigned, user-editable; unique
        within the project — contract §4). Persists via a manifest entry."""
        m = self.manifest()
        try:
            item = items_mod.set_part_number(m, item_id, part_number)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return items_mod.item_view(item)

    def start_new_rev(self, item_id: str) -> dict[str, Any]:
        """The "start rev C" action: a Released item goes back to *In work*
        so it can be edited; the working file carries forward (contract §4,
        no edit-in-place of Released). Idempotent when already In work."""
        m = self.manifest()
        try:
            out = items_mod.start_new_rev(m, item_id)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return {"item": items_mod.item_view(out["item"]),
                "working_rev": out["working_rev"]}

    def set_part_number_prefix(self, prefix: str) -> dict[str, Any]:
        """Configure the part-number scheme (T-R5 locked decision: numbers
        auto-suggest from a configurable scheme, e.g. ``PRJ-00042``).

        Only the prefix is configurable in v1; the counter stays the
        zero-padded max+1 rule in :func:`manifest.next_part_number`. Existing
        item numbers are never rewritten — the scheme applies to future
        suggestions only. Returns ``{part_number_prefix, next_part_number}``.
        """
        clean = "".join(ch for ch in prefix.upper() if ch.isalnum())
        if not clean:
            raise VaultError("invalid_mode",
                             "a part-number prefix needs letters or digits")
        m = self.manifest()
        if m.get("part_number_prefix") != clean:
            m["part_number_prefix"] = clean
            self._commit_manifest_change(m)
            m = self.manifest()
        return {"part_number_prefix": m["part_number_prefix"],
                "next_part_number": manifest.next_part_number(m)}

    def discard_working_rev(self, item_id: str) -> dict[str, Any]:
        """Undo a "start rev C" draft: return the working file to the item's
        last released form and mark it Released again (T-R5 locked decision —
        the auto-branch toast's "back to rev B (discard draft)" affordance).

        Forward-only like everything else: the return itself is a new history
        entry, so even a discarded draft can be recovered from the timeline.
        """
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        pins = [rr for rr in item.get("released_revs", []) or [] if rr.get("commit")]
        if not pins:
            raise VaultError("not_found",
                             "this design has no released version to return to")
        if item["lifecycle"] == manifest.LIFECYCLE_RELEASED:
            return {"item": items_mod.item_view(item), "entry": None}
        res = self.restore(pins[-1]["commit"], item_id)
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        item["lifecycle"] = manifest.LIFECYCLE_RELEASED
        self._commit_manifest_change(m)
        return {"item": items_mod.item_view(item), "entry": res["entry"]}

    # -- generic (non-CAD) items: docs, code, linked repos (2026-07) --------

    def add_item(self, rel_path: str, *, doc_type: str | None = None,
                 part_number: str | None = None) -> dict[str, Any]:
        """Track a non-CAD file or folder as a versioned item (contract §2).

        CAD ``.step+``/``.ecad+`` containers are registered automatically on
        save; this is the explicit "Track this file" path for everything else —
        specs, reports, spreadsheets, images, simulation folders, source trees.
        ``doc_type`` is auto-detected (:mod:`doctypes`) when omitted and is
        always user-retaggable. Returns the item view.
        """
        m = self.manifest()
        rel = str(rel_path).replace("\\", "/").lstrip("/")
        if manifest.find_item_by_path(m, rel) is not None:
            raise VaultError("invalid_mode", "that file is already tracked")
        abs_path = self.project_dir / rel
        dt = doc_type or doctypes.detect_doc_type(str(abs_path))
        if dt in doctypes.CONTAINER_DOC_TYPES:
            raise VaultError("invalid_mode",
                             "design documents are tracked automatically when saved")
        if not abs_path.exists():
            raise VaultError("not_found", f"there is nothing to track at {rel}")
        item = manifest.register_item(
            m, kind=doctypes.kind_for(dt), path=rel, doc_type=dt)
        if part_number:
            try:
                items_mod.set_part_number(m, item["item_id"], part_number)
            except items_mod.ItemRuleError as e:
                raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        m = self.manifest()
        return items_mod.item_view(manifest.find_item(m, item["item_id"]))

    def untrack_item(self, item_id: str) -> dict[str, Any]:
        """Stop tracking an item (forward-only: history keeps its old versions,
        only the live registry drops it — a restore of an earlier point still
        recovers it). Returns the removed item view."""
        m = self.manifest()
        try:
            item = items_mod.untrack_item(m, item_id)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        self._commit_manifest_change(m)
        return items_mod.item_view(item)

    def link_repo(self, url: str, name: str,
                  checkout_path: str | None = None) -> dict[str, Any]:
        """Link an external source repository by reference (contract §2 link).

        Stores only a reference (``link.json``) — never the code itself. At
        release the current upstream version is pinned (best-effort). The
        upstream version id is never surfaced to users as-is (contract §8.1).
        ``checkout_path`` (a local working copy, for pinning) is a per-machine
        detail recorded in the vault-local config, not the versioned tree.
        """
        url = str(url or "").strip()
        name = str(name or "").strip() or "Linked code"
        if not url:
            raise VaultError("invalid_mode", "a link needs a location")
        m = self.manifest()
        item = manifest.register_item(
            m, kind=doctypes.kind_for("linked_repo"), path=name, doc_type="linked_repo")
        iid = item["item_id"]
        inject = {iid: {generic_snapshot.link_path(iid):
                        generic_snapshot.new_link_doc(url, name)}}
        files = self._explode_project_into_shadow(
            m, kind=hist.KIND_AUTO, message="", inject=inject)
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local')
        self._bind.commit_tree(files, emeta)
        if checkout_path:
            self.set_local_config({"link_checkouts": {iid: str(checkout_path)}})
        m = self.manifest()
        return items_mod.item_view(manifest.find_item(m, iid))

    # -- vault-local (per-machine, unversioned) config ----------------------

    def get_local_config(self) -> dict[str, Any]:
        """Per-machine settings kept beside the vault (never in the shadow tree,
        never shared): visible columns, link checkout paths, last page. Returns
        ``{}`` when none is set."""
        import json as _json
        cfg_path = self._bind.vault_dir / "config.json"
        if not cfg_path.is_file():
            return {}
        try:
            return _json.loads(cfg_path.read_text("utf-8"))
        except Exception:
            return {}

    def set_local_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge *patch* into the vault-local config (one level deep for dicts)."""
        import json as _json
        cfg = self.get_local_config()
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        cfg_path = self._bind.vault_dir / "config.json"
        cfg_path.write_text(_json.dumps(cfg, indent=2, sort_keys=True), "utf-8")
        return cfg

    # -- planning store: tasks / issues / milestones (aicad.planning/1) ------

    def plan(self, op: str, entity: str, *, data: dict[str, Any] | None = None,
             ent_id: str | None = None, actor: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read or mutate the project planning store (contract plm_planning.md).

        ``op`` ∈ list | create | update | delete; ``entity`` ∈ task | issue |
        milestone. Mutations cut a planning-scoped history entry so the board is
        versioned/restore-safe/release-frozen with the design, but stays out of
        the curated design timeline (``plm_history`` default scope). Every
        mutation routes through the :mod:`permissions` seam (allow-all today).
        """
        op = str(op or "list")
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        try:
            fname = planning.file_name(entity)
            coll = planning.load(head_tree.get(fname), entity)
            if op == "list":
                return {"items": planning.entities(coll, entity)}
            action = {"create": "create", "update": "edit", "delete": "delete"}.get(op)
            if action is None:
                raise VaultError("invalid_mode", f"unknown planning action: {op!r}")
            permissions.require(actor, action, "planning")
            now = _now_iso()
            made: dict[str, Any] | None = None
            if op == "create":
                made = planning.create(coll, entity, data or {}, actor, now)
            elif op == "update":
                made = planning.update(coll, entity, str(ent_id or ""), data or {}, now)
            else:  # delete
                planning.delete(coll, entity, str(ent_id or ""))
        except planning.PlanningError as e:
            raise VaultError(e.reason, e.hint)
        except permissions.PermissionDenied as e:  # never in v1
            raise VaultError(e.reason, e.hint)
        self._commit_sibling(fname, planning.dump(coll), scope=hist.SCOPE_PLANNING)
        out: dict[str, Any] = {"items": planning.entities(coll, entity)}
        if made is not None:
            out["entity"] = made
        return out

    def overview(self, *, mine: dict[str, Any] | None = None) -> dict[str, Any]:
        """One round trip for the hub dashboard: unsaved items, recent design
        activity, open tasks/issues, and the next milestone."""
        m = self.manifest()
        dirty = self._dirty_items(m)
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        tasks = planning.entities(
            planning.load(head_tree.get("planning/tasks.json"), "task"), "task")
        issues = planning.entities(
            planning.load(head_tree.get("planning/issues.json"), "issue"), "issue")
        miles = planning.entities(
            planning.load(head_tree.get("planning/milestones.json"), "milestone"), "milestone")
        open_tasks = [t for t in tasks if t.get("status") != "done"]
        open_issues = [i for i in issues if i.get("state") == "open"]
        open_ms = sorted(
            (ms for ms in miles if ms.get("status") == "open"),
            key=lambda ms: (ms.get("due") is None, ms.get("due") or ""))
        return {
            "head": head,
            "ai_mode": m.get("ai_mode", "assisted"),
            "dirty_items": [items_mod.item_view(it) for d in dirty
                            if (it := manifest.find_item(m, d)) is not None],
            "recent_entries": self.history(limit=5, scope=hist.SCOPE_DESIGN),
            "open_tasks": open_tasks,
            "open_issues": open_issues,
            "next_milestone": open_ms[0] if open_ms else None,
            "counts": {"items": len(m["items"]), "dirty": len(dirty),
                       "open_tasks": len(open_tasks), "open_issues": len(open_issues),
                       "open_milestones": len(open_ms)},
        }

    def _commit_sibling(self, path: str, blob: bytes, *, scope: str) -> str:
        """Commit a change to a single versioned sibling path (e.g. a planning
        file) by carrying the HEAD tree forward verbatim and overriding just
        that path. Does NOT re-read working design files, so a planning edit
        never captures unsaved design changes. Respects the geometry.step
        release-only invariant (§2) by not carrying it forward."""
        head = self._bind.head_commit()
        if head is not None:
            files = {p: d for p, d in self._bind.read_tree(head).items()
                     if not p.endswith("/geometry.step")}
        else:
            files = {"manifest.json": manifest.dumps(self.manifest())}
        files[path] = blob
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
            scope=scope)
        return self._bind.commit_tree(files, emeta)

    def _commit_manifest_change(self, m: dict[str, Any]) -> str:
        """Persist a manifest-only change as an auto entry (§3.1: the manifest
        is re-committed with every history entry)."""
        files = self._explode_project_into_shadow(m, kind=hist.KIND_AUTO, message="")
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        return self._bind.commit_tree(files, emeta)

    # -- deterministic message drafting (T-R3) ------------------------------

    def draft_message(self, item_id: str | None = None) -> str:
        """Deterministic plain-language draft of "what changed since the last
        entry" — the template behind Copilot-drafted checkpoint messages.

        Numbers are copied from the semantic diff, never invented (Pillar 13).
        Returns ``""`` when nothing changed (an empty draft, not filler).
        """
        m = self.manifest()
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        parts: list[str] = []
        for it in m["items"]:
            if item_id is not None and it["item_id"] != item_id:
                continue
            exploded = self._working_exploded(it)
            if exploded is None:
                continue
            prev_raw = head_tree.get(snapshot.item_prefix(it["item_id"]) + "document.json")
            if prev_raw is not None and prev_raw == exploded.document_json:
                continue  # unchanged item — nothing to say
            parts.append(self._item_summary(prev_raw, exploded.document_json, it["path"]))
        return " ".join(p for p in parts if p and p != "No design changes.")

    def draft_release_note(self, item_id: str) -> str:
        """Deterministic draft of a release note: everything that changed on
        this item since its last released revision (or its first version)."""
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        exploded = self._working_exploded(item)
        if exploded is None:
            return ""
        base_raw: bytes | None = None
        pins = [rr for rr in item.get("released_revs", []) or [] if rr.get("commit")]
        if pins:
            base_tree = self._bind.read_tree(pins[-1]["commit"])
            base_raw = base_tree.get(snapshot.item_prefix(item_id) + "document.json")
        return self._item_summary(base_raw, exploded.document_json, item["path"])

    def restore(self, commit: str, item_id: str | None = None) -> dict[str, Any]:
        """Forward-only restore (contract §3.3): re-assemble the working files
        from a prior entry's tree, then cut a NEW ``restore`` entry. History is
        never rewound."""
        try:
            src_tree = self._bind.read_tree(commit)
        except KeyError:
            raise VaultError("not_found", "that point in history could not be found")
        m_raw = src_tree.get("manifest.json")
        m = manifest.loads(m_raw) if m_raw else self.manifest()

        # Re-assemble the working container files from the restored tree.
        targets = m["items"] if item_id is None else [it for it in m["items"] if it["item_id"] == item_id]
        if item_id is not None and not targets:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        for it in targets:
            dt = it.get("doc_type")
            if dt and dt in doctypes.GENERIC_DOC_TYPES:
                generic_snapshot.materialize(it, src_tree, self.project_dir)
                continue
            try:
                exploded = snapshot.read_item_from_tree(src_tree, it["item_id"])
            except KeyError:
                continue  # item not present at that point (e.g. added later)
            container = snapshot.reassemble_container(exploded)
            dst = self.project_dir / it["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(container)

        # Append a new forward restore entry (never rewrite). Plain-language
        # "when" — no git vocabulary, no hashes (contract §8.1).
        when = self._readable_when(commit)
        return self.save(hist.KIND_RESTORE, message=f"Restored to {when}", item_id=item_id)

    def _readable_when(self, commit: str) -> str:
        """A plain-language description of a history point (no hash surfaced)."""
        try:
            cmeta = self._bind.commit_meta(commit)
            return time.strftime("%d %b %Y, %H:%M", time.gmtime(cmeta["authored_at"]))
        except Exception:
            return "an earlier version"

    def release(self, item_id: str, message: str, *,
                geometry_step: bytes | None = None,
                author: dict[str, Any] | None = None,
                skip_preflight: bool = False,
                bom_pins: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Release an item (contract §5): rev stamp + lifecycle + geometry
        freeze + (for assemblies) the BOM freeze pin set.

        This is the atomic release ENTRY. The full T-R4 flow — pre-flight
        battery, component-pin computation, stamp orders — is orchestrated by
        :func:`services.plm.release.release`, which passes ``bom_pins`` here.
        The In-work → Released transition is enforced: releasing an
        already-Released item needs "start a new revision" first (§4).
        """
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
        try:
            items_mod.require_in_work_for_release(item)
        except items_mod.ItemRuleError as e:
            raise VaultError(e.reason, e.hint)
        if not message and assist.allowed(self.ai_mode(), "draft_message"):
            # assisted+ tiers: deterministically draft the release note from
            # the changes since the last released revision (user-editable —
            # an explicit message always wins; manual tier never drafts).
            message = self.draft_release_note(item_id)
        if not message:
            raise VaultError("preflight_failed", "a release needs a note")

        # rev stamp + lifecycle transition (contract §5.2, §5.3)
        new_rev = manifest.next_rev_letter(item.get("rev"))
        prior_rev = item.get("rev")
        head = self._bind.head_commit()
        if prior_rev:
            for rr in item["released_revs"]:
                if rr["rev"] == prior_rev:
                    rr["lifecycle"] = manifest.LIFECYCLE_SUPERSEDED
        item["rev"] = new_rev
        item["lifecycle"] = manifest.LIFECYCLE_RELEASED
        item["released_revs"].append({
            "rev": new_rev, "commit": None, "released_at": _now_iso(),
            "lifecycle": manifest.LIFECYCLE_RELEASED,
        })

        # BOM freeze (contract §5.5): pin the component reference set to the
        # exact revs computed by services.plm.release.component_pins.
        freeze = None
        if bom_pins is not None:
            freeze = {"assembly_item": item_id, "rev": new_rev, "commit": None,
                      "pins": bom_pins}
            m.setdefault("bom_freezes", []).append(freeze)

        # explode project, freezing geometry.step for the released item only (§5.4)
        files = self._explode_project_into_shadow(
            m, kind=hist.KIND_RELEASE, message=message, author=author,
            geometry_for={item_id: geometry_step} if geometry_step is not None else None,
        )
        author = author or _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_RELEASE, message=message,
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        commit = self._bind.commit_tree(files, emeta)

        # Back-fill the release commit id into the rev pin. A commit cannot embed
        # its own hash, so the manifest just committed still has commit:null for
        # this rev. Persist the pin with a tiny follow-up commit that rewrites ONLY
        # manifest.json over the same tree (append-only-safe; the release stays the
        # authoritative geometry-freeze entry, this just resolves the pointer).
        # released_revs[].commit MUST be non-null in the stored manifest so §4/§5
        # readers can locate the frozen geometry.step later.
        item["released_revs"][-1]["commit"] = commit
        if freeze is not None:
            freeze["commit"] = commit
        files["manifest.json"] = manifest.dumps(m)
        pin_meta = gitbind.EntryMeta(
            kind=hist.KIND_RELEASE, message=message,
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        pinned_commit = self._bind.commit_tree(files, pin_meta)

        return {"commit": pinned_commit, "release_commit": commit, "rev": new_rev,
                "entry": self._entry_view(pinned_commit, self._bind.read_tree(pinned_commit), item_id),
                "prior_rev": prior_rev}

    # -- maintenance -------------------------------------------------------

    # -- lightweight branching (wave-2) ------------------------------------

    def branch_list(self) -> dict[str, Any]:
        """List named design branches (refs into the content-addressed vault)."""
        from services.plm import branches as branches_mod
        return branches_mod.list_branches(self)

    def branch_create(self, name: str, *, from_ref: str | None = None) -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.create_branch(self, name, from_ref=from_ref)

    def branch_switch(self, name: str) -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.switch_branch(self, name)

    def branch_merge(self, src: str, dst: str, *, message: str = "") -> dict[str, Any]:
        from services.plm import branches as branches_mod
        return branches_mod.merge_branches(self, src, dst, message=message)

    def active_branch(self) -> str:
        from services.plm import branches as branches_mod
        return branches_mod.active_branch(self)


    def repack(self) -> str:
        return self._bind.repack()

    # -- internals ---------------------------------------------------------

    def _seed_initial_project(self) -> None:
        """On first open, register existing containers and cut the seed entry."""
        m = manifest.new_manifest(self.project_dir.name, _now_iso())
        containers = self._project_containers()
        if not containers:
            # empty project — still commit an empty manifest so HEAD exists
            files = {"manifest.json": manifest.dumps(m)}
            emeta = gitbind.EntryMeta(kind=hist.KIND_AUTO, message="",
                                      author_name="System", author_email="system@aicad.local")
            self._bind.commit_tree(files, emeta)
            return
        # register each container, then cut one auto entry over all of them
        files = self._explode_project_into_shadow(m, kind=hist.KIND_AUTO, message="")
        emeta = gitbind.EntryMeta(kind=hist.KIND_AUTO, message="",
                                  author_name="System", author_email="system@aicad.local")
        self._bind.commit_tree(files, emeta)

    def _project_containers(self) -> list[Path]:
        out: list[Path] = []
        for p in sorted(self.project_dir.rglob("*")):
            if not (p.is_file() and any(p.name.endswith(s)
                                        for s in _CONTAINER_SUFFIXES)):
                continue
            # skip anything inside a dot-prefixed dir (.aicadvault/.plm/.git/…)
            rel = p.relative_to(self.project_dir)
            if any(part.startswith(".") for part in rel.parts):
                continue
            out.append(p)
        return out

    def _explode_project_into_shadow(
        self, m: dict[str, Any], *, kind: str, message: str,
        author: dict[str, Any] | None = None, ai: dict[str, Any] | None = None,
        geometry_for: dict[str, bytes] | None = None,
        enforce_released_lock: bool = False,
        inject: dict[str, dict[str, bytes]] | None = None,
        inject_paths: dict[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        """Explode every project container into the shadow tree + manifest.

        Registers newly-seen containers into ``m`` (zero-config first check-in).
        Generic (non-container) items are re-emitted here too, per the
        carry-forward law (:mod:`generic_snapshot`) — a whole-tree commit that
        omitted them would drop them from that point in history. Any versioned
        sibling trees (e.g. ``planning/``) are carried forward verbatim.
        Returns the whole ``{path: bytes}`` shadow tree to commit.
        """
        author = author or _user_author()
        geometry_for = geometry_for or {}
        inject = inject or {}
        ai_block = ai if (ai and author.get("kind") == "ai") else None
        files: dict[str, bytes] = {}
        head = self._bind.head_commit()
        head_tree = self._bind.read_tree(head) if head else {}
        for path in self._project_containers():
            raw = path.read_bytes()
            try:
                exploded = snapshot.explode_container(raw)
            except (ValueError, Exception):
                continue  # not a valid container — skip (graceful degradation)
            import json as _json
            doc = _json.loads(exploded.document_json.decode("utf-8"))
            doc_uuid = doc.get("id", "")
            rel = path.relative_to(self.project_dir).as_posix()
            item = manifest.find_item_by_path(m, rel)
            schema_kind = _kind_from_schema(doc.get("schema", ""))
            if item is None:
                suffix = next((s for s in _CONTAINER_SUFFIXES if rel.endswith(s)), "")
                item = manifest.register_item(
                    m, doc_uuid=doc_uuid,
                    kind=schema_kind or _SUFFIX_KIND.get(suffix, "part"), path=rel)
            elif schema_kind and item["kind"] != schema_kind:
                item["kind"] = schema_kind  # refine a suffix-guessed kind
            iid = item["item_id"]
            meta_json = hist.make_meta(
                item_id=iid, entry_kind=kind, authored_at=_now_iso(),
                author_kind=author.get("kind", "user"), author_id=author["id"],
                author_display=author["display"], message=message,
                doc_uuid=doc_uuid, rev=item.get("rev"), lifecycle=item["lifecycle"],
                ai=ai_block,
            )
            # plain-language change summary per entry (T-R3, contract §2
            # summary.txt): deterministic, numerals copied from doc_diff.
            prev_raw = head_tree.get(snapshot.item_prefix(iid) + "document.json")
            if (enforce_released_lock and prev_raw is not None
                    and prev_raw != exploded.document_json):
                try:
                    items_mod.guard_released_edit(m, [iid])
                except items_mod.ItemRuleError as e:
                    raise VaultError(e.reason, e.hint)
            summary = self._item_summary(prev_raw, exploded.document_json, rel)
            files.update(snapshot.item_paths(
                iid, exploded, meta_json=meta_json,
                summary_txt=summary.encode("utf-8"),
                geometry_step=geometry_for.get(iid),
            ))

        # generic (non-container) items — the carry-forward law (contract §2):
        # re-emit their content every commit (re-reading the working file, or
        # carrying the HEAD blobs forward) so a whole-tree commit never drops
        # them from that point in history.
        for it in m["items"]:
            dt = it.get("doc_type")
            if not dt or dt in doctypes.CONTAINER_DOC_TYPES:
                continue  # container item (handled above) or legacy CAD item
            iid = it["item_id"]
            injected = inject.get(iid)
            content = (injected if injected is not None
                       else generic_snapshot.content_paths(it, self.project_dir, head_tree))
            old_content = generic_snapshot.head_content(iid, head_tree)
            if enforce_released_lock and old_content and content != old_content:
                try:
                    items_mod.guard_released_edit(m, [iid])
                except items_mod.ItemRuleError as e:
                    raise VaultError(e.reason, e.hint)
            summary = generic_snapshot.summarize(it, old_content, content)
            meta_json = hist.make_meta(
                item_id=iid, entry_kind=kind, authored_at=_now_iso(),
                author_kind=author.get("kind", "user"), author_id=author["id"],
                author_display=author["display"], message=message,
                doc_uuid=it.get("doc_uuid", ""), rev=it.get("rev"),
                lifecycle=it["lifecycle"], ai=ai_block,
            )
            p = snapshot.item_prefix(iid)
            files.update(content)
            files[p + "meta.json"] = meta_json
            files[p + "summary.txt"] = summary.encode("utf-8")

        # carry forward any versioned sibling trees the explode does not own
        # (e.g. planning/ — written via the dedicated planning path). Whole-tree
        # commits must include them or they vanish from that point in history.
        for path, data in head_tree.items():
            top = path.split("/", 1)[0]
            if top in _CARRY_FORWARD_TOPLEVEL and path not in files:
                files[path] = data

        # explicit sibling-path writes (e.g. planning files) override the
        # carried-forward blobs for exactly the paths being mutated.
        for path, data in (inject_paths or {}).items():
            files[path] = data

        files["manifest.json"] = manifest.dumps(m)
        return files

    def _working_exploded(self, item: dict[str, Any]) -> snapshot.ExplodedItem | None:
        """Explode an item's current working container (None when unreadable)."""
        path = self.project_dir / item["path"]
        if not path.is_file():
            return None
        try:
            return snapshot.explode_container(path.read_bytes())
        except Exception:
            return None

    def _item_summary(self, prev_raw: bytes | None, cur_raw: bytes, rel_path: str) -> str:
        """Deterministic summary between two canonical document blobs.

        Degrades to an empty string if the semantic diff engine is not
        importable — a missing summary must never block a save.
        """
        import json as _json
        try:
            cur_doc = _json.loads(cur_raw.decode("utf-8"))
            if prev_raw is None:
                return summarize.summarize_first_version(cur_doc, rel_path)
            if prev_raw == cur_raw:
                return "No design changes."
            prev_doc = _json.loads(prev_raw.decode("utf-8"))
            return summarize.summary_between_docs(prev_doc, cur_doc, rel_path)
        except Exception:
            return ""

    def _dirty_items(self, m: dict[str, Any]) -> list[str]:
        """Return item_ids whose working container differs from HEAD's canonical form."""
        head = self._bind.head_commit()
        if head is None:
            return [it["item_id"] for it in m["items"]]
        tree = self._bind.read_tree(head)
        dirty: list[str] = []
        for it in m["items"]:
            dt = it.get("doc_type")
            if dt and dt in doctypes.GENERIC_DOC_TYPES:
                if generic_snapshot.is_dirty(it, self.project_dir, tree):
                    dirty.append(it["item_id"])
                continue
            path = self.project_dir / it["path"]
            if not path.exists():
                continue
            try:
                exploded = snapshot.explode_container(path.read_bytes())
            except Exception:
                continue
            head_doc = tree.get(snapshot.item_prefix(it["item_id"]) + "document.json")
            if head_doc != exploded.document_json:
                dirty.append(it["item_id"])
        return dirty

    def _item_view(self, it: dict[str, Any]) -> dict[str, Any]:
        return items_mod.item_view(it)

    def _entry_view(self, commit: str, tree: dict[str, bytes], item_id: str | None) -> dict[str, Any]:
        cmeta = self._bind.commit_meta(commit)
        kind = hist.kind_from_commit_message(cmeta["message"])
        scope = hist.scope_from_commit_message(cmeta["message"])
        msg = hist.user_message_from_commit(cmeta["message"])
        # pull author + ai from a representative item meta.json
        author = {"kind": "user", "id": "user", "display": "User"}
        ai = None
        thumb_ref = None
        summary = ""
        pick_iid = item_id
        if pick_iid is None:
            for path in tree:
                if path.startswith("items/") and path.endswith("/meta.json"):
                    pick_iid = path.split("/")[1]
                    break
        if pick_iid is not None:
            mraw = tree.get(snapshot.item_prefix(pick_iid) + "meta.json")
            if mraw:
                import json as _json
                md = _json.loads(mraw.decode("utf-8"))
                author = md.get("author", author)
                ai = md.get("ai")
            if snapshot.item_prefix(pick_iid) + "thumbnail.png" in tree:
                thumb_ref = snapshot.item_prefix(pick_iid) + "thumbnail.png"
            summary = (tree.get(snapshot.item_prefix(pick_iid) + "summary.txt") or b"").decode("utf-8")
        return {
            "commit": commit, "kind": kind, "scope": scope,
            "authored_at": _iso(cmeta["authored_at"]),
            "author": author, "ai": ai, "message": msg,
            "thumbnail_ref": thumb_ref, "summary": summary,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _user_author() -> dict[str, Any]:
    """Session-aware user author (billet-web injects AICAD_USER_* env).

    Author lives ONLY in meta/commit (byte-stability law §0) — never document.json.
    """
    uid = os.environ.get("AICAD_USER_ID") or "user"
    display = os.environ.get("AICAD_USER_DISPLAY") or None
    if not display:
        display = "User" if uid == "user" else uid
    return {"kind": "user", "id": uid, "display": display}
