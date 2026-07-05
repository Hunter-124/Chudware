---
id: T-R1
title: PLM v1 - OSS survey + freeze contracts/plm.md (vault schema, lifecycle, lock/remote protocol)
workstream: R
owner: unassigned
status: todo
branch: task/T-R1-plm-contract
depends_on: []
lane: plm-projects
kind: new
priority: 3
---

## Context

HUNTER DIRECTIVE (2026-07-03): plan and build PLM version control for user documents
with **ease of use as the #1 priority** — users never learn version control; manual
controls primary, AI assist behind them. Full design:
[docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md) (Pillar 19).
Supersedes T-BL15/T-BL10. This task freezes the contract before any code
(cross-cutting rule 1) and satisfies the OSS-first directive before we write a
storage engine.

Decided direction (do not relitigate; refine details only): snapshot-based on the
T-H2 canonical serialization (`contracts/doc_schema.md` Appendix A), per-project
hidden git vault at `.aicadvault/`, containers exploded on check-in
(document.json/meta.json/thumbnail per item; geometry.step only at releases),
three user nouns (Project/History/Checkpoint+Release), fixed lifecycle
In work → Released → Superseded, advisory locks + no-merge team v1.

## Files / areas touched

- OSS survey (docs/ note or section in the plan doc): git binding choice —
  subprocess git vs pygit2/libgit2 vs dulwich (license guardrails per
  CONTRIBUTING.md: MIT/BSD/Apache in-process, GPL subprocess-only, AGPL never);
  LFS/locking prior art; existing CAD-PDM art (Ondsel/FreeCAD PDM, DVC, git-lfs
  locks). Record verdicts + rationale.
- contracts/plm.md — NEW, freeze `aicad.plm/1`: manifest.json schema (item
  registry, part numbers, rev letters, lifecycle, release pins, BOM freeze),
  shadow-tree layout, history-entry semantics (auto/checkpoint/release/restore,
  user vs AI attribution), lock manifest + atomic-push CAS protocol, remote
  profile kinds (folder/ssh/https git; `aicad-cloud` kind RESERVED config-only
  per Pillar-12 human gate), plm_* MCP tool surface (names + envelopes).
- docs/PLM_VERSION_CONTROL_PLAN.md — update with survey verdicts; mark contract
  frozen.

## Definition of done

- [ ] OSS survey recorded with license verdicts; git-binding decision made with
      rationale (lightweight + cross-platform + Fedora/Windows parity)
- [ ] contracts/plm.md frozen: aicad.plm/1 manifest schema, shadow-tree layout,
      lifecycle, lock/remote protocol, plm_* tool envelopes; changes after this
      require a version bump
- [ ] Ease-of-use invariants written as normative contract text: no user-facing
      git vocabulary, zero-config project init, history never rewritten
      (restore = new entry), AI actions attributed + reversible
- [ ] T-BL15/T-BL10 open questions all answered in the plan doc (they are —
      verify nothing regressed) and both backlog files point here
- [ ] gate + graphify update .
