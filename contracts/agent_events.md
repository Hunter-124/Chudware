# Contract: Agent Events — `aicad.agent_events/2`

> **Frozen `/1` by T-F17 (2026-07-03). Bumped to `/2` by M20 (2026-07-07).**
> Append-only; schema-string bump required for any field change or new enum value.
> This is the single event vocabulary shared by two producers (the DAG executor and
> the built-in agent loop) and two sinks (the mission-control UI live stream and the
> telemetry journal). Never duplicate this vocabulary in a parallel file.
>
> **Machine check:** the canonical machine-readable kind list is
> `contracts/agent_events.json` (kinds + required payload fields), verified against both
> emitters (`core/agent/events.cpp`, `services/agent/agent_events.py`) by
> `scripts/agent_events_audit.py` (wired into `scripts/check.py`). Any kind added here
> MUST be added to the JSON in the same change or the gate fails.
>
> **`/2` is a strict superset of `/1`** — every `/1` record is a valid `/2` record.
> `/2` adds four net-new agent-level kinds (`agent.context_injected`, `agent.spawn`,
> `agent.advisor`, and the forward-compat `agent.thought.delta`), a human-readable
> **`target`** field on `agent.tool_call`, and **per-event parent linkage**
> (`id` / `parent_id` span ids + `actor`) on all agent-level events. Because the
> conformance rules (§5) already require consumers to tolerate unknown kinds AND
> unknown fields, a `/1` consumer reads a `/2` stream without changes (it simply
> ignores the new kinds/fields). Producers stamp the schema string only where a
> schema field is written; the envelope itself is unversioned per-record.
>
> **Binding relationships:**
> - DAG-side events (`dag.*`) bind to the 8-event table in `contracts/dag_schema.md §11`.
>   Field names here MUST match exactly.
> - Thought / tool_call / artifact kinds reuse the T-H1 telemetry vocabulary
>   (`contracts/telemetry.md`) for the overlapping fields so one pipeline has two sinks.
>   The `/2` additions (`target`, `id`/`parent_id`, `actor`) are **superset fields**:
>   they are new to this contract and are NOT part of the bound telemetry vocabulary,
>   so the telemetry sink ignores them (per its own unknown-field rule).
> - Question / answer events bind to `contracts/interaction.md §7` (`ask_question` /
>   `ask_answer` kinds) — no new fields, referenced here for discoverability only.

---

## 1. Storage & transport

**Location:** `~/.local/share/Chudware/cache/runs/<run_id>/events.jsonl` (Linux/macOS)
or `%APPDATA%\Chudware\cache\runs\<run_id>\events.jsonl` (Windows).

One JSON object per line. The DAG executor writes this file via its `emit` callback
using `EventJournal` (services/dag/event_journal.py — created by T-F17). The built-in
agent loop (services/agent/loop.py) already writes to the same path pattern via
`contracts/events.md`; future versions SHOULD also emit the DAG-vocabulary events when
wrapping a DAG run.

**Replay:** The mission-control panel (`host/src/ui/copilot/activity_timeline.cpp`) scans
`~/.local/share/Chudware/cache/runs/` for directories containing `events.jsonl` and
presents them as replayable runs. Loading is triggered on panel open; it re-reads the
file if the mtime changed.

**Persist-then-callback rule:** the line is written to disk (and flushed) before the
in-process UI callback fires. If the process crashes after write but before callback,
the record persists. This is the same guarantee as `contracts/telemetry.md §1.2`.

---

## 2. Envelope

Every event is a bare JSON object (no wrapper envelope). All events carry:

| Field   | Type   | Required | Description |
|---------|--------|----------|-------------|
| `kind`  | string | yes      | Event discriminator (§3) |
| `ts`    | string | yes      | ISO-8601 UTC timestamp (`2026-07-03T12:00:00Z`) |
| `run_id`| string | yes      | The run this event belongs to |

Unknown fields MUST be tolerated (skip). Unknown `kind` values MUST be tolerated (skip).

### 2.1 Parent linkage (`/2` — agent-level events only)

Every **agent-level** event (the `agent.*` kinds in §3.2 and §3.3-new) additionally
carries the fields below so a consumer can reconstruct the **execution DAG tree**
(the Agent Workflow view). DAG-lifecycle events (`dag.*`) do NOT carry these — they
already form a tree via `node_id` and bind to `dag_schema.md §11` unchanged.

| Field       | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `id`        | string | yes      | **Span id** — unique per event, the tree key. A single `node_id` emits MANY events, so `node_id` is NOT the tree key; `id` is. Format: short opaque token (hex). |
| `parent_id` | string | no       | The `id` of the span that *caused* this one (the spawner / enclosing turn / trunk). Empty/absent for a run-root span. The GUI builds the tree by joining `parent_id → id`. |
| `actor`     | string | no       | Which agent/subagent/role emitted the event (e.g. `"orchestrator"`, `"cad_designer"`, a node name). Lets the GUI colour/group by actor. |
| `seq`       | int    | no       | Optional monotonic-per-run ordering hint. **Best-effort only** — producers that mint a fresh journal per emit (e.g. `loop.py`) cannot guarantee monotonicity, so consumers MUST fall back to `ts` + the `parent_id`→`id` tree and treat `seq` as advisory. May be absent. |

**Rule:** `id` is REQUIRED on every agent-level event; `parent_id`/`actor`/`seq` are
optional (a producer that has no parent in scope emits a root span with no `parent_id`).
Consumers tolerate a missing `parent_id` (treat as root) and a missing `id` on a legacy
`/1` record (fall back to `node_id`+`ts` grouping, no tree).

---

## 3. Event kinds

### 3.1 DAG lifecycle (bind to `contracts/dag_schema.md §11`)

| kind | Additional fields | When |
|------|-------------------|------|
| `dag.run.started` | `graph_id`, `node_count`, `graph_name` | Before first node |
| `dag.node.started` | `node_id`, `node_type`, `node_name` | Before each node |
| `dag.node.done` | `node_id`, `output_summary` (string, ≤120 chars) | After node succeeds |
| `dag.node.failed` | `node_id`, `error` | On node failure |
| `dag.node.paused` | `node_id`, `reason` (`"gate"` \| `"manual"` \| `"fail"`) | On pause |
| `dag.node.approval_required` | `node_id` | `autonomy: "approve"` gate |
| `dag.node.manual_required` | `node_id` | `autonomy: "manual"` node |
| `dag.run.done` | `status` (`"done"` \| `"failed"` \| `"paused"`), `node_states` (object) | After last node |

`node_states` in `dag.run.done` is `{node_id: {"status": NodeState}}` — a compact
summary for the panel's final-state rendering (not the full output dict).

### 3.2 Agent thought & action (T-H1 telemetry vocabulary — reused)

These are emitted by node runners that call an agent loop. They share fields with the
T-H1 `ai_artifact` / `ai_artifact_action` event kinds so a single telemetry sink can
consume both vocabularies. **All carry the §2.1 parent-linkage fields (`id`,
`parent_id`, `actor`).**

| kind | Fields | When |
|------|--------|------|
| `agent.thought` | `node_id`, `text` (≤500 chars; truncated with `…` if longer), `model` | Model produced a **completed** reasoning/thinking block (summary-on-arrival) |
| `agent.thought.delta` *(`/2`, forward-compat)* | `node_id`, `text` (token delta), `model` | A single streamed token/chunk of an in-progress thought. **Producer-gated:** only emitted when the provider seam exposes token streaming. The current `Provider.complete`/`complete_ex` seam returns whole strings (non-streaming), so NO producer emits this today — it is reserved so the GUI can render a live collapsible thought box once a streaming provider lands, without a further schema bump. Consumers coalesce deltas sharing a `parent_id` into one growing block, finalised by the matching `agent.thought`. |
| `agent.tool_call` | `node_id`, `tool_name`, **`target`** (`/2`; human-readable ≤120 chars — "what it acts on", e.g. `"top face of Part1"`, `"sketch S3"`; NOT a hash; best-effort derived from the call args, `""` when nothing legible), `args_digest` (SHA-256 hex of JSON args, first 16 chars), `result_digest` (SHA-256 hex of result text, first 16 chars), `ok` (bool), `latency_ms` | Tool call completed |
| `agent.artifact` | `node_id`, `artifact_id`, `kind`, `path`, `summary` (≤80 chars) | Agent produced a deliverable |
| `agent.question` | `node_id`, `batch_id`, `prompt` | Agent raised a blocking question (mirrors `ask_question`) |
| `agent.answer` | `node_id`, `batch_id`, `source` (`"user"` \| `"default"`) | Question answered |

### 3.2a Agent execution-DAG events (`/2` — net-new)

These carry the §2.1 parent-linkage fields and feed the **Agent Workflow DAG** (M9) +
the transparency chat (M12). They are emitted by the agent loop / delegation / advisor
paths, NOT the DAG executor.

| kind | Fields | When |
|------|--------|------|
| `agent.context_injected` | `node_id`, `source` (`"ledger"` \| `"advisory"` \| `"previous_output"` \| `"memory"` \| `"role"` \| `"plan"` \| `"other"`), `label` (human-readable ≤80 chars — what was injected, e.g. `"prior render + build plan"`), `token_est` (int, best-effort; may be 0/absent) | Context/memory/system material was assembled into a turn's prompt |
| `agent.spawn` | `node_id`, `subagent_id` (the spawned node's id), `role`, `spawn_kind` (`"worker"` \| `"ponder"`), `brief` (≤120 chars), `budget_tokens` (int) | The orchestrator delegated to a subagent/branch. `parent_id` links to the spawner span; `subagent_id` is the child's own execution root. |
| `agent.advisor` | `node_id`, `advisor_kind` (`"advise"` \| `"design_review"` \| `"council"` \| `"ladder_council"`), `verdict_summary` (≤120 chars) | An advisor / design-review / build-strategy-council path ran |

> **Reserved envelope key:** `kind` is the top-level event discriminator (§2), so no
> `/2` payload sub-field is named `kind` — hence `spawn_kind` / `advisor_kind` (a payload
> `kind` would clobber the discriminator when the journal spreads the payload into the
> record). New producers MUST NOT emit a payload field literally named `kind`. (The
> frozen `/1` `agent.artifact` still documents a `kind` sub-field; it has no live
> journal producer and is left unchanged — do not add one without addressing this.)

### 3.3 Steering events (mission-control → executor)

These are NOT written to `events.jsonl` (they are inbound commands, not log entries).
They are documented here for discoverability; the actual wire format is the worker RPC
(see §4 / `contracts/worker_rpc.md` for the `dag_approve` / `dag_resume` / `dag_pause`
/ `dag_inject` methods registered by T-F17).

| Steering action | RPC method | Payload |
|----------------|------------|---------|
| Pause a running mission | `dag_pause` | `{run_id}` |
| Resume / approve a gate | `dag_approve` | `{run_id, decision: "approve"\|"reject"\|{action:"modify",output:{…}}}` |
| Inject an instruction | `dag_inject` | `{run_id, instruction, at_node_boundary: true}` |
| Change autonomy dial | `dag_set_autonomy` | `{run_id, level: "auto"\|"approve"\|"manual"}` |

These methods are implemented in `services/dag/worker_rpc.py` (T-F17).

---

## 4. Design-brief event (Elicitation gold — `contracts/telemetry.md §5.4`)

The design-brief flow produces two additional events consumed by the telemetry exporter:

| kind | Fields | When |
|------|--------|------|
| `mission.brief.proposed` | `run_id`, `batch_id`, `brief_ref` (path to aicad.req/1 doc) | Agent proposed a design brief via `ask_user` approval |
| `mission.brief.signed` | `run_id`, `batch_id`, `brief_ref`, `signed_ref` (path to final brief) | User approved / edited and signed the brief |

These ride the same `events.jsonl` stream. The telemetry exporter (T-H5) uses them to
derive elicitation-gold training pairs per `contracts/telemetry.md §5.4`.

---

## 5. Schema validation

A conforming producer MUST:
1. Write `kind`, `ts`, and `run_id` on every record.
2. Include the additional fields listed in §3 for each `kind`.
3. Flush the file (`fsync` equivalent) before firing any in-process callback.

A conforming consumer MUST:
1. Tolerate unknown `kind` values (skip the line).
2. Tolerate unknown fields on known `kind` values (ignore).
3. Never crash on a malformed line (skip and continue).

---

## 6. Example stream (36 events — capstone thin-slice + full-mission)

```jsonl
{"kind":"dag.run.started","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","graph_id":"thin-slice-soil-sensor","node_count":2,"graph_name":"Thin-slice: design+gate"}
{"kind":"dag.node.started","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_design","node_type":"design","node_name":"ECAD schematic stub"}
{"kind":"dag.node.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_design","output_summary":"Stub ECAD design completed"}
{"kind":"dag.node.paused","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder","reason":"gate"}
{"kind":"dag.node.approval_required","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder"}
{"kind":"dag.node.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","node_id":"n_gate_preorder","output_summary":"Gate approved"}
{"kind":"dag.run.done","ts":"2026-07-03T00:47:41Z","run_id":"c8efe1ae","status":"done","node_states":{"n_design":{"status":"done"},"n_gate_preorder":{"status":"done"}}}
```

## Early-stop detail (2026-07-16, additive)
On `run_finished` / terminal `status` (phase done|error|failed|cancelled) and the
agent run RPC result (`agent_run_v2`; formerly also `agent_design`, removed T-RF5),
optional additive fields:
- `stop_reason` — short enum-ish string (confirmed|stabilized|refine_cap|max_iters|no_code|failed|provider_error|provider_auth|provider_unavailable|provider_refused|phase_step_cap|terminated_by_user|…)
- `error_detail` — human sentence, secret-scrubbed, ≤~300 chars; empty on normal completion. Prefer over legacy `error`.
GUI renders error bubbles as `stopped: <stop_reason> — <error_detail>` when present.

## Subagent governance (2026-07-16, additive)
- Config `agent.max_subagent_depth` (int, default 2, root chat = depth 0): hard cap on subagent nesting LAYERS (not concurrency); spawn past cap → DelegationError "max subagent nesting depth (N) reached".
- Coordination board (`services/agent/coordination.py`, per run_id, JSON sidecar `{runs_dir}/{run_id}.coordination.json`): `register_claim(agent_id, resources, intent) -> claim|CONFLICT(holder,intent)`, `release_claim`, `post_progress`, `board_snapshot`. Subagent tools: `coordinate_claim/release/progress/board`; board preamble injected into subagent prompts.

## aicad.corporate/1 (2026-07-16, additive)
Opt-in org mode. Config: `agent.corporate_mode` (bool, default false); `agent.corporate` {max_departments:3, team_leads:true, meeting_cadence:5, director_review:true}.
Structure per run: director (depth 0) → department heads (depth 1, one per derived department, capped/merged) → workers (depth 2, head-spawned; team lead auto-promoted >3 workers).
Channels (enforced): `dept:<id>` = head+workers(+lead); `heads` = heads+director. Worker→director → status=rejected.
Meetings: every `meeting_cadence` worker completions or on blockage, head posts MeetingDigest {done,in_progress,blocked,risks,trigger,worker_completions} to heads; director review is template-first, LLM steering note only on misalignment/blockage.
Tools: corporate_activate, channel_post, channel_read, chart, spawn_worker, hold_meeting, record_completion.

## aicad.corporate/1 stream events (2026-07-16, additive)
`kind:"corporate"`, `corp_event ∈ org_built|hire|team_lead|meeting|steering|route_rejected|wind_down`, `run_id`, `ts`, optional `chart_revision`/`dept`/`agent`, `payload` per event: org_built {chart_revision,director_id,departments[],mission_goals}; hire {role,rank,parent_id,dept_id,brief?,model?,live?}; team_lead {worker_id,dept_id,previous_rank}; meeting = MeetingDigest + counts; steering {text,to_dept,needs_llm,aligned,notes,llm_refined?}; route_rejected {from,channel,reason,from_rank}; wind_down {summary,success,departments_done,artifact_path?,digests}.
Emission seam: services.agent.corporate.emit_corporate_event → agent_events.emit_event. GUI consumer: legacy_chat feedTurnActivity (rowKinds 4–8).
Effective depth rule (binding): team-lead→worker spawns use effective_cap = max(agent.max_subagent_depth, 3) computed ONLY inside the corporate spawn path; the global key is never mutated and still governs flat delegation.

## Stream Thinking/Doing (2026-07-16, additive)
C++ agent_run path now emits agent-level kinds previously Python-only:
- `agent.thought`: after each provider completion when CompleteResult.thinking is non-empty. Fields: `id`, `text` (cap 4000 on C++; Python thought_event still caps 500), optional `model`, `stage`, `attempt`, `parent_id`, `actor`, `node_id` (=stage or `run`). Batched post-run on in-proc C++ path (no live stream refactor).
- `agent.tool_call` **args_preview** (optional string, <=~2KB): capped JSON of tool/op args for the Doing drawer. Digests (`args_digest`/`result_digest`) unchanged. Producers: Python `tool_call_event`; C++ synthetic emit after `doc_apply_ops` with `tool_name` = op type (homogeneous) or `doc_apply_ops`, `target` best-effort, `ok`, `latency_ms`. Consumers MUST tolerate unknown fields (additive; no schema bump).

## Tool artifacts + classification (2026-07-17, additive)
- `agent.tool_call` optional `artifacts` (array, <=8) of `{path, kind}` where `kind in image|mesh|file`: filesystem paths of deliverables produced by render/screenshot/export tools (contact sheets, `render_part`/`render_views`/`render_sketch`/`look_around` PNGs, STL/STEP exports). Lets the GUI expand a tool-call row to show the produced image(s) inline. `args_digest`/`result_digest` unchanged. Producer: Python `tool_call_event` when the tool result carries a path/`contact_sheet`. Consumers MUST tolerate unknown fields (additive; no schema bump).
- `agent.classification` (new kind): emitted ONCE at request entry, BEFORE dispatch/routing, so the GUI can show the pre-flight task-classification step the user sees while "the request gets routed". Fields: `task_class` (workflow_router `TaskClass`), `scaffold_level`, `tier`, `reason` (<=160 chars), `request_snippet` (<=120 chars), optional `picked_model`, `picked_provider`. Mirrors `aicad.router/1` RouterResult. Producer: services.agent entry (mission/design) via `workflow_router.route`. Consumers tolerate unknown kinds (conformance §5); GUI renders a "Routing" card at the top of the copilot activity timeline.

## Role-change + delegation cards (2026-07-27, additive)
- `agent.role` (new kind): emitted ONLY when the agent actually adopts a different role via `adopt_role` — never for the default first-turn `general_agent` entry. Fields: `node_id`, `role`, `goal` (<=280), `from_role`, `reason` (<=280). Producer: `services.agent.mission_dispatch.MissionDispatch.adopt_role` via `agent_events.role_event`. GUI: reveals the copilot Routing card (rowKind 10) with the new role; the per-request `agent.classification`/`complexity`/`task_route` telemetry alone now keeps the card hidden (stage `class:pending`) since every request enters via the universal `general_agent` role.
- `agent.spawn` (existing kind) additionally drives a GUI "Delegating" card (rowKind 19) in the copilot activity timeline — one card per turn, upserted as spawns land, listing `role`/`spawn_kind`/`brief` per subagent. No payload change.

## Provider usage metering (2026-08-10, additive)
- `agent.provider_usage`: emitted immediately by the Python provider usage sink with `{provider, model, prompt_tokens, completion_tokens, total_tokens, context_tokens, source}`. The payload deliberately carries no email, account id, balance, or other attribution: the authenticated gateway attributes the event to the WebSocket that delivered it. With no bound sink (desktop/local BYOK), usage keeps its existing in-process accumulator behavior and emits nothing.


## agent compaction / session settings (batch 2b)
Config keys under `agent` (Python services/agent/config.py; C++ config.cpp mirrors agent.prewalk*):
- `agent.compaction_strategy` string default "off" — off|snapcompact|shake|summary
- `agent.compaction.supersede_reads` bool true; `agent.compaction.drop_useless` bool true; `agent.compaction.keep_recent_tokens` int 20000
- `agent.prewalk` bool false; `agent.prewalk_target` string "@smol"; `agent.handoff_save_to_disk` bool false
Engines (pure): agent.compaction — serialize_transcript, snapcompact_archive (text-phase; frames=[] future), shake_messages ('[shaken ~N tokens — recover: artifact://<id> (region K)]'), reinject_archive.
Prewalk (C++): planning stages (intake|plan|decompose|review|memory) keep the strong model; implementation stages (model|fix|execute|build) switch once to agent.prewalk_target; run_note on switch; off = byte-identical.

## Session command RPCs (batch 2b)
Host passes its ChatMsg transcript as messages[]; RPCs are pure transforms, host applies result. services/agent/worker_rpc.py SESSION_METHODS merged into worker_rpc.py WORKER_METHODS.
- session_compact {messages, strategy, keep_recent_tokens?, supersede_reads?, drop_useless?, prior_archive?} -> {ok, kept_messages, archive?, summary, short_summary}
- session_shake {messages, mode(elide|images), aggressive?} -> {ok, messages, saved_tokens, notice}
- session_handoff {messages, focus?, provider, model} -> {ok, document} (## Goal/Constraints/Progress/Key Decisions/Critical Context/Next Steps; new-session is host-side)
- session_btw {messages, question, provider, model} -> {ok, answer} (ephemeral; never mutates history)

## plan_state (batch 2c, additive; C++ agent harness)
Event `kind:"plan_state"` { shape:"build_plan"|"checklist"|"phased_todo"|"objectives"|"dag", title, summary?, revision, items:[{id, title(<=10 words), desc(tooltip: purpose/dims/assignment), status:"pending"|"running"|"pass"|"fail"|"done"|"abandoned", kind:"part"|"step"|"task"|"objective"|"node"|"subagent", phase?, parent_id?, meta?}], edges?:[{from,to}] (dag only) }
Producers: core/agent/machine.cpp (build_plan after plan pass; checklist from plan_steps live; objectives when plan JSON has objectives/acceptance/success_criteria); core/agent/run.cpp mission loop (phased_todo). DAG not emitted from C++ (host reads buildgraph sidecar/RPC). Back-compat: kind:"plan"+PlanInfo kept; host normalizes PlanInfo→plan_state build_plan when planState empty. Raw plan_steps/model_step/mission_* remain. GUI: copilot Plan/Objectives section (tiles+hover tooltips, todos tree, subagents) above the activity timeline.
Python producer (2026-07-29, additive): `services/agent/task_list.py` publishes shape=`checklist` with `task:<n>`-namespaced ids on behalf of the `task_list` tool, streamed through `worker_rpc._agent_admin_turn`'s `_emit` and relayed verbatim by `core/agent/run.cpp`'s admin-turn branch. This is the ONLY plan_state producer reachable from the general lane (the universal online entry), which never enters the C++ machine/mission loops. Full-snapshot semantics: each publish restates every item, and the host's scoped-prune (common `task:` id prefix) drops rows the writer omitted.

## options.mode — copilot mode overhaul (Agent/Plan/Swarm/Edit/Ask)
`options` on agent_run/agent_run_v2/agent_mission_v1 (agent_design removed T-RF5):
| key | type | default | meaning |
|-----|------|---------|--------|
| mode | agent|plan|swarm|edit|ask | agent | copilot composer mode (persisted host-side as config copilot_mode) |
| plan_only | bool | false (true when mode=plan) | intake+plan → emit plan_state → stop before mutation; returns {status:plan_ready} |
| approved_plan | string|obj | null | seed plan; skip re-plan; clears plan_only (execute pass) |
| execute_as | agent|swarm | — | how an approved plan resumes |
| corporate_force | bool | false (true when mode=swarm) | force subagent-manager/corporate |
| scope | obj | {} | edit targeting {doc_id, feature_ids?, selection?, section?} |
| advisor | bool | true | advisor on for run + subagents; config agent.advisor default true, env CHUDWARE_ADVISOR honored |
Mode→role: agent→default, plan→planning, swarm→default(+corporate), edit→task, ask→default(read-only). Mode→tools (Python copilot_mode_tools): ask=read-only; plan=read+plan no mutation; edit=doc mutators no new_part/mission; agent=full+delegate; swarm=full+corporate/delegate. Subagents available in every mode. Plan gate: mode=plan → plan_only run → GUI drawPlanApproveCard (Execute as Agent/Swarm/Cancel) → resend approved_plan+execute_as. C++ (core/agent/run.cpp / core/api/libchudware.cpp) parses these; absent mode = legacy byte-identical.

## Mid-run steering (2026-07-19, additive)
- `agent_steer` (new kind): emitted by the C++ run machine when a queued user steering
  message is consumed at a stage/attempt boundary. Fields: `text` (the steering text).
  The machine folds the text into the request as `[User steering] <text>` so the next
  provider call sees it. Queue side: `agent_steer {run_id, text}` RPC (contracts/worker_rpc.md
  §AI) → `{ok, steered}`; cooperative like `agent_cancel` — never interrupts an in-flight
  provider HTTP call; unknown/finished run_id → `steered:false`. Consumers tolerate unknown
  kinds (conformance §5).

## No online classification step (2026-09-03, superseding)
- `agent.classification`, `task_route` and `complexity` are no longer produced by the ONLINE
  lane and have no GUI consumer: the Routing card (rowKind 10), the session scope
  color-code and `agent_events.classification_event` were removed. A chat send is one
  Chudcode `general_agent` turn; the model decides inline/delegate/mission itself. The native
  offline pipeline (`core/agent/run.cpp`, `micro.cpp`) may still emit `agent.classification`
  for its own stage structure; consumers MUST tolerate it exactly as any unknown kind (§5).
- Copilot modes are Agent | Plan | Ask only (`CopilotMode` 0/1/2). `swarm`/`edit`/`eng_firm`
  remain valid `options.mode` values for programmatic callers (the mode contract is unchanged)
  but the GUI never sends them; the plan-approve card resends `approved_plan` with
  `mode=agent` only.
- `options.scope {doc_id, feature_ids?, selection?}` is sent in EVERY mode whenever a document
  is open. It is editor CONTEXT (rendered into the user message as ACTIVE DOCUMENT), not an
  edit-targeting restriction.

## Inter-agent messages (2026-10-08, additive)
- `agent.message` (new kind): display-only mirror of agent-to-agent traffic so the main chat
  shows it inline. Fields: `node_id`, `id`, `channel` (`irc` | `pool` | `board`), `from`, `to`
  (`Main` for a message delivered to the main agent; `board` for a board post), `text` (the
  message body, <=4096; never the prompt wrapper Chudcode renders around it), optional
  `message_id`, `reply_to`, `mode` (workpool card mode), `run_id`, `actor`.
- Producers: `services/agent/chudcode_events.py` maps Chudcode `irc_message` frames
  (`irc:incoming` / `irc:relay` / `irc:workpool` records) — previously a `sub_run`
  "Message from <agent>" row whose body never rendered; `services/agent/coordination.py`
  `CoordinationBoard.post_progress` forwards each board post (`channel=board`) to the board's
  `on_event` sink (`get_board(run_id, on_event=...)`), else the turn's ambient live sink.
  Forwarding never affects delivery or the post.
- GUI: `copilot/feed.cpp` folds each into one compact transcript line (rowKind 7, stage
  `msg:<channel>:<message_id|id>`, deduped by that key); `ai/agent_dag.cpp` records a Note step
  on the sender when the trace knows it, else on the event's owner.

## Current producer ownership

The online producer is the Chudcode session host and `services/agent/worker_rpc.py`.
The offline-safe producer is `core/agent/run.cpp`; `services/agent/agent_turn.py`
retains the explicit Python provider loop. References above to `services.agent.loop`
are historical implementation pointers, not an active module or wire requirement.

## Online steer delivery (append-only, 2026-10-11)

- `agent.steer {steer_id, status}` (Chudcode lane, mapped from the session host's
  `agent_steer` frame by `services/agent/chudcode_events.py`). `status`:
  - `delivered` — the steer's user message streamed into the turn (the model has it), or a
    message a Stop withdrew opened the conversation's next prompt (emitted after that turn's
    `agent_start`).
  - `carried` — still in Chudcode's queue when the turn ended; withdrawn so Chudcode's settle
    drain cannot answer it in an unobserved turn. It opens the conversation's next prompt.
- `steer_id` is the GUI's correlation id from `agent_steer.steer_id`; steers sent without one
  emit no event. Distinct from the C++ run machine's `agent_steer` kind above.
- GUI: `copilot/feed.cpp::feedSteerEvent` moves the user bubble's `steerState`
  (`app_state.h` `SteerState`), drawn as a caption under the bubble (`chat_log.cpp`).

## Question-reply delivery evidence (append-only, 2026-10-13)

A text-only steer consumed as a pending question's reply is delivered through the tool
result, not a streamed user message. Its raw `agent_answer` frame carries the optional
`steer_id`; the mapper emits both `agent.answer` and correlated `agent.steer
{steer_id, status:"delivered"}`. A reply with images does not acknowledge the whole message
until the original text and images stream into the model context.

Confirmed delivery cannot be downgraded by a late queued/carried/failed control response.
Unknown user frames and a successful Stop are not blanket delivery evidence for other
messages. Legacy saved transcripts may reconcile an exact, unique, image-free resolved
question reply within the same turn; ambiguous or cross-turn matches remain unconfirmed.
