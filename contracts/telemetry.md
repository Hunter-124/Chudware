# Contract: Telemetry — AI-Artifact Envelope & Consent Tiers — v1

> **Scope anchor:** extends `contracts/events.md` v2. Defines capture semantics only.

---

## 1. Scope & Principles

This contract governs **what is captured locally** when Chudware produces an AI artifact,
how users react to it, and how semantic document deltas are recorded. It does **not**
specify upload code, scheduling, or staging.

**Principles:**

1. **Capture always; export conditionally.** Every event is written to the local
   `events.jsonl` sidecar regardless of consent tier. Any export enforces tier-based
   filtering (§4). This preserves the user's own replay history.

2. **Persist-then-callback ordering.** The local sidecar write is committed before any
   in-process callback or UI notification fires. If the process crashes after write but
   before callback, the record exists; the callback is lost. This is correct behavior.

3. **No new event system.** These event kinds are appended to the existing
   `contracts/events.md` stream (same `events.jsonl` per run, same drain mechanism).
   Producers follow the same rules: emit before final response, tolerate unknown keys.

4. **Consent stamped at emission.** The emitter reads the resolved consent tier and
   stamps it into every record before writing. Any downstream consumer re-checks; the
   stamp in the record is informational and audit-traceable, not the authoritative gate.

5. **Undo journal is a prerequisite.** `edited` and `undone` action derivations require
   the per-doc undo/ops journal persisted to disk; correction traces cannot be derived
   without it.

---

## 2. Event Kinds

### 2.1 `ai_artifact` — AI Output Record

Emitted once per AI agent turn that produces an output inserted into the document.
The `artifact_id` is a stable UUID assigned at emission time and referenced by all
subsequent action events for this artifact.

#### Wire format (JSONL, one object per line)

```jsonl
{"kind":"ai_artifact", "artifact_id":"a7f3…", "run_id":"…", "insertion_point":"IP-13",
 "model":{"provider":"anthropic","model":"claude-fable-5","adapter":null},
 "prompt_hash":"…", "input_ref":"sidecar path", "output_ref":"sidecar path",
 "base_revision":17, "verifier_scores":{"check_sketch.status":"full","dof":0},
 "latency_ms":8250}
```

#### Field table

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"ai_artifact"` | yes | Event discriminator |
| `artifact_id` | string (UUID) | yes | Stable ID for this artifact; referenced by `ai_artifact_action` and `doc_semantic_diff` |
| `run_id` | string | yes | Orchestrator run that produced this artifact; matches `run_started.run_id` in `events.md` §3 |
| `insertion_point` | string | yes | Insertion-point label (e.g. `"IP-13"`); determines training derivation path |
| `model` | object | yes | See model sub-object below |
| `model.provider` | string | yes | Model provider slug (e.g. `"anthropic"`, `"local"`) |
| `model.model` | string | yes | Model name/ID (e.g. `"claude-fable-5"`) |
| `model.adapter` | string \| null | yes | LoRA/adapter tag if a fine-tuned adapter was active; `null` for base model |
| `prompt_hash` | string (hex) | yes | SHA-256 of the rendered prompt sent to the model (before any scrubbing) |
| `input_ref` | string | yes | Path to sidecar file containing the full prompt/context; relative to the run's sidecar directory |
| `output_ref` | string | yes | Path to sidecar file containing the raw model output (ops JSON or text); relative to the run's sidecar directory |
| `base_revision` | int | yes | Document revision number at the time the artifact was generated (before application) |
| `verifier_scores` | object | yes | Map of verifier name → score. Keys are verifier-specific (e.g. `"check_sketch.status"`, `"dof"`). Empty object `{}` if no verifiers ran |
| `latency_ms` | int | yes | Wall-clock time from first model token request to last token received, in milliseconds |
| `consent` | string | yes | Resolved consent tier at emission time: `"off"` \| `"meta"` \| `"full"` (stamped by emitter; see §4) |

#### Validation rules

- `artifact_id` must be unique within a session. Duplicate IDs are a producer bug.
- `run_id` must match an already-emitted `run_started` event in the same stream.
- `base_revision` must equal the document's current revision counter at emission time.
- `verifier_scores` values: strings for status enums, ints/floats for numeric scores.
- `latency_ms` must be ≥ 0. Values > 300 000 (5 min) should be logged as warnings.

---

### 2.2 `ai_artifact_action` — User Reaction Event

Emitted each time the user takes an explicit action on an artifact. Multiple action
events may follow a single `ai_artifact`. The sequence of actions for one artifact
is the reaction trace used for preference-pair and correction-trace derivation.

#### Wire format

```jsonl
{"kind":"ai_artifact_action", "artifact_id":"a7f3…",
 "action":"accepted|applied|edited|rejected|regenerated|undone|flagged",
 "revision":18, "edit_ops_ref":"sidecar path (ops the user applied on top)",
 "undo_within_ops":0, "dwell_ms":41000, "note":"optional user text"}
```

#### Field table

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"ai_artifact_action"` | yes | Event discriminator |
| `artifact_id` | string (UUID) | yes | Must match an already-emitted `ai_artifact.artifact_id` |
| `action` | string enum | yes | One of: `accepted`, `applied`, `edited`, `rejected`, `regenerated`, `undone`, `flagged` |
| `revision` | int | yes | Document revision after this action was applied (or current revision if no doc change) |
| `edit_ops_ref` | string \| null | no | Path to sidecar file containing the ops the user applied on top of the AI output (populated only when `action` is `edited`) |
| `undo_within_ops` | int | yes | Number of undo operations the user performed within the K-op window immediately after applying this artifact; 0 if none |
| `dwell_ms` | int | yes | Time in milliseconds between the artifact being presented and this action being taken |
| `note` | string \| null | no | Optional free-text annotation entered by the user (e.g. from a "flag with note" dialog) |
| `consent` | string | yes | Resolved consent tier at emission time (stamped by emitter; see §4) |

#### Valid `action` values

| Value | Meaning |
|---|---|
| `accepted` | User accepted the artifact as-is (no-edit path); doc applied at base_revision+1 |
| `applied` | User explicitly clicked Apply (distinct from passive acceptance in some UX flows) |
| `edited` | User modified the artifact before or after application; `edit_ops_ref` must be populated |
| `rejected` | User dismissed or discarded the artifact; no doc change |
| `regenerated` | User requested a new generation for the same prompt; old artifact is the negative |
| `undone` | User undid the application of the artifact within the K-op undo window |
| `flagged` | User flagged the artifact as incorrect, offensive, or problematic; `note` recommended |

#### Validation rules

- `artifact_id` must reference an existing `ai_artifact` in the same session.
- `edit_ops_ref` must be non-null if and only if `action == "edited"`.
- `undo_within_ops` must be ≥ 0. It counts undo operations, not revision steps.
- `dwell_ms` must be ≥ 0. Values of 0 are valid (instant rejection).
- A single artifact may have at most one `accepted` or `applied` action in its lifetime.
  A second `accepted` after `undone` is valid (re-accept after undo).

---

### 2.3 `doc_semantic_diff` — Feature-Level Document Delta

Emitted when the document transitions through a range of revisions that span an AI
artifact's application and any subsequent user edits. Captures the feature-level diff
(per `contracts/doc_ops.md` §3.3 semantics) as a sidecar reference.

#### Wire format

```jsonl
{"kind":"doc_semantic_diff", "artifact_id":"a7f3…", "from_revision":17,
 "to_revision":21, "diff_ref":"sidecar path (feature-level diff, §3.3)"}
```

#### Field table

| Field | Type | Required | Description |
|---|---|---|---|
| `kind` | `"doc_semantic_diff"` | yes | Event discriminator |
| `artifact_id` | string (UUID) | yes | The artifact whose application initiated this diff span |
| `from_revision` | int | yes | Starting revision (exclusive lower bound; the state before the artifact was applied) |
| `to_revision` | int | yes | Ending revision (inclusive upper bound; final state after all user edits in this trace) |
| `diff_ref` | string | yes | Path to sidecar file containing the feature-level diff (format per `contracts/doc_ops.md` §3.3) |
| `consent` | string | yes | Resolved consent tier at emission time (stamped by emitter; see §4) |

#### Validation rules

- `from_revision` must equal `ai_artifact.base_revision` for the referenced artifact.
- `to_revision` must be > `from_revision`.
- `diff_ref` sidecar must exist on disk at time of emission.

---

## 3. Session Envelope

Every local session directory contains one session envelope
record, written at session open and updated at session close.

#### Wire format

```jsonl
{"consent":"off|meta|full", "origin":"team|external", "app_version":"…", "os":"…"}
```

#### Field table

| Field | Type | Required | Description |
|---|---|---|---|
| `consent` | string enum | yes | Resolved consent tier for this session: `"off"` \| `"meta"` \| `"full"` |
| `origin` | string enum | yes | `"team"` for internal development installs; `"external"` for all other users |
| `app_version` | string | yes | Application version string (semver or build tag) |
| `os` | string | yes | Operating system identifier (e.g. `"win32"`, `"linux"`, `"darwin"`) |

#### Consent tier defaults

| Origin | Default tier |
|---|---|
| `team` | `full` |
| `external` | `off` |

#### Offline / ITAR override

If `offline_enabled` is `true` in the resolved application config, the consent tier
is **hard-forced to `"off"`** and nothing is exported. This override is
applied at config-resolution time, before the session envelope is written. No UI toggle
can override it while `offline_enabled` is active.

---

## 4. Consent Stamping Rules

The emitter resolves and stamps the consent tier into every event
record before writing to the local `events.jsonl`. Any export re-verifies the tier;
the stamp is for audit traceability.

### Tier: `off`

- **Who:** external users (default); any user with `offline_enabled = true` (hard override).
- **Events written locally:** yes — full fidelity, unchanged from current behavior.
- **Export behavior:** no records from this session are exported. Records never leave the device.
- **Fields included in local write:** all fields (no scrubbing at write time).

### Tier: `meta`

- **Who:** users who opt in to meta-level sharing.
- **Events written locally:** yes — full fidelity.
- **Export behavior:** strips before export:
  - `input_ref` and `output_ref` sidecar contents are not uploaded (paths are nulled in export).
  - `edit_ops_ref` sidecar contents are not uploaded.
  - `diff_ref` sidecar contents are not uploaded.
  - `model.adapter` is included (not geometry).
  - `prompt_hash` is included (hash only, not the prompt).
  - `verifier_scores` are included.
  - `note` (free-text user annotation) is stripped.
- **What survives export:** event kinds, `artifact_id`, `run_id`, `insertion_point`,
  `model` object, `prompt_hash`, `base_revision`, `verifier_scores`, `latency_ms`,
  `action`, `revision`, `undo_within_ops`, `dwell_ms`, `from_revision`, `to_revision`,
  `consent`, session envelope.

### Tier: `full`

- **Who:** internal development installs (default); external users who explicitly opt in globally
  **and** have per-project opt-in enabled for the open project.
- **Events written locally:** yes — full fidelity.
- **Export behavior:** exports all fields. Sidecar files are bundled.
- **Scrubbing still applied on export:**
  - API keys, filesystem paths outside the project dir, and usernames are stripped from
    sidecar text content.
  - Machine IDs are hashed (not raw).
  - Attachments (binary blobs in sidecars) are included only if `full` tier is active.
- **Per-project opt-in:** for external users with global `full` consent, each project
  must also carry a per-project consent flag. A project may contain customer IP; global
  consent does not override project-level refusal.

### Consent stamping sequence (per event)

1. Read resolved consent tier from session state (already incorporating offline override).
2. Add `"consent": <tier>` field to the event object.
3. Write to local `events.jsonl` (blocking, fsync before return).
4. Fire in-process callbacks / UI notifications.

---

## 5. Derivation Rules

These derivations are computed mechanically, after capture, from the local
`events.jsonl`. They are not computed at capture time. All derivations require
`consent != "off"` for the session.

### 5.1 SFT Examples

**Trigger:** `ai_artifact` event followed by an `ai_artifact_action` with
`action == "accepted"` or `action == "applied"`, where no subsequent action with
`action == "edited"` exists for the same `artifact_id` within the session.

**Output:** `(input_ref → output_ref)` training pair, tagged with:
- `insertion_point` (determines the task head)
- `verifier_scores` (quality signal for filtering/weighting)
- `base_revision` (document state context)

**Rationale:** Clean accept with no follow-up edits = the model output was good enough
to use verbatim. High-quality SFT signal.

---

### 5.2 Preference Pairs

Three sub-cases, all producing `(rejected_output, chosen_output)` pairs:

**5.2a — Regeneration pairs:**
- Trigger: `ai_artifact_action` with `action == "regenerated"` on artifact A, followed
  by a new `ai_artifact` B for the same `run_id` or logical request, where B receives
  `action == "accepted"` or `"applied"`.
- `rejected_output` = artifact A's `output_ref`.
- `chosen_output` = artifact B's `output_ref`.

**5.2b — Retry-loop pairs:**
- Trigger: In `events.jsonl`, a failed orchestrator attempt (stage with
  `verdict == "fail"` or `"retry"`) followed by a passing attempt (`verdict == "pass"`)
  for the same `run_id` and `stage`.
- `rejected_output` = failed attempt's raw output (from `agent_output.raw` or sidecar).
- `chosen_output` = passing attempt's output.

**5.2c — Undo-negative pairs:**
- Trigger: `ai_artifact_action` with `action == "undone"` and `undo_within_ops > 0`
  (undo occurred within K ops of application).
- The undone artifact's `output_ref` is marked negative.
- Any later artifact for the same logical request that receives `accepted`/`applied`
  becomes the positive (chosen).
- K (the undo-window op count threshold) is a tunable derivation parameter; default = 5.

---

### 5.3 Correction Traces

**Trigger:** `ai_artifact_action` with `action == "edited"` and `edit_ops_ref` populated.

**Output:** A `(model_output, human_correction_diff)` pair suitable for DPO/GRPO:
- `model_output`: the artifact's `output_ref` sidecar.
- `human_correction`: the `edit_ops_ref` sidecar (ops the user applied on top of the
  model output, at feature-level granularity per §3.3).
- Tagged with `insertion_point`, `verifier_scores`, and `base_revision`.

**Prerequisite:** The per-doc undo/ops journal must be persisted to disk (§6). Without
the journal, `edit_ops_ref` cannot be populated and this derivation produces no output.

---

### 5.4 Elicitation Gold

**Trigger:** Design-brief sign-off event (`mission.brief.signed`, `contracts/agent_events.md` §4).

**Output:** `(model_draft_ref, user_signed_brief_ref)` diff pair:
- `model_draft_ref`: the `output_ref` of the IP-12 elicitation artifact.
- `user_signed_brief_ref`: the path to the signed/finalized brief document.
- The diff is computed at the slot/field level (requirement IDs, ranges, unit fields).

**Intake gold augmentation:** Ledger confirmation records gain structured provenance
fields: `{who: string, ts: ISO-8601, superseded_value: any, reason?: string}`.
These are appended to the confirmation event at the time the user confirms or rejects
an intake claim, before the record is written to `events.jsonl`.

---

## 6. Undo Journal Persistence

The derivations in §5.3 and §5.2c require a bounded, per-doc undo/ops journal
persisted to disk alongside the document. This section defines the persistence contract.

### Storage location

- The journal is a **sidecar file** adjacent to the `.step+` container for the document.
- It is **not** stored inside the `.step+` container (do not write to `document.json`
  or `meta.json` inside the container).
- Naming convention: `<document-stem>.undo_journal.jsonl` (one ops record per line).

### Bounds

| Document type | Max snapshots |
|---|---|
| CAM job | 50 snapshots |
| ECAD (schematic / PCB) | 100 snapshots |
| Part / assembly | Part revision counter (unbounded by snapshot count; bounded by revision depth) |

When the snapshot count exceeds the bound, the oldest snapshot is evicted (FIFO).
Evicted snapshots that are referenced by an unexported `edit_ops_ref` or
`undo_within_ops` event should be flagged in the event as `journal_evicted: true`
to signal that the correction trace is incomplete.

### Contents

Each journal entry is a JSONL record:

```jsonl
{"revision": 18, "ops": [...], "ts": "2026-07-02T14:32:00Z", "artifact_id": "a7f3…"}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `revision` | int | yes | Document revision this op set was applied at |
| `ops` | array | yes | Ordered list of doc-ops (per `contracts/doc_ops.md`) applied in this step |
| `ts` | string (ISO-8601) | yes | Wall-clock timestamp of this journal entry |
| `artifact_id` | string \| null | yes | The AI artifact that produced these ops, if any; `null` for user-originated ops |

### Write ordering

Journal entry is written to disk before the document revision counter is incremented.
This ensures that a crash after journal write but before revision increment leaves the
journal consistent (the entry exists but the revision did not advance; on recovery,
discard the unmatched journal tail).

---

## 7. Implementation Notes

### 7.1 Relationship to `contracts/events.md`

This contract **extends** `contracts/events.md` v2. The three new event kinds
(`ai_artifact`, `ai_artifact_action`, `doc_semantic_diff`) are appended to the existing
`events.jsonl` stream. All rules from `contracts/events.md` §4 apply:

- Producers must not emit events after their final response line.
- Consumers must tolerate unknown kinds and unknown data keys (skip).
- `pct`, `id`, `event` envelope fields from the existing stream are not used by these
  new kinds; they emit as bare JSONL objects (same file, different object shape).

### 7.2 Persist-then-callback ordering

The local sidecar write (`fsync`) completes before any in-process callback or UI
notification is fired. This is the same guarantee already required by the existing
orchestrator events (`run_started`, `agent_output`, etc.). No new mechanism is needed;
the existing write path must be used for these new event kinds.

### 7.3 Capture only

This contract defines **capture semantics only**. Nothing in this contract implies any
network call or background process.

### 7.4 No new event system

These event kinds ride the existing `events.jsonl` mechanism. Do not introduce a
parallel file, queue, or IPC channel for telemetry events. The Activity panel's existing
replay-from-file logic covers `ai_artifact` and `ai_artifact_action` without
modification (it already tolerates unknown kinds per `contracts/events.md` §4).

### 7.5 Sidecar directory convention

Sidecar files referenced by `input_ref`, `output_ref`, `edit_ops_ref`, and `diff_ref`
are stored in the run's sidecar directory:

```
%APPDATA%/Chudware/cache/runs/<run_id>/
  events.jsonl
  attempt_<stage>_<n>.txt        ← existing (contracts/events.md §3)
  artifact_<artifact_id>_input.jsonl
  artifact_<artifact_id>_output.jsonl
  artifact_<artifact_id>_edit_ops.jsonl
  artifact_<artifact_id>_diff.jsonl
```

Paths stored in `*_ref` fields are relative to the run directory; consumers resolve them
against the run directory.

### 7.6 Timestamps

All `ts` fields use ISO-8601 UTC (`2026-07-02T14:32:00Z`). The `events.jsonl` stream
does not currently mandate timestamps on event records; telemetry events should include
a `ts` field even though the base contract does not require it.

### 7.7 Provenance field requirement

Every training record derived per §5 must carry a `provenance` field:

```json
{"provenance": {"source": "telemetry", "session_id": "…", "artifact_id": "…",
                "consent": "full", "app_version": "…", "ts_derived": "…"}}
```

Legal cleanliness constraint: corpus = consented telemetry
+ synthetic factory + licensed/OSS datasets; every record must be traceable to its
consent grant.
