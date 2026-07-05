# T-W7 — role cards + playbooks: lead flags (opus-vint)

All 13 authored role cards loaded VERBATIM. No prompt wording changed, and NO
mechanical field defects were found — every card validated clean (schema, stage
vocabulary, allowlist ≤14, handoff schema compiles). Two items below are lead
decisions, NOT things a driver may fix, so they are flagged here rather than edited.

## Flag 1 (lead decision): cam_programmer prompt cites `tap_drill`, not in its allowlist

The `cam_programmer` system prompt instructs: "Tapped holes: tap_drill sizes the
pilot, never arithmetic from memory." But `tap_drill` is **not** in
`cam_programmer.tool_allowlist`, which is already at the 14-tool cap (contract §8
`max_tools_per_role`). A driver cannot add a 15th tool (cap) and must not rewrite the
prompt (verbatim policy), so this is a lead decision:

- (a) drop one existing cam_programmer tool to make room for `tap_drill`, or
- (b) have the foreman/advisory supply the pilot-drill size as a computed number in
  the [ADVISORY] block (then the prompt's intent is satisfied without a model-callable
  tool), or
- (c) accept the prompt as aspirational and leave the allowlist as authored.

The `tap_drill` MCP tool DOES exist and resolves. This is purely an allowlist-vs-prompt
mismatch. The `cam_job` playbook deliberately keeps `tap_drill` out of its step
`helper_tools` (helpers ⊆ allowlist model) pending this decision — see the note in
`services/agent/playbooks/cam_job.yaml`.

Recommended: (b) — the advisory-number path fits the "numbers are precomputed" §3.5
discipline and keeps the allowlist at its authored shape.

## Flag 2 (feature-flagged tools, no action needed): `delegate`, `workspace_status`

`orchestrator` and `overseer` allowlist `delegate` and/or `workspace_status`, which do
not yet resolve to a registered MCP tool — they land with sibling W tasks (delegation
tree T-W14 / workspace status). The loader reports these as `flag`, not `error`, so the
cards load cleanly today; when those tools register, the flags disappear with no card
change. No lead action required — recorded for traceability.
