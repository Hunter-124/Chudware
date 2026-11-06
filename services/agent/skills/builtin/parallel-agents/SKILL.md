---
name: parallel-agents
description: Coordinate parallel agents editing shared CAD documents and integrating their work.
inject: manual
---

# Parallel agents

Independent work can run in one `task` batch. Agents share the project and live document registry; there are no isolated document copies. Give each child its scope, existing doc_ids, units, save paths, datum frames, critical dimensions and shared interfaces. For multi-part designs record bolt patterns, shaft/bore fits, mating faces and clearances in a shared layout contract. New parts return their doc_id.

Pass doc_id explicitly on every document-scoped CAD call. Do not switch the user's viewport to select a part or assume another agent's current document. Same-part batches apply atomically in arrival order, not as a semantic merge. Read the latest state before dependent edits and sequence prerequisites. Verify combined geometry after its writers finish; do not run assembly motion or interference sweeps while contributors are still mutating it.

Children do not see this conversation. Include the relevant contract, tools, skill names and checked/awaited CAD-call instructions in each task. After compaction, recover the runtime roster and task history before replacing a missing handle. A failed lookup or transport disconnect does not prove completion; coordinate a takeover before duplicating work.

Integrate completed, saved contributions into one grounded assembly, mate its joints, solve mates, check motion and interference, and save. Resolve interface mismatches with the contributing agent and repeat integration and verification after dependent edits.
