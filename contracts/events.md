# Contract: Event Stream — v2

> Envelope (unchanged): `{"id": <request id>, "event": <kind>, "data": {…}}`, emitted before the
> final response of the request that triggered them. GUI drains on the main thread per frame
> (existing mechanism).

## 1. Legacy kinds (frozen)

`status {phase, text?}` · `iteration {index, ok, done, notes, thinking, code, diff, dims, error,
contact_sheet, stl_path, is_assembly, parts, fit}` · `plan {plan, raw}` — formerly emitted by
`agent_design` (removed 2026-07-22); shapes kept here for historical reference.

## 2. Document & simulation

| kind | data |
|---|---|
| `doc_progress` | `{stage: "rebuild"\|"tessellate", feature_id?, index?, total?, pct}` — emitted when a rebuild exceeds ~300 ms |
| `sim_progress` | `{job_id, stage: "voxelize"\|"mesh"\|"assemble"\|"solve"\|"stream"\|"post", pct, text, trace_point?: {step, drag}}` |

## 3. Orchestrator hierarchy

| kind | data |
|---|---|
| `run_started` | `{run_id, request, doc_id, stages:[names]}` |
| `stage_started` | `{run_id, stage, role, attempt, budget}` |
| `agent_call` | `{run_id, stage, role, attempt, prompt_file, prompt_hash, context_chars, context_tokens_est, inputs_digest}` |
| `agent_output` | `{run_id, stage, role, attempt, raw (≤16 KB, full text on disk), parsed_ok, parse_error?, ops_count?, duration_ms, tokens?:{in, out}}` |
| `stage_result` | `{run_id, stage, attempt, verdict: "pass"\|"retry"\|"fail"\|"skip", reason}` |
| `run_finished` | `{run_id, status: "done"\|"failed"\|"cancelled", stop_reason, revision}` |

Every orchestrator event is ALSO appended to
`%APPDATA%\Chudware\cache\runs\<run_id>\events.jsonl` (one JSON per line, uncapped raw in
sidecar files `attempt_<stage>_<n>.txt`) — the Activity panel replays from this after restart,
and tests assert against it.

## 4. Telemetry — AI artifact capture

Extends the same `events.jsonl` stream. Full schema in `contracts/telemetry.md`.

| kind | data |
|---|---|
| `ai_artifact` | `{artifact_id, insertion_point, model:{provider,model,adapter}, prompt_hash, input_ref, output_ref, base_revision, verifier_scores, latency_ms, consent:"off"\|"meta"\|"full"}` — emitted when an AI output is produced |
| `ai_artifact_action` | `{artifact_id, action:"accepted"\|"applied"\|"edited"\|"rejected"\|"regenerated"\|"undone"\|"flagged", revision, edit_ops_ref?, undo_within_ops, dwell_ms, note?, consent}` — emitted on user reaction |
| `doc_semantic_diff` | `{artifact_id, from_revision, to_revision, diff_ref, consent}` — emitted alongside `edited` action to record feature-level changes |

Rules specific to telemetry events:
- `consent` is stamped by the emitter from `consent_tier(cfg)`.
- `consent:"off"` events are written locally only and are never exported.
- `offline_enabled(cfg) ⇒ consent:"off"` — hard override, checked at config-resolution time.
- `meta` tier: `input_ref`, `output_ref`, `edit_ops_ref` fields must be empty strings.

## 5. Rules

- Events are best-effort UI hints; the FINAL response is the source of truth for state.
- Producers MUST NOT emit events after their final response line.
- Consumers MUST tolerate unknown kinds and unknown data keys (skip).
- `pct` is 0–100 int; omit when indeterminate.


### 5.1 Manufacturing subscription-lane amendment (`MFG-SUB-1`, `MFG-OBS-1`)

The §5 prohibition on post-final-response events remains the default. One explicit exception exists:
an accepted `mfg_device_subscribe` or `mfg_event_subscribe` call establishes a subscription lane. That
lane MAY emit event-envelope lines after the accepting call's final response for as long as the
subscription lives. The accepting response carries `{subscribed:true,from_seq}`; reconnecting
consumers recover durable ordering from `from_seq`/`mfg_event_query`, not from best-effort delivery.

A connection whose first `hello` declares `client:"observer"` may read, query, and establish these
lanes, but MUST reach no write dispatch path, may not own a command lease, and may not issue run or
approval mutations. Unknown event kinds remain skippable.

### 5.2 Manufacturing gateway kinds (`mfg.*`)

| kind | data |
|---|---|
| `mfg.gateway.epoch` | `{epoch,process_start_id,epoch_floor_seq,reconciled:{falsified,superseded,invariant}}` |
| `mfg.device.state` | `{device_id,state:FreshValue<DeviceState>,raw}` |
| `mfg.device.snapshot` | `{device_id,snapshot:FreshValue<ControllerSnapshot>}` |
| `mfg.device.alarm` | `{device_id,alarm:{code,class,text},raw}` |
| `mfg.device.lease` | `{device_id,lease,change:"granted"\|"released"\|"expired"\|"preempted"}` |
| `mfg.device.foreign_client_detected` | `{device_id,evidence}` |
| `mfg.device.capability_changed` | `{device_id,capability:CapabilityReport,downgraded[]}` |
| `mfg.run.state` | `{run_id,state,at_seq,cause}` |
| `mfg.run.gate_report` | `{run_id,gate_report_ref,verdict,rejecting[]}` |
| `mfg.approval.issued` | `{approval_id,run_id,at_seq}` |
| `mfg.approval.burned` | `{approval_id,run_id,initiator,at_seq}` |
| `mfg.approval.revoked` | `{approval_id,run_id?,initiator?,at_seq}` |
| `mfg.result.falsified` | `{class:"falsified",reason,why,affected[],at_seq}` |
| `mfg.result.superseded` | `{class:"superseded",reason,why,affected[],at_seq,superseded_by}` |

All durable seq values originate in the gateway journal. Consumers branch on invalidation `class`,
never on the event name. Invalidation applies only to affected rows with `seq<=at_seq`; falsification
clears, while supersession retains and points forward.