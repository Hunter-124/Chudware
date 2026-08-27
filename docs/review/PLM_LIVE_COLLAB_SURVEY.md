# PLM live collaboration — OSS survey and transport decision

Status: DECIDED for T-R8. This survey feeds `contracts/plm_live.md`
(`aicad.plm-live/1`). It does not replace the snapshot-sync and advisory-lock
behavior in `contracts/plm.md`; live editing is available only for an explicit
self-hosted PLM-server remote profile.

All licenses and behavior claims below were checked against upstream sources on
2026-08-27.

## Requirements from Chudware's engine

Chudware edits are already semantic, ordered operation batches
(`doc_apply_ops`), not arbitrary text or JSON patches. The engine validates a
batch against one document revision, applies it under the document lock, then
publishes one `doc.changed` snapshot to other clients. A collaboration layer
must preserve those properties:

- one total order for committed batches in a live item session;
- a batch is indivisible and clients swap its resulting snapshot between UI
  frames, never halfway through a feature or solver update;
- the authoritative engine, not a generic merge library, decides whether a CAD
  operation is valid;
- reconnect and late join recover by sequence plus a canonical snapshot;
- presence is best-effort and must never affect document truth;
- local/folder/SSH/HTTPS remotes keep T-R7 advisory locks unchanged.

## Options considered

| Option | License | Strength | Mismatch for this engine |
|---|---|---|---|
| Yjs | MIT | Mature operation-based CRDT, network agnostic, automatic concurrent merge, awareness/presence ecosystem | Requires translating Chudware's semantic CAD operations and identity graph into Y shared types. Update order deliberately does not matter in Yjs, while feature creation, references, solver constraints, and revisions are order-sensitive. A structurally merged JSON tree can still be invalid CAD. |
| Automerge / Automerge Repo | MIT | Local-first CRDT documents, compact history, transport-agnostic per-document sync, pluggable network/storage | Replaces the document/history model with an Automerge document and merge rules. Chudware already has its canonical document, revision ledger, vault, and semantic validator; keeping both histories authoritative would create a second source of truth. |
| Operational transformation framework | varies | Central ordering and transformed concurrent edits are proven for text/range operations | Chudware operations are heterogeneous feature/constraint commands, not one transformable text algebra. A correct pairwise transform matrix would become a second CAD engine and grow quadratically with operation types. |
| Authoritative WebSocket relay | Python `websockets`: BSD-3-Clause | Server assigns one monotonically increasing sequence, validates each whole batch through the existing engine, broadcasts commits and presence, and snapshots late joiners | Requires a self-hosted server and an online live session. That is intentional: offline work remains T-R7 snapshot sync, where divergence is preserved as a variant. |

## Decision

Use an **authoritative relay**, not a CRDT or OT layer.

The decision is architectural rather than preference-driven:

1. Chudware already has the conflict-resolution primitive live CAD needs: the
   engine's per-document serialized `doc_apply_ops` transaction and revision
   check. The live server must expose that authority, not duplicate it.
2. Total order makes same-document conflicts structurally impossible during a
   connected session. Invalid later operations receive a typed rejection from
   the real engine; they are never merged into an invalid tree.
3. Every committed batch produces one resulting snapshot. Clients install that
   snapshot between frames, which is the direct no-torn-preview invariant.
4. Offline edits are a different mode with a different honest contract. They
   remain local commits and T-R7 sync preserves a collision as a new item.
5. The only new runtime dependency is `websockets` (BSD-3-Clause), already used
   by Chudware's web gateway. It is permissive and commercial-safe; no license
   debt row is required. Yjs and Automerge are also permissive, but are not
   imported or vendored.

WebSocket supplies ordered delivery within each connection; RFC 6455 requires
message fragments to be delivered in sender order. The protocol does **not**
provide a total order across several connections, so `aicad.plm-live/1`
explicitly makes the server assign the session-wide sequence.

## Sources

- [Yjs introduction](https://docs.yjs.dev/) — shared types, automatic CRDT
  merge, and network/order independence.
- [Yjs license](https://github.com/yjs/yjs/blob/main/LICENSE) — MIT.
- [Automerge concepts](https://automerge.org/docs/reference/concepts/) —
  document/history model, repositories, and transport-agnostic sync protocol.
- [Automerge license](https://github.com/automerge/automerge/blob/main/LICENSE)
  — MIT.
- [RFC 6455 §5.4](https://www.rfc-editor.org/rfc/rfc6455#section-5.4) — ordered
  fragment delivery on a WebSocket connection.
- [python-websockets license](https://github.com/python-websockets/websockets/blob/main/LICENSE)
  — BSD-3-Clause.

## Revisit triggers

Reconsider a CRDT only if Chudware adds an independently mergeable data type
whose semantics are commutative (for example free-form comments or annotations)
and profiling shows the authoritative relay is the limiting factor. Do not put
feature geometry, constraints, mates, or revisions into a generic CRDT without
a new contract and geometry-invariant proof.
