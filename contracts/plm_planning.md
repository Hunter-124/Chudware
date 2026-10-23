# Contract: PLM Planning — `aicad.planning/1`

> **Additive companion to `aicad.plm/1`** (`contracts/plm.md`). Planning lives in
> its own schema and its own shadow-tree files, so the frozen PLM manifest is
> untouched. New OPTIONAL fields are additive; changing an existing field's
> meaning needs a version bump to `aicad.planning/2`.
>
> **Design source:** `docs/design/PLM_VERSION_CONTROL_PLAN.md` (2026-07-07). Native
> planning is the ground-up default; external trackers (GitHub/Jira/…) are a
> deferred import/sync seam (`services/plm/trackers.py`), not a dependency.

## 1. Where it lives

The planning store is three files under a `planning/` dir in the **same shadow
tree** as the design (`contracts/plm.md §2`):

```
planning/tasks.json        {"schema":"aicad.planning/1","tasks":[…]}
planning/issues.json       {"schema":"aicad.planning/1","issues":[…]}
planning/milestones.json   {"schema":"aicad.planning/1","milestones":[…]}
```

Because it is versioned with the project it is **restore-safe** (a Restore of an
earlier point brings the board back too), **sync-safe** (it travels on Sync), and
**release-frozen** (a Release snapshots it) for free. It is carried forward on
every whole-tree commit (`plm.md` Appendix B.2 carry-forward law).

## 2. Scope — planning entries stay out of the design timeline

A planning mutation cuts an ordinary history commit tagged **`scope:"planning"`**
(commit trailer `chudware-plm-scope: planning`). `plm_history` defaults to
`scope:"design"` and hides them, so the curated timeline a user watches shows
design changes only; `scope:"planning"` or `scope:"all"` surface the board's own
history. A planning commit never re-reads working design files, so it cannot
capture unsaved design edits.

## 3. Entities

**Actors** are objects `{kind, id, display}`. `kind:"profile"` is a local profile
name today; `kind:"org_member"` (id `org/user`) attaches when organizations land
— no migration. `null` = unassigned.

```json
task: {
  "id": "t<8hex>", "title": "...", "body": "...",
  "status": "todo|doing|done|blocked", "priority": "p0|p1|p2|p3",
  "assignee": {actor}|null, "labels": [], "milestone_id": "m…"|null,
  "links": [{"kind":"item","item_id":"i…"} | {"kind":"entry","commit":"…"} | {"kind":"issue","id":"b…"}],
  "external": {"tracker":"github","id":"#42","url":"…","synced_at":"…"}|null,
  "created_at": iso, "updated_at": iso, "created_by": {actor}|null
}
issue: {
  "id": "b<8hex>", "title", "body",
  "severity": "low|med|high|critical", "state": "open|closed",
  "assignee": {actor}|null,
  "found_in": {"item_id","rev?"}|null, "fixed_in": {"item_id","rev?"}|null,
  "links": [...], "external": {...}|null, "created_at","updated_at","created_by"
}
milestone: {
  "id": "m<8hex>", "title", "body", "status": "open|done", "due": iso|null,
  "release_link": {"item_id","rev?"}|null, "notes": "...", "created_at","updated_at","created_by"
}
```

Unknown enum values coerce to the safe default (`todo`/`p2`/`med`/`open`); unknown
keys are ignored and preserved (forward-tolerance).

## 4. Permission seam (allow-all in v1)

Every planning/release/lock mutation routes through
`services/plm/permissions.can(actor, action, resource)` — allow-all today. When
organizations arrive, coarse roles (viewer/contributor/maintainer/owner) over
resource classes (`item|history|release|planning|lock|settings|project`) resolve
here; **call sites do not change**. Permissions are meant to be easy to set up
(role presets, not raw ACLs) so a new hire can't get destructive access.

## 5. Tools

`plm_plan {path, op, entity, data?, id?}` → `{ok, items, entity?}` — the single
multiplexed planning tool (worker seam + MCP). `op` ∈ `list|create|update|delete`,
`entity` ∈ `task|issue|milestone`. `plm_overview {path}` → the hub dashboard
aggregate `{ok, dirty_items, recent_entries, open_tasks, open_issues,
next_milestone, counts, head, ai_mode}`. Reason codes reuse `plm.md §7`
(`invalid_project`, `invalid_mode`, `not_found`, `no_vault`).

## 6. External trackers (deferred)

`services/plm/trackers.py` defines a `Tracker` protocol (`pull_issues`,
`push_status`) with **no implementation** in v1. The `external` field on
tasks/issues reserves the per-entity link. Import/sync lands here later behind a
per-project tracker config, with no schema change.
