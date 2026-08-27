---
id: T-R7
title: PLM sharing & team sync v1 - remote profiles, one-button Sync, advisory locks, copy-as-variant conflicts
workstream: R
owner: unassigned
status: done
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
The Chudware Cloud hosted remote is a RESERVED config kind only (Pillar-12 human
gate before any server work).

## Files / areas touched

- services/plm/remote.py — NEW: remote profiles per contracts/plm.md
  (folder/ssh/https kinds; `chudware-cloud` kind parses but refuses activation with
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

- [x] Two independent checkouts round-trip through shared-folder and
      ssh/https-labelled remote profiles; complete item trees (including
      thumbnails/summaries) survive
- [x] Real advisory lock status, expiry, UI lock/unlock, non-blocking
      "locked by <name>" quickbar chip, and one-confirm force override
- [x] Divergence automatically preserves the losing edit as a provenance-linked
      copy-as-new-item; both results remain restorable and no merge UI exists
- [x] `chudware-cloud` profile parses but every activation politely refuses
- [x] Sync failures return fixed plain-language hints; raw transport detail is
      server-log-only and never reaches UI/agent envelopes
- [x] Deterministic CAS retry, stale expiry, offline edit/sync, identity,
      worker/MCP plumbing, complete PLM suite, GUI build/lints, and Team-page
      headless smoke all pass; graph updated

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

## Progress log

- 2026-08-27: Backend audit found the core folder/ssh/https sync,
  copy-as-variant collision preservation, advisory-lock CAS, and reserved cloud
  kind already implemented and covered. Completion work is focused on real
  status lock reads, explicit soft-lock override, plain-language error hygiene,
  identity/capability profile fields, and replacing the disabled Team & Sync UI.
  Frozen `contracts/plm.md` §6.4 remains authoritative: collisions are preserved
  automatically without a merge-choice UI; the older contradictory
  ask-with-preview sentence above is superseded for this implementation.
- 2026-08-27: Completed. Remote profiles now carry reserved account identity
  and forward-compatible snapshot-sync/advisory-lock capabilities. `plm_status`
  reads live locks best-effort, explicit force override reports the prior
  holder, and raw transport errors are quarantined to logs. Team & Sync is a
  real Hub destination with folder/server sharing, per-remote Sync, live lock
  rows, collision-copy results, and unavailable/offline honesty; the active-doc
  quickbar exposes lock ownership and explicit takeover. Verification: 388 PLM
  tests passed, integrated host build passed, five scoped GUI lint rules were
  clean, and a real remote-profile Team shot wrote a 1340x838 PNG.
