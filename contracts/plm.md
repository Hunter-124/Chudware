# Contract: PLM / Version Control — `aicad.plm/1`

> **Frozen — changes require a version bump to `aicad.plm/2`.** Append-only per
> the `contracts/README.md` frozen-seam policy; any change to an existing field's
> meaning, or to the lock/remote protocol, needs maintainer sign-off and a new
> schema string. New OPTIONAL fields and new `reserved` tool slots are additive
> and do not bump the string.
>
> **Contract-first.** This file is frozen BEFORE the storage engine
> (`services/plm/`) and the `plm_*` MCP tools. Design source of truth:
> [docs/design/PLM_VERSION_CONTROL_PLAN.md](../docs/design/PLM_VERSION_CONTROL_PLAN.md).
> Supersedes the earlier PDM-lite design.
>
> **Builds on `contracts/doc_schema.md`.** History snapshots are the
> **canonical `document.json`** (Appendix A: sorted keys, lossless numbers, **no
> timestamps**, indent=2, UTF-8 no-BOM). Semantic diffing reuses the existing
> `doc_diff` tool; visual compare reuses `eval_compare`. This contract does NOT
> re-specify either — it specifies only the vault, manifest, history, lock, and
> remote seams around them.
>
> **OSS-first.** Git binding: **dulwich (Apache-2.0
> arm), primary; subprocess `git` optional accelerator; pygit2 declined.**
> Machine-checked by `services/plm/canonical.py`; fixtures in
> `contracts/fixtures/plm/`.

## 0. Conventions & the byte-stability law

- **Canonical numbers/units** follow `doc_schema.md §0` (mm, deg, g).
- **IDs.** Document `id` is the UUID4 from `doc_schema.md §1`. A PLM **`item_id`**
  is a stable, PLM-owned string `"i" + <8 hex>` minted at first check-in of a
  document into a project, recorded in `manifest.json`. `item_id` is **distinct
  from** the document UUID: copy-as-new-item (§6.4 conflict guard) and
  save-as-new mint a **new** `item_id` while the underlying document may keep or
  change its UUID, so item identity can fork without colliding.
- **History-entry id** is the git commit id of the shadow-tree commit (§3). No
  separate entry id is minted; the commit is the entry.
- **THE BYTE-STABILITY LAW (normative, inherited from doc_schema Appendix A).**
  `document.json` MUST remain byte-identical across saves/rebuilds that do not
  change the model, and MUST NOT contain wall-clock time, author, attribution,
  or any per-save mutable field. **All mutable per-entry metadata — timestamp,
  author, user-vs-AI attribution, checkpoint/release message, entry kind — lives
  ONLY in `meta.json`, the git commit metadata, and/or `manifest.json`; NEVER in
  `document.json`.** A producer that writes any of these into `document.json`
  violates this contract and breaks `doc_diff` false-positive-freeness.
- **MCP envelope.** Every `plm_*` tool returns the flat result envelope: `{ok:
  true, …}` on success, `{ok:false, reason:"<code>", hint:"<prose>"}` on failure
  — **never a traceback**. Reason codes are enumerated in §7.

## 1. Overview — the three user nouns → the storage model

| User noun (plan §1) | Storage reality |
|---|---|
| **Project** | A folder + a hidden `.aicadvault/` git repo (§2). |
| **History** | The git commit DAG (linear in v1); one commit per explicit save (§3). |
| **Checkpoint** | A history commit tagged `kind:"checkpoint"` with a message (§3.2). |
| **Release** | A history commit tagged `kind:"release"` that also freezes geometry + BOM and stamps a rev letter + lifecycle in `manifest.json` (§3.2, §5). |
| **Restore** | A **new forward** commit (`kind:"restore"`) whose tree equals a prior tree — history is never rewritten (§3.3). |

Nouns explicitly NOT surfaced in v1 (plan §6): branch, merge, staging, conflict,
lock administration. There is no user-facing git vocabulary anywhere (§8).

## 2. Vault layout — `.aicadvault/` and the shadow tree

```
MyProject/                       # the folder the user sees
  Bracket.step+                  # working files, exactly as today (doc_schema §1)
  MainAssembly.step+
  Controller.ecad+
  .aicadvault/                   # HIDDEN per-project git repo (never surfaced)
```

`.aicadvault/` is an ordinary (bare-work-tree-less) git repository. Its committed
**shadow tree**, per history commit, is:

```
manifest.json                    # aicad.plm/1 project manifest — §4
items/<item_id>/document.json    # canonical parametric JSON, EXPLODED from the container
items/<item_id>/meta.json        # mutable per-item metadata (§3.4) — timestamps live here
items/<item_id>/thumbnail.png    # small render for the timeline
items/<item_id>/summary.txt      # plain-language change summary (doc_diff + narration)
items/<item_id>/geometry.step    # frozen B-rep — PRESENT ONLY IN RELEASE COMMITS (§5)
```

- **Containers exploded on check-in, re-assembled on restore.** `.step+`/`.ecad+`
  are ZIPs (git-hostile); on check-in the container is unpacked to
  `document.json` (canonical, §0) + `thumbnail.png`; on restore they are
  re-zipped into the working file. The working files in `MyProject/` are the
  live artifacts; the shadow tree is the versioned form.
- **`geometry.step` is derived + recomputable**, so it is committed **only at
  releases** — this keeps checkpoint commits tiny (JSON + PNG) while making a
  released rev exact even if the rebuild engine later evolves.
- **A commit is whole-tree; unchanged items reuse their blobs.** An explicit save
  typically touches one item, so only that item's ~3 small blobs are rewritten;
  every other item points at its existing content-addressed blob. The per-editor
  timeline (plan §4) is a **derived filtered view** over which item paths changed
  in each commit — not separately stored.

## 3. History-entry semantics

### 3.1 When a commit is cut

- **On every explicit user save** (not autosave — autosave keeps its separate
  crash-recovery ring, plan §3.5). One commit covers the **entire** shadow
  tree at that instant.
- Commit **kind** is recorded in the commit metadata and mirrored into
  `meta.json` (§3.4); it is one of **`auto` | `checkpoint` | `release` |
  `restore`**.

### 3.2 The four entry kinds

| kind | Cut by | Message | Geometry.step frozen? | Manifest side-effects |
|---|---|---|---|---|
| `auto` | Any explicit save | none (empty) | no | none |
| `checkpoint` | User "Checkpoint" button, or AI in AI-managed mode | Copilot-drafted, user-editable | no | none |
| `release` | User "Release" flow (pre-flight checks pass) | required (release note) | **yes**, for released item(s) | rev letter++, lifecycle → `Released`, prior rev → `Superseded`, BOM freeze, ref pins (§5) |
| `restore` | User "Restore" of a prior entry | auto ("Restored to <when>") | matches restored source | pointers move forward only; nothing rewritten |

### 3.3 Restore is always a new forward entry (normative)

Restoring history entry `E` creates a **new** commit whose shadow tree equals
`E`'s tree, appended at HEAD. The DAG is never rewound, amended, rebased, or
force-pushed. This is what makes "you can always go back" safe: restore itself is
undoable by restoring the pre-restore entry. **No `plm_*` tool may rewrite,
delete, amend, or reorder history.**

### 3.4 `meta.json` — the mutable metadata home (NOT in document.json)

```json
{
  "item_id": "i0a1b2c3",
  "entry_kind": "checkpoint",
  "authored_at": "2026-07-03T18:20:05Z",
  "author": {"kind": "user", "id": "alex", "display": "Alex"},
  "message": "Increased wall 3→5 mm, added 4× M6 bolt circle",
  "ai": null,
  "doc_uuid": "6f0c…",
  "rev": "B",
  "lifecycle": "In work"
}
```

- `entry_kind` ∈ `auto | checkpoint | release | restore`.
- `author.kind` ∈ `user | ai`. For an AI-cut entry (`author.kind:"ai"`), `ai`
  carries the attribution + reversibility record:

```json
"ai": {"provider": "anthropic", "model": "claude-fable-5", "adapter": null,
       "run_id": "run_…", "artifact_id": "a7f3…", "reversible": true}
```

  `artifact_id`/`run_id` join to the telemetry `ai_artifact` envelope so
  every AI action is attributed and one-click reversible (§8). `reversible` is
  always `true` in v1 (every entry is discardable by restore).
- Timestamps live **only** here (and in git commit metadata), never in
  `document.json` (§0 law).

## 4. `manifest.json` — the project registry (`aicad.plm/1`)

One per project, at the shadow-tree root, re-committed with every history entry.

```json
{
  "schema": "aicad.plm/1",
  "project": {"id": "p3f9…", "name": "Gearbox", "created_at": "2026-07-01T…"},
  "part_number_prefix": "PRJ",
  "ai_mode": "assisted",
  "items": [
    {
      "item_id": "i0a1b2c3",
      "doc_uuid": "6f0c…",
      "kind": "part",
      "path": "Bracket.step+",
      "part_number": "PRJ-0001",
      "rev": "B",
      "lifecycle": "In work",
      "released_revs": [
        {"rev": "A", "commit": "9ab…", "released_at": "2026-07-02T…",
         "lifecycle": "Superseded"}
      ]
    }
  ],
  "bom_freezes": [
    {"assembly_item": "i9f8e7d6", "rev": "A", "commit": "9ab…",
     "pins": [{"item_id": "i0a1b2c3", "rev": "A"}]}
  ],
  "remotes": [ /* §6.1 */ ],
  "lock_ref": "refs/aicad/locks"
}
```

- **`part_number_prefix`** — default from project name; `part_number` is
  auto-assigned `"<prefix>-NNNN"` (editable). Uniqueness within the project is
  enforced.
- **`kind`** ∈ `part | assembly | drawing | ecad | cam | requirements |
  deliverable` (mirrors the doc kinds the project groups, plan §1).
- **`rev`** — current working rev letter. Rev letters increment `A → B → …` at
  each **release** (§5). "In work" state carries the *next* rev being edited.
- **`lifecycle`** ∈ **`In work` | `Released` | `Superseded`** — the fixed,
  minimal state machine (plan §1). No custom states, no configurable transitions.
  Transitions: `In work → Released` (on release), and releasing rev N
  auto-`Released → Superseded` on rev N−1. `Superseded` is terminal for that rev.
- **`released_revs`** — the pinned history of releases per item, each pointing at
  its release commit (which carries the frozen `geometry.step`).
- **`bom_freezes`** — per assembly release, the exact component-rev pin set (BOM
  freeze snapshot), feeding the existing `bom_rollup` machinery (plan §3.4).
- **`ai_mode`** ∈ `manual | assisted | managed` (plan §2); default `assisted`.
- **Where-used** is **derived** from assembly component refs + manifest, not
  stored (plan §3.4).
- **Forward-tolerance** (like doc_schema §7): readers MUST reject an unknown
  major schema string (`aicad.plm/2`), MUST ignore unknown keys and preserve
  them on resave, and MUST emit every required key.

## 5. Release, rev letters, BOM freeze

A **release** (`kind:"release"` commit) is atomic and does all of:

1. **Pre-flight checks** (pass required, else `{ok:false, reason:"preflight_failed"}`):
   no stale refs (assembly using a superseded component rev), verify battery not
   failing, no unsaved working changes.
2. **Rev stamp.** `manifest.items[item].rev` increments (`A` on first release);
   the rev letter is stamped on the drawing title block.
3. **Lifecycle.** Item → `Released`; the prior released rev → `Superseded`.
4. **Geometry freeze.** `items/<item_id>/geometry.step` is written into this
   commit (the only commits that carry it) — a released rev is byte-exact
   forever, engine-evolution-proof.
5. **BOM freeze** (assemblies). The component reference set is pinned to exact
   revs and recorded in `manifest.bom_freezes`.

Releases are never un-done by rewriting history; a mistaken release is corrected
by a new release of a new rev (forward-only).

## 6. Sharing, remotes, and the advisory-lock / CAS protocol

Team concurrency v1 = **advisory locks + sync, no merging** (plan §3.3). "Sync"
= pull others' commits, then push yours; if the same item was edited on both
sides, the loser's state is preserved as **copy-as-new-item** — never a merge UI,
never data loss.

### 6.1 Remote profiles

`manifest.remotes[]` entries:

```json
{"id": "r1", "kind": "folder", "url": "/mnt/share/Gearbox.git", "name": "Team NAS"}
{"id": "r2", "kind": "ssh",    "url": "git@host:gearbox.git",     "name": "Self-host"}
{"id": "r3", "kind": "https",  "url": "https://git.example/gearbox.git", "name": "Gitea"}
{"id": "r4", "kind": "chudware-cloud", "url": null, "name": "Chudware Cloud", "reserved": true}
```

- **`kind`** ∈ **`folder` | `ssh` | `https` | `chudware-cloud`**.
- `folder` = a bare git repo on a filesystem path (shared drive / USB, plan §3.3
  Tier "Shared folder"). `ssh`/`https` = any git remote the user controls (bare
  repo over SSH, Gitea, GitHub, …) — self-hosting for free.
- **`chudware-cloud` is RESERVED, config-only.** It MAY appear in the manifest as a
  configured entry, but **no `plm_*` tool implements sync against it in
  `aicad.plm/1`** — every remote tool MUST return
  `{ok:false, reason:"reserved_remote", hint:"Chudware Cloud is not available yet"}`
  for a `kind:"chudware-cloud"` remote. Implementation is behind the **Pillar-12
  human gate** (plan §3.3, non-goal §6). The seam exists so the config surface is
  stable; the behavior is deliberately absent.

Credential-bearing remote URLs are rejected before a link or remote can enter
new history. A destination carried by versioned metadata is not network
authorization: sync requires explicit local approval and otherwise returns
`remote_destination_unapproved`.

Opening a legacy project with unsafe remote metadata exposes redacted,
read-only diagnostics; it does not silently repair or commit. Use
`plm_add_remote` to explicitly approve a clean replacement. Repair appends a
forward entry, preserves the original historical bytes, and permits ordinary
checkpoint/sync afterward. It does not revoke credentials or close an exposure
incident; those require separate owner action.

### 6.2 The lock manifest

Locks are **advisory** (they warn; they do not hard-block a determined offline
editor — data is never lost, only flagged). They live in a blob under a
**dedicated git ref** `refs/aicad/locks` (name in `manifest.lock_ref`), separate
from the history DAG so lock churn never pollutes history:

```json
{
  "schema": "aicad.plm.locks/1",
  "locks": [
    {"item_id": "i0a1b2c3", "holder": "sarah", "display": "Sarah",
     "acquired_at": "2026-07-03T18:00:00Z", "expires_at": "2026-07-03T22:00:00Z"}
  ]
}
```

- `expires_at` — advisory soft-expiry so a crashed client's lock self-heals; the
  UI shows "Sarah is editing this part" while live.

### 6.3 Atomic-push CAS (compare-and-swap) protocol

Both history push and lock update use **non-fast-forward push rejection as the
CAS-fail signal** (works identically on dulwich and subprocess-git):

**History push (sync "push yours"):**
1. `fetch` the remote. If remote HEAD did not move since your last sync, goto 3.
2. If remote HEAD moved, your unpushed local commit(s) and the remote have
   **diverged** (you both appended). To keep history **linear + no-merge**, take
   the fetched remote HEAD as the new base and **replay your unpushed local
   commit(s) forward on top of it**. A same-item collision during replay is
   resolved by copy-as-new-item (§6.4) — never a merge. (This forward-replay of
   **never-published** local commits is the one operation §8.3's never-rewrite
   guarantee does NOT cover — see the normative carve-out there. Published/shared
   commits are still never rewritten.)
3. `push` local HEAD with **expected-old = the fetched remote HEAD**.
4. If the remote rejects (non-fast-forward, i.e. someone else pushed during the
   replay race) → **CAS failed** → goto 1 (bounded retry). If it accepts → done.

**Lock acquire (update `refs/aicad/locks`):**
1. `fetch refs/aicad/locks`; parse the lock manifest.
2. If the item is already locked by another live holder → `{ok:false,
   reason:"locked", hint:"Sarah is editing this part"}`.
3. Else add your lock entry and `push` the new lock blob with **expected-old =
   the fetched lock-ref commit** (CAS). Non-fast-forward rejection → someone
   raced → refetch + retry (bounded). Accept → lock held.

Lock release is the same CAS, removing the entry. All CAS retries are bounded;
exhaustion returns `{ok:false, reason:"contended"}`.

### 6.4 Copy-as-new-item conflict guard

If a pull would fast-forward but the **same `item_id`** was edited on both sides
(divergent blob for that item's `document.json`), the incoming remote version
wins the canonical path and the **local** version is preserved as a **new item**:
new `item_id`, name suffixed (`"Bracket (Sarah's edits)"`), added to the
manifest. **No content is ever merged or discarded.** This is the entire "merge"
story in v1 — there is no merge UI and no conflict markers.

## 7. `plm_*` MCP tool surface

All registered by `services/mcp/server.py`, all returning the §0
envelope. Signatures take one object; responses are the shape shown.

| tool | request | success response | role |
|---|---|---|---|
| `plm_init` | `{path}` | `{ok, project_id, manifest}` | Zero-config: create `.aicadvault/`, seed `manifest.json`, register existing docs. Idempotent (re-init on an existing vault just opens). |
| `plm_open` | `{path}` | `{ok, project_id, manifest}` | Open a project folder; auto-`plm_init` if no vault exists (zero-config, §8). |
| `plm_save` | `{project_id, kind?}` | `{ok, commit, entry}` | Cut a history entry over the whole shadow tree. `kind` ∈ `auto|checkpoint` (default `auto`). **`release` is NOT accepted here** — releases require an item + note and go through `plm_release`; passing `kind:"release"` returns `{ok:false, reason:"invalid_mode"}`. |
| `plm_checkpoint` | `{project_id, message?, item_id?}` | `{ok, commit, entry}` | Convenience for `plm_save kind:"checkpoint"`; drafts `message` from `doc_diff` if omitted. |
| `plm_release` | `{project_id, item_id, message}` | `{ok, commit, rev, bom_freeze?}` | Run pre-flight (§5), freeze geometry+BOM, stamp rev+lifecycle. |
| `plm_history` | `{project_id, item_id?, limit?}` | `{ok, entries:[…]}` | The timeline (filtered per item if given): each entry = `{commit, kind, authored_at, author, message, thumbnail_ref, summary}`. |
| `plm_restore` | `{project_id, commit, item_id?}` | `{ok, commit, entry}` | Forward-only restore (§3.3). |
| `plm_compare` | `{project_id, commit_a, commit_b, item_id}` | `{ok, doc_diff, eval_compare?}` | **Reuses** `doc_diff` (semantic) + `eval_compare` (visual); this tool orchestrates, it does not re-specify diffing. |
| `plm_status` | `{project_id}` | `{ok, items:[…], head, dirty, ai_mode, remotes, locks}` | Project state for the Hub/badges. |
| `plm_set_ai_mode` | `{project_id, mode}` | `{ok, ai_mode}` | `mode` ∈ `manual|assisted|managed` (§4). |
| `plm_add_remote` | `{project_id, kind, url?, name}` | `{ok, remote}` | Register a remote profile (§6.1); `chudware-cloud` accepted config-only. |
| `plm_sync` | `{project_id, remote_id}` | `{ok, pulled, pushed, conflicts_copied:[…]}` | One-button sync (§6.3–6.4). `chudware-cloud` → `reserved_remote`. |
| `plm_lock` | `{project_id, item_id, remote_id}` | `{ok, lock}` | Advisory lock via CAS (§6.3). |
| `plm_unlock` | `{project_id, item_id, remote_id}` | `{ok}` | Release a lock via CAS. |

**Reserved (named, NOT specified by `aicad.plm/1`)** — slots so the tool family +
enums are stable; do not implement under this contract:
`plm_branch`, `plm_merge`, `plm_where_used` (where-used is derived; its dedicated
tool is reserved for the project Hub), `plm_cloud_login` (Pillar-12 gate).

**Reason codes** (`reason` field): `no_vault`, `invalid_project`, `invalid_item`,
`preflight_failed`, `stale_ref`, `unsaved_changes`, `locked`, `contended`,
`reserved_remote`, `remote_unreachable`, `invalid_mode`, `not_found`.

### 7.1 In-process `VaultClient` seam — `services/plm/client.py` (normative)

The `plm_*` MCP tools in §7 are the **user/agent-driven** surface (one object in,
the envelope out). Separately, in-process AI machinery — specifically the
**PLM bridge** (`services/agent/plm_bridge.py`, contract
`advisor.md §15 aicad.plmlink/1`) — needs a thin Python client, **not** a round
trip through MCP, to land AI-authored milestone checkpoints with full attribution.
That client is a normative seam of `aicad.plm/1` so the vault provides exactly what the
already-shipped bridge imports; it is not a parallel API.

- **Class + import path (frozen):** `services.plm.client.PLMVaultClient`. The
  bridge imports it lazily behind the `CHUDWARE_PLM_BRIDGE=1` feature flag and falls
  back to its in-memory `MockVault` when the import fails (vault unavailable) or
  the flag is unset. `services/plm` MUST provide `PLMVaultClient` at exactly this dotted
  path with the surface below; renaming either is a breaking change (version bump).
- **Surface (frozen — must satisfy the `VaultClient` Protocol in `plm_bridge.py`
  verbatim):**

```python
class PLMVaultClient:  # duck-types services.agent.plm_bridge.VaultClient
    def is_available(self) -> bool: ...
    def checkpoint(self, project_id: str, message: str,
                   item_id: str | None = None,
                   author_kind: str = "ai",
                   run_id: str | None = None,
                   artifact_id: str | None = None) -> dict: ...
    def project_id_for_run(self, run_id: str) -> str | None: ...
```

- **`checkpoint(...)` is the AI-authored twin of `plm_checkpoint` (§7) and the
  ONLY path that writes the §3.4 `ai` attribution block.** It cuts a
  `kind:"checkpoint"` entry over the shadow tree exactly like `plm_checkpoint`,
  and additionally, when `author_kind=="ai"`, stamps `meta.json.author.kind:"ai"`
  and the `ai` block (§3.4) from `run_id` (→ `ai.run_id`) and `artifact_id`
  (→ `ai.artifact_id`; the bridge passes the `buildgraph_node` here), joining the
  telemetry envelope. It returns the **same success shape as
  `plm_checkpoint`**: `{ok, commit, entry}` where `entry` carries at minimum
  `{commit, kind, authored_at, author, message}` (and the `ai` block for AI
  entries). Failure returns the §0 envelope `{ok:false, reason, hint}`.
  **The MCP `plm_checkpoint` envelope (§7) is NOT widened to carry
  `run_id`/`artifact_id`/`author_kind`** — the AI-attribution parameters live only
  on this in-process seam; the two surfaces stay deliberately distinct (§8.4 keeps
  AI attribution + reversibility a MUST on every AI-cut entry regardless of which
  seam cut it).
- **`is_available()`** returns `True` only when the vault module is importable AND
  a vault can be opened/served; when it returns `False` the bridge queues its
  `plmlink` events in the ledger and flushes on availability (advisor.md §15
  "no lost annotations"). A `PLMVaultClient` MUST NOT raise on unavailability —
  it reports `False`.
- **`project_id_for_run(run_id)` is BEST-EFFORT, not a mandated registry.** v1
  MAY return `None`; there is no required run→project backing store in
  `aicad.plm/1`. The bridge's primary path passes `project_id` explicitly to
  `on_milestone(...)`, and `project_id_for_run` is only the fallback lookup. The vault
  MAY back it with an in-vault run→project map if convenient, but callers MUST NOT
  depend on it resolving; a `None` return with no explicit `project_id` simply
  queues the link. Making this a mandatory registry is a future (`aicad.plm/2`)
  concern, deliberately deferred so it is not re-litigated against a frozen v1.

## 8. Ease-of-use invariants (NORMATIVE — MUST/SHALL)

These are contract obligations on every producer/consumer of `aicad.plm/1`, not
UX suggestions. They implement plan principle #1 ("users never *learn* version
control").

1. **No user-facing git vocabulary. SHALL NOT.** No surface reachable by a user
   (GUI label, tooltip, `plm_*` response prose, error `hint`, narration) may use
   the words *git, commit, repository, repo, branch, merge, rebase, HEAD, ref,
   stage, staging, push/pull, SHA/hash*. Users see **Project / History /
   Checkpoint / Release / Restore / Compare / Sync** and plain-language change
   summaries only. (Internal ids like `commit` in *machine* tool envelopes are
   fine; they MUST NOT be rendered to users as-is.)

2. **Zero-config project init. MUST.** Opening a folder MUST make it a project
   with working history and no setup step: `plm_open` MUST auto-create the vault
   if absent. There MUST be no required configuration — no init wizard, no
   tracked-file selection, no staging, no lifecycle authoring, no admin screen —
   before history works. A project on day one is a folder; the vault is built in.

3. **History is never rewritten. MUST.** Restore MUST be a new forward entry
   (§3.3); no tool may amend, delete, reorder, squash, rebase, or force-update
   history. "You can always go back" is a guarantee, not a best-effort.
   **Carve-out (sync only):** the guarantee protects **published/shared**
   history — any commit that has been pushed to a remote or pulled from one. A
   commit that exists **only locally and has never been shared** MAY be replayed
   forward onto a freshly-synced remote base during `plm_sync` (§6.3 step 2) to
   keep shared history linear. This replay never touches a shared commit and
   never loses content (collisions become copy-as-new-item, §6.4). No other
   operation may rewrite any commit.

4. **AI actions are attributed and reversible. MUST.** Every history entry cut by
   AI (AI-managed mode, or a copilot task) MUST carry `author.kind:"ai"` + the
   `ai` attribution block (§3.4) joining to the telemetry envelope, MUST be
   visibly marked as AI-authored in the timeline, and MUST be reversible in one
   step (restore of the prior entry). AI versioning is an **assist tier layered
   on the manual buttons, never an exclusive mode** (plan §2); manual controls
   are always available regardless of `ai_mode`.

5. **Automatic safety history is not policy. SHALL.** Auto-history (`kind:"auto"`
   on explicit save) SHALL run in all AI modes including `manual` — it is a
   safety net, not an AI feature, and MUST NOT require opt-in.

## 9. Relationship to existing contracts (no regressions)

- **`doc_schema.md` Appendix A:** snapshots ARE the canonical
  `document.json`; the byte-stability law (§0) is inherited verbatim. No conflict
  — this contract only adds sibling files (`meta.json`, `manifest.json`) that are
  explicitly exempt from byte-stability (they carry timestamps, as Appendix A
  directs: "Timestamps belong in `meta.json` only").
- **`doc_diff` / `eval_compare`:** reused, not redefined (§7 `plm_compare`).
- **`doc_ops.md`:** ops remain transient (no persistent op log); PLM is
  snapshot-based (plan §3.1). Undo/redo stay session-scoped and are NOT PLM
  history (plan §3.5).
- **Autosave:** unchanged crash-recovery ring; PLM entries cut on explicit
  save only, so the timeline stays curated (plan §3.5).
- **Configurations (`doc_schema.md §9`):** remain the variants mechanism; PLM
  versions the whole document including its configurations (plan §3.5, §6).
- **Telemetry (`telemetry.md`):** AI PLM actions route through the existing
  `ai_artifact` envelope; §3.4 `ai` block references its `artifact_id`/`run_id`.
- **PLM bridge (`advisor.md §15` `aicad.plmlink/1`):** the already-shipped
  `services/agent/plm_bridge.py` is a **consumer** of this contract, not a parallel
  one. Its agent milestone checkpoints (`plan_ratified`, `part_done`,
  `mission_done`, `stage_exit:{part_verify,assembly,sim}`, `change_request`) land
  as ordinary `kind:"checkpoint"` history entries (§3.2) via the §7.1 in-process
  `PLMVaultClient.checkpoint(...)` seam; each `aicad.plmlink/1` record's
  `checkpoint_ref` is the returned `entry.commit`. The bridge's Pillar-19
  git-vocabulary guard is the same normative rule as §8.1. No PLM-side schema is
  owed to the bridge beyond the §7.1 seam and the §3.4 `ai` block it fills.
```

## Appendix B — additive extensions (2026-07, still `aicad.plm/1`)

Per the frozen-seam policy (top of file), the following are **additive only** —
new OPTIONAL fields and new tool slots. They do NOT change any existing field's
meaning, the lifecycle machine, the lock/remote protocol, or the frozen
`ITEM_KINDS` enum, so the schema string stays `aicad.plm/1`. Planning (below) is
carried on its **own** schema `aicad.planning/1` in a sibling shadow-tree dir so
this manifest schema is untouched.

**B.1 Document-type taxonomy (`services/plm/doctypes.py`).** Each manifest item
MAY carry an OPTIONAL `doc_type` refining its frozen `kind` without widening the
enum. Absent `doc_type` = a legacy CAD item, read from `kind` as before (§4
forward-tolerance). Values + the frozen `kind` each maps onto:

| `doc_type` | maps to `kind` | shadow storage |
|---|---|---|
| part/assembly/drawing/ecad/cam | (same) | container explode (§2, unchanged) |
| `spec` | requirements | `items/<id>/file/<name>` verbatim |
| `report`/`datasheet`/`spreadsheet`/`dataset`/`image`/`document` | deliverable | `items/<id>/file/<name>` verbatim |
| `sim_study`/`software` | deliverable | `items/<id>/tree/<rel>` + `tree.index.json` |
| `linked_repo` | deliverable | `items/<id>/link.json` (`aicad.plm.link/1`, reference only) |

`doc_type` detection is a *suggestion* (`detect_doc_type`), always user-retaggable
via `plm_item_set`, and falls back to `document` — never an error.

**B.2 The carry-forward law (normative for generic items).** Because a history
commit is whole-tree (§2), generic-item blobs (`file/`, `tree/`, `link.json`) and
any sibling versioned dir (e.g. `planning/`) MUST be re-emitted on **every**
commit — re-read from the working copy when present, else carried forward from
the prior tree verbatim. Omitting them drops them from that commit's tree and a
later restore would lose them. `geometry.step` is the sole exception (release
commits only, §5) and is never carried forward.

**B.3 Additive tool slots** (worker seam + MCP, append-only per §7):

| tool | request | response |
|---|---|---|
| `plm_item_list` | `{path, doc_type?, kind?}` | `{ok, items:[{…item view, doc_type?, dirty}]}` |
| `plm_item_add` | `{path, file_path?\|folder_path?, doc_type?, part_number?}` | `{ok, item}` |
| `plm_item_untrack` | `{path, item_id}` | `{ok, item}` (registry-only; history retained) |
| `plm_link_repo` | `{path, url, name?, checkout_path?}` | `{ok, item}` |
| `plm_where_used` | `{path, item_id, transitive?}` | `{ok, consumers}` (promotes the §7 reserved slot) |
| `plm_settings_get`/`plm_settings_set` | `{path[, settings]}` | `{ok, settings}` (per-machine, unversioned) |
| `plm_plan` | `{path, op, entity, data?, id?}` | `{ok, tasks\|issues\|milestones\|entity}` (planning — `aicad.planning/1`) |
| `plm_overview` | `{path}` | `{ok, dirty_items, recent_entries, open_tasks, open_issues, next_milestone, head, ai_mode}` |

`plm_history` additionally accepts an OPTIONAL `scope` (`design`\|`planning`\|`all`,
default `design`); `plm_compare` returns `mode:"generic"` (with an optional
`text_diff`) for generic items instead of `doc_diff`. Reason codes unchanged (§7).

**B.4 Derived-cache exclusion, auto-record, and recursive tracking (2026-07 VCS
integration).** All additive; schema string stays `aicad.plm/1`.

- **Derived container caches are excluded from the shadow tree.** The engine
  embeds `cache.brep`, `cache.aicm`, and the `cache.json` header into the live
  `.step+`/`.ecad+` working file (`core/doc/session.cpp`) for a cold-open fast
  path. These are derived + recomputable (like `geometry.step`) and are stripped
  at the vault boundary — `explode_container` skips any entry whose name starts
  with `cache.` — so they are **never committed**. Restore reassembles a
  cache-less container and the engine rebuilds them on open. Old history entries
  that already contain cache blobs keep them (history is never rewritten, §3.3);
  the size relief applies to new commits.
- **`auto_commit`** — an OPTIONAL manifest key (`bool`, default `true`,
  forward-tolerant `m.get("auto_commit", True)`), persisted like `ai_mode` (§4).
  Surfaced in the `plm_settings_get` / `plm_settings_set` payloads and in
  `plm_status` (`auto_commit` field). When true, the GUI records each save into
  history automatically.
- **Recursive working-directory tracking.** Container auto-registration scans the
  project folder **recursively** (skipping any dot-prefixed path component:
  `.aicadvault`, `.plm`, `.git`, …). Manifest item `path` is the POSIX path
  relative to the project root (e.g. `sub/deep/Part.step+`); restore recreates the
  nested directory.
- **B.4 additive tool slot:**

| tool | request | response |
|---|---|---|
| `plm_record_outside` | `{path}` | `{ok, committed}` (+`commit, entry` when committed) — cut ONE `auto` entry capturing on-disk changes made outside the editor; no-op (`committed:false`) when the working tree already matches history |

**B.5 Graphical diffs + the recipe/solved storage line (2026-07).** All additive;
schema string stays `aicad.plm/1`. No new shadow-tree path, no new lifecycle state.

*The line itself, stated once and normatively.* The **recipe is the source code**
and the **solved model is the build artifact**. `document.json` (canonical, §0) is
the sole tracked source of truth: reverting it and rebuilding reproduces the model
exactly, so nothing derived from it earns vault disk. Concretely, at the vault
boundary `explode_container` drops:

| dropped | why |
|---|---|
| `cache.json`, `cache.aicm`, `cache.brep` | tessellation + B-rep cold-open caches (B.4) |
| `studies/<id>/results.*` | **solved** FEA/CFD result sets — capped at 20 MB *each* in the live container, and regenerable by re-running the study |

and keeps `studies/<id>.json` — a study **definition** (material, fixtures, loads,
mesh density) is recipe, not result. `geometry.step` stays the single deliberate
exception (§5.4): frozen into **release commits only**, so a released rev is
byte-exact forever even if the rebuild engine later evolves. `plm_release` now
actually produces those bytes (`services/plm/release.py:freeze_geometry_step`
rebuilds the recipe and calls the engine's `doc_export_step`); the freeze is
**best-effort** — no engine, or an unbuildable recipe, means the release still
succeeds and reports `geometry_frozen: false`.

*Graphical diffs.* `plm_compare` still orchestrates rather than re-specifies (§7),
but its OPTIONAL `visual` half now returns **rendered images of the difference**,
not merely two renders side by side. Added is always what exists in **B** (newer)
only; removed is what existed in **A** (older) only. Colours are fixed so every
surface — engine render, Python vector differ, GUI legend — agrees:

| meaning | RGB | hex |
|---|---|---|
| added | `41, 184, 82` | `#29B852` |
| removed | `219, 61, 61` | `#DB3D3D` |
| unchanged | `158, 163, 171` | `#9EA3AB` |

- **Parts / assemblies** route through the new engine RPC `doc_render_diff`
  (`core/doc/session.cpp`), which reuses `eval_compare`'s shape resolution and the
  same OCCT booleans `compare_shapes` already computes — `Common(a,b)` unchanged,
  `Cut(a,b)` removed, `Cut(b,a)` added — and then *renders* them instead of
  discarding the shapes. Unchanged material is drawn **ghosted** so an interior
  change (a new bore, a deepened pocket) is still visible; `mode:"changes_only"`
  drops it entirely. Every view and both modes share ONE camera framing, computed
  from `A ∪ B`, so the images register and can be flipped between without shift.
- **Drawings and ECAD layouts** route through the pure-Python vector differ
  (`services/plm/visual_diff.py`), which set-diffs quantized strokes and paints
  them in a shared frame. ECAD needs **no engine at all** — the stored document
  carries the whole layout — so a board diff works on a machine without
  `libchudware`. A moved element correctly shows as removed-red *plus* added-green.
- A registered pixel diff is the universal fallback and replaces the former 50%
  alpha blend, which was an overlay, not a diff.

`eval_compare` remains the response key (§7). Its additive OPTIONAL fields:
`identical`, `added_volume_mm3`, `removed_volume_mm3`, `diff_3d`,
`diff_3d_changes_only`, `diff_3d_views` (`{ISO,FRONT,TOP}`), `diff_2d`,
`diff_pixel`, `counts`. Every path is an absolute PNG that exists on disk; a
render that failed yields `null`, never a broken path. The block still degrades to
`{degraded: true, reason, hint}` — with the semantic `doc_diff` half unaffected —
when the engine is absent, and an engine predating `doc_render_diff` degrades to
`match_pct` + `diff_pixel` rather than failing.

**Diff renders are themselves solved state and are NEVER committed.** They are
written to a caller-supplied `out_dir` (a temp dir by default) and are disposable.

**B.5 additive tool slot** — none. `plm_compare` gains an OPTIONAL `views`
(subset of `ISO`/`FRONT`/`TOP`), an OPTIONAL `out_dir`, and an OPTIONAL
`annotate` (default `true`). `annotate` bakes the view name, the legend and the
bbox dims into the rendered pixels, which is what an agent reading a bare PNG
needs; a caller that draws those as real widgets (the GUI) passes `false` and
gets a clean image.

**B.6 `plm_history` per-item timeline is creation-anchored (normative
clarification, 2026-07).** No signature change. Given an `item_id`, an entry is
returned iff **the item exists at that commit AND its content differs from the
content at that commit's parent**. Consequences a consumer may rely on:

- The oldest entry is the item's **creation**; nothing precedes it, so it is a
  terminal step rather than a comparable one.
- Every adjacent pair `(entries[i+1], entries[i])` is a valid
  `plm_compare {commit_a, commit_b}` argument — the item is present at both ends.
- No entry predates the item, and no commit that left the item untouched appears.

This was previously ill-defined and the implementation walked newest-first,
recording the newest commit still *carrying* a content rather than the one that
*introduced* it. That credited changes to unrelated later commits (dropping the
commit that actually made the change) and emitted pre-creation entries, so a
consumer diffing adjacent entries could hit `invalid_item` / "that item is not
present at one of those points". Project-wide history (no `item_id`) is
unaffected: it still returns one entry per scope-matching commit.

## Appendix C — the version graph (additive, 2026-08)

§1 called the vault history "the git commit DAG (**linear in v1**)" and §7 reserved
`plm_branch`/`plm_merge` as *do not implement*. Reality diverged: `services/plm/branches.py`
writes genuine two-parent merge commits into `refs/aicad/branches/<name>`, and the GUI has
exposed branch create/switch/merge from the PLM overview panel. This appendix
records the surface that already exists and the additive reader/decoration layer built on
top of it. It changes no existing signature and no existing response key.

**C.1 The store was a DAG that was READ as a line.** `gitbind.log()` follows `parents[0]`
only, so every commit reachable only through a merge's SECOND parent was absent from
`plm_history`, `plm_overview` and `plm_compare` — merged-in work was invisible, not merged
away. `plm_history` keeps its first-parent walk (its per-item creation-anchored semantics in
B.6 depend on it); the full topology is exposed by the new reader below.

**C.2 Named versions — `refs/aicad/versions/<name>`.** A new ref namespace, plus the side
table `.aicadvault/versions/meta.json` `{versions:{<name>:{name,commit,created_at,author,message}}}`.
A named version is an immutable label on a commit — the Onshape "V1/V2" noun. It is NOT a
lifecycle state: item lifecycle remains exactly `In work → Released → Superseded` (§5), and
releases are still discovered from `manifest.items[].released_revs[].commit`.

**C.3 New history entry kind `merge`.** `ENTRY_KINDS` gains `merge`; merge commits are
stamped `chudware-plm-kind: merge` instead of masquerading as checkpoints. Consumers that
switch on entry kind MUST treat an unknown kind as `auto` rather than failing.

**C.4 Release pin trailer `chudware-plm-pin: 1`.** A release is two commits (the release
itself, then the `manifest.json` back-fill that pins the rev to its commit, §5). The
back-fill now carries this trailer so a renderer can collapse the pair into one node. Legacy
vaults with no trailer are recognised structurally (a commit whose tree differs from its
parent only in `manifest.json`).

**C.5 Additive tools.**

| tool | request | success response |
|---|---|---|
| `plm_version_graph` | `{path, limit?=400, scope?="all"}` | `{ok, head, active_branch, truncated, nodes:[…], branches:[…], versions:[…]}` |
| `plm_version_tag` | `{path, commit?, name?, message?}` | `{ok, name, commit, created_at}` |
| `plm_version_list` | `{path}` | `{ok, versions:[…]}` |

A graph node is `{commit, parents:[…], kind, scope, authored_at, author, ai, message, summary,
thumbnail_ref, refs:[{type:"branch"|"version"|"release"|"head", name}], release:{item_id,rev}|null, pin}`.
`nodes` is newest-first AND topologically consistent: a node always precedes every one of its
parents, so a consumer can assign lanes in one forward pass. The walk starts from ALL branch
tips + `HEAD` + every version ref, so nothing reachable is hidden. `branches` rows carry
`ahead`/`behind`/`merge_base` relative to the active branch tip.

`plm_version_tag` auto-allocates the next `V<n>` when `name` is omitted; a duplicate name is
`{ok:false, reason:"not_found"}`-style refusal, never an overwrite — labels are immutable, in
keeping with the history-is-never-rewritten MUST (§8).

**C.6 What is still deliberately absent.** No release-candidate or approval lifecycle state
(§5's three states are frozen). No conflict-resolution state: `plm_branch_merge` still commits
a dst-favoured tree and returns the conflict list for the caller to act on. Team sync remains
linear-with-forward-replay (§6) — a merge node is a LOCAL construct and is never pushed as one.

## Appendix D — design-pipeline participant attribution (additive, 2026-08)

The §7.1 in-process `VaultClient.checkpoint` seam gains one OPTIONAL argument:
`item_artifacts: {item_id: artifact_id}`. The MCP `plm_checkpoint` envelope
remains unchanged.

For an AI-authored whole-project checkpoint, the client constructs the ordinary
§3.4 AI block once per mapped item, changing only `artifact_id` to the value for
that item. Unmapped items retain the checkpoint-level `artifact_id`. User-authored
entries ignore the map. `Vault.save`/`Vault.checkpoint` carry this internally as
`ai_by_item`; it is not written into `document.json` and therefore preserves the
byte-stability law.

This is the multi-part rule: a shared design-pipeline boundary may produce
one Project History point while each changed item's timeline still joins to the
participant buildgraph node that produced it.

## Appendix E — self-hosted live collaboration (additive, 2026-08)

Remote profiles gain one OPTIONAL kind, `plm-server`, with capabilities
`snapshot_sync`, `live_coedit`, and `presence`. Its WebSocket live-session
behavior is frozen separately as `aicad.plm-live/1` in
[`contracts/plm_live.md`](plm_live.md).

This extension does not alter §6 dumb-remote sync or locks. Folder, SSH, and
HTTPS profiles still use advisory locks and automatic copy-as-new-item
collision preservation. A `plm-server` profile suppresses that advisory lock
only while an item is joined to an authenticated live session. Tokens are
external secrets and MUST NOT enter `manifest.json` or vault history.

## Appendix F — Explorer working changes (additive, 2026-09)

`plm_item_list` keeps its existing `items` fields and adds a `change` string on
each row: `"M"` for working content different from HEAD, `"A"` for an item not
yet present in HEAD, `"D"` for a tracked file missing from disk while HEAD still
contains its content, or `""` when unchanged. `dirty` retains its existing
meaning for older consumers. The response also adds `untracked:[{path,
change:"U"}]` for valid, unregistered native design containers found under
the project root; these rows have no `item_id` and are not registered by a
status read. Paths are project-relative POSIX paths. This is vault state, not
Git staging: the Explorer cannot stage a subset or discard a file through this
response. A named `plm_checkpoint` records the project as a whole.

When a missing native container is checkpointed, its document blob is absent
from the new HEAD; the registry row remains for history and its working change
becomes clean. Restoring an earlier entry can recover the prior content.
Generic files follow the B.2 carry-forward law instead: a missing generic file
still compares as deleted until its tracked working content is restored.

Revision strings and lifecycle belong to the item registry, not the filename.
Two physical native files with distinct revision-suffixed names remain separate
items until explicitly reconciled; a UI MUST NOT silently delete or merge them
based only on their names.
