# services/agent — Chudcode session host, CAD host tools, roles/missions/governance

Online agent turns are services-worker `agent_admin_turn` calls (`worker_rpc.py`
`_agent_admin_turn`). They are driven by `chudcode_host.ChudcodeSessionHost` over the
persistent Chudcode RPC session, and Chudcode owns providers, auth, models, history,
compaction and nested agents. This package owns the CAD side:
- role/mission/council context;
- the host-tool projection (`chudcode_tools.build_host_tools`) and event mapping
  (`chudcode_events`);
- the private control lane (`chudcode_control`);
- guardrails, the progress watchdog and the subagent registry.

Native `core/agent/run.cpp` is only the explicit offline-safe local-modeling fallback.
`agent_turn.run_turn` is explicit-offline. Online tool-internal ECAD/regen model steps
use `providers.chudcode_provider.helper_provider` → `ChudcodeSessionHost.complete_text`,
with scoped settings/credentials/cancellation inherited during host-tool dispatch
(governed role/config selections otherwise), and no helper tools or memory.
Offline/local-only helpers require an offline-safe provider, without online
fallback; other SDK adapters retain explicitly bound standalone/bench uses.

## Canonical guidance
- `docs/dev/chudcode-integration.md` — authority boundary, topology, files by
  responsibility, session/tool/event/recovery rules. Read it before touching sessions,
  providers, auth/models, memory, host tools, events or recovery.
- `docs/dev/engineering-notes.md` — repo rules, worker lanes, prompt-override isolation,
  persistence/save invariants.

## Entry points
- `pytest services/agent -q` (service venv) — the package suite.
- `scripts/run_mission.py` — a headless real turn through the same path.

## Contracts
`contracts/worker_rpc.md` (incl. worker lanes), `contracts/agent_events.md` +
`agent_events.json`, `contracts/events.md`, `contracts/copilot_modes.json`.
