# PLM live-session protocol — `aicad.plm-live/1`

Status: **FROZEN v1** (2026-08-27).

This is an additive live-editing extension to `contracts/plm.md`. It applies
only when a project remote profile has `kind:"plm-server"` and capability
`"live_coedit"`. Folder, SSH, and HTTPS snapshot remotes continue to use `aicad.plm/1`
advisory locks and copy-as-variant collision preservation without behavioral
change.

Normative terms SHALL, MUST, SHOULD, MAY, and MUST NOT are requirements.

## 1. Authority and invariants

A live item session has exactly one authoritative server process and one total
order of committed operation batches.

1. The server SHALL assign a monotonically increasing unsigned 64-bit `seq` to
   every accepted batch. Sequence 0 is the joined snapshot before any live
   commit.
2. The server SHALL validate and apply a proposed batch through the existing
   Chudware document engine. It MUST NOT merge or patch canonical document JSON
   independently of the engine.
3. An operation batch is atomic. A client MUST install the resulting snapshot
   only between render frames and MUST NOT display an intermediate batch state.
4. All connected clients SHALL observe committed batches in increasing `seq`
   order. A gap triggers catch-up; it never permits applying a later batch.
5. Presence is ephemeral, best-effort data. It MUST NOT change document state,
   revision, checkpoint history, or release state.
6. `(participant_id, client_op_id)` identifies a proposal. Reconnect/retry of the
   same pair MUST return the original commit or rejection and MUST NOT apply it
   twice.
7. A remote-profile kind selects the mode. `plm-server` means live sessions and
   no advisory lock for that item while joined. Every other remote kind keeps
   the `aicad.plm/1` lock/sync behavior.

## 2. Transport, framing, and limits

The transport is WebSocket (`ws://` for explicitly local development,
`wss://` otherwise). Each WebSocket message contains one UTF-8 JSON object.

- Schema discriminator: `"schema":"aicad.plm-live/1"` on every message.
- Maximum message: 4 MiB. Maximum `ops` batch: 1 MiB canonical JSON and 1,000
  operations. A larger proposal is rejected before allocation/copy.
- The server SHALL accept no WebSocket extensions in v1; this avoids ambiguous
  intermediary fragmentation semantics.
- Server-to-client messages on one socket are sent in increasing `seq` order.
  The server's session mutex/queue establishes order across sockets.
- Heartbeat: ping every 20 seconds; disconnect after 60 seconds without a pong
  or application message. Presence expires with the connection.

## 3. Identity and authentication

A live server is deny-by-default.

- Non-loopback listeners REQUIRE a bearer token configured from a token file or
  `CHUDWARE_PLM_LIVE_TOKEN`. Tokens MUST NOT be stored in a project manifest,
  vault commit, URL query, log, or event payload.
- A client obtains the token from the OS secret store keyed by remote profile id
  (environment override is allowed for headless tests/administration).
- The server compares token bytes in constant time and closes unauthorized
  handshakes with code 4401.
- The participant identity is `{id,display,account_id}`. `id` and `display` are
  required non-empty strings; `account_id` is nullable/reserved. The server
  records identity from the authenticated join, never from later messages.
- One token MAY authorize several participants on a small self-hosted server;
  account-level authorization is an additive future capability, not implied by
  v1.

## 4. Session key and lifecycle

A session key is `(project_id,item_id)`. `project_id` and `item_id` come from the
`aicad.plm/1` manifest; filesystem paths are never authority.

### 4.1 Join

Client request:

```json
{
  "schema":"aicad.plm-live/1", "type":"join",
  "project_id":"p…", "item_id":"i…",
  "participant":{"id":"u…","display":"Sarah","account_id":null},
  "last_seq":12,
  "token":"<redacted-on-receipt>"
}
```

Successful response:

```json
{
  "schema":"aicad.plm-live/1", "type":"joined",
  "session_id":"s…", "project_id":"p…", "item_id":"i…",
  "head_seq":14, "engine_revision":27,
  "snapshot":{"encoding":"canonical-json","document":{}},
  "participants":[{"id":"u…","display":"Sarah","account_id":null}],
  "checkpoint_base":"<vault commit>"
}
```

The snapshot is mandatory on first join, when `last_seq` is absent, or when the
server no longer retains the requested sequence range. Otherwise the server MAY
send `joined` without `snapshot` followed by contiguous `commit` messages from
`last_seq + 1`.

Joining the same session twice with the same participant id replaces the older
socket after the new socket has caught up. This makes reconnect atomic from the
user's perspective.

### 4.2 Leave and session end

A client SHOULD send `{"type":"leave"}` before closing. Socket loss is an
implicit leave after the heartbeat timeout.

When the last participant leaves:

1. the server flushes the authoritative canonical document;
2. it writes exactly one vault checkpoint for the live session, even if several
   sockets disconnect concurrently;
3. checkpoint attribution contains the de-duplicated participants who committed
   at least one batch: `participants:[{id,display,account_id}]`;
4. the checkpoint message is `Live session: <N> committed batch(es)` unless the
   clients supplied a non-empty session note;
5. the session remains resumable from that checkpoint and sequence summary.

An empty session writes no checkpoint.

## 5. Document operations

Proposal:

```json
{
  "schema":"aicad.plm-live/1", "type":"propose",
  "session_id":"s…", "client_op_id":"01J…",
  "base_seq":14,
  "ops":[{"op":"add_feature","feature":{}}]
}
```

The server serializes proposals per session. `base_seq` is diagnostic/causal
context, not permission to reorder. If it is behind, the proposal is evaluated
after all intervening commits against current authoritative state. The engine
may accept it or return a typed semantic rejection.

Accepted broadcast (including to the proposer):

```json
{
  "schema":"aicad.plm-live/1", "type":"commit",
  "session_id":"s…", "seq":15, "client_op_id":"01J…",
  "author":{"id":"u…","display":"Sarah","account_id":null},
  "engine_revision":28,
  "ops":[{"op":"add_feature","feature":{}}],
  "snapshot":{"encoding":"canonical-json","document":{}},
  "state_hash":"sha256:<hex>"
}
```

The `snapshot` and `state_hash` are authoritative results of the whole batch.
Clients MAY use `ops` for animation/audit but MUST bind displayed state to the
snapshot/hash. This is the no-torn-preview rule.

Rejected response (only to proposer):

```json
{
  "schema":"aicad.plm-live/1", "type":"rejected",
  "session_id":"s…", "client_op_id":"01J…",
  "head_seq":15, "reason":"stale_reference",
  "hint":"A referenced feature no longer exists; refresh and choose it again."
}
```

A rejection consumes no sequence and mutates no state. Raw exceptions, paths,
stack traces, engine internals, and transport stderr MUST NOT appear in `hint`.

## 6. Catch-up and resynchronization

Client request:

```json
{"schema":"aicad.plm-live/1","type":"catchup","session_id":"s…","from_seq":9}
```

The server replies with contiguous `commit` messages through `head_seq`. If the
range is unavailable or the client's state hash disagrees, it sends:

```json
{
  "schema":"aicad.plm-live/1", "type":"snapshot",
  "session_id":"s…", "head_seq":15, "engine_revision":28,
  "snapshot":{"encoding":"canonical-json","document":{}},
  "state_hash":"sha256:<hex>"
}
```

A client seeing a gap, duplicate with different hash, or unknown session MUST
stop applying commits until catch-up/snapshot succeeds. It MUST NOT guess.

## 7. Presence

Client presence update:

```json
{
  "schema":"aicad.plm-live/1", "type":"presence",
  "session_id":"s…",
  "cursor":{"space":"viewport","x":0.0,"y":0.0,"z":0.0},
  "selection":{"kind":"face","selector":"feature:Pad/face:top"},
  "viewport":{"camera_hash":"sha256:<hex>"}
}
```

All fields after `session_id` are optional and forward-tolerant. The server
coalesces updates per participant and broadcasts at most 20 per second per
participant. Presence messages carry no document `seq`; clients may drop them.
A leave/timeout broadcasts `presence_left`.

## 8. Remote profile extension

`aicad.plm/1` remote profiles gain one additive kind:

```json
{
  "id":"r…", "name":"Workshop server", "kind":"plm-server",
  "url":"wss://plm.example.test/live",
  "capabilities":["snapshot_sync","live_coedit","presence"],
  "identity":{"user_id":"u…","display":"Sarah","account_id":null}
}
```

- Token material is external to this object.
- `live_coedit` suppresses `aicad.plm/1` advisory-lock actions only while an item is
  joined live. Snapshot sync/checkpoints still use the vault.
- Missing `live_coedit` means the client MUST use `aicad.plm/1` advisory locks even if the URL
  happens to point at a live-capable service.
- `chudware-cloud` remains reserved/unavailable and does not imply this kind.

## 9. Server process

The reference self-hosted process is:

```text
python -m services.plm.server --project <folder> \
  --listen 127.0.0.1:8765 [--token-file <0600-file>]
```

Binding a non-loopback address without `--token-file` or
`CHUDWARE_PLM_LIVE_TOKEN` MUST fail closed. The process owns no SaaS dependency,
uses the local hidden vault for snapshots/checkpoints, and may host multiple item
sessions inside the configured project only. Path traversal and a join for a
project/item absent from that vault are rejected.

## 10. Required proof

A conforming implementation MUST prove:

1. two clients submit interleaved semantic batches and receive identical
   `state_hash`/canonical state at every committed `seq`;
2. late join and reconnect catch up without duplicate application;
3. no client installs a partial batch snapshot;
4. simultaneous last-leave writes one checkpoint attributed to all committers;
5. dumb remote profiles still require `aicad.plm/1` advisory locks;
6. authentication, message-size, invalid-schema, sequence-gap, duplicate-id,
   and path-scope failures are fail-closed and plain-language.
