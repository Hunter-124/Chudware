# DAG Run Schema — v0.1 (stub, T-F8)

Defines the shape of a DAG run dict passed to `describe_project(run=...)`.
WS-A / T-A1 will extend this; this stub pins the minimum contract that narration needs.

---

## Top-level run dict

```json
{
  "id":      "run-20260702-001",       // string — run identifier
  "name":    "T-F8 sprint",            // string — human-readable name (optional)
  "project": "AI-CAD",                 // string — project name (optional alias for name)
  "nodes":   [ <NodeRecord>, ... ],    // list of node records (may be empty)
  "budget":  <BudgetRecord>,           // optional — token usage
  "open_questions": [ <Question>, ... ] // list of open question records (may be empty)
}
```

## NodeRecord

```json
{
  "id":             "T-F8",          // string — task/node id
  "task":           "T-F8",          // string — alias for id
  "title":          "describe_assembly…", // string — human label
  "name":           "…",             // string — alias for title
  "status":         "done",          // "todo"|"ready"|"doing"|"review"|"done"|"blocked"|"pending"
  "owner":          "sonnet-kilo",   // string — owning agent/user (optional)
  "blocked_reason": "…",             // string — reason blocked (optional, status=blocked only)
  "reason":         "…"              // string — alias for blocked_reason
}
```

## BudgetRecord

```json
{
  "spent":       123456,   // int — tokens consumed so far  (primary key)
  "cap":         500000,   // int — token cap for this run  (primary key)
  "tokens_used": 123456,   // int — alias for spent
  "tokens_cap":  500000    // int — alias for cap
}
```

## Question

```json
{
  "id":       "Q1",        // string — question id
  "task":     "T-F8",      // string — owning task
  "question": "…",         // string — question text  (primary key)
  "text":     "…"          // string — alias for question
}
```

---

## Extension note

WS-A / T-A1 may add fields to NodeRecord (e.g. `branch`, `commit`, `cost_tokens`,
`started_at`, `finished_at`). The narration layer reads only the fields listed above and
ignores unknown keys — extend freely without breaking `describe_project`.
