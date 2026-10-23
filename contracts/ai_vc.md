# Contract: AI-VC (silent agent version control) — v1

Append-only. Maintainer-owned. Producer: `services/plm/ai_vc.py` (+ hooks in
`agent_turn` and `guardrails.take_auto_checkpoint`, `assist.ai_vc_on_mutate`).
Consumers: copilot chat rollback UI, MCP agents, PLM workspace.

Agent/MCP-authored docs carry `authored_by: agent|user|mixed` (an agent edit on a
user doc → mixed). Every successful mutating AI tool call on an agent/mixed doc
silently cuts a deterministic checkpoint (`id = sha256(doc_id, turn_id, op_batch)[:32]`,
idempotent — retries never duplicate). No dialogs, no user prompts, ever: AI-part VC is
automatic policy, user parts and existing PLM vault flows are untouched.

Storage: content-addressed delta chain under `.aicadvault/ai_vc/` — zlib/zstd-compressed
deltas against the previous snapshot, full snapshot every `plm.ai_vc.full_every`
(default 8) for a self-healing chain, content-hash dedup of identical states; at most one
full snapshot held in RAM per checkpoint op. Applies to `.step+` and ECAD text formats.

MCP tools + worker RPCs (identical shapes):
- `plm_timeline({doc_id?}) → {ok, checkpoints:[{id, turn, label, time, features_count}]}`
  (aliases `when`/`features` accepted)
- `plm_rollback({doc_id?, to: checkpoint_id | "turn:N" | "-N"}) →
  {ok, checkpoint_id, restored_turn, content_hash, via}`

Config: `plm.ai_vc: {keep_last: 0 (= keep all in session), full_every: 8, enabled: true}`.

## v2 — prompt/tool-call granularity (2026-07-14)
- Checkpoint BEFORE every user prompt turn starts (`kind:"prompt"`, labeled with the prompt's
  first ~60 chars) and BEFORE every mutating tool call (`kind:"tool"`, labeled with tool name),
  still delta-encoded + deduped by content hash — same storage rules as v1.
- Timeline rows gain: `kind: prompt|tool`, `msg_idx` (chat message ordinal), `call:{turn, i}`.
- `plm_rollback.to` additionally accepts `"msg:<idx>"` (state before that user message) and
  `"call:<turn>:<i>"` (state before that tool call). Rollback also returns `chat_truncate_to`
  (msg_idx) so the chat UI can rewind the transcript consistently.
- Silent invariant unchanged: never prompt the user about agent-authored file handling.
