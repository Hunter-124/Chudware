## Yield — correlation and deduplication update

I read `agent://DaemonIPC`. **Merge my GWB-01, GWB-07, and GWB-08 into DaemonIPC finding 1**, rather than counting spawn, recovery, and daemon lifecycle variants separately. That changes my original eleven entries to **nine distinct findings**, one of which is the shared administrative-surface finding already owned by DaemonIPC.

### Exact gateway correlation for the merged critical finding

| Boundary step | Current source evidence |
|---|---|
| Authenticated account/cloud entitlement required | `Webservice/gateway/engine_bridge.py:914-943` |
| Document IDs checked, not executable/process/recovery ownership | `engine_bridge.py:1018-1030`; collected names at `:594-632` |
| Explicit method denial | `:1031-1035`: only `agent_admin_turn` and `doc_file_operations` |
| Administrator-only provider names | `:1036-1055`: `agent_provider_*` and six explicit subscription names |
| Path checking | `:215-232,247-280,1072-1088`: dictionary-key heuristic; strings within `argv` are not paths |
| Agent identity/provider rewrite | `:812-910`: does **not** remove or authorize `argv`, `env`, `child_id`, or global recovery selectors |
| Forwarding | `:1207-1210`: records request and writes to shared daemon |
| Response filtering | `:646-701`: filters document-list and document-tagged notifications; does not tenant-filter `agent_list` or `recovery_list` results |

**Environment nuance:** `agent_spawn.env` is not wholly invisible to the recursive walker: a value under a dictionary key coincidentally matching `_PATH_KEYS`, such as uppercase `PATH`, is checked because keys are lowercased. That does **not** make environment overrides safe. Keys such as `HOME`, `PYTHONPATH`, `LD_PRELOAD`, and arbitrary application configuration variables are not recognized. More importantly, arbitrary `argv` already establishes execution without any environment override.

**Recovery nuance:** Agree with DaemonIPC: `recovery_open` is not a gateway mint method. Do not claim it automatically grants later document access. Unauthorized metadata disclosure, orphan consumption, and deletion are independently established.

## Other gateway-specific findings remain distinct

### 1. Multiline frame policy bypass — my GWB-02

Independent root cause from default-allow RPC policy:

- Parse failures become “no method/no paths/no doc IDs”: `engine_bridge.py:247-249,559-565,615-619`.
- The original frame is nevertheless forwarded: `:1190-1209`.
- Native hostd parses each newline separately: `core/hostd/server.cpp:623-645`.

A strict allowlist alone will not fix this if unknown/invalid envelopes still fall through. Unmatched responses are dropped, but side effects execute.

**Safe reproduction:** fake daemon only; send a single frame containing a forged `hello` and the explicitly blocked `doc_file_operations` request, then inspect recorded calls. No real file operation necessary.

### 2. NUL/normalization file-jail bypass — my GWB-03

- Authorization uses normalized path but forwards original: `engine_bridge.py:252-265`.
- `agent_b64_file` sink: `core/api/libchudware.cpp:1305-1309`.
- Native string reaches `std::ifstream`: `core/agent/providers.cpp:184-188`.

Fixture payload:
```python
"/tmp/audit/victim.txt\0/../own/probe"
```
Gateway authorizes `/tmp/audit/own/probe`; native opening uses the prefix ending at `victim.txt`.

**Safe reproduction:** temporary marker file only, isolated native handler. No existing host file.

### 3. `run_id` filesystem escape — my GWB-04

- `run_id` is not in gateway path keys: `engine_bridge.py:215-229`.
- `agent_run_events_read` joins it directly: `libchudware.cpp:1742-1756`.
- Write helpers construct `RunEvents`: `:1718-1740`.
- Absolute/traversal run IDs select a directory whose `events.jsonl` is created/truncated: `core/agent/events.cpp:633-644,664-674`.

This survives fixing multiline framing and blocking administrative process methods unless these helper RPCs or path semantics are also addressed.

### 4. Shared provider-management aliases — my GWB-05

This is the main **global worker method** correlation:

```text
non-admin /engine
  → chudcode_remove_key / chudcode_set_key / chudcode_logout
  → worker_rpc ADMIN_METHODS registration
  → process-global ChudcodeSessionHost.transient_call
  → shared no-session client
  → typed provider credential command
```

Evidence:
- Registration: `services/agent/worker_rpc.py:3834-3840`.
- Dispatch: `:1894-1921,2109-2145`.
- Shared client: `services/agent/chudcode_host.py:2496-2524`, specifically `_make_client({}, no_session=True)`.
- Invocation: `:2607-2623,2919-2939`.
- Shapes contain provider/account selectors, not tenant identity: `:247-253`.
- Gateway does not classify `chudcode_*` as provider administration: `engine_bridge.py:1036-1055`.

**Important distinction:** Ordinary web-turn credential isolation exists and is not disproved by this finding. These management calls use a **different, shared service client**.

**Safe reproduction:** recorder implementing `remove_api_key`; route `chudcode_remove_key {"providerId":"openai"}` through a non-admin fixture. Do not invoke real credentials or logout.

### 5. Session/run/subagent ownership — my GWB-06

Concrete enumeration removes the need to assume guessed opaque session IDs:

- `subagent_list {}` omits optional scope and enumerates global registry:
  - `services/agent/subagent_rpc.py:51-76,93-115`
  - `services/agent/subagent_registry.py:415-445`.
- Returned metadata includes parent run/session IDs and project/transcript paths: `subagent_registry.py:358-376`.
- Transcript read and steering accept the returned subagent ID without mandatory scope:
  - `subagent_rpc.py:120-173`.
- Known session IDs route to their owning worker without tenant comparison:
  - `core/hostd/worker_bridge.cpp:174-192`.
- `chudcode_close_session` closes the selected session:
  - `worker_rpc.py:2425-2435`
  - `chudcode_host.py:1417-1436`.
- Known run IDs route cancel/steer/answer through hostd’s authenticated control channel:
  - `server.cpp:1029-1043`
  - `worker_bridge.cpp:614-629,660-694`.

The worker control token authenticates **hostd to worker**, not **web tenant to run**. Hostd supplies that token on behalf of a requester whose run ownership was never established.

**Safe reproduction:** temporary registry containing two dummy customer records and marker transcripts; invoke list/read without optional scope. Use recorders for live close/cancel.

### 6. Pending-request memory leak — my GWB-09

- Gateway stores non-null string IDs indefinitely: `engine_bridge.py:1188-1208`.
- Native daemon substitutes `-1` for noninteger IDs: `server.cpp:629-631`.
- Returned `-1` does not remove the stored string key: `engine_bridge.py:668-670`.

Use three small requests to demonstrate retained entries; no flood. Existing gateway fake daemon echoes string IDs, so it masks this native/gateway mismatch.

### 7. Passive revocation gap — my GWB-10

- Session/account/team revalidation occurs only on inbound messages: `engine_bridge.py:985-1002`.
- Outbound forwarding trusts existing document capabilities: `:660-667,1212-1223`.
- Auth helper explicitly promises next-inbound-frame enforcement: `Webservice/gateway/auth.py:611-618`.

A revoked team observer can remain silent and continue receiving shared document updates. Reproduce with fake broadcasts and temporary team/account fixtures.

### 8. Native saves bypass aggregate quotas — my GWB-11

- Gateway quota functions: `Webservice/gateway/limits.py:180-206`; not called by bridge.
- Allowed owned-document/in-root `doc_save` reaches:
  - `core/api/libchudware.cpp:3205-3208`
  - `core/doc/session.cpp:16170-16226`
  - `core/doc/stepplus.cpp:308-320,373-386`.
- No tenant aggregate storage accounting occurs on this route.

Demonstrate with one tiny saved fixture and an artificially smaller fixture quota—not a disk-filling loop. Production filesystem-quota mitigation remains unavailable deployment evidence.

## Coverage conclusion

The critical process/lifecycle/recovery variants are now deduplicated under DaemonIPC’s finding. Eight additional gateway-specific root causes remain. Shared credential aliases and optional session/subagent scoping are concrete worker-boundary gaps, not speculative consequences of process execution.

**No additional verification or mutations performed.**
