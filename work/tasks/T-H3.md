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
