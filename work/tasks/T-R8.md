---
id: T-R8
title: Self-hosted PLM server + live co-editing — two people in the same part, no locks
workstream: R
owner: unassigned
status: todo
branch: task/T-R8-plm-live-server
depends_on: [T-R7]
lane: plm-projects
kind: new
priority: 3
---

## Context

Hunter directive (2026-07-05 planning session, verbatim intent): "2 people should be
able to live edit a part if they have the same one open when self hosting the plm
system" — dumb remotes (folder/Dropbox/git URL) keep file locks + soft chip (T-R7
mode a); a SELF-HOSTED PLM server unlocks real-time co-editing of the same doc.

The engine is already op-based (doc ops / eq_set / add_feature streams), which makes
op-streaming the natural transport: a live session relays each participant's committed
ops to the other participants, who apply them to their doc replica. Same-doc conflicts
must become structurally impossible (single authoritative op order per session), and
that INCLUDES live previews — a participant's viewport must never show a torn state
(Hunter: "make sure this isnt an issue in the live previews").

OSS-first directive applies: before coding, survey existing sync/collab machinery
(CRDT libs: Yjs/Automerge; OT frameworks; plain authoritative-relay websocket designs
like multiplayer game servers). The engine's sequential-op model likely favors an
authoritative relay (server orders ops, clients apply in order) over CRDTs — but
document the tradeoff and pick with citations. License guardrails per
contracts/license_debt.md process.

Modularity rule (locked): everything speaks aicad.plm/1 + a new live-session contract;
git remains a storage detail underneath the vault. The server is a thin self-hostable
process (single binary or `python -m services.plm.server`), NOT a SaaS dependency.

## Scope

- Live-session contract (aicad.plm-live/1): join/leave, op relay with authoritative
  ordering, presence (who's in the doc, cursors/selection best-effort), snapshot
  catch-up for late joiners.
- services/plm/server: self-hostable session server (websocket), vault-backed
  persistence (session end = checkpoint in the hidden vault, attributed to all
  participants).
- Client seam: doc session attaches to a live session when the project's remote
  profile is a PLM server and someone else has the doc open; T-R7's transport seam
  is the plug point. Presence chips in the doc tab (reuses T-R7 chip UI).
- GUI: minimal v1 — presence chips + "live" indicator + incoming-op viewport refresh
  (no torn previews: ops apply atomically between frames).
- Tests: two headless clients against a local server — interleaved ops converge to
  identical doc state (eval_compare); late-join catch-up; disconnect/reconnect.

## Definition of done

- [ ] OSS survey committed (docs/) with the relay-vs-CRDT decision + citations
- [ ] aicad.plm-live/1 contract frozen in contracts/
- [ ] Two headless clients co-edit one part via a local server; eval_compare proves
      identical final geometry; no torn intermediate renders (shot-verified)
- [ ] Session end writes ONE vault checkpoint attributed to all participants
- [ ] Dumb-remote projects are untouched (T-R7 lock mode keeps working); mode is
      chosen by remote-profile type, zero user config
- [ ] scripts/gate.sh PASS; graphify update
