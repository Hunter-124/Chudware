# Corporate Roles — authoritative design (`aicad.corporate/1`)

> **Dated design authority, not a current as-built specification.** This records the
> 2026-07-16 first cut and proposed lifecycle changes; "live", "missing" and the setting
> layout below refer to that snapshot. It does not establish that the listed gaps remain
> open or that all proposed org behavior shipped. Current governance is in
> `services/agent/corporate.py` / `mission_dispatch.py`; online turns and nested agents
> follow [Chudcode integration](../dev/chudcode-integration.md).
> Preserve the distinction from `services/agent/org.py`; do not replace either capability
> on the strength of this historical prose.

Status: **design** (2026-07-16). Grounded on first-cut commit `f538b36c` and live code in
`services/agent/corporate.py`, `coordination.py`, `delegation.py`, `mission_dispatch.py`,
`role_models.py`, `config.py`, `test_corporate.py`, `contracts/agent_events.md`, and the
Corporate Roles setting in `host/src/ui/settings_modal.cpp`.

This document is the design of record for Corporate Roles **org mode** — how Chudware's copilot
runs complex multi-domain missions as a company instead of a flat spawn tree. The first cut
landed a real data model + enforcement core; this design freezes intent, fills missing lifecycle
pieces, and lists concrete gaps before we deepen the feature.

**Related but distinct:** `services/agent/org.py` (`aicad.org/1`) is the mission-pipeline
deliverable clustering layer (departments = deliverable clusters + interface channels). Corporate
Roles is the **subagent orchestration** layer for the copilot. They should share vocabulary and
eventually hand off (see §10), but they are not the same system.

---

## 1. Goals and non-goals

### 1.1 Why an org

Flat delegation (`delegate` / `DelegationTree`) is correct for small work: one orchestrator, a
handful of role cards, coordination-board claims to avoid clobber. It fails as mission width grows:

| Failure of flat mode | Org mode answer |
|---|---|
| Every worker talks to the root; root context fills with noise | Enforced channel ranks: workers ↔ head, heads ↔ director |
| Parallel specialists edit the same doc/feature | Coordination board claims + dept-scoped ownership |
| No middle management → root micro-manages every leaf | Department heads plan, spawn, digest, and relay |
| No shared progress language | Meeting digests with fixed schema |
| Model thrash on multi-domain briefs | Bounded context per seat (dept goals + tools + channels only) |

**Primary goals**

1. **Division of labor** — map mission work into a small set of departments with clear ownership.
2. **Bounded context per agent** — each seat sees only its rank-appropriate channels, tools, goals,
   and recent digests — not the whole mission transcript.
3. **Communication discipline** — routing is *enforced* (reject illegal routes), not advisory.
4. **No clobbering** — claim-before-edit via the coordination board; corporate mode makes that
   etiquette first-class in preambles and meeting risk digests.
5. **Director stays LLM-light** — template-first alignment; spend a model call only when
   misalignment or blockage is detected.
6. **Opt-in, flat-safe** — when off, or when work is single-department, behavior is identical to
   today's flat delegation.

### 1.2 When it activates vs flat delegation

Activation predicate (all must hold):

1. `agent.corporate_mode == true` (`config.corporate_mode_enabled`).
2. Mission task set decomposes into **≥ 2** departments after cap/merge
   (`corporate.should_use_corporate` / `derive_departments`).
3. A run has a `run_id` and a `MissionDispatch` (or equivalent) that can bind an `OrgChart`.

Otherwise: flat mode. Single-dept missions (e.g. pure geometry sketch+extrude) stay flat even with
the toggle on — org overhead is not free.

**Force override:** tools/tests may pass `force=True` to activate for evaluation; production
mission start should never force past the dept-count gate.

### 1.3 Non-goals

- Not a replacement for `aicad.org/1` deliverable clustering / interface-change protocol.
- Not a free-form multi-agent chat room; channels are rank-gated and kind-typed.
- Not unlimited nesting: still bound by `agent.max_subagent_depth` (default 2).
- Not automatic human-in-the-loop org design; departments derive deterministically from tasks.
- Not a new model marketplace — head/worker models still resolve through `role_models`.
- Not cross-run org persistence as a product feature (optional sidecar is an implementation detail).

---

## 2. Org lifecycle

```text
 mission intake
      │
      ▼
 department derivation  ── cap + merge policy ──► departments[]
      │
      ▼
 head seating (persona + role/model/thinking)
      │
      ▼
 director seat binds to root orchestrator (depth 0)
      │
      ▼
 heads hire workers (spawn authority, depth budget, role mix)
      │
      ├──── team-lead promotion when workers > threshold
      ▼
 work loop (claim → edit → progress → completion)
      │
      ├──── meetings (cadence / blockage)
      │         └─► director review → steering relay
      ▼
 department completion
      │
      ▼
 org wind-down (artifacts, drop chart, release claims)
```

### 2.1 Mission intake

Inputs available at activation:

- `run_id`
- task list (DAG nodes, deliverable plan rows, or free-form `{role, task_class, stage, brief}`)
- mission goals / brief (strings)
- optional bound `CoordinationBoard`
- optional `DelegationTree`
- config: `agent.corporate_mode`, `agent.corporate.*`, `agent.max_subagent_depth`

**Design requirement:** activation is **automatic** at mission start when the predicate holds —
not only when a model remembers to call `corporate_activate`. The tool remains for re-plan and
tests; the runner owns the first build.

### 2.2 Department derivation

Canonical catalog (priority order when capping) — `corporate.DEPARTMENT_CATALOG`:

`geometry`, `simulation`, `manufacturing`, `documentation`, `electronics`, `firmware`,
`sourcing`, `intake`, `general`.

Classification order for each task (`classify_task_department`):

1. explicit `role` / `role_id` / `assigned_role` via `ROLE_TO_DEPT`
2. `task_class` / `class` / `kind` / `deliverable_kind` / `type` via `TASK_CLASS_TO_DEPT`
3. `stage` / `phase` / `stage_scope` via `STAGE_TO_DEPT`
4. free-text keyword fallback on brief/name/title/description/goal/id
5. else `general`

**Cap + merge policy** (`derive_departments`, `max_departments` default 3):

1. Bucket all tasks by department id.
2. Rank by catalog order.
3. If `len(buckets) ≤ max_departments`, keep all.
4. Else keep highest-priority non-general depts up to the cap; merge overflow into `general` if
   present in keep-set, else into the last kept dept.
5. Emit only depts that still hold tasks; each carries `{id, name, tasks, roles}`.

**Design refinements (not in first cut):**

- Prefer merge **peers** (e.g. `firmware`→`electronics`) over dumping into `general` when a
  `MERGE_AFFINITY` table says so.
- Re-derive on replan (`chart_revision++`), keeping existing head seats for surviving dept ids
  (already partially true in `build_from_tasks`).
- Optional: weight depts by task count so a 1-task intake dept loses to a 12-task geometry dept
  when capping (today priority is catalog order only).

### 2.3 Head seating

For each derived department:

| Field | Value |
|---|---|
| `agent_id` | `head_{dept_id}_{run_id[:8]}` (stable within run) |
| `rank` | `head` |
| `role` | default `department_manager` (first cut); design: allow dept-specialized head roles |
| `parent_id` | director |
| `status` | `forming` → `active` when live agent starts |
| depth | 1 |

**Persona / model / thinking**

- Resolve model via `role_models.resolve_role_model(role="department_manager" | specialized, …)`
  with task_class reflecting the department's dominant work (e.g. geometry head →
  `multi_feature_part` / `assembly`; simulation head → `analysis`).
- Thinking: `resolve_role_thinking` / `classify_subagent_thinking` for the head role — heads are
  planners, so default **medium/high**, never the cheap-leaf one-shot path.
- System preamble: `OrgChart.preamble_for(head_id)` + coordination board preamble + role card.

**Director seat**

- `agent_id = director_{run_id[:12]}`, rank `director`, role `orchestrator`, depth 0.
- The director is **not** a separate process by default: it is the root mission orchestrator
  *wearing* the director persona (preamble + restricted tools). Optional future: a dedicated
  director subagent when the root is a thin UI shim.

### 2.4 Head hiring workers

Authority: only `rank ∈ {head, team_lead}` may call `corporate_spawn_worker`
(`OrgChart.spawn_worker`, `MissionDispatch.corporate_spawn_worker`).

On spawn:

1. Depth check: head→worker = depth 2; team_lead→worker would be depth 3.
   **Effective depth cap (binding, local only):** when team leads are enabled and
   the spawner is a `team_lead`, the corporate spawn path uses
   `effective_cap = max(agent.max_subagent_depth, 3)` so lead→worker seats are
   allowed even when the global nesting default is 2. This is computed **inside**
   the corporate spawn path only (`effective_corporate_depth_cap`); the global
   `agent.max_subagent_depth` config is **never mutated** (it still governs flat
   delegation). Head→worker continues to use the stored global cap as-is.
2. Create `Seat` with role, parent, optional `delegation_node`.
3. Add to `dept:<id>` channel membership + department `workers[]`.
4. If a `DelegationTree` is bound, `delegate(role, brief, budget_tokens, parent_id=…)` and bind
   the node id back onto the seat; tag node with `corporate_agent_id/rank/dept` so
   `_run_subagent` injects the right preamble/tools.
5. Role mix rationale (head prompt requirement): spawn the **narrowest** role card that can do the
   slice; prefer catalog roles over `department_manager` for leaves; one worker per independent
   claimable resource when possible.

**Team-lead promotion** (`team_leads` config, threshold `_TEAM_LEAD_THRESHOLD = 3`):

- After a worker spawn, if worker count > 3 and no lead exists, promote the lexicographically first
  worker id to `team_lead` (deterministic). Design keeps this; optional later: promote the worker
  with most completions, not first id.
- Team lead: same dept channel; may spawn workers under the effective-depth rule above
  (`max(global_cap, 3)` when team_leads on).

### 2.5 Work loop

Per worker turn:

1. Read dept channel + board snapshot (preamble already has board).
2. `coordinate_claim` on resources before mutating geometry/files.
3. Do the slice (engine tools).
4. `corporate_channel_post(kind=progress|question|blockage|handoff)` on dept channel.
5. `corporate_record_completion` (or blockage) so cadence/meeting machinery runs.
6. `coordinate_release` on exit (success or fail).

Heads:

- Monitor dept channel; answer questions; re-spawn on failure; hold meetings; relay steering.
- Do **not** own long feature edits when workers exist — heads plan and integrate.

Director:

- Read heads channel; consume digests; template review; optional LLM steering; never message
  workers directly.

### 2.6 Meetings

See §5.

### 2.7 Director review

See §5.3.

### 2.8 Wind-down

Design lifecycle end-states (missing from first cut — must be implemented):

1. **Dept completion:** head posts final digest (`trigger=completion`, all tasks done, blocked=[]).
   Seat statuses → `done`. Release all claims held by dept agents.
2. **Org completion:** all depts `done` (or failed with director-accepted residual). Director
   writes a final ledger artifact `{event: corporate_org_finished, chart: snapshot}`.
3. **Teardown:** `drop_org_chart(run_id)` on run finish/cancel; clear process registry; optional
   JSON sidecar retain for replay.
4. **Artifacts retained:** digests, reviews, channel log summary, final snapshot — via ledger
   and/or `{run_id}.corporate.json` sidecar (mirror coordination board pattern).

---

## 3. Personas and prompts

Every seated agent receives a corporate preamble (`OrgChart.preamble_for`) **appended** to the
role-card system prompt (already done in `MissionDispatch._run_subagent`). Design content
requirements by rank:

### 3.1 Common preamble fields (all ranks)

- Schema tag: `[Corporate org mode — aicad.corporate/1]`
- `agent_id`, `rank`, `dept_id` (if any), `role_card`
- Allowed channels (explicit list)
- Mission goals relevant to this seat (director: all; head: dept slice + global goals;
  worker: assigned brief + 1–3 mission goals)
- Claim discipline: claim before edit; on CONFLICT wait/narrow; never hard-deadlock
- Escalation ladder (see §3.5)

### 3.2 Director preamble

Must contain:

- You are oversight only; heads report via digests on `heads`.
- You do **not** message workers; illegal routes are rejected.
- Template review runs first; request LLM steering only when `needs_llm` is set.
- Tools: `corporate_channel_read(heads)`, `corporate_chart`, `coordinate_board` (no spawn).
- Objective: keep departments aligned to mission goals; unblock; stop thrash.

### 3.3 Head preamble

Must contain:

- Department objective + task roster (ids/briefs).
- Spawn policy: `corporate_spawn_worker` within depth cap; role-mix rationale.
- Meeting duty: every `meeting_cadence` completions or on blockage → `corporate_hold_meeting`.
- Relay duty: when director posts `kind=steering` with `meta.to_dept == your dept`, post a
  worker-facing summary on `dept:<id>` within the same turn.
- Tools: spawn, hold_meeting, record_completion, channel post/read, coordinate_*, delegate.
- You integrate worker outputs; you escalate only via heads channel.

### 3.4 Team lead preamble

- Subset of head: intra-dept sync, may spawn only if depth allows, may record completion.
- Cannot post to `heads` (enforced by `_allowed_channels`).
- Reports to department head; surfaces blockage early.

### 3.5 Worker preamble

- Post progress/questions **only** on `dept:<id>`.
- Direct messages to director / `heads` are **REJECTED**.
- Escalate via department head (question or blockage kind).
- Tools: channel post/read (dept), record_completion, coordinate_*.
- Finish with `corporate_record_completion` so meetings fire.

### 3.6 Escalation ladder

```text
worker  →  team_lead (if any)  →  head  →  director  →  human (ask_user / autonomy gate)
```

Rules:

- Skip-level contact is rejected by the channel router (worker→director).
- Heads may mark `blocked` on a digest to force director attention.
- Human escalation only from director (or head if director_review is off), via existing
  `ask_user` / autonomy paths — never invent a new human channel.

---

## 4. Communication protocol

### 4.1 Channel model

| Channel | Members | Purpose |
|---|---|---|
| `heads` | director + all department heads | Digests, steering, cross-dept coordination |
| `dept:<dept_id>` | head + team lead + workers of that dept | Progress, questions, handoffs, relayed steering |

No other channels in v1. Invented names fail membership checks.

### 4.2 Message kinds

| Kind | Who typically posts | Meaning |
|---|---|---|
| `progress` | worker, head | Slice advanced; mirrored to coordination board |
| `question` | worker | Needs head decision; mirrored to board |
| `blockage` | worker, head | Hard stop; triggers meeting; mirrored to board |
| `handoff` | worker, head | Ownership transfer of a resource/claim (design: first-class) |
| `digest` | head | Structured MeetingDigest body on `heads` |
| `steering` | director | Alignment note on `heads` (`meta.to_dept`) |
| `info` | any allowed | Free-form non-actionable |

Body hard cap: 2000 chars (already enforced in `post_message`).

### 4.3 Enforced routing rules

Implemented in `OrgChart.post_message` / `read_channel`:

1. Unknown agent → reject.
2. Worker/team_lead → `heads`, `director*`, or meta targeting director → **`status=rejected`**.
3. Channel must be in `_allowed_channels(agent)`.
4. Agent must be a channel member.
5. Read path applies the same allow-list when `agent_id` is supplied.

Director may post only on `heads`. Heads: `heads` + own dept. Workers/leads: own dept only.

### 4.4 Coordination board integration

Corporate mode **does not replace** the board (`coordination.CoordinationBoard`); it layers on it:

- Progress/question/blockage posts mirror to `board.post_progress` (best-effort).
- Preambles instruct claim-before-edit.
- Meeting auto-digest treats questions as `risks` and blockages as `blocked`.
- Design requirement (gap today): heads should surface active **CONFLICT** claims for their
  workers' resources in digests (`risks`), and director steering should prefer re-scope over
  force-takeover.
- Cross-department claim conflicts: the second dept must post a `question` on its dept channel;
  head escalates on `heads` with `kind=info|blockage` including conflict holder/intent; director
  arbitrates via steering (never silent overwrite).

### 4.5 Claim etiquette (normative)

1. Before mutating a doc/feature/path: `coordinate_claim(resources, intent)`.
2. On `CONFLICT`: do not retry-spin; post `question` or wait; narrow resources.
3. On completion/fail: `coordinate_release`.
4. Handoff: releaser posts `handoff` with resource list; receiver claims before edit.

---

## 5. Meetings

### 5.1 Cadence and triggers

Config: `agent.corporate.meeting_cadence` (default 5 worker completions per department).

Triggers (`MEETING_TRIGGER_*`):

| Trigger | When |
|---|---|
| `completion` | `completions_since_meeting >= meeting_cadence` |
| `blockage` | `record_worker_completion(..., blocked=True)` |
| `manual` | Head calls `corporate_hold_meeting` / `hold_meeting` |

Counters are **per department**, reset on meeting.

### 5.2 MeetingDigest schema

```json
{
  "schema": "aicad.corporate/1#digest",
  "dept_id": "geometry",
  "head_id": "head_geometry_…",
  "done": ["…"],
  "in_progress": ["…"],
  "blocked": ["…"],
  "risks": ["…"],
  "trigger": "completion|blockage|manual",
  "worker_completions": 12,
  "ts": "ISO-8601Z"
}
```

Auto-fill (`_auto_digest_fields`) when fields omitted: scan last 30 dept messages by kind/keywords.
Heads should prefer explicit lists when calling `hold_meeting`.

Posted to `heads` as `kind=digest` with `meta.digest`.

### 5.3 Director alignment algorithm (template-first)

`_director_review_unlocked(digest)` — keep deterministic first:

1. If `blocked` non-empty → `aligned=false`, `needs_llm=true`.
2. If `risks` ≥ 3 → soft misalignment, `needs_llm=true`.
3. Goal keyword coverage: for each mission goal, take up to 3 significant tokens (≥4 chars);
   if digest blob non-empty and none of the tokens appear → note possible objective drift;
   if also blocked → `aligned=false`, `needs_llm=true`.
4. Blockage meeting with empty done/in_progress → investigate, `needs_llm=true`.
5. If not aligned: emit **template steering** string (prioritize unblock, address risks, re-align
   to goals, instruct head to relay). If aligned: no steering.

**When to spend an LLM call** (design; first cut only sets the flag):

- Only if `director_review` is on **and** `needs_llm` is true **and** a director LLM budget
  remains.
- Input: mission goals, digest, last N heads messages, board conflicts snapshot.
- Output: short steering note (≤ 400 tokens) replacing or refining the template string;
  still posted as `kind=steering` on `heads` with `meta.to_dept`.
- If LLM fails: keep template steering (never block the org on director model errors).

### 5.4 Steering propagation head → workers

Design (partially missing):

1. Director posts steering on `heads`.
2. Owning head, on next turn (or immediately if head is synchronous in-process), posts a
   worker-facing paraphrase on `dept:<id>` as `kind=info` or `kind=steering` with
   `meta.from_director=true`.
3. Optional automation: when review produces steering, org code may auto-post a relay message
   as the head (system-authored, still attributed to head_id) so workers see it without waiting
   for a head LLM turn. Prefer auto-relay for blockage-triggered steering.

---

## 6. Failure modes

| Mode | Detection | Policy |
|---|---|---|
| **Dead head** | Head agent failed/timeout; no digest past 2× cadence wall-clock | Reseat: new head agent same `agent_id` or `head_{dept}_r{n}`; inherit workers/channel; director note on `heads`. Max 2 respawns then fold dept into `general` or fail mission slice. |
| **Stuck worker** | No progress note / completion for N turns or budget idle | Head posts question; if still stuck → cancel worker seat (`status=done/failed`), release claims, re-spawn with narrower brief or different role. |
| **Cross-dept claim conflict** | `coordinate_claim` CONFLICT where holders' `dept_id` differ | No auto-steal. Escalate via heads; director steering assigns ownership; losing worker re-plans. |
| **Budget exhaustion mid-org** | Delegation/budget meter empty | Freeze new spawns; heads finish in-flight workers only; director digest of residual work; optional human ask_user for budget top-up. |
| **Depth-cap collisions** | `spawn_worker` attempted_depth > max_subagent_depth | Refuse with structured error (already). Head must do work inline or reassign to existing worker. Team leads cannot spawn under default depth 2 — promote is for sync only. |
| **Degenerate single-dept mission** | `derive_departments` length < 2 | Do not activate corporate mode; flat delegation. |
| **All depts blocked** | Multiple blockage digests | Director `needs_llm` + consider `ask_user`; do not infinite-meeting loop (cap meetings per dept per run). |
| **Illicit skip-level spam** | Worker→director rejects | Count rejects; if excessive, head is notified via dept auto-note (design). |
| **Chart rebuild thrash** | Replan every turn | Rebuild only on mission replan events; bump `chart_revision`; keep stable head ids. |

---

## 7. Budgets

Principles:

1. **Director is LLM-light** — default path is template-only; LLM steering is rare and short.
2. **Heads are the expensive coordinators** — medium/high thinking, larger token budgets than workers.
3. **Workers are narrow leaves** — smaller budgets; cheap local models allowed when
   `role_models` cost tier + task class say so.
4. Children's spend rolls up through `delegation.Budget` / `BudgetMeter` when a tree is bound.

### 7.1 Suggested default split (of mission token budget B)

| Seat | Share | Notes |
|---|---|---|
| Director | 5–10% | Mostly template; LLM steering from this pool |
| Heads (sum) | 25–35% | Split evenly or by task-count weight |
| Workers (sum) | 55–70% | Per-spawn `budget_tokens` from remaining head pool |
| Reserve | 5% | Respawn / emergency steering |

First cut hard-codes `budget_tokens=4000` in `corporate_spawn_worker` when unset — replace with
head-pool allocation.

### 7.2 Iteration budgets

- Worker: low turn cap (leaf behavior).
- Head: higher turn cap; meeting posts do not consume a full "design" turn budget.
- Director: review is O(1) template work per digest; LLM at most once per misaligned digest.

### 7.3 Depth vs budget

`max_subagent_depth` is a **layer** cap, not a concurrency cap. Concurrency remains
`DelegationTree.max_agents` / soft limits. Corporate mode must not invent a third depth system.

---

## 8. Observability and GUI

### 8.1 What the user should see

1. **Settings → AI → Behavior** (exists in `host/src/ui/settings_modal.cpp`):
   toggle company-structure auto-routing.
2. **During a run (design):**
   - Org chart panel or Activity-tree section: director → depts → heads → workers (status chips).
   - Meeting digests as chat/Activity cards (done/blocked counts, trigger).
   - Steering notes as distinct cards (director → dept).
   - Per-dept progress: completions, completions_since_meeting, claim conflicts.
3. **Subagent view** (existing AI workspace subagent registry): show `corporate_rank` and
   `corporate_dept` badges when present on the session/node.

### 8.2 Config surface

| Key | Type | Default | Meaning |
|---|---|---|---|
| `agent.corporate_mode` | bool | false | Master opt-in |
| `agent.corporate.max_departments` | int ≥1 | 3 | Cap after merge |
| `agent.corporate.team_leads` | bool | true | Auto-promote leads |
| `agent.corporate.meeting_cadence` | int ≥1 | 5 | Completions between meetings |
| `agent.corporate.director_review` | bool | true | Template (+ optional LLM) review |
| `agent.max_subagent_depth` | int | 2 | Shared nesting hard cap |

Accessors: `config.corporate_mode_enabled`, `config.get_corporate_config`.

Future knobs (not first cut): `director_llm_steering`, `auto_relay_steering`,
`meeting_cap_per_dept`, `budget_split`, `merge_affinity`.

### 8.3 Events to emit (additive `aicad.corporate/1` stream)

Contract today lists tools and structure; it does **not** yet enumerate stream events. Design
event kinds (payloads include `run_id`, `ts`, `chart_revision` where relevant):

| Event kind | When | GUI use |
|---|---|---|
| `corporate.org_built` | activate/build | Draw initial org chart |
| `corporate.seat_update` | spawn, promote, status change | Update tree nodes |
| `corporate.channel_message` | post_message ok | Optional live feed / debug |
| `corporate.meeting` | digest posted | Meeting card |
| `corporate.director_review` | review computed | Alignment badge + steering card |
| `corporate.steering_relayed` | head/auto relay to dept | Worker-visible note |
| `corporate.dept_complete` | dept wind-down | Collapse dept |
| `corporate.org_finished` | org teardown | Clear live org UI |
| `corporate.route_rejected` | illegal post (rate-limited) | Debug / health |

Ledger: keep `artifact` events for org_built / org_finished (activate already writes a thin
`corporate_org` artifact). Prefer dual-write: ledger for durability, agent_events for live UI.

### 8.4 Snapshot API

`OrgChart.snapshot()` / `corporate_chart` tool remains the pull API for UI and tests. Snapshot
must stay JSON-serializable and bounded (no full message dump by default; counts only).

---

## 9. Evaluation

### 9.1 Scenarios (corporate ON vs flat OFF, same brief + models)

1. **Multi-domain bracket** — geometry + FEA + drawing. Expect ≥2 depts; worker→director reject
   still holds under load; FEA does not clobber feature tree without claims.
2. **CAM + part** — manufacturing + geometry; handoff of stock/features via claims + handoff kind.
3. **ECAD + enclosure** — electronics + geometry; interface dims escalate through heads, not root spam.
4. **Single simple part** — corporate_mode on but 1 dept → stays flat (`active=false`).
5. **Blockage storm** — forced regen failure; meeting fires; template steering; no infinite LLM.
6. **Depth-cap** — max_subagent_depth=1 → heads seat but cannot spawn workers; head works or fails cleanly.
7. **Budget starve** — tiny B; spawns stop; residual reported.

### 9.2 Metrics

| Metric | Direction | Notes |
|---|---|---|
| Mission success / verify_battery pass | ↑ | Primary quality |
| Root/director tokens as % of total | ↓ | Should drop vs flat |
| Illegal route attempts | →0 after preamble learning | Soft health |
| Claim CONFLICT rate + time-to-resolution | ↓ | Parallel safety |
| Time-to-first-useful-artifact | ≈ or ↓ | Org must not only add latency |
| Meeting count / blockage meetings | bounded | Cap thrash |
| LLM director calls / digest | <<1 | Template-first proof |
| Clobber incidents (overlapping feature edits) | ↓ | vs flat baseline |
| Human escalations | ↓ for multi-domain noise; ≈ for true ambiguity | |

### 9.3 Proof bar for "org beats flat"

Ship corporate as default-off until:

1. Scenario suite shows success ≥ flat on multi-domain briefs.
2. Director token share ≤ 15% of run.
3. Zero silent cross-dept overwrites in claim-instrumented runs.
4. Single-dept missions remain bit-identical to flat (activate no-op).

---

## 10. Relationship to `aicad.org/1` (`org.py`)

| | `org.py` | `corporate.py` |
|---|---|---|
| Schema | `aicad.org/1` | `aicad.corporate/1` |
| Clustering basis | deliverable manifest + depends_on | task roles/classes/stages |
| Channels | between departments (interface_change) | heads + dept (subagent chat) |
| Managers | manifest managers/advisors | live subagent seats |
| Runtime | mission pipeline / ledger | copilot MissionDispatch / delegation |

**Design convergence (later):** when a mission has both a deliverable org chart and corporate mode,
map deliverable depts → corporate depts where names align; reuse interface_change as a *reason*
for heads-channel blockage. Do not merge codebases until both lifecycles are solid.

---

## 11. Gap analysis (first cut vs this design)

Honesty rule: first cut is a strong **kernel** (chart, routing, meetings, tools, config, tests).
It is not yet a closed lifecycle product. Gaps below cite live symbols.

| # | Design requirement | Current state | Gap | Proposed change | Size |
|---|---|---|---|---|---|
| 1 | Auto-activate at mission start when mode on and ≥2 depts | **DONE** — `auto_activate_corporate` + `run_mission` graph hook + `MissionDispatch.maybe_auto_activate_corporate` / `corporate_activate(seat_heads=True)` | closed | — | **L** |
| 2 | Heads are live hired agents with models | **DONE** — `OrgChart.seat_heads` + node_runner re-hire via `delegate` when tree bound; department_manager preamble | closed | — | **L** |
| 3 | Director LLM steering when `needs_llm` | **DONE** — `_llm_director_steering` via role_models advisor/orchestrator + ONE `complete()`; template fallback | closed | — | **M** |
| 4 | Stream events for GUI (`corporate.*`) | **DONE** — `kind=corporate` + `corp_event` ∈ org_built/hire/team_lead/meeting/steering/route_rejected/wind_down on agent event stream | closed | contracts append (integrator) | **L** |
| 5 | Org chart / meeting cards in UI | Settings box + CorporateUI Activity cards consume events | partial | polish only | **L** |
| 6 | Auto-relay steering head→workers | **DONE** for blockage/needs_llm in `_hold_meeting_unlocked` | closed | — | **S** |
| 7 | Wind-down / teardown | **DONE** — `wind_down` / `wind_down_corporate` + `{run_id}.corporate.json` + drop registry; hooked at mission end | closed | — | **M** |
| 8 | Budget split director/heads/workers | `corporate_spawn_worker` uses `budget_tokens or 4000`; no pool accounting | Uniform leaf budgets; director not budgeted | Head-owned pool + BudgetMeter integration; director steering budget | **M** |
| 9 | Role/model selection for heads via role_models | **PARTIAL** — `seat_heads` calls `resolve_role_model` / thinking | assignment not always stored on seat | store provider/model on seat or node | **M** |
| 10 | Team lead useful under default depth 2 | **DONE** — effective cap `max(global, 3)` for lead→worker only; global config untouched | closed | — | **S** |
| 11 | Failure policies (dead head, stuck worker, meeting caps) | Not implemented beyond spawn/route errors | Org can stall silently | Implement §6 policies in OrgChart + dispatch | **L** |
| 12 | Claim-before-edit enforced / digest integration | Board tools always injected; corporate only mirrors some posts to `post_progress` | No hard gate; digests ignore CONFLICT state | Optional soft-check in worker tools path; include board conflicts in auto-digest risks | **M** |
| 13 | Persistence sidecar for org (restart-safe) | **PARTIAL** — wind-down writes `{run_id}.corporate.json`; live chart still process-local | Restart loses live org | Optional load/flush like coordination board | **M** |
| 14 | `handoff` kind first-class | Free-form `kind` string; no handoff semantics | Ownership transfer ad-hoc | Document kind + optional claim transfer helper | **S** |
| 15 | Merge affinity / smarter capping | Overflow merges to `general` or last kept (`derive_departments`) | Odd depts collapse poorly | `MERGE_AFFINITY` table; task-count weights | **S** |
| 16 | Director is root persona, not dual process | Director seat id exists; root uses it when `corporate_agent_id` unset | OK as design; root may lack director preamble until activate | Ensure root turn injects director preamble when chart active | **M** |
| 17 | Evaluation harness | Unit tests in `test_corporate.py` (routing, cadence, depth, flat-off, auto-activate, wind-down, LLM steering) | No multi-domain vs flat benchmark | Scenario suite §9 in gym/benchmarks | **L** |
| 18 | Contract event enumeration | Stream events implemented; `contracts/agent_events.md` section not yet expanded | GUI/backend event contract incomplete | Expand aicad.corporate/1 section with event kinds (append text below) | **S** |
| 19 | Integration with `org.py` | Explicitly complementary in module docstring; no code bridge | Dual "org" concepts confuse | Doc + optional mapping layer later; no merge yet | **S** (doc) / **L** (bridge) |
| 20 | Worker completion without being seated | `record_worker_completion` counts even if post rejected (`posted=False`) — tested | Good for robustness; can inflate cadence from ghosts | Optionally require seated worker_id to count (config) | **S** |
### 11.1 What the first cut already does well

- Clear schema and module boundary (`CORPORATE_SCHEMA`, rich module docstring).
- Deterministic department taxonomy + cap/merge (`DEPARTMENT_CATALOG`, `derive_departments`).
- Real rank-gated channels with tested worker→director rejection (`post_message`,
  `TestCommunication`).
- Meeting cadence + blockage + auto-digest + template director review (`record_worker_completion`,
  `_director_review_unlocked`, `TestMeetings`).
- Depth-aware spawn + team-lead auto-promotion (`spawn_worker`, `TestSpawnDepth`).
- MissionDispatch tool surface + preamble/tool injection for seated agents
  (`TOOLS`, `_run_subagent` corporate block, `corporate_*` handlers).
- Config + GUI settings parity (`config.get_corporate_config`, `drawOvCorporateRolesBox`).
- Flat mode safety (`should_use_corporate`, `TestFlatMode`).
- Coordination board coexistence (mirror progress; shared run_id).
- Solid unit tests as executable spec (`test_corporate.py`).

### 11.2 Recommended implementation order

1. **S:** Auto-relay steering; contract event list; team-lead depth docs; handoff kind.
2. **M:** Root director preamble; wind-down; budget pools; role_models at head seat; optional LLM
   steering; board conflicts in digests.
3. **L:** Auto-activate + live head hire in the mission runner; agent_events + Activity/org UI;
   failure policies; evaluation suite.

Do not grow department taxonomy or GUI chrome until (1) auto-activate and (2) live heads exist —
otherwise the feature remains a library without a runtime.

---

## 12. Open design decisions (need product input)

1. **Default on vs off after eval?** Design keeps default **off** until §9 proof bar.
2. **Recommended `max_subagent_depth` when corporate_mode is on?** Keep 2 (leads non-spawning) or
   suggest 3 when team_leads enabled?
3. **Auto-relay steering always, or only on blockage?** Design default: always for template
   steering (cheap).
4. **Should corporate activation require a DAG/mission, or also single-shot copilot chat with a
   multi-domain brief?** Design: any MissionDispatch run with a task list; pure chat stays flat
   unless it enters mission mode.
5. **Merge affinity table ownership** — product taxonomy vs pure engineering defaults.
6. **How visible should rejected routes be to the user?** Debug-only vs occasional chat toast.

---

## 13. Code map (quick reference)

| Concern | Location |
|---|---|
| Org chart + routing + meetings | `services/agent/corporate.py` (`OrgChart`, …) |
| Config | `services/agent/config.py` (`corporate_mode_enabled`, `get_corporate_config`) |
| Tools + preamble injection | `services/agent/mission_dispatch.py` |
| Claims / progress board | `services/agent/coordination.py` |
| Nesting / budgets | `services/agent/delegation.py` |
| Model/thinking pick | `services/agent/role_models.py` |
| Deliverable org (other system) | `services/agent/org.py` |
| Tests | `services/agent/test_corporate.py` |
| Contract | `contracts/agent_events.md` § aicad.corporate/1 |
| Settings GUI | `host/src/ui/settings_modal.cpp` (`settings.corporateMode`) |

---

*End of design. Implementation must treat this file as the source of truth until superseded by a
dated revision in `docs/design/`.*
