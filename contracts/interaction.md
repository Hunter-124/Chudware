# Contract: Interaction — `ask_user` — v1

> ONE frozen contract for "the agent asks the human". Replaces three ad-hoc mechanisms
> (C++ `clarify`/`ask`, the MCP intake ledger, DAG `gate` nodes) with a single typed
> Q/A envelope, a machine-readable answer schema, one answer ledger, and a small set of
> interchangeable transports. Reference implementation: `services/agent/interaction.py`.
> This is the autonomy-balance keystone: every question carries a *suggested default* so
> the agent can propose a decision (auto-accept the default) instead of stalling or
> silently guessing.

Frozen-seam policy applies (see `README.md`): append-only, integrator sign-off + version
bump to change an existing field. The three legacy mechanisms become *callers* of this
contract (T-F4); this contract does not break them.

## 1. Question kinds

| kind | asks for | kind-specific fields | answer value |
|---|---|---|---|
| `choice` | pick from options | `options: [{value, label?}]`, `multi?: bool=false` | `value` (string) or `[value]` when `multi` |
| `dimension` | a measured quantity | `family: "length"\|"angle"\|"count"`, `min?`, `max?` | **typed** `{value: number, unit: string}` — never a bare float |
| `text` | free text | `multiline?: bool=false` | string |
| `file_request` | files/images from the user | `media: "file"\|"image"`, `accept?: [ext]`, `multiple?: bool=false` | `{paths: [string]}` |
| `approval` | approve a proposed action | `proposal: string` | bool (`true` = approved) |

## 2. Question object

```json
{
  "id": "output_shaft.dia",     // stable key (snake_case / dotted), unique within a batch
  "kind": "dimension",
  "prompt": "Output shaft diameter?",  // human question (legacy field name: "ask")
  "suggested": {"value": 8.0, "unit": "mm"},  // TYPED default, same shape as this kind's answer
  "required": false,            // false ⇒ `suggested` is auto-acceptable under the autonomy dial
  "why": "Drives the bore fit and bearing bore.",   // optional rationale shown to the user
  "family": "length"            // (kind-specific fields inline, per §1)
}
```

- `suggested` is **required non-null** for every question (the `clarify` rule): a question
  the agent cannot propose a default for is not ready to ask. Its JSON shape MUST equal the
  answer value shape for the kind (a `dimension` suggests `{value, unit}`, a `choice`
  suggests a `value`, an `approval` suggests a bool).
- `id` is the ledger key. A resumed agent keys replay on `id`, so `id` MUST be stable
  across re-asks of the same question.

## 3. Batch envelope (question side)

```json
{
  "schema": "aicad.interaction/1",
  "batch_id": "b_2f9c…",         // uuid4; ledger + event correlation key
  "scope": "run",                // "run" | "doc"
  "run_id": "r_…",               // required when scope=run
  "doc_id": "…", "revision": 7,  // required when scope=doc
  "blocking": true,              // true ⇒ the run suspends until answered/defaulted
  "questions": [ /* 1..4 */ ]
}
```

- **≤4 questions per batch** (the proven `clarify` cap). A producer with more splits into
  sequential batches. Building a batch with >4 questions is a contract error.
- `scope` decides where answers persist (§5) and whether a resume replays them.

## 4. Answer envelope

```json
{
  "schema": "aicad.interaction.answers/1",
  "batch_id": "b_2f9c…",
  "answers": {
    "output_shaft.dia": {
      "value": {"value": 8.0, "unit": "mm"},   // raw, as the kind's answer shape (§1)
      "canonical": {"value": 8.0, "unit": "mm"},// dimension only: normalized (§6)
      "source": "user",        // "user" | "default" | "resumed"
      "at": "2026-07-02T18:20:05Z"
    }
  }
}
```

- Every answer carries provenance: `source` (`user` = the human answered; `default` = the
  autonomy dial auto-accepted `suggested`; `resumed` = replayed from the ledger) and `at`.
- `dimension` answers MUST additionally carry `canonical` — the value normalized to the
  canonical unit for its family (§6). Callers read `canonical`, never the raw `value`.
- Unknown answer keys and extra fields MUST be tolerated (skip), per the events.md rule.

## 5. Persistence & replay (the resume guarantee)

Every asked question and every answer persists **before** the answer is returned to the
caller, so a resumed or handed-off agent NEVER re-asks a resolved question.

- **`scope: run`** → append to `<cache>/runs/<run_id>/interaction.jsonl`, one JSON record
  per line: `{"t": "question"|"answer", <object>}`. `<cache>` is resolved the same way the
  C++ run machine resolves it (`core/agent/paths.cpp` — `%APPDATA%/AI-CAD/cache` on Windows,
  `$XDG_DATA_HOME|~/.local/share`/`AI-CAD/cache` on Linux/macOS).
- **`scope: doc`** → the document's intake ledger (`IntakeState`,
  `engine_oracle/intake/intake.py`), round-tripped as JSON into the `.step+` record like
  today's `confirm_dimensions`. Same records; different store.
- **Replay:** before a batch is presented, the core loads the ledger and drops every
  question whose `id` already has an answer, re-emitting the stored answer with
  `source: "resumed"`. If the whole batch is already answered, no transport is invoked.

## 6. Unit normalization (typed dimensions)

Dimension answers are `{value, unit}` and are normalized to a canonical unit per family —
canonical units are the repo-wide invariant (mm / deg / count, per `stepplus.md` §3). No
prior normalizer existed (intake hard-coded mm); `interaction.py` provides `normalize_dimension`:

| family | canonical | accepted input units |
|---|---|---|
| length | `mm` | `mm, cm, m, um, µm, in, inch, ", mil, thou, ft, '` |
| angle | `deg` | `deg, °, rad` |
| count | `count` | `count, ""` (dimensionless, pass-through) |

An unknown or family-mismatched unit is a rejected answer (the transport re-asks); it is
never silently coerced.

## 7. Events

Q/A ride the existing `events.jsonl` envelope (`events.md`) with two new kinds, under the
persist-then-callback ordering contract (`core/agent/events.cpp`): the record is written to
`interaction.jsonl` **before** the event callback fires.

| kind | data |
|---|---|
| `ask_question` | `{batch_id, id, kind, prompt, suggested, scope, blocking}` |
| `ask_answer` | `{batch_id, id, value, canonical?, source}` |

## 8. Transports

One contract, many surfaces. A transport implements `ask(batch) -> {id: raw_answer}` and
nothing else; normalization, provenance, persistence, replay, and defaulting live in the
core, not the transport.

| transport | surface | mechanism |
|---|---|---|
| in-app dialog | desktop GUI | ImGui PropertyManager-style modal form built from the batch (`ui/abstract` form model + `ui/imgui` component) |
| MCP elicitation | external MCP clients | `mcp` 1.27.2 `Context.elicit(message, schema)` — the client prompts its own user (spec-stable; NOT sampling) |
| CLI | headless / CI | stdin/stdout prompts |
| DAG gate | orchestrator | WS-A `interrupt()` node raises a batch (future caller, T-A3) |

## 9. Proactive path (verify → ask, don't retry)

A verify step that fails MAY raise an `approval` or `choice` batch with evidence attached
instead of silently retrying. The question carries an optional `evidence` object:

```json
{"evidence": {"message": "Wall 0.6 mm < 0.8 mm min for the chosen process",
              "render": "<cache>/runs/r_…/verify_42.png",
              "numbers": {"min_wall_mm": 0.8, "measured_mm": 0.6}}}
```

The autonomy dial decides whether such a batch blocks or auto-accepts its default.

## 10. Rules

- Producers build questions with typed `suggested` defaults; a batch with a null
  `suggested` or >4 questions is a contract error.
- Consumers MUST tolerate unknown question kinds, unknown answer keys, and extra fields.
- `dimension` callers read `answers[id].canonical`, never the raw `value`.
- Persist-then-return: an answer is durable before it reaches the caller.
- Transports are dumb: they never persist, normalize, or default — the core owns that.
- Contract is append-only; new kinds get a new row in §1 + a version note, never a
  redefinition of an existing kind.

## 11. Worked example

Agent needs two numbers before it can build; runs headless (CLI transport), then the same
batch replays for free on resume.

```json
// batch (blocking, run-scoped)
{"schema":"aicad.interaction/1","batch_id":"b_1","scope":"run","run_id":"r_9",
 "blocking":true,"questions":[
   {"id":"bore.dia","kind":"dimension","family":"length","prompt":"Bore diameter?",
    "suggested":{"value":10,"unit":"mm"},"required":true,"why":"Sets the bearing."},
   {"id":"finish","kind":"choice","prompt":"Surface finish?","required":false,
    "options":[{"value":"as_machined"},{"value":"anodized"}],
    "suggested":"as_machined"}]}

// answers (user typed 0.5 in for the bore; accepted the finish default)
{"schema":"aicad.interaction.answers/1","batch_id":"b_1","answers":{
  "bore.dia":{"value":{"value":0.5,"unit":"in"},
              "canonical":{"value":12.7,"unit":"mm"},"source":"user","at":"…"},
  "finish":{"value":"as_machined","source":"default","at":"…"}}}
```
