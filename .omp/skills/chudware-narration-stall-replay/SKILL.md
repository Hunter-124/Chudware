---
name: chudware-narration-stall-replay
description: Replay agent narration stalls offline using recorded replies and the real role-turn path.
---

# chudware-narration-stall-replay

Inspect supplied `events.jsonl` records by `kind`; distinguish assistant text/thinking deltas from tool calls and run completion. Context events do not by themselves delimit model replies: reconstruct complete responses before replay. `agent.context.used` is reported context occupancy, so examine its source and any compaction or context replacement before interpreting a drop.

Feed recorded replies to a scripted provider through `services/agent/mission_dispatch.py`'s role-turn entry point, using private fixtures and deterministic tool responses. Assert actual tool dispatch and final outcome, not just narration. Cover the failing and valid response shapes.

Verify: `scripts/testbox.py --suite agent`
