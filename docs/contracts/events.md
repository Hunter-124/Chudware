# Contract: Event Stream — v2

> Envelope (unchanged): `{"id": <request id>, "event": <kind>, "data": {…}}`, emitted before the
> final response of the request that triggered them. GUI drains on the main thread per frame
> (existing mechanism).

## 1. Legacy kinds (frozen)

`status {phase, text?}` · `iteration {index, ok, done, notes, thinking, code, diff, dims, error,
contact_sheet, stl_path, is_assembly, parts, fit}` · `plan {plan, raw}` — emitted by
`agent_design`; unchanged until M9 retires nothing (legacy path stays).

## 2. Document & simulation

| kind | data |
|---|---|
| `doc_progress` | `{stage: "rebuild"\|"tessellate", feature_id?, index?, total?, pct}` — emitted when a rebuild exceeds ~300 ms |
| `sim_progress` | `{job_id, stage: "voxelize"\|"mesh"\|"assemble"\|"solve"\|"stream"\|"post", pct, text, trace_point?: {step, drag}}` |

## 3. Orchestrator hierarchy (M9)

| kind | data |
|---|---|
| `run_started` | `{run_id, request, doc_id, stages:[names]}` |
| `stage_started` | `{run_id, stage, role, attempt, budget}` |
| `agent_call` | `{run_id, stage, role, attempt, prompt_file, prompt_hash, context_chars, context_tokens_est, inputs_digest}` |
| `agent_output` | `{run_id, stage, role, attempt, raw (≤16 KB, full text on disk), parsed_ok, parse_error?, ops_count?, duration_ms, tokens?:{in, out}}` |
| `stage_result` | `{run_id, stage, attempt, verdict: "pass"\|"retry"\|"fail"\|"skip", reason}` |
| `run_finished` | `{run_id, status: "done"\|"failed"\|"cancelled", stop_reason, revision}` |

Every orchestrator event is ALSO appended to
`%APPDATA%\AI-CAD\cache\runs\<run_id>\events.jsonl` (one JSON per line, uncapped raw in
sidecar files `attempt_<stage>_<n>.txt`) — the Activity panel replays from this after restart,
and tests assert against it.

## 4. Rules

- Events are best-effort UI hints; the FINAL response is the source of truth for state.
- Producers MUST NOT emit events after their final response line.
- Consumers MUST tolerate unknown kinds and unknown data keys (skip).
- `pct` is 0–100 int; omit when indeterminate.
