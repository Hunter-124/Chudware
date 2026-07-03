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
