---
id: T-H3
title: Requirements layer - freeze aicad.req/1 schema + requirements doc CRUD
workstream: H
owner: unassigned
status: todo
branch: task/T-H3-requirements-schema
day: 11
depends_on: []
---

## Context

Promotes backlog T-BL09 into a contract. Schema `aicad.req/1` is defined in
`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §3.5: stakeholder_needs, requirements with
typed `quantity` (only place numbers live), `verify_by` (named deterministic
verifiers), status ladder proposed→user_confirmed→verified/failed/waived, audit trail,
markdown projection. It is simultaneously the elicitation target (IP-12), the T-F17
design-brief format, the verify-node contract (WS-A), and the eval spec for
spec→sketch benches (T-H8). Keep v1 minimal — no requirements-management UI beyond a
simple editable panel/markdown view.

## Files / areas touched

- `contracts/requirements.md` — new: aicad.req/1 (move schema from the survey doc,
  survey then references the contract)
- `services/` doc handling — requirements doc CRUD ops + persistence alongside/inside
  `.step+` (same posture as studies), markdown projection
- MCP tools: `req_create/req_update/req_status/req_report`
- `work/backlog/T-BL09.md` — mark superseded-by T-H3

## Definition of done

- [ ] aicad.req/1 frozen; every requirement names ≥1 deterministic verifier;
      user_confirmed only via explicit user action (Pillar-9 answer or edit)
- [ ] CRUD MCP tools + markdown projection working; docs persist and round-trip
- [ ] One end-to-end example: brief authored from a chat transcript, requirement
      verified by measure_between, status flips to verified with evidence ref
- [ ] `graphify update .`; gate green

## Driver notes (Fable, 2026-07-02)

- **Schema source:** copy `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §3.5 into
  `contracts/requirements.md` as the normative text, then edit the survey to
  reference the contract (one normative home).
- **Persistence precedent:** studies inside `.step+` (`contracts/stepplus.md` L15
  `studies/<id>.json`) — requirements doc rides the same way (`requirements.json`
  entry) AND standalone `.req.json` for project-level briefs that precede any part.
  Round-trip precedent: intake's `to_dict/from_dict`
  (`engine_oracle/intake/intake.py`).
- **Verifier names must be real:** validate `verify_by[].verifier` against the
  actual MCP tool names (`measure_between`, `fit_report`, `check_interference`,
  `run_fea_static`, …). Keep the valid-verifier list in one constant the T-H4
  battery can later own.
- **Number discipline:** `quantity` is the ONLY place numerals live; `text` is
  prose. Unit field from the same unit vocabulary intake uses (mm/deg/g canonical
  per `contracts/doc_schema.md` §0).
- **Markdown projection:** deterministic, diff-stable ordering (by requirement id);
  it's a VIEW — never parse markdown back.
- **Scope fence:** no elicitation model calls, no UI panel beyond what an MCP demo
  needs, no verify-node integration (WS-A owns that) — schema + CRUD + projection +
  one worked example. Mark `work/backlog/T-BL09.md` superseded (one line in its
  front matter comment, don't delete).
