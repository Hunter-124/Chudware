> Superseded 2026-08-10: the streamed-session product described here was
> removed; the gateway now serves only the WASM `/app` client and the `/engine`
> bridge. There are no per-user containers — wherever this report says
> "containerized session" or "per-session container volume", the live system has
> one shared `chudware-hostd` daemon and per-account workspace roots enforced by
> `gateway/engine_bridge.py`. The vault/git analysis below is still accurate.

# PLM Multi-User Research Report

**Scope:** Map `services/plm/` + contracts for shared multi-user use across N containerized Chudware sessions. Read-only. Non-goals: GUI panels, doc schema internals.

---

## 1. Vault layout & concurrent access safety

### Layout
- Project = ordinary folder; vault = hidden bare-ish git repo at `<project>/.aicadvault/` (`contracts/plm.md:64-90`, `services/plm/gitbind.py:20-24,43,102-107`).
- Shadow tree per commit: `manifest.json`, `items/<item_id>/{document.json,meta.json,thumbnail.png,summary.txt}`, `geometry.step` only on releases (`contracts/plm.md:72-83`).
- Working CAD files live in the project folder; containers exploded in-memory on save, re-zipped on restore (`contracts/plm.md:85-90`).
- Per-machine unversioned config: `.aicadvault/config.json` (`services/plm/vault.py:407-432`).
- AI-VC deltas: `.aicadvault/ai_vc/` (and per-branch under `ai_vc/<branch>/`) (`contracts/ai_vc.md:13-15`, `services/plm/branches.py:280-286`).

### What locking exists today
| Mechanism | Scope | Status |
|---|---|---|
| `dulwich` `refs.set_if_equals(HEAD, expected, new)` | Vault history commits | **Built** — `gitbind.py:194-207` |
| `Vault.save` rebuild+retry (6×) on `ConcurrentUpdate` | Same | **Built** — `vault.py:191-216` |
| Advisory item locks at `refs/aicad/locks` | Team edit presence | **Contract only** — `plm.md:271-288`; `status()["locks"]=[]` stub `vault.py:124` |
| OS flock / file locks | Cross-process | **None** in PLM |
| Remote push CAS for locks/history | Multi-machine sync | **Contract only** §6.3; T-R7 |

### Concurrent multi-process safety (same `.aicadvault`)
- **HEAD CAS is real** and prevents silent history loss when two writers commit different trees (`gitbind.py:194-207`, `vault.py:191-216`). Comment: “No cross-process lock”.
- **Not sufficient for shared bind-mount:**
  1. Save rebuilds shadow tree from **working files + HEAD**. Two processes editing different items can still lose one process’s unsaved working-file edits if the other saves first and the loser rebuilds from disk that no longer matches its in-memory doc (or overwrites working files on restore).
  2. Object-store / pack writes are not coordinated beyond ref CAS; concurrent loose-object writes to the same bare repo are generally OK in git, but `repack`/`gc` via subprocess (`gitbind.py:212-229`) is unsafe concurrent with writers.
  3. `.aicadvault/config.json` is read-modify-write with **no CAS** (`vault.py:420-432`) → last writer wins for local settings.
  4. Working `.step+` containers and AI-VC store under `ai_vc/` have **no** multi-writer protocol.
- **Verdict:** Concurrent access from multiple processes on one vault is **partially safe for history append** (CAS), **unsafe for shared working tree / multi-user CAD**. Contract v1 assumes one writer per item via advisory locks + sync, not simultaneous writers on one checkout (`contracts/plm.md:233-238`, `work/backlog/T-BL15.md:71-72`).

---

## 2. Remote / sync protocol

### Contract (specified, not implemented)
- Remote profiles in `manifest.remotes[]`: kinds `folder | ssh | https | chudware-cloud` (`plm.md:242-259`, `manifest.py:29,51-52`).
- `chudware-cloud` reserved: tools must return `reserved_remote` (`plm.md:253-259`).
- Sync = fetch + linear replay of unpublished local commits + push with expected-old CAS; same-item collision → copy-as-new-item (`plm.md:280-310`).
- Locks: blob under `refs/aicad/locks` (`lock_ref` in manifest), CAS push (`plm.md:271-288`).
- Tools: `plm_add_remote`, `plm_sync`, `plm_lock`, `plm_unlock` (**T-R7**) (`plm.md:339-342`).

### Code reality
- **No** `services/plm/remote.py`, `locks.py`, `conflict.py`, or `server` (glob empty).
- MCP explicitly defers: “Remote/lock tools … are T-R7” (`services/mcp/plm_tools.py:18`).
- Binding: **dulwich primary** for vault; subprocess `git` optional for `gc`/`repack` only (`gitbind.py:5-12`, `docs/design/PLM_VERSION_CONTROL_PLAN.md` T-R1 note).
- **`gitscm.py` is a different surface:** drives system `git`/`gh` against the project’s real **`.git`**, NOT `.aicadvault/` (`gitscm.py:1-8`). Power-user “Git” tab: status/log/branches/remotes/fetch/pull/push (`gitscm.py:339-373`, `worker_rpc.py:548-620`). **Must not be confused with PLM vault remotes.**

---

## 3. How GUI / agent talk to PLM; who owns the vault

### Paths
1. **Worker RPC (GUI primary):** `services/plm/worker_rpc.py` `WORKER_METHODS` → registered in `services/worker_main.py:138-139`. Host ImGui calls `st.rpc.call("plm_*", {path: projectDir})` (`host/src/ui/plm/plm_state.cpp` pattern). Methods: open/status/history/save/checkpoint/release/restore/compare/item_*/where_used/set_ai_mode/plan/overview/settings/scheme/draft/timeline/rollback + `git_*` (`worker_rpc.py:618-668`).
2. **MCP tools:** `services/mcp/plm_tools.py` registered in `server.py:328-330`. Same vault implementation; agent/external clients.
3. **In-process client:** `services/plm/client.py` `PLMVaultClient` — agent `plm_bridge` AI checkpoints with §3.4 `ai` block (`client.py:1-18,86-139`; `contracts/plm.md` §7.1).
4. **AI-VC hooks:** `ai_vc.py` + agent_turn/guardrails; storage under vault `ai_vc/` (`contracts/ai_vc.md`).

### Ownership model today
- **Path-addressed, open-per-call:** `_with_vault` opens `Vault.open(path)`, runs op, `close()` (`worker_rpc.py:52-76`). No long-lived vault daemon.
- **Owner = whichever process has the project folder open** (desktop app’s engine worker, or MCP process). Multiple openers = multiple independent `Repo` handles on the same files.
- There is **no** central PLM server process in-tree (T-R8 not built).

---

## 4. T-R7 / T-R8 plan vs built

| Task | Intent | Status | Built? |
|---|---|---|---|
| T-R1 | Freeze contract + OSS survey | done (DAG) | Yes — `contracts/plm.md`, `docs/review/PLM_OSS_SURVEY.md` |
| T-R2–R4 | Vault core, change intel, items/releases | done | Yes — `services/plm/*` |
| T-R5 | In-editor GUI | doing | Partial GUI over existing RPCs |
| T-R6 | Project Hub | todo | Partial hub UI; no team chips wired to real locks |
| **T-R7** | Remotes, Sync, advisory locks, copy-as-variant | **todo** (`work/tasks/T-R7.md`, DAG:188) | **0% modules**; schema fields only (`remotes:[], lock_ref`) |
| **T-R8** | Self-hosted PLM server + live co-edit | **todo**, depends T-R7 (`work/tasks/T-R8.md`) | **0%**; live contract `aicad.plm-live/1` not frozen |
| T-BL15 | Original VC proposal | superseded by R | N/A |

### T-R7 planned deliverables (unbuilt)
- `remote.py`, `locks.py`, `conflict.py` NEW
- Identity = per-machine display name (OS user prefill); reserve account-id
- Two lock modes: (a) dumb remote soft chip + override; (b) leave transport seam for T-R8 live
- DoD: two checkouts via bare SSH + folder remote; CAS lock races; chudware-cloud refuse

### T-R8 planned deliverables (unbuilt)
- `aicad.plm-live/1`: join/leave, authoritative op relay, presence, snapshot catch-up
- `services/plm/server` websocket; session end → one vault checkpoint multi-author
- Mode selected by remote-profile type; dumb remotes keep T-R7 locks

---

## 5. Identity & permissions

### Author fields (built)
- `meta.json` author: `{kind: user|ai, id, display}` (`history.py:48-54`, `contracts/plm.md:139-168`).
- AI block: provider/model/run_id/artifact_id/reversible (`history.py:62-75`).
- **Default user is a stub:** `_user_author() → {kind:"user", id:"user", display:"User"}` (`vault.py:977-978`). No OS username, no OAuth id, no per-session identity injection on the vault path today.
- Client AI path: `{kind:"ai", id:"agent", display:"AI Assistant"}` (`client.py:119-120`).
- AI-VC provenance: process-local `_AUTHORED_BY` map agent|user|mixed (`ai_vc.py:96,129-155`) — **not multi-user identity**.

### Planning actors (schema ready)
- Tasks/issues use actor `{kind, id, display}` with `kind:"profile"` today; `org_member` reserved (`planning.py:14-18`).

### permissions.py
- `can()` always `True`; `require()` no-op (`permissions.py:28-42`).
- Resource classes: item/history/release/planning/lock/settings/project; actions view/edit/create/delete/release/administer (`permissions.py:20-25`).
- Design intent: coarse roles viewer/contributor/maintainer/owner when orgs land (`permissions.py:11-13`).
- **No user-id store, no ACL files, no Entra/Google linkage in PLM.**

### T-R7 identity plan
- Per-machine profile display name at first share; reserve account-id for later vendor accounts (`work/tasks/T-R7.md:75-77`).

---

## 6. N containers sharing one project bind-mount — what breaks

Assume N Chudware session containers all bind-mount the **same** project directory (working files + `.aicadvault`).

| Hazard | Why |
|---|---|
| Working-file races | Each session writes `.step+` / `.ecad+` directly; last write wins; no file-level lock |
| Restore vs edit | `plm_restore` rewrites working containers while another session has doc open in memory |
| History CAS thrash | Concurrent saves retry up to 6× then `write_conflict` (`vault.py:214-216`); under load users see failures |
| Lost semantic edits | CAS preserves commit DAG integrity but shadow tree is whole-project explode from disk — session A’s unsaved engine state is not in B’s save |
| AI-VC store races | Parallel delta chains under `ai_vc/` without CAS |
| `config.json` clobber | Unversioned RMW (`vault.py:420-432`) |
| `git gc` / repack | Optional accelerator unsafe concurrent with writers |
| gitscm `.git` | If a real `.git` also exists, concurrent index.lock failures on git tab ops |
| Watchers / dirty | Each process polls dirty via explode vs HEAD (`vault.py:904-928`); thrash + false dirty |
| Identity collision | All sessions stamp author id `"user"` — timeline attribution useless |
| Locks | Nonexistent — no “Sarah is editing” |

**Shared bind-mount of one checkout does not fit the contract model.** Contract v1 is **one working tree per user/machine + sync to remote** (`plm.md:233-238`, plan §3.3).

### Central bare + per-session clones (fits contract better)
Yes — this **is** the T-R7 design:
- Host: bare repo (or folder remote) at e.g. `/var/chudware/plm/<project>.git` (or shared `folder` kind path).
- Each session container: private clone/worktree of project + private `.aicadvault`.
- Session mutates only its clone; `plm_sync` push/pull with CAS + copy-as-new-item.
- Advisory locks on remote `refs/aicad/locks` for dumb remotes; T-R8 live server later for same-doc co-edit.

Until T-R7 is implemented, even clone+manual git of `.aicadvault` is unsupported (no `plm_sync`; gitscm does not operate the vault).

---

## RECOMMENDATION

### Winner: Central bare vault remote + per-session private checkouts + implement T-R7 seams

**Do this for multi-user web sessions (stays inside `aicad.plm/1`):**

1. **Host layout**
   - Shared **bare** git remote per project (folder kind on host disk is enough for single-workstation podman; ssh/https optional).
   - Per-session container volume: private project working tree + private `.aicadvault` (clone of bare, or `plm_open` then first sync).
   - **Never** bind-mount one live working tree into N sessions.

2. **Reuse existing seams (no contract bump)**
   - Storage/CAS: `gitbind.GitBinding` + `Vault.save` ConcurrentUpdate retry.
   - Manifest: `remotes[]`, `lock_ref`, `REMOTE_KINDS`, `LOCKS_SCHEMA` already in `manifest.py`.
   - Tool/RPC envelopes already named in §7: implement `plm_add_remote` / `plm_sync` / `plm_lock` / `plm_unlock` in `vault`/new `remote.py`+`locks.py`+`conflict.py`, wire MCP + `worker_rpc.WORKER_METHODS`.
   - Permissions: route lock/sync/release through `permissions.can(actor, …)` and pass real `actor` from session auth.
   - Author: replace `_user_author()` stub with session-injected `{kind, id, display}` (and reserved account-id) from OAuth (Entra/Google) at the worker/MCP edge — additive, no schema bump if fields stay the same shape.
   - Agent path: keep `PLMVaultClient` / AI-VC local to the session checkout.

3. **Additive gaps to implement (T-R7, still `aicad.plm/1`)**
   - Dulwich (or subprocess-git accelerator) **fetch/push** against folder/ssh/https remotes for `.aicadvault` (not gitscm’s project `.git`).
   - Lock manifest read/write on `refs/aicad/locks` with CAS + soft expiry.
   - Copy-as-new-item materialization on same-`item_id` document.json divergence.
   - Session identity provider hook (web OAuth → author + lock holder).
   - Optional: single host-side “sync helper” sidecar if containers must not speak git/ssh directly — still presents as a `folder`/`https` remote to the contract.

4. **Explicitly defer T-R8** for live same-part co-edit; web multi-user v1 = T-R7 semantics (locks + sync + copy-as-variant). Live op-relay needs new contract `aicad.plm-live/1`.

### Ranked alternatives

| Option | Verdict | Why (≤3) |
|---|---|---|
| **A. Central bare + per-session clones + T-R7** | **WIN** | Matches frozen §6; reuses Vault/CAS/manifest; scales to 3–6 users on one host |
| B. Shared bind-mount + flock around Vault | Lose | Breaks working-file isolation; fights CAS-only design; not in contract; restore/edit races remain |
| C. One shared PLM daemon all sessions RPC into | Later / T-R8-ish | Clean multi-user owner, but new process topology + not specified in aicad.plm/1; do after T-R7 or as T-R8 server |

---

## OPEN QUESTIONS

1. **Who runs `plm_sync`?** User button only (T-R7 UX) vs auto-sync on session start/end for headless web sessions?
2. **OAuth identity mapping:** exact shape of `author.id` (Entra oid vs email vs local username) and whether lock `holder` equals `author.id`.
3. **Project provisioning:** does the web control plane create the bare remote + seed clone, or does first user `plm_open` then `plm_add_remote`?
4. **Large binaries / LFS:** releases store `geometry.step`; any host disk quota / GC policy for N users?
5. **AI-VC in multi-session:** keep session-local only, or ever sync `ai_vc/` (today not in shadow-tree history commits as first-class sync unit)?
6. **gitscm Git tab in web product:** expose, hide, or point at vault remote? (Today it targets wrong repo.)

---

## Evidence index (quick)

- Vault bare-ish + dulwich: `services/plm/gitbind.py:20-24,102-107,194-207`
- Save CAS retry: `services/plm/vault.py:191-216`
- Locks stub: `services/plm/vault.py:124`
- Author stub: `services/plm/vault.py:977-978`
- Permissions allow-all: `services/plm/permissions.py:28-36`
- Remotes schema only: `services/plm/manifest.py:29,51-52`
- gitscm ≠ vault: `services/plm/gitscm.py:1-8`
- MCP T-R7 gap: `services/mcp/plm_tools.py:18`
- Worker registration: `services/worker_main.py:138-139`
- Contract §6: `contracts/plm.md:233-342`
- T-R7/R8 cards: `work/tasks/T-R7.md`, `work/tasks/T-R8.md`
- DAG status: `work/DAG.md:187-189`
