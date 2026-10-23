# Contract: Requirements layer — `aicad.req/1`

> Normative v1.
> Changes require a schema-string bump and a migration note here.
> Consumed by: elicitation (insertion point `IP-12`), design-brief sign-off, DAG verify
> nodes, and spec→sketch/part benchmarks.

---

## 0. Conventions

- All numbers live **exclusively** in `quantity` fields. The `text` field is prose only — never
  embed numerals in text.
- Units follow the canonical vocabulary from `contracts/doc_schema.md §0`:
  `mm`, `deg`, `g` (mass), `N`, `MPa`, `mm/s`, `mm²`, `mm³`, `€`, `USD`, `—` (dimensionless).
- IDs are short opaque strings: needs `N1…`, requirements `R1…`, interfaces `IF1…`,
  assumptions `AS1…`, open questions `OQ1…`. Document `id` is a UUID4 string.
- `audit` is append-only; producers never delete or reorder entries.

---

## 1. Schema — `aicad.req/1`

```json
{
  "schema": "aicad.req/1",
  "id": "<uuid4>",
  "title": "Human-readable title of the brief",
  "revision": 1,
  "created": "<iso8601>",
  "modified": "<iso8601>",
  "stakeholder_needs": [
    {
      "id": "N1",
      "text": "Prose description of the need (no numbers)",
      "source": "chat | doc | import"
    }
  ],
  "requirements": [
    {
      "id": "R1",
      "need": "N1",
      "kind": "dimensional | functional | material | interface | cost | standard | process",
      "text": "Prose statement of the requirement (no numbers)",
      "quantity": {
        "param": "bore_dia",
        "op": "== | >= | <= | range",
        "value": 22.0,
        "unit": "mm",
        "tol": {"plus": 0.0, "minus": 0.013}
      },
      "verify_by": [
        {"verifier": "measure_between", "args": "optional hint string"},
        {"verifier": "fit_report"}
      ],
      "status": "proposed | user_confirmed | verified | failed | waived",
      "trace": {
        "features": ["f3"],
        "study": null,
        "evidence": []
      }
    }
  ],
  "interfaces": [
    {
      "id": "IF1",
      "text": "Interface description (no numbers)"
    }
  ],
  "assumptions": [
    {
      "id": "AS1",
      "text": "Assumption text (no numbers)"
    }
  ],
  "open_questions": [
    {
      "id": "OQ1",
      "text": "Question text"
    }
  ],
  "audit": [
    {
      "who": "user | agent",
      "ts": "<iso8601>",
      "change": "Human-readable description of what changed"
    }
  ]
}
```

---

## 2. Field rules (normative)

### 2.1 `quantity`

- `quantity` is the **only** place numerals live in a requirements document.
- `quantity` MAY be `null` for purely qualitative requirements (e.g. `kind: "standard"`), but at
  least one `verify_by` entry is still required.
- `op` values: `"=="` (exact), `">="` (minimum), `"<="` (maximum), `"range"` (requires both
  `value` and `value_max`).
- `tol` is optional; when present both `plus` and `minus` are non-negative floats in the same unit.

### 2.2 `verify_by`

- Every requirement MUST name ≥1 deterministic verifier.
- `verifier` MUST be a name from the **VALID_VERIFIERS** constant (see §3).
- `args` is an optional hint string passed to the verifier as context; it MUST NOT contain any
  semantics not already captured in `quantity` (it's advisory for the tool call).

### 2.3 `status` ladder

| Status | Who may set it | How |
|---|---|---|
| `proposed` | agent | Initial state; agent populated the requirement from elicitation |
| `user_confirmed` | user only | Requires a Pillar-9 `ask_user` answer OR an explicit user edit |
| `verified` | verifier (automated) | Verifier ran and passed; `trace.evidence` populated |
| `failed` | verifier (automated) | Verifier ran and failed; `trace.evidence` populated |
| `waived` | user only | User explicitly waived the requirement |

Transition rules:
- `proposed` → `user_confirmed`: only on explicit user action.
- `user_confirmed` → `verified` | `failed`: only when a verifier runs and returns evidence.
- Any status → `waived`: only on explicit user action.
- Agents MUST NOT set `user_confirmed` or `waived` autonomously.

### 2.4 `audit`

- Append-only. Every mutation appends one entry: `who` (`"user"` or `"agent"`), `ts` (ISO-8601 UTC),
  `change` (prose summary of what changed).
- `who: "agent"` is appropriate for automated transitions (e.g. `proposed` → `verified`).
- `who: "user"` is appropriate for `user_confirmed`, `waived`, and any user-authored edit.

---

## 3. Valid verifiers (VALID_VERIFIERS)

The canonical list of MCP tool names that may appear in `verify_by[].verifier`.
Owners: this contract defines the list; the verifier battery may extend it via contract amendment.

```
check_sketch
check_feature_overlap
check_interference
check_symmetry
compare_parts
confirm_dimensions
dfm_check
draft_analysis
fit_report
measure_between
measure_part
query_geometry
run_cfd_accurate
run_cfd_flow
run_fea_modal
run_fea_static
solve_mates
wall_thickness
```

---

## 4. Persistence

### 4.1 Inside a `.step+` document

When a requirements doc is associated with a specific part or assembly, it is stored as a
doc-meta key `requirements` (same mechanism as `studies`, `intake`, etc.):

- Persisted via `doc_set_meta(doc_id, "requirements", <dict>)`.
- Retrieved via `doc_get_meta(doc_id, "requirements")`.
- The value is the full `aicad.req/1` JSON object (a dict).

### 4.2 Standalone project-level briefs (`.req.json`)

For project-level briefs that predate any specific part (e.g. a design brief from a chat
transcript), the document is saved as `<name>.req.json` in the project directory.
Format: the full `aicad.req/1` JSON object, UTF-8, pretty-printed (indent=2).

---

## 5. Markdown projection

The markdown projection is a **read-only view** — never parse it back to JSON.
Ordering is deterministic (by requirement ID, lexicographic), diff-stable.

```markdown
# <title>  (rev <revision>)

## Stakeholder Needs
| ID | Need | Source |
|---|---|---|
| N1 | … | chat |

## Requirements
| ID | Need | Kind | Requirement | Quantity | Status |
|---|---|---|---|---|---|
| R1 | N1 | dimensional | … | bore_dia == 22.0 mm ±0.013 | proposed |

## Interfaces
…

## Assumptions
…

## Open Questions
…

## Audit Trail
| When | Who | Change |
|---|---|---|
| … | agent | … |
```

---

## 6. Example — worked end-to-end

See `services/requirements/example.py` for a worked example: a design brief authored from
a chat transcript, `R1` verified by `measure_between`, status transitions to `verified` with
an evidence reference.
