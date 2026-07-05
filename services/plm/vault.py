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

from services.plm import assist, gitbind, manifest, snapshot
from services.plm import history as hist  # aliased: Vault.history() method shadows the name
from services.plm import summarize

# document containers we version (contract §2)
_CONTAINER_SUFFIXES = (".step+", ".ecad+")

# doc-kind inference from container extension (best-effort; refined by document.json)
_SUFFIX_KIND = {".step+": "part", ".ecad+": "ecad"}


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
            "items": [self._item_view(it) for it in m["items"]],
            "remotes": m.get("remotes", []),
            "locks": [],  # lock manifest lives on refs/aicad/locks (T-R7)
        }

    def history(self, item_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the timeline (filtered per item if given) — contract §7 plm_history."""
        entries: list[dict[str, Any]] = []
        item_path = None
        if item_id is not None:
            item_path = snapshot.item_prefix(item_id) + "document.json"
        prev_tree: dict[str, bytes] | None = None
        commits = self._bind.log()
        for commit in commits:
            tree = self._bind.read_tree(commit)
            # per-item filter: include only commits where this item's blob changed
            if item_path is not None:
                cur = tree.get(item_path)
                if prev_tree is not None and prev_tree.get(item_path) == cur:
                    prev_tree = tree
                    continue
            entries.append(self._entry_view(commit, tree, item_id))
            prev_tree = tree
            if limit is not None and len(entries) >= limit:
                break
        return entries

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
        m = self.manifest()
        files = self._explode_project_into_shadow(m, kind=kind, message=message,
                                                  author=author, ai=ai)
        author = author or _user_author()
        emeta = gitbind.EntryMeta(
            kind=kind,
            message=message,
            author_name=author["display"],
            author_email=f'{author["id"]}@aicad.local',
        )
        commit = self._bind.commit_tree(files, emeta)
        return {"commit": commit, "kind": kind,
                "entry": self._entry_view(commit, self._bind.read_tree(commit), item_id)}

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
        files = self._explode_project_into_shadow(m, kind=hist.KIND_AUTO, message="")
        author = _user_author()
        emeta = gitbind.EntryMeta(
            kind=hist.KIND_AUTO, message="",
            author_name=author["display"], author_email=f'{author["id"]}@aicad.local',
        )
        self._bind.commit_tree(files, emeta)
        return mode

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
            exploded = snapshot.read_item_from_tree(src_tree, it["item_id"])
            container = snapshot.reassemble_container(exploded)
            (self.project_dir / it["path"]).write_bytes(container)

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
                skip_preflight: bool = False) -> dict[str, Any]:
        """Release an item (contract §5): rev stamp + lifecycle + geometry freeze.

        v1 engine core: rev++/lifecycle transition/geometry.step freeze + BOM
        pin recording. The full pre-flight battery + drawing title-block stamp
        are T-R4; this cuts the atomic release entry the contract requires.
        """
        m = self.manifest()
        item = manifest.find_item(m, item_id)
        if item is None:
            raise VaultError("invalid_item", f"no such item: {item_id}")
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
        for p in sorted(self.project_dir.iterdir()):
            if p.is_file() and any(p.name.endswith(s) for s in _CONTAINER_SUFFIXES):
                out.append(p)
        return out

    def _explode_project_into_shadow(
        self, m: dict[str, Any], *, kind: str, message: str,
        author: dict[str, Any] | None = None, ai: dict[str, Any] | None = None,
        geometry_for: dict[str, bytes] | None = None,
    ) -> dict[str, bytes]:
        """Explode every project container into the shadow tree + manifest.

        Registers newly-seen containers into ``m`` (zero-config first check-in).
        Returns the whole ``{path: bytes}`` shadow tree to commit.
        """
        author = author or _user_author()
        geometry_for = geometry_for or {}
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
            rel = path.name
            item = manifest.find_item_by_path(m, rel)
            if item is None:
                suffix = next((s for s in _CONTAINER_SUFFIXES if rel.endswith(s)), "")
                item = manifest.register_item(
                    m, doc_uuid=doc_uuid, kind=_SUFFIX_KIND.get(suffix, "part"), path=rel)
            iid = item["item_id"]
            ai_block = ai if (ai and author.get("kind") == "ai") else None
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
            summary = self._item_summary(prev_raw, exploded.document_json, rel)
            files.update(snapshot.item_paths(
                iid, exploded, meta_json=meta_json,
                summary_txt=summary.encode("utf-8"),
                geometry_step=geometry_for.get(iid),
            ))
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
        return {
            "item_id": it["item_id"], "path": it["path"], "kind": it["kind"],
            "part_number": it.get("part_number"), "rev": it.get("rev"),
            "lifecycle": it["lifecycle"],
        }

    def _entry_view(self, commit: str, tree: dict[str, bytes], item_id: str | None) -> dict[str, Any]:
        cmeta = self._bind.commit_meta(commit)
        kind = hist.kind_from_commit_message(cmeta["message"])
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
            "commit": commit, "kind": kind,
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
    return {"kind": "user", "id": "user", "display": "User"}
