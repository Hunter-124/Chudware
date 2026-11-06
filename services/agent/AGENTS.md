# services/agent — OMP session host, CAD host tools, roles/missions/governance

Online agent turns are services-worker `agent_admin_turn` calls (`worker_rpc.py`
`_agent_admin_turn`). They are driven by `omp_host.OmpSessionHost` over the
persistent official OMP RPC session, and OMP owns providers, auth, models, history,
compaction and nested agents. This package owns the CAD side:
- role/mission context;
- the host-tool projection (`omp_tools.build_host_tools`) and event mapping
  (`omp_events`);
- the private control lane (`omp_control`) and trusted per-session extension bridge;
- guardrails, the progress watchdog and the subagent registry.

`core/agent/` is provider/auth/HTTP/config helpers only; there is no native agent loop.
`agent_turn.run_turn` is explicit-offline. Online tool-internal ECAD/regen model steps
use `providers.omp_provider.helper_provider` → `OmpSessionHost.complete_text`,
with scoped settings/credentials/cancellation inherited during host-tool dispatch
(governed role/config selections otherwise), and no helper tools or memory.
Offline/local-only helpers require an offline-safe provider, without online
fallback; other SDK adapters retain explicitly bound standalone/bench uses.

## Canonical guidance
- `docs/dev/omp-integration.md` — authority boundary, topology, files by
  responsibility, session/tool/event/recovery rules. Read it before touching sessions,
  providers, auth/models, memory, host tools, events or recovery.
- `docs/dev/engineering-notes.md` — repo rules, worker lanes, prompt-override isolation,
  persistence/save invariants.

## Entry points
- `scripts/testbox.py --suite agent` — isolated agent suite (includes the Copilot UI tests); see [Running tests](../../CONTRIBUTING.md#running-tests). Verify with the suite(s) for the touched area; the full gate is only for releases or breakage hunts.
- `scripts/run_mission.py` — a headless real turn through the same path.

## Contracts
`contracts/worker_rpc.md` (incl. worker lanes), `contracts/agent_events.md` +
`agent_events.json`, `contracts/events.md`, `contracts/copilot_modes.json`.
