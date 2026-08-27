---
id: T-R8
title: Self-hosted PLM server + live co-editing — two people in the same part, no locks
workstream: R
owner: unassigned
status: done
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

- [x] OSS survey committed with relay-vs-CRDT decision, upstream behavior,
      license checks, and primary citations
- [x] `aicad.plm-live/1` frozen in `contracts/plm_live.md`
- [x] Two real WebSocket clients interleave authoritative engine batches,
      receive sequences 1/2 and identical final hashes; core-backed replay uses
      `eval_compare` to prove identical geometry; snapshots apply atomically
- [x] Last leave writes exactly one vault checkpoint attributed to every
      participant who committed
- [x] `plm-server` selects live mode automatically; folder/ssh/https remotes
      retain T-R7 advisory locks and copy-as-variant sync unchanged
- [x] Complete PLM suite, native GUI build, scoped UI lints, headless Team
      surface, `scripts/gate.sh`, and graph update pass

## Progress log

- 2026-08-27: OSS survey completed in
  `docs/review/PLM_LIVE_COLLAB_SURVEY.md`. Yjs and Automerge are permissive but
  deliberately not imported: their order-independent generic merge models do
  not preserve the existing engine's semantic, revisioned CAD operation
  invariants. Chosen architecture is one authoritative WebSocket relay that
  orders whole `doc_apply_ops` batches and publishes one canonical snapshot
  between frames. `contracts/plm_live.md` freezes `aicad.plm-live/1`; BSD-3
  `websockets` is pinned and recorded, with no license debt.
- 2026-08-27: Completed. `python -m services.plm.server` is a
  deny-by-default self-hosted relay with total-order batches, typed semantic
  rejection, snapshots/catch-up, reconnect idempotency, bounded presence, and
  one guarded final checkpoint. The Python client manager attaches the local
  hostd document and applies each authoritative batch exactly once; WorkerClient
  transparently reroutes every joined document's ordinary `doc_apply_ops`
  through the relay and never falls back locally after refusal. Team/quickbar UI
  exposes live-server mode, join/leave, LIVE/reconnecting and participant chips;
  incoming commits use the existing whole-batch `doc.changed` viewport refresh.
  Proof: 413 PLM tests, successful native build, five clean scoped GUI lints,
  actual two-seat sequences `[1,2]` with equal final hashes, one checkpoint with
  both participant identities, and a 1340x838 live-server Team capture.
