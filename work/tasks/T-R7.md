---
id: T-R7
title: PLM sharing & team sync v1 - remote profiles, one-button Sync, advisory locks, copy-as-variant conflicts
workstream: R
owner: unassigned
status: todo
branch: task/T-R7-plm-sharing-sync
depends_on: [T-R2, T-R4]
lane: plm-projects
kind: new
priority: 4
---

## Context

The sharing spectrum ([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)
§3.3) on one mechanism: the hidden vault syncs to a remote profile — shared
folder/USB, or any git remote the user controls (bare repo over SSH, Gitea,
GitHub…) = self-hosting for free. Team v1 is deliberately merge-free: advisory
edit locks + one-button Sync; a genuine collision preserves the loser's work as
copy-as-new-item ("Bracket (Sarah's edits)") — never a merge UI, never data loss.
The AI-CAD Cloud hosted remote is a RESERVED config kind only (Pillar-12 human
gate before any server work).

## Files / areas touched

- services/plm/remote.py — NEW: remote profiles per contracts/plm.md
  (folder/ssh/https kinds; `aicad-cloud` kind parses but refuses activation with
  a "not yet available" message), sync = fetch + fast-forward or
  collision-detect + push
- services/plm/locks.py — NEW: advisory lock manifest at the remote, atomic-push
  CAS per contract; stale-lock expiry; identity = simple display name per
  machine (no accounts in v1)
- services/plm/conflict.py — NEW: collision → copy-as-new-item materialization
  (new item_id + part number, provenance note linking both histories)
- plm_tools.py: plm_remote_set, plm_sync, plm_lock/plm_unlock; Hub/editor chips
  consume lock + sync state (T-R6 renders them)
- Setup UX: "Share this project" dialog — pick folder OR paste git URL; no
  other questions (keys/agent handled by system ssh; document limits honestly)

## Definition of done

- [ ] Two machines (or two checkouts simulating them) round-trip a project
      through a bare SSH remote AND a shared-folder remote: entries interleave
      correctly, thumbnails/summaries intact
- [ ] Locks: editor shows "locked by <name>" on open-for-edit of a locked item;
      lock/unlock via UI + plm tools; stale expiry works
- [ ] Forced collision (both edit same item while offline) → loser preserved as
      copy-as-new-item with provenance; NO merge prompt, NO lost state; test
      proves both end states restorable
- [ ] aicad-cloud remote kind: config parses, activation politely refused
      (seam verified, nothing built — Pillar-12 gate note in code comment)
- [ ] Sync failure modes are plain-language (no git stderr leaks to UI)
- [ ] Tests incl. CAS lock races + offline queue; gate + graphify update .

## Locked decisions (Hunter, 2026-07-05 planning session)

- **NOT git-shaped:** the vault stays contract-first (aicad.plm/1); git is a swappable
  v1 storage detail. No git semantics in the UX or the contract — the system stays
  modular/simplistic so a CAD-aware backend (geometry deltas, large binaries) can
  replace git without breaking anything. Design every R7 seam against the contract,
  never against git plumbing.
- **Remote types (v1):** share dialog offers BOTH a shared-folder path (NAS/Dropbox/
  Drive) and a server URL; both are remotes behind the same vault interface.
- **Locks — two modes:**
  (a) dumb-remote mode (folder/git URL/Dropbox): file locks + soft chip
      ("gissa is editing this") + one-confirm override; nobody hard-blocked.
  (b) self-hosted PLM server mode: LIVE CO-EDITING of the same part (see T-R8) —
      no locks needed for same-doc sessions. R7 must leave the transport seam so
      T-R8's live session layer plugs in without rework.
- **Conflicts:** ask-with-preview at sync time (reuse R5 compare view); the person
  syncing decides (keep-as-variant vs adopt theirs); everything non-conflicting syncs
  without stopping. Live co-edit sessions (T-R8) must make same-doc conflicts
  structurally impossible, incl. in live previews.
- **Identity:** v1 = per-machine profile display name (prompted at first share,
  pre-filled from OS user); the schema RESERVES an account-id field to tie into
  vendor/workstream-X accounts later.
